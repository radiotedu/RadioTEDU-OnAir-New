import pytest
from app.security.portable_credentials import PortableCredentialProvider


def test_portable_vault_round_trip_and_ciphertext_does_not_contain_secret(tmp_path):
    provider = PortableCredentialProvider(tmp_path / 'secrets' / 'credential.key')
    encrypted = provider.protect(b'test-only-secret')
    assert b'test-only-secret' not in encrypted
    assert provider.unprotect(encrypted) == b'test-only-secret'
    restored = PortableCredentialProvider(tmp_path / 'secrets' / 'credential.key')
    assert restored.unprotect(encrypted) == b'test-only-secret'


def test_wrong_key_and_missing_key_never_generate_a_replacement_on_read(tmp_path):
    provider = PortableCredentialProvider(tmp_path / 'one.key')
    token = provider.protect(b'test-only-secret')
    wrong = PortableCredentialProvider(tmp_path / 'two.key')
    wrong.protect(b'other-test-secret')
    with pytest.raises(ValueError):
        wrong.unprotect(token)
    missing = PortableCredentialProvider(tmp_path / 'absent.key')
    with pytest.raises(FileNotFoundError):
        missing.unprotect(token)
    assert not (tmp_path / 'absent.key').exists()
