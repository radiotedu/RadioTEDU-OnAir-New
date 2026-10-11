import struct

from app.audio.live_audio_mixer import LiveAudioMixer


def test_linked_limiting_is_independent_of_transport_packet_boundaries():
    import random
    rng = random.Random(24)
    music = struct.pack('<10000h', *[rng.randrange(-32000, 32000) for _ in range(10000)])
    voice = struct.pack('<5000h', *[rng.randrange(-32000, 32000) for _ in range(5000)])
    continuous = LiveAudioMixer().mix_pcm_chunk(music, voice, program_music_mode='duck', mic_gain=2)
    mixer = LiveAudioMixer()
    split = b''
    offset = 0
    for frames in (128, 512, 1024, 3336):
        split += mixer.mix_pcm_chunk(music[offset*4:(offset+frames)*4], voice[offset*2:(offset+frames)*2], program_music_mode='duck', mic_gain=2)
        offset += frames
    a = struct.unpack('<10000h', continuous)
    b = struct.unpack('<10000h', split)
    assert max(abs(left - right) for left, right in zip(a, b)) <= 1


def pcm(values):
    return struct.pack('<' + 'h' * len(values), *values)


def samples(data):
    return struct.unpack('<' + 'h' * (len(data) // 2), data)


def test_idle_music_remains_bit_exact_with_stereo_image():
    music = pcm([-32768, 32767, -4000, 9000, 12, -21] * 200)
    assert LiveAudioMixer().mix_pcm_chunk(music, b'\x00' * (len(music) // 2)) == music


def test_manual_duck_has_no_abrupt_gain_step_and_restores_music():
    mixer = LiveAudioMixer()
    music = pcm([10000, 10000] * 960)
    silent = pcm([0] * 960)
    first = samples(mixer.mix_pcm_chunk(music, silent, program_music_mode='duck'))
    assert abs(first[0] - 10000) < 20
    assert first[-1] < first[0]
    for _ in range(100):
        mixer.mix_pcm_chunk(music, silent, program_music_mode='duck')
    restored = [mixer.mix_pcm_chunk(music, silent) for _ in range(250)]
    assert restored[-1] == music


def test_voice_and_effect_overload_use_one_limiter_for_both_channels():
    mixed = samples(LiveAudioMixer().mix_pcm_chunk(pcm([30000, 10000]), pcm([10000]), effect_pcm=pcm([10000])))
    assert mixed[0] == 32767
    assert 19000 < mixed[1] < 20000


def test_duck_hold_duration_is_independent_of_packet_size():
    def after_silence(frames):
        mixer = LiveAudioMixer()
        music = pcm([10000, 10000] * frames)
        for _ in range(4800 // frames):
            mixer.mix_pcm_chunk(music, pcm([6000] * frames))
        for _ in range(48000 // frames):
            mixer.mix_pcm_chunk(music, pcm([0] * frames))
        return mixer._auto_duck_factor
    assert abs(after_silence(480) - after_silence(960)) < 0.02
