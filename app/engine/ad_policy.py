from collections.abc import Mapping

from app.repositories.settings_repo import SettingsRepository

_TRUTHY = {"1", "true", "yes", "on"}


def is_truthy(raw) -> bool:
    return str(raw or "").strip().lower() in _TRUTHY


def ads_enabled_from_settings(settings: Mapping | None) -> bool:
    values = dict(settings or {})
    return is_truthy(values.get("hourly_ad_v2_enabled")) or is_truthy(
        values.get("hourly_ad_enabled")
    )


def rocket_ad_insertion_enabled_from_settings(
    settings: Mapping | None,
) -> bool:
    return is_truthy(
        (settings or {}).get("rocket_ad_insertion_enabled", "false")
    )


def station_ads_enabled(conn, station_id: int) -> bool:
    settings = SettingsRepository(conn).get_station(int(station_id))
    return ads_enabled_from_settings(settings)
