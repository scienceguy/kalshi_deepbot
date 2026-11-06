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
from local_db import get_local_db_api, get_local_db  # noqa: E402
from n8n_client import get_client as get_n8n_client, n8n_health_check  # noqa: E402
from kalshi_client import KalshiClient  # noqa: E402
from market_finder import find_active_markets, _parse_api_time, _dollars_to_cents, _parse_market_prices, _settle_yes_cents  # noqa: E402
from risk import RiskManager  # noqa: E402
from config import MIN_CASH_ABSOLUTE, CASH_FLOOR_PCT, DRAWDOWN_HALT_PCT, MARKET_DAILY_LOSS_PCT  # noqa: E402
from signals import floor_contracts, TradeSignal  # noqa: E402
import signals as se  # noqa: E402
from resilience import CycleSupervisor  # noqa: E402
from llm_eval import should_block_trade, evaluate_trade  # noqa: E402
import paper_mode  # noqa: E402

lf = Path(config.LOG_FILE).expanduser()
lf.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(lf)],
)
log = logging.getLogger("trader")

# Global LocalDB client (lazy) — primary write path with background Supabase sync
_local_db_api = None
# Raw Supabase client for direct reads (e.g., reconciliation)
_raw_supabase = None


def get_supabase_client():
    """Get the local database API (SupabaseClient-compatible interface).
    
    All writes go to local SQLite immediately (fast, reliable, offline-capable).
    A background thread syncs changes to Supabase periodically.
    """
    global _local_db_api
    if _local_db_api is None:
        _local_db_api = get_local_db_api()
    return _local_db_api


def get_raw_supabase_client():
    """Get the raw Supabase client for direct Supabase reads (e.g., reconciliation)."""
    global _raw_supabase
    if _raw_supabase is None:
        from supabase_client import get_supabase
        _raw_supabase = get_supabase()
    return _raw_supabase

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
        self.paper_resting: dict = {}  # oid -> {"sig": sig, "placed_at": ts}
        self.live_resting_orders: list = []  # raw resting orders seen this cycle
        self.last_order_time: float = 0.0
        self.orders_today: int = 0
        self.today: str = ""

    def _pre_trade_check(self, sig, market, cash, exposure, total_value, client) -> bool:
        """Pre-trade risk checks including LLM evaluation FIRST."""
        ticker = market.ticker

        # LLM evaluation FIRST - NVIDIA via integrate.api.nvidia.com
        # Requires both an API key and explicit LLM_EVALUATION_ENABLED=true.
        # Only evaluate if we're within the entry window.
        minutes_in = 15.0 - market.minutes_left
        # Any of the 7 layers may serve the verdict, so do not require the
        # NVIDIA key specifically — a missing one skipped the whole gate.
        if config.LLM_EVALUATION_ENABLED:
            if config.ENTRY_WINDOW_START_MIN <= minutes_in <= config.ENTRY_WINDOW_END_MIN:
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
                if should_block and not config.LLM_EVAL_ENFORCE:
                    log.info(
                        "LLM DISAGREE %s (prob=%s) — advisory only, not enforced",
                        ticker,
                        eval_result["probability_of_net_profit"] if eval_result else "N/A",
                    )
                    should_block = False
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
                    except Exception as e:
                        log.warning("Supabase insert_signal failed for %s: %s", ticker, e)
                    return False

                # Record LLM insight. eval_result is None when the eval failed
                # open — indexing it crashed every cycle and, with fail-open on,
                # stopped the bot from placing any order at all.
                if eval_result:
                    self.llm_trade_insights[ticker.upper()] = {
                        "probability": eval_result["probability_of_net_profit"],
                        "hold_confidence": eval_result["confidence_hold_to_expiry"],
                        "layer": eval_result.get("layer", ""),
                        "timestamp": time.time(),
                    }
                    # Persist the insight so the LLM's verdict is auditable next
                    # to the order, instead of living only in memory.
                    try:
                        get_supabase_client().insert_signal({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "ticker": ticker,
                            "side": sig.side,
                            "price_cents": sig.entry_price,
                            "contracts": sig.contracts,
                            "reason": (f"LLM pass via {eval_result.get('layer', '?')}: "
                                       f"{eval_result.get('reasoning', '')}")[:500],
                            "executed": False,
                            "blocked_reason": None,
                        })
                    except Exception as e:
                        log.warning("Supabase insert_signal (LLM insight) failed for %s: %s", ticker, e)

        # Per-window budget gate
        remaining_window = max(0.0, self.window["budget"] - self.window["used"])

        hard_cap = remaining_window

        if sig.dollar_risk > hard_cap + WINDOW_EPSILON:
            cost_per = sig.entry_price / 100.0
            n = floor_contracts(hard_cap / cost_per) if cost_per > 0 else 0.0
            if n < 0.01:
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
            market_daily_loss = self.risk.state.market_daily_losses.get(ticker, 0.0)
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
            paper=config.DRY_RUN,
        )
        if not allowed:
            log.info("RISK BLOCK %s — %s", ticker, reason)
            return False

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
        filled = floor_contracts(fill_count) if fill_count else floor_contracts(sig.contracts)
        filled_risk = filled * sig.entry_price / 100.0
        self.positions[ticker] = {
            "side": sig.side,
            "entry_price": sig.entry_price,
            "contracts": filled,
            "order_id": oid,
            "paper": config.DRY_RUN,
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
    """Namespaced client order id so our positions are identifiable from
    exchange position data (prevents adopting foreign positions on restart)."""
    return "sb_" + uuid.uuid4().hex


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
    except Exception as e:
        log.warning("Failed to load dry state: %s — starting fresh", e)
    return None


def _save_dry_state(cash: float):
    try:
        ds = Path(__file__).resolve().parent / "dry_state.json"
        with open(ds, "w") as f:
            json.dump({"cash": round(cash, 4)}, f)
    except Exception as e:
        log.warning("Failed to save dry state: %s", e)


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
    yp = _dollars_to_cents(p.get("yes_avg_price_dollars", 0) or 0) / 100.0
    np_ = _dollars_to_cents(p.get("no_avg_price_dollars", 0) or 0) / 100.0
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
        v = resp.get("fill_count")
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
    """Load pending settlements from the local queue file.
    
    Uses file locking to prevent race conditions if multiple processes
    access the queue (e.g., bot restart + cron job).
    """
    try:
        with open(SETTLEMENT_QUEUE_FILE) as f:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        log.error("Settlement queue file corrupted: %s — starting fresh", e)
        return []
    except Exception as e:
        log.error("Failed to load settlement queue: %s — starting fresh", e)
        return []


def _save_settlement_queue(queue: list):
    """Persist pending settlements to the local queue file.
    
    Uses file locking and atomic write (temp file + rename) to prevent
    corruption and race conditions.
    """
    try:
        # Atomic write: write to temp file then rename
        tmp_file = SETTLEMENT_QUEUE_FILE.with_suffix(SETTLEMENT_QUEUE_FILE.suffix + ".tmp")
        with open(tmp_file, "w") as f:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(queue, f)
        os.replace(tmp_file, SETTLEMENT_QUEUE_FILE)
    except Exception as e:
        log.error("Failed to save settlement queue: %s — QUEUE NOT PERSISTED", e)
        # Don't silently drop the queue; caller will retry on next cycle
        raise


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
    buy_cost = fees.buy_cash_out(contracts, entry_price / 100.0, taker=False)
    sell_revenue = fees.sell_cash_in(contracts, exit_price, taker=False)
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
                new_session_pnl = state.session_pnl + pnl
                # Write PnL to Supabase FIRST, then update local state
                if get_supabase_client().set_bot_state_batch({"session_pnl": new_session_pnl}):
                    state.session_pnl = new_session_pnl
                log.debug("Replayed settlement PnL $%.2f persisted to Supabase", new_session_pnl)
            log.info("Replayed settlement for %s", payload.get("order_id"))
        except Exception as e:
            log.warning("Replay settlement failed for %s — re-queueing: %s",
                        payload.get("order_id"), e)
            remaining.append(entry)
    _save_settlement_queue(remaining)


# CYCLE
# ═════════════════════════════════════════════════════════════════════

def _exit_quote(client, ticker: str):
    """(bid_cents, minutes_left) for the side we want out of, or None.

    Fetched per position rather than from find_active_markets(): that list is
    filtered to the entry window/band, so an open position whose price moved
    out of the band would never be seen and never exit.
    """
    mkt = client.get_market(ticker)
    if not mkt:
        return None
    yes_bid, _, no_bid, _, _, _ = _parse_market_prices(mkt)
    close = _parse_api_time(mkt.get("close_time") or mkt.get("expected_expiration_time") or "")
    if close is None:
        return None
    minutes_left = (close - datetime.now(timezone.utc)).total_seconds() / 60.0
    return yes_bid, no_bid, minutes_left


def _book_exit(client, ticker: str, pos: dict, exit_cents: int, filled: float, reason: str):
    """Book realized P&L for an early exit.

    Mirrors the settlement accounting (paper ledger, idempotent local ledger,
    session P&L, risk outcomes) but tags the webhook action as 'exit' so the
    settlement replay queue never treats it as a settlement.
    """
    side = pos.get("side", "")
    entry_price = pos.get("entry_price", 0)
    order_id = pos.get("order_id", ticker)
    is_paper = bool(pos.get("paper"))

    exit_price = exit_cents / 100.0
    buy_cost = fees.buy_cash_out(filled, entry_price / 100.0, taker=False)   # maker entry
    sell_revenue = fees.sell_cash_in(filled, exit_price, taker=True)         # taker exit
    realized_pnl = sell_revenue - buy_cost
    won = sell_revenue > buy_cost
    # Fee deducted inside sell_cash_in uses taker_fee_dollars (6-decimal);
    # match it exactly instead of recomputing via taker_fee_cents (cent-ceiled).
    fees_dollars = fees.taker_fee_dollars(filled, exit_price)

    state.risk.record_trade_outcome(won, realized_pnl)
    if not won:
        state.risk.record_market_loss(ticker, abs(realized_pnl))
    if is_paper:
        paper_mode.add_pnl(client.get_balance(), realized_pnl)

    key = f"exit:{ticker}:{order_id}"
    try:
        applied = get_local_db().apply_settlement_pnl(
            key, ticker, realized_pnl, fee_dollars=fees_dollars, source="local")
        pnl_to_add = 0.0 if applied is False else realized_pnl
    except Exception:
        log.warning("Failed to record exit P&L for %s — using fallback", ticker)
        pnl_to_add = realized_pnl

    new_session_pnl = state.session_pnl + pnl_to_add
    try:
        if get_supabase_client().set_bot_state_batch({"session_pnl": new_session_pnl}):
            state.session_pnl = new_session_pnl
    except Exception:
        log.warning("Supabase session_pnl write failed — keeping local state only")
        state.session_pnl = new_session_pnl

    log.info(
        "EXIT %s (%s) %.0fx @%dc -> @%dc, P&L $%+.2f, won=%s",
        ticker, reason, filled, entry_price, exit_cents, realized_pnl, won,
    )

    try:
        get_n8n_client().execute_webhook("superbot-trade", {
            "action": "exit",
            "order_id": order_id,
            "status": "exited",
            "fill_price_cents": int(exit_cents),
            "outcome_won": won,
            "pnl_dollars": realized_pnl,
            "ticker": ticker,
            "side": side,
            "contracts": filled,
        }, max_retries=1)
    except Exception:
        log.warning("Failed to record exit for %s via n8n webhook (not queued — exits are not settlements)", ticker)


def _manage_paper_fills(markets, client):
    """Fill paper maker orders only when the book actually crosses them.

    A posted bid does not fill because we posted it. It fills when someone
    sells into it, which shows up as that side's ask coming down to our price.
    Filling at placement overstated the dry run's trade count and compounding
    speed, which is the number we tune against.
    """
    by_ticker = {m.ticker: m for m in markets}
    now = time.time()
    for oid, rec in list(state.paper_resting.items()):
        sig = rec["sig"]
        ticker = sig.market.ticker
        m = by_ticker.get(ticker)
        if m is None:
            # Window closed (or market no longer returned) — the order died with it.
            state.paper_resting.pop(oid, None)
            state.resting_value.pop(oid, None)
            continue
        ask = m.yes_ask if sig.side == "yes" else m.no_ask
        if ask and ask <= sig.entry_price:
            state._record_filled_order(
                client, sig, oid, sig.contracts, "executed", 0.0, state.cycles
            )
            state.paper_resting.pop(oid, None)
            client.invalidate_markets(series_ticker=sig.market.series)
        elif now - rec["placed_at"] > config.STALE_ORDER_MINS * 60:
            state.paper_resting.pop(oid, None)
            state.resting_value.pop(oid, None)
            log.info("Paper order %s unfilled after %.0fmin — cancelled",
                     ticker, config.STALE_ORDER_MINS)


def clear_resting_orders(client, ticker: str):
    """Cancel every resting order on `ticker` (Kalshi blocks a close while any exist)."""
    for o in list(state.live_resting_orders):
        if o.get("ticker") != ticker:
            continue
        oid = o.get("order_id") or o.get("id")
        if not oid:
            continue
        if not config.DRY_RUN:
            try:
                client.cancel_order(oid)
            except Exception as e:
                log.debug("Could not cancel resting order %s: %s", oid, e)
        state.resting_value.pop(oid, None)
        state.live_resting_orders.remove(o)
    # A parked paper entry on this ticker must go too, or it would fill after exit.
    for oid, rec in list(state.paper_resting.items()):
        if rec["sig"].market.ticker == ticker:
            state.paper_resting.pop(oid, None)
            state.resting_value.pop(oid, None)


def _manage_exits(client):
    """Sell open positions that hit the profit target or the stop.

    Positions that are simply left to expiry lock their cash for the rest of
    the window; exiting at the bid frees it for the next entry. The exit crosses
    the spread (taker), so it actually fills and pays the taker fee. Kalshi
    blocks a close while any resting order exists on the market, so
    clear_resting_orders() runs first.
    """
    if not config.EXIT_ENABLED:
        return
    for ticker, pos in list(state.positions.items()):
        side = pos.get("side")
        entry = pos.get("entry_price", 0)
        contracts = pos.get("contracts", 0)
        if side not in ("yes", "no") or contracts <= 0 or entry <= 0:
            continue
        try:
            quote = _exit_quote(client, ticker)
        except Exception as e:
            log.warning("Exit quote fetch failed for %s: %s", ticker, e)
            continue
        if quote is None:
            continue

        yes_bid, no_bid, minutes_left = quote
        if minutes_left <= config.EXIT_MIN_MINUTES_LEFT:
            continue  # settlement is imminent and free; an exit adds queue risk for nothing
        bid = yes_bid if side == "yes" else no_bid
        if not bid or not 1 <= bid <= 99:
            continue

        delta = bid - entry
        if delta >= config.EXIT_TAKE_PROFIT_CENTS:
            reason = f"take profit +{delta}c"
        elif -delta >= config.EXIT_STOP_LOSS_CENTS:
            reason = f"stop loss {delta}c"
        else:
            continue

        # Kalshi blocks an early-exit sell while ANY resting order exists on the
        # market, so clear every resting order for this ticker first: any
        # earlier unfilled exit, and any resting entry that would add contracts.
        clear_resting_orders(client, ticker)
        pos.pop("exit_order_id", None)

        try:
            resp = client.sell_position(
                ticker, side, contracts, int(bid),
                client_order_id=f"exit-{ticker}-{int(time.time())}",
            )
        except Exception as e:
            log.error("Exit order failed for %s: %s", ticker, e)
            continue
        if resp is None:
            continue

        exit_oid = (resp.get("order") or {}).get("order_id") or resp.get("order_id", "")
        if exit_oid:
            pos["exit_order_id"] = exit_oid

        filled = floor_contracts(contracts) if config.DRY_RUN else _order_fill_count(resp)
        if filled <= 0:
            log.info("Exit order for %s did not fill — retrying next cycle", ticker)
            continue

        # Only remove what was actually sold: an IOC sell can fill partially,
        # and popping the whole position would drop the unsold contracts from
        # local state (they would reappear next cycle at a recomputed basis).
        remaining = round(contracts - filled, 2)
        if remaining >= 0.01:
            pos["contracts"] = remaining
            state.position_risk[ticker] = remaining * entry / 100.0
        else:
            state.positions.pop(ticker, None)
            state.position_risk.pop(ticker, None)
        _book_exit(client, ticker, pos, int(bid), filled, reason)
        client.invalidate_markets(series_ticker=ticker.split("-")[0])


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

    # Dry runs place no real orders, so the exchange balance never moves. Swap
    # in a paper balance derived from recorded settlement P&L so the dry run
    # actually compounds and its sizing tracks the curve.
    if config.DRY_RUN and cash is not None:
        cash = paper_mode.cash(cash, sum(state.position_risk.values()))

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

    # Reconcile resting orders: fetch open orders from Kalshi, sync into
    # state.resting_value for intra-cycle cash tracking, and cancel any
    # order older than STALE_ORDER_MINS so stale limit orders don't tie up
    # capital indefinitely. Live only — a dry run has no exchange orders, and
    # the empty list would wipe the paper reservations every cycle.
    resting_orders = [] if config.DRY_RUN else client.get_orders(status="resting")
    state.live_resting_orders = resting_orders or []
    try:
        now = time.time()
        # In a dry run the exchange has no orders, so the live set would be
        # empty and the prune below would erase every paper reservation each
        # cycle — letting the bot park more bids than it has cash for.
        live_ids = set(state.paper_resting) if config.DRY_RUN else set()
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
                except Exception as e:
                    log.warning("Failed to cancel stale order %s: %s", oid, e)
        # Prune reservations for orders no longer resting (filled or cancelled),
        # otherwise stale entries permanently shrink available cash and the bot
        # eventually stops trading while believing cash is reserved.
        for stale_oid in list(state.resting_value):
            if stale_oid not in live_ids:
                state.resting_value.pop(stale_oid, None)
    except Exception as e:
        log.warning("Could not reconcile resting orders: %s", e)

    if config.DRY_RUN:
        _manage_paper_fills(markets, client)

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
        # Only adopt positions this bot created (order_id starts with "sb_").
        # Foreign positions from another bot or a manual trade are left alone
        # — booking them would misattribute P&L and corrupt risk math.
        order_id = p.get("order_id", "")
        if not order_id.startswith("sb_"):
            continue
        state.positions[tk] = {
            "side": side, "entry_price": entry_price,
            "contracts": contracts, "order_id": order_id,
        }

    # Sync positions from Kalshi API — clear settled tickers from state
    api_tickers = {p.get("ticker", "") for p in positions if "15M" in p.get("ticker", "").upper()}
    settled = [t for t in list(state.positions.keys()) if t not in api_tickers]
    for t in settled:
        # A paper position never appears in the exchange position list, so it
        # looks "settled" the moment it is opened. Only clear it once the market
        # has a real settlement value, otherwise the paper trade is dropped
        # before it can book P&L.
        if state.positions.get(t, {}).get("paper"):
            try:
                if _settle_yes_cents(client.get_market(t)) is None:
                    continue
            except Exception:
                continue
        pos_info = state.positions.pop(t, {})
        reserved = state.position_risk.pop(t, 0.0)
        is_paper = bool(pos_info.get("paper"))
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
            buy_cost = fees.buy_cash_out(contracts, entry_price / 100.0, taker=False)
            sell_revenue = fees.sell_cash_in(contracts, exit_price, taker=False)
            realized_pnl = sell_revenue - buy_cost
            won = sell_revenue > buy_cost

            # Fees in dollars for the order
            # Maker limit order: Kalshi charges the taker, so entry fees are 0.
            fees_dollars = 0.0

            log.info(
                "Cleared settled position: %s (side=%s, released $%.2f, realized P&L $%.2f, won=%s)",
                t, side, reserved, realized_pnl, won,
            )

            # Track consecutive losses and per-market daily loss for risk cooldown
            state.risk.record_trade_outcome(won, realized_pnl)
            if not won:
                state.risk.record_market_loss(t, abs(realized_pnl))

            # --- Markout instrumentation: record fill-vs-settlement toxicity ---
            # markout_cents = settlement_payout - fill_price (positive = filled
            # below eventual payout = good fill for us). Tagged by side so we
            # can later compute fill-conditioned adverse selection by moneyness
            # band and time-to-expiry.
            settled_yes_cents = int(round(settled_yes)) if settled_yes is not None else None
            if settled_yes_cents is not None:
                if side == "yes":
                    markout_cents = settled_yes_cents - entry_price
                else:  # NO side: payout = 100 - settled_yes
                    markout_cents = (100 - settled_yes_cents) - entry_price
                markout_rec = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "ticker": t,
                    "side": side,
                    "fill_price_cents": entry_price,
                    "settle_price_cents": settled_yes_cents,
                    "markout_cents": markout_cents,
                    "contracts": contracts,
                    "realized_pnl_dollars": realized_pnl,
                    "won": won,
                }
                try:
                    mf_path = Path(__file__).resolve().parent / "markout_log.jsonl"
                    with open(mf_path, "a") as mf:
                        mf.write(json.dumps(markout_rec) + "\n")
                except Exception:
                    log.warning("Failed to write markout log for %s", t)

            # Credit the paper ledger so the dry-run curve compounds. The local
            # settle_key ledger below is shared with live runs, so paper P&L is
            # kept separate to keep the two curves from contaminating.
            if is_paper:
                paper_mode.add_pnl(client.get_balance(), realized_pnl)

            # Record settlement P&L idempotently — use idempotency key so replays
            # after a crash can't double-count. Key is "settle:<ticker>:<order_id>".
            settle_key = f"settle:{t}:{order_id}"
            try:
                applied = get_local_db().apply_settlement_pnl(
                    settle_key, t, realized_pnl,
                    fee_dollars=fees_dollars,
                    source="local",
                )
                if applied is False:
                    log.info("Settlement P&L already recorded for %s — skipping duplicate", t)
                    # Still queue webhook for n8n but don't credit local session_pnl again
                    pnl_to_add = 0.0
                else:
                    pnl_to_add = realized_pnl
            except Exception:
                log.warning("Failed to record settlement P&L to local ledger for %s — using fallback", t)
                pnl_to_add = realized_pnl

            new_session_pnl = state.session_pnl + pnl_to_add
            # Write PnL to Supabase FIRST, then update local state.
            # This ensures: if process crashes after Supabase write, local state is
            # reconciled on restart.
            try:
                if get_supabase_client().set_bot_state_batch({"session_pnl": new_session_pnl}):
                    state.session_pnl = new_session_pnl
                log.debug("Session PnL updated to $%.2f and persisted to Supabase", state.session_pnl)
            except Exception:
                log.warning("Supabase session_pnl write failed — keeping local state only", t)
                state.session_pnl = new_session_pnl

            # Invalidate market cache after settlement — the market is no longer open.
            series = t.split("-")[0] if "-" in t else t
            client.invalidate_markets(series_ticker=series)
            # Fire n8n webhook — if this fails, queue replay handles it.
            # Runs regardless of Supabase write success/failure so settlement
            # P&L always routes through n8n.
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

    # Sell positions that hit the profit target or stop, so cash compounds
    # inside the same 15m window instead of sitting locked until settlement.
    try:
        _manage_exits(client)
    except Exception as e:
        log.warning("Exit management failed: %s", e)

    # Compute mark-to-market exposure using current orderbook data for positions
    # (single pass via _mark_to_market_exposure; the previous duplicate loop
    # here was identical to the one above and computed the same value twice).
    mark_to_market_exposure = _mark_to_market_exposure(positions, markets)
    if config.DRY_RUN:
        # Paper fills never appear on the exchange account, so mark exposure from
        # the local paper book — otherwise the ceiling and drawdown halt are inert.
        mark_to_market_exposure = sum(state.position_risk.values())

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
    if config.DRY_RUN and cash_available:
        start = paper_mode.start_balance(cash)
        pct = (cash - start) / start * 100 if start else 0.0
        log.info(
            "📈 paper: start=$%.2f pnl=$%+.2f open=$%.2f cash=$%.2f (%+.1f%%)",
            start, paper_mode.pnl(cash), sum(state.position_risk.values()), cash, pct,
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
            if get_supabase_client().set_bot_state_batch(current_state):
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

    if config.DRY_RUN:
        open_count = len(state.positions)
    else:
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
        paper=config.DRY_RUN,
    )
    if cash_available and max_trade < 0.01:
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

    # Latency-arb is deliberately NOT wired in: it buys the underdog at the
    # taker price (e.g. NO at 3c when YES is 98c), which is the opposite of the
    # favourite-maker strategy and pays taker fees. Keep it disabled until a
    # real edge is measured; the module is still here for experiments.

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
            paper=config.DRY_RUN,
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
            if config.DRY_RUN:
                # A maker bid does not fill because we posted it. Park it and let
                # _manage_paper_fills() fill it only once the book crosses.
                state.paper_resting[oid] = {"sig": sig, "placed_at": time.time()}
                fill_count = 0.0
            if fill_count > 0:
                fees_dollars = 0.0  # maker limit order — no taker fee
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
    log.info("Risk: %.0f%% cash floor | %.0f%% exposure ceiling | %.0f%% per-trade | %.0f%% drawdown halt%s",
             (config.PAPER_CASH_FLOOR_PCT if config.DRY_RUN else config.CASH_FLOOR_PCT) * 100,
             (config.PAPER_EXPOSURE_CEILING_PCT if config.DRY_RUN else config.EXPOSURE_CEILING_PCT) * 100,
             (config.PAPER_PER_TRADE_PCT if config.DRY_RUN else config.PER_TRADE_PCT) * 100,
             config.DRAWDOWN_HALT_PCT * 100,
             " (paper — dry runs fill at the posted maker price and settle for real)" if config.DRY_RUN else "")
    log.info("LLM eval: %s | Supabase: %s",
             "enabled (NVIDIA)" if config.NVIDIA_API_KEY else "disabled",
             "enabled" if get_supabase_client().enabled else "disabled")
    # Supabase is non-fatal — writes degrade gracefully when credentials are
    # missing or the service is down. The bot trades without it (dry-run).
    log.info("=" * 58)

    # Validate credentials, dry-run mode, and bands before connecting to Kalshi.
    # The local .env sets DRY_RUN=false, which overrides the safe default of True.
    try:
        config.validate_credentials()
    except ValueError as e:
        log.error("Credential validation failed: %s — continuing without validation", e)
    if not config.validate_dry_run(live_explicit=args.live):
        log.error("Dry-run validation failed — refusing to start")
        return
    try:
        config.validate_bands()
    except ValueError as e:
        log.error("Band validation failed: %s — continuing without band checks", e)

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
    n8n_ok = n8n_health_check()
    if not n8n_ok:
        log.warning("n8n health check failed — settlement webhooks may not work. "
                    "Bot continues trading without n8n routing.")
    log.info("n8n health check %s", "✓" if n8n_ok else "✗")

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

    # Validate the NO market-data format against a live market. This only
    # affects how NO quotes are read; order pricing always sends the YES-leg
    # complement (see KalshiClient.no_price_to_api_price).
    try:
        markets = client.get_markets(status="open", limit=5)
        if markets:
            sample = markets[0]
            has_no_ask = "no_ask" in sample and sample.get("no_ask") is not None
            has_no_ask_dollars = "no_ask_dollars" in sample and sample.get("no_ask_dollars") is not None
            has_no_price_dollars = "no_price_dollars" in sample and sample.get("no_price_dollars") is not None
            if has_no_ask or has_no_ask_dollars or has_no_price_dollars:
                log.info("NO price format validated: market has NO price fields (complement=False)")
            else:
                log.warning(
                    "NO price format UNVERIFIED: market %s lacks NO price fields. "
                    "NO quotes may be read inverted. Check how the API reports "
                    "NO prices and set NO_PRICE_IS_COMPLEMENT accordingly.",
                    sample.get("ticker", "unknown"),
                )
        else:
            log.warning("No markets available to validate NO price format")
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

    # Reconcile session_pnl on startup — prevents P&L drift.
    # Read from LOCAL DB first (may have newer data not yet synced to Supabase),
    # then fall back to Supabase if local is missing/zero.
    try:
        local_db = get_local_db()
        local_pnl = local_db.get_bot_state("session_pnl")
        if local_pnl is not None and local_pnl != 0:
            state.session_pnl = float(local_pnl)
            log.info("Reconciled session_pnl from LOCAL DB: $%.2f", state.session_pnl)
        else:
            # Fall back to Supabase
            saved_pnl = get_raw_supabase_client().get_bot_state("session_pnl")
            if saved_pnl is not None:
                state.session_pnl = float(saved_pnl)
                log.info("Reconciled session_pnl from Supabase: $%.2f", state.session_pnl)
    except Exception:
        log.warning("Could not reconcile session_pnl — starting at $0.00")

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

    # Stop local DB background sync thread so unsynced rows aren't lost
    # on unclean shutdown.
    try:
        get_local_db().close()
    except Exception as e:
        log.warning("Could not stop local DB sync on shutdown: %s", e)

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
                # Never proceed without the lock. The old check read the shell's
                # DRY_RUN, but --live only changes config.DRY_RUN, so a live bot
                # started with the documented env saw "dry run" and ran anyway —
                # two live bots, one account, two ideas of available cash.
                log.error("Could not acquire lock after 5 attempts — another instance running")
                sys.exit(1)
    else:
        # This runs if the loop wasn't broken (shouldn't happen)
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
