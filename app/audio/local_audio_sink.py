import subprocess
from typing import Callable

from app.audio.ffmpeg_pipeline import build_ffplay_local_cmd
from app.audio.gst_pipeline import StationPipelineConfig


class LocalAudioSink:
    def __init__(
        self,
        ffplay_bin: str,
        spawn_process: Callable[..., subprocess.Popen],
    ) -> None:
        self.ffplay_bin = ffplay_bin
        self._spawn_process = spawn_process
        self._process = None

    @property
    def process(self):
        return self._process

    @property
    def stdin(self):
        if not self._process:
            return None
        return getattr(self._process, "stdin", None)

    def is_running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def ensure_started(self, cfg: StationPipelineConfig):
        if self.is_running() and self.stdin is not None:
            return self._process
        self.stop()
        cmd = build_ffplay_local_cmd(cfg, self.ffplay_bin)
        self._process = self._spawn_process(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self._process

    def stop(self) -> None:
        if not self._process:
            return
        proc = self._process
        self._process = None
        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            try:
                stdin.close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
