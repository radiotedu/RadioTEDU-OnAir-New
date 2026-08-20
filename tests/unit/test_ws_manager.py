import asyncio

from app.ws.manager import ConnectionManager


class _DummyWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)


def test_connection_manager_tracks_rooms_and_presence():
    async def scenario():
        manager = ConnectionManager()
        ws = _DummyWebSocket()

        await manager.connect(
            ws,
            connection_id="conn-1",
            rooms={"station:1", "all"},
            user={"id": 1, "username": "admin"},
        )

        assert ws.accepted is True
        assert manager.rooms_for("conn-1") == {"station:1", "all"}
        assert manager.presence("station:1")["count"] == 1

        await manager.broadcast({"type": "test"}, rooms={"station:1"})
        assert ws.sent == [{"type": "test"}]

        manager.disconnect("conn-1")
        assert manager.presence("station:1")["count"] == 0
        assert manager._loop is None

    asyncio.run(scenario())
