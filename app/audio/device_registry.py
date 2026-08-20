class DeviceRegistry:
    def __init__(self, max_local_outputs: int = 4):
        self.max_local_outputs = max_local_outputs
        self._station_to_device: dict[int, str] = {}

    def validate_assignment(self, station_id: int, device_id: str) -> tuple[bool, str]:
        sid = int(station_id)
        normalized = str(device_id or "").strip()
        if not normalized:
            return False, "device_required"
        for assigned_station_id, assigned_device_id in self._station_to_device.items():
            if int(assigned_station_id) != sid and str(assigned_device_id) == normalized:
                return False, "device_in_use"
        if sid not in self._station_to_device and len(self._station_to_device) >= self.max_local_outputs:
            return False, "max_local_outputs"
        return True, ""

    def assign(self, station_id: int, device_id: str) -> bool:
        ok, _reason = self.validate_assignment(station_id, device_id)
        if not ok:
            return False
        self._station_to_device[int(station_id)] = str(device_id).strip()
        return True

    def release(self, station_id: int) -> None:
        self._station_to_device.pop(int(station_id), None)

    def snapshot(self) -> dict[int, str]:
        return dict(self._station_to_device)
