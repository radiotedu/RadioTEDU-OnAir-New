"""Retry unattended cold starts without disturbing already-started stations."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any


_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
_log = logging.getLogger(__name__)


def recover_station_startup(
    connect: Callable[[], Any],
    start: Callable[[Any, set[int] | None, threading.Event], Iterable[int] | None],
    stop_event: threading.Event,
) -> None:
    """Retry failed starts until recovered or shutdown, using fresh connections.

    None selects the initial roster; subsequent successful passes return only
    the stations still needing recovery. An empty result ends this boot task,
    so intentionally stopped stations are not periodically resurrected.
    """
    pending: set[int] | None = None
    failures = 0
    while not stop_event.is_set():
        conn = None
        try:
            conn = connect()
            if stop_event.is_set():
                return
            pending = set(start(conn, pending, stop_event) or ())
            if not pending:
                return
        except Exception:
            # A busy database or unavailable media volume at Windows boot must
            # not leave the API healthy but the authorized stations stopped.
            _log.exception("Station startup is temporarily unavailable; will retry")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    _log.warning("Could not close startup connection", exc_info=True)

        delay = _RETRY_DELAYS[min(failures, len(_RETRY_DELAYS) - 1)]
        failures += 1
        _log.warning(
            "Station startup retry scheduled in %.1fs; pending=%s",
            delay,
            sorted(pending) if pending is not None else "initial roster",
        )
        if stop_event.wait(delay):
            return
