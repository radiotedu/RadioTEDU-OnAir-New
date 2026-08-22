from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _source_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
    )
    paths = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        source = (ROOT / relative).resolve()
        if source.is_file() and ROOT in source.parents:
            paths.append(relative)
    return sorted(set(paths), key=lambda value: value.as_posix().lower())


def _replace_required(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    if old not in content:
        raise RuntimeError(f"required edition marker missing: {path}: {old}")
    path.write_text(content.replace(old, new), encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_launcher(destination: Path) -> None:
    launcher = r"""@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "CLEANROOM_PRODUCT_EDITION=rtai-onair"
set "CLEANROOM_DISABLE_PRODUCT_CATALOG=1"
set "CLEANROOM_SKIP_STARTUP_AI=0"
set "CLEANROOM_OPEN_PANEL=1"
set "CLEANROOM_HOST=127.0.0.1"
set "CLEANROOM_PORT=8100"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run_cleanroom.py
  exit /b %errorlevel%
)
echo rtAI OnAir runtime is not installed in this source release.
echo Build the offline desktop bundle first; see docs\RTAI_ONAIR_EDITION.md.
pause
exit /b 2
"""
    destination.write_text(launcher, encoding="utf-8", newline="\r\n")


def _write_zip(source: Path, archive: Path, epoch: int) -> None:
    timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
    safe_year = max(1980, timestamp.year)
    zip_time = (safe_year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(source.rglob("*"), key=lambda value: value.relative_to(source).as_posix().lower()):
            if not path.is_file():
                continue
            relative = Path(source.name) / path.relative_to(source)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=zip_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build(output_root: Path, *, force: bool = False) -> dict[str, object]:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    target = (output_root.resolve() / f"rtAI-onair-{version}").resolve()
    if target.parent != output_root.resolve() or not target.name.startswith("rtAI-onair-"):
        raise RuntimeError("refusing unsafe rtAI output path")
    if target.exists():
        if not force:
            raise FileExistsError(f"edition output already exists: {target}")
        shutil.rmtree(target)
    target.mkdir(parents=True)

    for relative in _source_files():
        source = ROOT / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    _replace_required(
        target / "app" / "static" / "onair" / "index.html",
        '<meta name="onair-edition" content="radiotedu">',
        '<meta name="onair-edition" content="rtai-onair">',
    )
    manifest_path = target / "app" / "static" / "manifest.json"
    web_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    web_manifest.update(
        {
            "name": "rtAI OnAir",
            "short_name": "rtAI",
            "description": "Deterministic local-first AI broadcast automation",
        }
    )
    manifest_path.write_text(
        json.dumps(web_manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_launcher(target / "START-rtAI-onair.bat")
    (target / "rtai-onair.env").write_text(
        "CLEANROOM_PRODUCT_EDITION=rtai-onair\n"
        "CLEANROOM_DISABLE_PRODUCT_CATALOG=1\n"
        "CLEANROOM_SKIP_STARTUP_AI=0\n",
        encoding="utf-8",
        newline="\n",
    )

    commit = _git("rev-parse", "HEAD")
    epoch = int(_git("show", "-s", "--format=%ct", "HEAD"))
    files = []
    for path in sorted(target.rglob("*"), key=lambda value: value.relative_to(target).as_posix().lower()):
        if path.is_file() and path.name != "rtai-onair-manifest.json":
            files.append(
                {
                    "path": path.relative_to(target).as_posix(),
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )
    edition_manifest = {
        "product": "rtAI OnAir",
        "version": version,
        "source_commit": commit,
        "source_epoch": epoch,
        "features": {
            "local_ai": True,
            "native_folder_pickers": True,
            "voting": False,
            "radiotedu_integrations": False,
            "radiotedu_campaign": False,
            "radiotedu_quality_plan": False,
            "product_media_catalog": False,
        },
        "files": files,
    }
    (target / "rtai-onair-manifest.json").write_text(
        json.dumps(edition_manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    archive = output_root.resolve() / f"rtAI-onair-{version}.zip"
    if archive.exists():
        if not force:
            raise FileExistsError(f"edition archive already exists: {archive}")
        archive.unlink()
    _write_zip(target, archive, epoch)
    return {
        "directory": str(target),
        "archive": str(archive),
        "archive_sha256": _sha256(archive),
        "files": len(files) + 1,
        "version": version,
        "source_commit": commit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the white-label rtAI OnAir source edition")
    parser.add_argument("--output-root", type=Path, default=ROOT / "dist" / "editions")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    args.output_root.mkdir(parents=True, exist_ok=True)
    print(json.dumps(build(args.output_root, force=args.force), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
