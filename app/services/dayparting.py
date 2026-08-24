from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Europe/Istanbul"
DAYPART_SETTING_KEY = "dayparting_enabled"
DAYPART_TIMEZONE_KEY = "dayparting_timezone"
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class DaypartRule:
    position: int
    name: str
    start_minute: int
    end_minute: int
    min_bpm: float
    max_bpm: float
    enabled: bool = True
    id: int | None = None
    day_of_week: int = 0


def _rule(position: int, name: str, start: str, end: str, bpm: tuple[int, int]) -> DaypartRule:
    return DaypartRule(
        position=position,
        name=name,
        start_minute=_parse_clock(start),
        end_minute=_parse_clock(end),
        min_bpm=float(bpm[0]),
        max_bpm=float(bpm[1]),
    )


def _parse_clock(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":", 1))
    return hour * 60 + minute


# The time blocks are deliberately identical across stations. Program names and
# tempo envelopes remain genre-specific, so an operator can understand the
# clock at a glance without flattening every station into the same sound.
DEFAULT_SCHEDULES: dict[str, tuple[DaypartRule, ...]] = {
    "energize": (
        _rule(0, "Rise & Ignite", "05:00", "09:00", (118, 138)),
        _rule(1, "Power Current", "09:00", "12:00", (108, 128)),
        _rule(2, "Midday Rush", "12:00", "15:00", (112, 132)),
        _rule(3, "Drive Reactor", "15:00", "18:00", (118, 142)),
        _rule(4, "Peak Circuit", "18:00", "22:00", (124, 150)),
        _rule(5, "Neon Overdrive", "22:00", "02:00", (122, 148)),
        _rule(6, "Afterglow Motion", "02:00", "05:00", (95, 120)),
    ),
    "rock": (
        _rule(0, "Morning Amplifier", "05:00", "09:00", (115, 155)),
        _rule(1, "Workday Riffs", "09:00", "12:00", (95, 135)),
        _rule(2, "Lunch Break Live", "12:00", "15:00", (100, 145)),
        _rule(3, "Drive-Time Distortion", "15:00", "18:00", (115, 160)),
        _rule(4, "Prime Time Voltage", "18:00", "22:00", (120, 175)),
        _rule(5, "Midnight Headliner", "22:00", "02:00", (118, 170)),
        _rule(6, "Aftershow", "02:00", "05:00", (75, 115)),
    ),
    "lofi": (
        _rule(0, "Sunrise Beats", "05:00", "09:00", (82, 105)),
        _rule(1, "Study Flow", "09:00", "12:00", (68, 92)),
        _rule(2, "Cafe Circuit", "12:00", "15:00", (74, 98)),
        _rule(3, "Focus Drive", "15:00", "18:00", (80, 106)),
        _rule(4, "Golden Hour Grooves", "18:00", "22:00", (86, 112)),
        _rule(5, "Sleep Tapes", "22:00", "02:00", (55, 78)),
        _rule(6, "Deep Sleep Loops", "02:00", "05:00", (45, 70)),
    ),
    "jazz": (
        _rule(0, "First Light Swing", "05:00", "09:00", (105, 155)),
        _rule(1, "Blue Note Workday", "09:00", "12:00", (78, 118)),
        _rule(2, "Lunch Set", "12:00", "15:00", (90, 132)),
        _rule(3, "City Swing", "15:00", "18:00", (105, 155)),
        _rule(4, "Main Stage Jazz", "18:00", "22:00", (112, 175)),
        _rule(5, "Midnight Ballads", "22:00", "02:00", (55, 85)),
        _rule(6, "Dreamland Jazz", "02:00", "05:00", (45, 72)),
    ),
    "classic": (
        _rule(0, "Allegro Sunrise", "05:00", "09:00", (100, 150)),
        _rule(1, "Focus in Motion", "09:00", "12:00", (72, 112)),
        _rule(2, "Matinee Classics", "12:00", "15:00", (78, 120)),
        _rule(3, "Symphonic Drive", "15:00", "18:00", (95, 145)),
        _rule(4, "Grand Concert", "18:00", "22:00", (100, 150)),
        _rule(5, "Midnight Virtuoso", "22:00", "02:00", (102, 155)),
        _rule(6, "Nocturne", "02:00", "05:00", (40, 82)),
    ),
    "general": (
        _rule(0, "Campus Wake-Up", "05:00", "09:00", (110, 140)),
        _rule(1, "Day Shift", "09:00", "12:00", (82, 118)),
        _rule(2, "Lunch Mix", "12:00", "15:00", (90, 125)),
        _rule(3, "Campus Drive", "15:00", "18:00", (105, 140)),
        _rule(4, "Prime Mix", "18:00", "22:00", (115, 150)),
        _rule(5, "Night Shift", "22:00", "02:00", (112, 150)),
        _rule(6, "Late Night Blend", "02:00", "05:00", (65, 100)),
    ),
}


WEEKLY_NAMES: dict[str, tuple[tuple[str, ...], ...]] = {
    "energize": (
        tuple(rule.name for rule in DEFAULT_SCHEDULES["energize"]),
        ("Tuesday Takeoff", "Powerline Tuesday", "Noon Accelerator", "Turbo Drive", "Voltage Hour", "Neon Velocity", "Pulse Reset"),
        ("Midweek Ignition", "Momentum Works", "Halfway Rush", "Velocity Drive", "Peak Charge", "Electric Midnight", "Recharge Motion"),
        ("Thunder Rise", "Current Affairs", "Noon Voltage", "Fast Lane Thursday", "Powerhouse Prime", "Night Reactor", "Low-Glow Motion"),
        ("Friday Fire-Up", "Weekend Loading", "Lunch Launch", "Escape Velocity", "Friday Peak", "Nightlife Overdrive", "Afterparty Motion"),
        ("Weekend Spark", "Daylight Drive", "Saturday Rush", "Open-Road Reactor", "Saturday Supercharge", "Neon Weekend", "Dawn Recovery"),
        ("Sunday Charge", "Bright Current", "Noon Lift", "Sunset Drive", "Sunday Peak", "Starlight Energy", "Quiet Charge"),
    ),
    "rock": (
        tuple(rule.name for rule in DEFAULT_SCHEDULES["rock"]),
        ("Tuesday Soundcheck", "Riff Workshop", "Lunchbox Rock", "Highway Volume", "Amped Evening", "After-Hours Arena", "Roadie Rest"),
        ("Midweek Feedback", "Working-Class Riffs", "Power Chords at Noon", "Distortion Drive", "Wednesday Headliner", "Moonlight Metal", "Unplugged Hours"),
        ("Morning Turn-Up", "Riffs on the Clock", "Lunch Stage", "Heavy Traffic", "Prime-Time Guitars", "Night Shift Rock", "Acoustic Comedown"),
        ("Friday Soundcheck", "Weekend Riffs", "Lunch Encore", "Freeway Rock", "Friday Main Stage", "Midnight Mosh", "Backstage Slowdown"),
        ("Saturday Kickstart", "Garage Session", "Open-Air Rock", "Road Trip Volume", "Weekend Headliner", "Late-Night Legends", "Amp Cooldown"),
        ("Sunday Amplifier", "Classic Riff Brunch", "Noon Rewind", "Sunset Guitars", "Sunday Stadium", "Night Drive Rock", "Last Chord"),
    ),
    "lofi": (
        tuple(rule.name for rule in DEFAULT_SCHEDULES["lofi"]),
        ("Soft Start Tuesday", "Desk Beats", "Noon Notes", "Afternoon Flow", "Sunset Loops", "Pillow Beats", "Quiet Hours"),
        ("Midweek Mornings", "Deep Focus Desk", "Halfway Loops", "Study Lane", "Evening Ease", "Sleepy Signals", "Dream Loops"),
        ("Gentle Thursday", "Focus Tapes", "Cafe Notes", "Productive Drift", "Dusk Beats", "Pillow Radio", "Deep Rest Tapes"),
        ("Friday Soft Start", "Last Study Session", "Lunchroom Loops", "Weekend Wind-Down", "Sunset Chill", "Sleep Mode", "Dream Cache"),
        ("Slow Saturday Rise", "Weekend Loops", "Easy Noon", "Window Seat Beats", "Cozy Evening", "Saturday Sleep Tapes", "Deep Weekend Rest"),
        ("Sunday Soft Light", "Reading Room", "Brunch Beats", "Calm Afternoon", "Golden Quiet", "Sunday Sleep Session", "Dream Reset"),
    ),
    "jazz": (
        tuple(rule.name for rule in DEFAULT_SCHEDULES["jazz"]),
        ("Tuesday in Swing", "Office Blue Notes", "Noon Quartet", "Rush Hour Swing", "Evening at the Club", "Velvet Midnight", "Dreamtime Standards"),
        ("Midweek Bebop", "Workday Cool", "Halfway Set", "Uptown Swing", "Wednesday Main Room", "Moonlight Ballads", "Blue Dream Hour"),
        ("Brass at Breakfast", "Cool Desk Sessions", "Thursday Matinee", "City Line Swing", "Nightclub Thursday", "Satin Ballads", "Dreamland Standards"),
        ("Friday First Set", "Workweek Jazz", "Lunchroom Trio", "Downtown Drive", "Friday Headliner", "Late Set Ballads", "After-Hours Dreams"),
        ("Weekend Swing-Up", "Saturday Cool", "Brunch Quartet", "Boulevard Jazz", "Saturday Main Stage", "Slow Dance Set", "Sleepy Standards"),
        ("Sunday Morning Swing", "Easy Blue Notes", "Brunch at the Club", "Sunset Quartet", "Sunday Concert", "Candlelight Ballads", "Blue Night Dreams"),
    ),
    "classic": (
        tuple(rule.name for rule in DEFAULT_SCHEDULES["classic"]),
        ("Vivace Tuesday", "Counterpoint at Work", "Noon Chamber", "Orchestral Drive", "Tuesday Gala", "Moonlight Allegro", "Quiet Adagio"),
        ("Midweek Overture", "Study in Counterpoint", "Chamber at Noon", "Crescendo Drive", "Wednesday Philharmonic", "Midnight Sonata", "Restful Largo"),
        ("Bright Baroque Morning", "Focus Concerto", "Thursday Matinee", "Symphonic Momentum", "Grand Hall Thursday", "Virtuoso After Dark", "Sleeping Adagio"),
        ("Friday Fanfare", "Workweek Finale", "Lunch Recital", "Weekend Crescendo", "Friday Grand Concert", "Midnight Rhapsody", "Quiet Coda"),
        ("Weekend Allegro", "Baroque Brunch", "Saturday Matinee", "Open-Air Symphony", "Saturday Gala", "Starlight Concerto", "Deep Nocturne"),
        ("Sunday Overture", "Sacred Morning Classics", "Brunch Chamber", "Sunset Symphony", "Sunday Grand Hall", "Moonlit Masterworks", "Gentle Nocturne"),
    ),
    "general": (
        tuple(rule.name for rule in DEFAULT_SCHEDULES["general"]),
        ("Tuesday Wake-Up Call", "Campus Current", "Lunch Break Mix", "Tuesday Transit", "Evening Mixdown", "After-Class Energy", "Night Owl Radio"),
        ("Midweek Morning Mix", "Study Hall Radio", "Halfway Lunch", "Campus Commute", "Wednesday Prime", "Midnight on Campus", "Quiet Quad"),
        ("Thursday Lift-Off", "Campus Daylight", "Noon Rotation", "Homebound Mix", "Thursday Spotlight", "Night Campus Live", "Library Lights"),
        ("Friday Wake-Up", "Weekend Countdown", "Lunch on the Lawn", "Friday Escape", "Campus Prime Live", "Friday Night Shift", "Afterparty Blend"),
        ("Saturday Campus Rise", "Weekend Radio", "Brunch Mix", "City Campus Drive", "Saturday Spotlight", "Campus After Dark", "Dawn Blend"),
        ("Sunday Wake-Up", "Easy Campus Morning", "Sunday Lunch Mix", "Sunset on Campus", "Sunday Prime", "Night Before Monday", "Campus Wind-Down"),
    ),
}


def station_profile(station_name: str) -> str | None:
    token = " ".join(str(station_name or "").strip().lower().replace("-", " ").split())
    if "energ" in token:
        return "energize"
    if "rock" in token:
        return "rock"
    if "lo fi" in token or "lofi" in token:
        return "lofi"
    if "jazz" in token:
        return "jazz"
    if "classic" in token:
        return "classic"
    if token in {"radiotedu", "radio tedu"}:
        return "general"
    return None


def default_rules_for_station(station_name: str) -> list[DaypartRule]:
    profile = station_profile(station_name)
    if not profile:
        return []
    base_rules = DEFAULT_SCHEDULES[profile]
    weekly_names = WEEKLY_NAMES[profile]
    return [
        replace(
            base_rule,
            name=weekly_names[day_of_week][position],
            day_of_week=day_of_week,
            id=None,
        )
        for day_of_week in range(7)
        for position, base_rule in enumerate(base_rules)
    ]


def ensure_default_dayparts_persisted(conn) -> dict[str, int]:
    """Persist missing RadioTEDU clocks without overwriting operator edits.

    Runtime defaults keep legacy databases on air, but storing the rules makes
    the weekly clock explicit, inspectable, and durable across deployments.
    Existing rules and explicit enable/disable choices always win.
    """

    inserted_rules = 0
    initialized_stations = 0
    station_rows = conn.execute("SELECT id, name FROM stations ORDER BY id").fetchall()
    for station in station_rows:
        station_id = int(station["id"])
        defaults = default_rules_for_station(str(station["name"] or ""))
        if not defaults:
            continue
        existing = {
            (int(row["day_of_week"]), int(row["position"]))
            for row in conn.execute(
                "SELECT day_of_week, position FROM daypart_rules WHERE station_id=?",
                (station_id,),
            ).fetchall()
        }
        station_inserted = 0
        for rule in defaults:
            key = (int(rule.day_of_week), int(rule.position))
            if key in existing:
                continue
            conn.execute(
                "INSERT INTO daypart_rules "
                "(station_id, day_of_week, position, name, start_minute, end_minute, "
                "min_bpm, max_bpm, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    station_id,
                    int(rule.day_of_week),
                    int(rule.position),
                    str(rule.name),
                    int(rule.start_minute),
                    int(rule.end_minute),
                    float(rule.min_bpm),
                    float(rule.max_bpm),
                ),
            )
            existing.add(key)
            station_inserted += 1
        conn.execute(
            "INSERT INTO station_settings(station_id, key, value, updated_at) "
            "VALUES (?, ?, 'true', CURRENT_TIMESTAMP) ON CONFLICT(station_id, key) DO NOTHING",
            (station_id, DAYPART_SETTING_KEY),
        )
        conn.execute(
            "INSERT INTO station_settings(station_id, key, value, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(station_id, key) DO NOTHING",
            (station_id, DAYPART_TIMEZONE_KEY, DEFAULT_TIMEZONE),
        )
        if station_inserted:
            initialized_stations += 1
            inserted_rules += station_inserted
    conn.commit()
    return {
        "initialized_stations": initialized_stations,
        "inserted_rules": inserted_rules,
    }


def _truthy(value: object, default: bool = False) -> bool:
    token = str(value if value is not None else "").strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default


def load_station_dayparts(conn, station_id: int) -> tuple[str, bool, str, list[DaypartRule]]:
    station = conn.execute("SELECT name FROM stations WHERE id=?", (int(station_id),)).fetchone()
    if station is None:
        return "", False, DEFAULT_TIMEZONE, []
    station_name = str(station["name"] or "")
    profile = station_profile(station_name)
    setting_rows = conn.execute(
        "SELECT key, value FROM station_settings WHERE station_id=? "
        "AND key IN (?, ?)",
        (int(station_id), DAYPART_SETTING_KEY, DAYPART_TIMEZONE_KEY),
    ).fetchall()
    settings = {str(row["key"]): str(row["value"] or "") for row in setting_rows}
    enabled = _truthy(settings.get(DAYPART_SETTING_KEY), default=profile is not None)
    timezone_name = str(settings.get(DAYPART_TIMEZONE_KEY) or DEFAULT_TIMEZONE).strip()
    rows = conn.execute(
        "SELECT id, day_of_week, position, name, start_minute, end_minute, "
        "min_bpm, max_bpm, enabled FROM daypart_rules WHERE station_id=? "
        "ORDER BY day_of_week ASC, position ASC, id ASC",
        (int(station_id),),
    ).fetchall()
    rules = [
        DaypartRule(
            id=int(row["id"]),
            day_of_week=int(row["day_of_week"]),
            position=int(row["position"]),
            name=str(row["name"]),
            start_minute=int(row["start_minute"]),
            end_minute=int(row["end_minute"]),
            min_bpm=float(row["min_bpm"]),
            max_bpm=float(row["max_bpm"]),
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]
    defaults = default_rules_for_station(station_name)
    if not rules:
        rules = defaults
    elif defaults:
        stored_days = {rule.day_of_week for rule in rules}
        rules.extend(rule for rule in defaults if rule.day_of_week not in stored_days)
        rules.sort(key=lambda rule: (rule.day_of_week, rule.position))
    return station_name, enabled, timezone_name, rules


def minute_in_rule(minute: int, rule: DaypartRule) -> bool:
    if not rule.enabled:
        return False
    if rule.start_minute < rule.end_minute:
        return rule.start_minute <= minute < rule.end_minute
    return minute >= rule.start_minute or minute < rule.end_minute


def active_daypart(conn, station_id: int, at: datetime | None = None) -> DaypartRule | None:
    _name, enabled, timezone_name, rules = load_station_dayparts(conn, station_id)
    if not enabled:
        return None
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo(DEFAULT_TIMEZONE)
    local_now = (at or datetime.now(timezone)).astimezone(timezone)
    minute = local_now.hour * 60 + local_now.minute
    weekday = local_now.weekday()
    for rule in rules:
        if not minute_in_rule(minute, rule):
            continue
        if rule.start_minute < rule.end_minute and rule.day_of_week == weekday:
            return rule
        if rule.start_minute > rule.end_minute:
            if minute >= rule.start_minute and rule.day_of_week == weekday:
                return rule
            if minute < rule.end_minute and rule.day_of_week == (weekday - 1) % 7:
                return rule
    return None


def format_minute(minute: int) -> str:
    minute = int(minute) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def rule_payload(rule: DaypartRule) -> dict:
    payload = asdict(rule)
    payload["start"] = format_minute(rule.start_minute)
    payload["end"] = format_minute(rule.end_minute)
    payload["day"] = DAY_NAMES[rule.day_of_week]
    return payload


def bpm_coverage(conn, station_id: int) -> dict[str, int | float]:
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN COALESCE(bpm, 0)>0 THEN 1 ELSE 0 END) AS known "
        "FROM tracks WHERE station_id=? AND is_active=1 "
        "AND LOWER(COALESCE(track_type, 'music'))='music' "
        "AND COALESCE(exclude_from_autoplay, 0)=0",
        (int(station_id),),
    ).fetchone()
    total = int(row["total"] or 0)
    known = int(row["known"] or 0)
    return {
        "total_music_tracks": total,
        "bpm_known_tracks": known,
        "bpm_unknown_tracks": max(0, total - known),
        "coverage_percent": round((known / total * 100.0) if total else 0.0, 1),
    }
