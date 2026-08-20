from app.ws.events import (
    EVENT_CHAT_MESSAGE,
    EVENT_DJ_PRESENCE,
    EVENT_QUEUE_UPDATED,
    EVENT_STUDIO_STATUS,
    EVENT_WEBRTC_ANSWER,
    EVENT_WEBRTC_ICE,
    EVENT_WEBRTC_ERROR,
    make_event,
)


def test_make_event_builds_expected_shape():
    event = make_event(EVENT_QUEUE_UPDATED, station_id=7, payload={"items": [1, 2]})

    assert event["type"] == EVENT_QUEUE_UPDATED
    assert event["station_id"] == 7
    assert event["payload"] == {"items": [1, 2]}
    assert isinstance(event["sent_at"], str)
    assert "T" in event["sent_at"]


def test_studio_coordination_event_names_are_stable():
    assert EVENT_STUDIO_STATUS == "studio.status"
    assert EVENT_DJ_PRESENCE == "dj.presence"
    assert EVENT_CHAT_MESSAGE == "chat.message"


def test_webrtc_event_constants_exist():
    assert EVENT_WEBRTC_ANSWER == "webrtc.answer"
    assert EVENT_WEBRTC_ICE == "webrtc.ice"
    assert EVENT_WEBRTC_ERROR == "webrtc.error"


def test_soundboard_event_constants_exist():
    from app.ws import events
    assert events.EVENT_SOUNDBOARD_PLAYED == "soundboard.played"
    assert events.EVENT_SOUNDBOARD_STOPPED == "soundboard.stopped"
