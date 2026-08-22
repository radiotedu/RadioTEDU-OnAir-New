from __future__ import annotations


def _truthy(value, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off", ""}:
        return False
    return bool(default)


def icecast_metadata_outputs(cfg) -> list[dict]:
    """Select outputs allowed to receive public now-playing metadata."""
    outputs = [
        {
            "icecast_host": cfg.icecast_host,
            "icecast_port": cfg.icecast_port,
            "icecast_mount": cfg.icecast_mount,
            "icecast_user": cfg.icecast_user,
            "icecast_password": cfg.icecast_password,
            "icecast_tls_enabled": cfg.icecast_tls_enabled,
            "metadata_suppressed": _truthy(
                getattr(cfg, "metadata_suppressed", False), False
            ),
        }
    ]
    outputs.extend(
        dict(output)
        for output in getattr(cfg, "extra_icecast_outputs", ()) or ()
        if _truthy(dict(output).get("enabled"), True)
    )
    return [
        output
        for output in outputs
        if not _truthy(output.get("metadata_suppressed"), False)
    ]
