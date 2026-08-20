import struct

from app.audio.live_audio_mixer import LiveAudioMixer


def _stereo_frame(left: int, right: int) -> bytes:
    return struct.pack("<2h", left, right)


def _mono_sample(value: int) -> bytes:
    return struct.pack("<h", value)


def test_effect_pcm_none_has_no_impact():
    mixer = LiveAudioMixer()
    music = _stereo_frame(1000, -1000)
    mic = _mono_sample(0)
    result_with = mixer.mix_pcm_chunk(music, mic, effect_pcm=None, program_music_mode="normal")
    result_without = mixer.mix_pcm_chunk(music, mic, program_music_mode="normal")
    assert result_with == result_without


def test_effect_pcm_overlays_on_music():
    mixer = LiveAudioMixer()
    music = _stereo_frame(5000, 5000)
    mic = _mono_sample(0)
    effect = _mono_sample(3000)
    result = mixer.mix_pcm_chunk(music, mic, effect_pcm=effect, program_music_mode="normal")
    left, right = struct.unpack("<2h", result)
    assert left == 8000
    assert right == 8000


def test_effect_pcm_clamps_at_boundaries():
    mixer = LiveAudioMixer()
    music = _stereo_frame(30000, 30000)
    mic = _mono_sample(0)
    effect = _mono_sample(10000)
    result = mixer.mix_pcm_chunk(music, mic, effect_pcm=effect, program_music_mode="normal")
    left, right = struct.unpack("<2h", result)
    assert left == 32767
    assert right == 32767


def test_effect_pcm_works_with_duck_mode():
    mixer = LiveAudioMixer()
    music = _stereo_frame(10000, 10000)
    mic = _mono_sample(5000)
    effect = _mono_sample(2000)
    result = mixer.mix_pcm_chunk(
        music, mic, effect_pcm=effect,
        program_music_mode="duck", mic_gain=1.0, duck_level=0.5,
    )
    left, right = struct.unpack("<2h", result)
    # duck: music*0.5 + mic*1.0 + effect = 5000 + 5000 + 2000 = 12000
    assert left == 12000
    assert right == 12000


def test_effect_pcm_shorter_than_music_pads_silence():
    mixer = LiveAudioMixer()
    music = _stereo_frame(1000, 1000) + _stereo_frame(2000, 2000)
    mic = _mono_sample(0) + _mono_sample(0)
    effect = _mono_sample(500)  # only 1 frame, music has 2
    result = mixer.mix_pcm_chunk(music, mic, effect_pcm=effect, program_music_mode="normal")
    left1, right1, left2, right2 = struct.unpack("<4h", result)
    assert left1 == 1500  # 1000 + 500
    assert left2 == 2000  # 2000 + 0 (padded silence)
