import threading

from app.audio.live_mic_registry import live_mic_registry
from app.repositories.log_repo import LogRepository
from app.repositories.studio_repo import StudioRepository


class StudioServiceError(Exception):
    pass


class StudioNotFoundError(StudioServiceError):
    pass


class StudioConflictError(StudioServiceError):
    pass


class StudioForbiddenError(StudioServiceError):
    pass


class StudioValidationError(StudioServiceError):
    pass


_STATION_LOCKS: dict[int, threading.RLock] = {}
_STATION_LOCKS_LOCK = threading.Lock()


def _station_lock_for(station_id: int) -> threading.RLock:
    sid = int(station_id)
    with _STATION_LOCKS_LOCK:
        lock = _STATION_LOCKS.get(sid)
        if lock is None:
            lock = threading.RLock()
            _STATION_LOCKS[sid] = lock
        return lock


def _normalized_role(actor: dict | None) -> str:
    role = str((actor or {}).get("role") or "").strip().lower()
    return "admin" if role == "superadmin" else role


class StudioService:
    def __init__(self, conn, *, log_repo: LogRepository | None = None):
        self.conn = conn
        self.repo = StudioRepository(conn)
        self.log_repo = log_repo or LogRepository(conn)

    def list_studios(self, station_id: int, actor: dict | None = None) -> list[dict]:
        snapshot = self.snapshot_station(station_id, actor=actor)
        return list(snapshot["studios"])

    def snapshot_station(
        self,
        station_id: int,
        actor: dict | None = None,
        live_presence: dict | None = None,
    ) -> dict:
        station_id = int(station_id)
        actor_id = int((actor or {}).get("id", 0) or 0)
        joined_ids = self._joined_studio_ids(actor_id) if actor_id > 0 else set()
        current_studio_id = (
            self._current_studio_for_user(actor_id, station_id=station_id)
            if actor_id > 0
            else 0
        )
        live_presence = dict(live_presence or {})
        studios = []
        for row in self.repo.list_by_station(station_id):
            studios.append(
                {
                    "id": int(row["id"]),
                    "station_id": int(row["station_id"]),
                    "name": str(row["name"]),
                    "description": str(row["description"] or ""),
                    "sort_order": int(row["sort_order"] or 1),
                    "is_active": bool(row["is_active"]),
                    "is_on_air": bool(row["is_on_air"]),
                    "current_user_id": (
                        int(row["current_user_id"])
                        if row["current_user_id"] is not None
                        else None
                    ),
                    "joined": int(row["id"]) in joined_ids,
                    "live_presence_count": int(
                        live_presence.get(int(row["id"]), 0) or 0
                    ),
                }
            )
        return {
            "station_id": station_id,
            "selected_studio_id": int(current_studio_id or 0),
            "studios": studios,
        }

    def create_studio(
        self,
        *,
        station_id: int,
        name: str,
        actor: dict,
        description: str = "",
    ) -> int:
        self._require_admin(actor)
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise StudioValidationError("studio_name_required")
        station_rows = self.repo.list_by_station(int(station_id))
        next_sort_order = len(station_rows) + 1
        studio_id = self.repo.create(
            station_id=int(station_id),
            name=normalized_name,
            description=str(description or "").strip(),
            sort_order=next_sort_order,
            is_active=True,
            is_on_air=False,
            current_user_id=None,
        )
        self._log(
            station_id=int(station_id),
            message=f"Studio created: {normalized_name}",
            event_type="studio.create",
            actor=actor,
            payload={"studio_id": studio_id},
        )
        return studio_id

    def update_studio(
        self,
        *,
        studio_id: int,
        actor: dict,
        name: str | None = None,
        description: str | None = None,
        sort_order: int | None = None,
        is_active: bool | None = None,
    ) -> dict:
        self._require_admin(actor)
        studio = self._require_studio(studio_id)

        updates: dict[str, object] = {}
        if name is not None:
            normalized_name = str(name or "").strip()
            if not normalized_name:
                raise StudioValidationError("studio_name_required")
            updates["name"] = normalized_name
        if description is not None:
            updates["description"] = str(description or "").strip()
        if sort_order is not None:
            updates["sort_order"] = int(sort_order)
        if is_active is not None:
            updates["is_active"] = 1 if bool(is_active) else 0

        if not updates:
            return self.snapshot_station(int(studio["station_id"]), actor=actor)

        updated = self.repo.update(int(studio_id), **updates)
        if not updated:
            raise StudioConflictError("studio_update_failed")

        self._log(
            station_id=int(studio["station_id"]),
            message=f"Studio updated: {studio_id}",
            event_type="studio.update",
            actor=actor,
            payload={"studio_id": int(studio_id), "updates": sorted(updates.keys())},
        )
        return self.snapshot_station(int(studio["station_id"]), actor=actor)

    def join_studio(self, studio_id: int, actor: dict) -> dict:
        self._require_participant(actor)
        studio = self._require_studio(studio_id)
        with self._station_lock(int(studio["station_id"])):
            studio = self._require_studio(studio_id)
            self._assert_no_other_active_session(studio_id=int(studio_id), actor=actor)

            session_role = self._session_role_for(actor)
            if session_role == "dj":
                active_owner_id = self._active_dj_user_id(int(studio_id))
                if active_owner_id not in {0, int(actor["id"])}:
                    raise StudioConflictError("studio_already_has_active_dj")
                self.repo.set_current_user(int(studio_id), int(actor["id"]))
                # Auto on-air: if no studio is on-air for this station, go on-air
                has_on_air = any(
                    bool(row["is_on_air"])
                    for row in self.repo.list_by_station(int(studio["station_id"]))
                )
                if not has_on_air:
                    self.repo.set_on_air(int(studio_id), True)

            self.repo.upsert_session(
                studio_id=int(studio_id),
                user_id=int(actor["id"]),
                session_role=session_role,
                status="active",
            )
            self._log(
                station_id=int(studio["station_id"]),
                message=f"Studio join: {actor['username']}",
                event_type="studio.join",
                actor=actor,
                payload={"studio_id": int(studio_id), "session_role": session_role},
            )
            return self.snapshot_station(int(studio["station_id"]), actor=actor)

    def leave_studio(self, studio_id: int, actor: dict) -> dict:
        studio = self._require_studio(studio_id)
        with self._station_lock(int(studio["station_id"])):
            studio = self._require_studio(studio_id)
            self.repo.leave_session(int(studio_id), int(actor["id"]))
            if self._studio_current_user_id(studio) == int(actor["id"]):
                self.repo.set_current_user(int(studio_id), None)
                if bool(studio["is_on_air"]):
                    self.repo.set_on_air(int(studio_id), False)
            self._log(
                station_id=int(studio["station_id"]),
                message=f"Studio leave: {actor['username']}",
                event_type="studio.leave",
                actor=actor,
                payload={"studio_id": int(studio_id)},
            )
            return self.snapshot_station(int(studio["station_id"]), actor=actor)

    def handoff(
        self,
        *,
        station_id: int,
        source_studio_id: int,
        target_studio_id: int,
        actor: dict,
    ) -> dict:
        source = self._require_studio(source_studio_id)
        target = self._require_studio(target_studio_id)
        with self._station_lock(int(station_id)):
            source = self._require_studio(source_studio_id)
            target = self._require_studio(target_studio_id)
            self._require_handoff_actor(actor, source)

            if int(source["station_id"]) != int(station_id) or int(target["station_id"]) != int(
                station_id
            ):
                raise StudioConflictError("studio_station_mismatch")
            if not bool(source["is_on_air"]):
                raise StudioConflictError("source_studio_not_on_air")
            if self._active_dj_user_id(int(target_studio_id)) <= 0:
                raise StudioConflictError("target_studio_missing_active_dj")

            for row in self.repo.list_by_station(int(station_id)):
                self.repo.set_on_air(int(row["id"]), int(row["id"]) == int(target_studio_id))

            self._log(
                station_id=int(station_id),
                message=f"Studio handoff: {source_studio_id}->{target_studio_id}",
                event_type="studio.handoff",
                actor=actor,
                payload={
                    "source_studio_id": int(source_studio_id),
                    "target_studio_id": int(target_studio_id),
                },
            )
            return self.snapshot_station(int(station_id), actor=actor)

    def start_mic_transmission(
        self,
        station_id: int,
        actor: dict,
        *,
        live_mic_registry=live_mic_registry,
    ) -> dict:
        station_id = int(station_id)
        with self._station_lock(station_id):
            if _normalized_role(actor) != "admin":
                allowed, detail = self.can_user_activate_mic(
                    station_id, int(actor["id"])
                )
                if not allowed:
                    raise StudioForbiddenError(detail)
            return live_mic_registry.start_transmission(
                station_id,
                {
                    "id": int(actor["id"]),
                    "username": str(actor["username"]),
                    "role": str(actor["role"]),
                },
            )

    def start_render_transmission(
        self,
        station_id: int,
        actor: dict,
        *,
        source_name: str = "OmniVoice",
        input_format: str = "s16le",
        sample_rate: int = 24000,
        channels: int = 1,
        max_buffer_bytes: int = 480000,
        live_mic_registry=live_mic_registry,
    ) -> dict:
        station_id = int(station_id)
        with self._station_lock(station_id):
            if _normalized_role(actor) != "admin":
                allowed, detail = self.can_user_activate_mic(
                    station_id, int(actor["id"])
                )
                if not allowed:
                    raise StudioForbiddenError(detail)
            return live_mic_registry.start_render_transmission(
                station_id,
                {
                    "id": int(actor["id"]),
                    "username": str(actor["username"]),
                    "role": str(actor["role"]),
                },
                source_name=str(source_name or "OmniVoice"),
                input_format=str(input_format or "s16le"),
                sample_rate=int(sample_rate or 24000),
                channels=int(channels or 1),
                max_buffer_bytes=int(max_buffer_bytes or 480000),
            )

    def list_chat_messages(
        self,
        studio_id: int,
        *,
        limit: int = 50,
    ) -> list[dict]:
        rows = self.repo.list_chat_messages(int(studio_id), limit=limit)
        return [self._message_row_to_dict(row) for row in reversed(list(rows))]

    def send_chat_message(self, studio_id: int, actor: dict, message: str) -> dict:
        studio = self._require_studio(studio_id)
        normalized = str(message or "").strip()
        if not normalized:
            raise StudioValidationError("message_required")
        if not self._has_joined_session(int(studio_id), int(actor["id"])):
            raise StudioForbiddenError("chat_requires_joined_session")
        message_id = self.repo.create_chat_message(int(studio_id), int(actor["id"]), normalized)
        self._log(
            station_id=int(studio["station_id"]),
            message=f"Studio chat: {actor['username']}",
            event_type="studio.chat",
            actor=actor,
            payload={"studio_id": int(studio_id), "message_id": int(message_id)},
        )
        rows = self.repo.list_chat_messages(int(studio_id), limit=1)
        return self._message_row_to_dict(rows[0])

    def can_user_activate_mic(self, station_id: int, user_id: int) -> tuple[bool, str]:
        on_air_studio = None
        for row in self.repo.list_by_station(int(station_id)):
            if bool(row["is_on_air"]):
                on_air_studio = row
                break
        if on_air_studio is None:
            return False, "no_on_air_studio"
        current_user_id = self._studio_current_user_id(on_air_studio)
        if current_user_id != int(user_id):
            return False, "not_studio_owner"
        if not self._has_dj_session(int(on_air_studio["id"]), int(user_id)):
            return False, "no_dj_session"
        return True, "ok"

    def _station_lock(self, station_id: int) -> threading.RLock:
        return _station_lock_for(int(station_id))

    def _require_studio(self, studio_id: int):
        row = self.repo.get(int(studio_id))
        if row is None:
            raise StudioNotFoundError("studio_not_found")
        return row

    def _require_admin(self, actor: dict) -> None:
        if _normalized_role(actor) != "admin":
            raise StudioForbiddenError("admin_required")

    def _require_participant(self, actor: dict) -> None:
        if _normalized_role(actor) not in {"admin", "dj", "producer"}:
            raise StudioForbiddenError("participant_role_required")

    def _require_handoff_actor(self, actor: dict, source) -> None:
        role = _normalized_role(actor)
        if role == "admin":
            return
        if role != "dj":
            raise StudioForbiddenError("handoff_forbidden")
        if self._studio_current_user_id(source) != int(actor["id"]):
            raise StudioForbiddenError("handoff_forbidden")

    def _session_role_for(self, actor: dict) -> str:
        if _normalized_role(actor) == "producer":
            return "producer"
        return "dj"

    def _studio_current_user_id(self, studio_row) -> int:
        if studio_row["current_user_id"] is None:
            return 0
        return int(studio_row["current_user_id"])

    def _joined_studio_ids(self, user_id: int) -> set[int]:
        if user_id <= 0:
            return set()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT studio_id FROM studio_sessions "
            "WHERE user_id=? AND status IN ('active', 'standby')",
            (int(user_id),),
        )
        return {int(row["studio_id"]) for row in cur.fetchall()}

    def _current_studio_for_user(self, user_id: int, *, station_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT s.id FROM studio_sessions ss "
            "JOIN studios s ON s.id = ss.studio_id "
            "WHERE ss.user_id=? AND ss.status IN ('active', 'standby') AND s.station_id=? "
            "ORDER BY s.sort_order ASC, s.id ASC LIMIT 1",
            (int(user_id), int(station_id)),
        )
        row = cur.fetchone()
        if row is None:
            return 0
        return int(row["id"])

    def _assert_no_other_active_session(self, *, studio_id: int, actor: dict) -> None:
        current_joined = self._joined_studio_ids(int(actor["id"]))
        current_joined.discard(int(studio_id))
        if current_joined:
            raise StudioConflictError("user_already_joined_other_studio")

    def _active_dj_user_id(self, studio_id: int) -> int:
        studio = self._require_studio(int(studio_id))
        if studio["current_user_id"] is None:
            return 0
        user_id = int(studio["current_user_id"])
        if self._has_dj_session(int(studio_id), user_id):
            return user_id
        return 0

    def _has_dj_session(self, studio_id: int, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM studio_sessions WHERE studio_id=? AND user_id=? "
            "AND status IN ('active', 'standby') AND session_role='dj' LIMIT 1",
            (int(studio_id), int(user_id)),
        )
        return cur.fetchone() is not None

    def _has_joined_session(self, studio_id: int, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM studio_sessions WHERE studio_id=? AND user_id=? "
            "AND status IN ('active', 'standby') LIMIT 1",
            (int(studio_id), int(user_id)),
        )
        return cur.fetchone() is not None

    def _message_row_to_dict(self, row) -> dict:
        return {
            "id": int(row["id"]),
            "studio_id": int(row["studio_id"]),
            "user_id": int(row["user_id"]),
            "message": str(row["message"]),
            "created_at": str(row["created_at"]),
        }

    def _log(
        self,
        *,
        station_id: int,
        message: str,
        event_type: str,
        actor: dict,
        payload: dict | None = None,
    ) -> int:
        merged_payload = {
            "actor_user_id": int(actor["id"]),
            "actor_username": str(actor["username"]),
            "actor_role": str(actor["role"]),
            **dict(payload or {}),
        }
        return self.log_repo.add_operation_log(
            station_id=int(station_id),
            message=str(message),
            event_type=str(event_type),
            level="info",
            payload=merged_payload,
        )
