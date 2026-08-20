from __future__ import annotations


class ProcessIsolatedSoundEffectPlayer:
    def __init__(self, station_id: int, worker_manager):
        self.station_id = int(station_id)
        self.worker_manager = worker_manager

    def play(self, item: dict) -> None:
        self.worker_manager.soundboard_play(self.station_id, dict(item))

    def stop(self, item_id: int | None = None) -> None:
        self.worker_manager.soundboard_stop(self.station_id, item_id=item_id)


class ProcessIsolatedRuntimeFacade:
    """Expose station-worker-owned runtimes through the existing API contract."""

    def __init__(self, local_registry, worker_manager):
        self.local_registry = local_registry
        self.worker_manager = worker_manager
        live_mic_registry = getattr(local_registry, "_live_mic_registry", None)
        register_listener = getattr(live_mic_registry, "register_listener", None)
        if callable(register_listener):
            register_listener(self._handle_live_mic_event)

    def _handle_live_mic_event(
        self,
        event_type: str,
        station_id: int,
        _snapshot: dict,
    ) -> None:
        if str(event_type or "").strip().lower() != "start":
            return
        try:
            self.promote_live_mix(int(station_id))
        except Exception:
            # The registry event must remain non-fatal; a station may not have
            # started its scheduler yet.
            pass

    def start_station(self, station_id: int, input_uri: str, **kwargs) -> dict:
        return self.worker_manager.start_runtime(
            int(station_id),
            str(input_uri),
            **kwargs,
        )

    def stop_station(self, station_id: int) -> dict:
        station_id = int(station_id)
        if bool(self.worker_manager.status(station_id).get("running")):
            return self.worker_manager.stop_runtime(station_id)
        return dict(self.local_registry.stop_station(station_id) or {})

    def status(self, station_id: int) -> dict:
        station_id = int(station_id)
        worker = self.worker_manager.status(station_id)
        runtime = worker.get("runtime_status")
        if isinstance(runtime, dict) and runtime:
            payload = dict(runtime)
        else:
            payload = dict(self.local_registry.status(station_id) or {})
        payload["station_worker"] = worker
        return payload

    def is_process_running(self, station_id: int) -> bool:
        status = self.status(int(station_id))
        return bool(
            status.get("output_feed_active")
            or status.get("program_running")
            or status.get("running")
        )

    def snapshot(self) -> list[dict]:
        snapshots = []
        seen = set()
        for worker in self.worker_manager.snapshot():
            station_id = int(worker.get("station_id") or 0)
            if station_id <= 0:
                continue
            seen.add(station_id)
            snapshots.append(self.status(station_id))
        for runtime in self.local_registry.snapshot():
            station_id = int(runtime.get("station_id") or 0)
            if station_id > 0 and station_id not in seen:
                snapshots.append(dict(runtime))
        return snapshots

    def stop_all(self) -> dict:
        worker_result = dict(self.worker_manager.stop_all() or {})
        local_result = dict(self.local_registry.stop_all() or {})
        return {"isolated_workers": worker_result, "local_runtimes": local_result}

    def recover_station(self, station_id: int, *, force: bool = False) -> dict:
        station_id = int(station_id)
        if bool(self.worker_manager.status(station_id).get("running")):
            return self.worker_manager.recover_runtime(station_id, force=force)
        return dict(
            self.local_registry.recover_station(station_id, force=force) or {}
        )

    def required_outputs_healthy(self, station_id: int) -> bool:
        status = self.status(int(station_id))
        feed_active = bool(
            status.get("output_feed_active")
            or status.get("program_running")
            or status.get("running")
        )
        if not feed_active:
            return False
        branches = status.get("branch_health")
        if not isinstance(branches, dict) or not branches:
            return True
        required = status.get("required_outputs")
        if not isinstance(required, dict):
            required = {"icecast": True, "local": False}
        required_branches = [
            str(branch) for branch, enabled in required.items() if bool(enabled)
        ]
        if not required_branches:
            return True
        return all(
            bool(
                branches.get(
                    branch,
                    branches.get("icecast", False)
                    if branch.startswith("icecast:")
                    else False,
                )
            )
            for branch in required_branches
        )

    def refresh_live_audio_settings(self, station_id: int) -> dict:
        station_id = int(station_id)
        if bool(self.worker_manager.status(station_id).get("running")):
            return self.worker_manager.refresh_runtime_settings(station_id)
        return dict(
            self.local_registry.refresh_live_audio_settings(station_id) or {}
        )

    def refresh_output_settings(self, station_id: int) -> dict:
        station_id = int(station_id)
        if bool(self.worker_manager.status(station_id).get("running")):
            return self.worker_manager.refresh_output_settings(station_id)
        return dict(self.local_registry.refresh_output_settings(station_id) or {})

    def promote_live_mix(self, station_id: int, *, force: bool = False) -> bool:
        station_id = int(station_id)
        if bool(self.worker_manager.status(station_id).get("running")):
            return bool(
                self.worker_manager.promote_runtime_live_mix(
                    station_id,
                    force=bool(force),
                )
            )
        return bool(
            self.local_registry.promote_live_mix(
                station_id,
                force=bool(force),
            )
        )

    def get_sound_effect_player(self, station_id: int):
        station_id = int(station_id)
        if bool(self.worker_manager.status(station_id).get("running")):
            runtime_status = self.worker_manager.runtime_status(station_id)
            if not bool(
                runtime_status.get("program_running")
                or runtime_status.get("output_feed_active")
                or runtime_status.get("running")
            ):
                return None
            return ProcessIsolatedSoundEffectPlayer(
                station_id,
                self.worker_manager,
            )
        return self.local_registry.get_sound_effect_player(station_id)

    def __getattr__(self, name: str):
        return getattr(self.local_registry, name)
