from app.engine.recovery_policy import decide_recovery_action


class RuntimeSupervisor:
    def __init__(self, runtime_registry):
        self.runtime_registry = runtime_registry

    def evaluate_station(self, station_id: int) -> dict:
        status = self.runtime_registry.status(station_id)
        running = bool(status.get("running", False))
        branches = status.get("branch_health", {}) or {}
        delivery = status.get("delivery_health", branches) or branches
        required = status.get("required_outputs", {}) or {}
        icecast_ok = bool(delivery.get("icecast", branches.get("icecast", False)))
        local_ok = bool(delivery.get("local", branches.get("local", False)))
        icecast_required = bool(required.get("icecast", True))
        local_required = bool(required.get("local", True))
        extra_required_unhealthy = [
            str(output_name)
            for output_name, is_required in required.items()
            if bool(is_required)
            and str(output_name) not in {"icecast", "local"}
            and not bool(
                delivery.get(
                    str(output_name), branches.get(str(output_name), False)
                )
            )
        ]

        if not running:
            if bool(status.get("program_running", False)):
                recover = getattr(self.runtime_registry, "recover_station", None)
                if callable(recover):
                    recover(station_id)
                return {"station_id": station_id, "action": "degrade"}
            action = decide_recovery_action(component_error=True, recoverable=False)
            return {"station_id": station_id, "action": action}

        if icecast_required and not icecast_ok:
            recover_primary = getattr(
                self.runtime_registry, "recover_station_primary_output", None
            )
            if callable(recover_primary):
                recover_primary(station_id)
                return {"station_id": station_id, "action": "recover_primary_output"}
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

        if extra_required_unhealthy:
            action = decide_recovery_action(component_error=True, recoverable=True)
            recover_output = getattr(
                self.runtime_registry, "recover_station_output", None
            )
            if callable(recover_output):
                for branch in extra_required_unhealthy:
                    recover_output(station_id, branch)
            return {"station_id": station_id, "action": action}

        return {"station_id": station_id, "action": "none"}
