import sqlite3

import pytest

from app.db import get_connection, init_db
from app.repositories.rbac_repo import RbacRepository
from app.repositories.show_repo import ShowRepository


def _create_user(conn, username: str = "dj-1") -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, display_name, password_hash, role, is_active) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, "DJ 1", "hash", "dj", 1),
    )
    return int(cur.lastrowid)


def _create_station_and_show(conn, user_id: int) -> tuple[int, int]:
    cur = conn.cursor()
    cur.execute("INSERT INTO stations (name) VALUES (?)", ("Station 1",))
    station_id = int(cur.lastrowid)
    cur.execute(
        "INSERT INTO shows (station_id, name, is_active) VALUES (?, ?, ?)",
        (station_id, "Morning Show", 1),
    )
    show_id = int(cur.lastrowid)
    ShowRepository(conn).assign(show_id, user_id)
    return station_id, show_id


def test_role_template_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        role_id = repo.create_role_template("Scheduler", "Can manage schedule", False)
        repo.replace_role_permissions(role_id, {"schedule.view", "schedule.edit"})
        row = repo.get_role_template(role_id)
        perms = repo.list_role_permissions(role_id)
    finally:
        conn.close()

    assert row["name"] == "Scheduler"
    assert perms == {"schedule.view", "schedule.edit"}


def test_replace_user_roles_rolls_back_on_invalid_role_template_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        user_id = _create_user(conn, "dj-rollback")
        role_a = repo.create_role_template("Queue", "", False)
        role_b = repo.create_role_template("Logs", "", False)
        repo.replace_user_roles(user_id, {role_a})

        with pytest.raises(sqlite3.IntegrityError):
            repo.replace_user_roles(user_id, {role_b, 999999})

        roles = repo.list_user_role_ids(user_id)
    finally:
        conn.close()

    assert roles == {role_a}


def test_update_role_template_and_list_role_templates_include_inactive(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        role_id = repo.create_role_template("Scheduler", "Original", False)
        updated = repo.update_role_template(
            role_id,
            description="Updated",
            is_active=False,
        )
        active_templates = repo.list_role_templates()
        all_templates = repo.list_role_templates(include_inactive=True)
        row = repo.get_role_template(role_id)
    finally:
        conn.close()

    assert updated is True
    assert {template["name"] for template in active_templates} == {
        "Legacy Admin",
        "Legacy DJ",
        "Legacy Producer",
        "Legacy Viewer",
    }
    assert len(all_templates) == 5
    assert {template["name"] for template in all_templates} == {
        "Legacy Admin",
        "Legacy DJ",
        "Legacy Producer",
        "Legacy Viewer",
        "Scheduler",
    }
    scheduler = next(template for template in all_templates if template["name"] == "Scheduler")
    assert scheduler["id"] == role_id
    assert scheduler["description"] == "Updated"
    assert scheduler["is_active"] == 0
    assert row["description"] == "Updated"
    assert row["is_active"] == 0


def test_list_user_role_ids_and_empty_replace(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        user_id = _create_user(conn, "dj-roles")
        role_a = repo.create_role_template("Queue", "", False)
        role_b = repo.create_role_template("Logs", "", False)

        repo.replace_user_roles(user_id, {role_a, role_b})
        assigned_roles = repo.list_user_role_ids(user_id)
        repo.replace_user_roles(user_id, set())
        cleared_roles = repo.list_user_role_ids(user_id)
    finally:
        conn.close()

    assert assigned_roles == {role_a, role_b}
    assert cleared_roles == set()


def test_replace_user_roles_within_open_transaction(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        user_id = _create_user(conn, "dj-nested")
        role_a = repo.create_role_template("Queue", "", False)
        role_b = repo.create_role_template("Logs", "", False)
        conn.commit()

        conn.execute("BEGIN")
        repo.replace_user_roles(user_id, {role_a, role_b})
        conn.commit()

        roles = repo.list_user_role_ids(user_id)
    finally:
        conn.close()

    assert roles == {role_a, role_b}


def test_role_create_and_update_roll_back_with_outer_transaction(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)

        conn.execute("BEGIN")
        role_id = repo.create_role_template("Scheduler", "Original", False)
        repo.update_role_template(role_id, description="Updated", is_active=False)
        conn.rollback()

        row = repo.get_role_template(role_id)
    finally:
        conn.close()

    assert row is None


def test_list_effective_global_permissions_ignores_inactive_roles(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        user_id = _create_user(conn, "dj-effective")
        active_role = repo.create_role_template("Queue", "", False)
        inactive_role = repo.create_role_template("Logs", "", False)
        repo.replace_role_permissions(active_role, {"queue.view"})
        repo.replace_role_permissions(inactive_role, {"logs.view"})
        repo.update_role_template(inactive_role, is_active=False)
        repo.replace_user_roles(user_id, {active_role, inactive_role})
        effective = repo.list_effective_global_permissions(user_id)
    finally:
        conn.close()

    assert effective == {"queue.view"}


def test_replace_show_permissions_uses_show_assignment_and_cascades_on_unassign(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "rbac.sqlite3"))
    init_db()
    conn = get_connection()
    try:
        repo = RbacRepository(conn)
        user_id = _create_user(conn, "dj-show")
        _, show_id = _create_station_and_show(conn, user_id)

        repo.replace_show_permissions(show_id, user_id, {"show.broadcast", "show.end"})
        before_unassign = repo.list_show_permissions(show_id, user_id)

        ShowRepository(conn).unassign(show_id, user_id)
        after_unassign = repo.list_show_permissions(show_id, user_id)
    finally:
        conn.close()

    assert before_unassign == {"show.broadcast", "show.end"}
    assert after_unassign == set()
