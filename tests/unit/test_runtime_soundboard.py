from app.audio.sound_effect_player import SoundEffectPlayer
from app.audio.station_runtime import StationRuntime
from app.engine.runtime_registry import StationRuntimeRegistry


def test_station_runtime_has_sound_effect_player():
    rt = StationRuntime(station_id=1)
    assert isinstance(rt.sound_effect_player, SoundEffectPlayer)
    assert rt.sound_effect_player.station_id == 1


def test_registry_get_sound_effect_player_returns_none_for_missing():
    registry = StationRuntimeRegistry()
    assert registry.get_sound_effect_player(999) is None


def test_registry_get_sound_effect_player_returns_player():
    registry = StationRuntimeRegistry()
    runtime = registry._get_or_create(1)
    player = registry.get_sound_effect_player(1)
    assert player is not None
    assert isinstance(player, SoundEffectPlayer)
