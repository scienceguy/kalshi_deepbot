"""
emit.py — N3: typed event emission for both traders through the N2 client.

One envelope (events.make_event), one emission path (n8n_client outbox).
Delivery is durable: critical/normal events persist to local_db.event_outbox
and are retried with backoff by the sender threads; telemetry coalesces.

Webhook routing (lane -> path, matching n8n/workflows/ JSONs):
    critical  -> superbot-critical-alert  (HMAC-verified, routes the page)
    normal    -> superbot-trade           (order/fill/settlement processing)
    telemetry -> superbot-monitor         (ack-only sink)
"""

import logging
import time
from typing import Optional

import config
from events import delivery_class, make_event

log = logging.getLogger(__name__)

LANE_WEBHOOK_PATH = {
    "critical": "superbot-critical-alert",
    "normal": "superbot-trade",
    "telemetry": "superbot-monitor",
}

SOURCES = ("kalshi_trader", "poly_us_trader")

# sync.lag is emitted only when the mirror is actually behind (D3 measures it).
SYNC_LAG_MAX_AGE_S = 600.0
SYNC_LAG_MAX_ROWS = 1000


def emit_event(source: str, event_type: str, payload: dict, *,
               key: Optional[str] = None,
               severity: Optional[str] = None,
               mode: Optional[str] = None,
               agent_id: Optional[str] = None) -> object:
    """Build the N3 envelope and hand it to the durable outbox.

    `key` makes the event_id deterministic so a replayed settlement/fill
    dedupes downstream. Never raises: an event is telemetry-grade — the
    trading cycle must not fail because n8n is down (D1).
    """
    ev = make_event(source, mode or ("dry" if config.DRY_RUN else "live"),
                    event_type, payload, key=key, severity=severity,
                    agent_id=agent_id)
    path = LANE_WEBHOOK_PATH[delivery_class(event_type)]
    try:
        from n8n_client import get_client
        get_client().execute_webhook(path, ev.to_dict(), fire_and_forget=True)
    except Exception as e:
        log.warning("emit_event(%s) failed (event still durable?): %s", event_type, e)
    return ev


def emit_sync_lag(source: str, *, unsynced_count: int, oldest_age_s: float,
                  mode: Optional[str] = None) -> object:
    """Emit sync.lag when the Supabase mirror is behind; no-op otherwise."""
    if unsynced_count <= 0 and oldest_age_s <= 0:
        return None
    if oldest_age_s < SYNC_LAG_MAX_AGE_S and unsynced_count < SYNC_LAG_MAX_ROWS:
        return None
    return emit_event(
        source, "sync.lag",
        {"unsynced_count": int(unsynced_count),
         "oldest_unsynced_age_s": float(oldest_age_s)},
        key=f"sync.lag:{int(time.time() // 300)}",  # at most once per 5 min
        severity="warning", mode=mode,
    )
