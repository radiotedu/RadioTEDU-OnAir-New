"""Bounded server-side evidence for configured public stream endpoints.

The returned object is deliberately free of origins, paths, credentials, response
bodies, and exception text.  It is intended for the local Health Wall only.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import time
from copy import deepcopy
from typing import Any
from http.client import HTTPException, HTTPSConnection
from urllib.parse import urljoin, urlsplit

from app.config import get_public_base_url


_PROBE_TTL_SECONDS = 30.0
_NETWORK_TIMEOUT_SECONDS = 2.0
_DNS_TIMEOUT_SECONDS = 2.0
_AUDIO_BYTE_LIMIT = 8 * 1024
_MAX_RESOLVED_ADDRESSES = 2


def _is_globally_routable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def _safe_origin(value: object) -> str | None:
    """Accept only a credential-free HTTPS public origin for server probing."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return value.strip().rstrip("/")
    if not _is_globally_routable(address):
        return None
    return value.strip().rstrip("/")


def _configured_origins(system_settings: dict[str, Any] | None) -> list[str]:
    values = [get_public_base_url()]
    if isinstance(system_settings, dict):
        values.append(system_settings.get("stream_public_base_url"))
    origins: list[str] = []
    for value in values:
        origin = _safe_origin(value)
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def _unknown_endpoint(*, configured: bool = False) -> dict[str, Any]:
    return {
        "state": "unknown",
        "configured": configured,
        "dns": "unknown",
        "tls": "unknown",
        "http": "unknown",
        "audio_bytes": "unknown",
        "decode": "unknown",
        "decode_observed_at": None,
    }


def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve once with a deadline and reject a host containing any non-global IP."""
    result: list[object] = []

    def resolve() -> None:
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except (OSError, ValueError):
            result.append(None)

    try:
        worker = threading.Thread(target=resolve, name="public-stream-dns", daemon=True)
        worker.start()
        worker.join(_DNS_TIMEOUT_SECONDS)
    except RuntimeError:
        return ()
    if worker.is_alive() or not result or not isinstance(result[0], list):
        return ()

    addresses: list[str] = []
    for item in result[0]:
        try:
            address = str(item[4][0])
            parsed = ipaddress.ip_address(address)
        except (IndexError, TypeError, ValueError):
            return ()
        if not _is_globally_routable(parsed):
            return ()
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses[:_MAX_RESOLVED_ADDRESSES])


def _tls_reachable(host: str, port: int, address: str) -> bool:
    try:
        with socket.create_connection((address, port), timeout=_NETWORK_TIMEOUT_SECONDS) as raw:
            context = ssl.create_default_context()
            with context.wrap_socket(raw, server_hostname=host):
                return True
    except (OSError, ValueError):
        return False


class _PinnedHTTPSConnection(HTTPSConnection):
    """HTTPS connection that never re-resolves the checked public hostname."""

    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(host, port=port, timeout=_NETWORK_TIMEOUT_SECONDS, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _fetch_pinned_audio(url: str, address: str) -> tuple[int, str, bytes] | None:
    parsed = urlsplit(url)
    host = str(parsed.hostname or "")
    port = int(parsed.port or 443)
    target = parsed.path or "/"
    connection = _PinnedHTTPSConnection(host, port, address)
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Icy-MetaData": "0",
                "Range": f"bytes=0-{_AUDIO_BYTE_LIMIT - 1}",
                "User-Agent": "RadioTEDU-OnAir-public-evidence/1",
                "Accept": "audio/*, application/ogg;q=0.9",
            },
        )
        response = connection.getresponse()
        return int(response.status), str(response.getheader("Content-Type") or ""), response.read(_AUDIO_BYTE_LIMIT)
    except (HTTPException, OSError, ValueError):
        return None
    finally:
        connection.close()


def _is_audio_evidence(content_type: str, payload: bytes) -> bool:
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    trusted_type = normalized_type.startswith("audio/") or normalized_type in {
        "application/ogg",
        "application/x-ogg",
    }
    if not trusted_type or not payload:
        return False
    return (
        payload.startswith((b"OggS", b"fLaC", b"ID3"))
        or (payload.startswith(b"RIFF") and payload[8:12] == b"WAVE")
        or (len(payload) >= 2 and payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0)
    )


def _probe_endpoint(
    url: str,
    *,
    now_epoch: int,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    host = str(parsed.hostname or "")
    port = int(parsed.port or 443)
    result = _unknown_endpoint(configured=True)
    addresses = _resolve_public_addresses(host, port)
    if not addresses:
        result.update({"state": "unavailable", "dns": "unavailable"})
        return result
    result["dns"] = "reachable"
    for address in addresses:
        if not _tls_reachable(host, port, address):
            continue
        result["tls"] = "reachable"
        fetched = _fetch_pinned_audio(url, address)
        if fetched is None:
            continue
        status, content_type, audio = fetched
        if status not in {200, 206}:
            result.update({"state": "unavailable", "http": "unavailable"})
            return result
        result["http"] = "reachable"
        if not _is_audio_evidence(content_type, audio):
            result.update({"state": "degraded", "audio_bytes": "unavailable"})
            return result
        result.update({"state": "healthy", "audio_bytes": "present"})
        return result

    result.update({"state": "unavailable", "tls": "unavailable"})
    return result


def _aggregate_endpoint(items: list[dict[str, Any]], origin_count: int) -> dict[str, Any]:
    if not items:
        return _unknown_endpoint(configured=False)
    states = {str(item.get("state") or "unknown") for item in items}
    state = (
        "healthy"
        if states == {"healthy"}
        else "degraded"
        if states & {"healthy", "degraded"}
        else "unavailable"
    )
    fields = ("dns", "tls", "http", "audio_bytes", "decode")
    result = {
        "state": state,
        "configured": True,
        "origin_count": origin_count,
    }
    for field in fields:
        values = {str(item.get(field) or "unknown") for item in items}
        result[field] = "healthy" if values == {"healthy"} else "reachable" if values == {"reachable"} else "present" if values == {"present"} else "degraded" if values & {"healthy", "reachable", "present"} else "unavailable" if values == {"unavailable"} else "unknown"
    timestamps = [item.get("decode_observed_at") for item in items if isinstance(item.get("decode_observed_at"), int)]
    result["decode_observed_at"] = min(timestamps) if timestamps else None
    return result


class PublicStreamEvidenceService:
    """Single-flight evidence collection; requests only read cached results."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: tuple[float, tuple[str, ...], dict[str, Any]] | None = None
        self._refreshing = False

    def snapshot(self, system_settings: dict[str, Any] | None) -> dict[str, Any]:
        origins = _configured_origins(system_settings)
        origin_key = tuple(origins)
        now = time.monotonic()
        with self._lock:
            cache_matches = bool(self._cache and self._cache[1] == origin_key)
            cached = deepcopy(self._cache[2]) if cache_matches else None
            if self._cache and not cache_matches:
                self._cache = None
            stale = not cache_matches or now - self._cache[0] >= _PROBE_TTL_SECONDS
            if origins and stale and not self._refreshing:
                self._refreshing = True
                try:
                    threading.Thread(
                        target=self._refresh,
                        args=(origin_key,),
                        name="health-wall-public-stream-probe",
                        daemon=True,
                    ).start()
                except RuntimeError:
                    self._refreshing = False
        if cached is not None:
            return cached
        return {
            "state": "probing" if origins else "unknown",
            "configured": bool(origins),
            "observed_at": None,
            "streams": {"ai": _unknown_endpoint(configured=bool(origins)), "event": _unknown_endpoint(configured=bool(origins))},
        }

    def _refresh(self, origins: tuple[str, ...]) -> None:
        now_epoch = int(time.time())
        try:
            stream_items: dict[str, list[dict[str, Any]]] = {"ai": [], "event": []}
            for origin in origins:
                for name in ("ai", "event"):
                    url = urljoin(f"{origin}/", name)
                    stream_items[name].append(_probe_endpoint(url, now_epoch=now_epoch))
            streams = {name: _aggregate_endpoint(items, len(origins)) for name, items in stream_items.items()}
            states = {item["state"] for item in streams.values()}
            snapshot = {
                "state": "unknown" if not origins else "healthy" if states == {"healthy"} else "degraded" if "healthy" in states else "unavailable",
                "configured": bool(origins),
                "observed_at": now_epoch,
                "streams": streams,
            }
            with self._lock:
                self._cache = (time.monotonic(), origins, snapshot)
        finally:
            with self._lock:
                self._refreshing = False


_service: PublicStreamEvidenceService | None = None


def get_public_stream_evidence_service() -> PublicStreamEvidenceService:
    global _service
    if _service is None:
        _service = PublicStreamEvidenceService()
    return _service
