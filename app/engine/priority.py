def choose_source(
    manual_count: int,
    ad_due: bool,
    schedule_ready: bool,
    fallback_ready: bool,
    host_count: int = 0,
) -> str:
    if host_count > 0:
        return "host"
    if manual_count > 0:
        return "manual"
    if ad_due:
        return "ads"
    if schedule_ready:
        return "schedule"
    if fallback_ready:
        return "fallback"
    return "none"
