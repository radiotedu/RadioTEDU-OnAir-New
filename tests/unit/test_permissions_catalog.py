import pytest

from app.auth.permissions import GLOBAL_PERMISSION_GROUPS, GLOBAL_PERMISSION_KEYS, SHOW_PERMISSION_KEYS


def test_global_permission_catalog_shape_is_fixed():
    assert GLOBAL_PERMISSION_GROUPS == {
        "stations": frozenset({"stations.view", "stations.create", "stations.edit", "stations.delete"}),
        "library": frozenset({"library.view", "library.edit"}),
        "playlists": frozenset({"playlists.view", "playlists.edit"}),
        "ads": frozenset({"ads.view", "ads.edit"}),
        "downloads": frozenset({"downloads.use"}),
        "schedule": frozenset({"schedule.view", "schedule.edit"}),
        "logs": frozenset({"logs.view"}),
        "queue": frozenset({"queue.view", "queue.edit"}),
        "soundboard": frozenset({"soundboard.view", "soundboard.play", "soundboard.manage"}),
        "program": frozenset({"program.panel.open"}),
        "shows": frozenset({"shows.view", "shows.manage", "show.assign.manage"}),
        "users": frozenset({"users.manage", "users.reset_password"}),
        "roles": frozenset({"roles.manage"}),
        "stream": frozenset({"stream.configure_basic", "stream.configure_advanced", "stream.failover"}),
    }


def test_global_permission_keys_are_flattened_and_unique():
    flattened_permissions = [
        permission
        for permissions in GLOBAL_PERMISSION_GROUPS.values()
        for permission in permissions
    ]

    assert GLOBAL_PERMISSION_KEYS == {
        "stations.view",
        "stations.create",
        "stations.edit",
        "stations.delete",
        "library.view",
        "library.edit",
        "playlists.view",
        "playlists.edit",
        "ads.view",
        "ads.edit",
        "downloads.use",
        "schedule.view",
        "schedule.edit",
        "logs.view",
        "queue.view",
        "queue.edit",
        "soundboard.view",
        "soundboard.play",
        "soundboard.manage",
        "program.panel.open",
        "shows.view",
        "shows.manage",
        "show.assign.manage",
        "users.manage",
        "users.reset_password",
        "roles.manage",
        "stream.configure_basic",
        "stream.configure_advanced",
        "stream.failover",
    }
    assert len(flattened_permissions) == len(set(flattened_permissions)) == len(GLOBAL_PERMISSION_KEYS)


def test_global_and_show_permission_keys_are_disjoint():
    assert GLOBAL_PERMISSION_KEYS.isdisjoint(SHOW_PERMISSION_KEYS)


def test_show_permission_keys_are_fixed():
    assert SHOW_PERMISSION_KEYS == frozenset({
        "show.broadcast",
        "show.queue_edit",
        "show.jingle_manage",
        "show.break_control",
        "show.end",
        "show.guest_manage",
        "show.guest_record",
    })


def test_global_permission_catalog_is_immutable():
    assert type(GLOBAL_PERMISSION_GROUPS).__name__ == "mappingproxy"
    assert all(type(keys).__name__ == "frozenset" for keys in GLOBAL_PERMISSION_GROUPS.values())
    assert type(GLOBAL_PERMISSION_KEYS).__name__ == "frozenset"
    assert type(SHOW_PERMISSION_KEYS).__name__ == "frozenset"
    with pytest.raises(TypeError):
        GLOBAL_PERMISSION_GROUPS["stations"] = frozenset()
    with pytest.raises(AttributeError):
        GLOBAL_PERMISSION_GROUPS["stations"].add("stations.audit")
    with pytest.raises(AttributeError):
        SHOW_PERMISSION_KEYS.add("show.audit")
