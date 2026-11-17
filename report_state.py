"""B19 — per-cycle operator visibility state report.

One classification per cycle per trader: trading / idle-no-signal / halted /
cooldown / degraded / stopped. Emitted to the superbot-monitor n8n webhook and
bot_state ONLY on state change, plus a heartbeat (default 15 min) so a quiet bot
is still visible. Every emit is best-effort: a broken webhook or Supabase write
can never crash or slow the trading cycle.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

VALID_STATES = ("trading", "idle-no-signal", "halted", "cooldown", "degraded", "stopped")

DEFAULT_HEARTBEAT_SECONDS = 900.0  # 15 min = 60 cycles at the 15s cadence

# Lazy singletons; tests monkeypatch these attributes with fakes.
_n8n_client = None
_state_db = None


def _get_n8n():
    global _n8n_client
    if _n8n_client is None:
        try:
            from n8n_client import get_client
            _n8n_client = get_client()
        except Exception as e:
            log.debug("report_state: n8n client unavailable (%s)", e)
    return _n8n_client


def _get_state_db():
    """D1: heartbeat state goes to the local DB; the sync thread mirrors it.

    Writing remote Supabase inline put a synchronous HTTP call in the trading
    cycle; the local write keeps the same key the sync path upserts by.
    """
    global _state_db
    if _state_db is None:
        try:
            from local_db import get_local_db
            _state_db = get_local_db()
        except Exception as e:
            log.debug("report_state: local db unavailable (%s)", e)
    return _state_db


def classify(*, halted: bool = False, cooldown: bool = False,
             degraded: bool = False, traded: bool = False,
             stopped: bool = False) -> str:
    """Priority: halted > cooldown > degraded > stopped > trading > idle."""
    if halted:
        return "halted"
    if cooldown:
        return "cooldown"
    if degraded:
        return "degraded"
    if stopped:
        return "stopped"
    if traded:
        return "trading"
    return "idle-no-signal"


class StateReporter:
    """Emit once per state change, plus a heartbeat while the state persists."""

    def __init__(self, trader: str, heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
                 bot_state_key: str = "reported_state"):
        self.trader = trader
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.key = bot_state_key
        self._last_state: str | None = None
        self._last_emit: float = 0.0
        log.info("report_state[%s]: heartbeat every %.0fs", trader, self.heartbeat_seconds)

    def _now(self) -> float:
        return time.time()

    def report(self, new_state: str, **fields) -> None:
        try:
            self._report(new_state, fields)
        except Exception as e:
            # Never crash or slow the trading cycle on observability code.
            log.warning("report_state[%s] failed: %s", self.trader, e)

    def _report(self, new_state: str, fields: dict) -> None:
        if new_state not in VALID_STATES:
            log.warning("report_state[%s]: unknown state %r — reporting degraded", self.trader, new_state)
            new_state = "degraded"
        now = self._now()
        changed = new_state != self._last_state
        heartbeat_due = (now - self._last_emit) >= self.heartbeat_seconds
        if not changed and not heartbeat_due:
            return
        payload = {
            "state": new_state,
            "trader": self.trader,
            "ts": datetime.now(timezone.utc).isoformat(),
            "heartbeat": not changed,
            **fields,
        }
        n8n = _get_n8n()
        if n8n is not None:
            try:
                n8n.run_monitor(payload)
            except Exception as e:
                log.debug("report_state[%s]: monitor webhook failed: %s", self.trader, e)
        sdb = _get_state_db()
        if sdb is not None:
            try:
                sdb.set_bot_state(self.key, payload)
            except Exception as e:
                log.debug("report_state[%s]: bot_state write failed: %s", self.trader, e)
        self._last_state = new_state
        self._last_emit = now
        log.info("report_state[%s]: %s%s", self.trader, new_state,
                 " (heartbeat)" if not changed else "")
