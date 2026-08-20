import asyncio
from collections import defaultdict


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, object] = {}
        self._connection_rooms: dict[str, set[str]] = {}
        self._rooms: dict[str, set[str]] = defaultdict(set)
        self._presence: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def reset(self) -> None:
        self._connections.clear()
        self._connection_rooms.clear()
        self._rooms.clear()
        self._presence.clear()
        self._loop = None

    async def connect(
        self,
        websocket,
        *,
        connection_id: str,
        rooms: set[str] | list[str] | tuple[str, ...],
        user: dict | None = None,
    ) -> None:
        await websocket.accept()
        self.bind_loop(asyncio.get_running_loop())
        room_set = {str(room) for room in rooms if str(room).strip()}
        self._connections[connection_id] = websocket
        self._connection_rooms[connection_id] = room_set
        self._presence[connection_id] = {
            "connection_id": str(connection_id),
            "user_id": int((user or {}).get("id", 0) or 0),
            "username": str((user or {}).get("username", "") or ""),
            "rooms": sorted(room_set),
        }
        for room in room_set:
            self._rooms[room].add(connection_id)

    async def move_to_rooms(
        self,
        connection_id: str,
        rooms: set[str] | list[str] | tuple[str, ...],
    ) -> None:
        previous_rooms = self._connection_rooms.get(connection_id, set())
        next_rooms = {str(room) for room in rooms if str(room).strip()}
        for room in previous_rooms - next_rooms:
            bucket = self._rooms.get(room)
            if bucket is not None:
                bucket.discard(connection_id)
                if not bucket:
                    self._rooms.pop(room, None)
        for room in next_rooms - previous_rooms:
            self._rooms[room].add(connection_id)
        self._connection_rooms[connection_id] = next_rooms
        if connection_id in self._presence:
            self._presence[connection_id]["rooms"] = sorted(next_rooms)

    def disconnect(self, connection_id: str) -> None:
        rooms = self._connection_rooms.pop(connection_id, set())
        self._connections.pop(connection_id, None)
        self._presence.pop(connection_id, None)
        for room in rooms:
            bucket = self._rooms.get(room)
            if bucket is not None:
                bucket.discard(connection_id)
                if not bucket:
                    self._rooms.pop(room, None)
        if not self._connections:
            self._loop = None

    def rooms_for(self, connection_id: str) -> set[str]:
        return set(self._connection_rooms.get(connection_id, set()))

    def presence(self, room: str | None = None) -> dict:
        if room is None:
            connections = list(self._presence.values())
        else:
            room_ids = self._rooms.get(str(room), set())
            connections = [
                self._presence[connection_id]
                for connection_id in room_ids
                if connection_id in self._presence
            ]
        return {
            "count": len(connections),
            "connections": sorted(
                connections,
                key=lambda item: (
                    int(item.get("user_id", 0) or 0),
                    str(item.get("connection_id", "")),
                ),
            ),
        }

    async def send_json(self, connection_id: str, payload: dict) -> None:
        websocket = self._connections.get(connection_id)
        if websocket is None:
            return
        try:
            await websocket.send_json(payload)
        except Exception:
            self.disconnect(connection_id)

    async def broadcast(self, payload: dict, rooms: set[str] | list[str] | tuple[str, ...] | None = None) -> None:
        if rooms:
            target_ids: set[str] = set()
            for room in rooms:
                target_ids.update(self._rooms.get(str(room), set()))
        else:
            target_ids = set(self._connections.keys())

        for connection_id in list(target_ids):
            await self.send_json(connection_id, payload)

    def broadcast_sync(self, payload: dict, rooms: set[str] | list[str] | tuple[str, ...] | None = None) -> bool:
        if not self._connections:
            self._loop = None
            return False
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return False
        future = asyncio.run_coroutine_threadsafe(self.broadcast(payload, rooms=rooms), loop)
        try:
            future.result(timeout=1.0)
            return True
        except Exception:
            return False
