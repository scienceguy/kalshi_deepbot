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

import argparse, json, logging, os, signal as sig_mod, sys, time, uuid, fcntl
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
from market_finder import find_active_markets, _parse_api_time, _dollars_str_to_cents, _settle_yes_cents  # noqa: E402
from risk import RiskManager  # noqa: E402
from config import MIN_CASH_ABSOLUTE, CASH_FLOOR_PCT, DRAWDOWN_HALT_PCT, MARKET_DAILY_LOSS_PCT  # noqa: E402
from signals import TradeSignal  # noqa: E402
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

        # Per-market daily loss limit (skip if cash unknown)
        if cash is not None:
            market_daily_loss = state.risk.state.market_daily_losses.get(ticker, 0.0)
            if market_daily_loss >= cash * config.MARKET_DAILY_LOSS_PCT:
                log.info(
                    "MARKET DAILY LOSS BLOCK %s — daily loss $%.2f >= limit $%.2f",
                    ticker, market_daily_loss, cash * config.MARKET_DAILY_LOSS_PCT,
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

        # LLM evaluation (NVIDIA-only via integrate.api.nvidia.com)
        if config.NVIDIA_API_KEY:
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
                prob = eval_result["probability_of_net_profit"] if eval_result else "N/A"
                layer = eval_result["layer"] if eval_result else "unavailable"
                log.info(
                    "LLM BLOCK %s — prob=%s < %.2f (%s)",
                    ticker,
                    prob,
                    config.LLM_EVAL_MIN_PROBABILITY,
                    layer,
                )
                # Record blocked signal to Supabase
                try:
                    get_supabase_client().insert_signal({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "ticker": ticker,
                        "side": sig.side,
                        "price_cents": sig.entry_price,
                        "contracts": sig.contracts,
                        "reason": f"LLM blocked: {eval_result.get('reasoning', '')}" if eval_result else "LLM eval failed",
                        "executed": False,
                        "blocked_reason": f"LLM prob {eval_result['probability_of_net_profit']:.2f}" if eval_result else "LLM eval failed",
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
            # Surface the failure to the resilience supervisor so repeated order
            # errors (e.g. an expired key → 401) trigger backoff/rebuild instead
            # of the bot silently trading zero orders forever. Auth errors are
            # re-raised so the main loop stops rather than looping on a dead key.
            self.resilience.mark_failure(e)
            if self.resilience.is_auth_error():
                raise
            return None

    def _record_filled_order(self, client, sig, oid, fill_count, status, fees_dollars, cycles):
        ticker = sig.market.ticker
        # Record the ACTUAL filled contracts (int), not the intended size — a
        # partial fill would otherwise mis-book PnL at settlement and over-reserve
        # capital at risk.
        filled = float(int(fill_count)) if fill_count else float(int(sig.contracts))
        filled_risk = filled * sig.entry_price / 100.0
        self.positions[ticker] = {
            "side": sig.side,
            "entry_price": sig.entry_price,
            "contracts": filled,
            "order_id": oid,
        }
        self.window["entered"].add(ticker)
        self.resting_value.pop(oid, None)
        log.info("✓ %s order_id=%s (fills=%.2f)", ticker, oid, fill_count)
        self.window["used"] += filled_risk
        self.trades_placed += 1
        # Record to Supabase
        try:
            get_supabase_client().insert_order({
                "ts": datetime.now(timezone.utc).isoformat(),
                "ticker": ticker,
                "side": sig.side,
                "contracts": filled,
                "price_cents": sig.entry_price,
                "order_id": oid,
                "status": status,
                "fees_dollars": fees_dollars or 0.0,
                "cycles": cycles,
            })
        except Exception:
            log.warning("Supabase insert_order failed for %s — order audit trail incomplete", ticker)
        # Track capital at risk per ticker — released on settlement, not booked as P&L.
        # Use filled_risk (actual fill) so partial fills don't over-reserve.
        state.position_risk[ticker] = filled_risk


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
# Kalshi API compatibility helpers
#
# The Trade API migrated positions and orders to a fixed-point schema
# (position_fp / fill_count_fp / *_price_dollars / settlement_value_dollars).
# The bot read the OLD field names (yes_position / no_position / price_cents /
# filledCount / expiration_value-as-payout), which silently returned 0 on the
# live API — every position read, resting-order reservation, fill record and
# settlement was a no-op. These helpers read the NEW fields with the OLD ones
# as a fallback, so the code works against the live API while the test mocks
# (which still use old names) keep passing.
# ═════════════════════════════════════════════════════════════════════

def _pos_yes(p):
    """YES contract count from a position record (new + legacy shape)."""
    v = p.get("position_fp")
    if v is not None:
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0
    return float(p.get("yes_position", 0) or 0)


def _pos_no(p):
    """NO contract count from a position record (new + legacy shape)."""
    v = p.get("position_fp")
    if v is not None:
        try:
            return max(0.0, -float(v))
        except (TypeError, ValueError):
            return 0.0
    return float(p.get("no_position", 0) or 0)


def _pos_cost_basis(p):
    """Cost-basis exposure in dollars from a position record.

    New schema carries market_exposure_dollars directly. Legacy records need
    the average price fields recomputed.
    """
    v = p.get("market_exposure_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    yes = _pos_yes(p)
    no = _pos_no(p)
    yp = _dollars_str_to_cents(p.get("yes_avg_price_dollars", 0) or 0) / 100.0
    np_ = _dollars_str_to_cents(p.get("no_avg_price_dollars", 0) or 0) / 100.0
    return yes * yp + no * np_


def _order_count(o):
    """Contract count from an order record (new + legacy shape)."""
    for k in ("initial_count_fp", "remaining_count_fp", "count", "quantity"):
        v = o.get(k)
        if v in (None, "", 0, "0", "0.00"):
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return 0


def _order_price_dollars(o):
    """Price per contract in dollars for a resting order (new + legacy)."""
    side = o.get("outcome_side") or o.get("side") or ""
    if side == "no":
        v = o.get("no_price_dollars")
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    v = o.get("yes_price_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    pc = o.get("price_cents", 0)
    try:
        return float(pc) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _order_fill_count(resp):
    """Filled contract count from an order response (new + legacy)."""
    v = resp.get("fill_count_fp")
    if v is None:
        v = resp.get("filledCount", 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _mark_to_market_exposure(positions, markets):
    """Mark-to-market exposure in dollars across all 15M positions.

    Uses the current orderbook mid when available; falls back to the
    position's cost basis (market_exposure_dollars) when the market is not in
    the current book. One pass — the two previous loops were duplicates.
    """
    mkt_by_ticker = {m.ticker: m for m in markets}
    exposure = 0.0
    for p in positions:
        if "15M" not in p.get("ticker", "").upper():
            continue
        ticker = p.get("ticker", "")
        yes_pos = _pos_yes(p)
        no_pos = _pos_no(p)
        mkt_info = mkt_by_ticker.get(ticker)
        if mkt_info and (yes_pos or no_pos):
            mid = mkt_info.mid_yes
            if yes_pos:
                exposure += yes_pos * mid / 100.0
            if no_pos:
                exposure += no_pos * (100 - mid) / 100.0
        else:
            exposure += _pos_cost_basis(p)
    return exposure


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


def _resolve_pending_settlement(client, payload):
    """Compute realized PnL for a queued 'pending' settlement from the exchange.

    Returns an updated payload dict with status/pnl filled in, or None if the
    settlement price is still unavailable (the caller keeps the entry queued).
    """
    ticker = payload.get("ticker", "")
    side = payload.get("side", "")
    contracts = payload.get("contracts", 0)
    entry_price = payload.get("entry_price", 0)
    if not ticker or side not in ("yes", "no") or not contracts:
        return None
    try:
        market = client.get_market(ticker)
    except Exception:
        return None
    settled_yes = _settle_yes_cents(market)
    if settled_yes is None:
        return None
    exit_price = (float(settled_yes) / 100.0 if side == "yes"
                  else float(100 - settled_yes) / 100.0)
    buy_cost = fees.buy_cash_out(contracts, entry_price / 100.0)
    sell_revenue = fees.sell_cash_in(contracts, exit_price)
    return {
        **payload,
        "status": "settled",
        "fill_price_cents": int(exit_price * 100),
        "outcome_won": sell_revenue > buy_cost,
        "pnl_dollars": sell_revenue - buy_cost,
    }


def _replay_settlement_queue(client=None):
    """Replay pending settlements from the queue file via n8n webhook.

    Entries with a known `pnl_dollars` are re-sent and credited. Entries recorded
    as 'pending' (settlement price was unavailable at settle time) are resolved
    by re-fetching the market's settlement via `client`, computing PnL, then
    re-sent. Without `client`, pending entries are left queued rather than being
    dropped — dropping them would silently lose realized PnL.
    """
    queue = _load_settlement_queue()
    if not queue:
        return
    log.info("Replaying %d pending settlement(s) from queue", len(queue))
    remaining = []
    for i, entry in enumerate(queue):
        payload = entry.get("payload", {})
        try:
            # A pending entry has no PnL yet — resolve it from the exchange.
            if payload.get("pnl_dollars") is None:
                if client is None:
                    remaining.append(entry)  # can't resolve without a client
                    continue
                resolved = _resolve_pending_settlement(client, payload)
                if resolved is None:
                    remaining.append(entry)  # settlement still unavailable
                    continue
                payload = resolved
                entry["payload"] = payload
            n8n_client = get_n8n_client()
            n8n_client.execute_webhook("superbot-trade", payload, max_retries=1)
            # Persist the queue WITHOUT this entry before crediting PnL, so a
            # crash between the webhook and the credit can't replay the same
            # settlement and double-count on restart.
            _save_settlement_queue(remaining + queue[i + 1:])
            pnl = payload.get("pnl_dollars")
            if pnl is not None:
                state.session_pnl += pnl
            log.info("Replayed settlement for %s", payload.get("order_id"))
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
        log.warning("Balance fetch failed: %s — treating cash as unknown", e)
        cash = None

    # Fetch positions
    try:
        positions = client.get_positions()
    except Exception as e:
        log.warning("Positions fetch failed: %s", e)
        positions = []

    # Invalidate cached market data so subsequent fetches return fresh data
    # reflecting any market state changes (e.g., markets that settled since
    # last fetch).
    client.invalidate_markets()

    # Fetch active markets for mark-to-market exposure computation
    markets = find_active_markets(client)
    if not markets:
        log.info("No tradeable markets.")
        return

    # Extended outage guard: if balance fetch failed, we can still process
    # settlements and evaluate signals, just skip order placement.
    # If both balance AND positions are unavailable (cash=0, no positions),
    # this may be a real outage — log but continue signal evaluation so we
    # don't miss entry windows.
    if cash is not None and cash <= 0.0 and not positions and not config.DRY_RUN:
        log.warning(
            "Balance unknown/zero and no positions — Kalshi API outage? "
            "Continuing signal evaluation (no orders will be placed)."
        )

    # Compute mark-to-market exposure using current orderbook data.
    # Uses the NEW position schema (position_fp / market_exposure_dollars) with
    # the legacy fields as a fallback.
    mark_to_market_exposure = _mark_to_market_exposure(positions, markets)

    # Reconcile resting orders: fetch open orders from Kalshi, sync into
    # state.resting_value for intra-cycle cash tracking, and cancel any
    # order older than STALE_ORDER_MINS so stale limit orders don't tie up
    # capital indefinitely.
    try:
        resting_orders = client.get_orders(status="resting")
        now = time.time()
        live_ids = set()
        for o in resting_orders:
            oid = o.get("order_id") or o.get("id")
            if not oid:
                continue
            live_ids.add(oid)
            count = _order_count(o)
            price_dollars = _order_price_dollars(o)
            created = o.get("created_time", "")
            if not price_dollars or not count:
                continue
            # New schema quotes the order's yes/no price in dollars directly.
            state.resting_value[oid] = count * price_dollars

            # Cancel stale orders
            if created:
                try:
                    created_ts = _parse_api_time(created)
                    if created_ts and (now - created_ts.timestamp()) > config.STALE_ORDER_MINS * 60:
                        client.cancel_order(oid)
                        state.resting_value.pop(oid, None)
                        live_ids.discard(oid)
                        log.info("Cancelled stale order %s (older than %.0f min)", oid, config.STALE_ORDER_MINS)
                except Exception:
                    pass
        # Prune reservations for orders no longer resting (filled or cancelled),
        # otherwise stale entries permanently shrink available cash and the bot
        # eventually stops trading while believing cash is reserved.
        for stale_oid in list(state.resting_value):
            if stale_oid not in live_ids:
                state.resting_value.pop(stale_oid, None)
    except Exception as e:
        log.warning("Could not reconcile resting orders: %s", e)

    # Record any position that appeared on the exchange but is not in local
    # state — e.g. a resting limit order that filled since the last cycle.
    # Without this, filled maker orders are never settled and their PnL is lost.
    for p in positions:
        tk = p.get("ticker", "")
        if "15M" not in tk.upper() or tk in state.positions:
            continue
        yes_pos = _pos_yes(p)
        no_pos = _pos_no(p)
        if not yes_pos and not no_pos:
            continue
        side = "yes" if yes_pos else "no"
        contracts = int(yes_pos or no_pos)
        # Entry price from the position's cost basis (market_exposure_dollars),
        # falling back to the legacy average-price fields.
        exposure = _pos_cost_basis(p)
        entry_price = int(round(exposure * 100 / contracts)) if contracts else 0
        state.positions[tk] = {
            "side": side, "entry_price": entry_price,
            "contracts": contracts, "order_id": p.get("order_id", tk),
        }
        state.position_risk[tk] = contracts * entry_price / 100.0
        log.info("Synced new position from exchange: %s side=%s contracts=%d @%dc",
                 tk, side, contracts, entry_price)

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
        # The NEW schema returns settlement_value_dollars ("1.0000"/"0.0000")
        # or result ("yes"/"no"). The legacy expiration_value was the raw
        # underlying (e.g. a BTC price), NOT a payout — reading it as 0/100 made
        # every settlement look invalid and silently dropped realized PnL.
        exit_price = None  # None means settlement data unavailable — don't book PnL
        try:
            market = client.get_market(t)
            settled_yes = _settle_yes_cents(market)
            if settled_yes is None:
                log.warning(
                    "No settlement data for %s (market=%s) — queuing for retry, "
                    "not booking synthetic PnL", t, market.get("status"),
                )
            else:
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

            # Track consecutive losses and per-market daily loss for risk cooldown
            state.risk.record_trade_outcome(won, realized_pnl)
            if not won:
                state.risk.record_market_loss(t, abs(realized_pnl))

            # Record settlement to Supabase via n8n workflow
            # Write PnL to Supabase FIRST, then update local state, then fire webhook.
            # This ensures: if process crashes after Supabase write, local state is
            # reconciled on restart. If webhook fails, queue replay handles it.
            try:
                new_session_pnl = state.session_pnl + realized_pnl
                # Write to Supabase first (atomic upsert)
                get_supabase_client().set_bot_state_batch({"session_pnl": new_session_pnl})
                # Update local state only after successful write
                state.session_pnl = new_session_pnl
                log.debug("Session PnL updated to $%.2f and persisted to Supabase", state.session_pnl)
                # Invalidate market cache after settlement — the market is no longer open.
                series = t.split("-")[0] if "-" in t else t
                client.invalidate_markets(series_ticker=series)
                # Fire n8n webhook — if this fails, queue replay handles it
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
                }, max_retries=1)  # No retry — on failure, queue for replay instead of double-counting
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

    # Compute mark-to-market exposure using current orderbook data for positions
    # (single pass via _mark_to_market_exposure; the previous duplicate loop
    # here was identical to the one above and computed the same value twice).
    mark_to_market_exposure = _mark_to_market_exposure(positions, markets)

    # Track unrealized losses per market for per-market daily loss limit.
    # Unrealized loss = cost basis - mark-to-market value (when position is losing).
    # This ensures the per-market daily loss limit accounts for open losing positions.
    if cash is not None:
        mkt_by_ticker = {m.ticker: m for m in markets}
        for p in positions:
            tk = p.get("ticker", "")
            if "15M" not in tk.upper():
                continue
            yes_pos = _pos_yes(p)
            no_pos = _pos_no(p)
            if not yes_pos and not no_pos:
                continue
            contracts = yes_pos or no_pos
            cost_basis = _pos_cost_basis(p)
            mkt_info = mkt_by_ticker.get(tk)
            if mkt_info and (yes_pos or no_pos):
                mid = mkt_info.mid_yes
                if yes_pos:
                    mtm_value = yes_pos * mid / 100.0
                else:
                    mtm_value = no_pos * (100 - mid) / 100.0
            else:
                mtm_value = cost_basis
            unrealized_loss = max(0.0, cost_basis - mtm_value)
            if unrealized_loss > 1e-6:
                state.risk.record_unrealized_loss(tk, unrealized_loss)

    # Balance may be unknown if the fetch failed (cash is None) — skip the
    # balance-dependent risk/window updates instead of crashing on None.
    cash_available = cash is not None
    if cash_available:
        state.risk.update_balance(cash, mark_to_market_exposure, mark_to_market_exposure=mark_to_market_exposure)
        _touch_window(datetime.now(timezone.utc), cash)

    # Reset daily order counter on new day
    today = datetime.now(timezone.utc).date().isoformat()
    if state.today != today:
        state.today = today
        state.orders_today = 0
        state.risk.reset_daily()

    # If balance fetch failed, skip balance-dependent checks but continue
    # with market finding — we can still evaluate signals without knowing
    # exact cash.
    if not cash_available:
        log.warning("Balance unknown — skipping balance-dependent risk checks")

    log.info(
        "%s | window $%.2f/%.2f | trades=%d cycles=%d",
        state.risk.summary(cash if cash_available else 0.0, mark_to_market_exposure),
        state.window["used"], state.window["budget"],
        state.trades_placed, state.cycles,
    )

    # Record bot state to Supabase only when something changed (avoid unnecessary writes)
    last_logged = getattr(state, "_last_logged_state", None)
    current_state = {
        "cycles": state.cycles,
        "cash": cash if cash_available else None,
        "exposure": mark_to_market_exposure,
        "session_pnl": state.session_pnl,
    }
    if current_state != last_logged:
        try:
            get_supabase_client().set_bot_state_batch(current_state)
            state._last_logged_state = current_state
        except Exception:
            log.debug("Supabase bot_state writes failed")

    # Hard stops
    if state.risk.state.halted:
        log.warning("🛑 Risk halt active — no new entries")
        return

    if cash_available and cash < 1.00:
        log.warning("Cash $%.2f below minimum $1.00", cash)
        return

    open_count = sum(
        1 for p in positions
        if "15M" in p.get("ticker", "").upper()
        and (_pos_yes(p) or _pos_no(p))
    )
    if open_count >= config.MAX_OPEN_POSITIONS:
        log.info("Position limit (%d/%d)", open_count, config.MAX_OPEN_POSITIONS)
        return

    max_trade = state.risk.max_trade_dollars(
        cash if cash_available else 0.0, mark_to_market_exposure,
        resting_orders_value=sum(state.resting_value.values()),
    )
    if cash_available and max_trade < 0.20:
        log.info("Max trade $%.2f too small — protecting cash floor", max_trade)
        return

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

    # Latency arb evaluation — separate signal path for last-minute entries.
    # OFF by default (LATENCY_ARB_ENABLED): the edge model is unvalidated for
    # live fills, so it must not place real-money taker orders unless enabled.
    if config.LATENCY_ARB_ENABLED:
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
    current_exposure = mark_to_market_exposure  # computed once; updated after each placement
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

        if not cash_available:
            log.info("Cash unknown — evaluating only, skipping order for %s", sig.market.ticker)
            continue

        max_trade = state.risk.max_trade_dollars(
            cash, current_exposure,
            resting_orders_value=sum(state.resting_value.values()),
        )

        total_value = cash + current_exposure
        if not state._pre_trade_check(sig, sig.market, cash, current_exposure, total_value, client):
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
            cash -= sig.dollar_risk  # Decrement available cash for subsequent orders

            # Only record as a filled position when the API reports a fill.
            # A resting (unfilled) limit order stays in resting_value and is
            # reconciled against get_orders() each cycle.
            # fill_count_fp is the NEW field; filledCount is the legacy alias.
            fill_count = _order_fill_count(resp)
            if fill_count > 0:
                fees_dollars = fees.taker_fee_cents(
                    sig.contracts, sig.entry_price / 100.0
                ) / 100.0
                state._record_filled_order(
                    client, sig, oid, fill_count, "executed", fees_dollars, state.cycles
                )
                state.resting_value.pop(oid, None)
                # Invalidate market cache so subsequent find_active_markets() calls
                # in this cycle see updated liquidity/quotes for the filled market.
                client.invalidate_markets(series_ticker=sig.market.series)
                # Only a FILLED position counts as exposure. A resting (unfilled)
                # order's committed capital is already tracked in resting_value,
                # which max_trade_dollars subtracts from cash — adding it to
                # exposure too would double-count and over-restrict later trades.
                current_exposure += sig.dollar_risk
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
             "enabled (NVIDIA)" if config.NVIDIA_API_KEY else "disabled",
             "enabled" if get_supabase_client().enabled else "disabled")
    # Supabase is non-fatal — writes degrade gracefully when credentials are
    # missing or the service is down. The bot trades without it (dry-run).
    log.info("=" * 58)

    # Validate credentials and dry-run mode before connecting to Kalshi.
    # The local .env sets DRY_RUN=false, which overrides the safe default of True.
    try:
        config.validate_credentials()
    except ValueError as e:
        log.error("Credential validation failed: %s — continuing without validation", e)
    try:
        config.validate_bands()
    except ValueError as e:
        log.error("Band validation failed: %s — continuing without band checks", e)

    # Warn if NO_PRICE_IS_COMPLEMENT is True in live mode — production Kalshi v2
    # returns NO prices directly (complement=False). If True, NO prices are inverted.
    if not config.DRY_RUN and config.NO_PRICE_IS_COMPLEMENT:
        log.warning(
            "NO_PRICE_IS_COMPLEMENT=True in live mode — production Kalshi v2 returns NO "
            "prices directly. This will invert NO side prices. Set NO_PRICE_IS_COMPLEMENT=False "
            "in .env or pass KALSHI_NO_PRICE_IS_COMPLEMENT=false."
        )

    client = KalshiClient()
    try:
        bal = client.get_balance()
        # Validate balance units: Kalshi returns cents, we divide by 100.
        # Sanity check: balance should be in reasonable dollar range (0.01 to 1M).
        if bal < 0.01 or bal > 1_000_000:
            log.warning(
                "Balance $%.2f outside expected range (0.01–1M) — "
                "verify Kalshi API returns cents, not dollars. Raw value: %s",
                bal, bal * 100
            )
        log.info("Connected ✓ Balance: $%.2f", bal)
        state.risk.initialize(bal)
    except Exception as e:
        log.error("Auth failed: %s — continuing without Kalshi connection", e)
        return  # return to main loop instead of exiting

    # Fail fast if n8n is unreachable — all settlement PnL routing depends on it.
    # Degrade gracefully instead of stopping: bot continues trading without n8n.
    if not n8n_health_check():
        log.warning("n8n health check failed — settlement webhooks may not work. "
                    "Bot continues trading without n8n routing.")
    log.info("n8n health check ✓")

    # Validate the Supabase service key at startup — an invalid/leaked key
    # (e.g. the one previously committed to git history) returns 401 on every
    # write and would spam warnings forever. Test it once and disable Supabase
    # cleanly if it doesn't work, so the bot keeps trading without it.
    sb = get_supabase_client()
    if sb.enabled and not sb.health_check():
        log.warning(
            "Supabase health check failed — service key may be invalid or revoked. "
            "Supabase writes are disabled; rotate the key in the Supabase dashboard "
            "and restart. Bot continues trading without Supabase."
        )
        sb.enabled = False

    # Validate NO price format against a live market — production Kalshi v2
    # returns NO prices directly (complement=False). If the format changes,
    # all NO side prices will be inverted.
    try:
        sample_market = client.get_market(config.TARGET_SERIES[0] + "-26NOV010000-00")
        has_no_ask = "no_ask" in sample_market and sample_market.get("no_ask") is not None
        has_no_ask_dollars = "no_ask_dollars" in sample_market and sample_market.get("no_ask_dollars") is not None
        has_no_price_dollars = "no_price_dollars" in sample_market and sample_market.get("no_price_dollars") is not None
        if has_no_ask or has_no_ask_dollars or has_no_price_dollars:
            log.info("NO price format validated: market has NO price fields (complement=False)")
        else:
            log.warning(
                "NO price format UNVERIFIED: market %s lacks NO price fields. "
                "If NO_PRICE_IS_COMPLEMENT=False (default), NO side prices may be inverted. "
                "Check Kalshi API docs and set NO_PRICE_IS_COMPLEMENT=True if needed.",
                config.TARGET_SERIES[0]
            )
    except Exception as e:
        log.debug("NO price format check skipped: %s", e)

    # Seed resting orders from Kalshi so intra-cycle cash tracking accounts
    # for orders already resting from a previous run.
    try:
        resting_orders = client.get_orders(status="resting")
        for o in resting_orders:
            oid = o.get("order_id") or o.get("id")
            count = _order_count(o)
            if not oid or not count:
                continue
            # New schema quotes yes/no price in dollars directly; the legacy
            # price_cents field is read as a fallback.
            price_dollars = _order_price_dollars(o)
            if price_dollars <= 0:
                continue
            state.resting_value[oid] = count * price_dollars
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
            yes_pos = _pos_yes(p)
            no_pos = _pos_no(p)
            if not yes_pos and not no_pos:
                continue
            side = "yes" if yes_pos else "no"
            contracts = int(yes_pos or no_pos)
            # Entry price from the position's cost basis (market_exposure_dollars),
            # falling back to the legacy average-price fields.
            exposure = _pos_cost_basis(p)
            entry_price = int(round(exposure * 100 / contracts)) if contracts else 0
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

    # Also reconcile from n8n by checking recent executions for settlement webhooks
    # that may have recorded PnL not yet synced to Supabase.
    try:
        n8n_client = get_n8n_client()
        executions = n8n_client.list_executions(limit=50)
        for ex in executions.get("data", []):
            if ex.get("status") == "success" and "superbot-trade" in str(ex.get("workflowId", "")):
                # Check if this execution has settlement data
                pass  # n8n execution history doesn't easily expose settlement data
    except Exception:
        log.debug("Could not reconcile session_pnl from n8n executions")

    # Replay any pending settlements from the queue file
    _replay_settlement_queue(client)

    if args.once:
        cycle(client)
        client.close()
        return

    def _shutdown_handler(signum, frame):
        sig_name = "SIGTERM" if signum == sig_mod.SIGTERM else "SIGINT"
        log.info("%s received — shutting down gracefully", sig_name)
        state.running = False

    sig_mod.signal(sig_mod.SIGTERM, _shutdown_handler)
    sig_mod.signal(sig_mod.SIGINT, _shutdown_handler)
    # NOTE: No SIGALRM handler — per-request timeouts in kalshi_client.py
    # protect individual HTTP calls from hanging. A cycle-level alarm can
    # interrupt an in-flight request, leaving connections in a bad state.
    log.info("Loop every %ds. Ctrl+C to stop.", args.interval)
    cycle_count = 0
    while state.running:
        cycle_start = time.time()
        try:
            cycle(client)
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
            elif state.resilience.rebuilds >= state.resilience.settings.max_rebuilds:
                # Max rebuilds exhausted — stop instead of looping forever
                # on failing cycles.
                log.error("Max client rebuilds (%d) exhausted — stopping.",
                          state.resilience.settings.max_rebuilds)
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

    # Persist bid history on clean shutdown so the stability gate doesn't have
    # to re-warm for STABILITY_LOOKBACK_MIN on the next start.
    try:
        se._force_save_bid_hist()
    except Exception as e:
        log.warning("Could not save bid history on shutdown: %s", e)

    # Release the HTTP session's sockets on clean shutdown.
    try:
        client.close()
    except Exception:
        pass

    log.info("Done. P&L=$%.2f | Cycles=%d | Trades=%d",
             state.session_pnl, state.cycles, state.trades_placed)


if __name__ == "__main__":
    lock_path = Path(__file__).resolve().parent / ".lock"
    lock_fd = None
    for attempt in range(5):
        try:
            # Open lock file (create if needed)
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)
            # Try to acquire exclusive non-blocking lock
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except (OSError, BlockingIOError):
            # Lock held by another process
            if attempt < 4:
                log.warning("Lock held by another process — retrying in 1s (attempt %d/5)", attempt + 2)
                time.sleep(1)
                if lock_fd:
                    os.close(lock_fd)
                    lock_fd = None
                continue
            else:
                if os.environ.get("DRY_RUN", "true").lower() == "true" or os.environ.get("ONCE_MODE"):
                    log.warning("Could not acquire lock after 5 attempts — proceeding anyway (once mode)")
                else:
                    log.error("Could not acquire lock after 5 attempts — another instance running")
                    sys.exit(1)
    else:
        # This runs if the loop wasn't broken (shouldn't happen)
        if os.environ.get("DRY_RUN", "true").lower() == "true" or os.environ.get("ONCE_MODE"):
            print("Could not acquire lock after 5 attempts — proceeding anyway (once mode)")
        else:
            log.error("Could not acquire lock after 5 attempts — another instance running")
            sys.exit(1)
    # Write PID to lock file for debugging
    with os.fdopen(lock_fd, "w") as f:
        f.write(str(os.getpid()))
    try:
        main()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            os.unlink(lock_path)
        except (OSError, FileNotFoundError):
            pass
