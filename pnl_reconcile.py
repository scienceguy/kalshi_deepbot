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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class PnlReconciliation:
    """Result of one reconciliation pass (all money values in dollars).

    ``reference`` is None when the authoritative local P&L could not be read
    (transient DB error); in that case no comparison is made and nothing is
    corrected - a transient failure must never zero session P&L.
    """

    reference: Optional[float]           # authoritative local P&L (paper_mode or ledger sum); None = unknown
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
        return asdict(self)


def local_reference_pnl(local_db=None) -> Optional[float]:
    """Return the authoritative local realized P&L, or None when unknown.

    Dry-run uses the paper bankroll (paper_mode.pnl()); live uses the local
    pnl_ledger sum - the same sources the startup reconcile in trader.py uses.
    None is returned when the source read fails (transient DB error) so the
    caller can skip the pass instead of mistaking the failure for $0.00.
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
        log.warning("get_total_realized_pnl failed (%s) - reference unknown, skipping", e)
        return None


def _remote_client(supabase_client):
    """Return a usable Supabase client, or None (never raises).

    Distinguishes "not configured" (quiet skip) from "configured but
    unavailable" (warns) so remote drift detection never silently no-ops
    when the deployment is supposed to have Supabase.
    """
    if supabase_client is not None:
        return supabase_client
    try:
        import supabase_client as sb_mod
    except Exception as e:
        log.warning("Supabase module import failed - remote drift detection disabled: %s", e)
        return None
    try:
        enabled = sb_mod.is_enabled()
    except Exception as e:
        log.warning("Supabase is_enabled() check failed - remote drift detection disabled: %s", e)
        return None
    if not enabled:
        log.info("Supabase credentials not configured - remote comparison skipped")
        return None
    try:
        client = sb_mod.get_supabase()
    except Exception as e:
        log.warning("Supabase client creation failed - remote drift detection disabled: %s", e)
        return None
    if getattr(client, "enabled", True) is False:
        log.warning("Supabase client disabled (startup health check/auth failure) - "
                    "remote drift detection skipped")
        return None
    return client


def _remote_float(client, method: str):
    """Call client.<method>() -> float or None (never raises).

    Expected transport failures (network, timeout, HTTP, decode) are logged at
    debug and treated as unknown. Anything else is likely a bug (e.g. a typo'd
    method name) - logged at warning so it is not silently swallowed, but still
    not raised: the reconcile pass must never break the trading cycle.
    """
    import requests  # local import: keep module import side-effect free

    expected = (requests.RequestException, OSError, ValueError, TypeError)
    try:
        val = getattr(client, method)()
    except expected as e:
        log.debug("reconcile remote read %s failed: %s", method, e)
        return None
    except Exception as e:
        log.warning("reconcile remote read %s raised unexpected %s: %s",
                    method, type(e).__name__, e)
        return None
    try:
        return round(float(val), 4) if val is not None else None
    except (ValueError, TypeError) as e:
        log.warning("reconcile remote read %s returned non-numeric %r: %s",
                    method, val, e)
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

    if reference is None:
        # Reference unknown (transient DB error): skip the pass entirely -
        # no correction, no batched re-write, no drift alert.
        result = PnlReconciliation(
            reference=None,
            local_state=local_state,
            remote_bot_state=None,
            remote_ledger=None,
            remote_orders=None,
            tolerance=tol,
            drift=drift,
            corrected=False,
            consistent=True,
            notes=["local reference unavailable - reconciliation skipped"],
        )
        log.info("session_pnl reconcile: %s", "; ".join(result.notes))
        return result

    # 1. in-memory state vs reference
    state_drift = round(local_state - reference, 4)
    drift["local_state"] = state_drift
    # Ambiguous zero applies in BOTH modes: a zero reference (fresh/re-seeded
    # paper ledger or empty local ledger) cannot overwrite a non-zero state
    # that may hold pre-ledger history - report, don't zero.
    ambiguous_zero = reference == 0.0 and local_state != 0.0
    if abs(state_drift) > tol:
        if ambiguous_zero:
            # Mirror the startup rule: a zero reference cannot overwrite a
            # non-zero state (could be pre-ledger/pre-paper history).
            notes.append(
                f"local reference is 0.00 but state is {local_state:+.2f} - not auto-corrected")
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
        if remote_bot_state is None and remote_ledger is None and remote_orders is None:
            notes.append("Supabase client present but every remote read failed - "
                         "drift detection degraded")
            log.warning("Supabase configured but all remote reads failed - "
                        "remote drift detection degraded")
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
    #    One event per drifting source, keyed by source name, so a lost
    #    settlement webhook (n8n_orders_pnl) is distinguishable from a
    #    failed sync thread (supabase_bot_state / supabase_pnl_ledger).
    if emit:
        for name in (k for k in remote_keys
                     if k in drift and abs(drift[k]) > tol):
            try:
                from emit import emit_event
                emit_event(
                    "kalshi_trader", "reconcile.mismatch",
                    {"context": "session_pnl",
                     "source": name,
                     "reference": reference,
                     "drift": {name: drift[name]},
                     "notes": notes},
                    key=f"reconcile.mismatch:pnl:{name}:{datetime.now(timezone.utc):%Y-%m-%dT%H}",
                    severity="warning")
            except Exception as e:
                log.debug("pnl drift emit failed for %s: %s", name, e)

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


def reconcile_due(state, *, interval_s=None, now=None) -> bool:
    """True when the throttled reconcile is due. Never raises - callers on the
    hot loop can skip the reconcile call entirely when this returns False."""
    try:
        import config
        interval = float(interval_s if interval_s is not None
                         else config.PNL_RECONCILE_INTERVAL_S)
        now = time.time() if now is None else float(now)
        try:
            last = float(getattr(state, "_last_pnl_reconcile_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        return (now - last) >= interval
    except Exception:
        return False


def maybe_reconcile_session_pnl(state, *, interval_s=None, now=None,
                                local_db=None, supabase_client=None,
                                tolerance=None, emit=True):
    """Throttled wrapper for the main loop; returns None when not due.

    Parameters:
        state: in-memory BotState (needs ``session_pnl``).
        interval_s: minimum seconds between reconcile passes; defaults to
            ``config.PNL_RECONCILE_INTERVAL_S``.
        now: override for the current time (tests); defaults to time.time().
        local_db: injectable local DB (tests); default is the shared local DB.
        supabase_client: injectable Supabase client (tests); default is the
            shared (possibly disabled) client.
        tolerance: drift tolerance in dollars; default is
            ``config.PNL_DRIFT_TOLERANCE``.
        emit: when False, skip the reconcile.mismatch event on drift.

    Throttling is per-process via ``state._last_pnl_reconcile_ts``. Never
    raises - a reconciliation failure must not stall the trading loop.
    """
    now = time.time() if now is None else float(now)
    if not reconcile_due(state, interval_s=interval_s, now=now):
        return None
    state._last_pnl_reconcile_ts = now
    try:
        return reconcile_session_pnl(state, local_db=local_db,
                                     supabase_client=supabase_client,
                                     tolerance=tolerance, emit=emit)
    except Exception as e:
        log.warning("session_pnl reconcile failed: %s", e)
        return None
