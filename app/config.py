import os
import secrets
import sys
from pathlib import Path


PRODUCT_VENDOR_DIR_NAME = "RadioTEDU"
PRODUCT_DATA_DIR_NAME = "OnAir"
LEGACY_PRODUCT_DATA_DIR_NAMES = ("RadioTEDU OnAir",)
DB_FILENAME = "cleanroom.db"
_EPHEMERAL_JWT_SECRET = secrets.token_urlsafe(48)


def _repo_data_root() -> Path:
    return (Path(__file__).resolve().parents[1] / "data").resolve()


def _configured_path(primary: str, compatibility: str) -> str:
    return os.getenv(primary, "").strip() or os.getenv(compatibility, "").strip()


def _shared_product_root(program_data: str | Path) -> Path:
    base = Path(program_data).expanduser().resolve()
    return (base / PRODUCT_VENDOR_DIR_NAME / PRODUCT_DATA_DIR_NAME).resolve()


def _frozen_data_root() -> Path:
    if getattr(sys, "frozen", False):
        configured = os.getenv("CLEANROOM_DATA_ROOT", "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        program_data = os.getenv("PROGRAMDATA", "").strip()
        if program_data:
            return _shared_product_root(program_data)
        return (Path.home() / ".radiotedu-onair" / "shared").resolve()
    return _repo_data_root()


def get_data_root() -> Path:
    configured = os.getenv("CLEANROOM_DATA_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return _frozen_data_root()
    return _repo_data_root()


def get_user_config_root() -> Path:
    configured = os.getenv("CLEANROOM_USER_CONFIG_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return (Path(local_app_data).expanduser().resolve() / PRODUCT_VENDOR_DIR_NAME / PRODUCT_DATA_DIR_NAME).resolve()
    return (Path.home() / ".radiotedu-onair" / "user").resolve()


def _default_db_path() -> Path:
    return (get_data_root() / DB_FILENAME).resolve()


def get_db_path() -> Path:
    raw = os.getenv("CLEANROOM_DB_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # Keep default DB location stable regardless of process working directory.
    return _default_db_path()


def get_jwt_secret_path() -> Path:
    configured_path = os.getenv("CLEANROOM_JWT_SECRET_FILE", "").strip()
    return (
        Path(configured_path).expanduser().resolve()
        if configured_path
        else (get_user_config_root() / "secrets" / "jwt-signing.key").resolve()
    )


def get_jwt_secret_key() -> str:
    raw = os.getenv("JWT_SECRET_KEY", "").strip()
    if raw:
        return raw
    secret_path = get_jwt_secret_path()
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = secret_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        else:
            generated = secrets.token_urlsafe(48)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(generated)
            return generated
        generated = secrets.token_urlsafe(48)
        secret_path.write_text(generated, encoding="utf-8")
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
        return generated
    except OSError:
        # A read-only development tree still gets an unpredictable, process-local
        # secret instead of a published default. Installed data directories are writable.
        return _EPHEMERAL_JWT_SECRET


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return bool(default)
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def get_public_base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:8100", "http://127.0.0.1:8100"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_trust_proxy_headers() -> bool:
    return _env_flag("TRUST_PROXY_HEADERS", default=False)


def get_security_headers_enabled() -> bool:
    return _env_flag("SECURITY_HEADERS_ENABLED", default=True)


def get_max_upload_bytes() -> int:
    raw = os.getenv("MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return 512 * 1024 * 1024
    try:
        return max(1024, int(raw))
    except ValueError:
        return 512 * 1024 * 1024


def _webrtc_runtime_available() -> bool:
    try:
        import aiortc  # noqa: F401
        return True
    except ImportError:
        return False


def get_webrtc_enabled() -> bool:
    return _env_flag("WEBRTC_ENABLED", default=True) and _webrtc_runtime_available()


def get_webrtc_stun_url() -> str:
    return os.getenv("WEBRTC_STUN_URL", "stun:stun.l.google.com:19302").strip()


def get_webrtc_turn_url() -> str:
    return os.getenv("WEBRTC_TURN_URL", "").strip()


def get_webrtc_turn_username() -> str:
    return os.getenv("WEBRTC_TURN_USERNAME", "").strip()


def get_webrtc_turn_credential() -> str:
    return os.getenv("WEBRTC_TURN_CREDENTIAL", "").strip()


def get_webrtc_ice_servers() -> list[dict]:
    servers = [{"urls": get_webrtc_stun_url()}]
    turn_url = get_webrtc_turn_url()
    if turn_url:
        servers.append({
            "urls": turn_url,
            "username": get_webrtc_turn_username(),
            "credential": get_webrtc_turn_credential(),
        })
    return servers


DB_PATH = get_db_path()
MAX_LOCAL_OUTPUTS = int(os.getenv("MAX_LOCAL_OUTPUTS", "4"))
MAX_OPERATION_LOG_ROWS = int(os.getenv("MAX_OPERATION_LOG_ROWS", "50000"))
MAX_EVENT_ROWS = int(os.getenv("MAX_EVENT_ROWS", "20000"))
