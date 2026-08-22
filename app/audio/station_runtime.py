import atexit
import ctypes
import inspect
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from app.audio.ffmpeg_pipeline import (
    LOCAL_MONITOR_CATCHUP_RATE,
    LOCAL_MONITOR_INITIAL_BURST_SECONDS,
    build_ffmpeg_crossfade_pcm_cmd,
    build_ffmpeg_icecast_cmd,
    build_ffmpeg_pcm_producer_cmd,
    build_ffmpeg_local_pcm_cmd,
    build_ffplay_local_cmd,
    release_fast_cached_uri,
)
from app.audio.gst_pipeline import StationPipelineConfig, build_gst_pipeline
from app.audio.icecast_audio_sink import IcecastAudioSink
from app.audio.shoutcast_audio_sink import ShoutcastAudioSink
from app.audio.live_audio_mixer import LiveAudioMixer
from app.audio.local_audio_sink import LocalAudioSink
from app.audio.output_health_router import OutputHealthRouter
from app.audio.sound_effect_player import SoundEffectPlayer
from app.runtime_paths import resolve_binary

_log = logging.getLogger("cleanroom.runtime")
_LIVE_MIX_CHUNK_BYTES = 4096
_LIVE_MIX_RETURN_GRACE_SECONDS = 0.75
_PCM_BYTES_PER_SECOND = 48000 * 2 * 2
_SILENCE_FLOOR_CHUNK_BYTES = 4096
# Windows pipe reads commonly arrive as 1 KiB fragments even though 4 KiB was
# requested.  Sixty-four queue items therefore seed roughly 0.34-1.36 seconds
# of programme reserve: enough to absorb coarse scheduler quanta without
# recreating the many-second latency that previously grew without bound.
_ICECAST_PIPE_STARTUP_RESERVE_CHUNKS = 64
_SILENCE_FLOOR_INTERVAL_SECONDS = (
    _SILENCE_FLOOR_CHUNK_BYTES / _PCM_BYTES_PER_SECOND
)
# Keep the encoder clock continuous across decoder startup and track handoff.
# Waiting a full second starved the AAC encoder long enough for small listener
# buffers to underrun.  Two PCM chunks avoids competing with normal writes but
# closes a gap before it becomes audible as a source interruption.
_SILENCE_FLOOR_AFTER_SECONDS = _SILENCE_FLOOR_INTERVAL_SECONDS * 2.0
_SILENCE_FLOOR_STARTUP_GRACE_SECONDS = 1.0
_PROGRAM_PCM_STALL_SECONDS = 5.0
_DIRECT_ICECAST_STARTUP_GRACE_SECONDS = 2.0
_DEFAULT_LIVE_AUDIO_SETTINGS = {
    "program_music_mode": "normal",
    "mic_gain": 1.0,
    "music_gain": 1.0,
    "duck_level": 0.15,
}
_DEFAULT_LIVE_SNAPSHOT = {
    "live_input_enabled": False,
    "transmitting": False,
    "active_user": None,
    "receiving": False,
    "level_db": -60.0,
    "peak_db": -60.0,
    "buffer_bytes": 0,
    "last_error": "",
}


def _sanitize_process_text(raw: str) -> str:
    return re.sub(r"://([^:/@\s]+):([^@/\s]+)@", r"://\1:***@", str(raw or ""))


# ---------------------------------------------------------------------------
# Windows Job Object: auto-kill child processes when Python exits
# ---------------------------------------------------------------------------
_win_job_handle = None

def _create_win_job_object():
    """Create a Windows Job Object that kills all children on close."""
    global _win_job_handle
    if sys.platform != "win32" or _win_job_handle is not None:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        # CreateJobObjectW(lpJobAttributes, lpName)
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return

        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        kernel32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )

        _win_job_handle = handle
        _log.info("Windows Job Object created — child processes will be killed on exit")
    except Exception as exc:
        _log.warning("Failed to create Windows Job Object: %s", exc)


def _assign_to_job(proc: subprocess.Popen) -> bool:
    """Assign a subprocess to the Windows Job Object so it dies with us."""
    if sys.platform != "win32" or _win_job_handle is None:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        h_process = kernel32.OpenProcess(0x001F0FFF, False, proc.pid)  # PROCESS_ALL_ACCESS
        if not h_process:
            _log.warning(
                "Could not open child PID %d for Job Object assignment (error %d)",
                proc.pid,
                ctypes.get_last_error(),
            )
            return False
        try:
            if not kernel32.AssignProcessToJobObject(_win_job_handle, h_process):
                _log.warning(
                    "Could not assign child PID %d to the playout Job Object (error %d)",
                    proc.pid,
                    ctypes.get_last_error(),
                )
                return False
            return True
        finally:
            kernel32.CloseHandle(h_process)
    except Exception as exc:
        _log.warning("Could not assign PID %d to the playout Job Object: %s", proc.pid, exc)
        return False


# Create the job object once at import time
_create_win_job_object()


def _normalize_track_type(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if token in {"ad", "ads", "advertisement", "advertisements"}:
        return "ad"
    if token in {
        "music",
        "jingle",
        "announcement",
        "station_id",
        "show",
        "startup",
    }:
        return token
    return "music"


def _get_named_ffplay(ffplay_bin: str, station_name: str) -> str:
    """Create a copy of ffplay.exe named after the station for Windows Volume Mixer."""
    if sys.platform != "win32" or not ffplay_bin or not station_name:
        return ffplay_bin
    try:
        import re
        import shutil as _shutil
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', station_name.strip())
        if not safe_name:
            return ffplay_bin
        src = Path(ffplay_bin)
        if not src.exists():
            return ffplay_bin
        # Use app data directory to avoid permission issues
        app_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "CleanRoomRadio" / "players"
        if not str(app_dir).strip() or app_dir == Path(""):
            app_dir = Path(__file__).parent.parent.parent / "data" / "players"
        app_dir.mkdir(parents=True, exist_ok=True)
        dest = app_dir / f"{safe_name}.exe"
        if not dest.exists() or dest.stat().st_size != src.stat().st_size:
            _shutil.copy2(str(src), str(dest))
            _log.info("Created station player: %s", dest)
        return str(dest)
    except Exception as exc:
        _log.warning("Could not create named ffplay copy: %s", exc)
        return ffplay_bin


def _normalize_program_music_mode(raw: str) -> str:
    token = str(raw or "").strip().lower()
    if token in {"duck", "mute", "normal"}:
        return token
    return "normal"


def _normalize_live_gain(raw: float, default: float) -> float:
    try:
        return max(0.0, min(2.0, float(raw)))
    except (TypeError, ValueError):
        return float(default)


def _normalize_duck_level(raw: float) -> float:
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return float(_DEFAULT_LIVE_AUDIO_SETTINGS["duck_level"])


class StationRuntime:
    def __init__(
        self,
        gst_bin: str = "gst-launch-1.0",
        process_factory=None,
        station_id: int | None = None,
        live_mic_registry=None,
        guest_audio_registry=None,
        live_settings_provider=None,
    ):
        self.gst_bin = gst_bin
        self.ffmpeg_bin = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
        self.ffplay_bin = resolve_binary("ffplay.exe") or resolve_binary("ffplay")
        self._uses_default_process_factory = process_factory is None
        self._process_factory = process_factory or self._spawn
        self._process = None
        self._local_process = None
        self._icecast_sink = None
        self._extra_icecast_sinks: dict[str, object] = {}
        self._extra_icecast_configs: dict[str, StationPipelineConfig] = {}
        self._extra_icecast_lock = threading.RLock()
        self._local_sink = None
        self._backend = "none"
        self._router = OutputHealthRouter()
        self._active_signature = None
        self._active_cfg = None
        self._active_started_monotonic = None
        self._playout_generation = 0
        self._transition_until_monotonic = None
        self._last_transition_mode = "none"
        self.station_id = int(station_id) if station_id is not None else None
        self.live_mic_registry = live_mic_registry
        self.guest_audio_registry = guest_audio_registry
        self.live_settings_provider = live_settings_provider
        self._live_audio_mixer = LiveAudioMixer()
        self._sound_effect_player = SoundEffectPlayer(station_id=int(station_id) if station_id else 0)
        self._live_mix_thread = None
        self._live_mix_stop = threading.Event()
        self._pcm_write_lock = threading.Lock()
        self._last_program_pcm_monotonic = time.monotonic()
        self._silence_floor_started_monotonic = 0.0
        self._silence_floor_thread = None
        self._silence_floor_stop = threading.Event()
        self._icecast_pipe_thread = None
        self._icecast_pipe_stop = threading.Event()
        self._icecast_pipe_process = None
        # Final ownership ledger for children whose narrower runtime reference
        # may be lost during a failed hand-off or transition.
        self._owned_processes = []
        # Register atexit handler to clean up on normal shutdown
        atexit.register(self._atexit_cleanup)

    def _refresh_runtime_bins(self) -> None:
        if not self._uses_default_process_factory:
            return
        self.ffmpeg_bin = resolve_binary("ffmpeg.exe") or resolve_binary("ffmpeg")
        self.ffplay_bin = resolve_binary("ffplay.exe") or resolve_binary("ffplay")

    @property
    def sound_effect_player(self) -> SoundEffectPlayer:
        return self._sound_effect_player

    def configure_live_context(
        self,
        *,
        station_id: int | None = None,
        live_mic_registry=None,
        guest_audio_registry=None,
        live_settings_provider=None,
    ) -> None:
        if station_id is not None:
            self.station_id = int(station_id)
        if live_mic_registry is not None:
            self.live_mic_registry = live_mic_registry
        if guest_audio_registry is not None:
            self.guest_audio_registry = guest_audio_registry
        if live_settings_provider is not None:
            self.live_settings_provider = live_settings_provider

    def _next_playout_generation(self) -> int:
        self._playout_generation += 1
        return int(self._playout_generation)

    def _current_playout_generation(self) -> int:
        return int(self._playout_generation)

    def _generation_is_current(self, generation: int | None) -> bool:
        return generation is None or int(generation) == int(
            self._playout_generation
        )

    @staticmethod
    def _icecast_branch(mount: str) -> str:
        normalized = str(mount or "").strip() or "/stream"
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return f"icecast:{normalized}"

    @staticmethod
    def _output_bool(value, default: bool) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        token = str(value).strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off", ""}:
            return False
        return bool(default)

    def _extra_output_configs(
        self, cfg: StationPipelineConfig
    ) -> dict[str, StationPipelineConfig]:
        outputs: dict[str, StationPipelineConfig] = {}
        primary_mount = str(cfg.icecast_mount or "").strip()
        if primary_mount and not primary_mount.startswith("/"):
            primary_mount = f"/{primary_mount}"
        for raw in getattr(cfg, "extra_icecast_outputs", ()) or ():
            value = dict(raw or {})
            if not self._output_bool(value.get("enabled"), True):
                continue
            mount = str(
                value.get("icecast_mount") or value.get("mount") or ""
            ).strip()
            if not mount:
                continue
            if not mount.startswith("/"):
                mount = f"/{mount}"
            if mount == primary_mount:
                continue
            branch = self._icecast_branch(mount)
            if branch in outputs:
                continue
            bitrate = value.get("stream_bitrate_kbps")
            if bitrate is None:
                bitrate = value.get("bitrate_kbps")
            if bitrate is None:
                bitrate = cfg.stream_bitrate_kbps
            outputs[branch] = replace(
                cfg,
                icecast_enabled=True,
                icecast_host=str(
                    value.get("icecast_host") or value.get("host") or cfg.icecast_host
                ),
                icecast_port=int(
                    value.get("icecast_port")
                    or value.get("port")
                    or cfg.icecast_port
                ),
                icecast_mount=mount,
                icecast_user=str(
                    value.get("icecast_user") or value.get("user") or cfg.icecast_user
                ),
                icecast_password=str(
                    value.get("icecast_password")
                    or value.get("password")
                    or cfg.icecast_password
                ),
                stream_codec_profile=str(
                    value.get("stream_codec_profile")
                    or value.get("codec")
                    or cfg.stream_codec_profile
                ),
                stream_bitrate_kbps=int(bitrate),
                # Icecast exposes one now-playing string per mount.  Keep all
                # quality branches aligned with the primary programme so
                # listeners see the same artist/title/album metadata.
                stream_title=str(cfg.stream_title),
                stream_artist=str(cfg.stream_artist),
                stream_album=str(getattr(cfg, "stream_album", "")),
                icecast_stream_name=str(
                    value.get("icecast_stream_name")
                    or value.get("name")
                    or cfg.icecast_stream_name
                ),
                icecast_description=str(
                    value.get("icecast_description")
                    or cfg.icecast_description
                ),
                icecast_genre=str(
                    value.get("icecast_genre") or cfg.icecast_genre
                ),
                icecast_url=str(value.get("icecast_url") or cfg.icecast_url),
                icecast_public=self._output_bool(
                    value.get("icecast_public"), cfg.icecast_public
                ),
                icecast_user_agent=str(
                    value.get("icecast_user_agent") or cfg.icecast_user_agent
                ),
                icecast_tls_enabled=self._output_bool(
                    value.get("icecast_tls_enabled"), cfg.icecast_tls_enabled
                ),
                icecast_legacy_source_enabled=self._output_bool(
                    value.get(
                        "icecast_legacy_source_enabled",
                    ),
                    cfg.icecast_legacy_source_enabled,
                ),
                source_protocol=str(
                    value.get("source_protocol") or cfg.source_protocol
                ),
                extra_icecast_outputs=(),
            )
        return outputs

    def _icecast_output_targets(self) -> list[tuple[str, object]]:
        targets: list[tuple[str, object]] = []
        if self._icecast_sink is not None:
            targets.append(("icecast", self._icecast_sink))
        with self._extra_icecast_lock:
            targets.extend(sorted(self._extra_icecast_sinks.items()))
        return targets

    def _pcm_output_targets(self) -> list[tuple[str, object]]:
        targets = self._icecast_output_targets()
        if self._local_sink is not None:
            targets.append(("local", self._local_sink))
        return targets

    def _silence_floor_targets(self) -> list[tuple[str, object]]:
        # Queued Icecast sinks own their output clock and generate continuity
        # PCM only when the programme reserve is actually empty.  Feeding the
        # runtime floor into those queues placed zeroes *behind* valid audio, so
        # a brief decoder handoff was heard later as a 100-300 ms microdrop.
        return [
            (branch, sink)
            for branch, sink in self._pcm_output_targets()
            if not bool(getattr(sink, "manages_pcm_continuity", False))
        ]

    def _write_pcm_chunk_to_targets(
        self,
        chunk: bytes,
        targets: list[tuple[str, object]],
        *,
        program_data: bool = True,
        generation: int | None = None,
    ) -> None:
        if not self._generation_is_current(generation):
            return
        wrote_any = False
        with self._pcm_write_lock:
            if not self._generation_is_current(generation):
                return
            for branch, sink in targets:
                queued_writer = getattr(sink, "write_pcm", None)
                if callable(queued_writer):
                    try:
                        accepted = bool(queued_writer(chunk))
                    except Exception:
                        accepted = False
                    is_running = getattr(sink, "is_running", None)
                    healthy = bool(
                        accepted
                        and callable(is_running)
                        and is_running()
                    )
                    self._router.set_branch_health(branch, healthy)
                    wrote_any = wrote_any or accepted
                    continue
                stdin = getattr(sink, "stdin", None)
                is_running = getattr(sink, "is_running", None)
                if stdin is None or not callable(is_running) or not is_running():
                    self._router.set_branch_health(branch, False)
                    continue
                try:
                    stdin.write(chunk)
                    flush = getattr(stdin, "flush", None)
                    if callable(flush):
                        flush()
                    self._router.set_branch_health(branch, True)
                    wrote_any = True
                except Exception:
                    self._router.set_branch_health(branch, False)
        # Programme generation is authoritative even while every remote mount
        # is reconnecting.  Do not let an origin outage look like a decoder
        # stall and trigger a destructive whole-station restart.
        if program_data:
            self._last_program_pcm_monotonic = time.monotonic()
            station_id = self._live_station_id()
            if station_id is not None:
                try:
                    guest_audio_registry = self._guest_audio_provider()
                    guest_audio_registry.publish_program_pcm(
                        station_id,
                        chunk,
                        voice_gain=float(self._live_audio_settings().get("mic_gain", 1.0)),
                    )
                except Exception:
                    pass
                try:
                    from app.services.program_recording import program_recording_service

                    program_recording_service.publish_pcm(station_id, chunk)
                except Exception:
                    pass

    def _silence_floor_loop(self) -> None:
        silence = b"\x00" * _SILENCE_FLOOR_CHUNK_BYTES
        while not self._silence_floor_stop.is_set():
            targets = self._silence_floor_targets()
            if not targets:
                self._silence_floor_stop.wait(0.05)
                continue
            quiet_for = time.monotonic() - float(
                self._last_program_pcm_monotonic or 0.0
            )
            startup_remaining = (
                self._silence_floor_started_monotonic
                + _SILENCE_FLOOR_STARTUP_GRACE_SECONDS
                - time.monotonic()
            )
            if self._last_program_pcm_monotonic and startup_remaining > 0:
                self._silence_floor_stop.wait(min(0.05, startup_remaining))
                continue
            if quiet_for >= _SILENCE_FLOOR_AFTER_SECONDS:
                self._write_pcm_chunk_to_targets(
                    silence, targets, program_data=False
                )
                # The encoder is receiving deterministic continuity PCM. Keep
                # the health clock current too: a short decoder handoff must not
                # be reported as an output PCM stall while the sink is flowing.
                self._last_program_pcm_monotonic = time.monotonic()
                self._silence_floor_stop.wait(
                    _SILENCE_FLOOR_INTERVAL_SECONDS
                )
                continue
            self._silence_floor_stop.wait(
                max(
                    0.005,
                    min(
                        _SILENCE_FLOOR_INTERVAL_SECONDS,
                        _SILENCE_FLOOR_AFTER_SECONDS - quiet_for,
                    ),
                )
            )

    def _start_silence_floor_worker(self) -> None:
        if (
            self._silence_floor_thread is not None
            and self._silence_floor_thread.is_alive()
        ):
            return
        self._last_program_pcm_monotonic = time.monotonic()
        self._silence_floor_started_monotonic = self._last_program_pcm_monotonic
        self._silence_floor_stop.clear()
        self._silence_floor_thread = threading.Thread(
            target=self._silence_floor_loop,
            name=f"station-silence-floor-{self.station_id or 'unknown'}",
            daemon=True,
        )
        self._silence_floor_thread.start()

    def _stop_silence_floor_worker(self) -> None:
        self._silence_floor_stop.set()
        if self._silence_floor_thread is not None:
            self._silence_floor_thread.join(timeout=1.0)
        self._silence_floor_thread = None
        self._silence_floor_stop.clear()

    def _atexit_cleanup(self):
        """Kill any running child processes on Python exit."""
        try:
            self._stop_silence_floor_worker()
            self._stop_producers()
            if self._icecast_sink is not None:
                self._icecast_sink.stop()
            if self._local_sink is not None:
                self._local_sink.stop()
        except Exception:
            pass

    def _spawn(
        self,
        cmd: list[str],
        *,
        stdin=None,
        stdout=None,
        stderr=None,
    ):
        kwargs = dict(
            stdin=subprocess.DEVNULL if stdin is None else stdin,
            stdout=subprocess.DEVNULL if stdout is None else stdout,
            stderr=subprocess.DEVNULL if stderr is None else stderr,
        )
        # On Windows, set SDL_VIDEODRIVER=dummy for ffplay and use CREATE_NEW_PROCESS_GROUP
        if sys.platform == "win32":
            env = os.environ.copy()
            env["SDL_VIDEODRIVER"] = "dummy"
            kwargs["env"] = env
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, **kwargs)
        # Assign to job object so ffplay dies when Python exits
        _assign_to_job(proc)
        return proc

    def _spawn_process(self, cmd: list[str], **kwargs):
        proc = None
        if kwargs:
            try:
                signature = inspect.signature(self._process_factory)
            except (TypeError, ValueError):
                signature = None
            if signature is not None:
                params = signature.parameters.values()
                accepts_kwargs = any(
                    param.kind is inspect.Parameter.VAR_KEYWORD for param in params
                )
                accepted_names = {
                    param.name
                    for param in params
                    if param.kind
                    in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    )
                }
                if accepts_kwargs or all(name in accepted_names for name in kwargs):
                    proc = self._process_factory(cmd, **kwargs)
                else:
                    proc = self._process_factory(cmd)
        if proc is None:
            try:
                proc = self._process_factory(cmd, **kwargs)
            except TypeError:
                proc = self._process_factory(cmd)
        if callable(getattr(proc, "poll", None)):
            self._owned_processes = [
                item
                for item in self._owned_processes
                if callable(getattr(item, "poll", None)) and item.poll() is None
            ]
            self._owned_processes.append(proc)
        return proc

    def _signature(self, cfg: StationPipelineConfig) -> tuple:
        extra_outputs = tuple(
            sorted(
                (
                    branch,
                    extra.icecast_host,
                    int(extra.icecast_port),
                    extra.icecast_mount,
                    extra.icecast_user,
                    extra.icecast_password,
                    extra.stream_codec_profile,
                    int(extra.stream_bitrate_kbps),
                    bool(extra.icecast_tls_enabled),
                    str(extra.source_protocol or "icecast").lower(),
                )
                for branch, extra in self._extra_output_configs(cfg).items()
            )
        )
        return (
            cfg.input_uri,
            bool(cfg.icecast_enabled),
            str(getattr(cfg, "source_protocol", "icecast") or "icecast").lower(),
            cfg.icecast_host,
            int(cfg.icecast_port),
            str(cfg.icecast_mount),
            str(cfg.icecast_user),
            str(cfg.icecast_password),
            str(cfg.stream_codec_profile),
            int(cfg.stream_bitrate_kbps),
            bool(cfg.local_output_enabled),
            str(cfg.output_device_id),
            float(cfg.output_gain_db),
            str(cfg.stream_title),
            str(cfg.stream_artist),
            str(getattr(cfg, "stream_album", "")),
            _normalize_track_type(cfg.track_type),
            extra_outputs,
        )

    def _can_crossfade(
        self, current_cfg: StationPipelineConfig, next_cfg: StationPipelineConfig
    ) -> bool:
        try:
            crossfade_seconds = max(0.0, float(next_cfg.crossfade_seconds or 0.0))
        except (TypeError, ValueError):
            crossfade_seconds = 0.0
        return (
            self.is_running()
            and crossfade_seconds > 0.0
            and _normalize_track_type(current_cfg.track_type) in {"music", "jingle"}
            and _normalize_track_type(next_cfg.track_type) in {"music", "jingle"}
        )

    @staticmethod
    def _transition_cfg(
        current_cfg: StationPipelineConfig,
        next_cfg: StationPipelineConfig,
    ) -> StationPipelineConfig:
        """Cap station-ID overlap while keeping the configured music blend."""

        current_type = _normalize_track_type(current_cfg.track_type)
        next_type = _normalize_track_type(next_cfg.track_type)
        if "jingle" not in {current_type, next_type}:
            return next_cfg
        try:
            seconds = max(0.0, float(next_cfg.crossfade_seconds or 0.0))
        except (TypeError, ValueError):
            seconds = 0.0
        return replace(next_cfg, crossfade_seconds=min(0.25, seconds))

    def _terminate_process(self, proc) -> None:
        if not proc:
            return
        poll = getattr(proc, "poll", None)
        if not callable(poll):
            return
        if poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass

    def _terminate_owned_processes(self) -> None:
        owned = list(self._owned_processes)
        self._owned_processes = []
        for proc in owned:
            self._terminate_process(proc)

    def _mark_active_request(
        self,
        cfg: StationPipelineConfig,
        target_signature: tuple,
        started_monotonic: float | None = None,
    ) -> None:
        self._next_playout_generation()
        self._active_signature = target_signature
        self._active_cfg = cfg
        self._active_started_monotonic = (
            time.monotonic() if started_monotonic is None else float(started_monotonic)
        )
        # Each new producer gets a bounded grace window in which to emit its
        # first decoded PCM, independent of any previous track or recovery.
        self._last_program_pcm_monotonic = time.monotonic()

    def _clear_transition_window(self) -> None:
        self._transition_until_monotonic = None

    def _is_transition_active(self) -> bool:
        if self._transition_until_monotonic is None:
            return False
        return time.monotonic() < float(self._transition_until_monotonic)

    def _current_offset_seconds(self) -> float:
        if self._active_started_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - float(self._active_started_monotonic))

    def _transition_backend_supported(self, cfg: StationPipelineConfig) -> bool:
        if not self.ffmpeg_bin:
            return False
        if cfg.local_output_enabled and not cfg.icecast_enabled and not self.ffplay_bin:
            return False
        if self._is_transition_active():
            return False
        return True

    def _build_ffplay_pipe_cmd(self, cfg: StationPipelineConfig | None = None) -> list[str]:
        if not self.ffplay_bin:
            raise FileNotFoundError("ffplay")
        station_name = str(getattr(cfg, "station_name", "") or "").strip()
        named_bin = _get_named_ffplay(self.ffplay_bin, station_name) if station_name else self.ffplay_bin
        return build_ffplay_local_cmd(
            cfg if cfg is not None else StationPipelineConfig(
                input_uri="",
                icecast_host="127.0.0.1",
                icecast_port=8000,
                icecast_mount="/stream",
                icecast_user="source",
                icecast_password="",
                local_output_enabled=True,
                output_device_id="",
            ),
            named_bin,
        )

    def _spawn_transition_pair(
        self, ffmpeg_cmd: list[str], ffplay_cmd: list[str]
    ) -> tuple[subprocess.Popen, subprocess.Popen]:
        producer = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            consumer = subprocess.Popen(
                ffplay_cmd,
                stdin=producer.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            if producer.stdout:
                producer.stdout.close()
            self._terminate_process(producer)
            raise
        if producer.stdout:
            producer.stdout.close()
        return producer, consumer

    def _local_monitor_process(self):
        if self._local_process is not None:
            return self._local_process
        if (
            self._active_cfg
            and self._active_cfg.local_output_enabled
            and self._backend in {"ffmpeg-local", "ffmpeg-transition"}
        ):
            return self._process
        return None

    def _ensure_local_sink(self, cfg: StationPipelineConfig) -> bool:
        if not cfg.local_output_enabled or not self.ffplay_bin:
            return False
        desired_bin = _get_named_ffplay(self.ffplay_bin, str(cfg.station_name or "").strip())
        if self._local_sink is not None and str(self._local_sink.ffplay_bin or "") != str(desired_bin):
            self._local_sink.stop()
            self._local_sink = None
        if self._local_sink is None:
            self._local_sink = LocalAudioSink(desired_bin, self._spawn_process)
        try:
            self._local_sink.ensure_started(cfg)
            self._router.set_branch_health("local", True)
            return True
        except FileNotFoundError:
            self._router.set_branch_health("local", False)
            return False

    def _ensure_icecast_sink(self, cfg: StationPipelineConfig) -> bool:
        if not cfg.icecast_enabled or not self.ffmpeg_bin:
            return False
        protocol = str(getattr(cfg, "source_protocol", "icecast") or "icecast").strip().lower()
        if protocol not in {"icecast", "shoutcast"}:
            raise ValueError("unsupported source protocol")
        active_protocol = str(
            getattr(self._icecast_sink, "protocol", "icecast")
            if self._icecast_sink is not None
            else ""
        ).lower()
        if self._icecast_sink is not None and active_protocol != protocol:
            self._icecast_sink.stop()
            self._icecast_sink = None
        if self._icecast_sink is None:
            if protocol == "shoutcast":
                self._icecast_sink = ShoutcastAudioSink(
                    self.ffmpeg_bin,
                    self._spawn_process,
                )
            else:
                self._icecast_sink = IcecastAudioSink(
                    self.ffmpeg_bin,
                    self._spawn_process,
                    initial_connect_spread_sec=30.0,
                    drop_on_backpressure=True,
                )
        try:
            self._icecast_sink.ensure_started(cfg)
            self._router.set_branch_health("icecast", True)
            return True
        except (FileNotFoundError, RuntimeError) as exc:
            _log.warning(
                "%s sink unavailable for station %s: %s",
                protocol,
                self._live_station_id(),
                exc,
            )
            self._router.set_branch_health("icecast", False)
            return False
        except Exception as exc:
            _log.exception(
                "%s sink failed for station %s: %s",
                protocol,
                self._live_station_id(),
                exc,
            )
            self._router.set_branch_health("icecast", False)
            return False

    def _ensure_extra_icecast_sinks(
        self, cfg: StationPipelineConfig
    ) -> dict[str, bool]:
        desired = self._extra_output_configs(cfg)
        with self._extra_icecast_lock:
            for branch in set(self._extra_icecast_sinks) - set(desired):
                sink = self._extra_icecast_sinks.pop(branch)
                try:
                    sink.stop()
                finally:
                    self._router.set_branch_health(branch, False)
            self._extra_icecast_configs = dict(desired)
            results: dict[str, bool] = {}
            for branch, output_cfg in desired.items():
                protocol = str(output_cfg.source_protocol or "icecast").strip().lower()
                if protocol not in {"icecast", "shoutcast"}:
                    self._router.set_branch_health(branch, False)
                    results[branch] = False
                    continue
                sink = self._extra_icecast_sinks.get(branch)
                active_protocol = str(
                    getattr(sink, "protocol", "icecast") if sink is not None else ""
                ).lower()
                if sink is not None and active_protocol != protocol:
                    sink.stop()
                    sink = None
                    self._extra_icecast_sinks.pop(branch, None)
                if sink is None:
                    if protocol == "shoutcast":
                        sink = ShoutcastAudioSink(self.ffmpeg_bin, self._spawn_process)
                    else:
                        sink = IcecastAudioSink(
                            self.ffmpeg_bin,
                            self._spawn_process,
                            initial_connect_spread_sec=30.0,
                            drop_on_backpressure=True,
                        )
                    self._extra_icecast_sinks[branch] = sink
                try:
                    sink.ensure_started(output_cfg)
                    healthy = bool(sink.is_running())
                except Exception as exc:
                    healthy = False
                    _log.warning(
                        "Additional %s sink unavailable for station %s mount=%s: %s",
                        protocol,
                        self._live_station_id(),
                        output_cfg.icecast_mount,
                        exc,
                    )
                self._router.set_branch_health(branch, healthy)
                results[branch] = healthy
            return results

    def refresh_extra_icecast_outputs(
        self, outputs: tuple[dict, ...]
    ) -> dict[str, object]:
        """Hot-apply quality branches without restarting the programme producer."""
        with self._extra_icecast_lock:
            active_cfg = self._active_cfg
            if active_cfg is None:
                return {
                    "running": False,
                    "producer_preserved": True,
                    "branches": {},
                }
            updated_cfg = replace(
                active_cfg,
                extra_icecast_outputs=tuple(dict(item) for item in outputs),
            )
            producer = self._process
            results = self._ensure_extra_icecast_sinks(updated_cfg)
            self._active_cfg = updated_cfg
            self._active_signature = self._signature(updated_cfg)
            return {
                "running": bool(self.is_running()),
                "producer_preserved": self._process is producer,
                "branches": dict(results),
            }

    def _spawn_pcm_producer(self, cfg: StationPipelineConfig, sink_stdin):
        if not self.ffmpeg_bin:
            raise FileNotFoundError("ffmpeg")
        if sink_stdin is None:
            raise RuntimeError("sink stdin unavailable")
        cmd = build_ffmpeg_pcm_producer_cmd(cfg, self.ffmpeg_bin)
        return self._spawn_process(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=sink_stdin,
            stderr=subprocess.DEVNULL,
        )

    def _spawn_direct_icecast_process(self, cfg: StationPipelineConfig):
        if not self.ffmpeg_bin:
            raise FileNotFoundError("ffmpeg")
        cmd = build_ffmpeg_icecast_cmd(cfg, self.ffmpeg_bin)
        proc = self._spawn_process(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            # This direct path has no long-lived stderr reader.  Never leave a
            # broadcast encoder attached to a bounded pipe it can eventually
            # fill and deadlock on.
            stderr=subprocess.DEVNULL,
        )
        time.sleep(_DIRECT_ICECAST_STARTUP_GRACE_SECONDS)
        if proc.poll() is None:
            return proc

        _log.warning("Direct Icecast ffmpeg exited during startup with code %s", proc.returncode)
        raise RuntimeError(f"Icecast source failed during startup with code {proc.returncode}")

    def _live_station_id(self) -> int | None:
        if self.station_id is None:
            return None
        return int(self.station_id)

    def _live_snapshot(self) -> dict:
        station_id = self._live_station_id()
        if station_id is None or self.live_mic_registry is None:
            return dict(_DEFAULT_LIVE_SNAPSHOT)
        snapshot = getattr(self.live_mic_registry, "snapshot", None)
        if not callable(snapshot):
            return dict(_DEFAULT_LIVE_SNAPSHOT)
        try:
            payload = snapshot(station_id)
        except Exception:
            return dict(_DEFAULT_LIVE_SNAPSHOT)
        result = dict(_DEFAULT_LIVE_SNAPSHOT)
        result.update(dict(payload or {}))
        return result

    def _guest_audio_provider(self):
        if self.guest_audio_registry is not None:
            return self.guest_audio_registry
        from app.audio.guest_audio_registry import guest_audio_registry

        return guest_audio_registry

    def _should_use_live_mix(self) -> bool:
        snapshot = self._live_snapshot()
        if bool(snapshot.get("transmitting") or snapshot.get("active_user")):
            return True
        try:
            station_id = self._live_station_id()
            if station_id is not None and self._guest_audio_provider().has_on_air(
                station_id
            ):
                return True
        except Exception:
            pass
        return bool(self._sound_effect_player.has_active)

    def _live_audio_settings(self) -> dict[str, float | str]:
        station_id = self._live_station_id()
        payload = {}
        provider = self.live_settings_provider
        if callable(provider):
            try:
                payload = provider(station_id) if station_id is not None else provider()
            except TypeError:
                payload = provider()
            except Exception:
                payload = {}
        return {
            "program_music_mode": _normalize_program_music_mode(
                (payload or {}).get("program_music_mode")
            ),
            "mic_gain": _normalize_live_gain(
                (payload or {}).get("mic_gain"),
                _DEFAULT_LIVE_AUDIO_SETTINGS["mic_gain"],
            ),
            "music_gain": _normalize_live_gain(
                (payload or {}).get("music_gain"),
                _DEFAULT_LIVE_AUDIO_SETTINGS["music_gain"],
            ),
            "duck_level": _normalize_duck_level((payload or {}).get("duck_level")),
        }

    def _read_live_mic_pcm(self, num_bytes: int) -> bytes:
        requested = max(0, int(num_bytes))
        if requested <= 0:
            return b""
        station_id = self._live_station_id()
        if station_id is None or self.live_mic_registry is None:
            return b"\x00" * requested
        reader = getattr(self.live_mic_registry, "read_pcm", None)
        if not callable(reader):
            return b"\x00" * requested
        try:
            return reader(station_id, requested)
        except Exception:
            return b"\x00" * requested

    def _write_live_mix_chunk(
        self, chunk: bytes, generation: int | None = None
    ) -> None:
        self._write_pcm_chunk_to_targets(
            chunk, self._pcm_output_targets(), generation=generation
        )

    @staticmethod
    def _sum_mono_pcm(left: bytes, right: bytes) -> bytes:
        length = max(len(left), len(right))
        output = bytearray(length)
        for offset in range(0, length - 1, 2):
            a = int.from_bytes(left[offset : offset + 2], "little", signed=True) if offset + 2 <= len(left) else 0
            b = int.from_bytes(right[offset : offset + 2], "little", signed=True) if offset + 2 <= len(right) else 0
            value = max(-32768, min(32767, a + b))
            output[offset : offset + 2] = value.to_bytes(2, "little", signed=True)
        return bytes(output)

    def _live_mix_loop(self, producer, generation: int) -> None:
        stdout = getattr(producer, "stdout", None)
        if stdout is None:
            return
        inactive_since = None
        demote_requested = False
        try:
            while (
                not self._live_mix_stop.is_set()
                and self._generation_is_current(generation)
            ):
                chunk = stdout.read(_LIVE_MIX_CHUNK_BYTES)
                if not chunk:
                    if producer.poll() is not None:
                        break
                    time.sleep(0.01)
                    continue
                self._last_program_pcm_monotonic = time.monotonic()
                mic_pcm = self._read_live_mic_pcm(len(chunk) // 2)
                try:
                    guest_pcm = self._guest_audio_provider().read_on_air_pcm(
                        int(self._live_station_id() or 0), len(chunk) // 2
                    )
                    mic_pcm = self._sum_mono_pcm(mic_pcm, guest_pcm)
                except Exception:
                    pass
                effect_pcm = self._sound_effect_player.read_pcm(len(chunk) // 2)
                mixed = self._live_audio_mixer.mix_pcm_chunk(
                    chunk,
                    mic_pcm,
                    effect_pcm=effect_pcm,
                    **self._live_audio_settings(),
                )
                self._write_live_mix_chunk(mixed, generation)
                if self._should_use_live_mix():
                    inactive_since = None
                else:
                    inactive_since = inactive_since or time.monotonic()
                    if (
                        time.monotonic() - inactive_since
                        >= _LIVE_MIX_RETURN_GRACE_SECONDS
                    ):
                        demote_requested = True
                        break
        except Exception as exc:
            _log.warning(
                "Live mix loop failed for station %s: %s",
                self._live_station_id(),
                exc,
            )
        if demote_requested and self._generation_is_current(generation):
            threading.Thread(
                target=self._demote_live_mix_if_current,
                args=(generation,),
                name=f"station-live-return-{self._live_station_id() or 'unknown'}",
                daemon=True,
            ).start()

    def _demote_live_mix_if_current(self, generation: int) -> None:
        if not self._generation_is_current(generation):
            return
        if self._backend != "live-mix" or self._active_cfg is None:
            return
        if self._should_use_live_mix():
            return
        self._restart_with(
            self._active_cfg,
            start_offset_seconds=self._current_offset_seconds(),
        )

    def _start_live_mix_worker(self, producer) -> None:
        self._live_mix_stop.clear()
        generation = self._current_playout_generation()
        self._live_mix_thread = threading.Thread(
            target=self._live_mix_loop,
            args=(producer, generation),
            name=f"station-live-mix-{self._live_station_id() or 'unknown'}",
            daemon=True,
        )
        self._live_mix_thread.start()

    def _stop_live_mix_worker(self) -> None:
        self._live_mix_stop.set()
        process = self._process
        stdout = getattr(process, "stdout", None) if process is not None else None
        if stdout is not None:
            try:
                stdout.close()
            except Exception:
                pass
        if self._live_mix_thread is not None:
            self._live_mix_thread.join(timeout=1.0)
        self._live_mix_thread = None
        self._live_mix_stop.clear()

    def _icecast_pipe_loop(self, producer, sink, generation: int) -> None:
        stdout = getattr(producer, "stdout", None)
        initial_targets = self._icecast_output_targets()
        if stdout is None or not initial_targets:
            self._router.set_branch_health("icecast", False)
            return
        pacing_sink = sink or initial_targets[0][1]
        queued_chunks = 0
        health_snapshot = getattr(pacing_sink, "health_snapshot", None)
        if callable(health_snapshot):
            try:
                queued_chunks = max(
                    0,
                    int(health_snapshot().get("queued_pcm_chunks") or 0),
                )
            except Exception:
                queued_chunks = 0
        startup_reserve_remaining = max(
            0,
            _ICECAST_PIPE_STARTUP_RESERVE_CHUNKS - queued_chunks,
        )
        pcm_clock_started = False
        next_delivery = time.monotonic()
        bytes_written = 0
        last_log = time.monotonic()
        try:
            while (
                not self._icecast_pipe_stop.is_set()
                and self._generation_is_current(generation)
            ):
                chunk = stdout.read(_LIVE_MIX_CHUNK_BYTES)
                if not chunk:
                    if producer.poll() is not None:
                        break
                    time.sleep(0.01)
                    continue
                # FFmpeg's -readrate clock is intentionally approximate and
                # can run about 1-2% fast on Windows.  Without a second exact
                # byte clock, every healthy sink queue eventually filled and
                # resynchronized by deleting programme audio.  Seed a small
                # reserve, then phase-lock delivery to the PCM sample clock.
                if pcm_clock_started:
                    now = time.monotonic()
                    if now < next_delivery and self._icecast_pipe_stop.wait(
                        next_delivery - now
                    ):
                        break
                    if not self._generation_is_current(generation):
                        break
                # Decode progress is distinct from remote output health.  The
                # listener mount may be offline while the program remains
                # healthy and ready to reconnect.
                self._last_program_pcm_monotonic = time.monotonic()
                if not self._generation_is_current(generation):
                    break
                self._write_pcm_chunk_to_targets(
                    chunk,
                    self._icecast_output_targets(),
                    generation=generation,
                )
                bytes_written += len(chunk)
                delivered_at = time.monotonic()
                frame_seconds = len(chunk) / _PCM_BYTES_PER_SECOND
                if startup_reserve_remaining > 0:
                    startup_reserve_remaining -= 1
                    if startup_reserve_remaining == 0:
                        pcm_clock_started = True
                        next_delivery = delivered_at + frame_seconds
                elif not pcm_clock_started:
                    pcm_clock_started = True
                    next_delivery = delivered_at + frame_seconds
                else:
                    # Preserve the long-term PCM phase after ordinary scheduler
                    # jitter, but permit only one frame of catch-up after a
                    # genuinely late Windows scheduling quantum.
                    next_delivery = max(
                        next_delivery + frame_seconds,
                        delivered_at,
                    )
                now = delivered_at
                if now - last_log >= 5.0:
                    _log.debug(
                        "Icecast PCM pipe wrote %s bytes for station %s",
                        bytes_written,
                        self._live_station_id(),
                    )
                    last_log = now
        except Exception as exc:
            self._router.set_branch_health("icecast", False)
            _log.warning(
                "Icecast PCM pipe failed for station %s after %s bytes: %s",
                self._live_station_id(),
                bytes_written,
                exc,
            )

    def _start_icecast_pipe_worker(self, producer, sink, generation: int) -> None:
        self._stop_icecast_pipe_worker()
        self._icecast_pipe_stop.clear()
        self._icecast_pipe_process = producer
        self._icecast_pipe_thread = threading.Thread(
            target=self._icecast_pipe_loop,
            args=(producer, sink, generation),
            name=f"station-icecast-pipe-{self._live_station_id() or 'unknown'}",
            daemon=True,
        )
        self._icecast_pipe_thread.start()

    def _stop_icecast_pipe_worker(self) -> None:
        self._icecast_pipe_stop.set()
        process = self._icecast_pipe_process
        stdout = getattr(process, "stdout", None) if process is not None else None
        if stdout is not None:
            try:
                stdout.close()
            except Exception:
                pass
        if self._icecast_pipe_thread is not None:
            self._icecast_pipe_thread.join(timeout=1.0)
        self._icecast_pipe_thread = None
        self._icecast_pipe_process = None
        self._icecast_pipe_stop.clear()

    def _spawn_live_mix_producer(
        self,
        cfg: StationPipelineConfig,
        start_offset_seconds: float = 0.0,
    ):
        if not self.ffmpeg_bin:
            raise FileNotFoundError("ffmpeg")
        cmd = build_ffmpeg_pcm_producer_cmd(
            cfg,
            self.ffmpeg_bin,
            start_offset_seconds=start_offset_seconds,
            # The pipe and every Icecast sink own exact PCM clocks. Seed their
            # bounded reserve before those clocks start, then let FFmpeg catch
            # up after a late Windows scheduling quantum. Running the decoder
            # at exactly 1.0x with no reserve caused recurring 21 ms silence
            # substitutions even though disk, encoder, and network were alive.
            initial_burst_seconds=LOCAL_MONITOR_INITIAL_BURST_SECONDS,
            catchup_rate=LOCAL_MONITOR_CATCHUP_RATE,
        )
        return self._spawn_process(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _launch_live_mix_state(
        self,
        cfg: StationPipelineConfig,
        target_signature: tuple,
        *,
        start_offset_seconds: float = 0.0,
    ) -> None:
        if (
            not cfg.icecast_enabled
            and not cfg.local_output_enabled
            and not self._extra_output_configs(cfg)
        ):
            raise ValueError("no output targets enabled")
        icecast_enabled = bool(cfg.icecast_enabled and self._ensure_icecast_sink(cfg))
        extra_results = self._ensure_extra_icecast_sinks(cfg)
        extra_enabled = any(extra_results.values())
        local_enabled = bool(cfg.local_output_enabled and self._ensure_local_sink(cfg))
        if (
            cfg.icecast_enabled
            and not icecast_enabled
            and not extra_enabled
            and not local_enabled
        ):
            raise FileNotFoundError("ffmpeg")
        if (
            cfg.local_output_enabled
            and not local_enabled
            and not icecast_enabled
            and not extra_enabled
        ):
            raise FileNotFoundError("ffplay")
        if not icecast_enabled and not extra_enabled and not local_enabled:
            raise ValueError("no output targets enabled")

        self._local_process = None
        self._process = self._spawn_live_mix_producer(
            cfg,
            start_offset_seconds=start_offset_seconds,
        )
        self._backend = "live-mix"
        self._router.set_branch_health("icecast", bool(icecast_enabled))
        self._router.set_branch_health("local", bool(local_enabled))
        self._mark_active_request(
            cfg,
            target_signature,
            started_monotonic=time.monotonic() - max(0.0, float(start_offset_seconds or 0.0)),
        )
        self._clear_transition_window()
        self._start_live_mix_worker(self._process)
        self._start_silence_floor_worker()

    def _spawn_local_pcm_producer(
        self,
        cfg: StationPipelineConfig,
        start_offset_seconds: float = 0.0,
    ):
        if not self._ensure_local_sink(cfg):
            raise FileNotFoundError("ffplay")
        sink_stdin = self._local_sink.stdin if self._local_sink else None
        if not self.ffmpeg_bin:
            raise FileNotFoundError("ffmpeg")
        if sink_stdin is None:
            raise RuntimeError("sink stdin unavailable")
        cmd = build_ffmpeg_local_pcm_cmd(
            cfg,
            self.ffmpeg_bin,
            start_offset_seconds=start_offset_seconds,
        )
        return self._spawn_process(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=sink_stdin,
            stderr=subprocess.DEVNULL,
        )

    def _spawn_icecast_pcm_producer(
        self,
        cfg: StationPipelineConfig,
        start_offset_seconds: float = 0.0,
    ):
        if not self._icecast_output_targets():
            if not self._ensure_icecast_sink(cfg):
                raise FileNotFoundError("ffmpeg")
        if not self.ffmpeg_bin:
            raise FileNotFoundError("ffmpeg")
        cmd = build_ffmpeg_pcm_producer_cmd(
            cfg,
            self.ffmpeg_bin,
            start_offset_seconds=start_offset_seconds,
            # Seed the bounded Icecast PCM reserve, then permit recovery from
            # ordinary Windows scheduling jitter without changing media time.
            initial_burst_seconds=LOCAL_MONITOR_INITIAL_BURST_SECONDS,
            catchup_rate=LOCAL_MONITOR_CATCHUP_RATE,
        )
        producer = self._spawn_process(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return producer

    def _spawn_crossfade_pcm_producer(
        self,
        current_cfg: StationPipelineConfig,
        next_cfg: StationPipelineConfig,
        current_offset_seconds: float,
        sink_stdin,
        *,
        buffered: bool,
    ):
        if not self.ffmpeg_bin:
            raise FileNotFoundError("ffmpeg")
        if sink_stdin is None:
            raise RuntimeError("sink stdin unavailable")
        # Both direct-local and piped-Icecast transitions need the initial
        # reserve. The Icecast pipe consumes the burst into its bounded queue
        # and then phase-locks delivery, so this cannot burst audio at TinyIce.
        del buffered
        cmd = build_ffmpeg_crossfade_pcm_cmd(
            current_cfg,
            next_cfg,
            ffmpeg_bin=self.ffmpeg_bin,
            current_offset_seconds=current_offset_seconds,
            realtime=True,
        )
        return self._spawn_process(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=sink_stdin,
            stderr=subprocess.DEVNULL,
        )

    def _stop_producers(self) -> None:
        active_input_uri = str(
            self._active_cfg.input_uri if self._active_cfg is not None else ""
        )
        self._next_playout_generation()
        self._stop_live_mix_worker()
        self._stop_icecast_pipe_worker()
        self._terminate_process(self._local_process)
        self._local_process = None
        self._terminate_process(self._process)
        self._process = None
        self._backend = "none"
        self._active_signature = None
        self._active_cfg = None
        self._active_started_monotonic = None
        self._clear_transition_window()
        self._router.set_branch_health("icecast", False)
        self._router.set_branch_health("local", False)
        if active_input_uri:
            release_fast_cached_uri(active_input_uri)

    def _stop_sinks(self) -> None:
        if self._icecast_sink is not None:
            self._icecast_sink.stop()
            self._icecast_sink = None
        with self._extra_icecast_lock:
            for branch, sink in list(self._extra_icecast_sinks.items()):
                try:
                    sink.stop()
                finally:
                    self._router.set_branch_health(branch, False)
            self._extra_icecast_sinks = {}
            self._extra_icecast_configs = {}
        if self._local_sink is not None:
            self._local_sink.stop()
            self._local_sink = None

    def _release_disabled_sinks(self, cfg: StationPipelineConfig) -> None:
        if not cfg.icecast_enabled and self._icecast_sink is not None:
            self._icecast_sink.stop()
            self._icecast_sink = None
        if not self._extra_output_configs(cfg):
            with self._extra_icecast_lock:
                for branch, sink in list(self._extra_icecast_sinks.items()):
                    try:
                        sink.stop()
                    finally:
                        self._router.set_branch_health(branch, False)
                self._extra_icecast_sinks = {}
                self._extra_icecast_configs = {}
        if not cfg.local_output_enabled and self._local_sink is not None:
            self._local_sink.stop()
            self._local_sink = None

    def _launch_steady_state(
        self,
        cfg: StationPipelineConfig,
        target_signature: tuple,
        *,
        start_offset_seconds: float = 0.0,
    ) -> None:
        extra_configured = bool(self._extra_output_configs(cfg))
        if (
            not cfg.icecast_enabled
            and not cfg.local_output_enabled
            and not extra_configured
        ):
            raise ValueError("no output targets enabled")
        if (cfg.icecast_enabled or extra_configured) and not cfg.local_output_enabled:
            try:
                icecast_enabled = bool(
                    cfg.icecast_enabled and self._ensure_icecast_sink(cfg)
                )
                extra_results = self._ensure_extra_icecast_sinks(cfg)
                if not icecast_enabled and not any(extra_results.values()):
                    raise FileNotFoundError("ffmpeg")
                self._process = self._spawn_icecast_pcm_producer(
                    cfg,
                    start_offset_seconds=start_offset_seconds,
                )
            except Exception:
                self._process = None
                self._backend = "none"
                self._active_signature = None
                self._active_cfg = None
                self._active_started_monotonic = None
                self._router.set_branch_health("icecast", False)
                self._router.set_branch_health("local", False)
                raise
            self._backend = "ffmpeg"
            self._router.set_branch_health("icecast", bool(icecast_enabled))
            self._router.set_branch_health("local", False)
            self._mark_active_request(
                cfg,
                target_signature,
                started_monotonic=time.monotonic()
                - max(0.0, float(start_offset_seconds or 0.0)),
            )
            self._start_icecast_pipe_worker(
                self._process,
                self._icecast_sink,
                self._playout_generation,
            )
            self._start_silence_floor_worker()
            self._clear_transition_window()
            return
        pipeline = build_gst_pipeline(cfg)
        gst_cmd = [self.gst_bin, "-e", pipeline]
        try:
            if self._extra_output_configs(cfg):
                raise FileNotFoundError("multiple Icecast outputs require FFmpeg fan-out")
            # GStreamer startup does not accept an input seek in this runtime.
            # Recovery must resume deterministically, so use the FFmpeg path
            # whenever a non-zero offset is requested.
            if float(start_offset_seconds or 0.0) > 0.0:
                raise FileNotFoundError("offset resume requires ffmpeg")
            self._process = self._spawn_process(gst_cmd)
            self._backend = "gst"
            self._router.set_branch_health("icecast", bool(cfg.icecast_enabled))
            self._router.set_branch_health("local", bool(cfg.local_output_enabled))
            self._mark_active_request(cfg, target_signature)
            self._clear_transition_window()
            return
        except FileNotFoundError:
            pass

        icecast_enabled = bool(cfg.icecast_enabled and self._ensure_icecast_sink(cfg))
        extra_results = self._ensure_extra_icecast_sinks(cfg)
        extra_enabled = any(extra_results.values())
        local_enabled = bool(cfg.local_output_enabled and self._ensure_local_sink(cfg))
        self._local_process = None

        if cfg.icecast_enabled or extra_enabled:
            if not icecast_enabled and not extra_enabled:
                raise FileNotFoundError("gst-launch-1.0 or ffmpeg")

            self._process = self._spawn_icecast_pcm_producer(
                cfg,
                start_offset_seconds=start_offset_seconds,
            )
            self._backend = "ffmpeg"
            self._router.set_branch_health("icecast", bool(icecast_enabled))
            self._router.set_branch_health("local", False)
            self._mark_active_request(
                cfg,
                target_signature,
                started_monotonic=time.monotonic()
                - max(0.0, float(start_offset_seconds or 0.0)),
            )
            self._start_icecast_pipe_worker(
                self._process,
                self._icecast_sink,
                self._playout_generation,
            )
            self._start_silence_floor_worker()
            self._clear_transition_window()

            if local_enabled:
                try:
                    self._local_process = self._spawn_local_pcm_producer(
                        cfg,
                        start_offset_seconds=start_offset_seconds,
                    )
                    self._router.set_branch_health("local", True)
                except Exception:
                    self._local_process = None
                    self._router.set_branch_health("local", False)
            return

        # Local-only fallback path when GStreamer is unavailable.
        if not self.ffmpeg_bin or not local_enabled:
            raise FileNotFoundError("gst-launch-1.0 or ffmpeg/ffplay")
        self._process = self._spawn_local_pcm_producer(
            cfg,
            start_offset_seconds=start_offset_seconds,
        )
        self._backend = "ffmpeg-local"
        self._router.set_branch_health("icecast", False)
        self._router.set_branch_health("local", True)
        self._mark_active_request(
            cfg,
            target_signature,
            started_monotonic=time.monotonic()
            - max(0.0, float(start_offset_seconds or 0.0)),
        )
        self._clear_transition_window()

    def _restart_with(
        self,
        cfg: StationPipelineConfig,
        *,
        start_offset_seconds: float = 0.0,
    ) -> None:
        target_signature = self._signature(cfg)
        self._last_transition_mode = "restart"
        self._stop_producers()
        self._release_disabled_sinks(cfg)
        if self._should_use_live_mix():
            self._launch_live_mix_state(
                cfg,
                target_signature,
                start_offset_seconds=start_offset_seconds,
            )
            return
        self._launch_steady_state(
            cfg,
            target_signature,
            start_offset_seconds=start_offset_seconds,
        )

    def _start_crossfade(self, cfg: StationPipelineConfig) -> None:
        if self._active_cfg is None or not self.ffmpeg_bin:
            raise RuntimeError("transition backend unavailable")
        previous_cfg = self._active_cfg
        # Validate both source files before stopping the current producer. A
        # cold H: drive, temporary network disconnect, or a removed queue file
        # must defer the handoff; killing the current process first turns that
        # recoverable condition into an audible hard cut.
        for transition_cfg in (self._active_cfg, cfg):
            uri = str(transition_cfg.input_uri or "")
            if not uri or "://" in uri:
                continue
            if not os.path.isfile(uri):
                raise RuntimeError("transition input unavailable")
        target_signature = self._signature(cfg)
        started_monotonic = time.monotonic()
        current_offset_seconds = self._current_offset_seconds()
        icecast_enabled = bool(cfg.icecast_enabled and self._ensure_icecast_sink(cfg))
        extra_results = self._ensure_extra_icecast_sinks(cfg)
        extra_enabled = any(extra_results.values())
        local_enabled = bool(cfg.local_output_enabled and self._ensure_local_sink(cfg))
        if (
            cfg.local_output_enabled
            and not local_enabled
            and not icecast_enabled
            and not extra_enabled
        ):
            raise RuntimeError("local sink unavailable")
        if (
            cfg.icecast_enabled
            and not icecast_enabled
            and not extra_enabled
            and not local_enabled
        ):
            raise RuntimeError("icecast sink unavailable")
        current_process = self._process
        current_local_process = self._local_process
        new_process = None
        new_local_process = None
        try:
            self._terminate_process(current_local_process)
            self._local_process = None
            self._terminate_process(current_process)
            self._process = None
            if icecast_enabled or extra_enabled:
                new_process = self._spawn_crossfade_pcm_producer(
                    self._active_cfg,
                    cfg,
                    current_offset_seconds,
                    subprocess.PIPE,
                    buffered=False,
                )
            elif local_enabled:
                sink_stdin = self._local_sink.stdin if self._local_sink else None
                new_process = self._spawn_crossfade_pcm_producer(
                    self._active_cfg,
                    cfg,
                    current_offset_seconds,
                    sink_stdin,
                    buffered=True,
                )
            else:
                raise RuntimeError("no transition output target available")
            if (icecast_enabled or extra_enabled) and local_enabled:
                sink_stdin = self._local_sink.stdin if self._local_sink else None
                new_local_process = self._spawn_crossfade_pcm_producer(
                    self._active_cfg,
                    cfg,
                    current_offset_seconds,
                    sink_stdin,
                    buffered=True,
                )
            self._process = new_process
            self._local_process = new_local_process
            self._backend = "ffmpeg-transition"
            self._router.set_branch_health("icecast", bool(icecast_enabled))
            self._router.set_branch_health("local", bool(local_enabled))
            self._mark_active_request(
                cfg, target_signature, started_monotonic=started_monotonic
            )
            if icecast_enabled or extra_enabled:
                self._start_icecast_pipe_worker(
                    self._process,
                    self._icecast_sink,
                    self._playout_generation,
                )
                self._start_silence_floor_worker()
            self._transition_until_monotonic = started_monotonic + max(
                0.0, float(cfg.crossfade_seconds or 0.0)
            )
            self._last_transition_mode = "crossfade"
            # The transition producer reads the old track only for the fade
            # head.  Release it after that reader has safely moved on.
            release_fast_cached_uri(
                str(previous_cfg.input_uri or ""),
                delay_seconds=max(2.0, float(cfg.crossfade_seconds or 0.0) + 2.0),
            )
        except Exception:
            self._terminate_process(new_process)
            self._terminate_process(new_local_process)
            self._process = None
            self._local_process = None
            self._backend = "none"
            self._active_signature = None
            self._active_cfg = None
            self._active_started_monotonic = None
            self._clear_transition_window()
            self._router.set_branch_health("icecast", False)
            self._router.set_branch_health("local", False)
            raise

    def start(
        self,
        cfg: StationPipelineConfig,
        *,
        start_offset_seconds: float = 0.0,
    ) -> None:
        self._refresh_runtime_bins()
        target_signature = self._signature(cfg)
        live_mix_requested = self._should_use_live_mix()
        if self.is_running():
            if self._active_signature == target_signature:
                self._active_cfg = cfg
                if float(start_offset_seconds or 0.0) > 0.0:
                    self._restart_with(
                        cfg,
                        start_offset_seconds=start_offset_seconds,
                    )
                    return
                if live_mix_requested and self._backend != "live-mix":
                    self._restart_with(
                        cfg,
                        start_offset_seconds=self._current_offset_seconds(),
                    )
                    return
                self._last_transition_mode = "noop"
                return
            if live_mix_requested:
                self._restart_with(
                    cfg,
                    start_offset_seconds=start_offset_seconds,
                )
                return
            if self._backend == "ffmpeg-direct":
                self._restart_with(
                    cfg,
                    start_offset_seconds=start_offset_seconds,
                )
                return
            transition_cfg = (
                self._transition_cfg(self._active_cfg, cfg)
                if self._active_cfg is not None
                else cfg
            )
            if (
                self._active_cfg
                and float(start_offset_seconds or 0.0) <= 0.0
                and self._can_crossfade(self._active_cfg, transition_cfg)
                and self._transition_backend_supported(transition_cfg)
            ):
                try:
                    self._start_crossfade(transition_cfg)
                    return
                except Exception:
                    self._restart_with(
                        cfg,
                        start_offset_seconds=start_offset_seconds,
                    )
                    return
            self._restart_with(
                cfg,
                start_offset_seconds=start_offset_seconds,
            )
            return
        if self._process is not None or self._local_process is not None:
            self._stop_producers()
            self._release_disabled_sinks(cfg)
        self._last_transition_mode = "start"
        if live_mix_requested:
            self._launch_live_mix_state(
                cfg,
                target_signature,
                start_offset_seconds=start_offset_seconds,
            )
            return
        self._launch_steady_state(
            cfg,
            target_signature,
            start_offset_seconds=start_offset_seconds,
        )

    def promote_live_mix(self, *, force: bool = False) -> None:
        if self._backend == "live-mix":
            return
        if not self.is_running() or self._active_cfg is None:
            return
        if not force and not self._should_use_live_mix():
            return
        self._restart_with(
            self._active_cfg,
            start_offset_seconds=self._current_offset_seconds(),
        )

    def recover_outputs(self) -> dict:
        """Rebuild failed output branches while keeping the current track position."""
        cfg = self._active_cfg
        if cfg is None:
            raise RuntimeError("no active playout request")
        offset_seconds = self._current_offset_seconds()
        self._restart_with(cfg, start_offset_seconds=offset_seconds)
        return self.status()

    def stop(self) -> None:
        station_id = self._live_station_id()
        if station_id is not None:
            try:
                from app.services.program_recording import program_recording_service
                program_recording_service.interrupt_station(station_id, "runtime_stopped")
            except Exception:
                pass
        self._stop_silence_floor_worker()
        self._stop_producers()
        self._stop_sinks()
        self._terminate_owned_processes()

    def _program_running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def _local_program_running(self) -> bool:
        local_proc = self._local_monitor_process()
        return bool(local_proc and local_proc.poll() is None)

    def _icecast_sink_running(self) -> bool:
        return bool(self._icecast_sink and self._icecast_sink.is_running())

    def _extra_icecast_sinks_running(self) -> bool:
        with self._extra_icecast_lock:
            return any(
                bool(sink and sink.is_running())
                for sink in self._extra_icecast_sinks.values()
            )

    def _local_sink_running(self) -> bool:
        return bool(self._local_sink and self._local_sink.is_running())

    def _program_pcm_health(self) -> tuple[float, bool]:
        age = max(
            0.0,
            time.monotonic() - float(self._last_program_pcm_monotonic or 0.0),
        )
        monitored = self._backend in {
            "ffmpeg",
            "live-mix",
        }
        stalled = bool(
            monitored
            and self._program_running()
            and age >= _PROGRAM_PCM_STALL_SECONDS
        )
        return age, stalled

    def _output_feed_active(self) -> bool:
        _, program_pcm_stalled = self._program_pcm_health()
        if self._backend in {"ffmpeg", "ffmpeg-transition"}:
            return bool(
                (
                    self._router.is_output_active("icecast")
                    and self._icecast_sink_running()
                    and not program_pcm_stalled
                )
                or (
                    self._extra_icecast_sinks_running()
                    and not program_pcm_stalled
                )
                or (
                    self._router.is_output_active("local")
                    and self._local_sink_running()
                )
            )
        if self._backend == "ffmpeg-local":
            return bool(self._local_sink_running() and self._program_running())
        if self._backend == "live-mix":
            return bool(
                self._program_running()
                and not program_pcm_stalled
                and (
                    (
                        self._router.is_output_active("icecast")
                        and self._icecast_sink_running()
                    )
                    or self._extra_icecast_sinks_running()
                    or (
                        self._router.is_output_active("local")
                        and self._local_sink_running()
                    )
                )
            )
        return self._program_running()

    def is_running(self) -> bool:
        return self._program_running()

    def set_branch_health(self, branch: str, healthy: bool) -> None:
        self._router.set_branch_health(branch, healthy)

    def branch_health(self) -> dict[str, bool]:
        icecast = self._router.is_output_active("icecast")
        local = self._router.is_output_active("local")
        local_sink_running = self._local_sink_running()
        icecast_sink_running = self._icecast_sink_running()
        _, program_pcm_stalled = self._program_pcm_health()
        if self._backend in {"ffmpeg", "ffmpeg-transition"}:
            icecast = icecast and icecast_sink_running and not program_pcm_stalled
            local = local and local_sink_running and self._local_program_running()
        if self._backend == "ffmpeg-local":
            icecast = False
            local = local and self._program_running() and local_sink_running
        if self._backend == "live-mix":
            icecast = (
                icecast
                and self._program_running()
                and icecast_sink_running
                and not program_pcm_stalled
            )
            local = (
                local
                and self._program_running()
                and local_sink_running
                and not program_pcm_stalled
            )
        if self._backend == "gst":
            local = local and True
        branches = {
            "icecast": bool(icecast),
            "local": bool(local),
        }
        with self._extra_icecast_lock:
            extra_sinks = list(self._extra_icecast_sinks.items())
        for branch, sink in extra_sinks:
            healthy = bool(
                self._router.is_output_active(branch)
                and sink.is_running()
                and not program_pcm_stalled
            )
            branches[branch] = healthy
        return branches

    def status(self) -> dict:
        live_snapshot = self._live_snapshot()
        live_settings = self._live_audio_settings()
        program_running = self._program_running()
        output_feed_active = self._output_feed_active()
        program_pcm_age, program_pcm_stalled = self._program_pcm_health()
        icecast_mount_health = (
            self._icecast_sink.health_snapshot()
            if self._icecast_sink is not None
            else {
                "process_running": False,
                "mount_healthy": None,
                "consecutive_probe_failures": 0,
            }
        )
        extra_icecast_mounts = []
        with self._extra_icecast_lock:
            extra_configs = sorted(self._extra_icecast_configs.items())
            extra_sinks = dict(self._extra_icecast_sinks)
        for branch, output_cfg in extra_configs:
            sink = extra_sinks.get(branch)
            health = (
                sink.health_snapshot()
                if sink is not None and callable(getattr(sink, "health_snapshot", None))
                else {
                    "process_running": bool(sink and sink.is_running()),
                    "mount_healthy": None,
                    "consecutive_probe_failures": 0,
                }
            )
            extra_icecast_mounts.append(
                {
                    "branch": branch,
                    "mount": output_cfg.icecast_mount,
                    "codec_profile": output_cfg.stream_codec_profile,
                    "bitrate_kbps": int(output_cfg.stream_bitrate_kbps),
                    "running": bool(sink and sink.is_running()),
                    "health": health,
                }
            )
        branch_health = self.branch_health()
        # Branch health proves that current PCM is reaching an output worker.
        # Delivery health is intentionally stricter: a network branch is only
        # healthy after the Icecast transport has verified the mount itself.
        # Keeping these signals separate lets the writer retry an origin outage
        # without making the supervisor restart healthy program playout.
        delivery_health = dict(branch_health)
        delivery_health["icecast"] = bool(
            branch_health.get("icecast")
            and icecast_mount_health.get("mount_healthy") is True
        )
        for item in extra_icecast_mounts:
            branch = str(item.get("branch") or "")
            if branch:
                delivery_health[branch] = bool(
                    branch_health.get(branch)
                    and dict(item.get("health") or {}).get("mount_healthy") is True
                )
        return {
            "running": output_feed_active,
            "program_running": program_running,
            # Internal consumers use this to prove that the decoder is
            # rendering the queue-owned item, not merely that some continuity
            # process is alive. API health responses redact the local path.
            "active_input_uri": str(
                self._active_cfg.input_uri if self._active_cfg is not None else ""
            ),
            "output_feed_active": output_feed_active,
            "program_pcm_age_seconds": round(program_pcm_age, 3),
            "program_pcm_stalled": program_pcm_stalled,
            "icecast_sink_running": self._icecast_sink_running(),
            "icecast_mount_health": icecast_mount_health,
            "extra_icecast_mounts": extra_icecast_mounts,
            "source_protocol": str(
                getattr(self._active_cfg, "source_protocol", "icecast")
                if self._active_cfg is not None
                else "icecast"
            ),
            "local_sink_running": self._local_sink_running(),
            "backend": str(self._backend or "none"),
            "transition_mode": str(self._last_transition_mode or "none"),
            "transition_active": self._is_transition_active(),
            "branch_health": branch_health,
            "delivery_health": delivery_health,
            "elapsed": self._current_offset_seconds(),
            "live_input_enabled": bool(live_snapshot.get("live_input_enabled")),
            "live_mic_active": bool(live_snapshot.get("transmitting")),
            "live_mic_user": live_snapshot.get("active_user"),
            "live_mic_receiving": bool(live_snapshot.get("receiving")),
            "live_mic_level_db": float(live_snapshot.get("level_db", -60.0)),
            "live_mic_peak_db": float(live_snapshot.get("peak_db", -60.0)),
            "live_mic_buffer_bytes": int(live_snapshot.get("buffer_bytes", 0)),
            "program_music_mode": str(live_settings["program_music_mode"]),
            "live_mix_backend": "active" if self._backend == "live-mix" else "inactive",
        }
