from app.ws.events import (
    EVENT_CHAT_MESSAGE,
    EVENT_DJ_PRESENCE,
    EVENT_ENGINE_EVENT,
    EVENT_HEALTH_UPDATED,
    EVENT_MIC_STATUS,
    EVENT_QUEUE_UPDATED,
    EVENT_RUNTIME_UPDATED,
    EVENT_STUDIO_STATUS,
    EVENT_TRACK_CHANGED,
    make_event,
)
from app.ws.manager import ConnectionManager


class EventBroadcaster:
    def __init__(self, manager: ConnectionManager):
        self.manager = manager

    @staticmethod
    def station_room(station_id: int) -> str:
        return f"station:{int(station_id)}"

    @staticmethod
    def base_rooms(station_id: int) -> set[str]:
        return {"all", EventBroadcaster.station_room(station_id)}

    async def broadcast_queue_changed(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_QUEUE_UPDATED, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_engine_event(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_ENGINE_EVENT, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_runtime_updated(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_RUNTIME_UPDATED, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_track_changed(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_TRACK_CHANGED, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_health_changed(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_HEALTH_UPDATED, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_studio_status(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_STUDIO_STATUS, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_dj_presence(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_DJ_PRESENCE, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_chat_message(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_CHAT_MESSAGE, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_mic_status(self, station_id: int, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(EVENT_MIC_STATUS, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_queue_changed(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_QUEUE_UPDATED, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_engine_event(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_ENGINE_EVENT, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_runtime_updated(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_RUNTIME_UPDATED, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_track_changed(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_TRACK_CHANGED, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_health_changed(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_HEALTH_UPDATED, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_studio_status(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_STUDIO_STATUS, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_dj_presence(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_DJ_PRESENCE, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_chat_message(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_CHAT_MESSAGE, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_mic_status(self, station_id: int, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(EVENT_MIC_STATUS, station_id, payload),
            rooms=self.base_rooms(station_id),
        )


    def on_soundboard_event(self, station_id: int, event_type: str, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(event_type, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_soundboard_event(self, station_id: int, event_type: str, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(event_type, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    def on_show_event(self, station_id: int, event_type: str, payload: dict) -> bool:
        return self.manager.broadcast_sync(
            make_event(event_type, station_id, payload),
            rooms=self.base_rooms(station_id),
        )

    async def broadcast_show_event(self, station_id: int, event_type: str, payload: dict) -> None:
        await self.manager.broadcast(
            make_event(event_type, station_id, payload),
            rooms=self.base_rooms(station_id),
        )


connection_manager = ConnectionManager()
broadcaster = EventBroadcaster(connection_manager)
