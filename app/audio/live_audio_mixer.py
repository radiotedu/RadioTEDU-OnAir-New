class LiveAudioMixer:
    # Voice activity threshold (~-24 dB in 16-bit PCM).
    # Must be above typical mic noise floor to avoid permanent ducking.
    _VOICE_THRESHOLD = 2000
    # Per-chunk attack/release rates for smooth ducking.
    _ATTACK_RATE = 0.35
    _RELEASE_RATE = 0.06
    # Hold ducked state for ~1 s after voice stops (47 chunks @ 4096-byte/48 kHz).
    _HOLD_CHUNKS = 47

    def __init__(self) -> None:
        self.max_sample = 32767
        self.min_sample = -32768
        self._auto_duck_factor: float = 0.0
        self._hold_remaining: int = 0

    @staticmethod
    def _clamp_sample(value: float) -> int:
        rounded = int(round(float(value)))
        if rounded > 32767:
            return 32767
        if rounded < -32768:
            return -32768
        return rounded

    @staticmethod
    def _normalize_mode(program_music_mode: str) -> str:
        token = str(program_music_mode or "").strip().lower()
        if token in {"duck", "mute", "normal"}:
            return token
        return "normal"

    @staticmethod
    def _int16_at(data: bytes, offset: int) -> int:
        if offset + 2 > len(data):
            return 0
        return int.from_bytes(data[offset : offset + 2], byteorder="little", signed=True)

    @staticmethod
    def _mic_peak(mic_pcm: bytes) -> int:
        peak = 0
        for i in range(0, len(mic_pcm) - 1, 2):
            sample = int.from_bytes(mic_pcm[i : i + 2], byteorder="little", signed=True)
            v = sample if sample >= 0 else -sample
            if v > peak:
                peak = v
        return peak

    def mix_pcm_chunk(
        self,
        music_pcm: bytes,
        mic_pcm: bytes,
        *,
        effect_pcm: bytes | None = None,
        program_music_mode: str = "normal",
        mic_gain: float = 1.0,
        music_gain: float = 1.0,
        duck_level: float = 0.15,
    ) -> bytes:
        mode = self._normalize_mode(program_music_mode)
        mic_scale = max(0.0, float(mic_gain or 0.0))
        base_music_scale = max(0.0, float(music_gain or 0.0))
        duck_scale = max(0.0, min(1.0, float(duck_level or 0.0)))

        if mode == "normal":
            # Auto-duck: music lowers when voice detected, restores when silent.
            # Hold keeps music ducked for ~1 s after last voice to avoid pumping
            # between words/syllables.
            peak = self._mic_peak(mic_pcm)
            if peak > self._VOICE_THRESHOLD:
                self._auto_duck_factor = min(
                    1.0, self._auto_duck_factor + self._ATTACK_RATE
                )
                self._hold_remaining = self._HOLD_CHUNKS
            elif self._hold_remaining > 0:
                self._hold_remaining -= 1
            else:
                self._auto_duck_factor = max(
                    0.0, self._auto_duck_factor - self._RELEASE_RATE
                )
            df = self._auto_duck_factor
            effective_music_scale = base_music_scale * (1.0 - df * (1.0 - duck_scale))
            effective_mic_scale = mic_scale
        elif mode == "duck":
            effective_music_scale = base_music_scale * duck_scale
            effective_mic_scale = mic_scale
        else:
            effective_music_scale = 0.0
            effective_mic_scale = mic_scale

        frame_count = max(len(music_pcm) // 4, len(mic_pcm) // 2)
        output = bytearray(frame_count * 4)

        for frame_index in range(frame_count):
            music_offset = frame_index * 4
            mic_offset = frame_index * 2

            music_left = self._int16_at(music_pcm, music_offset)
            music_right = self._int16_at(music_pcm, music_offset + 2)
            mic_sample = self._int16_at(mic_pcm, mic_offset)

            mixed_left = self._clamp_sample(
                (music_left * effective_music_scale) + (mic_sample * effective_mic_scale)
            )
            mixed_right = self._clamp_sample(
                (music_right * effective_music_scale) + (mic_sample * effective_mic_scale)
            )

            output[music_offset : music_offset + 2] = int(mixed_left).to_bytes(
                2, byteorder="little", signed=True
            )
            output[music_offset + 2 : music_offset + 4] = int(mixed_right).to_bytes(
                2, byteorder="little", signed=True
            )

        if effect_pcm is not None:
            for frame_index in range(frame_count):
                music_offset = frame_index * 4
                effect_offset = frame_index * 2
                effect_sample = self._int16_at(effect_pcm, effect_offset)
                if effect_sample == 0:
                    continue
                current_left = int.from_bytes(
                    output[music_offset : music_offset + 2], byteorder="little", signed=True
                )
                current_right = int.from_bytes(
                    output[music_offset + 2 : music_offset + 4], byteorder="little", signed=True
                )
                output[music_offset : music_offset + 2] = self._clamp_sample(
                    current_left + effect_sample
                ).to_bytes(2, byteorder="little", signed=True)
                output[music_offset + 2 : music_offset + 4] = self._clamp_sample(
                    current_right + effect_sample
                ).to_bytes(2, byteorder="little", signed=True)

        return bytes(output)
