from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def _version_candidates() -> tuple[Path, ...]:
    candidates = [Path(__file__).resolve().parents[1] / "VERSION"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent / "VERSION")
        bundle_root = getattr(sys, "_MEIPASS", "")
        if bundle_root:
            candidates.insert(0, Path(bundle_root).resolve() / "VERSION")
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


@lru_cache(maxsize=1)
def get_product_version() -> str:
    for candidate in _version_candidates():
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _VERSION_PATTERN.fullmatch(value):
            return value
        raise RuntimeError(f"invalid product version file: {candidate}")
    raise RuntimeError("RadioTEDU OnAir product VERSION file is missing")


PRODUCT_VERSION = get_product_version()
