import io
from types import SimpleNamespace

from tools.monitor_stream_continuity import StreamState, _read_diagnostics
from tools.run_live_delivery_soak import _same_codec_profile


def test_decoder_corruption_is_recorded_even_if_ffmpeg_keeps_running():
    state = StreamState("maincharacter", "http://example.test/maincharacter", SimpleNamespace(), 0.0)
    _read_diagnostics(state, io.StringIO("Stream #0:0: Audio: aac (LC), 48000 Hz, stereo, fltp, 191 kb/s\nStream #0:0: Audio: pcm_s16le, 48000 Hz, stereo, s16\nError submitting packet to decoder: Invalid data found when processing input\nNumber of bands (52) exceeds limit (44).\n"))
    assert state.input_audio_description.startswith("aac (LC)")
    assert state.transport_errors == 2


def test_codec_alias_does_not_accept_a_different_quality_profile():
    assert _same_codec_profile("aac_low_192", "aac_lc_192")
    assert not _same_codec_profile("aac_he_v2_64", "aac_lc_192")
