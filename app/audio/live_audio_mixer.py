import math
import os

# Radio workers use elementwise DSP, not threaded BLAS. Keep each independent
# worker from allocating a core-sized thread pool unless the operator opted in.
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')
import numpy as np


class LiveAudioMixer:
    """48 kHz stereo program + mono voice, with smooth gain and linked limiting."""
    _VOICE_THRESHOLD = 2000

    def __init__(self):
        self._auto_duck_factor = 0.0
        self._hold_seconds = 0.0
        self._music_scale = 1.0
        self._limiter_gain = 1.0

    @staticmethod
    def _samples(data):
        return np.frombuffer(data[:len(data) // 2 * 2], dtype='<i2')

    @staticmethod
    def _clamp_sample(value):
        return max(-32768, min(32767, round(value)))

    @staticmethod
    def _normalize_mode(mode):
        return mode if mode in {"normal", "duck", "mute"} else "normal"

    @staticmethod
    def _gain(value, default):
        try:
            number = float(value)
            return max(0.0, min(2.0, number)) if math.isfinite(number) else default
        except (TypeError, ValueError):
            return default

    def mix_pcm_chunk(self, music_pcm, mic_pcm, *, effect_pcm=None,
                      program_music_mode="normal", mic_gain=1.0,
                      music_gain=1.0, duck_level=0.15):
        music = self._samples(music_pcm)
        mic = self._samples(mic_pcm)
        effect = self._samples(effect_pcm or b"")
        frames = max(len(music) // 2, len(mic), len(effect))
        if not frames:
            return b""
        mode = self._normalize_mode(program_music_mode)
        voice_gain = self._gain(mic_gain, 1.0)
        base_gain = self._gain(music_gain, 1.0)
        duck_gain = min(1.0, self._gain(duck_level, 0.15))
        duration = frames / 48000.0
        peak = float(np.abs(mic.astype(np.int32)).max(initial=0)) * voice_gain
        if mode == "normal":
            if peak > self._VOICE_THRESHOLD:
                self._hold_seconds = 1.0
                self._auto_duck_factor += (1 - self._auto_duck_factor) * (1 - math.exp(-duration / 0.025))
            else:
                release = max(0.0, duration - self._hold_seconds)
                self._hold_seconds = max(0.0, self._hold_seconds - duration)
                self._auto_duck_factor *= math.exp(-release / 0.3)
                if self._auto_duck_factor < 0.000001:
                    self._auto_duck_factor = 0.0
            target = base_gain * (1 - self._auto_duck_factor * (1 - duck_gain))
        elif mode == "duck":
            target = base_gain * duck_gain
        else:
            target = 0.0

        # Preserve untouched music byte-for-byte, including its stereo image.
        if (mode == "normal" and target == 1.0 and self._music_scale == 1.0
                and self._limiter_gain == 1.0 and not np.any(mic) and not np.any(effect)
                and frames == len(music) // 2):
            return music_pcm

        smoothing = math.exp(-1 / (48000 * (0.02 if target < self._music_scale else 0.3)))
        gains = target + (self._music_scale - target) * np.power(smoothing, np.arange(1, frames + 1))
        gains[np.abs(gains - target) < 0.000001] = target
        self._music_scale = float(gains[-1])
        output = np.zeros((frames, 2), dtype=np.float64)
        music_frames = len(music) // 2
        output[:music_frames] = music[:music_frames * 2].reshape(-1, 2) * gains[:music_frames, None]
        output[:len(mic)] += mic[:, None] * voice_gain
        output[:len(effect)] += effect[:, None]
        overload = np.maximum(1, np.maximum(output.max(axis=1) / 32767, -output.min(axis=1) / 32768))
        allowed = 1 / overload
        # d[n] = max(1-allowed[n], release*d[n-1]), where d=1-gain.
        # A cumulative maximum computes the exact linked attack/release envelope
        # in native array code. Bound blocks so exponential scaling stays finite.
        release = math.exp(-1 / (48000 * 0.08))
        for start in range(0, frames, 4800):
            end = min(frames, start + 4800)
            decay = np.power(release, np.arange(1, end - start + 1))
            deficits = decay * np.maximum.accumulate(np.maximum((1 - allowed[start:end]) / decay, 1 - self._limiter_gain))
            limiter = 1 - deficits
            limiter[limiter > 0.999999] = 1
            self._limiter_gain = float(limiter[-1])
            output[start:end] *= limiter[:, None]
        return np.clip(np.rint(output), -32768, 32767).astype('<i2').tobytes()
