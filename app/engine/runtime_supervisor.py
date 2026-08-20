from app.engine.recovery_policy import decide_recovery_action


class RuntimeSupervisor:
    def __init__(self, runtime_registry):
        self.runtime_registry = runtime_registry

    def evaluate_station(self, station_id: int) -> dict:
        status = self.runtime_registry.status(station_id)
        running = bool(status.get("running", False))
        branches = status.get("branch_health", {}) or {}
        required = status.get("required_outputs", {}) or {}
        icecast_ok = bool(branches.get("icecast", False))
        local_ok = bool(branches.get("local", False))
        icecast_required = bool(required.get("icecast", True))
        local_required = bool(required.get("local", True))

        if not running:
            if bool(status.get("program_running", False)):
                recover = getattr(self.runtime_registry, "recover_station", None)
                if callable(recover):
                    recover(station_id)
                return {"station_id": station_id, "action": "degrade"}
            action = decide_recovery_action(component_error=True, recoverable=False)
            return {"station_id": station_id, "action": action}

        if icecast_required and not icecast_ok:
            action = decide_recovery_action(component_error=True, recoverable=True)
            recover = getattr(self.runtime_registry, "recover_station", None)
            if callable(recover):
                recover(station_id)
            return {"station_id": station_id, "action": action}

        if local_required and not local_ok:
            action = decide_recovery_action(component_error=True, recoverable=True)
            recover = getattr(self.runtime_registry, "recover_station", None)
            if callable(recover):
                recover(station_id)
            return {"station_id": station_id, "action": action}

        return {"station_id": station_id, "action": "none"}
