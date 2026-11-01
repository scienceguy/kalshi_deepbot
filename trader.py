"""
trader.py — Kalshi SuperBot main loop.

Consolidated from kalshi_15m_bot, kalshi_live_bot, kalshi_gemini_bot,
kalshi_exp_bot, FlashBot.v6, Momentum.v15.

Features:
- Two-sided favourite maker strategy (Approach D) from kalshi_live_bot
- Stability check / bid history tracking (doubles EV per backtests)
- Confidence scoring
- Fee-aware P&L calculation (kalshi_live_bot fees.py)
- Supabase integration for all signals, orders, bot state
- LLM evaluation with 7-layer fallback from free model providers
- Resilience / cycle supervisor with auto client rebuild
- Stop loss and profit take scanning
- Dry run mode

Usage:
    python trader.py          # live trading
    python trader.py --dry    # dry run (recommended first)
    python trader.py --once   # one cycle and exit
    python trader.py --debug  # verbose logging
"""

import argparse, csv, json, logging, os, signal as sig_mod, sys, time, uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config  # noqa: E402

# Parse args FIRST so env is set before config loads
parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true", help="Enable LIVE trading (overrides DRY_RUN=true)")
parser.add_argument("--dry", action="store_true")
parser.add_argument("--once", action="store_true")
parser.add_argument("--debug", action="store_true")
parser.add_argument("--interval", type=int, default=config.LOOP_INTERVAL_SECONDS)
parser.add_argument("--cycle-limit", type=int, default=0,
                    help="stop after this many cycles (0 = disabled)")
args = parser.parse_args()

# Set DRY_RUN directly on the config module — avoids mutating os.environ
# before config loads, which could leak into other modules/subprocesses.
if args.live:
    config.DRY_RUN = False
elif args.dry:
    config.DRY_RUN = True

if args.debug:
    config.LOG_LEVEL = "DEBUG"  # noqa: E402

import fees  # noqa: E402
from supabase_client import get_supabase  # noqa: E402
from n8n_client import get_client as get_n8n_client, n8n_health_check  # noqa: E402
from kalshi_client import KalshiClient  # noqa: E402
from market_finder import find_active_markets, _parse_api_time, MarketInfo  # noqa: E402
from risk import (  # noqa: E402
    MIN_CASH_ABSOLUTE, RiskManager, CASH_FLOOR_PCT, DRAWDOWN_HALT_PCT,
)
import signals as se  # noqa: E402
from resilience import CycleSupervisor  # noqa: E402
from llm_eval import should_block_trade, evaluate_trade  # noqa: E402
from latency_arb import evaluate_market as latency_evaluate_market  # noqa: E402

lf = Path(config.LOG_FILE).expanduser()
lf.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(lf)],
)
log = logging.getLogger("trader")

# Global Supabase client (lazy)
_supabase = None


def get_supabase_client():
    global _supabase
    if _supabase is None:
        _supabase = get_supabase()
    return _supabase

# ═════════════════════════════════════════════════════════════════════
# BOT STATE
# ═════════════════════════════════════════════════════════════════════

WINDOW_EPSILON = 0.01

SETTLEMENT_QUEUE_FILE = Path(__file__).resolve().parent / "settlement_queue.json"


class BotState:
    """Encapsulates all mutable bot state."""

    def __init__(self):
        self.positions: dict = {}          # ticker -> order_id or "dry"
        self.resting_value: dict = {}      # order_id -> reserved dollars
        self.last_cash_log: float = -1.0
        self.session_pnl: float = 0.0
        self.cycles: int = 0
        self.trades_placed: int = 0
        self.running: bool = True
        self.risk = RiskManager()
        self.resilience = CycleSupervisor()
        self.window = {
            "id": None,
            "start_cash": 0.0,
            "budget": 0.0,
            "used": 0.0,
            "reserved_cash": 0.0,
            "entered": set(),
        }
        self.dry_cash: float | None = None
        self.dry_book: dict = {}
        self.active_risk: float = 0.0
        self.position_risk: dict = {}
        self.llm_hold_to_expiry: set = set()
        self.llm_trade_insights: dict = {}
        self.last_order_time: float = 0.0
        self.orders_today: int = 0
        self.today: str = ""

    def _pre_trade_check(self, sig, market, cash, exposure, total_value, client) -> bool:
        """Pre-trade risk checks including LLM evaluation."""
        ticker = market.ticker

        # Per-window budget gate
        remaining_window = max(0.0, self.window["budget"] - self.window["used"])

        # Per-trade cap
        per_trade_cap = cash * config.POSITION_FRACTION
        hard_cap = min(per_trade_cap, remaining_window)

        if sig.dollar_risk > hard_cap + WINDOW_EPSILON:
            cost_per = sig.entry_price / 100.0
            n = int(hard_cap / cost_per) if cost_per > 0 else 0
            if n < 1:
                log.info(
                    "WINDOW BLOCK %s — need $%.2f, remaining $%.2f of $%.2f",
                    ticker, sig.dollar_risk, remaining_window, self.window["budget"],
                )
                return False
            log.info(
                "WINDOW CLAMP %s — %.4fx@%dc ($%.2f) -> %.0fx ($%.2f), remaining $%.2f",
                ticker, sig.contracts, sig.entry_price, sig.dollar_risk,
                n, n * cost_per, remaining_window,
            )
            sig.contracts = float(n)
            sig.dollar_risk = n * cost_per

        if sig.dollar_risk > remaining_window + WINDOW_EPSILON:
            log.info(
                "WINDOW BLOCK %s — need $%.2f, remaining $%.2f of $%.2f",
                ticker, sig.dollar_risk, remaining_window, self.window["budget"],
            )
            return False

        reserved_cash = self.window.get("reserved_cash", 0.0)
        if reserved_cash:
            remaining_after = cash - sig.dollar_risk
            if remaining_after < reserved_cash - 1e-3:
                log.info(
                    "RESERVE BLOCK %s — keep $%.2f reserved (need $%.2f, would have $%.2f)",
                    ticker, reserved_cash, sig.dollar_risk, remaining_after,
                )
                return False

        # Check resting orders
        pending = sum(self.resting_value.values())
        if pending + sig.dollar_risk > cash + 1e-9:
            log.info(
                "CASH BLOCK %s — new $%.2f + resting $%.2f > available cash $%.2f",
                ticker, sig.dollar_risk, pending, cash,
            )
            return False

        tradable_balance = cash * config.TOTAL_CASH_TRADABLE_PCT
        deployed = exposure + pending + sig.dollar_risk
        if tradable_balance > 0 and deployed > tradable_balance + 1e-3:
            log.info(
                "TRADABLE BLOCK %s — deployed $%.2f would exceed %.2f",
                ticker, deployed, tradable_balance,
            )
            return False

        allowed, reason = self.risk.check(
            cash, exposure, sig.dollar_risk, ticker, total_value,
            resting_orders_value=sum(self.resting_value.values()),
        )
        if not allowed:
            log.info("RISK BLOCK %s — %s", ticker, reason)
            return False

        # LLM evaluation (7-layer fallback)
        if config.LLM_EVALUATION_ENABLED and config.OPENROUTER_API_KEY:
            signal_info = {
                "ticker": ticker,
                "direction": sig.direction,
                "entry_price": sig.entry_price,
                "contracts": sig.contracts,
                "dollar_risk": sig.dollar_risk,
                "cash": cash,
                "exposure": exposure,
                "total_value": total_value,
                "mid": market.mid_yes,
                "skew": market.skew,
                "trend": market.mid_yes - 50.0,
                "minutes_left": market.minutes_left,
                "source": getattr(sig, 'source', 'unknown'),
                "strength": sig.strength,
            }
            should_block, eval_result = should_block_trade(
                signal_info, min_probability=config.LLM_EVAL_MIN_PROBABILITY
            )
            if should_block:
                log.info(
                    "LLM BLOCK %s — prob=%.2f < %.2f (%s)",
                    ticker,
                    eval_result["probability_of_net_profit"],
                    config.LLM_EVAL_MIN_PROBABILITY,
                    eval_result["layer"],
                )
                # Record blocked signal to Supabase
                try:
                    get_supabase_client().insert_signal({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "ticker": ticker,
                        "side": sig.side,
                        "price_cents": sig.entry_price,
                        "contracts": sig.contracts,
                        "reason": f"LLM blocked: {eval_result.get('reasoning', '')}",
                        "executed": False,
                        "blocked_reason": f"LLM prob {eval_result['probability_of_net_profit']:.2f}",
                    })
                except Exception:
                    log.debug("Supabase insert_signal failed for %s", ticker)
                return False

            # Record LLM insight
            self.llm_trade_insights[ticker.upper()] = {
                "probability": eval_result["probability_of_net_profit"],
                "hold_confidence": eval_result["confidence_hold_to_expiry"],
                "timestamp": time.time(),
            }

        return True

    def _place_order(self, client, sig, coid):
        try:
            return client.place_order(
                ticker=sig.market.ticker, side=sig.side,
                contracts=sig.contracts, price_cents=sig.entry_price,
                client_order_id=coid, order_type="limit",
            )
        except Exception as e:
            log.error("Order failed %s: %s", sig.market.ticker, e)
            return None

    def _record_filled_order(self, client, sig, oid, fill_count, status, fees_dollars, cycles):
        ticker = sig.market.ticker
        self.positions[ticker] = {
            "side": sig.side,
            "entry_price": sig.entry_price,
            "contracts": sig.contracts,
            "order_id": oid,
        }
        self.window["entered"].add(ticker)
        self.resting_value.pop(oid, None)
        log.info("✓ %s order_id=%s (fills=%.2f)", ticker, oid, fill_count)
        self.window["used"] += sig.dollar_risk
        self.trades_placed += 1
        # Record to Supabase
        try:
            get_supabase_client().insert_order({
                "ts": datetime.now(timezone.utc).isoformat(),
                "ticker": ticker,
                "side": sig.side,
                "contracts": sig.contracts,
                "price_cents": sig.entry_price,
                "order_id": oid,
                "status": status,
                "fees_dollars": fees_dollars or 0.0,
                "cycles": cycles,
            })
        except Exception:
            log.warning("Supabase insert_order failed for %s — order audit trail incomplete", ticker)
        # Track capital at risk per ticker — released on settlement, not booked as P&L
        state.position_risk[ticker] = sig.dollar_risk


# Global state
state = BotState()


# ═════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════

def _coid(ticker: str, side: str) -> str:
    return uuid.uuid4().hex


def _window_id(now: datetime) -> int:
    return int(now.timestamp() // (15 * 60))


def _touch_window(now: datetime, cash: float):
    """Reset per-window budget if a new 15m window begins."""
    wid = _window_id(now)
    if state.window["id"] == wid:
        if cash > state.window["start_cash"] + 1e-9:
            state.window["start_cash"] = cash
            state.window["budget"] = cash * config.WINDOW_TRADE_PCT
            state.window["reserved_cash"] = max(0.0, cash * config.CASH_RESERVED_PCT)
            log.info(
                "Window budget refreshed — cash grew to $%.2f, budget=$%.2f reserved=$%.2f",
                cash, state.window["budget"], state.window["reserved_cash"],
            )
        return
    state.window["id"] = wid
    state.window["start_cash"] = cash
    state.window["budget"] = cash * config.WINDOW_TRADE_PCT
    state.window["used"] = 0.0
    state.window["reserved_cash"] = max(0.0, cash * config.CASH_RESERVED_PCT)
    state.window["entered"] = set()
    log.info(
        "New 15m window — start_cash=$%.2f window_budget=$%.2f reserved=$%.2f",
        cash, state.window["budget"], state.window["reserved_cash"],
    )


def _load_dry_state():
    try:
        ds = Path(__file__).resolve().parent / "dry_state.json"
        if ds.exists():
            with open(ds) as f:
                return float(json.load(f).get("cash", 0.0))
    except Exception:
        pass
    return None


def _save_dry_state(cash: float):
    try:
        ds = Path(__file__).resolve().parent / "dry_state.json"
        with open(ds, "w") as f:
            json.dump({"cash": round(cash, 4)}, f)
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════
def _load_settlement_queue() -> list:
    """Load pending settlements from the local queue file."""
    try:
        with open(SETTLEMENT_QUEUE_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_settlement_queue(queue: list):
    """Persist pending settlements to the local queue file."""
    try:
        with open(SETTLEMENT_QUEUE_FILE, "w") as f:
            json.dump(queue, f)
    except Exception:
        pass


def _replay_settlement_queue():
    """Replay any pending settlements from the queue file via n8n webhook."""
    queue = _load_settlement_queue()
    if not queue:
        return
    log.info("Replaying %d pending settlement(s) from queue", len(queue))
    remaining = []
    for entry in queue:
        try:
            n8n_client = get_n8n_client()
            n8n_client.execute_webhook("superbot-trade", entry["payload"])
            pnl = entry["payload"].get("pnl_dollars")
            if pnl is not None:
                state.session_pnl += pnl
            log.info("Replayed settlement for %s", entry["payload"].get("order_id"))
        except Exception:
            remaining.append(entry)
    _save_settlement_queue(remaining)


# CYCLE
# ═════════════════════════════════════════════════════════════════════

def cycle(client: KalshiClient):
    state.cycles += 1
    log.info("── Cycle %d ──────────────────────", state.cycles)

    # Maintenance
    try:
        se._maybe_save_bid_hist(time.time())
    except Exception:
        pass

    # Fetch balance
    try:
        cash = client.get_balance()
    except Exception as e:
        log.warning("Balance fetch failed: %s", e)
        cash = 0.0

    # Fetch positions
    try:
        positions = client.get_positions()
    except Exception as e:
        log.warning("Positions fetch failed: %s", e)
        positions = []

    # Extended outage guard: if both balance and positions failed (cash=0,
    # no positions), skip the rest of the cycle to avoid wasted API calls.
    # Dry run is exempt — simulated cash may legitimately be 0.
    if cash == 0.0 and not positions and not config.DRY_RUN:
        log.warning(
            "Balance and positions both unavailable — Kalshi API outage? "
            "Skipping cycle %d.", state.cycles,
        )
        return

    # Reconcile resting orders: fetch open orders from Kalshi, sync into
    # state.resting_value for intra-cycle cash tracking, and cancel any
    # order older than STALE_ORDER_MINS so stale limit orders don't tie up
    # capital indefinitely.
    try:
        resting_orders = client.get_orders(status="resting")
        now = time.time()
        for o in resting_orders:
            oid = o.get("order_id") or o.get("id")
            side = o.get("side", "")
            price_cents = o.get("price_cents", 0)
            count = o.get("count", 0)
            created = o.get("created_time", "")
            if not oid or not price_cents or not count:
                continue
            # NO side price is stored as complement (100 - no_price_cents)
            entry_cents = price_cents if side == "yes" else (100 - price_cents)
            state.resting_value[oid] = int(count) * entry_cents / 100.0

            # Cancel stale orders
            if created:
                try:
                    created_ts = _parse_api_time(created)
                    if created_ts and (now - created_ts.timestamp()) > config.STALE_ORDER_MINS * 60:
                        client.cancel_order(oid)
                        state.resting_value.pop(oid, None)
                        log.info("Cancelled stale order %s (older than %.0f min)", oid, config.STALE_ORDER_MINS)
                except Exception:
                    pass
    except Exception as e:
        log.warning("Could not reconcile resting orders: %s", e)

    # Sync positions from Kalshi API — clear settled tickers from state
    api_tickers = {p.get("ticker", "") for p in positions if "15M" in p.get("ticker", "").upper()}
    settled = [t for t in list(state.positions.keys()) if t not in api_tickers]
    for t in settled:
        pos_info = state.positions.pop(t, {})
        reserved = state.position_risk.pop(t, 0.0)
        side = pos_info.get("side", "")
        entry_price = pos_info.get("entry_price", 0)
        contracts = pos_info.get("contracts", 0)
        order_id = pos_info.get("order_id", "")

        # Fetch actual settlement price from Kalshi market data.
        # Kalshi settles YES at 100 (yes wins) or 0 (yes loses);
        # NO settles at the complement (0 if yes wins, 100 if no wins).
        exit_price = None  # None means settlement data unavailable — don't book PnL
        try:
            market = client.get_market(t)
            exp_val = market.get("expiration_value")
            if exp_val is not None:
                settled_yes = int(round(float(exp_val) * 100))  # 0 or 100
                if side == "yes":
                    exit_price = float(settled_yes) / 100.0  # 1.0 or 0.0
                else:
                    exit_price = float(100 - settled_yes) / 100.0  # 0.0 or 1.0
        except Exception:
            log.warning(
                "Could not fetch settlement for %s — queuing for retry, not booking synthetic PnL",
                t,
            )

        # Only book PnL when we have real settlement data.
        if exit_price is not None:
            buy_cost = fees.buy_cash_out(contracts, entry_price / 100.0)
            sell_revenue = fees.sell_cash_in(contracts, exit_price)
            realized_pnl = sell_revenue - buy_cost
            won = sell_revenue > buy_cost

            log.info(
                "Cleared settled position: %s (side=%s, released $%.2f, realized P&L $%.2f, won=%s)",
                t, side, reserved, realized_pnl, won,
            )

            # Track consecutive losses for risk cooldown
            state.risk.record_trade_outcome(won, realized_pnl)

            # Record settlement to Supabase via n8n workflow
            try:
                n8n_client = get_n8n_client()
                n8n_client.execute_webhook("superbot-trade", {
                    "action": "settlement",
                    "order_id": order_id,
                    "status": "settled",
                    "fill_price_cents": int(exit_price * 100),
                    "outcome_won": won,
                    "pnl_dollars": realized_pnl,
                    "ticker": t,
                    "side": side,
                    "contracts": contracts,
                })
                state.session_pnl += realized_pnl  # add realized P&L only after successful record
            except Exception:
                log.warning("Failed to record settlement for %s via n8n webhook — queuing", t)
                queue = _load_settlement_queue()
                queue.append({
                    "order_id": order_id,
                    "payload": {
                        "action": "settlement",
                        "order_id": order_id,
                        "status": "settled",
                        "fill_price_cents": int(exit_price * 100),
                        "outcome_won": won,
                        "pnl_dollars": realized_pnl,
                        "ticker": t,
                        "side": side,
                        "contracts": contracts,
                    },
                })
                _save_settlement_queue(queue)
        else:
            # No settlement data available — queue for retry without booking PnL.
            # Position is already released from state.positions above.
            queue = _load_settlement_queue()
            queue.append({
                "order_id": order_id,
                "payload": {
                    "action": "settlement",
                    "order_id": order_id,
                    "status": "pending",
                    "fill_price_cents": None,
                    "outcome_won": None,
                    "pnl_dollars": None,
                    "ticker": t,
                    "side": side,
                    "contracts": contracts,
                    "entry_price": entry_price,
                },
            })
            _save_settlement_queue(queue)

    exposure = sum(
        (p.get("yes_position", 0) or 0) * (p.get("yes_avg_price_dollars", 0) or 0) / 100
        + (p.get("no_position", 0) or 0) * (p.get("no_avg_price_dollars", 0) or 0) / 100
        for p in positions if "15M" in p.get("ticker", "").upper()
    )

    state.risk.update_balance(cash, exposure)
    _touch_window(datetime.now(timezone.utc), cash)

    # Reset daily order counter on new day
    today = datetime.now(timezone.utc).date().isoformat()
    if state.today != today:
        state.today = today
        state.orders_today = 0
        state.risk.reset_daily()

    log.info(
        "%s | window $%.2f/%.2f | trades=%d cycles=%d",
        state.risk.summary(cash, exposure),
        state.window["used"], state.window["budget"],
        state.trades_placed, state.cycles,
    )

    # Record bot state to Supabase (non-critical state writes)
    try:
        get_supabase_client().set_bot_state("cycle", state.cycles)
        get_supabase_client().set_bot_state("cash", cash)
        get_supabase_client().set_bot_state("exposure", exposure)
        get_supabase_client().set_bot_state("session_pnl", state.session_pnl)
    except Exception:
        log.debug("Supabase bot_state writes failed")

    # Hard stops
    if state.risk.state.halted:
        log.warning("🛑 Risk halt active — no new entries")
        return

    if cash < 1.00:
        log.warning("Cash $%.2f below minimum $1.00", cash)
        return

    open_count = sum(
        1 for p in positions
        if "15M" in p.get("ticker", "").upper()
        and (p.get("yes_position", 0) or p.get("no_position", 0))
    )
    if open_count >= config.MAX_OPEN_POSITIONS:
        log.info("Position limit (%d/%d)", open_count, config.MAX_OPEN_POSITIONS)
        return

    max_trade = state.risk.max_trade_dollars(
        cash, exposure, resting_orders_value=sum(state.resting_value.values())
    )
    if max_trade < 0.20:
        log.info("Max trade $%.2f too small — protecting cash floor", max_trade)
        return

    # Find + evaluate markets
    markets = find_active_markets(client)
    if not markets:
        log.info("No tradeable markets.")
        return

    signals = []
    for m in markets:
        ticker = m.ticker
        if ticker in state.positions:
            continue
        for sig in se.evaluate(m, cash, max_trade_override=max_trade):
            if sig.is_trade:
                signals.append(sig)

    # Latency arb evaluation — separate signal path for last-minute entries
    for m in markets:
        ticker = m.ticker
        if ticker in state.positions:
            continue
        # Convert MarketInfo to dict for latency_arb
        market_dict = {
            "ticker": m.ticker,
            "series": m.series,
            "yes_bid": m.yes_bid,
            "yes_ask": m.yes_ask,
            "no_bid": m.no_bid,
            "no_ask": m.no_ask,
            "close_time": m.close_time.isoformat() if m.close_time else "",
            "minutes_left": m.minutes_left,
        }
        sig = latency_evaluate_market(market_dict)
        if sig:
            # Convert to TradeSignal for uniform handling through ranking/placement
            trade_sig = TradeSignal(
                direction=sig["direction"],
                side=sig["side"],
                entry_price=sig["entry_price_cents"],
                contracts=sig["contracts"],
                dollar_risk=sig["contracts"] * sig["entry_price_cents"] / 100.0,
                reason=f"Latency arb: edge={sig['edge_cents']:.1f}c",
                market=m,
                strength=sig["edge_cents"],
                confidence=1.0,
            )
            signals.append(trade_sig)

    ranked = se.rank_signals(signals)
    if not ranked:
        log.info("No signals this cycle.")
        return

    slots = min(config.MAX_OPEN_POSITIONS - open_count, config.MAX_ORDERS_PER_CYCLE)
    placed = 0
    current_exposure = exposure  # computed once; updated after each placement
    for sig in ranked[:slots]:
        # Daily order limit check
        if state.orders_today >= config.MAX_ORDERS_PER_DAY:
            log.info(
                "Daily order limit (%d/%d) reached — no new orders today",
                state.orders_today, config.MAX_ORDERS_PER_DAY,
            )
            break

        # Enforce minimum gap between orders
        now_t = time.time()
        gap = now_t - state.last_order_time
        if gap < config.MIN_ORDER_GAP_SECONDS:
            log.info(
                "ORDER GAP — %.1fs since last order, need %.1fs — skipping %s",
                gap, config.MIN_ORDER_GAP_SECONDS, sig.market.ticker,
            )
            continue

        max_trade = state.risk.max_trade_dollars(
            cash, current_exposure,
            resting_orders_value=sum(state.resting_value.values()),
        )

        total_value = cash + exposure
        if not state._pre_trade_check(sig, sig.market, cash, exposure, total_value, client):
            continue

        coid = _coid(sig.market.ticker, sig.side)
        resp = state._place_order(client, sig, coid)
        if resp is None:
            continue

        oid = (resp.get("order") or {}).get("order_id") or resp.get("order_id", "")
        if oid:
            # Track reserved cash for this resting order so intra-cycle cash
            # tracking doesn't overspend on orders already placed this cycle.
            state.resting_value[oid] = sig.dollar_risk

            # Only record as a filled position when the API reports a fill.
            # A resting (unfilled) limit order stays in resting_value and is
            # reconciled against get_orders() each cycle.
            fill_count = float(resp.get("filledCount", 0))
            if fill_count > 0:
                fees_dollars = fees.taker_fee_cents(
                    sig.contracts, sig.entry_price / 100.0
                ) / 100.0
                state._record_filled_order(
                    client, sig, oid, fill_count, "executed", fees_dollars, state.cycles
                )
                state.resting_value.pop(oid, None)
            current_exposure += sig.dollar_risk  # track incremental exposure
            state.last_order_time = time.time()
            state.orders_today += 1
            log.info("✓ %s order_id=%s (fill_count=%.0f)", sig.market.ticker, oid, fill_count)
            placed += 1

    if placed:
        log.info("Placed %d order(s) this cycle.", placed)


# ═════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════

def main():
    mode = "DRY RUN 🔵" if config.DRY_RUN else "LIVE ⚡"
    log.info("=" * 58)
    log.info("Kalshi SuperBot v%s — %s", config.__version__, mode)
    log.info("Series: %s", " ".join(config.TARGET_SERIES))
    log.info("Signal: skew≥%dc | trend≥%dc | window %.0f-%.0fmin | price %d-%dc",
             config.MIN_SKEW_CENTS, config.MIN_TREND_CENTS,
             config.ENTRY_WINDOW_START_MIN, config.ENTRY_WINDOW_END_MIN,
             config.MIN_YES_PRICE_CENTS, config.MAX_YES_PRICE_CENTS)
    log.info("Maker mode: %s | Max trade: $%.2f | Max positions: %d",
             "ON (limit orders)" if not config.AGGRESSIVE_FILL else "OFF (taker)",
             config.MAX_TRADE_DOLLARS, config.MAX_OPEN_POSITIONS)
    log.info("Risk: 70%% cash floor | 30%% exposure ceiling | "
             "8%% per-trade | 15%% drawdown halt")
    log.info("LLM eval: %s | Supabase: %s",
             "enabled" if config.LLM_EVALUATION_ENABLED else "disabled",
             "enabled" if get_supabase_client().enabled else "disabled")
    log.info("=" * 58)

    # Validate credentials and dry-run mode before connecting to Kalshi.
    # The local .env sets DRY_RUN=false, which overrides the safe default of True.
    try:
        config.validate_credentials()
    except ValueError as e:
        log.error("Credential validation failed: %s", e)
        sys.exit(1)
    if not config.validate_dry_run(live_explicit=args.live):
        sys.exit(1)
    config.validate_bands()

    client = KalshiClient()
    try:
        bal = client.get_balance()
        log.info("Connected ✓ Balance: $%.2f", bal)
        state.risk.initialize(bal)
    except Exception as e:
        log.error("Auth failed: %s", e)
        sys.exit(1)

    # Fail fast if n8n is unreachable — all settlement PnL routing depends on it.
    if not n8n_health_check():
        log.error("n8n health check failed — settlement webhooks will not work. "
                  "Start n8n (http://127.0.0.1:5678) and try again.")
        sys.exit(1)
    log.info("n8n health check ✓")

    # Seed resting orders from Kalshi so intra-cycle cash tracking accounts
    # for orders already resting from a previous run.
    try:
        resting_orders = client.get_orders(status="resting")
        for o in resting_orders:
            oid = o.get("order_id") or o.get("id")
            side = o.get("side", "")
            price_cents = o.get("price_cents", 0)
            count = o.get("count", 0)
            if oid and price_cents and count:
                # NO side price is stored as complement (100 - no_price_cents)
                if side == "no":
                    price_cents = 100 - price_cents
                state.resting_value[oid] = int(count) * price_cents / 100.0
        if state.resting_value:
            log.info("Seeded %d resting orders worth $%.2f reserved cash",
                     len(state.resting_value), sum(state.resting_value.values()))
    except Exception as e:
        log.warning("Could not seed resting orders from Kalshi: %s", e)

    # Seed positions from Kalshi exchange so the bot knows about existing
    # positions and doesn't try to re-enter them on restart.
    try:
        positions = client.get_positions()
        for p in positions:
            ticker = p.get("ticker", "")
            if "15M" not in ticker.upper():
                continue
            yes_pos = int(p.get("yes_position", 0) or 0)
            no_pos = int(p.get("no_position", 0) or 0)
            if not yes_pos and not no_pos:
                continue
            side = "yes" if yes_pos else "no"
            contracts = yes_pos or no_pos
            entry_price = int(round(float(p.get("yes_avg_price_dollars", 0) or 0) * 100))
            if side == "no":
                entry_price = int(round(float(p.get("no_avg_price_dollars", 0) or 0) * 100))
            order_id = p.get("order_id", ticker)
            state.positions[ticker] = {
                "side": side,
                "entry_price": entry_price,
                "contracts": contracts,
                "order_id": order_id,
            }
            state.position_risk[ticker] = contracts * entry_price / 100.0
        if state.positions:
            log.info("Seeded %d existing positions from exchange",
                     len(state.positions))
    except Exception as e:
        log.warning("Could not seed positions from Kalshi: %s", e)

    # Reconcile session_pnl from Supabase on startup — prevents P&L drift
    # if n8n webhook settlement writes failed in a previous run.
    try:
        saved_pnl = get_supabase_client().get_bot_state("session_pnl")
        if saved_pnl is not None:
            state.session_pnl = float(saved_pnl)
            log.info("Reconciled session_pnl from Supabase: $%.2f", state.session_pnl)
    except Exception:
        log.warning("Could not reconcile session_pnl from Supabase — starting at $0.00")

    # Replay any pending settlements from the queue file
    _replay_settlement_queue()

    if args.once:
        cycle(client)
        return

    def _shutdown_handler(signum, frame):
        sig_name = "SIGTERM" if signum == sig_mod.SIGTERM else "SIGINT"
        log.info("%s received — shutting down gracefully", sig_name)
        state.running = False

    def _cycle_timeout_handler(signum, frame):
        raise TimeoutError(f"Cycle exceeded {config.CYCLE_TIMEOUT_SECONDS}s timeout")

    sig_mod.signal(sig_mod.SIGTERM, _shutdown_handler)
    sig_mod.signal(sig_mod.SIGINT, _shutdown_handler)
    sig_mod.signal(sig_mod.SIGALRM, _cycle_timeout_handler)
    log.info("Loop every %ds. Ctrl+C to stop.", args.interval)
    cycle_count = 0
    while state.running:
        cycle_start = time.time()
        try:
            sig_mod.alarm(config.CYCLE_TIMEOUT_SECONDS)
            cycle(client)
            sig_mod.alarm(0)
            cycle_count += 1
            if args.cycle_limit and cycle_count >= args.cycle_limit:
                log.info("Cycle limit %d reached — stopping", args.cycle_limit)
                break
        except Exception as e:
            sig_mod.alarm(0)
            log.exception("Unhandled cycle error: %s", e)
            delay = state.resilience.mark_failure(e)
            if state.resilience.should_rebuild_client():
                # Don't rebuild on auth errors — a bad key won't fix itself.
                if state.resilience.is_auth_error():
                    log.error("Authentication error — not rebuilding client. "
                              "Check KALSHI_KEY_ID and private key.")
                    break
                log.info("Rebuilding Kalshi client after %d errors",
                         state.resilience.settings.rebuild_threshold)
                try:
                    client = KalshiClient()
                    # Verify the new client works before resetting the error counter.
                    # If verification fails, stop rather than looping on a broken client.
                    client.get_markets(limit=1)
                    state.resilience.after_rebuild()
                except Exception as rebuild_e:
                    log.error("Client rebuild failed: %s — stopping", rebuild_e)
                    break
            time.sleep(delay)
            continue
        state.resilience.mark_success()
        # Drift-corrected sleep: measure cycle duration and sleep only the
        # remaining time so the loop runs at a stable interval regardless of
        # how long the cycle took.
        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, args.interval - elapsed)
        if elapsed > args.interval:
            log.warning("Cycle took %.1fs (> %ds interval)", elapsed, args.interval)
        time.sleep(sleep_for)

    log.info("Done. P&L=$%.2f | Cycles=%d | Trades=%d",
             state.session_pnl, state.cycles, state.trades_placed)


if __name__ == "__main__":
    lock_path = Path(__file__).resolve().parent / ".lock"
    lock_fd = None
    for attempt in range(5):
        try:
            # Atomic create — fails if lock already exists (TOCTOU-safe)
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            pid = open(lock_path).read().strip()
            if os.path.exists(f"/proc/{pid}"):
                print(f"Bot already running (PID {pid}). Exiting.")
                sys.exit(1)
            # Stale lock — remove and retry the atomic open
            os.unlink(lock_path)
    else:
        print("Could not acquire lock after 5 attempts. Exiting.")
        sys.exit(1)
    with os.fdopen(lock_fd, "w") as f:
        f.write(str(os.getpid()))
    try:
        main()
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass