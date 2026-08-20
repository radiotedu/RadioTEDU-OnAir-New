from app.audio.gst_pipeline import StationPipelineConfig, build_gst_pipeline


def test_pipeline_includes_icecast_and_local_branches():
    cfg = StationPipelineConfig(
        input_uri="C:/music/fallback.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station1",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=True,
        output_device_id="Speakers (USB)",
        icecast_enabled=True,
    )
    pipeline = build_gst_pipeline(cfg)
    assert "shout2send" in pipeline
    assert "mount=/station1" in pipeline
    assert "wasapisink device=\"Speakers (USB)\"" in pipeline
    assert "tee name=t" in pipeline


def test_pipeline_without_local_output_only_has_icecast_branch():
    cfg = StationPipelineConfig(
        input_uri="C:/music/fallback.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station2",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
    )
    pipeline = build_gst_pipeline(cfg)
    assert "shout2send" in pipeline
    assert "wasapisink" not in pipeline


def test_pipeline_with_icecast_disabled_has_only_local_branch():
    cfg = StationPipelineConfig(
        input_uri="C:/music/fallback.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station3",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=True,
        output_device_id="",
        icecast_enabled=False,
    )
    pipeline = build_gst_pipeline(cfg)
    assert "shout2send" not in pipeline
    assert "wasapisink" in pipeline


def test_pipeline_rejects_when_no_outputs_enabled():
    cfg = StationPipelineConfig(
        input_uri="C:/music/fallback.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station4",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=False,
    )
    try:
        build_gst_pipeline(cfg)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "output target" in str(exc)


def test_pipeline_supports_internal_silence_source():
    cfg = StationPipelineConfig(
        input_uri="silence://continuous",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station5",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
    )
    pipeline = build_gst_pipeline(cfg)
    assert "audiotestsrc wave=silence is-live=true" in pipeline
    assert 'filesrc location="' not in pipeline


def test_pipeline_supports_mp3_profile():
    cfg = StationPipelineConfig(
        input_uri="C:/music/fallback.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station6",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        stream_codec_profile="mp3_128",
        stream_bitrate_kbps=128,
    )
    pipeline = build_gst_pipeline(cfg)
    assert "lamemp3enc target=1 bitrate=128 cbr=true" in pipeline


def test_pipeline_supports_aac_plus_profile():
    cfg = StationPipelineConfig(
        input_uri="C:/music/fallback.mp3",
        icecast_host="127.0.0.1",
        icecast_port=8000,
        icecast_mount="/station7",
        icecast_user="source",
        icecast_password="hackme",
        local_output_enabled=False,
        output_device_id="",
        icecast_enabled=True,
        stream_codec_profile="aac_plus_196",
        stream_bitrate_kbps=196,
    )
    pipeline = build_gst_pipeline(cfg)
    assert "voaacenc bitrate=196000" in pipeline
