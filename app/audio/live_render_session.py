from app.audio.mic_session import (
    MicSession,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
)


class LiveRenderSession(MicSession):
    def __init__(
        self,
        station_id: int,
        *,
        input_format: str = "s16le",
        input_sample_rate: int = 24000,
        input_channels: int = 1,
        max_buffer_bytes: int = 480000,
    ) -> None:
        super().__init__(station_id, max_buffer_bytes=max_buffer_bytes)
        self.input_format = self._normalize_input_format(input_format)
        self.input_sample_rate = max(8000, int(input_sample_rate or 24000))
        self.input_channels = max(1, min(8, int(input_channels or 1)))

    @staticmethod
    def _normalize_input_format(raw: str) -> str:
        token = str(raw or "").strip().lower()
        aliases = {
            "pcm": "s16le",
            "pcm16": "s16le",
            "pcm_s16le": "s16le",
            "aac+": "adts",
            "aacp": "adts",
        }
        normalized = aliases.get(token, token)
        if normalized in {
            "auto",
            "s16le",
            "f32le",
            "wav",
            "mp3",
            "aac",
            "adts",
            "ogg",
            "opus",
            "webm",
            "flac",
            "m4a",
        }:
            return normalized
        return "s16le"

    def _decoder_cmd(self) -> list[str]:
        cmd = [
            str(self.ffmpeg_bin),
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+nobuffer",
            "-flags",
            "low_delay",
        ]
        if self.input_format != "auto":
            cmd.extend(["-f", self.input_format])
            if self.input_format in {"s16le", "f32le"}:
                cmd.extend(
                    [
                        "-ar",
                        str(self.input_sample_rate),
                        "-ac",
                        str(self.input_channels),
                    ]
                )
        cmd.extend(
            [
                "-i",
                "pipe:0",
                "-vn",
                "-f",
                "s16le",
                "-ar",
                str(PCM_SAMPLE_RATE),
                "-ac",
                str(PCM_CHANNELS),
                "pipe:1",
            ]
        )
        return cmd
