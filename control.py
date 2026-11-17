#!/usr/bin/env python3
"""N4 — signed control commands from n8n to the traders' local inbox.

Path: n8n webhook (superbot-control) -> Verify Signature node ->
scripts/send_command.py --stdin -> control_inbox table -> trader drains the
inbox at the top of each cycle and applies the command.

Rules (spec AGENT_TASKS_v7.md N4):
- a command is valid only with a fresh HMAC-SHA256 over the timestamp,
  nonce and body (n8n_webhook_auth), an allowlisted type and a bounded
  payload — free text is never parsed;
- replay protection: UNIQUE(nonce) in control_inbox;
- destructive or risk-increasing commands (resume_entries after a risk
  halt) require a second confirmation identity in `confirm_by`, different
  from the issuing agent;
- reduce_exposure_caps can only lower caps, never raise them;
- every command gets a result event (control.applied / control.rejected).
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import config
import n8n_webhook_auth

COMMANDS = frozenset({
    "pause_entries", "resume_entries", "reduce_exposure_caps",
    "request_status", "request_report",
})

# risk.py reads these at call time, so mutating the config module applies now.
CAP_ATTRS = {"per_trade_pct": "PER_TRADE_PCT",
             "exposure_ceiling_pct": "EXPOSURE_CEILING_PCT"}

# Caps live in the running process only; on restart config raises them back
# to .env defaults. Persisting them would require a risk-state format bump —
# out of scope for N4.


@dataclass
class ControlState:
    pause_entries: bool = False

    def snapshot(self) -> dict:
        return {"pause_entries": self.pause_entries}


STATE = ControlState()


def verify_command(row: dict) -> tuple:
    """Re-verify a stored command. Returns (ok, reason, body, agent_id, event_id)."""
    sig = row.get("signature") or ""
    ts = row.get("ts")
    nonce = row.get("nonce") or ""
    body = row.get("payload_json") or "{}"
    agent_id = row.get("agent_id") or "unknown"
    if not sig:
        return False, "missing signature", {}, agent_id, ""
    ok, reason = n8n_webhook_auth.verify_request(ts, nonce, body, sig)
    if not ok:
        return False, f"signature: {reason}", {}, agent_id, ""
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except Exception as e:
        return False, f"payload: {e}", {}, agent_id, ""
    return True, "ok", payload, agent_id, f"control:{row.get('command')}:{nonce}"


def _reject(db, row: dict, reason: str, source: str) -> dict:
    """Mark a command rejected, log, and emit a critical ack."""
    import logging
    logging.getLogger(__name__).warning(
        "control command %s rejected (agent=%s): %s",
        row.get("command"), row.get("agent_id"), reason)
    db.set_control_command_result(row["id"], "rejected", reason)
    import emit
    emit.emit_event(source, "control.rejected",
                    {"command": row.get("command"), "agent_id": row.get("agent_id"),
                     "reason": reason},
                    key=f"control.rejected:{row.get('command')}:{row.get('nonce')}",
                    severity="critical")
    return {"command": row.get("command"), "status": "rejected", "reason": reason}


def _apply(db, row: dict, source: str, emit_result, **context) -> dict:
    db.set_control_command_result(row["id"], "applied", json.dumps(emit_result))
    emit_result = dict(emit_result)
    emit_result["command"] = row.get("command")
    import emit
    emit.emit_event(source, "control.applied", emit_result,
                    key=f"control.applied:{row.get('command')}:{row.get('nonce')}")
    return {"command": row.get("command"), "status": "applied", "result": emit_result}


def _snapshot(db, source: str, **context) -> dict:
    halted = bool(context.get("risk_halted"))
    snap = {
        "positions_open": len(context.get("positions") or {}),
        "risk_halted": halted,
        "caps": {a: getattr(config, attr, None) for a, attr in CAP_ATTRS.items()},
        "control": STATE.snapshot(),
    }
    return snap


def apply_command(db, row: dict, *, risk_halted: bool = False,
                  positions: Optional[dict] = None,
                  source: str = "kalshi_trader") -> dict:
    """Verify and apply one drained row. Never raises."""
    command = (row.get("command") or "").strip()
    try:
        ok, reason, payload, agent_id, _key = verify_command(row)
        if not ok:
            return _reject(db, row, reason, source)
        if command not in COMMANDS:
            return _reject(db, row, f"unknown command: {command!r}", source)

        context = {"risk_halted": risk_halted, "positions": positions}
        if command == "pause_entries":
            STATE.pause_entries = True
            result = _apply(db, row, source, {"paused": True, "reason": payload.get("reason") or ""})
            return result

        if command == "resume_entries":
            confirm_by = str(payload.get("confirm_by") or "").strip()
            if risk_halted:
                if not confirm_by or confirm_by == agent_id:
                    return _reject(
                        db, row,
                        "resume after a risk halt requires confirm_by from a different identity",
                        source)
                # second confirmation accepted: lift the control pause only; the
                # risk halt itself still needs HALT_AUTO_RESUME / VPS --resume.
                STATE.pause_entries = False
                return _apply(db, row, source,
                              {"paused": False, "confirmed_by": confirm_by,
                               "risk_halt_still_active": True})
            STATE.pause_entries = False
            return _apply(db, row, source, {"paused": False, "confirmed_by": confirm_by or ""})

        if command == "reduce_exposure_caps":
            new_caps = {}
            for key, attr in CAP_ATTRS.items():
                if key not in payload:
                    continue
                try:
                    value = float(payload[key])
                except (TypeError, ValueError):
                    return _reject(db, row, f"{key} must be a number", source)
                if not (0.0 < value <= 1.0):
                    return _reject(db, row, f"{key} must be in (0, 1]", source)
                current = float(getattr(config, attr))
                if value > current:
                    return _reject(db, row,
                                   f"{key} {value} would raise the cap above {current}; "
                                   "reduce_exposure_caps can only lower caps",
                                   source)
                new_caps[attr] = value
            for attr, value in new_caps.items():
                setattr(config, attr, value)
            return _apply(db, row, source,
                          {"caps": {k: getattr(config, v) for k, v in CAP_ATTRS.items()}})

        if command == "request_status":
            snap = _snapshot(db, source, **context)
            import emit
            emit.emit_event(source, "control.status", snap,
                            key=f"control.status:{row.get('nonce')}")
            return _apply(db, row, source, {"status": snap})

        if command == "request_report":
            snap = _snapshot(db, source, **context)
            try:
                conn = db._get_conn()
                counts = {
                    "orders_24h": conn.execute(
                        "SELECT COUNT(*) FROM orders WHERE created_at >= "
                        "datetime('now', '-1 day')").fetchone()[0],
                    "pnl_rows_24h": conn.execute(
                        "SELECT COUNT(*) FROM pnl_ledger WHERE applied_at >= "
                        "datetime('now', '-1 day')").fetchone()[0],
                }
            except Exception as e:
                counts = {"error": str(e)[:120]}
            snap.update(counts)
            import emit
            emit.emit_event(source, "control.report", snap,
                            key=f"control.report:{row.get('nonce')}")
            return _apply(db, row, source, {"report": snap})

        # unreachable: COMMANDS allowlist already checked
        return _reject(db, row, f"unknown command: {command!r}", source)
    except Exception as e:  # never raise on the trading path
        log_reason = f"internal: {e}"
        return _reject(db, row, log_reason[:200], source)


def drain_and_apply(db, *, risk_halted: bool = False,
                    positions: Optional[dict] = None,
                    source: str = "kalshi_trader") -> list:
    """Apply every pending command oldest-first. Never raises."""
    results = []
    try:
        rows = db.get_pending_control_commands()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("control inbox drain failed: %s", e)
        return results
    for row in rows:
        results.append(apply_command(db, row, risk_halted=risk_halted,
                                     positions=positions, source=source))
    return results
