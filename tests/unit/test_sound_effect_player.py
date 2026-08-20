import time

import app.audio.sound_effect_player as sound_effect_module
from app.audio.sound_effect_player import SoundEffectSlot


class _InfiniteStdout:
    def read(self, size=-1):
        return b"\x01" * max(0, int(size))


class _FakeProcess:
    def __init__(self):
        self.stdout = _InfiniteStdout()
        self.running = True

    def poll(self):
        return None if self.running else 0

    def kill(self):
        self.running = False

    def wait(self, timeout=None):
        self.running = False
        return 0


def test_sound_effect_decoder_buffer_is_bounded(monkeypatch):
    process = _FakeProcess()
    monkeypatch.setattr(sound_effect_module, "resolve_binary", lambda _name: "ffmpeg")
    monkeypatch.setattr(sound_effect_module.subprocess, "Popen", lambda *_args, **_kwargs: process)

    slot = SoundEffectSlot(1, "Long effect", "C:/effects/long.wav")
    deadline = time.time() + 1.0
    while slot._buffer_bytes < sound_effect_module.PCM_MAX_BUFFER_BYTES and time.time() < deadline:
        time.sleep(0.005)

    first_size = slot._buffer_bytes
    time.sleep(0.05)
    second_size = slot._buffer_bytes
    slot.stop()

    assert first_size == sound_effect_module.PCM_MAX_BUFFER_BYTES
    assert second_size == sound_effect_module.PCM_MAX_BUFFER_BYTES
