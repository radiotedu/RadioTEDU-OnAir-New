from app.auth.password import hash_password, verify_password


def test_hash_password_uses_bcrypt_and_verify_round_trip():
    hashed = hash_password("changeme123")

    assert hashed != "changeme123"
    assert verify_password("changeme123", hashed) is True
    assert verify_password("wrong-pass", hashed) is False
