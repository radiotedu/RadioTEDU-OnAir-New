from scripts import prune_fast_audio_cache as cli


def test_cleanup_cli_uses_site_policy_by_default(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["prune_fast_audio_cache.py"])
    assert cli._arguments().max_bytes is None


def test_cleanup_cli_preserves_explicit_operator_budget(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["prune_fast_audio_cache.py", "--max-bytes", "8589934592"])
    assert cli._arguments().max_bytes == 8589934592
