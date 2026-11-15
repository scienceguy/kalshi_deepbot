"""
resilience.py — Helpers to keep the trading loop alive when errors occur.

From kalshi_live_bot.
"""
import logging
import os
from dataclasses import dataclass, field

import config
from kalshi_client import KalshiAuthError

log = logging.getLogger(__name__)


@dataclass
class ResilienceSettings:
    base_backoff: float = field(default_factory=lambda: config.ERROR_BACKOFF_SECONDS)
    max_backoff: float = field(default_factory=lambda: config.ERROR_BACKOFF_MAX_SECONDS)
    rebuild_threshold: int = field(default_factory=lambda: config.CLIENT_REBUILD_THRESHOLD)
    max_rebuilds: int = field(default_factory=lambda: config.CLIENT_MAX_REBUILDS if hasattr(config, 'CLIENT_MAX_REBUILDS') else 5)


class CycleSupervisor:
    """
    Tracks consecutive cycle errors and decides when to back off or rebuild the
    Kalshi client. Designed to be lightweight and side-effect free.
    """

    def __init__(self, settings: ResilienceSettings | None = None):
        self.settings = settings or ResilienceSettings()
        self.errors_in_a_row = 0
        self.last_error_type: str | None = None
        self.last_error_message: str | None = None
        self.last_error: Exception | None = None
        self.error_types: dict[str, int] = {}
        self.rebuilds = 0

    def mark_success(self):
        self.errors_in_a_row = 0
        if self.rebuilds > 0:
            self._consecutive_successes = getattr(self, "_consecutive_successes", 0) + 1
            if self._consecutive_successes >= 10:
                self.rebuilds = max(0, self.rebuilds - 1)
                self._consecutive_successes = 0
        else:
            self._consecutive_successes = 0

    def mark_failure(self, exc: Exception) -> float:
        self.errors_in_a_row += 1
        self.last_error_type = type(exc).__name__
        self.last_error_message = str(exc)
        self.last_error = exc
        self.error_types[self.last_error_type] = self.error_types.get(self.last_error_type, 0) + 1
        # Exponential backoff: 1st failure → base, 2nd → 2×base, 3rd → 4×base, …
        # Capped at max_backoff so retries never grow unbounded.
        # First failure: retry immediately (no backoff). Subsequent failures
        # use exponential backoff starting at base_backoff.
        if self.errors_in_a_row == 1:
            delay = 0.0
        else:
            delay = min(
                self.settings.base_backoff * (2 ** (self.errors_in_a_row - 2)),
                self.settings.max_backoff,
            )
        log.warning(
            "Cycle error (%d in a row): %s — backing off %.1fs",
            self.errors_in_a_row, exc, delay,
        )
        return delay

    def is_auth_error(self) -> bool:
        """Return True if the last recorded error was authentication-related.

        Structured checks only: a KalshiAuthError, or any exception carrying
        an HTTP 401/403 status_code. The legacy substring fallback keeps only
        'auth'/'permission' — numeric substrings like '401'/'403' matched
        order ids such as '...3f401a...' and stopped the loop on a healthy
        key (B8). A plain HTTPError (429 rate limit, 5xx, 404...) is NOT an
        auth error — it must trigger backoff/retry, not kill the bot.
        """
        exc = self.last_error
        if exc is None:
            return False
        if isinstance(exc, KalshiAuthError):
            return True
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return True
        haystack = f"{type(exc).__name__} {exc}".lower()
        return "auth" in haystack or "permission" in haystack

    def should_rebuild_client(self) -> bool:
        return (self.errors_in_a_row >= max(1, self.settings.rebuild_threshold)
                and self.rebuilds < self.settings.max_rebuilds)

    def after_rebuild(self):
        # Reset errors after a successful rebuild to avoid infinite rebuilds.
        # Increment rebuild counter; if we hit max_rebuilds, should_rebuild_client()
        # will return False and the loop will stop instead of rebuilding forever.
        self.errors_in_a_row = 0
        self.last_error_type = None
        self.last_error_message = None
        self.last_error = None
        self.rebuilds += 1


def systemd_notify(state: str = "WATCHDOG=1") -> bool:
    """Send notification to systemd via $NOTIFY_SOCKET without external dependencies."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return False
    try:
        import socket
        if sock_path.startswith("@"):
            sock_path = "\0" + sock_path[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(sock_path)
            sock.sendall(state.encode())
        return True
    except Exception:
        return False
