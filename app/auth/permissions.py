from types import MappingProxyType


_GLOBAL_PERMISSION_GROUPS = {
    "stations": {"stations.view", "stations.create", "stations.edit", "stations.delete"},
    "library": {"library.view", "library.edit"},
    "playlists": {"playlists.view", "playlists.edit"},
    "ads": {"ads.view", "ads.edit"},
    "downloads": {"downloads.use"},
    "schedule": {"schedule.view", "schedule.edit"},
    "logs": {"logs.view"},
    "queue": {"queue.view", "queue.edit"},
    "soundboard": {"soundboard.view", "soundboard.play", "soundboard.manage"},
    "program": {"program.panel.open"},
    "shows": {"shows.view", "shows.manage", "show.assign.manage"},
    "stream": {"stream.configure_basic", "stream.configure_advanced", "stream.failover"},
    "users": {"users.manage", "users.reset_password"},
    "roles": {"roles.manage"},
}

GLOBAL_PERMISSION_GROUPS = MappingProxyType(
    {group: frozenset(keys) for group, keys in _GLOBAL_PERMISSION_GROUPS.items()}
)

GLOBAL_PERMISSION_KEYS = frozenset(
    permission
    for permissions in GLOBAL_PERMISSION_GROUPS.values()
    for permission in permissions
)

SHOW_PERMISSION_KEYS = frozenset(
    {
        "show.broadcast",
        "show.queue_edit",
        "show.jingle_manage",
        "show.break_control",
        "show.end",
        "show.guest_manage",
        "show.guest_record",
    }
)
