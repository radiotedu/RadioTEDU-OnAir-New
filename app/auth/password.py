from passlib.context import CryptContext


_PWD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    return _PWD_CONTEXT.hash(str(plain))


def verify_password(plain: str, hashed: str) -> bool:
    return _PWD_CONTEXT.verify(str(plain), str(hashed))
