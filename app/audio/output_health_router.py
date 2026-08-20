class OutputHealthRouter:
    def __init__(self):
        self._health = {"icecast": True, "local": True}

    def set_branch_health(self, branch: str, healthy: bool) -> None:
        self._health[branch] = bool(healthy)

    def is_output_active(self, branch: str) -> bool:
        return bool(self._health.get(branch, False))

    def status(self) -> dict[str, bool]:
        return {
            "icecast": self.is_output_active("icecast"),
            "local": self.is_output_active("local"),
        }
