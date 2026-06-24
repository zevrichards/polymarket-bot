"""Shared repeating-scan loop for all three bots.

Each bot's run_once() is self-contained (loads config, does one scan,
returns). This just calls it on an interval and shuts down cleanly on
Ctrl+C / SIGTERM instead of dying mid-scan.
"""
from __future__ import annotations

import logging
import signal
import time
from typing import Callable

log = logging.getLogger(__name__)


class _Stop:
    requested = False


def run_forever(run_once: Callable[[], list], interval_seconds: int, label: str) -> None:
    stop = _Stop()

    def _handle_signal(signum, frame):
        log.info("%s: received shutdown signal, finishing current scan then exiting", label)
        stop.requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        pass  # SIGTERM not available on this platform

    log.info("%s: starting scan loop every %ds (Ctrl+C to stop)", label, interval_seconds)
    while not stop.requested:
        start = time.monotonic()
        try:
            run_once()
        except Exception:
            log.exception("%s: scan failed, will retry next interval", label)

        elapsed = time.monotonic() - start
        remaining = max(0.0, interval_seconds - elapsed)
        slept = 0.0
        while slept < remaining and not stop.requested:
            chunk = min(1.0, remaining - slept)
            time.sleep(chunk)
            slept += chunk

    log.info("%s: stopped", label)
