"""Dry-run paper ledger.

A dry run places no real orders, so the exchange balance never moves and
nothing compounds. This module keeps its own paper balance so a dry run
compounds instead of sitting still:

    paper cash = start + paper pnl - capital locked in open positions

`start` is captured once from the real account balance and persisted, so
restarting the bot does not reset the curve. Paper P&L is tracked here rather
than reusing `session_pnl` so live settlements from an earlier session can
never leak into the paper curve.

Paper fills are modelled in trader._manage_paper_fills(): a parked bid fills
only when that side's ask crosses it, so adverse selection is reflected rather
than assumed away. Exits still fill at the quoted price.
"""
import json
import logging
import os
import sys
from pathlib import Path

import config

log = logging.getLogger("paper_mode")

STATE_FILE = Path(os.environ.get(
    "PAPER_STATE_FILE",
    Path(__file__).resolve().parent / "paper_state.json",
))

_state = None

LOCAL_DB_KEY = "paper_pnl_dollars"

# Under pytest, skip local_db recovery so tests don't depend on external state.
_IS_TEST = "pytest" in sys.modules


def _local_db():
    """Lazy local_db client for redundant paper P&L backup."""
    try:
        from local_db import get_local_db
        return get_local_db()
    except Exception:
        return None


def _seed() -> float:
    """Starting paper bankroll: config.DRY_STARTING_BALANCE, else a default.

    The live account is $1-2, where one contract is already ~12% of the balance,
    so a percentage sizing rule cannot be expressed at all. DRY_STARTING_BALANCE
    lets a dry run compound at a measurable scale (the same $10 the backtest
    uses) instead of being blocked by cent granularity.
    """
    return round(float(config.DRY_STARTING_BALANCE or 10.0), 4)


def _load() -> dict:
    """Load the ledger, seeding start_balance once."""
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
        # Try recovering paper pnl from local_db backup before reseeding.
        # Skipped under pytest so tests don't depend on external state.
        ldb = None if _IS_TEST else _local_db()
        saved_pnl = None
        if ldb is not None:
            try:
                saved_pnl = ldb.get_bot_state(LOCAL_DB_KEY)
            except Exception:
                pass
        if saved_pnl is not None:
            _state = {"start_balance": _seed(), "pnl": float(saved_pnl)}
            log.info("Recovered paper pnl $%.2f from local_db backup", _state["pnl"])
        else:
            _state = {"start_balance": _seed(), "pnl": 0.0}
            log.info("Paper ledger seeded at $%.2f", _state["start_balance"])
    except Exception as e:
        log.warning("Could not read %s (%s) — reseeding", STATE_FILE, e)
        _state = {"start_balance": _seed(), "pnl": 0.0}
    _save()
    return _state


def _save():
    try:
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(_state))
        tmp.replace(STATE_FILE)  # atomic on same filesystem
    except Exception as e:
        log.warning("Could not write %s: %s", STATE_FILE, e)


def start_balance() -> float:
    return _load()["start_balance"]


def pnl() -> float:
    return _load()["pnl"]


def add_pnl(realized: float) -> None:
    """Credit realized paper P&L when a paper position settles."""
    data = _load()
    data["pnl"] = round(data["pnl"] + realized, 6)
    _save()
    # Redundant backup: also persist paper pnl to local_db so a lost or
    # corrupt paper_state.json can be recovered on restart.
    ldb = _local_db()
    if ldb is not None:
        try:
            ldb.set_bot_state(LOCAL_DB_KEY, str(data["pnl"]))
        except Exception as e:
            log.warning("Could not back up paper pnl to local_db: %s", e)


def cash(open_risk: float) -> float:
    """Paper cash: start + realized paper P&L - capital in open positions."""
    data = _load()
    return data["start_balance"] + data["pnl"] - open_risk


def reset() -> float:
    """Restart the paper curve from the configured starting balance."""
    global _state
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    _state = None
    return start_balance()
