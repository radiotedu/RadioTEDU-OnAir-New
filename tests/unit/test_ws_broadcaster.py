import asyncio

from app.ws.broadcaster import EventBroadcaster
from app.ws.manager import ConnectionManager


class _DummyWebSocket:
    def __init__(self):
        self.sent = []

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.sent.append(payload)


def test_broadcaster_sends_queue_and_engine_events_to_station_room():
    async def scenario():
        manager = ConnectionManager()
        ws = _DummyWebSocket()
        await manager.connect(
            ws,
            connection_id="conn-1",
            rooms={"station:3"},
            user={"id": 9, "username": "producer"},
        )

        broadcaster = EventBroadcaster(manager)
        await broadcaster.broadcast_queue_changed(3, {"items": [{"id": 11}]})
        await broadcaster.broadcast_engine_event(3, {"alive": True})

        assert [item["type"] for item in ws.sent] == [
            "queue.updated",
            "engine.event",
        ]
        assert ws.sent[0]["payload"]["items"][0]["id"] == 11

    asyncio.run(scenario())


def test_broadcaster_sends_studio_presence_and_chat_events_to_station_room():
    async def scenario():
        manager = ConnectionManager()
        ws = _DummyWebSocket()
        await manager.connect(
            ws,
            connection_id="conn-2",
            rooms={"station:5"},
            user={"id": 15, "username": "dj-live"},
        )

        broadcaster = EventBroadcaster(manager)
        await broadcaster.broadcast_studio_status(5, {"studios": [{"id": 9}]})
        await broadcaster.broadcast_dj_presence(5, {"count": 1})
        await broadcaster.broadcast_chat_message(5, {"message": "hello"})

        assert [item["type"] for item in ws.sent] == [
            "studio.status",
            "dj.presence",
            "chat.message",
        ]
        assert ws.sent[2]["payload"]["message"] == "hello"

    asyncio.run(scenario())
