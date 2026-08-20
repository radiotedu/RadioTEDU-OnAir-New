from app.audio.device_registry import DeviceRegistry


def test_rejects_fifth_local_output():
    reg = DeviceRegistry(max_local_outputs=4)
    assert reg.assign(1, "dev1") is True
    assert reg.assign(2, "dev2") is True
    assert reg.assign(3, "dev3") is True
    assert reg.assign(4, "dev4") is True
    assert reg.assign(5, "dev5") is False
