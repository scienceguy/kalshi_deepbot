"""Dry-run paper ledger.

A dry run places no real orders, so the exchange balance never moves and
nothing compounds. This module keeps its own paper balance so a dry run
compounds instead of sitting still:

    paper cash = start + paper pnl - capital locked in open positions

`start` is captured once from the real account balance and persisted, so
restarting the bot does not reset the curve. Paper P&L is tracked here rather
than reusing `session_pnl` so live settlements from an earlier session can
never leak into the paper curve.

shortcut: paper fills assume the posted maker price fills. The real fill rate
for ask-1c maker orders is unmodelled, so trade counts (and therefore
compounding speed) are optimistic — per-trade edge is not. Upgrade to a fill
model when orderbook history is available.
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("paper_mode")

STATE_FILE = Path(os.environ.get(
    "PAPER_STATE_FILE",
    Path(__file__).resolve().parent / "paper_state.json",
))

_state = None


def _load(real_balance: float) -> dict:
    """Load the ledger, seeding start_balance from the real account once."""
    global _state
    if _state is not None:
        return _state
    try:
        data = json.loads(STATE_FILE.read_text())
        _state = {
            "start_balance": float(data["start_balance"]),
            "pnl": float(data.get("pnl", 0.0)),
        }
    except FileNotFoundError:
        _state = {"start_balance": round(real_balance, 4), "pnl": 0.0}
        log.info("Paper ledger seeded at $%.2f", real_balance)
    except Exception as e:
        log.warning("Could not read %s (%s) — reseeding", STATE_FILE, e)
        _state = {"start_balance": round(real_balance, 4), "pnl": 0.0}
    _save()
    return _state


def _save():
    try:
        STATE_FILE.write_text(json.dumps(_state))
    except Exception as e:
        log.warning("Could not write %s: %s", STATE_FILE, e)


def start_balance(real_balance: float) -> float:
    return _load(real_balance)["start_balance"]


def pnl(real_balance: float) -> float:
    return _load(real_balance)["pnl"]


def add_pnl(real_balance: float, realized: float) -> None:
    """Credit realized paper P&L when a paper position settles."""
    data = _load(real_balance)
    data["pnl"] = round(data["pnl"] + realized, 6)
    _save()


def cash(real_balance: float, open_risk: float) -> float:
    """Paper cash: start + realized paper P&L - capital in open positions."""
    data = _load(real_balance)
    return data["start_balance"] + data["pnl"] - open_risk


def reset(real_balance: float) -> float:
    """Restart the paper curve from the current real balance."""
    global _state
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    _state = None
    return start_balance(real_balance)
