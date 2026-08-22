from __future__ import annotations

import os
import sys
import asyncio
import py_compile
import shutil
from pathlib import Path


_STAGED_UPDATE_ID = "aac-policy-20260823T002626"
_STAGED_UPDATE_FILES = (
    "app/audio/gst_pipeline.py",
    "app/audio/icecast_source_transport.py",
    "app/audio/icecast_audio_sink.py",
    "app/services/quality_outputs.py",
    "app/services/stream_config_service.py",
    "app/services/codec_migration.py",
    "app/services/encoder_capabilities.py",
    "app/engine/runtime_registry.py",
    "app/api/setup.py",
    "app/api/stations.py",
    "app/api/stream_config.py",
    "app/api/streaming.py",
    "app/api/legacy.py",
    "app/services/replication_applier.py",
    "app/static/js/setup-wizard.js",
    "app/static/onair/index.html",
    "app/static/onair/app.js",
    "app/db.py",
    "app/repositories/station_output_repo.py",
)


def _service_data_root() -> Path:
    return (
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "RadioTEDU"
        / "OnAir"
    ).resolve()


def apply_pending_staged_update(repository_root: Path, data_root: Path) -> bool:
    """Apply a validated update once, restoring old source on any copy failure."""

    stage = data_root / "staged" / _STAGED_UPDATE_ID
    pending = stage / ".apply-on-next-start"
    if not pending.is_file():
        return False
    sources = [(relative, stage / relative) for relative in _STAGED_UPDATE_FILES]
    for relative, source in sources:
        if not source.is_file():
            print(f"Staged update retained: required file is missing ({relative})")
            return False
        if source.suffix.lower() == ".py":
            try:
                py_compile.compile(str(source), doraise=True)
            except py_compile.PyCompileError:
                print(f"Staged update retained: Python validation failed ({relative})")
                return False

    backup = data_root / "backups" / _STAGED_UPDATE_ID / "preapply-live"
    copied: list[str] = []
    try:
        for relative, _source in sources:
            current = repository_root / relative
            if not current.is_file():
                raise FileNotFoundError(relative)
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            if not saved.exists():
                shutil.copy2(current, saved)
        for relative, source in sources:
            target = repository_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(relative)
        pending.replace(stage / ".applied")
        print(f"Applied staged update {_STAGED_UPDATE_ID} ({len(copied)} files)")
        return True
    except Exception as exc:
        for relative in copied:
            saved = backup / relative
            target = repository_root / relative
            if saved.is_file():
                try:
                    shutil.copy2(saved, target)
                except OSError:
                    pass
        print(f"Staged update retained after rollback ({type(exc).__name__})")
        return False


def _resolve_tools_root(repository_root: Path) -> Path:
    candidates: list[Path] = []
    last_build = repository_root / "last_build_path.txt"
    if last_build.is_file():
        try:
            recorded = Path(last_build.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError):
            recorded = Path()
        if recorded.is_absolute():
            candidates.append(recorded.parent / "tools")
    candidates.append(repository_root / "dist" / "backend" / "tools")
    candidates.extend(
        item / "tools"
        for item in sorted(
            (repository_root / "dist").glob("backend-*"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
            reverse=True,
        )
    )
    for candidate in candidates:
        if (candidate / "bin" / "ffmpeg.exe").is_file():
            return candidate.resolve()
    raise RuntimeError("managed FFmpeg tools directory is unavailable")


def configure_environment(repository_root: Path) -> dict[str, str]:
    repository_root = repository_root.resolve()
    data_root = _service_data_root()
    user_root = data_root
    database = data_root / "cleanroom.db"
    jwt_secret = user_root / "secrets" / "jwt-signing.key"
    entrypoint = repository_root / "run_cleanroom.py"
    for path, label in (
        (database, "migrated database"),
        (jwt_secret, "JWT signing key"),
        (entrypoint, "source entrypoint"),
    ):
        if not path.is_file():
            raise RuntimeError(f"{label} is missing: {path}")
    tools_root = _resolve_tools_root(repository_root)
    values = {
        "CLEANROOM_PORT": "18110",
        "CLEANROOM_DB_PATH": str(database),
        "CLEANROOM_DATA_ROOT": str(data_root),
        "CLEANROOM_TOOLS_DIR": str(tools_root),
        "CLEANROOM_USER_CONFIG_ROOT": str(user_root),
        "CLEANROOM_JWT_SECRET_FILE": str(jwt_secret),
        "CLEANROOM_CREDENTIAL_STORE_FILE": str(
            data_root
            / "secrets"
            / "station-credentials.json"
        ),
        "CLEANROOM_CREDENTIAL_DPAPI_SCOPE": "machine",
        "CLEANROOM_OPEN_PANEL": "0",
        "CLEANROOM_SKIP_STARTUP_AI": "1",
        # Icecast source metadata is part of the public stream contract.
        # AI remains disabled independently via CLEANROOM_SKIP_STARTUP_AI.
        "CLEANROOM_SKIP_ICECAST_METADATA": "0",
        "RADIOTEDU_PROCESS_ISOLATED_WORKERS": "1",
    }
    os.environ.update(values)
    return values


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    apply_pending_staged_update(repository_root, _service_data_root())
    configure_environment(repository_root)
    os.chdir(repository_root)
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    # Proactor's AcceptEx path can stop accepting localhost connections after
    # WinError 64 while leaving the host process alive. The selector policy
    # uses the ordinary Winsock accept loop, which is more reliable for this
    # long-running, localhost-only control service.
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    from run_cleanroom import main as run_cleanroom

    run_cleanroom()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
