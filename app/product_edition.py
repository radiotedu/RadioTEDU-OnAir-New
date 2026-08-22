from __future__ import annotations

import os


RADIOTEDU_EDITION = "radiotedu"
RTAI_ONAIR_EDITION = "rtai-onair"
_RTAI_ALIASES = {"rtai", "rtai-onair", "rtai_onair"}
_RTAI_DISABLED_API_PREFIXES = (
    "/api/campaign",
    "/api/public/campaign",
    "/api/integrations/radiotedu",
    "/api/streaming/quality-outputs",
)


def get_product_edition() -> str:
    raw = str(os.getenv("CLEANROOM_PRODUCT_EDITION", "")).strip().lower()
    return RTAI_ONAIR_EDITION if raw in _RTAI_ALIASES else RADIOTEDU_EDITION


def is_rtai_onair() -> bool:
    return get_product_edition() == RTAI_ONAIR_EDITION


def get_product_name() -> str:
    return "rtAI OnAir" if is_rtai_onair() else "RadioTEDU OnAir"


def api_path_enabled(path: str) -> bool:
    if not is_rtai_onair():
        return True
    normalized = "/" + str(path or "").strip().lstrip("/")
    return not any(
        normalized == prefix or normalized.startswith(prefix + "/")
        for prefix in _RTAI_DISABLED_API_PREFIXES
    )


def public_product_profile() -> dict[str, object]:
    rtai = is_rtai_onair()
    return {
        "edition": get_product_edition(),
        "product_name": get_product_name(),
        "features": {
            "local_ai": True,
            "voting": not rtai,
            "radiotedu_integrations": not rtai,
            "radiotedu_campaign": not rtai,
            "radiotedu_quality_plan": not rtai,
            "product_media_catalog": not rtai,
        },
    }
