from app.auth.brute_force import BruteForceProtection


def test_brute_force_blocks_after_five_failures():
    protection = BruteForceProtection()
    identifier = "admin"

    for _ in range(5):
        protection.record_failure(identifier)

    assert protection.check_allowed(identifier) is False
