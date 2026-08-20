from app.auth.dependencies import (
    role_is_allowed_for_request,
    user_has_permission,
    user_has_show_permission,
    user_is_allowed_for_request,
    user_is_superadmin,
)


def test_user_has_permission_checks_effective_permissions():
    user = {"effective_permissions": {"logs.view", "queue.view"}}
    assert user_has_permission(user, "logs.view") is True
    assert user_has_permission(user, "stations.create") is False


def test_user_has_show_permission_checks_show_capabilities():
    user = {"show_permissions": {7: {"show.broadcast"}}}
    assert user_has_show_permission(user, 7, "show.broadcast") is True
    assert user_has_show_permission(user, 7, "show.end") is False


def test_soundboard_play_permission_allows_play_and_stop_routes():
    user = {"role": "viewer", "effective_permissions": {"soundboard.play"}}
    assert user_is_allowed_for_request(user, "/api/soundboard/play", "POST") is True
    assert user_is_allowed_for_request(user, "/api/soundboard/stop", "POST") is True
    assert user_is_allowed_for_request(user, "/api/soundboard/", "POST") is False


def test_soundboard_manage_permission_still_allows_play_and_stop_routes():
    user = {"role": "viewer", "effective_permissions": {"soundboard.manage"}}
    assert user_is_allowed_for_request(user, "/api/soundboard/play", "POST") is True
    assert user_is_allowed_for_request(user, "/api/soundboard/stop", "POST") is True


def test_legacy_superadmin_role_keeps_current_admin_access():
    assert role_is_allowed_for_request("superadmin", "/api/stations", "POST") is True
    assert role_is_allowed_for_request("SUPERADMIN", "/api/setup/state", "GET") is True
    assert user_is_superadmin({"role": "superadmin"}) is True
    assert user_is_allowed_for_request(
        {"role": "superadmin", "effective_permissions": set()},
        "/api/liquidsoap/program/music",
        "POST",
    ) is True
