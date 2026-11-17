#!/usr/bin/env python3
"""
scripts/heartbeat_watchdog.py — N3 heartbeat watchdog per trader.

Scheduled by the superbot-heartbeat-watchdog workflows (every 5 min).
Reads the trader's last reported_state heartbeat from Supabase bot_state and
alerts when it is stale. Supabase being unreachable is NOT an alert (the sync
lag workflow covers that); it yields a skipped verdict instead.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/superbot")  # when run by n8n from elsewhere

TRADER_KEYS = {"kalshi": "reported_state:kalshi", "poly_us": "reported_state:poly_us"}


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def check(trader: str, max_age_s: float, now: datetime | None = None,
          get_bot_state=None) -> dict:
    """Return {"alert": bool, ...} for one trader's heartbeat freshness."""
    now = now or datetime.now(timezone.utc)
    key = TRADER_KEYS[trader]
    if get_bot_state is None:
        try:
            from supabase_client import get_supabase_client, is_enabled
        except Exception:
            get_bot_state = None
        else:
            if not is_enabled():
                return {"alert": False, "skipped": "supabase disabled", "trader": trader}
            sb = get_supabase_client()
            get_bot_state = sb.get_bot_state if sb else None
    if get_bot_state is None:
        return {"alert": False, "skipped": "supabase unavailable", "trader": trader}
    try:
        payload = get_bot_state(key)
    except Exception as e:
        return {"alert": False, "skipped": f"read failed: {e}", "trader": trader}
    if not isinstance(payload, dict):
        return {"alert": True, "trader": trader, "reason": "no heartbeat stored",
                "age_s": None}
    ts = _parse_iso(str(payload.get("ts") or ""))
    if ts is None:
        return {"alert": True, "trader": trader, "reason": "heartbeat ts unparseable",
                "age_s": None}
    age_s = (now - ts).total_seconds()
    return {"alert": age_s > max_age_s, "trader": trader,
            "age_s": round(age_s, 1), "max_age_s": max_age_s,
            "state": payload.get("state")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, choices=sorted(TRADER_KEYS))
    ap.add_argument("--max-age-s", type=float, default=1200.0)
    a = ap.parse_args(argv)
    print(json.dumps(check(a.source, a.max_age_s), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
