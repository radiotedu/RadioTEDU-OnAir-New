import json
import sqlite3
from types import SimpleNamespace

from tools import run_live_delivery_soak as soak

from tools.run_live_delivery_soak import capture_roster, codec_contract_errors, source_changes


def test_live_roster_reads_enabled_outputs_and_station_override_without_credentials(tmp_path):
    db = tmp_path / "live.db"
    with sqlite3.connect(db) as c:
        c.executescript("CREATE TABLE station_outputs(station_id,icecast_enabled,icecast_host,icecast_port,icecast_mount,stream_codec_profile,stream_bitrate_kbps,icecast_password); CREATE TABLE station_settings(station_id,key,value); CREATE TABLE system_settings(key,value);")
        c.execute("INSERT INTO station_outputs VALUES(1,1,'example.test',8000,'/classic','aac_low_192',192,'private-test-value')")
        c.execute("INSERT INTO station_outputs VALUES(2,0,'example.test',8000,'/disabled','aac_low_192',192,'private-test-value')")
        key = "station_1_extra_icecast_outputs"
        c.execute("INSERT INTO system_settings VALUES(?,?)", (key, json.dumps([{"enabled": True, "icecast_mount": "/classic-low"}])))
        c.execute("INSERT INTO station_settings VALUES(1,?,?)", (key, json.dumps([{"enabled": True, "icecast_mount": "/classic-flac", "stream_codec_profile": "ogg_flac_lossless", "stream_bitrate_kbps": 0}, {"enabled": False, "icecast_mount": "/classic-low"}])))
    rows = capture_roster(db)
    assert [r["label"] for r in rows] == ["classic", "classic-flac"]
    assert "private-test-value" not in json.dumps(rows)
    assert rows[1]["codec_profile"] == "ogg_flac_lossless"


def test_source_proof_retains_restarts_and_generated_silence_as_failures():
    before = {"classic-low": {"ready": True, "pid": 123, "generation": 5, "counters": {"encoded_bytes_sent": 100, "continuity_silence_chunks": 0, "dropped_pcm_chunks": 0, "encoder_error_count": 0, "network_error_count": 0}}}
    after = json.loads(json.dumps(before))
    after["classic-low"]["pid"] = 456
    after["classic-low"]["counters"]["encoded_bytes_sent"] = 200
    after["classic-low"]["counters"]["continuity_silence_chunks"] = 1
    assert source_changes(before, after) == ["classic-low: worker restarted", "classic-low: continuity_silence_chunks increased"]


def test_codec_evidence_checks_received_profile_and_bitrate():
    roster = [{"label": "radio", "codec_profile": "aac_low_192", "bitrate_kbps": 192}, {"label": "radio-low", "codec_profile": "aac_he_v2_64", "bitrate_kbps": 64}, {"label": "classic-flac", "codec_profile": "ogg_flac_lossless", "bitrate_kbps": 0}]
    received = {"radio": {"input_audio_description": "aac (LC), 48000 Hz, stereo, fltp, 191 kb/s"}, "radio-low": {"input_audio_description": "aac (HE-AACv2), 48000 Hz, stereo, fltp, 63 kb/s"}, "classic-flac": {"input_audio_description": "flac, 48000 Hz, stereo, s16"}}
    assert codec_contract_errors(roster, received) == []
    received["radio-low"]["input_audio_description"] = "aac (LC), 48000 Hz, stereo, fltp, 192 kb/s"
    assert len(codec_contract_errors(roster, received)) == 2


def test_windows_system_worker_pid_is_not_misreported_dead_on_access_denied(monkeypatch):
    class Function:
        def __init__(self, call):
            self.call = call

        def __call__(self, *args):
            return self.call(*args)

    kernel = SimpleNamespace(OpenProcess=Function(lambda *args: 0), GetExitCodeProcess=Function(lambda *args: 0), CloseHandle=Function(lambda *args: 1))

    def enumerate_processes(pids, size, returned):
        pids[0], pids[1] = 123, 456
        returned._obj.value = 2 * soak.ctypes.sizeof(soak.ctypes.c_uint32)
        return 1

    psapi = SimpleNamespace(EnumProcesses=Function(enumerate_processes))
    monkeypatch.setattr(soak, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(soak.ctypes, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else psapi, raising=False)
    monkeypatch.setattr(soak.ctypes, "get_last_error", lambda: 5, raising=False)
    assert soak._pid_alive(123)
    assert not soak._pid_alive(789)
    monkeypatch.setattr(soak.ctypes, "get_last_error", lambda: 87)
    assert not soak._pid_alive(123)


def test_unready_observation_collects_decoder_evidence_but_never_passes(monkeypatch, tmp_path):
    roster = [{"station_id": 1, "primary": True, "label": "radio", "url": "http://example.test/radio", "mount": "/radio", "codec_profile": "aac_low_192", "bitrate_kbps": 192}]
    sources = {"radio": {"ready": False, "pid": 123, "generation": 1, "counters": {key: 0 for key in soak.COUNTERS}}}
    monkeypatch.setattr(soak, "capture_roster", lambda path: roster)
    monkeypatch.setattr(soak, "sample_sources", lambda *args: sources)
    monkeypatch.setattr(soak.monitor, "_canonical_stream_roster", lambda: ({"radio": roster[0]["url"]}, {}))
    called = []

    def decoder(argv):
        called.append(argv)
        output = soak.Path(argv[argv.index("--output") + 1])
        summary = {"duration_boundary_sampled": True, "measured_duration_seconds": 10, "streams": {"radio": {"input_audio_description": "aac (LC), 48000 Hz, stereo, fltp, 192 kb/s"}}}
        output.with_suffix(output.suffix + ".summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return 0

    monkeypatch.setattr(soak.monitor, "main", decoder)
    args = SimpleNamespace(data_root=str(tmp_path), ffmpeg="unused", duration_seconds=10, startup_timeout_seconds=0, observe_unready=True)
    assert soak.run(args) == 2
    assert len(called) == 1
    latest = json.loads((tmp_path / "Diagnostics" / "live-delivery-soak" / "latest.json").read_text())
    state = json.loads(soak.Path(latest["state"]).read_text())
    assert state["phase"] == "failed"
    assert state["verified"] is False
    assert "source readiness deadline exceeded" in state["source_issues"]
