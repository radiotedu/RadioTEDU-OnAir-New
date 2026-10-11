import math
import sys
from array import array


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
        samples = array("h")
        samples.frombytes(bytes(data[:len(data) // 2 * 2]))
        if sys.byteorder != "little":
            samples.byteswap()
        return samples

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
        peak = max((abs(v) for v in mic), default=0) * voice_gain
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
                and self._limiter_gain == 1.0 and not any(mic) and not any(effect)
                and frames == len(music) // 2):
            return music_pcm

        smoothing = math.exp(-1 / (48000 * (0.02 if target < self._music_scale else 0.3)))
        limiter_release = math.exp(-1 / (48000 * 0.08))
        output = array("h")
        for frame in range(frames):
            self._music_scale = target + (self._music_scale - target) * smoothing
            if abs(self._music_scale - target) < 0.000001:
                self._music_scale = target
            voice = mic[frame] * voice_gain if frame < len(mic) else 0
            fx = effect[frame] if frame < len(effect) else 0
            index = frame * 2
            left = (music[index] if index < len(music) else 0) * self._music_scale + voice + fx
            right = (music[index + 1] if index + 1 < len(music) else 0) * self._music_scale + voice + fx
            overload = max(1.0, left / 32767, right / 32767, -left / 32768, -right / 32768)
            allowed_gain = 1.0 / overload
            recovered = 1 - (1 - self._limiter_gain) * limiter_release
            self._limiter_gain = min(allowed_gain, recovered)
            if self._limiter_gain > 0.999999:
                self._limiter_gain = 1.0
            output.append(self._clamp_sample(left * self._limiter_gain))
            output.append(self._clamp_sample(right * self._limiter_gain))
        if sys.byteorder != "little":
            output.byteswap()
        return output.tobytes()
