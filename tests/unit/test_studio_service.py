import pytest

from app.db import get_connection, init_db
from app.repositories.station_repo import StationRepository
from app.repositories.user_repo import UserRepository


def _make_service(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANROOM_DB_PATH", str(tmp_path / "cleanroom.db"))
    init_db()
    conn = get_connection()
    return conn


def _make_user(repo: UserRepository, username: str, role: str) -> dict:
    user_id = repo.create_user(username, username.replace("-", " ").title(), "hash", role)
    return {
        "id": user_id,
        "username": username,
        "role": role,
    }


def test_join_rejects_second_active_dj_for_same_studio(tmp_path, monkeypatch):
    from app.services.studio_service import StudioConflictError, StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("Studio Test")
        user_repo = UserRepository(conn)
        dj_a = _make_user(user_repo, "dj-a", "dj")
        dj_b = _make_user(user_repo, "dj-b", "dj")
        service = StudioService(conn)
        studio = service.list_studios(station_id=station_id)[0]

        snapshot = service.join_studio(int(studio["id"]), dj_a)

        assert snapshot["studios"][0]["current_user_id"] == dj_a["id"]

        with pytest.raises(StudioConflictError):
            service.join_studio(int(studio["id"]), dj_b)
    finally:
        conn.close()


def test_handoff_requires_target_active_dj(tmp_path, monkeypatch):
    from app.services.studio_service import StudioConflictError, StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("Talk Test")
        admin = _make_user(UserRepository(conn), "admin-handoff", "admin")
        dj_a = _make_user(UserRepository(conn), "dj-source", "dj")
        service = StudioService(conn)
        studios = service.list_studios(station_id=station_id)
        source_studio_id = int(studios[0]["id"])
        target_studio_id = service.create_studio(
            station_id=station_id,
            name="Studio B",
            actor=admin,
        )

        service.join_studio(source_studio_id, dj_a)

        with pytest.raises(StudioConflictError):
            service.handoff(
                station_id=station_id,
                source_studio_id=source_studio_id,
                target_studio_id=target_studio_id,
                actor=admin,
            )
    finally:
        conn.close()


def test_update_studio_requires_admin_and_persists_changes(tmp_path, monkeypatch):
    from app.services.studio_service import StudioForbiddenError, StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("Update Test")
        admin = _make_user(UserRepository(conn), "admin-update", "admin")
        dj_user = _make_user(UserRepository(conn), "dj-update", "dj")
        service = StudioService(conn)
        studio = service.list_studios(station_id=station_id)[0]

        with pytest.raises(StudioForbiddenError):
            service.update_studio(
                studio_id=int(studio["id"]),
                actor=dj_user,
                name="Renamed Studio",
            )

        snapshot = service.update_studio(
            studio_id=int(studio["id"]),
            actor=admin,
            name="Renamed Studio",
            description="Updated desc",
            sort_order=7,
            is_active=False,
        )

        updated = snapshot["studios"][0]
        assert updated["name"] == "Renamed Studio"
        assert updated["description"] == "Updated desc"
        assert updated["sort_order"] == 7
        assert updated["is_active"] is False
    finally:
        conn.close()


def test_update_studio_rejects_state_fields_and_preserves_ownership(tmp_path, monkeypatch):
    from app.services.studio_service import StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("State Guard Test")
        admin = _make_user(UserRepository(conn), "admin-state", "admin")
        dj_user = _make_user(UserRepository(conn), "dj-state", "dj")
        service = StudioService(conn)
        studio = service.list_studios(station_id=station_id)[0]

        service.join_studio(int(studio["id"]), dj_user)
        before = service.snapshot_station(station_id, actor=admin)["studios"][0]

        with pytest.raises(TypeError):
            service.update_studio(
                studio_id=int(studio["id"]),
                actor=admin,
                current_user_id=None,
            )

        with pytest.raises(TypeError):
            service.update_studio(
                studio_id=int(studio["id"]),
                actor=admin,
                is_on_air=True,
            )

        snapshot = service.update_studio(
            studio_id=int(studio["id"]),
            actor=admin,
            name="Metadata Only",
            description="Still metadata",
            sort_order=3,
            is_active=True,
        )

        updated = snapshot["studios"][0]
        assert updated["name"] == "Metadata Only"
        assert updated["description"] == "Still metadata"
        assert updated["sort_order"] == 3
        assert updated["current_user_id"] == before["current_user_id"]
        assert updated["is_on_air"] == before["is_on_air"]
    finally:
        conn.close()


def test_producer_join_does_not_claim_current_user(tmp_path, monkeypatch):
    from app.services.studio_service import StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("Producer Test")
        producer = _make_user(UserRepository(conn), "producer-a", "producer")
        service = StudioService(conn)
        studio = service.list_studios(station_id=station_id)[0]

        snapshot = service.join_studio(int(studio["id"]), producer)

        assert snapshot["studios"][0]["current_user_id"] is None
        assert snapshot["studios"][0]["joined"] is True
    finally:
        conn.close()


def test_send_chat_requires_joined_session(tmp_path, monkeypatch):
    from app.services.studio_service import StudioForbiddenError, StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("Chat Test")
        dj_user = _make_user(UserRepository(conn), "chat-dj", "dj")
        service = StudioService(conn)
        studio = service.list_studios(station_id=station_id)[0]

        with pytest.raises(StudioForbiddenError):
            service.send_chat_message(int(studio["id"]), dj_user, "hello")

        service.join_studio(int(studio["id"]), dj_user)
        message = service.send_chat_message(int(studio["id"]), dj_user, "hello")

        assert message["message"] == "hello"
        assert message["user_id"] == dj_user["id"]
    finally:
        conn.close()


def test_can_user_activate_mic_requires_on_air_owner(tmp_path, monkeypatch):
    from app.services.studio_service import StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("Mic Test")
        admin = _make_user(UserRepository(conn), "admin-mic", "admin")
        dj_a = _make_user(UserRepository(conn), "mic-dj-a", "dj")
        dj_b = _make_user(UserRepository(conn), "mic-dj-b", "dj")
        service = StudioService(conn)
        source_studio = service.list_studios(station_id=station_id)[0]
        target_studio_id = service.create_studio(
            station_id=station_id,
            name="Studio B",
            actor=admin,
        )

        service.join_studio(int(source_studio["id"]), dj_a)
        service.join_studio(target_studio_id, dj_b)

        allowed, detail = service.can_user_activate_mic(station_id, dj_a["id"])
        assert allowed is True
        assert detail == "ok"

        service.handoff(
            station_id=station_id,
            source_studio_id=int(source_studio["id"]),
            target_studio_id=target_studio_id,
            actor=admin,
        )

        allowed, detail = service.can_user_activate_mic(station_id, dj_a["id"])
        assert allowed is False
        assert detail == "not_studio_owner"

        allowed, detail = service.can_user_activate_mic(station_id, dj_b["id"])
        assert allowed is True
        assert detail == "ok"
    finally:
        conn.close()


def test_start_mic_transmission_requires_authorization_and_activates_registry(
    tmp_path, monkeypatch
):
    from app.services.studio_service import StudioForbiddenError, StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("Mic Start Test")
        admin = _make_user(UserRepository(conn), "admin-start", "admin")
        producer = _make_user(UserRepository(conn), "producer-start", "producer")
        service = StudioService(conn)
        studio = service.list_studios(station_id=station_id)[0]
        service.join_studio(int(studio["id"]), admin)

        started = {}

        class FakeRegistry:
            def start_transmission(self, station_id: int, user: dict) -> dict:
                started["station_id"] = int(station_id)
                started["user"] = dict(user)
                return {
                    "station_id": int(station_id),
                    "transmitting": True,
                    "active_user": dict(user),
                }

        with pytest.raises(StudioForbiddenError):
            service.start_mic_transmission(
                station_id=station_id,
                actor=producer,
                live_mic_registry=FakeRegistry(),
            )

        snapshot = service.start_mic_transmission(
            station_id=station_id,
            actor=admin,
            live_mic_registry=FakeRegistry(),
        )

        assert started["station_id"] == station_id
        assert started["user"]["id"] == admin["id"]
        assert snapshot["station_id"] == station_id
        assert snapshot["transmitting"] is True
        assert snapshot["active_user"]["id"] == admin["id"]
    finally:
        conn.close()


def test_superadmin_can_start_direct_operator_mic_without_studio_claim(
    tmp_path, monkeypatch
):
    from app.services.studio_service import StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_id = StationRepository(conn).create("Direct Operator Mic")
        superadmin = _make_user(
            UserRepository(conn), "legacy-superadmin-mic", "superadmin"
        )
        started = {}

        class FakeRegistry:
            def start_transmission(self, station_id: int, user: dict) -> dict:
                started["station_id"] = int(station_id)
                started["user"] = dict(user)
                return {
                    "station_id": int(station_id),
                    "transmitting": True,
                    "active_user": dict(user),
                }

        snapshot = StudioService(conn).start_mic_transmission(
            station_id=station_id,
            actor=superadmin,
            live_mic_registry=FakeRegistry(),
        )

        assert snapshot["transmitting"] is True
        assert started["station_id"] == station_id
        assert started["user"]["role"] == "superadmin"
    finally:
        conn.close()


def test_snapshot_station_only_selects_joined_studio_in_requested_station(
    tmp_path, monkeypatch
):
    from app.services.studio_service import StudioService

    conn = _make_service(tmp_path, monkeypatch)
    try:
        station_a_id = StationRepository(conn).create("Station A")
        station_b_id = StationRepository(conn).create("Station B")
        dj_user = _make_user(UserRepository(conn), "station-scope-dj", "dj")
        service = StudioService(conn)
        studio_a = service.list_studios(station_id=station_a_id)[0]

        service.join_studio(int(studio_a["id"]), dj_user)
        snapshot = service.snapshot_station(station_b_id, actor=dj_user)

        assert snapshot["selected_studio_id"] == 0
        assert len(snapshot["studios"]) == 1
        assert snapshot["studios"][0]["station_id"] == station_b_id
    finally:
        conn.close()
