"""Issue #15 - session_pnl reconciliation across local state, n8n and Supabase.

Four places hold a P&L number and nothing compared them:

- local: the pnl_ledger table (idempotent, authoritative) and the in-memory
  BotState.session_pnl, mirrored to local bot_state;
- n8n: settlement webhooks PATCH orders.pnl_dollars in Supabase - the
  "n8n-reported" record of realized P&L that flowed through the trade lane;
- Supabase: bot_state.session_pnl, the mirror of the local running total
  pushed by the background sync thread.

A quarantined sync row, a lost settlement webhook or a replay double-credit
silently diverged them. reconcile_session_pnl() compares all of them and:

- corrects the in-memory state from the authoritative reference (paper
  bankroll in dry-run, pnl_ledger sum in live) without touching the risk
  circuit breaker accounting;
- re-writes local bot_state through the batched path so the sync thread
  re-pushes a corrected mirror (never an inline remote write - D1);
- emits reconcile.mismatch (critical lane) when the n8n/Supabase record
  diverges beyond tolerance, e.g. a lost settlement webhook or sync lag.

Never raises and never blocks the trading cycle: remote reads are
best-effort, throttled by maybe_reconcile_session_pnl(), and only the
correction path writes anything.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class PnlReconciliation:
    """Result of one reconciliation pass (all money values in dollars)."""

    reference: float                    # authoritative local P&L (paper_mode or ledger sum)
    local_state: float                  # state.session_pnl before any repair
    remote_bot_state: Optional[float]   # Supabase bot_state.session_pnl mirror
    remote_ledger: Optional[float]      # Supabase pnl_ledger sum
    remote_orders: Optional[float]      # n8n-reported P&L (Supabase orders.pnl_dollars sum)
    tolerance: float
    drift: dict = field(default_factory=dict)      # name -> value - reference
    corrected: bool = False                        # local state was repaired
    consistent: bool = True                        # every available remote is within tolerance
    notes: list = field(default_factory=list)      # human-readable findings

    def to_dict(self) -> dict:
        d = {
            "reference": self.reference,
            "local_state": self.local_state,
            "remote_bot_state": self.remote_bot_state,
            "remote_ledger": self.remote_ledger,
            "remote_orders": self.remote_orders,
            "tolerance": self.tolerance,
            "drift": dict(self.drift),
            "corrected": self.corrected,
            "consistent": self.consistent,
            "notes": list(self.notes),
        }
        return d


def local_reference_pnl(local_db=None) -> float:
    """Return the authoritative local realized P&L.

    Dry-run uses the paper bankroll (paper_mode.pnl()); live uses the local
    pnl_ledger sum - the same sources the startup reconcile in trader.py uses.
    """
    import config  # lazy: keep module import side-effect free
    if config.DRY_RUN:
        try:
            import paper_mode
            return round(float(paper_mode.pnl()), 4)
        except Exception as e:
            log.warning("paper_mode.pnl() failed (%s) - falling back to local ledger", e)
    db = local_db
    if db is None:
        from local_db import get_local_db
        db = get_local_db()
    try:
        return round(float(db.get_total_realized_pnl()), 4)
    except Exception as e:
        log.warning("get_total_realized_pnl failed (%s) - reference is 0.0", e)
        return 0.0


def _remote_client(supabase_client):
    if supabase_client is not None:
        return supabase_client
    try:
        import supabase_client as sb_mod
        if not sb_mod.is_enabled():
            return None
        return sb_mod.get_supabase()
    except Exception as e:
        log.debug("Supabase client unavailable for reconcile: %s", e)
        return None


def _remote_float(client, method: str):
    """Call client.<method>() -> float or None (never raises)."""
    try:
        val = getattr(client, method)()
        return round(float(val), 4) if val is not None else None
    except Exception as e:
        log.debug("reconcile remote read %s failed: %s", method, e)
        return None


def reconcile_session_pnl(state, *, local_db=None, supabase_client=None,
                          tolerance=None, emit=True) -> PnlReconciliation:
    """Compare session_pnl across local state, n8n and Supabase; repair local.

    ``state`` is the in-memory BotState (needs ``session_pnl``). ``local_db``
    and ``supabase_client`` are injectable for tests; by default the shared
    local DB and the (possibly disabled) Supabase client are used.
    """
    import config

    tol = float(tolerance if tolerance is not None else config.PNL_DRIFT_TOLERANCE)
    db = local_db
    if db is None:
        from local_db import get_local_db
        db = get_local_db()

    reference = local_reference_pnl(db)
    local_state = float(getattr(state, "session_pnl", 0.0) or 0.0)
    drift: dict[str, float] = {}
    notes: list[str] = []
    corrected = False

    # 1. in-memory state vs reference
    state_drift = round(local_state - reference, 4)
    drift["local_state"] = state_drift
    ambiguous_zero = (not config.DRY_RUN) and reference == 0.0 and local_state != 0.0
    if abs(state_drift) > tol:
        if ambiguous_zero:
            # Mirror the startup rule: a zero ledger total cannot overwrite a
            # non-zero state (could be pre-ledger history) - report, don't zero.
            notes.append(
                f"local ledger total is 0.00 but state is {local_state:+.2f} - not auto-corrected")
        else:
            state.session_pnl = reference
            if hasattr(state, "realized_pnl"):
                state.realized_pnl = reference
            corrected = True
            notes.append(
                f"state.session_pnl {local_state:+.2f} vs reference {reference:+.2f} - corrected")

    # 2. n8n/Supabase record vs reference (best-effort reads)
    remote_bot_state = remote_ledger = remote_orders = None
    client = _remote_client(supabase_client)
    if client is not None:
        try:
            raw = client.get_bot_state("session_pnl")
            remote_bot_state = round(float(raw), 4) if raw is not None else None
        except Exception as e:
            log.debug("reconcile remote read bot_state failed: %s", e)
        remote_ledger = _remote_float(client, "get_pnl_ledger_total")
        remote_orders = _remote_float(client, "get_orders_pnl_total")
    else:
        notes.append("Supabase client unavailable - remote comparison skipped")

    for name, val in (("supabase_bot_state", remote_bot_state),
                      ("supabase_pnl_ledger", remote_ledger),
                      ("n8n_orders_pnl", remote_orders)):
        if val is None:
            continue
        d = round(val - reference, 4)
        drift[name] = d
        if abs(d) > tol:
            notes.append(f"{name} drifts {d:+.2f} vs local reference "
                         f"(remote={val:+.2f} local={reference:+.2f})")

    remote_keys = ("supabase_bot_state", "supabase_pnl_ledger", "n8n_orders_pnl")
    remote_drift = any(abs(drift[k]) > tol for k in remote_keys if k in drift)
    consistent = not remote_drift

    # 3. Repair the mirror through the normal batched write path. The sync
    #    thread picks the row up (synced=0) and re-pushes within
    #    LOCAL_DB_SYNC_INTERVAL - no inline remote write (D1).
    bot_state_drift = abs(drift.get("supabase_bot_state", 0.0)) > tol
    if corrected or bot_state_drift:
        try:
            db.set_bot_state_batch({
                "session_pnl": reference,
                "pnl_reconcile_ts": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            notes.append(f"local bot_state batch re-write failed: {e}")

    # 4. Surface unrepairable drift (n8n lane or ledger mirror lost rows).
    if remote_drift and emit:
        try:
            from emit import emit_event
            emit_event(
                "kalshi_trader", "reconcile.mismatch",
                {"context": "session_pnl",
                 "reference": reference,
                 "drift": {k: v for k, v in drift.items() if k != "local_state"},
                 "notes": notes},
                key=f"reconcile.mismatch:pnl:{datetime.now(timezone.utc):%Y-%m-%dT%H}",
                severity="warning")
        except Exception as e:
            log.debug("pnl drift emit failed: %s", e)

    result = PnlReconciliation(
        reference=reference,
        local_state=local_state,
        remote_bot_state=remote_bot_state,
        remote_ledger=remote_ledger,
        remote_orders=remote_orders,
        tolerance=tol,
        drift=drift,
        corrected=corrected,
        consistent=consistent,
        notes=notes,
    )
    if notes:
        log.info("session_pnl reconcile: %s", "; ".join(notes))
    return result


def maybe_reconcile_session_pnl(state, *, interval_s=None, now=None, **kwargs):
    """Throttled wrapper for the main loop; returns None when not due.

    Throttling is per-process via ``state._last_pnl_reconcile_ts``. Never
    raises - a reconciliation failure must not stall the trading loop.
    """
    import config
    interval = float(interval_s if interval_s is not None else config.PNL_RECONCILE_INTERVAL_S)
    now = time.time() if now is None else float(now)
    try:
        last = float(getattr(state, "_last_pnl_reconcile_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    if now - last < interval:
        return None
    state._last_pnl_reconcile_ts = now
    try:
        return reconcile_session_pnl(state, **kwargs)
    except Exception as e:
        log.warning("session_pnl reconcile failed: %s", e)
        return None
