"""
poly_us_trader.py — Polymarket US Paper Trading Engine ($100 Starting Bankroll).

Monitors Polymarket US production API (api.polymarket.us), scans active markets across
crypto and event categories, evaluates high-probability favorite pricing (92c-99c),
validates order parameters with real orders.preview calls, simulates paper fills,
and tracks portfolio mark-to-market equity and venue settlements.

Usage:
    python poly_us_trader.py --once
    python poly_us_trader.py --interval 15
    python poly_us_trader.py --status
"""

import argparse
import fcntl
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make scripts/ importable for bs_calibration (Task 2: Brier calibration + vol scaling).
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from bs_calibration import RollingCalibrationMonitor, AdaptiveVolatilityScaler

import polymarket_us_client
import crossvenue_edge
import config
from resilience import systemd_notify

# Optional ML fair-value model (torch may not be installed everywhere).
try:
    from ml_model import MLFairValue
except Exception:  # torch or ml_model unavailable -> ML gate disabled
    MLFairValue = None

# One anchor for every runtime path: the repo's local_data/ directory. Launching
# from any CWD writes the same files (R13).
DATA_DIR = Path(__file__).resolve().parent / "local_data"
STATE_FILE = DATA_DIR / "poly_us_state.json"
LEDGER_FILE = DATA_DIR / "poly_us_paper_ledger.jsonl"
POLY_US_LOG_FILE = DATA_DIR / "poly_us_dry.log"
LOCK_FILE = Path(__file__).resolve().parent / ".lock_poly"

log = logging.getLogger("poly_us_trader")

# R17: SIGTERM/SIGINT set this flag; the main loop exits cleanly after the
# current cycle instead of dying mid-write (state/ledger stay consistent).
_stop_requested = False


def _request_stop(signum, frame):
    global _stop_requested
    _stop_requested = True



def market_end_dt(market: dict):
    """Single source of truth for a market's end time (R5).

    ``assetPriceTerms.windowEnd`` (event-window markets) wins over ``endDate``.
    ``gameStartTime`` is a game's START, never an end, so it is deliberately
    not consulted. Returns None when no parseable end time exists.
    """
    terms = market.get("assetPriceTerms") or {}
    for candidate in (terms.get("windowEnd"), market.get("endDate")):
        if not candidate:
            continue
        try:
            dt = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _book_yes_prices(book: dict):
    """(yes_bid, yes_ask) from a Polymarket US BBO response; (None, None) if unreadable."""
    if not isinstance(book, dict):
        return None, None
    bbo = book.get("bbo") if isinstance(book.get("bbo"), dict) else book
    try:
        bid = float(bbo["yes_bid"])
        ask = float(bbo["yes_ask"])
        return bid, ask
    except (KeyError, TypeError, ValueError):
        return None, None


def _slippage_cents() -> float:
    return float(getattr(config, "PM_SLIPPAGE_TICKS", 1)) * 1.0


def _fee_cents(price_dollars: float) -> float:
    """Per-contract taker fee in cents."""
    return _taker_fee(1.0, price_dollars) * 100.0


def _fill_buy_price(displayed: float, ask) -> float | None:
    """Paper fill at the best ask plus slippage ticks.

    Returns None when the book is unreadable — the caller must not trade
    (A2: never fall back to the displayed mark on an unreadable book).
    """
    if ask is None:
        return None
    slippage = _slippage_cents() / 100.0  # cents -> dollars
    return round(ask + slippage, 4)


def _fill_sell_price(displayed: float, bid) -> float | None:
    """Paper fill at the best bid minus slippage ticks.

    Returns None when the book is unreadable.
    """
    if bid is None:
        return None
    slippage = _slippage_cents() / 100.0  # cents -> dollars
    return round(bid - slippage, 4)


def price_for_maker_entry(book: dict, side: str) -> float | None:
    """Maker entry price for a live post-only order.

    For a YES buy: best bid + 1 tick ($0.01), capped at ask - 1 tick — never
    at or through the ask, so the order can never take liquidity.
    For a NO sell: best ask - 1 tick, capped at bid + 1 tick — never at or
    through the bid.

    Returns None when the book is unreadable or no valid maker price exists.
    (A3: live maker entries must always rest below the ask; paper taker fills
    at the ask are a different model.)
    """
    if not isinstance(book, dict):
        return None
    bbo = book.get("bbo") if isinstance(book.get("bbo"), dict) else book
    tick = 0.01
    try:
        bid = float(bbo["yes_bid"])
        ask = float(bbo["yes_ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid is None or ask is None or bid >= ask:
        return None
    if side == "yes":
        price = round(bid + tick, 4)
        cap = round(ask - tick, 4)
        return price if price <= cap else None
    else:  # "no"
        # NO sell as maker: offer at NO bid + tick = (1 - YES ask) + tick,
        # capped at NO ask - tick = (1 - YES bid) - tick. Never at or through
        # the NO bid (which would cross the spread and take liquidity).
        price = round(1.0 - ask + tick, 4)
        cap = round(1.0 - bid - tick, 4)
        return price if price <= cap else None


def _taker_fee(qty: float, price_dollars: float) -> float:
    """Polymarket taker fee: C x feeRate x p x (1 - p), rounded to 5dp.

    Official schedule (docs.polymarket.com/trading/fees): crypto 0.07, sports
    0.05, finance/politics/tech 0.04. Makers pay nothing; paper fills at the
    ask, which is a taker fill, so the taker fee is the honest model.
    """
    rate = float(getattr(config, "PM_FEE_RATE", 0.07))
    return round(qty * rate * price_dollars * (1.0 - price_dollars), 5)


class PolyUSPaperTrader:
    def __init__(self, initial_cash: float = 35.0, reset: bool = False, live_mode: bool = False):
        self.state_file = STATE_FILE
        self.ledger_file = LEDGER_FILE
        self.max_open_positions = 25
        self.live_mode = live_mode
        if reset:
            self.state = self.reset_state(initial_cash)
        else:
            self.state = self._load_state(initial_cash)
        self.client = polymarket_us_client._get_client()
        self.auth_client = None
        try:
            self.auth_client = polymarket_us_client._get_authenticated_client()
        except Exception as e:
            log.warning("Polymarket US authenticated client not available: %s", e)

        # Wire in gateway health check at startup
        if polymarket_us_client.health_check():
            log.info("Polymarket US gateway health check: OK")
        else:
            log.warning("Polymarket US gateway health check: FAILED or unreachable")

        # Task 2: continuous Brier calibration + adaptive volatility scaling.
        self.cal_monitor = RollingCalibrationMonitor(
            ledger_path=str(DATA_DIR / "bs_calibration_ledger.jsonl"))
        self.vol_scaler = AdaptiveVolatilityScaler(
            base_sigma=float(getattr(config, "MODEL_VOLATILITY", 0.80)),
            base_min_z=float(getattr(config, "GATES_MIN_CUSHION_Z", 1.80)),
        )
        self.adjusted_sigma = self.vol_scaler.base_sigma
        self.adjusted_min_z = self.vol_scaler.base_min_z
        self.calibration_halted = False
        self.calibration_reason = "not started"
        self.calibration_probing = False

        # Optional ML fair-value model filter (opt-in via ML_MODEL_PATH).
        self.ml_model = None
        ml_path = getattr(config, "ML_MODEL_PATH", "")
        if ml_path and MLFairValue is not None:
            try:
                self.ml_model = MLFairValue.load(ml_path)
                log.info("ML fair-value model loaded from %s", ml_path)
            except Exception as e:
                log.warning("Failed to load ML model from %s: %s", ml_path, e)
        elif ml_path and MLFairValue is None:
            log.warning("ML_MODEL_PATH set but ml_model/torch unavailable; ML gate disabled")

        # A3: paper fill model — refuse to mix models in one state epoch.
        self.paper_fill_model = getattr(config, "PM_PAPER_FILL_MODEL", "taker")
        if self.paper_fill_model not in ("taker", "maker"):
            log.warning("PM_PAPER_FILL_MODEL=%r is not 'taker' or 'maker'; defaulting to 'taker'",
                        self.paper_fill_model)
            self.paper_fill_model = "taker"
        # If the persisted state already has positions/orders booked under a
        # different model, refuse to mix — the operator must --reset first.
        existing_models = set()
        for p in self.state.get("positions") or []:
            m = p.get("paper_fill_model", "taker")  # legacy positions are taker
            existing_models.add(m)
        for o in self.state.get("resting_orders") or []:
            m = o.get("paper_fill_model", "taker" if o.get("state") != "resting" else "taker")
            existing_models.add(m)
        if existing_models and self.paper_fill_model not in existing_models and len(existing_models) > 1:
            log.warning("State already mixes fill models %s; refusing to switch to %s — run --reset first",
                        sorted(existing_models), self.paper_fill_model)
            self.paper_fill_model = None  # disable new entries until reset

    def reset_state(self, initial_cash: float = 35.0) -> dict:
        """Manual reset to a fresh bankroll (--reset only; the bot never auto-resets).

        Lifetime stats (lifetime_realized_pnl, peak_equity, max_drawdown,
        ruin_events, trades_count) survive a reset — losing runs are not erased.
        Positions discarded by the reset are booked at the best available mark
        with a "forfeit" ledger event.
        """
        old = getattr(self, "state", None) or {}
        old_realized = float(old.get("realized_pnl", 0.0) or 0.0)
        old_lifetime = float(old.get("lifetime_realized_pnl", 0.0) or 0.0)
        forfeit_pnl = 0.0
        for pos in old.get("positions", []):
            mark = float(pos.get("current_price") or pos.get("price") or 0.0)
            qty = float(pos.get("qty", 0) or 0)
            payout = round(mark * qty, 4)
            pnl = round(payout - float(pos.get("cost", 0.0) or 0.0), 4)
            forfeit_pnl = round(forfeit_pnl + pnl, 4)
            self._append_ledger({
                "event": "forfeit",
                "slug": pos.get("slug"),
                "side": pos.get("side"),
                "qty": qty,
                "mark": mark,
                "payout": payout,
                "pnl": pnl,
                "reason": "manual_reset",
                "cash_after": initial_cash,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        state = {
            "start_balance": initial_cash,
            "cash": initial_cash,
            "positions": [],
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "trades_count": int(old.get("trades_count", 0) or 0),
            "lifetime_realized_pnl": round(old_lifetime + old_realized + forfeit_pnl, 4),
            "peak_equity": round(max(float(old.get("peak_equity", initial_cash) or initial_cash), initial_cash), 4),
            "max_drawdown": float(old.get("max_drawdown", 0.0) or 0.0),
            "ruin_events": int(old.get("ruin_events", 0) or 0),
            "ruin_halted": False,
            "milestones_reached": [float(initial_cash)],
            "window_spent": 0.0,
            "cycles": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # A reset starts a new bankroll epoch; pre-epoch P&L is recomputed from
        # the ledger by _reconcile_ledger().
        state.pop("epoch_started_at", None)
        state.pop("pre_epoch_realized_pnl", None)
        self._reconcile_ledger(state)
        self._save_state(state)
        self.state = state
        self._append_ledger({
            "event": "reset",
            "start_balance": initial_cash,
            "cash": initial_cash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        log.info("Reset Polymarket US paper state with initial bankroll $%.2f", initial_cash)
        return state

    def _daily_loss_key(self) -> str:
        """Return today's date key (YYYY-MM-DD) for daily loss tracking."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _track_daily_realized(self, pnl: float) -> None:
        """Update daily realized loss and set halt if limit exceeded."""
        today = self._daily_loss_key()
        if self.state.get("daily_loss_date", today) != today:
            # New day — reset the counter before judging this P&L, otherwise a
            # winning first trade of the day leaves yesterday's loss standing
            # and the next loss halts entries early.
            self.state["daily_loss_date"] = today
            self.state["daily_realized_loss"] = 0.0
            self.state["daily_loss_halted"] = False
        if pnl >= 0:
            return
        daily_loss = float(self.state.get("daily_realized_loss", 0.0) or 0.0)
        daily_loss = round(daily_loss + abs(pnl), 4)
        self.state["daily_realized_loss"] = daily_loss
        limit = float(getattr(config, "POLY_DAILY_LOSS_LIMIT_DOLLARS", 5.0))
        if not self.state.get("daily_loss_halted") and daily_loss >= limit:
            self.state["daily_loss_halted"] = True
            log.warning("Daily realized loss $%.2f >= limit $%.2f — halting new entries (auto-clears next day)",
                        daily_loss, limit)

    def _daily_loss_halted(self) -> bool:
        """True when today's realized loss already hit the daily limit.

        Also performs the day rollover so the halt clears automatically at the
        next UTC day even if no settlement event fires.
        """
        today = self._daily_loss_key()
        if self.state.get("daily_loss_date", today) != today:
            self.state["daily_loss_date"] = today
            self.state["daily_realized_loss"] = 0.0
            self.state["daily_loss_halted"] = False
        return bool(self.state.get("daily_loss_halted", False))

    def _ledger_totals(self):
        """(net realized P&L, settlement count) recorded in the paper ledger."""
        total = 0.0
        settled = 0
        try:
            with open(self.ledger_file) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # torn write: skip the line, not the whole file
                    event = rec.get("event")
                    if event in ("settlement", "rotate_exit", "forfeit"):
                        total += float(rec.get("pnl") or 0.0)
                    if event == "settlement":
                        settled += 1
        except OSError:
            return 0.0, 0
        return round(total, 4), settled

    def _reconcile_ledger(self, state: dict) -> None:
        """Record the ledger's view of realized P&L and shout on any divergence.

        The state file is authoritative for cash, but the ledger is the audit
        trail. A gap means a settlement or rotation reached one and not the
        other — a crash between the two writes, or a bankroll reset that dropped
        open positions. That must be visible, never silent.
        """
        ledger_realized, settled_count = self._ledger_totals()
        state["ledger_realized_pnl"] = ledger_realized
        state["ledger_settlements"] = settled_count
        state["settlements_count"] = max(int(state.get("settlements_count", 0) or 0),
                                         settled_count)
        state_realized = float(state.get("realized_pnl", 0.0) or 0.0)
        # Every bankroll baseline starts somewhere. On the first reconciled run
        # for this state file the baseline begins now; whatever the ledger
        # already recorded predates it. Keeping that split means a refilled
        # bankroll (the old auto-reset) can never read as a break-even curve.
        if "epoch_started_at" not in state:
            state["epoch_started_at"] = datetime.now(timezone.utc).isoformat()
            state["pre_epoch_realized_pnl"] = round(ledger_realized - state_realized, 4)
        pre_epoch = float(state.get("pre_epoch_realized_pnl", 0.0) or 0.0)
        # Invariant: pre-epoch P&L + this epoch's realized P&L == the ledger.
        gap = round(state_realized + pre_epoch - ledger_realized, 4)
        state["ledger_realized_gap"] = gap
        if abs(gap) >= 0.005:
            log.warning(
                "Settlement tracking mismatch: state realized %+.2f %+.2f pre-epoch vs ledger %+.2f "
                "(gap %+.2f, %d settled) — run scripts/poly_ledger_audit.py for detail",
                state_realized, pre_epoch, ledger_realized, gap, settled_count)
        else:
            log.info("Settlement tracking reconciled: state realized %+.2f %+.2f pre-epoch == ledger %+.2f (%d settled)",
                     state_realized, pre_epoch, ledger_realized, settled_count)

    def _load_state(self, initial_cash: float) -> dict:
        fresh = {
            "start_balance": initial_cash,
            "cash": initial_cash,
            "positions": [],
            "realized_pnl": 0.0,
            "trades_count": 0,
            "lifetime_realized_pnl": 0.0,
            "peak_equity": float(initial_cash),
            "max_drawdown": 0.0,
            "ruin_events": 0,
            "ruin_halted": False,
            "milestones_reached": [float(initial_cash)],
            "settlements_count": 0,
            "settled_realized_pnl": 0.0,
            "ledger_realized_pnl": 0.0,
            "cycles": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                if "cash" in data and "positions" in data:
                    # Older state files predate the lifetime stats; default them.
                    for k, v in fresh.items():
                        data.setdefault(k, v)
                    # Normalise stale nulls left by older code paths (e.g. cycles stored as None).
                    if data.get("cycles") is None:
                        data["cycles"] = 0
                    self._reconcile_ledger(data)
                    # Persist the epoch baseline: without this a restart before
                    # the first cycle's save re-baselines the epoch timestamp.
                    self._save_state(data)
                    return data
                raise ValueError("state file missing required keys (cash/positions)")
            except Exception as e:
                # Quarantine the unreadable file instead of silently discarding
                # the bankroll history (R13).
                log.error("Paper state file %s unreadable (%s) — renaming aside, starting fresh",
                          self.state_file, e)
                try:
                    quarantine = self.state_file.with_name(
                        f"{self.state_file.name}.corrupt-{int(time.time())}")
                    self.state_file.replace(quarantine)
                except OSError as re:
                    log.error("Could not quarantine corrupt state file %s: %s", self.state_file, re)
        self._reconcile_ledger(fresh)
        self._save_state(fresh)
        return fresh

    def _save_state(self, state: dict):
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write to prevent file corruption
        tmp_file = self.state_file.with_name(f"{self.state_file.name}.{os.getpid()}.tmp")
        tmp_file.write_text(json.dumps(state, indent=2))
        os.replace(tmp_file, self.state_file)

    def _append_ledger(self, record: dict):
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_file, "a") as f:
            f.write(json.dumps(record) + "\n")

    def _held_position_qty(self, slug: str):
        """Held contract qty for a slug, or None when the venue cannot answer.

        None is distinct from 0.0 on purpose: reconcile treats "not held" as
        "order cancelled", so a venue failure must not look like zero held or
        a live order's reserved cash is released while the position exists.
        Rows with no recognisable slug are skipped — they are not evidence
        about this market.
        """
        try:
            res = polymarket_us_client.get_portfolio_positions() or {}
        except Exception as e:
            log.warning("Live reconcile: get_portfolio_positions failed: %s", e)
            return None
        rows = []
        if isinstance(res, list):
            rows = res
        elif isinstance(res, dict):
            for k in ("positions", "data", "results"):
                v = res.get(k)
                if isinstance(v, list):
                    rows = v
                    break
        total = 0.0
        for r in rows:
            if not isinstance(r, dict):
                continue
            r_slug = r.get("marketSlug") or r.get("slug") or r.get("market")
            if not r_slug:
                continue  # row without a slug says nothing about this market
            if r_slug != slug:
                continue
            try:
                total += float(r.get("qty") or r.get("quantity") or 0)
            except (TypeError, ValueError):
                pass
        return total

    def reconcile_live_orders(self) -> bool:
        """R3: reconcile live resting orders against venue fills.

        - Order gone from the open list and no longer held -> cancelled, release
          the reserved cash back into available cash.
        - Order gone and still held -> fill confirmed (partial): book the filled
          quantity's position and release the reservation for the unfilled part.
        - Open order older than POLY_RESTING_ORDER_TTL_SECONDS -> cancel and
          release the reservation.
        """
        if not self.live_mode:
            return False
        try:
            open_orders = polymarket_us_client.get_open_orders() or []
        except Exception as e:
            log.warning("Live reconcile: get_open_orders failed: %s", e)
            return False
        open_ids = {o.get("id") for o in open_orders if o.get("id")}
        now = datetime.now(timezone.utc)
        ttl_seconds = float(getattr(config, "POLY_RESTING_ORDER_TTL_SECONDS", 900))
        cancelled_any = False
        # A3: per-order fill attribution — subtract quantities already booked
        # for each slug so a never-filled order isn't booked as filled just
        # because an earlier order on the same slug was filled.
        booked_by_slug: dict = {}
        for p in self.state.get("positions") or []:
            bs = p.get("slug")
            if bs:
                booked_by_slug[bs] = booked_by_slug.get(bs, 0) + float(p.get("qty", 0) or 0)
        for o in list(self.state.get("resting_orders") or []):
            if o.get("state") != "resting":
                continue
            oid = o.get("order_id")
            if not oid:
                continue
            placed = datetime.fromisoformat(o.get("placed_at") or now.isoformat())
            if (oid in open_ids and o.get("window_ts") == self.state.get("current_window_ts")
                    and (now - placed).total_seconds() < ttl_seconds):
                continue  # still resting, same window, not stale
            if oid in open_ids:
                cancelled = polymarket_us_client.cancel_order(oid, o.get("slug", ""))
                if cancelled:
                    o["state"] = "cancelled"
                    release_cost = float(o.get("cost", 0.0) or 0.0)
                    self.state["cash"] = round(self.state["cash"] + release_cost, 4)
                    self.state["window_spent"] = max(0.0, round(float(self.state.get("window_spent", 0.0) or 0.0) - release_cost, 4))
                    log.info("Live order %s cancelled (TTL) on %s — reserved cash released", oid, o.get("slug", ""))
                continue
            # Order resolved: filled (if still held) or cancelled by venue/expiry.
            held = self._held_position_qty(o.get("slug", ""))
            if held is None:
                continue  # venue didn't answer — leave the order resting, retry next cycle
            slug_held = max(0.0, held - booked_by_slug.get(o.get("slug", ""), 0.0))
            filled_qty = int(min(float(o.get("qty", 0) or 0), slug_held)) if slug_held > 0 else 0
            if filled_qty > 0 and not any(p.get("order_id") == oid for p in self.state.get("positions") or []):
                fill_price = float(o.get("price") or 0.0)
                fill_cost = round(filled_qty * fill_price, 4)
                self.state["cash"] = round(self.state["cash"] + (float(o.get("cost", 0.0) or 0.0) - fill_cost), 4)
                self.state["window_spent"] = max(0.0, round(float(self.state.get("window_spent", 0.0) or 0.0) - (float(o.get("cost", 0.0) or 0.0) - fill_cost), 4))
                self.state["positions"].append({
                    "slug": o.get("slug", ""),
                    "question": o.get("question", ""),
                    "side": o.get("side", ""),
                    "price": fill_price,
                    "fill_price": fill_price,
                    "displayed_price": fill_price,
                    "fee": float(o.get("fee", 0.0) or 0.0),
                    "source": o.get("source", ""),
                    "qty": filled_qty,
                    "cost": fill_cost,
                    "order_id": oid,
                    "category": o.get("category", ""),
                    "opened_at": o.get("placed_at"),
                })
            if filled_qty > 0:
                o["state"] = "filled"
            else:
                o["state"] = "cancelled"
                release_cost = float(o.get("cost", 0.0) or 0.0)
                self.state["cash"] = round(self.state["cash"] + release_cost, 4)
                self.state["window_spent"] = max(0.0, round(float(self.state.get("window_spent", 0.0) or 0.0) - release_cost, 4))
                cancelled_any = True
                log.info("Live order %s resolved with no held position -> cancelled, reserved cash released", oid)
        self.state["resting_orders"] = [o for o in self.state.get("resting_orders") or [] if o.get("state") == "resting"]
        return True

    def reconcile_paper_maker_orders(self, market_map: dict):
        """A3: fill resting paper maker orders when the book trades through.

        For a YES buy resting at price P: fills if the book's best ask <= P
        (the market has traded down to our price or below).
        For a NO sell resting at price P: fills if the book's best bid >= P
        (the market has traded up to our price or above).
        No fee is charged on maker fills. Stale orders past the TTL are
        cancelled and reserved cash released.
        """
        now = datetime.now(timezone.utc)
        ttl_seconds = float(getattr(config, "POLY_RESTING_ORDER_TTL_SECONDS", 900))
        still_resting = []
        for o in list(self.state.get("resting_orders") or []):
            if o.get("paper_fill_model") != "maker" or o.get("state") != "resting":
                still_resting.append(o)
                continue
            slug = o.get("slug", "")
            side = o.get("side", "")
            resting_price = float(o.get("price", 0.0) or 0.0)
            placed = datetime.fromisoformat(o.get("placed_at") or now.isoformat())
            # TTL: cancel stale resting paper orders.
            if (now - placed).total_seconds() > ttl_seconds:
                o["state"] = "cancelled"
                release_cost = float(o.get("cost", 0.0) or 0.0)
                self.state["cash"] = round(self.state["cash"] + release_cost, 4)
                self.state["window_spent"] = max(
                    0.0, round(float(self.state.get("window_spent", 0.0) or 0.0) - release_cost, 4))
                log.info("Paper maker order %s cancelled (TTL) on %s — reserved cash released",
                         o.get("order_id"), slug)
                continue
            m_info = market_map.get(slug)
            if not m_info:
                # No current market data — keep resting.
                still_resting.append(o)
                continue
            # Use the book from market_map if present (e.g. in tests), else
            # fetch a fresh book for this slug to test the trade-through condition.
            book_in_map = m_info.get("book")
            if isinstance(book_in_map, dict) and book_in_map:
                book = book_in_map
            else:
                try:
                    book = polymarket_us_client.get_market_book(slug)
                except Exception:
                    book = {}
            yes_bid, yes_ask = _book_yes_prices(book)
            filled = False
            if side == "yes" and yes_ask is not None and yes_ask <= resting_price:
                # Book has traded down to our buy price or below.
                filled = True
            elif side == "no" and yes_bid is not None and yes_bid >= resting_price:
                # Book has traded up to our sell price or above.
                filled = True
            if filled:
                qty = int(o.get("qty", 0) or 0)
                fill_price = resting_price
                fill_cost = round(qty * fill_price, 4)  # no maker fee
                self.state["cash"] = round(self.state["cash"] + (float(o.get("cost", 0.0) or 0.0) - fill_cost), 4)
                self.state["window_spent"] = max(
                    0.0, round(float(self.state.get("window_spent", 0.0) or 0.0) - (float(o.get("cost", 0.0) or 0.0) - fill_cost), 4))
                self.state["positions"].append({
                    "slug": slug,
                    "question": o.get("question", ""),
                    "side": side,
                    "price": fill_price,
                    "fill_price": fill_price,
                    "displayed_price": fill_price,
                    "fee": 0.0,
                    "source": o.get("source", ""),
                    "qty": qty,
                    "cost": fill_cost,
                    "order_id": o.get("order_id"),
                    "category": o.get("category", ""),
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                    "paper_fill_model": "maker",
                })
                o["state"] = "filled"
                log.info("Paper maker order %s filled on %s @ %.4f — no maker fee",
                         o.get("order_id"), slug, fill_price)
            else:
                still_resting.append(o)
        self.state["resting_orders"] = still_resting
        self._save_state(self.state)

    def scan_markets(self) -> list:
        """Scan open active markets across crypto and active categories."""
        if not self.client:
            return []
        markets_by_slug = {}
        # 1. Fetch active crypto price markets
        try:
            res_crypto = self.client.get("/v1/markets?active=true&closed=false&categories=crypto&limit=100", authenticated=False)
            for m in res_crypto.get("markets", []):
                slug = m.get("slug")
                if slug:
                    markets_by_slug[slug] = m
        except Exception as e:
            log.debug("Failed crypto scan: %s", e)

        # 2. Fetch general active markets
        try:
            res_gen = self.client.get("/v1/markets?active=true&closed=false&limit=100", authenticated=False)
            for m in res_gen.get("markets", []):
                slug = m.get("slug")
                if slug:
                    markets_by_slug[slug] = m
        except Exception as e:
            log.debug("Failed general scan: %s", e)

        # 3. Fetch active fast-expiring crypto threshold & up/down markets via events
        for symbol in ("BTC", "ETH", "SOL"):
            try:
                events = polymarket_us_client.search_events(symbol)
                for ev in events[:4]:  # Top 4 events per symbol
                    ev_slug = ev.get("slug")
                    if not ev_slug or "updown-5m" in ev_slug or "-5m-" in ev_slug:
                        continue
                    ev_full = polymarket_us_client.get_event_by_slug(ev_slug)
                    for m in ev_full.get("event", {}).get("markets", []):
                        m_slug = m.get("slug")
                        if m_slug and not m.get("closed", False):
                            markets_by_slug[m_slug] = m
            except Exception as e:
                log.debug("Failed %s event search: %s", symbol, e)

        return list(markets_by_slug.values())

    def preview_order(self, slug: str, side: str, price_dollars: float, qty: int) -> bool:
        """Validate order against Polymarket US production order preview API.

        Always calls the live orders.preview endpoint (even in paper mode) so
        that every simulated fill is backed by HTTP 200 validation against
        api.polymarket.us. Returns True only on a successful preview.
        """
        try:
            res = polymarket_us_client.preview_order(slug, side, price_dollars, qty)
            return res is not None
        except Exception as e:
            log.warning("Order preview failed for %s %s @ $%.2f: %s",
                        slug, side, price_dollars, e)
            return False

    def check_settlements(self, market_map: dict):
        """Check settlement status for closed paper positions.

        Paper mode only: when a held position's market settles (venue returns a
        settlement price), the position is closed at that price and the payout is
        credited exactly once — win pays 1.0 per contract, loss 0.0. The position
        is removed from state, a "settlement" event is appended to the paper
        ledger (payout, pnl, cash_after), and realized_pnl / total_pnl are
        updated. Missing or malformed settlement prices keep the position open
        for retry next cycle; any live resting order on the settled market is
        cancelled first; credited positions are persisted before the ledger is
        appended so a crash cannot re-credit.
        """
        remaining_positions = []
        now = datetime.now(timezone.utc)
        # Per-slug caches: all positions in a settled market share one outcome,
        # so fetch settlement once and cancel resting orders once per slug.
        settle_cache = {}
        orders_by_slug: dict | None = None  # None = not fetched yet (live mode only)

        def _settlement(slug: str):
            if slug not in settle_cache:
                try:
                    time.sleep(0.05)
                    settle_cache[slug] = polymarket_us_client.get_market_settlement(slug)
                except Exception as e:
                    log.debug("Error checking settlement for %s: %s", slug, e)
                    settle_cache[slug] = None
            return settle_cache[slug]

        def _cancel_resting_orders(slug: str):
            nonlocal orders_by_slug
            if not self.live_mode:
                return
            if orders_by_slug is None:
                orders_by_slug = {}
                try:
                    for o in polymarket_us_client.get_open_orders():
                        oslug = o.get("marketSlug") or o.get("slug")
                        if oslug:
                            orders_by_slug.setdefault(oslug, []).append(o)
                except Exception as e:
                    log.warning("Could not list open orders for settlement cancel: %s", e)
                    orders_by_slug = {}
            for o in orders_by_slug.pop(slug, []):
                oid = o.get("id")
                if not oid:
                    continue
                try:
                    if polymarket_us_client.cancel_order(oid, slug):
                        log.info("Cancelled resting order %s on settled market %s", oid, slug)
                    else:
                        log.warning("cancel_order returned False for %s on %s", oid, slug)
                except Exception as e:
                    log.warning("Failed to cancel resting order %s on %s: %s", oid, slug, e)

        for pos in list(self.state.get("positions", [])):
            slug = pos["slug"]
            m_info = market_map.get(slug)

            # Check if market has closed, resolved, or passed its windowEnd/endDate
            is_closed = m_info.get("closed", False) if m_info else False
            end_dt = market_end_dt(m_info) if m_info else None
            if m_info and not is_closed:
                status = str(m_info.get("status") or "").upper()
                if status in ("RESOLVED", "RESOLVING", "CLOSED"):
                    is_closed = True
                elif end_dt and now >= end_dt:
                    is_closed = True

            # Hold only when the market is KNOWN to still be open. A market with
            # no parseable end time is "unknown", not "open": falling through to
            # the settlement lookup is the only way a position like that ever
            # closes, and holding on it strands the position forever.
            if not is_closed and m_info is not None and end_dt is not None:
                remaining_positions.append(pos)
                continue

            settle_res = _settlement(slug)
            settlement = None
            if isinstance(settle_res, dict):
                settlement = settle_res.get("settlement")
            if settlement is None:
                # Not settled yet (or not found): track consecutive failures and
                # alert once per position so a stranded position is visible.
                fail_count = int(pos.get("settle_fail_count", 0)) + 1
                pos["settle_fail_count"] = fail_count
                if fail_count % 12 == 0:  # once every 12 cycles (~3 min at 15s)
                    log.warning(
                        "STRANDED POSITION %s: settlement not published after %d consecutive checks "
                        "(past windowEnd: %s) — realized_pnl stuck until venue settles",
                        slug, fail_count, bool(pos.get("end_date")))
                remaining_positions.append(pos)
                continue
            try:
                settlement = float(settlement)
            except (TypeError, ValueError):
                log.debug("Malformed settlement price for %s: %r — retrying next cycle", slug, settle_res.get("settlement"))
                remaining_positions.append(pos)
                continue

            # Live resting order on a market that just settled: cancel it first.
            _cancel_resting_orders(slug)

            payout_per_contract = settlement if pos["side"] == "yes" else (1.0 - settlement)
            total_payout = round(payout_per_contract * pos["qty"], 4)
            cost = pos["cost"]
            pnl = round(total_payout - cost, 4)
            # Task 2: reconcile settlement for rolling Brier calibration.
            outcome = 1 if settlement >= 0.5 else 0  # 1 = Yes won
            try:
                self.cal_monitor.reconcile_settlement(slug, outcome)
            except Exception as e:
                log.warning("Settlement calibration reconcile failed for %s: %s", slug, e)
            self.state["cash"] = round(self.state["cash"] + total_payout, 4)
            self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 4)
            self.state["lifetime_realized_pnl"] = round(
                float(self.state.get("lifetime_realized_pnl", 0.0) or 0.0) + pnl, 4)
            self._track_daily_realized(pnl)  # <-- daily loss tracking
            # Move this position's unrealized into realized so total_pnl stays consistent.
            pos_unreal = float(pos.get("unrealized_pnl", 0.0) or 0.0)
            self.state["unrealized_pnl"] = round(self.state.get("unrealized_pnl", 0.0) - pos_unreal, 4)
            self.state["total_pnl"] = round(self.state["realized_pnl"] + self.state["unrealized_pnl"], 4)
            log.info(
                "🏆 SETTLED %s %s: outcome=%.2f payout=$%.2f cost=$%.2f PnL=$%+.2f | Cash=$%.2f",
                slug, pos["side"].upper(), payout_per_contract, total_payout, cost, pnl, self.state["cash"]
            )
            # Detach the position and persist BEFORE the ledger append so a crash
            # between the two cannot double-credit on the next cycle.
            self.state["positions"] = [p for p in self.state.get("positions", []) if p is not pos]
            self.state["settlements_count"] = int(self.state.get("settlements_count", 0) or 0) + 1
            self.state["settled_realized_pnl"] = round(
                float(self.state.get("settled_realized_pnl", 0.0) or 0.0) + pnl, 4)
            self._save_state(self.state)
            self._append_ledger({
                "event": "settlement",
                "slug": slug,
                "side": pos["side"],
                "qty": pos["qty"],
                "entry_price": pos["price"],
                "settlement": settlement,
                "payout": total_payout,
                "pnl": pnl,
                "cash_after": self.state["cash"],
                "source": pos.get("source", ""),
                "timestamp": now.isoformat(),
            })
            continue

        self.state["positions"] = remaining_positions
        self._save_state(self.state)

    def evaluate_opportunity(self, market: dict) -> dict | None:
        """Find high-probability favourite opportunities (92c-99c) directly from market data."""
        slug = market.get("slug")
        if not slug:
            return None

        terms = market.get("assetPriceTerms") or {}
        end_dt = market_end_dt(market)
        end_date = end_dt.isoformat() if end_dt else ""
        hours_left = None
        if end_dt:
            hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0

        # Only trade markets expiring within max_hours_to_expiry (default 24h) and not already expired
        max_h = getattr(self, "max_hours_to_expiry", 24.0)
        if hours_left is not None:
            if hours_left <= 0.0 or hours_left > max_h:
                return None

        # Filter out 5-minute micro-noise contracts (focus on 15m, hourly, and daily markets with strike cushion)
        if "updown-5m" in slug or "-5m-" in slug:
            return None

        # Strike/spot resolution shared by BOTH entry paths (R6): the gates must
        # not depend on which branch found the candidate.
        strike_val = None
        strike_dict = terms.get("strike") or terms.get("priceToBeat")
        if strike_dict and isinstance(strike_dict, dict):
            try:
                strike_val = float(strike_dict.get("value", 0))
            except Exception:
                pass

        symbol = (terms.get("asset") or {}).get("symbol", "").upper()
        if not symbol:
            slug_lower = slug.lower()
            if "btc" in slug_lower:
                symbol = "BTC"
            elif "eth" in slug_lower:
                symbol = "ETH"
            elif "sol" in slug_lower:
                symbol = "SOL"

        spot = crossvenue_edge.get_cached_spot(symbol) if symbol else None

        # 1. Evaluate cross-venue spot price & distributional fair value edge
        cv_edge = crossvenue_edge.evaluate_crossvenue_edge(market)
        # High-EV favorite hurdle: ref_prob >= MODEL_PROB_MIN (0.96) and favorite band (92c-99c).
        # Edge must clear modelled costs (fee + slippage + margin) — the raw 0.5c hurdle
        # was below any plausible spread/model error (R5).
        if (cv_edge
                and cv_edge["ref_prob"] >= getattr(config, "MODEL_PROB_MIN", 0.96)
                and 0.92 <= cv_edge["best_price"] <= 0.99):
            modelled_cost_cents = (_fee_cents(cv_edge["best_price"]) + _slippage_cents()
                                   + float(getattr(config, "PM_EDGE_MARGIN_CENTS", 0.5)))
            if cv_edge["best_edge_cents"] >= modelled_cost_cents:
                if not self._passes_entry_gates(slug, cv_edge["best_side"], terms,
                                                strike_val, spot, hours_left):
                    return None
                return {
                    "slug": slug,
                    "question": market.get("question", slug),
                    "side": cv_edge["best_side"],
                    "price": cv_edge["best_price"],
                    "category": "crypto",
                    "cross_edge_cents": cv_edge["best_edge_cents"],
                    "ref_prob": cv_edge["ref_prob"],
                    "spot": cv_edge["spot"],
                    "strike": cv_edge["strike"],
                    "end_date": end_date,
                    "hours_left": hours_left,
                    "source": "crossvenue",
                }
        # Fallback branch has no model of its own: only trade markets a model can
        # score (crypto with strike + spot), unless PM_ALLOW_UNMODELLED=true (R6).
        modelable = bool(strike_val and spot and spot > 0)
        if not modelable and not getattr(config, "PM_ALLOW_UNMODELLED", False):
            return None

        long_p = None
        short_p = None

        # Check marketSides first for unambiguous side assignment
        market_sides = market.get("marketSides") or []
        for side_info in market_sides:
            p_val = side_info.get("price")
            if p_val is not None:
                try:
                    price_float = float(p_val)
                    is_long = side_info.get("long") is True or str(side_info.get("description")).lower() in ("yes", "long")
                    if is_long:
                        long_p = price_float
                        if short_p is None:
                            short_p = round(1.0 - long_p, 4)
                    else:
                        short_p = price_float
                        if long_p is None:
                            long_p = round(1.0 - short_p, 4)
                except (ValueError, TypeError):
                    pass

        # Fall back to outcomePrices if marketSides gave no quote
        if long_p is None or short_p is None:
            outcome_raw = market.get("outcomePrices")
            if not outcome_raw:
                return None
            prices = []
            if isinstance(outcome_raw, str):
                try:
                    prices = json.loads(outcome_raw)
                except Exception:
                    return None
            elif isinstance(outcome_raw, list):
                prices = outcome_raw

            if len(prices) == 1:
                try:
                    p0 = float(prices[0])
                    long_p = p0
                    short_p = round(1.0 - p0, 4)
                except (ValueError, TypeError):
                    return None
            elif len(prices) >= 2:
                try:
                    long_p = float(prices[0])
                    short_p = float(prices[1])
                except (ValueError, TypeError):
                    return None
            else:
                return None

        candidates = []
        # Check Long (Yes) favorite in high-certainty favorite band (92c-99c)
        if 0.92 <= long_p <= 0.99:
            candidates.append(("yes", long_p))

        # Check Short (No) favorite in high-certainty favorite band (92c-99c)
        if 0.92 <= short_p <= 0.99:
            candidates.append(("no", short_p))

        if not candidates:
            return None

        side, price = candidates[0]

        # Same strike-cushion gates as the cross-venue branch (R6).
        if not self._passes_entry_gates(slug, side, terms, strike_val, spot, hours_left):
            return None

        # ML fair-value confirmatory gate (opt-in via ML_MODEL_PATH).
        # Denies the entry when the ML model assigns too little probability
        # to the chosen side. Skipped when the model is not loaded or when
        # spot/strike/minutes are unavailable (graceful degradation).
        if (self.ml_model is not None and cv_edge is not None and long_p is not None
                and spot and strike_val and hours_left is not None):
            try:
                minutes_left = hours_left * 60.0
                sigma_min = self.adjusted_sigma / math.sqrt(525600.0)
                row = {
                    "strike": strike_val,
                    "kalshi": long_p,        # venue YES mid price (Polymarket)
                    "fair": cv_edge["ref_prob"],  # GBM fair value
                    "spread": 0.0,
                }
                ml_p = self.ml_model.predict_proba(row, spot, sigma_min, minutes_left)
                ml_side_p = ml_p if side == "yes" else (1.0 - ml_p)
                ml_min = getattr(config, "ML_PROB_MIN", 0.50)
                if ml_side_p < ml_min:
                    log.debug("ML gate denies %s: side_prob=%.3f < min=%.2f (ml_p=%.3f)",
                              slug, ml_side_p, ml_min, ml_p)
                    return None
            except Exception as e:
                log.debug("ML gate skipped for %s: %s", slug, e)

        return {
            "slug": slug,
            "question": market.get("question", slug),
            "side": side,
            "price": price,
            "category": market.get("category", ""),
            "spot": spot,
            "strike": strike_val,
            "end_date": end_date,
            "hours_left": hours_left,
            "source": "fallback",
        }

    def _passes_entry_gates(self, slug: str, side: str, terms: dict,
                            strike_val, spot, hours_left) -> bool:
        """Strike-cushion gates shared by both entry paths (R6).

        2% directional distance check (above/below markets) and the adaptive
        z-gate vs self.adjusted_min_z. Missing model inputs (strike/spot/hours)
        mean the gate cannot score the market, so it passes — the caller decides
        whether unmodelled markets may trade at all (PM_ALLOW_UNMODELLED).
        """
        if not (strike_val and spot and spot > 0):
            return True

        pct_distance = abs(spot - strike_val) / spot
        is_above_below = "above" in slug.lower() or terms.get("marketType") == "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW"
        if is_above_below:
            # NO on "BTC above K" needs spot >=2% BELOW strike; YES needs >=2% ABOVE.
            if side == "no" and (spot >= strike_val or pct_distance < 0.020):
                log.debug("Distance gate denies no %s (spot=%.2f strike=%.2f d=%.3f)",
                          slug, spot, strike_val, pct_distance)
                return False
            if side == "yes" and (spot <= strike_val or pct_distance < 0.020):
                log.debug("Distance gate denies yes %s (spot=%.2f strike=%.2f d=%.3f)",
                          slug, spot, strike_val, pct_distance)
                return False

        if hours_left is not None:
            minutes_left = hours_left * 60.0
            if minutes_left > 0:
                sigma_per_sqrt_min = self.adjusted_sigma / math.sqrt(525600.0)
                denom = sigma_per_sqrt_min * math.sqrt(minutes_left)
                if denom > 0:
                    z = abs(math.log(spot / strike_val)) / denom
                    if z < self.adjusted_min_z:
                        log.debug("Cushion z %.2f < min_z %.2f for %s — denying entry",
                                  z, self.adjusted_min_z, slug)
                        return False
        return True

    @staticmethod
    def compute_tier_size(total_balance: float) -> float:
        """Dynamic position sizing: 3% of total equity per window, re-anchored at each milestone.

        Implements the compounding ladder: per-window budget = 3% of total equity,
        which scales 100x at every milestone ($35->$100: $1.05; $100->$1K: $3.00;
        $1K->$10K: $30.00; $10K->$100K: $300.00; $100K->$1M: $3,000.00). The number
        of contracts is floor(budget / entry_price). No per-tier caps: the 3% rule is
        applied uniformly so milestone 4 sizes at $3,000 (not $2,000).
        """
        return round(total_balance * 0.03, 2)

    def execute_paper_trade(self, opp: dict, probe_size_fraction: float = 1.0) -> bool:
        """Size and execute a paper trade from the bankroll."""
        slug = opp["slug"]
        side = opp["side"]
        price = opp["price"]

        # Ruin halt (R4): no new entries until a manual --reset.
        if self.state.get("ruin_halted"):
            log.info("Ruin halt active — refusing new entries (manual --reset required)")
            return False
        # Daily realized loss halt (R3): refuse new entries once today's realized
        # P&L reaches POLY_DAILY_LOSS_LIMIT_DOLLARS.
        if self._daily_loss_halted():
            log.info("Daily realized loss limit reached — refusing new entries until next day")
            return False
        # A3: refuse new entries if the fill model is disabled due to model mixing.
        if self.paper_fill_model is None:
            log.info("Fill model disabled (state mixes taker/maker models) — refusing new entries (--reset required)")
            return False

        # Check if already in this market — positions (paper fills) and
        # resting_orders (live maker orders) both count, so a live cycle can
        # not stack multiple orders on one slug (A3).
        if any(p["slug"] == slug for p in self.state["positions"]):
            return False
        if any(o.get("slug") == slug and o.get("state") == "resting"
               for o in self.state.get("resting_orders") or []):
            return False

        # Cap max concurrent open positions (default 25)
        if len(self.state["positions"]) >= getattr(self, "max_open_positions", 25):
            return False

        # Enforce 30% cash reserve floor
        total_mtm = sum(p.get("mtm_value", p.get("cost", 0.0)) for p in self.state["positions"])
        total_balance = round(self.state["cash"] + total_mtm, 2)
        min_cash_floor = total_balance * 0.30
        if self.state["cash"] <= min_cash_floor:
            return False

        # Enforce 3% max per window budget
        window_budget = self.state.get("window_budget", round(total_balance * 0.03, 2))
        window_spent = self.state.get("window_spent", 0.0)
        available_window_budget = max(0.0, window_budget - window_spent)
        if available_window_budget < 0.90:
            log.debug("Window budget exhausted ($%.2f / $%.2f spent)", window_spent, window_budget)
            return False

        # Sizing: strictly limited to remaining window budget and available cash
        available_cash = max(0.0, self.state["cash"] - min_cash_floor)
        max_trade_dollars = self.compute_tier_size(total_balance)
        max_trade_dollars = min(max_trade_dollars, available_window_budget, available_cash, round(total_balance * 0.03, 2))
        # Apply probe size fraction when calibrating after a halt (A1).
        max_trade_dollars = round(max_trade_dollars * probe_size_fraction, 4)
        if max_trade_dollars < 0.90:  # Allow 1 contract at 92-99c on starting $35 bankrolls
            return False

        qty = int(max_trade_dollars / price)
        if qty < 1:
            return False

        # Model the fill price and fee. Live orders always use the maker price
        # (price_for_maker_entry: bid + 1 tick, capped at ask - 1 tick) so a
        # post-only order never takes liquidity (A3). Paper mode uses the taker
        # fill (ask + slippage) by default; PM_PAPER_FILL_MODEL=maker records a
        # resting paper order instead of filling immediately.
        try:
            book = polymarket_us_client.get_market_book(slug)
        except Exception:
            book = {}
        yes_bid, yes_ask = _book_yes_prices(book)
        if side == "yes":
            ask = yes_ask
        else:
            ask = round(1.0 - yes_bid, 4) if yes_bid is not None else None

        fill_price = None
        entry_fee = 0.0
        if getattr(self, "live_mode", False):
            # Live: always maker entry — never at or through the ask.
            maker_price = price_for_maker_entry(book, side)
            if maker_price is None:
                self.state["book_unreadable"] = int(self.state.get("book_unreadable", 0)) + 1
                self._save_state(self.state)
                log.info("A3 unreadable book for %s %s — skipping live maker order", slug, side)
                return False
            fill_price = maker_price
            entry_fee = 0.0  # makers pay nothing
        elif self.paper_fill_model == "maker":
            # Paper maker: resting order at maker price (fills when the book
            # trades through; no fee). Never at or through the ask.
            maker_price = price_for_maker_entry(book, side)
            if maker_price is None:
                self.state["book_unreadable"] = int(self.state.get("book_unreadable", 0)) + 1
                self._save_state(self.state)
                log.info("A3 unreadable book for %s %s — skipping paper maker order", slug, side)
                return False
            fill_price = maker_price
            entry_fee = 0.0  # no maker fee
        else:
            # Paper taker (default): fill at best ask + slippage ticks.
            fill_price = _fill_buy_price(price, ask)
            # A2: never fall back to the displayed mark when the book is unreadable.
            if fill_price is None:
                self.state["book_unreadable"] = int(self.state.get("book_unreadable", 0)) + 1
                self._save_state(self.state)
                log.info("A2 unreadable book for %s %s — skipping trade", slug, side)
                return False
            entry_fee = _taker_fee(qty, fill_price)

        # A2: re-check edge at the fill price, not the displayed mark. If the
        # edge no longer clears fee + margin after slippage, skip the trade.
        ref_prob = float(opp.get("ref_prob") or 0.0)
        net_edge_cents = (ref_prob - fill_price) * 100.0
        hurdle_cents = _fee_cents(fill_price) + float(getattr(config, "PM_EDGE_MARGIN_CENTS", 0.5))
        if net_edge_cents < hurdle_cents:
            log.info(
                "A2 edge check failed at fill price: net_edge=%.1fc < hurdle=%.1fc "
                "(ref_prob=%.4f fill_price=%.4f) — skipping %s %s",
                net_edge_cents, hurdle_cents, ref_prob, fill_price, slug, side)
            return False

        cost = round(qty * fill_price + entry_fee, 4)
        if cost > available_window_budget or cost > available_cash:
            return False

        # Validate order syntax with Polymarket US production API (at the fill price)
        valid = self.preview_order(slug, side, fill_price, qty)
        if not valid:
            return False

        order_id = None
        if getattr(self, "live_mode", False):
            # preview_order above validated the order at fill_price; place at the
            # same price so the booked resting record matches the venue quote.
            resp = polymarket_us_client.place_maker_order(slug, side, fill_price, qty,
                                                          preview=False)
            if not resp or not resp.get("id"):
                log.warning("Live order placement failed for %s %s @ %.2f", slug, side, fill_price)
                return False
            order_id = resp.get("id")
            log.info("Live maker order placed: %s (id=%s)", slug, order_id)

        if getattr(self, "live_mode", False):
            # Live mode: book the position only after a confirmed fill. The order
            # sits in a `resting` record with cash reserved until reconciliation
            # confirms the fill (R3).
            self.state["cash"] = round(self.state["cash"] - cost, 4)
            self.state["window_spent"] = round(window_spent + cost, 4)
            self.state["trades_count"] += 1
            self.state.setdefault("resting_orders", []).append({
                "order_id": order_id,
                "slug": slug,
                "question": opp["question"],
                "side": side,
                "price": fill_price,
                "qty": qty,
                "cost": cost,
                "fee": entry_fee,
                "source": opp.get("source", ""),
                "category": opp.get("category", ""),
                "state": "resting",
                "placed_at": datetime.now(timezone.utc).isoformat(),
                "window_ts": self.state.get("current_window_ts"),
            })
            log.info("Live maker order resting: %s (id=%s) — cash reserved, position booked on confirmed fill",
                     slug, order_id)
        elif self.paper_fill_model == "maker":
            # Paper maker model: do not fill immediately. Record a resting
            # paper order; it fills only when a later book observation trades
            # through the resting price within the order TTL (A3).
            self.state["cash"] = round(self.state["cash"] - cost, 4)
            self.state["window_spent"] = round(window_spent + cost, 4)
            self.state["trades_count"] += 1
            self.state.setdefault("resting_orders", []).append({
                "order_id": order_id,
                "slug": slug,
                "question": opp["question"],
                "side": side,
                "price": fill_price,
                "qty": qty,
                "cost": cost,
                "fee": entry_fee,
                "source": opp.get("source", ""),
                "category": opp.get("category", ""),
                "state": "resting",
                "placed_at": datetime.now(timezone.utc).isoformat(),
                "window_ts": self.state.get("current_window_ts"),
                "paper_fill_model": "maker",
            })
            log.info("Paper maker order resting: %s (id=%s) — fills when the book trades through",
                     slug, order_id)
        else:
            # Paper taker mode (default): fill modelled at BBO (R5), position booked immediately.
            self.state["cash"] = round(self.state["cash"] - cost, 4)
            self.state["window_spent"] = round(window_spent + cost, 4)
            self.state["trades_count"] += 1
            pos = {
                "slug": slug,
                "question": opp["question"],
                "side": side,
                "price": fill_price,
                "fill_price": fill_price,
                "displayed_price": price,
                "fee": entry_fee,
                "source": opp.get("source", ""),
                "qty": qty,
                "cost": cost,
                "order_id": order_id,
                "category": opp.get("category", ""),
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "paper_fill_model": "taker",
            }
            self.state["positions"].append(pos)
        # Task 2: record entry for rolling Brier calibration (model p, z, price band).
        try:
            minutes_left = opp.get("hours_left")
            minutes_left = minutes_left * 60.0 if minutes_left is not None else None
            sigma_per_sqrt_min = self.adjusted_sigma / math.sqrt(525600.0)
            denom = sigma_per_sqrt_min * math.sqrt(minutes_left) if minutes_left else 0.0
            spot = opp.get("spot")
            strike = opp.get("strike")
            z = (abs(math.log(spot / strike)) / denom
                 if (spot and strike and denom > 0) else None)
            self.cal_monitor.record_entry(
                slug=slug,
                side=side,
                price_dollars=fill_price,
                predicted_p=opp.get("ref_prob"),
                z=z,
                strike=strike,
                minutes_left=minutes_left,
                sigma=self.adjusted_sigma,
            )
        except Exception as e:
            log.debug("Calibration record_entry failed for %s: %s", slug, e)
        self._save_state(self.state)

        edge_tag = f" [CrossEdge: {opp['cross_edge_cents']:+.1f}c]" if "cross_edge_cents" in opp else ""
        log.info(
            "⚡ PAPER BUY [%s] %s | %s %dx @ $%.2f = $%.2f%s | Cash Remaining: $%.2f",
            opp.get("category", "gen"), slug, side.upper(), qty, fill_price, cost, edge_tag, self.state["cash"]
        )
        self._append_ledger({
            "event": "open",
            "slug": slug,
            "side": side,
            "qty": qty,
            "price": fill_price,
            "cost": cost,
            "fee": entry_fee,
            "source": opp.get("source", ""),
            "cash_after": self.state["cash"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return True

    def cycle(self):
        """Run one full cycle."""
        markets = self.scan_markets()
        market_map = {m["slug"]: m for m in markets if "slug" in m}

        # Ensure market data is available for all open positions efficiently by batching through event parents
        missing_slugs = [p["slug"] for p in self.state.get("positions", []) if p["slug"] not in market_map]
        if missing_slugs:
            events_to_fetch = set()
            for s in missing_slugs:
                parts = s.split("-")
                # cpc-btc-above-hr-2026-11-14-1400z-66400 -> btc-above-hr-2026-11-14-1400z
                if len(parts) >= 7 and parts[0].startswith("cpc"):
                    ev_slug = "-".join(parts[1:-1])
                    events_to_fetch.add(ev_slug)
            for ev_slug in events_to_fetch:
                try:
                    ev_full = polymarket_us_client.get_event_by_slug(ev_slug)
                    for m in ev_full.get("event", {}).get("markets", []):
                        m_slug = m.get("slug")
                        if m_slug:
                            market_map[m_slug] = m
                except Exception:
                    pass
            # For any still missing, fetch individually with spacing
            for s in missing_slugs:
                if s not in market_map:
                    try:
                        time.sleep(0.05)
                        fresh_m = polymarket_us_client.get_market_by_slug(s)
                        if fresh_m and isinstance(fresh_m, dict):
                            m_data = fresh_m.get("market") or fresh_m
                            if m_data and "slug" in m_data:
                                market_map[s] = m_data
                    except Exception:
                        pass

        self.check_settlements(market_map)

        # Live mode: reconcile resting orders against venue fills (R3) — book
        # only confirmed filled quantity, cancel stale ones, release reserved cash.
        if self.live_mode:
            self.reconcile_live_orders()

        # Paper maker mode: fill resting paper orders when the book trades through
        # their resting price (no fee, maker fill).
        if not self.live_mode and self.paper_fill_model == "maker":
            self.reconcile_paper_maker_orders(market_map)

        # In paper mode, rotate out any stale long-dated positions (>24h) to free cash for active intraday compounding
        now = datetime.now(timezone.utc)
        active_positions = []
        for pos in self.state.get("positions", []):
            slug = pos["slug"]
            m_info = market_map.get(slug)
            end_dt = market_end_dt(m_info) if m_info else None
            is_stale_long_dated = False
            if end_dt:
                h_left = (end_dt - now).total_seconds() / 3600.0
                if h_left > getattr(self, "max_hours_to_expiry", 24.0):
                    is_stale_long_dated = True
            if is_stale_long_dated:
                # Exit at the venue bid with the same fee model — never at the
                # displayed mark (R5). Selling NO means its bid = 1 - YES ask.
                try:
                    book = polymarket_us_client.get_market_book(slug)
                except Exception:
                    book = {}
                yes_bid, yes_ask = _book_yes_prices(book)
                current_mark = float(pos.get("current_price", pos["price"]))
                bid = yes_bid if pos["side"] == "yes" else (
                    round(1.0 - yes_ask, 4) if yes_ask is not None else None)
                exit_price = _fill_sell_price(current_mark, bid)
                exit_fee = _taker_fee(pos["qty"], exit_price)
                payout = round(exit_price * pos["qty"] - exit_fee, 4)
                pnl = round(payout - pos["cost"], 4)
                self.state["cash"] = round(self.state["cash"] + payout, 4)
                self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 4)
                self.state["lifetime_realized_pnl"] = round(
                    float(self.state.get("lifetime_realized_pnl", 0.0) or 0.0) + pnl, 4)
                self._track_daily_realized(pnl)  # <-- daily loss tracking
                log.info(
                    "🔄 ROTATED long-dated position %s (>24h) | Exit=$%.4f Fee=$%.4f Payout=$%.2f PnL=$%+.2f -> Freed cash=$%.2f",
                    slug, exit_price, exit_fee, payout, pnl, self.state["cash"]
                )
                self._append_ledger({
                    "event": "rotate_exit",
                    "slug": slug,
                    "side": pos["side"],
                    "qty": pos["qty"],
                    "exit_price": exit_price,
                    "fee": exit_fee,
                    "payout": payout,
                    "pnl": pnl,
                    "source": pos.get("source", ""),
                    "cash_after": self.state["cash"],
                    "timestamp": now.isoformat(),
                })
            else:
                active_positions.append(pos)
        self.state["positions"] = active_positions

        # Compute mark-to-market valuation and unrealized P&L per position
        total_mtm = 0.0
        total_unrealized = 0.0
        for pos in self.state.get("positions", []):
            slug = pos["slug"]
            m_info = market_map.get(slug)
            cur_price = pos["price"]
            if m_info:
                # Check marketSides first for unambiguous side quote
                market_sides = m_info.get("marketSides") or []
                found_quote = False
                for s in market_sides:
                    p_val = s.get("price")
                    if p_val is not None:
                        try:
                            px = float(p_val)
                            is_long = s.get("long") is True or str(s.get("description")).lower() in ("yes", "long")
                            if is_long:
                                cur_price = px if pos["side"] == "yes" else round(1.0 - px, 4)
                                found_quote = True
                                break
                            else:
                                cur_price = px if pos["side"] == "no" else round(1.0 - px, 4)
                                found_quote = True
                                break
                        except Exception:
                            pass
                if not found_quote:
                    outcome_raw = m_info.get("outcomePrices") or []
                    if isinstance(outcome_raw, str):
                        try:
                            outcome_raw = json.loads(outcome_raw)
                        except Exception:
                            outcome_raw = []
                    if len(outcome_raw) == 1:
                        try:
                            long_p = float(outcome_raw[0])
                            cur_price = long_p if pos["side"] == "yes" else round(1.0 - long_p, 4)
                        except Exception:
                            pass
                    elif len(outcome_raw) >= 2:
                        try:
                            cur_price = float(outcome_raw[0]) if pos["side"] == "yes" else float(outcome_raw[1])
                        except Exception:
                            pass
            pos_mtm = round(cur_price * pos["qty"], 4)
            pos_unrealized = round(pos_mtm - pos["cost"], 4)
            pos["current_price"] = cur_price
            pos["mtm_value"] = pos_mtm
            pos["unrealized_pnl"] = pos_unrealized
            total_mtm += pos_mtm
            total_unrealized += pos_unrealized

        realized_pnl = float(self.state.get("realized_pnl", 0.0))
        total_pnl = round(realized_pnl + total_unrealized, 4)
        total_equity = round(self.state["cash"] + total_mtm, 2)

        # Ruin rule (R4): evaluated on realized P&L and drawdown from peak —
        # never on instantaneous MTM. A trip HALTS entries and needs a manual
        # --reset; the bankroll is never auto-reset and positions are never
        # silently discarded.
        peak = max(float(self.state.get("peak_equity", total_equity) or 0.0), total_equity)
        drawdown = round(peak - total_equity, 4)
        self.state["peak_equity"] = round(peak, 4)
        self.state["max_drawdown"] = round(
            max(float(self.state.get("max_drawdown", 0.0) or 0.0), drawdown), 4)
        realized_loss = -float(self.state.get("realized_pnl", 0.0) or 0.0)
        if (not self.state.get("ruin_halted")
                and (realized_loss >= float(getattr(config, "POLY_RUIN_REALIZED_LOSS_DOLLARS", 10.0))
                     or drawdown >= float(getattr(config, "POLY_MAX_DRAWDOWN_DOLLARS", 15.0)))):
            self.state["ruin_halted"] = True
            self.state["ruin_events"] = int(self.state.get("ruin_events", 0) or 0) + 1
            log.error(
                "RUIN HALT: realized loss $%.2f / drawdown from peak $%.2f hit the limit — "
                "no new entries until a manual --reset (lifetime stats preserved)",
                realized_loss, drawdown)
            self._append_ledger({
                "event": "ruin_halt",
                "realized_loss": realized_loss,
                "drawdown": drawdown,
                "peak_equity": peak,
                "timestamp": now.isoformat(),
            })

        self.state["unrealized_pnl"] = round(total_unrealized, 4)
        self.state["total_pnl"] = total_pnl

        # Track compounding milestones ($35, $100, $1K, $10K, $100K, $1M)
        milestones = [35.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0]
        reached = self.state.setdefault("milestones_reached", [])
        for m in milestones:
            if total_equity >= m and m not in reached:
                reached.append(m)
                log.info("🎉 MILESTONE REACHED: Total Equity $%.2f crossed $%.2f milestone!", total_equity, m)
                self._append_ledger({
                    "event": "milestone",
                    "milestone": m,
                    "total_equity": total_equity,
                    "cash": self.state["cash"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        # Check 15-minute window transition (900 seconds)
        window_interval = getattr(self, "window_seconds", 900)
        current_window_ts = int(time.time() // window_interval) * window_interval
        window_dt = datetime.fromtimestamp(current_window_ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if self.state.get("current_window_ts") != current_window_ts:
            self.state["current_window_ts"] = current_window_ts
            self.state["current_window_name"] = window_dt
            self.state["window_budget"] = round(total_equity * 0.03, 2)
            self.state["window_spent"] = 0.0
            log.info(
                "── New Window [%s] ── Total Equity: $%.2f | Window Budget (3%%): $%.2f | Spent: $0.00",
                window_dt, total_equity, self.state["window_budget"]
            )

        self._save_state(self.state)

        # ── Task 2: continuous calibration monitor + circuit breaker ──
        snap = self.cal_monitor.snapshot_rolling()
        scale, self.adjusted_sigma, self.adjusted_min_z, drift = self.vol_scaler.scale(
            snap.get("rolling_brier"))
        halted, reason, probing = self.cal_monitor.circuit_breaker(
            brier_min_samples=config.CAL_BRIER_MIN_SAMPLES,
            wr_min_samples=config.CAL_WIN_RATE_MIN_SAMPLES,
            wr_floor=config.CAL_WIN_RATE_FLOOR,
            halt_minutes=config.CAL_HALT_MINUTES,
            probe_size_fraction=config.CAL_PROBE_SIZE_FRACTION,
            probe_outcomes_n=config.CAL_PROBE_OUTCOMES_N,
        )
        self.calibration_halted = halted
        self.calibration_probing = probing
        self.calibration_reason = reason
        band_gap = None
        if snap.get("price_band_92_99c"):
            band_gap = snap["price_band_92_99c"].get("calibration_gap_pp")
        log.info(
            "CALIBRATION rolling_brier=%s ece=%s trailing10_wr=%s "
            "band92_99_gap_pp=%s vol_scale=%s adj_sigma=%.4f adj_min_z=%.2f drift=%s -> %s",
            snap.get("rolling_brier"), snap.get("ece"),
            snap.get("trailing_10_win_rate"), band_gap,
            scale, self.adjusted_sigma, self.adjusted_min_z, drift, reason)

        opportunities = []
        for m in markets:
            opp = self.evaluate_opportunity(m)
            if opp:
                opportunities.append(opp)

        self.state["available_markets_count"] = len(markets)
        self.state["tradeable_brackets_count"] = len(opportunities)
        self._save_state(self.state)

        log.info(
            "── PolyUS Cycle ── Equity: $%.2f (Cash: $%.2f, MTM: $%.2f) | Realized P&L: %+.2f | Unrealized P&L: %+.2f | Total P&L: %+.2f | Window Budget: $%.2f ($%.2f spent) | Positions: %d | Available Markets: %d | Tradable Brackets: %d",
            total_equity, self.state["cash"], total_mtm, realized_pnl, total_unrealized, total_pnl,
            self.state.get("window_budget", round(total_equity * 0.03, 2)),
            self.state.get("window_spent", 0.0),
            len(self.state["positions"]), len(markets), len(opportunities)
        )

        current_slugs = {p["slug"] for p in self.state.get("positions", [])}
        new_opps = [opp for opp in opportunities if opp["slug"] not in current_slugs]

        # Prioritize opportunities closing soonest so settlements compound in real-time
        now_ts = time.time()

        def _opp_sort_key(opp):
            ed = opp.get("end_date") or ""
            if not ed:
                return float("inf")
            try:
                dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                ts = dt.timestamp()
                # Deprioritize already-expired markets so active markets trade first
                if ts <= now_ts:
                    return float("inf")
                return ts
            except Exception:
                return float("inf")

        new_opps.sort(key=_opp_sort_key)
        log.info(
            "Found %d high-certainty (92-99c) opportunity brackets (%d unheld)",
            len(opportunities), len(new_opps)
        )
        trades_placed = 0
        if self.calibration_halted and not self.calibration_probing:
            log.info("CALIBRATION HALTED: %s — no new entries this cycle", self.calibration_reason)
            # Shadow calibration: log candidates that passed gates but are skipped.
            for opp in new_opps:
                self.cal_monitor.shadow_log_candidate(
                    slug=opp["slug"], side=opp["side"], price=opp["price"],
                    predicted_p=opp.get("predicted_p", 0.0), z=opp.get("z", 0.0),
                    reason=self.calibration_reason,
                )
        else:
            probe_size = (
                config.CAL_PROBE_SIZE_FRACTION
                if (self.calibration_halted and self.calibration_probing)
                else 1.0
            )
            for opp in new_opps:
                if trades_placed >= 2:
                    break
                if self.execute_paper_trade(opp, probe_size_fraction=probe_size):
                    trades_placed += 1

        systemd_notify("WATCHDOG=1")


def acquire_instance_lock(lock_path: Path, attempts: int = 5, retry_delay: float = 1.0):
    """Take the single-instance lock (.lock_poly). Returns an fd that must stay
    open, or None when flock itself is unusable (non-fatal, logged).

    Contention after `attempts` retries exits non-zero: two traders sharing the
    state file and bankroll would double-book fills.
    """
    for attempt in range(attempts):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)
        except OSError as e:
            log.warning("Instance lock %s unusable (%s) — proceeding without it", lock_path, e)
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
            if attempt < attempts - 1:
                log.warning("Lock %s held by another process — retrying in %.0fs (attempt %d/%d)",
                            lock_path, retry_delay, attempt + 2, attempts)
                time.sleep(retry_delay)
                continue
            log.error("Could not acquire lock %s after %d attempts — another instance running",
                      lock_path, attempts)
            raise SystemExit(1)
        except OSError as e:
            # flock unsupported on this filesystem (FUSE/overlay): non-fatal.
            log.warning("flock failed on %s (%s) — proceeding without instance lock", lock_path, e)
            os.close(fd)
            return None
    return None


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(POLY_US_LOG_FILE, mode="a"),
        ],
    )
    parser = argparse.ArgumentParser(description="Polymarket US Paper Trader ($35 Bankroll)")
    parser.add_argument("--once", action="store_true", help="Run single cycle and exit")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between cycles (default 15)")
    parser.add_argument("--status", action="store_true", help="Show current paper state and exit")
    parser.add_argument("--reset", nargs="?", const=35.0, type=float, default=None, help="Reset paper bankroll to starting balance (default: 35.0)")
    parser.add_argument("--live", action="store_true", help="Enable live trading mode (places real maker orders via Polymarket US API)")
    args = parser.parse_args()

    # R3: refuse --live unless POLY_CONFIRM_LIVE=true (exit non-zero).
    if args.live and not getattr(config, "POLY_CONFIRM_LIVE", False):
        log.error("Polymarket US paper mode --live requires POLY_CONFIRM_LIVE=true; refusing.")
        parser.print_help()
        sys.exit(2)

    init_cash = args.reset if args.reset is not None else 35.0
    trader = PolyUSPaperTrader(initial_cash=init_cash, reset=args.reset is not None, live_mode=args.live)

    if args.status:
        state = trader.state
        positions = state.get("positions", [])
        total_mtm = sum(p.get("mtm_value", p.get("cost", 0.0)) for p in positions)
        total_equity = round(state.get("cash", init_cash) + total_mtm, 2)
        realized = state.get("realized_pnl", 0.0)
        unrealized = state.get("unrealized_pnl", 0.0)
        total_pnl = state.get("total_pnl", round(realized + unrealized, 4))
        print("=== Polymarket US Paper Trader ===")
        print(f"Start Balance:  ${state.get('start_balance', init_cash):.2f}")
        print(f"Total Equity:   ${total_equity:.2f}")
        print(f"Available Cash: ${state.get('cash', init_cash):.2f}")
        print(f"Open Positions: {len(positions)} (${total_mtm:.2f} MTM)")
        print(f"Available Markets: {state.get('available_markets_count', 0)}")
        print(f"Tradable Brackets: {state.get('tradeable_brackets_count', 0)}")
        w_budget = state.get('window_budget', round(total_equity * 0.03, 2))
        w_spent = state.get('window_spent', 0.0)
        print(f"Window Budget:  ${w_budget:.2f} (Spent: ${w_spent:.2f}, Left: ${max(0.0, w_budget - w_spent):.2f})")
        print(f"Realized P&L:   ${realized:+.2f}")
        print(f"Unrealized P&L: ${unrealized:+.2f}")
        print(f"Total Net P&L:  ${total_pnl:+.2f}")
        print(f"Lifetime Realized P&L: ${state.get('lifetime_realized_pnl', realized):+.2f} "
              f"(never cleared by resets)")
        print(f"Peak Equity:    ${state.get('peak_equity', total_equity):.2f} | "
              f"Max Drawdown: ${state.get('max_drawdown', 0.0):.2f} | "
              f"Ruin Events: {state.get('ruin_events', 0)} | "
              f"Ruin Halted: {bool(state.get('ruin_halted', False))}")
        print(f"Trades Count:   {state.get('trades_count', 0)}")
        print(f"Settlements:    {state.get('settlements_count', 0)} "
              f"(settled P&L ${float(state.get('settled_realized_pnl', 0.0) or 0.0):+.2f})")
        print(f"Ledger Reconciliation: ledger realized ${float(state.get('ledger_realized_pnl', 0.0) or 0.0):+.2f} "
              f"vs state ${realized:+.2f} (gap ${float(state.get('ledger_realized_gap', 0.0) or 0.0):+.2f})")
        if state.get("pre_epoch_realized_pnl"):
            print(f"Pre-epoch ledger P&L (before this ${state.get('start_balance', 0.0):.2f} baseline "
                  f"started {state.get('epoch_started_at')}): ${float(state['pre_epoch_realized_pnl']):+.2f}")
        print(f"Milestones Reached: {state.get('milestones_reached', [init_cash])}")
        for p in positions:
            cat = f"[{p['category']}] " if p.get("category") else ""
            cur_p = p.get("current_price", p["price"])
            u_pnl = p.get("unrealized_pnl", 0.0)
            print(f"  - {cat}{p['slug']} | {p['side'].upper()} {p['qty']}x @ ${p['price']:.2f} -> Cur: ${cur_p:.2f} (cost: ${p['cost']:.2f}, MTM: ${p.get('mtm_value', p['cost']):.2f}, Unrealized: ${u_pnl:+.2f})")
        return

    lock_fd = acquire_instance_lock(LOCK_FILE)
    # R17: SIGTERM/SIGINT -> clean shutdown after the current cycle finishes.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass  # not main thread / unsupported platform: KeyboardInterrupt still works
    try:
        log.info("Starting Polymarket US Paper Trader with $%.2f Bankroll", trader.state["start_balance"])
        if args.once:
            trader.cycle()
            return

        cycle_failures = 0
        # Type=notify unit: init (state, lock, signals) is done; systemd may
        # now consider the service started and enforce WatchdogSec.
        systemd_notify("READY=1")
        while not _stop_requested:
            try:
                trader.cycle()
                cycle_failures = 0
            except KeyboardInterrupt:
                log.info("Polymarket US paper trader stopped by user.")
                break
            except Exception as e:
                cycle_failures += 1
                # R17: back off exponentially on repeated failures so a venue
                # outage does not hammer the API at full cadence.
                backoff = min(args.interval * (2 ** (cycle_failures - 1)), 300)
                log.error("Cycle error (%d consecutive, backing off %.0fs): %s",
                          cycle_failures, backoff, e)
                time.sleep(backoff)
                continue
            time.sleep(args.interval)
        if _stop_requested:
            log.info("Stop signal received — shutting down cleanly.")
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                # Not unlinked: unlinking lets a waiter lock the old inode while
                # a newcomer creates and locks a fresh one — two bots, two locks.
            except OSError:
                pass


if __name__ == "__main__":
    main()
