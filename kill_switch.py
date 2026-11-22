"""
kill_switch.py - Armed/halted circuit breaker for the trading loop.

Extracted from the clayve-one/kalshi kill switch pattern:

- States: ARMED (orders allowed) | HALTED (no new orders).
- trip(): a monitor or a human flips it to HALTED, recording reason,
  tripped_by, metric, value, limit and tripped_at.
- check(): called every cycle; returns False when halted so the cycle
  skips order placement but keeps heart-beating (monitoring stays alive).
- reset(): human-confirmed (confirm=True) and rate-limited to one reset
  per 5 minutes per scope, so a stuck loop cannot hammer the re-arm path.

Usage
-----
    ks = KillSwitch(scope="btc-15m")

    # every cycle - the engine keeps heart-beating while halted:
    if not ks.check():
        log.warning("halted, skipping orders: %s", ks.status()["reason"])
        continue

    # a monitor trips it:
    ks.trip("atr hurdle", "monitor", metric="atr_cents", value=12, limit=15)

    # a human re-arms (rate-limited, one per 5 minutes per scope):
    ks.reset("false positive confirmed", reset_by="operator", confirm=True)
"""

import threading
import time
from typing import Optional

ARMED = "armed"
HALTED = "halted"

# Default rate-limit window for human resets: one per 5 minutes per scope.
RESET_MIN_INTERVAL_SECONDS = 5 * 60


class KillSwitch:
    """Armed/halted circuit breaker with a confirmed, rate-limited reset.

    State transitions are guarded by a lock so the cycle loop, a monitor
    thread and a webhook handler can all call check/trip/reset safely.
    """

    def __init__(self, scope: str = "default",
                 reset_interval_seconds: float = RESET_MIN_INTERVAL_SECONDS) -> None:
        self.scope = scope
        self.reset_interval_seconds = float(reset_interval_seconds)
        self._lock = threading.Lock()
        self.state = ARMED
        self.reason: Optional[str] = None
        self.tripped_by: Optional[str] = None
        self.metric: Optional[str] = None
        self.value = None
        self.limit = None
        self.tripped_at: Optional[float] = None
        self.reset_at: Optional[float] = None
        self.reset_by: Optional[str] = None
        self.reset_reason: Optional[str] = None
        self._last_reset_monotonic: Optional[float] = None

    # -- queries -----------------------------------------------------------

    def is_halted(self) -> bool:
        """True while HALTED; the cycle must not place orders."""
        with self._lock:
            return self.state == HALTED

    def check(self) -> bool:
        """Called every cycle. False when halted (no orders), True when armed.

        Only order placement is gated; monitoring keeps heart-beating while
        the switch is halted.
        """
        with self._lock:
            return self.state == ARMED

    def status(self) -> dict:
        """Snapshot of every field, safe to log or return over HTTP."""
        with self._lock:
            return {
                "scope": self.scope,
                "state": self.state,
                "halted": self.state == HALTED,
                "reason": self.reason,
                "tripped_by": self.tripped_by,
                "metric": self.metric,
                "value": self.value,
                "limit": self.limit,
                "tripped_at": self.tripped_at,
                "reset_at": self.reset_at,
                "reset_by": self.reset_by,
                "reset_reason": self.reset_reason,
            }

    # -- transitions -------------------------------------------------------

    def trip(self, reason: str, tripped_by: str,
             metric: Optional[str] = None, value=None, limit=None) -> None:
        """Flip to HALTED and record why.

        ``tripped_by`` is "monitor" or "human". ``metric``/``value``/``limit``
        capture the gate that fired (e.g. atr_cents=12 against limit 15).
        Re-tripping while halted overwrites the trip record with the latest.
        """
        with self._lock:
            self.state = HALTED
            self.reason = reason
            self.tripped_by = tripped_by
            self.metric = metric
            self.value = value
            self.limit = limit
            self.tripped_at = time.time()

    def reset(self, reason: str, reset_by: str, confirm: bool = False) -> None:
        """Re-arm to ARMED.

        Requires ``confirm=True`` (human confirmation) and is rate-limited
        to one reset per ``reset_interval_seconds`` per scope. Raises
        ValueError if either precondition fails; the switch is left untouched
        on a rejected reset.
        """
        if not confirm:
            raise ValueError(
                "kill switch reset requires human confirmation: pass confirm=True"
            )
        now = time.monotonic()
        with self._lock:
            if self._last_reset_monotonic is not None:
                elapsed = now - self._last_reset_monotonic
                if elapsed < self.reset_interval_seconds:
                    raise ValueError(
                        "kill switch reset rate-limited: %.0fs since last reset, "
                        "need %.0fs (scope=%r)" % (
                            elapsed, self.reset_interval_seconds, self.scope)
                    )
            self.state = ARMED
            self.reset_at = time.time()
            self.reset_by = reset_by
            self.reset_reason = reason
            self._last_reset_monotonic = now


if __name__ == "__main__":  # pragma: no cover - quick self-check
    ks = KillSwitch(reset_interval_seconds=0.1)
    assert ks.check() is True
    ks.trip("self-check", "human", metric="m", value=1, limit=2)
    assert ks.is_halted() and ks.check() is False
    try:
        ks.reset("r", "me")
    except ValueError:
        pass
    else:
        raise AssertionError("expected confirmation error")
    ks.reset("r", "me", confirm=True)
    assert ks.check() is True
    print("kill switch self-check OK")
