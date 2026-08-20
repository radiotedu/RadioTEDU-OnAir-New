from datetime import datetime, timedelta, timezone
from secrets import token_hex

from jose import jwt

from app.config import get_jwt_secret_key

ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 30
JWT_ALGORITHM = "HS256"


def _encode(payload: dict) -> str:
    return jwt.encode(payload, get_jwt_secret_key(), algorithm=JWT_ALGORITHM)


def create_access_token(user_id: int, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(int(user_id)),
        "role": str(role),
        "type": "access",
        "jti": token_hex(8),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
    }
    return _encode(payload)


def create_refresh_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(int(user_id)),
        "type": "refresh",
        "jti": token_hex(16),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)).timestamp()),
    }
    return _encode(payload)


def decode_token(token: str) -> dict:
    return dict(
        jwt.decode(
            str(token),
            get_jwt_secret_key(),
            algorithms=[JWT_ALGORITHM],
        )
    )
