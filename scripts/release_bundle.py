from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.validate_radiotedu_release import validate_release_paths as validate_radiotedu_release_paths

BACKEND_DIR = "dist/backend"
DESKTOP_DIR = "dist/desktop"
DESKTOP_SHELL_DIR = f"{DESKTOP_DIR}/shell"
DESKTOP_SUPERVISOR_DIR = f"{DESKTOP_DIR}/supervisor"

RELEASE_ARTIFACTS = (
    ("RadioTEDU-OnAir-Backend.exe", f"{BACKEND_DIR}/RadioTEDU-OnAir-Backend.exe"),
    ("RadioTEDU-OnAir.exe", f"{DESKTOP_SHELL_DIR}/RadioTEDU-OnAir.exe"),
    ("RadioTEDU-OnAir-Supervisor.exe", f"{DESKTOP_SUPERVISOR_DIR}/RadioTEDU-OnAir-Supervisor.exe"),
)

VERSION_FILE = REPOSITORY_ROOT / "VERSION"


def get_release_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def build_release_manifest(version: str | None = None) -> dict[str, Any]:
    source_version = get_release_version()
    if version is not None and str(version).strip() != source_version:
        raise ValueError(
            f"release version {version!r} does not match VERSION {source_version!r}"
        )
    artifacts = RELEASE_ARTIFACTS
    validate_radiotedu_release_paths(tuple(path for _, path in artifacts))
    return {
        "version": source_version,
        "layout": {
            "backend_dir": BACKEND_DIR,
            "desktop_dir": DESKTOP_DIR,
            "desktop_shell_dir": DESKTOP_SHELL_DIR,
            "desktop_supervisor_dir": DESKTOP_SUPERVISOR_DIR,
        },
        "artifacts": [
            {"name": name, "path": path}
            for name, path in artifacts
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    version = args[0] if args else None
    json.dump(build_release_manifest(version), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
