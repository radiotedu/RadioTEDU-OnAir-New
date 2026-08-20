import os
import sys
from threading import Thread


def _panel_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _should_auto_open_panel() -> bool:
    raw = os.getenv("CLEANROOM_OPEN_PANEL", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return bool(getattr(sys, "frozen", False))


def _launch_panel_when_ready(host: str, port: int) -> None:
    from app.launcher import open_panel, wait_for_health

    base_url = f"http://{_panel_host(host)}:{port}"
    if wait_for_health(f"{base_url}/api/health/ready"):
        open_panel(base_url)


def main() -> None:
    if len(sys.argv) > 1:
        if sys.argv[1] == "station-worker-process":
            from app.engine.process_worker_child import run_station_worker_process

            raise SystemExit(run_station_worker_process())
        if sys.argv[1] != "rotate-jwt-secret":
            raise SystemExit(f"unknown maintenance command: {sys.argv[1]}")
        if os.getenv("JWT_SECRET_KEY", "").strip():
            raise SystemExit("JWT rotation cannot replace JWT_SECRET_KEY environment input")
        from app.config import get_data_root, get_db_path, get_jwt_secret_path
        from app.security.jwt_rotation import rotate_jwt_secret

        result = rotate_jwt_secret(
            database_path=get_db_path(),
            secret_path=get_jwt_secret_path(),
            recovery_root=get_data_root() / "Recovery" / "jwt-rotation",
        )
        import json

        print(json.dumps(result, sort_keys=True))
        return

    import uvicorn

    from app.main import app

    host = os.getenv("CLEANROOM_HOST", "127.0.0.1")
    port = int(os.getenv("CLEANROOM_PORT", "8100"))
    if _should_auto_open_panel():
        Thread(target=_launch_panel_when_ready, args=(host, port), daemon=True).start()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
