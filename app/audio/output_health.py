"""Shared health checks for bounded PCM queues and Icecast mount writers."""

from __future__ import annotations


_PCM_CHUNK_BYTES = 4096
_PCM_BYTES_PER_SECOND = 48_000 * 2 * 2
_FRESH_WRITE_AGE_SECONDS = 5.0
_SUSTAINED_BACKPRESSURE_SECONDS = 30.0
_QUEUE_SATURATION_RATIO = 0.9


def _queue_is_sustained_near_capacity(
    health: dict,
    *,
    backpressured_key: str,
    age_key: str,
    queued_seconds_key: str,
    queued_chunks_key: str,
    capacity_chunks_key: str,
) -> bool:
    if not bool(health.get(backpressured_key)):
        return False
    try:
        age = float(health.get(age_key))
    except (TypeError, ValueError):
        # An asserted pressure flag without an age cannot be certified as
        # transient. Fail closed so repair does not wait forever on ambiguity.
        return True
    if age < _SUSTAINED_BACKPRESSURE_SECONDS:
        return False
    try:
        capacity_chunks = float(health.get(capacity_chunks_key))
        if capacity_chunks <= 0.0:
            return True
        capacity_seconds = (
            capacity_chunks * _PCM_CHUNK_BYTES / _PCM_BYTES_PER_SECOND
        )
        queued_seconds = health.get(queued_seconds_key)
        if queued_seconds is not None:
            queued = float(queued_seconds)
            return queued >= capacity_seconds * _QUEUE_SATURATION_RATIO
        queued_chunks = float(health.get(queued_chunks_key))
        return queued_chunks >= capacity_chunks * _QUEUE_SATURATION_RATIO
    except (TypeError, ValueError):
        return True


def icecast_mount_has_sustained_saturation(health: dict | None) -> bool:
    """Return true when an output FIFO remains nearly full for 30 seconds."""

    mount = dict(health or {})
    return bool(
        _queue_is_sustained_near_capacity(
            mount,
            backpressured_key="writer_backpressured",
            age_key="writer_backpressure_age_seconds",
            queued_seconds_key="queued_pcm_seconds",
            queued_chunks_key="queued_pcm_chunks",
            capacity_chunks_key="pcm_queue_capacity_chunks",
        )
        or _queue_is_sustained_near_capacity(
            mount,
            backpressured_key="pcm_dispatch_backpressured",
            age_key="pcm_dispatch_backpressure_age_seconds",
            queued_seconds_key="queued_dispatch_pcm_seconds",
            queued_chunks_key="queued_dispatch_pcm_chunks",
            capacity_chunks_key="pcm_dispatch_queue_capacity_chunks",
        )
    )


def icecast_mount_transport_is_healthy(
    health: dict | None,
    *,
    require_mount_healthy: bool = True,
    require_process: bool = True,
    sink_running: bool | None = None,
    require_network_writer: bool = True,
) -> bool:
    """Check a mount's writer, freshness, and sustained queue pressure."""

    mount = dict(health or {})
    if not mount:
        return False
    mount_health = mount.get("mount_healthy")
    if mount_health is False:
        return False
    if require_mount_healthy and mount_health is not True:
        return False

    process_evidence = []
    if sink_running is not None:
        process_evidence.append(bool(sink_running))
    if "process_running" in mount:
        process_evidence.append(bool(mount.get("process_running")))
    if (require_process and not process_evidence) or (
        process_evidence and not all(process_evidence)
    ):
        return False
    if not bool(mount.get("writer_running")):
        return False
    if require_network_writer and mount.get("network_writer_running") is not True:
        return False
    if "network_writer_running" in mount and not bool(
        mount.get("network_writer_running")
    ):
        return False
    if bool(mount.get("writer_failed")) or bool(mount.get("network_failed")):
        return False

    try:
        write_age = float(mount.get("last_write_age_seconds"))
    except (TypeError, ValueError):
        return False
    if write_age > _FRESH_WRITE_AGE_SECONDS:
        return False

    network_write_age = mount.get("last_network_write_age_seconds")
    if require_network_writer or network_write_age is not None:
        try:
            if float(network_write_age) > _FRESH_WRITE_AGE_SECONDS:
                return False
        except (TypeError, ValueError):
            return False

    return not icecast_mount_has_sustained_saturation(mount)
