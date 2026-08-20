"""Fail closed when a staged RadioTEDU release contains foreign or secret-bearing files."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath


REQUIRED_EXECUTABLE_PATHS = {
    "backend/radiotedu-onair-backend.exe",
    "shell/radiotedu-onair.exe",
    "supervisor/radiotedu-onair-supervisor.exe",
}
REQUIRED_RUNTIME_EXECUTABLE_PATHS = {
    "backend/tools/bin/ffmpeg.exe",
    "backend/tools/bin/ffplay.exe",
    "backend/tools/bin/ffprobe.exe",
}
ALL_REQUIRED_EXECUTABLE_PATHS = REQUIRED_EXECUTABLE_PATHS | REQUIRED_RUNTIME_EXECUTABLE_PATHS
ALLOWED_EXECUTABLE_PATHS = ALL_REQUIRED_EXECUTABLE_PATHS | {
    "backend/tools/bin/yt-dlp.exe",
    # PyInstaller onedir retains the two encoder tools beside the embedded
    # runtime.  They are the same managed binaries as tools/bin and are not a
    # second product or an arbitrary executable.
    "backend/_internal/ffmpeg.exe",
    "backend/_internal/ffprobe.exe",
    # Self-contained .NET publishes include the official crash-dump helper.
    "shell/createdump.exe",
    "supervisor/createdump.exe",
}
SECRET_MARKERS = (
    "initial-admin-password",
    "jwt-signing",
    "credential-vault",
    "credentials.json",
    "cleanroom.db",
)


def collect_release_paths(root: Path, prefix: str) -> tuple[str, ...]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    return tuple(
        f"{prefix}/{path.relative_to(root).as_posix()}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def validate_release_paths(
    paths: tuple[str, ...], *, require_runtime_executables: bool = False
) -> None:
    violations: list[str] = []
    normalized = tuple(str(PurePosixPath(path.replace("\\", "/"))).casefold() for path in paths)
    for path in normalized:
        if path.startswith(("../", "/")) or path == "..":
            violations.append(f"path escapes release root: {path}")
            continue
        if any(marker in path for marker in SECRET_MARKERS) or path.endswith((".key", ".db", ".sqlite", ".sqlite3")):
            violations.append(f"secret or mutable data in release: {path}")
        name = PurePosixPath(path).name
        scoped_path = path[5:] if path.startswith("dist/") else path
        if scoped_path.startswith("desktop/"):
            scoped_path = scoped_path[8:]
        if name.endswith(".exe"):
            if scoped_path not in ALLOWED_EXECUTABLE_PATHS and not name.startswith("unins"):
                violations.append(f"unexpected executable in release: {path}")
        if name.startswith("rtai-onair") or name.startswith("cleanroomradio"):
            violations.append(f"foreign product executable in RadioTEDU release: {path}")
    required_paths = ALL_REQUIRED_EXECUTABLE_PATHS if require_runtime_executables else REQUIRED_EXECUTABLE_PATHS
    scoped_paths = []
    for path in normalized:
        scoped_path = path[5:] if path.startswith("dist/") else path
        if scoped_path.startswith("desktop/"):
            scoped_path = scoped_path[8:]
        scoped_paths.append(scoped_path)
    for required in required_paths:
        if required not in scoped_paths:
            violations.append(f"required executable missing: {required}")
    if violations:
        raise ValueError("; ".join(sorted(set(violations))))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True, type=Path)
    parser.add_argument("--shell", required=True, type=Path)
    parser.add_argument("--supervisor", required=True, type=Path)
    args = parser.parse_args()
    paths = (
        *collect_release_paths(args.backend.resolve(), "backend"),
        *collect_release_paths(args.shell.resolve(), "shell"),
        *collect_release_paths(args.supervisor.resolve(), "supervisor"),
    )
    validate_release_paths(paths, require_runtime_executables=True)
    print(f"Validated {len(paths)} focused RadioTEDU release files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
