#!/usr/bin/env python3
"""Install selected protected service .env files from the operator's rt.md archive.

The command never prints values. It recognizes exact archived source paths,
writes atomically, and reports only destination paths plus variable counts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


RAW_FILE_PREFIX = "RAW FILE:"
TARGETS = {
    r"\desktop\juke-local\media-agent\.env": (
        "radiotedu-jukebox",
        "media-agent",
        ".env",
    ),
    r"\desktop\voting\rtjukebox\tools\local-voting-agent\.env": (
        "radiotedu-voting",
        "tools",
        "local-voting-agent",
        ".env",
    ),
}
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FRAME_DELIMITER = re.compile(r"^={20,}$")


def _normalize_archive_path(raw: str) -> str:
    return str(raw or "").strip().replace("/", "\\").lower()


def parse_raw_files(text: str) -> dict[str, str]:
    lines = str(text or "").splitlines()
    blocks: dict[str, str] = {}
    current_path = ""
    content: list[str] = []

    def flush() -> None:
        nonlocal current_path, content
        if current_path:
            while content and (not content[0].strip() or FRAME_DELIMITER.fullmatch(content[0])):
                content.pop(0)
            while content and (not content[-1].strip() or FRAME_DELIMITER.fullmatch(content[-1])):
                content.pop()
            blocks[_normalize_archive_path(current_path)] = "\n".join(content).rstrip() + "\n"
        current_path = ""
        content = []

    for line in lines:
        if line.startswith(RAW_FILE_PREFIX):
            flush()
            current_path = line[len(RAW_FILE_PREFIX) :].strip()
        elif current_path:
            content.append(line)
    flush()
    return blocks


def _apply_overrides(content: str, overrides: dict[str, str]) -> str:
    remaining = dict(overrides)
    output: list[str] = []
    for line in content.splitlines():
        match = ENV_KEY.match(line)
        key = line.split("=", 1)[0] if match else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(output).rstrip() + "\n"


def install(
    rt_md: Path,
    services_root: Path,
    *,
    voting_overrides: dict[str, str] | None = None,
    juke_overrides: dict[str, str] | None = None,
) -> dict:
    blocks = parse_raw_files(rt_md.read_text(encoding="utf-8"))
    installed: list[dict[str, object]] = []
    for suffix, relative_destination in TARGETS.items():
        matches = [
            content
            for archived_path, content in blocks.items()
            if archived_path.endswith(suffix)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one protected archive block ending in {suffix}; "
                f"found {len(matches)}"
            )
        content = matches[0]
        if suffix.endswith(r"local-voting-agent\.env"):
            content = _apply_overrides(content, dict(voting_overrides or {}))
        elif suffix.endswith(r"media-agent\.env"):
            content = _apply_overrides(content, dict(juke_overrides or {}))
        key_count = sum(1 for line in content.splitlines() if ENV_KEY.match(line))
        if key_count < 5:
            raise RuntimeError(f"protected archive block {suffix} is incomplete")
        destination = services_root.joinpath(*relative_destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, destination)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        installed.append(
            {
                "destination": str(destination.resolve()),
                "variable_count": key_count,
            }
        )
    return {"ok": True, "installed": installed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rt-md", required=True)
    parser.add_argument("--services-root", required=True)
    parser.add_argument("--music-root", default="")
    parser.add_argument("--juke-root", default="")
    parser.add_argument("--jingles-root", default="")
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--ffprobe", default="")
    args = parser.parse_args()
    voting_overrides = {
        key: value
        for key, value in {
            "MUSIC_LIBRARY_DIR": str(args.music_root).strip(),
            "JINGLE_LIBRARY_DIR": str(args.jingles_root).strip(),
            "FFMPEG_PATH": str(args.ffmpeg).strip(),
            "FFPROBE_PATH": str(args.ffprobe).strip(),
            "VOTING_METADATA_PROBE_ENABLED": "false",
            # OnAir owns /lofi during commissioning. Optional agents stay
            # connected for health/control but must not claim another mount.
            "ICECAST_STREAM_ENABLED": "false",
            "LOCAL_HTTP_STREAM_ENABLED": "false",
        }.items()
        if value
    }
    juke_overrides = {
        key: value
        for key, value in {
            "LOCAL_MUSIC_ROOT": str(args.juke_root or args.music_root).strip(),
            "AI_MIRROR_FFMPEG_PATH": str(args.ffmpeg).strip(),
            "AI_MIRROR_ENABLED": "false",
            "AI_AUTOPLAY_ENABLED": "false",
        }.items()
        if value
    }
    report = install(
        Path(args.rt_md),
        Path(args.services_root),
        voting_overrides=voting_overrides,
        juke_overrides=juke_overrides,
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
