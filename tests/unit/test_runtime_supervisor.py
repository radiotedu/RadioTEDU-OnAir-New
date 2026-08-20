from app.engine.runtime_supervisor import RuntimeSupervisor


class _FakeRegistry:
    def __init__(self, status_map):
        self.status_map = status_map
        self.stop_calls = []

    def status(self, station_id: int):
        return self.status_map[station_id]

    def stop_station(self, station_id: int):
        self.stop_calls.append(station_id)
        return self.status_map[station_id]


def test_degrade_when_only_local_branch_unhealthy():
    reg = _FakeRegistry(
        {
            1: {
                "station_id": 1,
                "running": True,
                "branch_health": {"icecast": True, "local": False},
            }
        }
    )
    sup = RuntimeSupervisor(reg)
    out = sup.evaluate_station(1)
    assert out["action"] == "degrade"
    assert reg.stop_calls == []


def test_no_action_when_local_branch_unhealthy_but_not_required():
    reg = _FakeRegistry(
        {
            3: {
                "station_id": 3,
                "running": True,
                "branch_health": {"icecast": True, "local": False},
                "required_outputs": {"icecast": True, "local": False},
            }
        }
    )
    sup = RuntimeSupervisor(reg)
    out = sup.evaluate_station(3)
    assert out["action"] == "none"
    assert reg.stop_calls == []


def test_degrade_when_icecast_branch_fails_but_program_is_running():
    reg = _FakeRegistry(
        {
            2: {
                "station_id": 2,
                "running": True,
                "branch_health": {"icecast": False, "local": False},
            }
        }
    )
    sup = RuntimeSupervisor(reg)
    out = sup.evaluate_station(2)
    assert out["action"] == "degrade"
    assert reg.stop_calls == []
