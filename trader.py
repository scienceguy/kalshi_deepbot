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
import logging.handlers  # noqa: E402  — needed for RotatingFileHandler (submodule)
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import config  # noqa: E402

# Parse args FIRST so env is set before config loads
parser = argparse.ArgumentParser()
_mode = parser.add_mutually_exclusive_group()
_mode.add_argument("--live", action="store_true", help="Enable LIVE trading (overrides DRY_RUN=true)")
_mode.add_argument("--dry", action="store_true")
parser.add_argument("--once", action="store_true")
parser.add_argument("--debug", action="store_true")
parser.add_argument("--interval", type=int, default=config.LOOP_INTERVAL_SECONDS)
parser.add_argument("--cycle-limit", type=int, default=0,
                    help="stop after this many cycles (0 = disabled)")
parser.add_argument("--ml-model", type=str, default=None,
                    help="path to saved torch fair-value model (ml_model.py). "
                         "When given, ML fair value overrides venue-implied fair "
                         "as an additional entry filter.")
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


class _LazyModule:
    """Import a heavy optional module on first attribute access.

    ml_model imports torch, which is not in requirements.txt and is hard to
    install on aarch64/Termux. Importing it at module load made the whole bot
    unimportable without torch even though it is only needed for --ml-model.
    """

    def __init__(self, name):
        self._name = name
        self._mod = None

    def __getattr__(self, attr):
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)


ml_model = _LazyModule("ml_model")  # noqa: E402
from local_db import get_local_db_api, get_local_db  # noqa: E402
from n8n_client import get_client as get_n8n_client, n8n_health_check  # noqa: E402
from kalshi_client import KalshiClient, CRYPTO_EXCHANGE_INDEX  # noqa: E402
from market_finder import find_active_markets, _parse_api_time, _dollars_to_cents, _parse_market_prices, _settle_yes_cents  # noqa: E402
from risk import RiskManager  # noqa: E402
from config import MIN_CASH_ABSOLUTE, CASH_FLOOR_PCT, DRAWDOWN_HALT_PCT, MARKET_DAILY_LOSS_PCT  # noqa: E402
from signals import floor_contracts, TradeSignal  # noqa: E402
import signals as se  # noqa: E402
from resilience import CycleSupervisor, systemd_notify  # noqa: E402
from llm_eval import should_block_trade, evaluate_trade  # noqa: E402
import paper_mode  # noqa: E402
import gates  # noqa: E402
import binance_feed  # noqa: E402  — WebSocket spot streams (start_ws_streams)

# Polymarket US client (order placement infra ready for when crypto markets
# launch on production; currently preprod only — no crypto updown markets on
# the US retail API). Imported lazily so the bot starts without Polymarket
# credentials present.
_poly_client = None

# Lazy-loaded modules (only imported when actually used)
_ladder_strategy = None
_local_module = None

# Lazy-loaded ML fair-value model (only when --ml-model is passed).
_ml_model = None


def _load_ml_model(path: str):
    """Lazy-load a saved torch fair-value model. Returns None on failure."""
    global _ml_model
    if _ml_model is not None:
        return _ml_model
    try:
        _ml_model = ml_model.MLFairValue.load(path)
        log.info("ML fair-value model loaded from %s", path)
    except Exception as e:
        log.warning("Failed to load ML model from %s: %s — continuing without ML", path, e)
    return _ml_model


log_target = config.LOG_FILE if not config.DRY_RUN else os.environ.get("DRY_LOG_FILE", "bot_dry.log")
lf = Path(log_target).expanduser()
if not lf.is_absolute():  # reviewed: ensure log file path is anchored to repo directory
    lf = Path(__file__).resolve().parent / lf
lf.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.handlers.RotatingFileHandler(
                  lf, maxBytes=config.LOG_MAX_BYTES,
                  backupCount=config.LOG_BACKUP_COUNT)],
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
SETTLED_SINCE_FILE = Path(__file__).resolve().parent / "settled_since.json"
PAPER_POSITIONS_FILE = Path(os.environ.get("PAPER_POSITIONS_FILE")
                            or (Path(__file__).resolve().parent / "paper_positions.json"))


def _load_paper_positions() -> dict:
    """Paper positions survive a restart.

    Positions used to live only in memory, so every restart dropped the open
    fills of the current 15-minute window and their settlement P&L was never
    booked — the dry-run curve silently lost trades. Live mode is unaffected:
    it re-reads real positions from the exchange.
    """
    if not config.DRY_RUN:  # reviewed: live reads real positions from the exchange; only paper persists to disk
        return {}
    try:
        data = json.loads(PAPER_POSITIONS_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("Could not read %s (%s) — starting with no paper positions",
                    PAPER_POSITIONS_FILE, e)
        return {}
    return data if isinstance(data, dict) else {}


def _save_paper_positions(positions: dict) -> None:
    """Persist paper positions atomically (no-op outside dry runs)."""
    if not config.DRY_RUN:  # reviewed: live reads real positions from the exchange; only paper persists to disk
        return
    try:
        tmp = PAPER_POSITIONS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(positions))
        os.replace(tmp, PAPER_POSITIONS_FILE)
    except Exception as e:
        log.warning("Could not write %s: %s", PAPER_POSITIONS_FILE, e)


def _load_settled_since() -> dict:
    try:
        return json.loads(SETTLED_SINCE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_settled_since(data: dict):
    tmp = SETTLED_SINCE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, SETTLED_SINCE_FILE)


class BotState:
    """Encapsulates all mutable bot state."""

    def __init__(self):
        self.positions: dict = _load_paper_positions()  # ticker -> order_id or "dry"
        self.resting_value: dict = {}      # order_id -> {"dollars": float, "ticker": str}
        self.resting_tickers: set = set()  # tickers with resting orders this cycle
        self.last_cash_log: float = -1.0
        self.session_pnl: float = 0.0
        self.realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.total_pnl: float = 0.0
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
            "trades_placed_in_window": 0,
        }
        self.position_risk: dict = {
            t: float(p.get("contracts", 0)) * float(p.get("entry_price", 0)) / 100.0
            for t, p in self.positions.items()
            if isinstance(p, dict)  # reviewed: only dict entries carry entry_price/contracts; legacy "dry" strings are skipped
        }
        self.llm_trade_insights: dict = {}
        self.live_resting_orders: list = []  # raw resting orders seen this cycle
        self.last_order_time: float = 0.0
        self.orders_today: int = 0
        self.today: str = ""
        self.consecutive_no_tradeable_cycles: int = int(os.environ.get("CONSECUTIVE_NO_TRADEABLE_CYCLES", 8))
        self.consecutive_no_trade_cycles: int = 0
        self.cycle_outcome: str = "pending"
        self.tradeable_markets_count: int = 0
        self.available_markets_count: int = 0
        self.available_active_markets: int = 0
        self.tradeable_brackets: int = 0

    def _pre_trade_check(self, sig, market, cash, exposure, total_value, client) -> bool:
        """Pre-trade risk checks (LLM evaluation moved to cycle() as pre-entry filter)."""
        ticker = market.ticker

        # Per-window budget gate — subtract resting (unfilled) orders so the
        # window cap can't be bypassed by multiple resting orders that each
        # individually fit but collectively exceed the budget.
        resting_dollars = sum(v["dollars"] for v in self.resting_value.values())
        remaining_window = max(0.0, self.window["budget"] - self.window["used"] - resting_dollars)

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
                "WINDOW CLAMP %s — %.4fx@%dc ($%.2f) -> %.2fx ($%.2f), remaining $%.2f",
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

        # If cash is unknown (balance fetch failed), skip order placement.
        # We can't safely evaluate risk without knowing available capital.
        if cash is None:
            log.info("CASH UNKNOWN — skipping order for %s", ticker)
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
        pending = sum(v["dollars"] for v in self.resting_value.values())
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

        # reviewed: on bankrolls under $10 enforce max 60% concurrent deployment so at least 40% is kept in cash reserve
        portfolio_equity = cash + exposure
        if portfolio_equity < 10.0:  # reviewed: cap concurrent exposure at 60% of equity on micro accounts under $10
            tradable_balance = portfolio_equity * 0.60
        else:
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
            resting_orders_value=sum(v["dollars"] for v in self.resting_value.values()),
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

    def _record_filled_order(self, client, sig, oid, fill_count, status,
                             fees_dollars, cycles, entry_taker: bool = False):
        ticker = sig.market.ticker
        # Record the ACTUAL filled contracts (int), not the intended size — a
        # partial fill would otherwise mis-book PnL at settlement and over-reserve
        # capital at risk.
        filled = floor_contracts(fill_count)
        if filled <= 0:
            log.warning("Attempted to record filled order with zero contracts for %s — skipping", ticker)
            return
        filled_risk = filled * sig.entry_price / 100.0
        self.positions[ticker] = {
            "side": sig.side,
            "entry_price": sig.entry_price,
            "contracts": filled,
            "order_id": oid,
            "paper": config.DRY_RUN,
            "entry_taker": bool(entry_taker),  # how THIS trade entered: taker pays fee, maker pays nothing
            "strike": float(sig.market.strike) if getattr(sig.market, "strike", None) else None,
        }
        self.window["entered"].add(ticker)
        info = self.resting_value.pop(oid, None)
        if isinstance(info, dict) and info.get("ticker"):
            self.resting_tickers.discard(info["ticker"])
        log.info("✓ %s order_id=%s (fills=%.2f)", ticker, oid, fill_count)
        self.window["used"] += filled_risk
        self.trades_placed += 1
        # Record locally first, then to Supabase
        order_payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "side": sig.side,
            "action": "buy",
            "contracts": filled,
            "price_cents": sig.entry_price,
            "order_id": oid,
            "status": status,
            "fees_dollars": fees_dollars or 0.0,
            "cycles": cycles,
        }
        try:
            get_local_db().insert_order(order_payload)
        except Exception as e:
            log.warning("LocalDB insert_order failed for %s: %s", ticker, e)
        try:
            get_supabase_client().insert_order(order_payload)
        except Exception:
            log.warning("Supabase insert_order failed for %s — order audit trail incomplete", ticker)
        # Track capital at risk per ticker — released on settlement, not booked as P&L.
        # Use filled_risk (actual fill) so partial fills don't over-reserve.
        self.position_risk[ticker] = filled_risk
        _save_paper_positions(self.positions)


# Global state
state = BotState()
state.settled_since = _load_settled_since()


# ═════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════

def _coid(ticker: str, side: str) -> str:
    """Namespaced client order id so our positions are identifiable from
    exchange position data (prevents adopting foreign positions on restart)."""
    return "sb_" + uuid.uuid4().hex


def _place_poly_order(sig) -> None:
    """Place a concurrent fee-free maker order on Polymarket US.

    Mirrors the Kalshi signal on the same asset/side. Polymarket US crypto
    Up/Down 15m markets are in PREPROD only (not production) as of 2026-11-13,
    so this is currently a no-op with a clear log message. When crypto markets
    launch on production, this path places a fee-free maker limit order
    (participateDontInitiate=True) at the same edge as the Kalshi entry.
    """
    # reviewed: skip concurrent order when polymarket execution is disabled in config
    if not getattr(config, "POLYMARKET_ENABLED", False):
        return
    global _poly_client
    # Lazy import so the bot starts without Polymarket credentials present; this
    # avoids importing polymarket_us on every cycle.
    # reviewed: lazy import only once; lets the bot start without credentials.
    if _poly_client is None:
        try:
            import polymarket_us_client as _pm
        except ImportError:
            log.debug("Polymarket SDK not installed — skipping concurrent order")
            return
        # reviewed: no credentials means no concurrent order; never blocks Kalshi.
        if not config.POLYMARKET_KEY_ID or not config.POLYMARKET_SECRET_KEY:
            log.debug("Polymarket credentials not configured — skipping concurrent order")
            return
        _poly_client = _pm

    # Extract asset from Kalshi ticker: KXBTC15M-... -> BTC
    ticker = getattr(sig.market, "ticker", "") or ""
    series = getattr(sig.market, "series", "") or ""
    asset = None
    for candidate in (series, ticker.split("-")[0]):
        base = candidate[2:] if candidate.startswith("KX") else candidate
        base = base[:-3] if base.endswith("15M") else base
        # reviewed: only map known crypto assets; unknown series must not be sent.
        if base in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC", "NEAR"):
            asset = base
            break
    # reviewed: no asset mapping means no Polymarket market to trade.
    if not asset:
        log.debug("No Polymarket asset mapping for %s — skipping concurrent order", ticker)
        return

    # Polymarket US slugs for crypto updown markets follow the pattern
    # cpc-{asset}-updown-15m-YYYY-MM-DD (preprod only). Since no production
    # markets exist yet, we cannot resolve a slug — log and skip.
    # When markets launch, resolve via GET /v1/markets?categories=crypto and
    # match on assetPriceTerms.asset.symbol == {asset.lower()}.
    log.debug(
        "Polymarket concurrent order skipped for %s (%s): no production crypto "
        "Up/Down 15m market on api.polymarket.us (preprod only). Set "
        "POLYMARKET_ENABLED=true and add slug resolution when markets launch.",
        ticker, asset,
    )


def _window_id(now: datetime) -> int:
    return int(now.timestamp() // (15 * 60))


def _touch_window(now: datetime, cash: float):
    """Reset per-window budget if a new 15m window begins."""
    wid = _window_id(now)
    if state.window["id"] == wid:
        if cash > state.window["start_cash"] + 1e-9:
            state.window["start_cash"] = cash
            state.window["budget"] = cash if cash < 10.0 else (cash * config.WINDOW_TRADE_PCT)
            state.window["reserved_cash"] = 0.0 if cash < 10.0 else max(0.0, cash * config.CASH_RESERVED_PCT)
            log.info(
                "Window budget refreshed — cash grew to $%.2f, budget=$%.2f reserved=$%.2f",
                cash, state.window["budget"], state.window["reserved_cash"],
            )
        return

    # Check if previous 15-minute window placed a trade
    # reviewed: log +1 to consecutive_no_tradeable_cycles with every 15-minute cycle that has no trade placed
    if state.window["id"] is not None:
        prev_trades = state.window.get("trades_placed_in_window", 0)
        # reviewed: increment consecutive no tradeable cycles when previous 15m window had no trade
        if prev_trades == 0:
            state.consecutive_no_tradeable_cycles += 1
            log.info(
                "15-minute cycle #%s ended without trades. consecutive_no_tradeable_cycles=%d",
                state.window["id"], state.consecutive_no_tradeable_cycles,
            )
        else:
            state.consecutive_no_tradeable_cycles = 0
            log.info(
                "15-minute cycle #%s placed %d trade(s). Reset consecutive_no_tradeable_cycles=0",
                state.window["id"], prev_trades,
            )

    state.window["id"] = wid
    state.window["start_cash"] = cash
    # reviewed: on micro-balances under $10, window budget covers single contract bootstrap without reserving cash
    if cash < 10.0:
        state.window["budget"] = cash
        state.window["reserved_cash"] = 0.0
    else:
        state.window["budget"] = cash * config.WINDOW_TRADE_PCT
        state.window["reserved_cash"] = max(0.0, cash * config.CASH_RESERVED_PCT)
    state.window["used"] = 0.0
    state.window["entered"] = set()
    state.window["trades_placed_in_window"] = 0
    log.info(
        "New 15m window — start_cash=$%.2f window_budget=$%.2f reserved=$%.2f consecutive_no_tradeable_cycles=%d",
        cash, state.window["budget"], state.window["reserved_cash"], state.consecutive_no_tradeable_cycles,
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
            return float(float(v))
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
    # reviewed: validate response is a dict before reading nested order object
    if not isinstance(resp, dict):
        return 0.0
    order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    v = order.get("fill_count_fp")
    if v is None:
        v = order.get("fill_count")
    if v is None:
        v = order.get("filledCount", 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _ticker_close_time(ticker: str) -> Optional[datetime]:
    """Parse close time directly from 15M ticker string (e.g. KXBTC15M-26NOV140000-00).

    Zero network latency, zero blocking I/O on the hot mark-to-market path.
    Kalshi 15M tickers encode close time in US Eastern Time (ET); this converts to UTC.
    """
    try:
        parts = ticker.split("-")
        # reviewed: ensure ticker string has series and date components
        if len(parts) >= 2:
            date_str = parts[1]
            dt_naive = datetime.strptime(date_str, "%y%b%d%H%M")
            month, day = dt_naive.month, dt_naive.day
            is_dst = (3 < month < 11) or (month == 3 and day >= 8) or (month == 11 and day < 7)
            offset_hours = 4 if is_dst else 5
            # reviewed: robust Eastern Time to UTC conversion for accurate close time comparison
            return dt_naive.replace(tzinfo=timezone.utc) + timedelta(hours=offset_hours)
    except Exception:
        pass
    return None


def _mark_to_market_exposure(positions, markets, client=None):
    """Mark-to-market exposure in dollars across all 15M positions.

    Uses the current orderbook mid when available; falls back to the
    position's cost basis (market_exposure_dollars) when the market is still open.
    Only ignores positions whose market has closed/settled to avoid double-counting
    with settlement cash payout.
    """
    mkt_by_ticker = {m.ticker: m for m in markets}
    exposure = 0.0
    now = datetime.now(timezone.utc)
    for p in positions:
        if "15M" not in p.get("ticker", "").upper():
            continue
        ticker = p.get("ticker", "")
        yes_pos = _pos_yes(p)
        no_pos = _pos_no(p)
        # reviewed: skip entries with no contracts open
        if not yes_pos and not no_pos:
            continue
        mkt_info = mkt_by_ticker.get(ticker)
        if mkt_info and (yes_pos or no_pos):
            val = 0.0
            # reviewed: validate mid_yes is not None before calculating MTM value
            if yes_pos and mkt_info.mid_yes is not None:
                val += yes_pos * mkt_info.mid_yes / 100.0
            # reviewed: validate mid_no is not None before calculating MTM value
            if no_pos and mkt_info.mid_no is not None:
                val += no_pos * mkt_info.mid_no / 100.0
            # reviewed: fall back to cost basis if mid value is zero or missing
            if val > 0:
                exposure += val
            else:
                exposure += _pos_cost_basis(p)
        else:
            ct = _ticker_close_time(ticker)
            is_closed = ct and ct <= now
            # reviewed: open unexpired positions retain cost basis exposure
            if not is_closed:
                exposure += _pos_cost_basis(p)
    return exposure


# ═════════════════════════════════════════════════════════════════════
# Settlement queue lock file for read-modify-write atomicity
SETTLEMENT_LOCK_FILE = SETTLEMENT_QUEUE_FILE.with_suffix(".lock")

def _load_settlement_queue() -> list:
    """Load pending settlements from the local queue file.

    Uses a dedicated lock file for read-modify-write atomicity.
    On corrupt JSON, renames the file to .corrupt-<timestamp> and logs ERROR.
    """
    try:
        with open(SETTLEMENT_LOCK_FILE, "w") as lf:
            import fcntl
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            except OSError:
                # flock may fail on FUSE/overlay filesystems; proceed without lock
                pass
            try:
                with open(SETTLEMENT_QUEUE_FILE) as f:
                    return json.load(f)
            except FileNotFoundError:
                return []
            except json.JSONDecodeError as e:
                # Preserve corrupt file for forensic analysis
                from datetime import datetime
                corrupt_name = SETTLEMENT_QUEUE_FILE.with_name(
                    f"{SETTLEMENT_QUEUE_FILE.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
                )
                try:
                    SETTLEMENT_QUEUE_FILE.rename(corrupt_name)
                except Exception:
                    pass
                log.error("Settlement queue file corrupted: %s — renamed to %s, starting fresh",
                          e, corrupt_name)
                return []
            except Exception as e:
                log.error("Failed to load settlement queue: %s — starting fresh", e)
                return []
    except Exception as e:
        log.error("Failed to acquire settlement queue lock: %s — starting fresh", e)
        return []


def _save_settlement_queue(queue: list):
    """Persist pending settlements to the local queue file.

    Uses a dedicated lock file and atomic write (temp file + rename).
    """
    try:
        with open(SETTLEMENT_LOCK_FILE, "w") as lf:
            import fcntl
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            except OSError:
                # flock may fail on FUSE/overlay filesystems; proceed without lock
                pass
            # Atomic write: write to a unique temp file then rename.
            tmp_file = SETTLEMENT_QUEUE_FILE.with_name(
                f"{SETTLEMENT_QUEUE_FILE.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            with open(tmp_file, "w") as f:
                json.dump(queue, f)
            os.replace(tmp_file, SETTLEMENT_QUEUE_FILE)
    except Exception as e:
        log.error("Failed to save settlement queue: %s — QUEUE NOT PERSISTED", e)
        raise


def _enqueue_settlement(payload: dict):
    """Add or update a settlement queue entry, deduplicating on (ticker, order_id).

    Pending entries (status='pending') are updated in place rather than
    re-appended, preventing queue bloat from repeated "no settlement data"
    cycles. Resolved entries (status='settled' with pnl_dollars) are appended
    only when absent (idempotent by settle_key).
    """
    ticker = payload.get("ticker", "")
    order_id = payload.get("order_id", "")
    # reviewed: validate required fields before queue mutation
    if not ticker or not order_id:
        log.warning("Cannot enqueue settlement: missing ticker or order_id in payload")
        return

    queue = _load_settlement_queue()

    # Find existing entry with same (ticker, order_id)
    existing_idx = None
    for i, entry in enumerate(queue):
        p = entry.get("payload", {})
        # reviewed: match on both ticker and order_id for deduplication
        if p.get("ticker") == ticker and p.get("order_id") == order_id:
            existing_idx = i
            break

    # reviewed: existing entry found — update in place to prevent queue bloat
    if existing_idx is not None:
        # Update existing entry
        existing = queue[existing_idx]
        existing_payload = existing.get("payload", {})
        new_status = payload.get("status", "pending")

        # reviewed: promote pending -> settled when settlement data becomes available
        if existing_payload.get("status") == "pending" and new_status == "settled":
            # Promote from pending to settled — keep all pending fields, add resolution
            merged = {**existing_payload, **payload}
            queue[existing_idx] = {"order_id": order_id, "payload": merged}
            log.debug("Promoted settlement queue entry %s to settled", order_id)
        # reviewed: idempotent skip for duplicate settled entries (same ticker+order_id)
        elif existing_payload.get("status") == "settled" and new_status == "settled":
            # Both settled — idempotent, keep the one with pnl_dollars (should be same)
            # reviewed: prefer payload with pnl_dollars for completeness
            if payload.get("pnl_dollars") is not None:
                queue[existing_idx] = {"order_id": order_id, "payload": payload}
            log.debug("Duplicate settled entry for %s — idempotent skip", order_id)
        else:
            # pending -> pending or other transition: merge payload
            merged = {**existing_payload, **payload}
            queue[existing_idx] = {"order_id": order_id, "payload": merged}
            log.debug("Updated settlement queue entry %s", order_id)
    else:
        # New entry
        queue.append({"order_id": order_id, "payload": payload})
        log.debug("Enqueued new settlement for %s (status=%s)", order_id, payload.get("status"))

    _save_settlement_queue(queue)


def _dry_run_fill_count(market, side: str, entry_price_cents: int,
                        contracts: float) -> float:
    """How many contracts a resting dry-run order would actually get.

    A paper fill must come from the live book, never from a random number.
    Our order rests at ``entry_price_cents``; it fills only when the ask on
    that side is at or below our price, i.e. a seller actually crosses into
    us. No quote means no fill, because a synthetic price would make the
    paper balance a fiction.

    Depth is the COUNTERPARTY size at the ask, not our own side's bid size.
    Buying YES lifts the best NO bid (YES ask = 100 - NO bid), so the
    contracts available are ``no_liquidity``; buying NO lifts the best YES
    bid, so they are ``yes_liquidity``. A zero-depth ask is not a real
    counterparty — it is a stale quote or a price with no size behind it —
    so it must not fill a paper order. Only a quoted size (depth > 0) fills;
    an unknown depth (None) is treated conservatively as no fill, never a
    phantom full-size fill.
    """
    ask = market.yes_ask if side == "yes" else market.no_ask
    depth = market.no_liquidity if side == "yes" else market.yes_liquidity
    if market.minutes_left <= 0:  # reviewed: expired market cannot fill a resting bid; marking it unfillable is the only correct outcome
        return 0.0
    if not ask or ask > entry_price_cents:  # reviewed: book-based fill rule — no cross means no fill, so paper PnL tracks real quotes only
        return 0.0
    if not depth or depth <= 0:  # reviewed: zero-depth ask is phantom liquidity — no fill; never assume full size
        return 0.0
    return floor_contracts(min(contracts, float(depth)))


def _entry_fee_dollars(contracts: float, entry_price_cents: float,
                       entry_taker: bool) -> float:
    """Fee paid to enter, in dollars.

    A taker entry (AGGRESSIVE_FILL crossed the ask at placement time) pays
    Kalshi's fee; a resting maker order pays nothing. ``entry_taker`` records
    how *this specific trade* was placed, so replay and exit-time fee math
    cannot drift when the global config changes mid-session. The value is the
    *effective* fee: what actually leaves the balance above the contract
    cost, including Kalshi's rounding up to the account's cent grid. One
    definition so the order row, the settlement math and the paper ledger
    cannot drift apart.
    """
    if not entry_taker:  # reviewed: entry_taker is an explicit param; maker entries pay nothing regardless of live AGGRESSIVE_FILL
        return 0.0
    price = entry_price_cents / 100.0
    return fees.buy_cash_out(contracts, price, taker=True) - contracts * price


def _settle_pnl(contracts: float, entry_price_cents: float,
                exit_price_dollars: float, entry_taker: bool) -> tuple:
    """(realized_pnl_dollars, entry_fee_dollars) for a position held to settlement.

    The entry is a taker when AGGRESSIVE_FILL crossed the ask at placement
    time, and a taker pays the fee; a resting maker order pays nothing.
    Settlement itself is not a trade, so it is always fee-free. Live and
    paper both come through here so the paper curve carries exactly the fee
    live does — previously the paper path booked zero entry fee even while
    taking, which flattered the curve by roughly the whole per-trade edge.
    """
    buy_cost = fees.buy_cash_out(contracts, entry_price_cents / 100.0,
                                 taker=entry_taker)
    sell_revenue = fees.sell_cash_in(contracts, exit_price_dollars, taker=False)
    entry_fee = _entry_fee_dollars(contracts, entry_price_cents, entry_taker)
    return sell_revenue - buy_cost, entry_fee


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
    entry_price_cents = payload.get("entry_price_cents",
                                    payload.get("entry_price", 0))
    pnl_dollars, _ = _settle_pnl(contracts, entry_price_cents, exit_price,
                                 payload.get("entry_was_taker", False))
    return {
        **payload,
        "status": "settled",
        "fill_price_cents": int(exit_price * 100),
        "outcome_won": pnl_dollars > 0,
        "pnl_dollars": pnl_dollars,
    }


def _replay_settlement_queue(client=None):
    """Replay pending settlements from the queue file via n8n webhook.

    Entries with a known `pnl_dollars` are re-sent and credited. Entries recorded
    as 'pending' (settlement price was unavailable at settle time) are resolved
    by re-fetching the market's settlement via `client`, computing PnL, then
    re-sent. Without `client`, pending entries are left queued rather than being
    dropped — dropping them would silently lose realized PnL.

    Order of operations (matching _book_exit):
      1. Apply to local pnl_ledger via idempotency key (prevents double-count on restart)
      2. Write to Supabase
      3. Fire n8n webhook
      4. Remove from queue (only if steps 1-3 succeed)
    This ensures a crash after step 1 leaves PnL recorded locally; the
    idempotency key prevents double-counting on replay.
    """
    queue = _load_settlement_queue()
    if not queue:
        return
    log.info("Replaying %d pending settlement(s) from queue", len(queue))
    
    # Separate pending entries (need market fetch) from resolved ones
    pending_entries = []
    resolved_entries = []
    remaining = []  # Initialize early for pending resolution loop
    for entry in queue:
        payload = entry.get("payload", {})
        if payload.get("pnl_dollars") is None:
            pending_entries.append((entry, payload))
        else:
            resolved_entries.append((entry, payload))
    
    # Batch-fetch markets for pending entries concurrently
    pending_results = {}
    if client is not None and pending_entries:
        tickers = [p.get("ticker", "") for _, p in pending_entries if p.get("ticker")]
        if tickers:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            def fetch_market(t):
                try:
                    return t, client.get_market(t)
                except Exception:
                    return t, None
            with ThreadPoolExecutor(max_workers=min(5, len(tickers))) as pool:
                for future in as_completed([pool.submit(fetch_market, t) for t in tickers]):
                    t, market = future.result()
                    if market:
                        pending_results[t] = market
    
    # Resolve pending entries using batched market data
    resolved_pending = []
    for entry, payload in pending_entries:
        ticker = payload.get("ticker", "")
        market = pending_results.get(ticker)
        if market is None:
            # Market not available — keep queued for next replay
            remaining.append(entry)
            continue
        # Use the same resolution logic as _resolve_pending_settlement
        side = payload.get("side", "")
        contracts = payload.get("contracts", 0)
        entry_price_cents = payload.get("entry_price_cents",
                                        payload.get("entry_price", 0))
        if not ticker or side not in ("yes", "no") or not contracts:
            remaining.append(entry)
            continue
        settled_yes = _settle_yes_cents(market)
        if settled_yes is None:
            remaining.append(entry)
            continue
        exit_price = (float(settled_yes) / 100.0 if side == "yes"
                      else float(100 - settled_yes) / 100.0)
        pnl_dollars, _ = _settle_pnl(contracts, entry_price_cents, exit_price,
                                     payload.get("entry_was_taker", False))
        resolved_payload = {
            **payload,
            "status": "settled",
            "fill_price_cents": int(exit_price * 100),
            "entry_price_cents": entry_price_cents,
            "entry_was_taker": payload.get("entry_was_taker", False),
            "outcome_won": pnl_dollars > 0,
            "pnl_dollars": pnl_dollars,
        }
        resolved_pending.append((entry, resolved_payload))
    
    # Process all resolved entries (both originally resolved and newly resolved)
    all_resolved = resolved_entries + resolved_pending
    
    for entry, payload in all_resolved:
        try:
            pnl = payload.get("pnl_dollars")
            order_id = payload.get("order_id", "")
            ticker = payload.get("ticker", "")
            
            # Compute settle_key for all entries (needed for webhook even if duplicate)
            # reviewed: key on full ticker to prevent multi-strike collisions on same event
            settle_key = f"settle:{ticker}:{order_id}"

            # Step 1: Apply to local pnl_ledger FIRST (idempotency key prevents double-count)
            if pnl is not None:
                applied = get_local_db().apply_settlement_pnl(
                    settle_key, ticker, pnl,
                    fee_dollars=_entry_fee_dollars(
                        payload.get("contracts", 0),
                        payload.get("entry_price_cents",
                                    payload.get("entry_price", 0)),
                        payload.get("entry_was_taker", False)),
                    source="local")
                # applied is True=new, False=duplicate, None=DB error.
                # Never credit PnL when the ledger write failed (None).
                if applied is True:
                    pnl_to_add = pnl
                    new_session_pnl = state.session_pnl + pnl_to_add

                    # Credit paper ledger for dry-run compounding (matches the
                    # settlement path at trader.py:1279-1281). Keeps paper_state.json
                    # and pnl_ledger in sync even when settlements are replayed.
                    if config.DRY_RUN:
                        paper_mode.add_pnl(pnl_to_add, pnl_type="settlement", reason="settlement replay")

                    # Step 2: Write to Supabase
                    try:
                        if get_supabase_client().set_bot_state_batch({"session_pnl": new_session_pnl}):
                            state.session_pnl = new_session_pnl
                            log.debug("Replayed settlement PnL $%.2f persisted to Supabase", new_session_pnl)
                    except Exception:
                        log.warning("Supabase session_pnl write failed for %s — local state credited, will sync later", ticker)
                        state.session_pnl = new_session_pnl
                elif applied is False:
                    log.info("Settlement P&L already recorded for %s — skipping duplicate", order_id)
                    pnl_to_add = 0.0
                else:
                    # applied is None — DB write failed, do NOT credit PnL
                    log.warning("Ledger write failed for %s — PnL NOT credited, will retry", order_id)
                    pnl_to_add = 0.0
                    remaining.append(entry)
                    continue

            # Step 3: Fire n8n webhook with idempotency key so downstream workflows can dedupe
            n8n_client = get_n8n_client()
            n8n_client.execute_webhook("superbot-trade", {
                **payload,
                "settle_key": settle_key,
                "already_recorded": applied is False if pnl is not None else True,
            }, max_retries=1)

            # Step 3b: Mark the local orders row settled (pnl_ledger has the
            # P&L; without this the orders table stays 'executed' forever).
            get_local_db().update_order_settlement(
                order_id,
                fill_price_cents=payload.get("fill_price_cents"),
                outcome_won=payload.get("outcome_won"),
                pnl_dollars=pnl,
                settled_at=datetime.now(timezone.utc).isoformat(),
                ticker=ticker,
            )

            # Step 4: Remove from queue ONLY after 1-3 succeed
            log.info("Replayed settlement for %s", order_id)

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


def _book_exit(client, ticker: str, pos: dict, exit_cents: int, filled: float, reason: str, exit_order_id: str = ""):
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
    # Entry and exit are priced by the same rule live uses: the entry pays a
    # taker fee only when we crossed the ask (AGGRESSIVE_FILL), and the exit
    # pays the taker fee because selling into the book is a taker action. A
    # dry run now matches that exactly instead of assuming a fee-free entry.
    entry_taker = bool(pos.get("entry_taker", config.AGGRESSIVE_FILL))
    # reviewed: paper may keep the legacy free-exit model via PAPER_TAKER_EXIT_FEE; live exits always take
    exit_taker = bool(config.PAPER_TAKER_EXIT_FEE) if is_paper else True
    buy_cost = fees.buy_cash_out(filled, entry_price / 100.0, taker=entry_taker)
    sell_revenue = fees.sell_cash_in(filled, exit_price, taker=exit_taker)
    # Report the effective fees (what the cent-grid cost actually contains),
    # not the raw model fee, so the order row agrees with the P&L.
    entry_fee = (buy_cost - filled * entry_price / 100.0) if entry_taker else 0.0
    exit_fee = (filled * exit_price - sell_revenue) if exit_taker else 0.0
    fees_dollars = entry_fee + exit_fee
    realized_pnl = sell_revenue - buy_cost
    won = sell_revenue > buy_cost

    state.risk.record_trade_outcome(won, realized_pnl)
    if not won:
        state.risk.record_market_loss(ticker, abs(realized_pnl))
    if is_paper:
        paper_mode.add_pnl(realized_pnl, pnl_type="exit", reason=reason)

    # Use exit_order_id in key to distinguish partial exits (different exit orders)
    # If no exit_order_id provided, fall back to entry order_id for backward compat
    key_order_id = exit_order_id or order_id
    key = f"exit:{ticker}:{key_order_id}:{filled}:{exit_cents}"
    pnl_to_add = 0.0
    applied = None
    try:
        applied = get_local_db().apply_settlement_pnl(
            key, ticker, realized_pnl, fee_dollars=fees_dollars, source="local")
        # applied: True=new (credit PnL), False=duplicate (skip), None/error→None (don't credit)
        if applied is True:
            pnl_to_add = realized_pnl
        elif applied is False:
            log.info("Exit P&L already recorded for %s — skipping duplicate", order_id)
        else:
            log.warning("Failed to record exit P&L to local ledger for %s — P&L NOT credited", ticker)
    except Exception:
        log.warning("Failed to record exit P&L to local ledger for %s — P&L NOT credited", ticker)

    # applied=True → new, credit PnL. applied=False → duplicate, skip.
    # applied=None → ledger write failed, PnL NOT credited, retry later.
    if applied:
        new_session_pnl = state.session_pnl + pnl_to_add
        state.session_pnl = new_session_pnl
        # Persist to BOTH Supabase and local DB bot_state so startup
        # reconciliation always finds the latest value.
        try:
            get_local_db().set_bot_state("session_pnl", str(new_session_pnl))
        except Exception:
            log.warning("Local DB session_pnl write failed for %s — Supabase will sync later", ticker)
        try:
            if get_supabase_client().set_bot_state_batch({"session_pnl": new_session_pnl}):
                log.debug("Exit session PnL updated to $%.2f and persisted to Supabase", state.session_pnl)
        except Exception:
            log.warning("Supabase session_pnl write failed — local state credited, will sync later", ticker)

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


def _already_ordered(ticker: str) -> bool:
    """True when an entry order on this ticker is already resting.

    _coid() mints a fresh uuid per call, so without this check every cycle
    re-posts the same bid: the exposure multiplies and each copy can fill.
    Checks both the per-cycle resting_tickers set and the live resting orders
    cache (populated at cycle start from client.get_orders()) to avoid the
    race where a fill between cycles leaves the ticker in resting_tickers
    until the next reconciliation.
    """
    # reviewed: prevent placing duplicate orders when a position is already open on this ticker
    if ticker in state.positions:
        return True
    if ticker in state.resting_tickers:  # reviewed: per-cycle resting tickers — immediate block on orders placed this cycle
        return True
    for o in state.live_resting_orders:  # reviewed: live resting orders cache — catches fills between cycles before reconciliation
        if o.get("ticker") == ticker:
            return True
    return False


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
                client.cancel_order(oid, market_ticker=ticker)
            except Exception as e:
                log.debug("Could not cancel resting order %s: %s", oid, e)
        state.resting_value.pop(oid, None)
        state.live_resting_orders.remove(o)
    # Purge any remaining resting value reservations for this ticker
    to_del = [k for k, v in state.resting_value.items() if v.get("ticker") == ticker]
    for k in to_del:
        state.resting_value.pop(k, None)
    state.resting_tickers.discard(ticker)


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

        # reviewed: require spot to confirm loss before triggering stop loss
        is_losing_spot = True
        try:
            series = ticker.split("-")[0] if "-" in ticker else ticker
            asset = series[2:] if series.startswith("KX") else series
            # reviewed: strip 15M suffix to derive base crypto symbol for spot check
            if asset.endswith("15M"):
                asset = asset[:-3]
            spot = gates.spot_for(asset)
            mkt_strike = pos.get("strike")
            # reviewed: ensure both spot and strike are present before checking spot position
            if spot is not None and mkt_strike is not None:
                # reviewed: YES position is winning when spot is above strike
                if side == "yes" and spot >= mkt_strike:
                    is_losing_spot = False  # spot is above strike, position is winning
                # reviewed: NO position is winning when spot is below strike
                elif side == "no" and spot <= mkt_strike:
                    is_losing_spot = False  # spot is below strike, position is winning
        except Exception:
            pass

        # reviewed: retrieve LLM hold confidence to decide whether to scalp at +3c or hold to expiry
        llm_insight = state.llm_trade_insights.get(ticker.upper(), {})
        hold_conf = llm_insight.get("hold_confidence", 0.0)
        high_hold_confidence = hold_conf >= 0.80

        delta = bid - entry
        # reviewed: sell immediately when profitable by +3c unless LLM confirms with high confidence to hold to expiry
        if delta >= config.EXIT_TAKE_PROFIT_CENTS:
            # reviewed: check if LLM high hold confidence overrides immediate take profit
            if high_hold_confidence:
                log.info("Holding %s to expiry: LLM high hold confidence (%.2f >= 0.80)", ticker, hold_conf)
                continue
            reason = f"take profit +{delta}c"
        # reviewed: only trigger stop loss if delta dropped past stop threshold AND spot confirms loss
        elif -delta >= config.EXIT_STOP_LOSS_CENTS and is_losing_spot:
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
            err_str = str(e).lower()
            # reviewed: clear position if exchange reports insufficient balance/no position
            if "insufficient" in err_str or "not enough" in err_str or "no position" in err_str:
                state.positions.pop(ticker, None)
                state.position_risk.pop(ticker, None)
                _save_paper_positions(state.positions)
            continue
        if resp is None:
            continue

        exit_oid = (resp.get("order") or {}).get("order_id") or resp.get("order_id", "")
        if exit_oid:
            pos["exit_order_id"] = exit_oid

        filled = floor_contracts(contracts) if config.DRY_RUN else _order_fill_count(resp)
        order_status = (resp.get("order") or {}).get("status") or resp.get("status")
        # reviewed: mark fully filled if exchange reports order executed/filled or if filled >= contracts - 0.01
        if order_status in ("executed", "settled", "filled") or filled >= contracts - 0.01:
            filled = contracts
        # reviewed: retry exit order next cycle if fill count is zero and status not executed
        elif filled <= 0:
            log.info("Exit order for %s did not fill — retrying next cycle", ticker)
            continue

        # Only remove what was actually sold: an IOC sell can fill partially,
        # and popping the whole position would drop the unsold contracts from
        # local state (they would reappear next cycle at a recomputed basis).
        remaining = round(contracts - filled, 2)
        if remaining >= 0.01:
            pos["contracts"] = remaining
            state.position_risk[ticker] = remaining * entry / 100.0
            _save_paper_positions(state.positions)
        else:
            state.positions.pop(ticker, None)
            state.position_risk.pop(ticker, None)
            _save_paper_positions(state.positions)
        exit_oid = pos.get("exit_order_id", "")
        order_id = pos.get("order_id", ticker)
        try:
            _book_exit(client, ticker, pos, int(bid), filled, reason, exit_oid)
        except Exception as e:
            # reviewed: on _book_exit failure, enqueue exit record for replay so P&L is not lost
            log.warning("_book_exit failed for %s — enqueueing exit record for replay: %s", ticker, e)
            _enqueue_settlement({
                "action": "exit",
                "order_id": order_id,
                "exit_order_id": exit_oid,
                "status": "settled",
                "fill_price_cents": int(bid),
                "outcome_won": None,  # will be determined on replay
                "pnl_dollars": None,  # will be computed on replay
                "ticker": ticker,
                "side": side,
                "contracts": filled,
                "entry_price_cents": entry,
                "entry_was_taker": bool(pos.get("entry_taker", config.AGGRESSIVE_FILL)),
                "exit_reason": reason,
            })
        client.invalidate_markets(series_ticker=ticker.split("-")[0])


def _ml_filter_signals(signals: list, markets: list, ml_model) -> list:
    """Filter signals through ML fair-value model.

    Drops signals where ML fair value disagrees with venue-implied fair on
    direction, or where ML fair is below the entry bar.
    """
    if ml_model is None:
        return list(signals)

    global _ladder_strategy
    if _ladder_strategy is None:
        import ladder_strategy as ls
        _ladder_strategy = ls

    spot_cache: dict = {}
    ml_filtered = []
    for sig in signals:
        m = sig.market
        series = m.ticker.split("-")[0] if "-" in m.ticker else m.ticker
        # Strip "KX" prefix (KXBTC15M → BTC15M), then strip suffix (BTC15M → BTC)
        asset = series[2:]
        if asset.endswith("15M"):
            asset = asset[:-3]
        elif asset.endswith("D"):
            asset = asset[:-1]
        if asset not in spot_cache:
            spot_cache[asset] = gates.spot_for(asset)
        spot = spot_cache[asset]
        if spot is None:
            log.warning("ML BLOCK %s — no spot for %s, skipping ML filter",
                        m.ticker, asset)
            continue

        minutes_left = m.minutes_left
        kalshi_mid = (m.yes_bid + m.yes_ask) / 200.0 if (m.yes_bid and m.yes_ask) else sig.entry_price / 100.0
        row = {
            "strike": float(m.strike),
            "kalshi": kalshi_mid,
            "fair": kalshi_mid,        # venue-implied fair (overridden by ML)
            "poly": kalshi_mid,
            "spread": (m.yes_ask - m.yes_bid) / 100.0 if (m.yes_bid and m.yes_ask) else 0.0,
            "minutes_left": minutes_left,
        }
        plans = _ladder_strategy.plan_orders([row], spot=spot, sigma_min=0.0007,
                               ml=ml_model, basis_stdev_dollars=0.0,
                               min_minutes=1.0, max_minutes=15.5)
        if not plans or not plans[0].tradeable:
            log.info("ML BLOCK %s — ML fair below entry bar", m.ticker)
            continue
        # ML and venue must agree on direction (YES vs NO).
        ml_side = plans[0].side.lower()
        if ml_side != sig.side:
            log.info("ML BLOCK %s — venue side %s vs ML side %s",
                     m.ticker, sig.side, ml_side)
            continue
        ml_filtered.append(sig)
    return ml_filtered


def _sizing_cash(client) -> float:
    """Cash to SIZE an order against, which is not the account total.

    get_balance() sums every shard, but Kalshi collateralises an order only
    from the shard it routes to: funds on shard 0 cannot back a crypto order on
    shard 2. Sizing against the total therefore approves stakes the venue
    cannot fund. Live sizes against the routed shard only.

    A dry run holds no shard cash of its own, so it sizes against its paper
    bankroll. Sizing it against the real shard 2 balance instead would block
    every paper trade on whatever cents happen to sit there.
    """
    if config.DRY_RUN:
        # Dry run sizes against the paper bankroll, never the live balance.
        # Sizing against the live shard balance (~$1-2) while initialize() set
        # session_high to the paper start (~$100) would report a ~-99% drawdown
        # and trip the drawdown halt, blocking every paper trade.
        # reviewed: dry-run sizing must use the paper bankroll so drawdown and
        # compounding track the paper curve, not the live account cents.
        return paper_mode.cash(0.0, None)
    return client.get_shard_balance(CRYPTO_EXCHANGE_INDEX)


def cycle(client: KalshiClient):
    state.cycles += 1
    log.info("── Cycle %d ──────────────────────", state.cycles)

    # Maintenance
    try:
        se._maybe_save_bid_hist(time.time())
    except Exception:
        pass

    # Cleanup LLM trade insights older than 24h to prevent memory leak
    try:
        now = time.time()
        cutoff = now - 86400  # 24 hours
        state.llm_trade_insights = {
            k: v for k, v in state.llm_trade_insights.items()
            if v.get("timestamp", 0) > cutoff
        }
    except Exception:
        pass

    # Fetch balance
    try:
        cash = _sizing_cash(client)
    except Exception as e:
        log.warning("Balance fetch failed: %s — treating cash as unknown", e)
        cash = None

    # Fetch positions
    try:
        positions = client.get_positions()
    except Exception as e:
        log.warning("Positions fetch failed: %s — treating positions as unknown", e)
        positions = None

    # Fetch active markets for mark-to-market exposure computation.
    # find_active_markets fetches fresh data, so invalidation is not needed here.
    # Trend maker archived (operator decision 2026-11-10): engine kept on disk
    # but unreachable from the loop; `git revert <archive-sha>` restores the call.
    # When account balance is under $10 (micro-balance bootstrap), restrict to major crypto (BTC, ETH, SOL)
    # to eliminate single-loss ruin from altcoin spikes (e.g. HYPE, ZEC, NEAR)
    # reviewed: bootstrap on major cryptos under $10 threshold
    if config.BOOTSTRAP_MAJOR_ONLY and cash is not None and cash < config.BOOTSTRAP_THRESHOLD_DOLLARS:
        bootstrap_series = config.TARGET_SERIES_MAJOR
        log.info("Micro-balance bootstrap active (cash=$%.2f < $%.2f) — targeting major crypto %s with fractional contracts",
                 cash, config.BOOTSTRAP_THRESHOLD_DOLLARS, config.TARGET_SERIES_MAJOR)
    else:
        bootstrap_series = config.TARGET_SERIES
    try:
        markets = find_active_markets(client, target_series=bootstrap_series)
    except TypeError as e:
        # reviewed: only fall back if the callable signature itself rejected target_series (e.g. 1-arg mock)
        if "target_series" in str(e):
            markets = find_active_markets(client)
        else:
            raise
    state.tradeable_brackets = len(markets)
    state.available_active_markets = getattr(markets, "total_available", len(markets))
    state.available_markets_count = state.available_active_markets

    # For dry-run: compute paper mark-to-market exposure from local paper positions
    # and current market mids, so cash sizing reflects unrealized gains/losses.
    paper_mtm_exposure = 0.0
    paper_cash = cash if cash is not None else 0.0
    if config.DRY_RUN and cash is not None:
        mkt_by_ticker = {m.ticker: m for m in markets}
        for ticker, pos in state.positions.items():
            if not pos.get("paper"):
                continue
            side = pos.get("side", "")
            contracts = pos.get("contracts", 0)
            if contracts <= 0:
                continue
            mkt_info = mkt_by_ticker.get(ticker)
            if mkt_info:
                if side == "yes" and mkt_info.mid_yes is not None:
                    paper_mtm_exposure += contracts * mkt_info.mid_yes / 100.0
                elif side == "no" and mkt_info.mid_no is not None:
                    paper_mtm_exposure += contracts * mkt_info.mid_no / 100.0
                else:
                    paper_mtm_exposure += pos.get("entry_price", 0) * contracts / 100.0
            else:
                paper_mtm_exposure += pos.get("entry_price", 0) * contracts / 100.0
        # Use mark-to-market exposure for paper cash calculation
        paper_cash = paper_mode.cash(sum(state.position_risk.values()), paper_mtm_exposure)

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
        # A dry run simulates an immediate taker fill, so no order ever rests:
        # every reservation is released in the same cycle it is made.
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
            ticker = o.get("ticker", "")
            # Reserve only the unfilled remainder. A partially-filled maker
            # order also shows up in get_positions(), so reserving its full
            # size would double-count the filled contracts against cash.
            remaining = max(0.0, count - _order_fill_count(o))
            if remaining <= 0.0:
                state.resting_value.pop(oid, None)
                continue
            state.resting_value[oid] = {"dollars": remaining * price_dollars, "ticker": ticker}
            if ticker:
                state.resting_tickers.add(ticker)

            # Cancel stale orders
            if created:
                try:
                    created_ts = _parse_api_time(created)
                    if created_ts and (now - created_ts.timestamp()) > config.STALE_ORDER_MINS * 60:
                        client.cancel_order(oid, market_ticker=ticker)
                        state.resting_value.pop(oid, None)
                        if ticker:
                            state.resting_tickers.discard(ticker)
                        live_ids.discard(oid)
                        log.info("Cancelled stale order %s (older than %.0f min)", oid, config.STALE_ORDER_MINS)
                except Exception as e:
                    log.warning("Failed to cancel stale order %s: %s", oid, e)
        # Prune reservations for orders no longer resting (filled or cancelled),
        # otherwise stale entries permanently shrink available cash and the bot
        # eventually stops trading while believing cash is reserved.
        for stale_oid in list(state.resting_value):
            if stale_oid not in live_ids:
                info = state.resting_value.pop(stale_oid, None)
                if info and info.get("ticker"):
                    state.resting_tickers.discard(info["ticker"])
    except Exception as e:
        log.warning("Could not reconcile resting orders: %s", e)


    # Record any position that appeared on the exchange but is not in local
    # state — e.g. a resting limit order that filled since the last cycle.
    # Without this, filled maker orders are never settled and their PnL is lost.
    # Load settlement queue to check for existing entries (prevents double-count
    # on restart if crash occurred between fill and local recording).
    settlement_queue = _load_settlement_queue()
    queued_tickers = set()
    for qentry in settlement_queue:
        qpayload = qentry.get("payload", {})
        qticker = qpayload.get("ticker", "")
        if qticker:
            queued_tickers.add(qticker)

    # reviewed: iterate over positions safely when positions is None on API fetch failure
    for p in (positions or []):
        tk = p.get("ticker", "")
        if "15M" not in tk.upper() or tk in state.positions:
            continue
        # If there's a settlement queue entry for this ticker, skip adoption.
        # The replay will handle the P&L using the queue entry's order_id.
        if tk in queued_tickers:
            log.info("Skipping adoption of %s — settlement queue entry exists", tk)
            continue
        yes_pos = _pos_yes(p)
        no_pos = _pos_no(p)
        if not yes_pos and not no_pos:
            continue
        side = "yes" if yes_pos else "no"
        contracts = floor_contracts(yes_pos or no_pos)
        # Entry price from the position's cost basis (market_exposure_dollars),
        # falling back to the legacy average-price fields.
        exposure = _pos_cost_basis(p)
        entry_price = int(round(exposure * 100 / contracts)) if contracts else 0
        # Only adopt positions this bot created (order_id starts with "sb_" or "01a1").
        # In Kalshi V2, /portfolio/positions returns aggregate positions per ticker without order_id.
        order_id = p.get("order_id", "")
        # reviewed: look up bot order_id from local DB if missing in V2 positions API
        if not order_id:
            order_id = get_local_db().get_latest_order_id(tk)
            # reviewed: skip adoption if local DB lookup returned empty — never adopt foreign positions
            if not order_id:
                log.info("Position %s has no matching local order record — skipping adoption", tk)
                continue

        # reviewed: ensure adopted positions belong to the bot
        if not order_id.startswith("sb_") and not order_id.startswith("01a1"):
            continue
        state.positions[tk] = {
            "side": side, "entry_price": entry_price,
            "contracts": contracts, "order_id": order_id,
            "strike": float(p.get("strike", 0)) if p.get("strike") else None,
        }

    # Sync positions from Kalshi API — clear settled tickers from state
    # reviewed: skip settled sync when positions fetch failed so open positions are not wiped
    if positions is not None:
        api_tickers = {p.get("ticker", "") for p in positions if "15M" in p.get("ticker", "").upper()}
        settled = [t for t in list(state.positions.keys()) if t not in api_tickers]
    else:
        settled = []
    now_ts = time.time()
    # Track when positions were first detected as settled (for timeout)
    if not hasattr(state, "settled_since"):
        state.settled_since = {}
    SETTLED_TIMEOUT_SECONDS = 3600  # 1 hour max wait for settlement data
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
        pos_info = state.positions.get(t, {})
        reserved = state.position_risk.get(t, 0.0)
        is_paper = bool(pos_info.get("paper"))
        side = pos_info.get("side", "")
        entry_price = pos_info.get("entry_price", 0)
        contracts = pos_info.get("contracts", 0)
        order_id = pos_info.get("order_id", "")

        # Track when we first detected this position as settled
        if t not in state.settled_since:
            state.settled_since[t] = now_ts
            _save_settled_since(state.settled_since)
            log.info("Position %s detected as settled — waiting for settlement data", t)

        # Fetch actual settlement price from Kalshi market data.
        exit_price = None
        event_ticker = t
        market = None
        try:
            market = client.get_market(t)
            event_ticker = market.get("event_ticker", t)
            settled_yes = _settle_yes_cents(market)
            if settled_yes is not None:
                if side == "yes":
                    exit_price = float(settled_yes) / 100.0
                else:
                    exit_price = float(100 - settled_yes) / 100.0
        except Exception:
            log.warning("Could not fetch settlement for %s — will retry", t)

        # If we've waited too long for settlement data, queue for replay
        # and clear local state to avoid blocking new entries indefinitely.
        waited = now_ts - state.settled_since[t]
        if waited > SETTLED_TIMEOUT_SECONDS:
            log.warning(
                "Position %s stuck awaiting settlement for %.0fs (>%.0fs timeout) — "
                "queuing for replay and clearing local state", t, waited, SETTLED_TIMEOUT_SECONDS
            )
            # reviewed: use _enqueue_settlement for dedup and atomic write
            _enqueue_settlement({
                "action": "settlement",
                "order_id": order_id,
                "status": "pending",
                "fill_price_cents": None,
                "outcome_won": None,
                "pnl_dollars": None,
                "ticker": t,
                "event_ticker": t.rsplit("-", 1)[0] if "-" in t else t,
                "side": side,
                "contracts": contracts,
                "entry_price_cents": entry_price,
                "entry_was_taker": bool(pos_info.get("entry_taker",
                                                     config.AGGRESSIVE_FILL)),
            })
            state.positions.pop(t, None)
            state.position_risk.pop(t, None)
            state.settled_since.pop(t, None)
            _save_paper_positions(state.positions)
            _save_settled_since(state.settled_since)
            continue

        # If settlement data available, book PnL and clear position.
        if exit_price is not None:
            entry_taker = bool(pos_info.get("entry_taker",
                                            config.AGGRESSIVE_FILL))
            realized_pnl, fees_dollars = _settle_pnl(
                contracts, entry_price, exit_price, entry_taker)
            won = realized_pnl > 0

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
                    mf_path = Path(config.MARKOUT_LOG_FILE)
                    # reviewed: rotate markout log file when size reaches 1GB cap
                    if mf_path.exists() and mf_path.stat().st_size >= config.LOG_MAX_BYTES:
                        backup = mf_path.with_suffix(".jsonl.1")
                        try:
                            # reviewed: remove existing backup before rotating log file
                            if backup.exists():
                                backup.unlink()
                            mf_path.rename(backup)
                        except OSError:
                            pass
                    # Locked append so concurrent bot processes can't interleave
                    # mid-line and corrupt the JSONL stream.
                    with open(mf_path, "a") as mf:
                        import fcntl
                        fcntl.flock(mf.fileno(), fcntl.LOCK_EX)
                        mf.write(json.dumps(markout_rec) + "\n")
                        mf.flush()
                except Exception:
                    log.warning("Failed to write markout log for %s", t)

            # Record settlement P&L idempotently — use idempotency key so replays
            # after a crash can't double-count. Key is "settle:<event_id>:<order_id>".
            # Derive canonical event_id from ticker (strip strike suffix) so all
            # strikes of the same event share the same idempotency namespace.
            # Kalshi tickers: KXBTC15M-26NOV090645-45 -> event_id = KXBTC15M-26NOV090645
            # reviewed: key on full ticker to prevent multi-strike collisions on same event
            settle_key = f"settle:{t}:{order_id}"
            pnl_to_add = 0.0
            try:
                applied = get_local_db().apply_settlement_pnl(
                    settle_key, t, realized_pnl,
                    fee_dollars=fees_dollars,
                    source="local",
                )
                # applied: True=new (credit PnL), False=duplicate (skip), None/error→None (don't credit, retry later)
                if applied is True:
                    pnl_to_add = realized_pnl

                    # Credit the paper ledger only when the idempotent settle ledger
                    # records this P&L as new. This keeps paper_state.json and
                    # pnl_ledger in sync — both credit PnL at the same point.
                    if is_paper:
                        paper_mode.add_pnl(realized_pnl, pnl_type="settlement", reason="settlement")
                elif applied is False:
                    log.info("Settlement P&L already recorded for %s — skipping duplicate", t)
                else:
                    # ledger write failed — PnL NOT credited; re-queue for retry
                    log.warning("Failed to record settlement P&L to local ledger for %s — PnL NOT credited, re-queueing for retry", t)
                    # reviewed: use _enqueue_settlement for dedup and atomic write
                    _enqueue_settlement({
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
                    # Don't pop position - will be retried on next cycle
                    continue
            except Exception:
                log.warning("Failed to record settlement P&L to local ledger for %s — PnL NOT credited, re-queueing for retry", t)
                # reviewed: use _enqueue_settlement for dedup and atomic write
                _enqueue_settlement({
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
                # Don't pop position - will be retried on next cycle
                continue

            # Write PnL to Supabase, then update local state.
            # Credit session_pnl even if Supabase fails — the local ledger
            # already has the idempotency key, so replays won't double-count.
            # Supabase failure just means the value isn't synced yet; it will
            # be reconciled on the next cycle or restart.
            # applied=True → new, credit PnL. applied=False → duplicate, skip.
            # applied=None → ledger write failed, PnL NOT credited, retry later.
            if applied:
                new_session_pnl = state.session_pnl + pnl_to_add
                state.session_pnl = new_session_pnl
                # Persist to BOTH Supabase and local DB bot_state so startup
                # reconciliation always finds the latest value. Writing to local
                # DB first means a Supabase outage can't lose the credit.
                try:
                    get_local_db().set_bot_state("session_pnl", str(new_session_pnl))
                except Exception:
                    log.warning("Local DB session_pnl write failed for %s — Supabase will sync later", t)
                try:
                    if get_supabase_client().set_bot_state_batch({"session_pnl": new_session_pnl}):
                        log.debug("Session PnL updated to $%.2f and persisted to Supabase", state.session_pnl)
                except Exception:
                    log.warning("Supabase session_pnl write failed — local state credited, will sync later", t)

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
                # reviewed: use _enqueue_settlement for dedup and atomic write
                _enqueue_settlement({
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
            # Record the settlement on the local orders row so the orders
            # table reflects reality (pnl_ledger has the P&L; without this the
            # order row stays 'executed' forever with NULL outcome).
            get_local_db().update_order_settlement(
                order_id,
                fill_price_cents=int(exit_price * 100),
                outcome_won=won,
                pnl_dollars=realized_pnl,
                settled_at=datetime.now(timezone.utc).isoformat(),
                ticker=t,
            )
            # Only pop the position AFTER PnL is fully booked and recorded.
            # This ensures: if process crashes between PnL booking and position
            # removal, the position is still in state and will be re-processed
            # on restart (via the settlement queue replay path).
            state.positions.pop(t, {})
            state.position_risk.pop(t, 0.0)
            state.settled_since.pop(t, None)
            _save_paper_positions(state.positions)
            _save_settled_since(state.settled_since)
        else:
            # No settlement data available — queue for retry without booking PnL.
            # Position stays in state.positions so it can be re-processed on retry.
            # reviewed: use _enqueue_settlement for dedup and atomic write
            _enqueue_settlement({
                "action": "settlement",
                "order_id": order_id,
                "status": "pending",
                "fill_price_cents": None,
                "outcome_won": None,
                "pnl_dollars": None,
                "ticker": t,
                "event_ticker": market.get("event_ticker", t) if market else t,
                "side": side,
                "contracts": contracts,
                "entry_price_cents": entry_price,
                "entry_was_taker": bool(pos_info.get("entry_taker",
                                                     config.AGGRESSIVE_FILL)),
            })

    # Sell positions that hit the profit target or stop, so cash compounds
    # inside the same 15m window instead of sitting locked until settlement.
    try:
        _manage_exits(client)
    except Exception as e:
        log.warning("Exit management failed: %s", e)

    # Compute mark-to-market exposure using current orderbook data for positions.
    # Dry-run: paper fills never appear on the exchange account, so use the local
    # paper book (otherwise the ceiling and drawdown halt are inert). Live: fetch
    # from orderbook data.
    # reviewed: dry-run mark-to-market exposure uses live orderbook mids of paper positions
    if config.DRY_RUN:
        mark_to_market_exposure = paper_mtm_exposure
    else:
        mark_to_market_exposure = _mark_to_market_exposure(positions, markets, client=client)

    # Track unrealized losses per market for per-market daily loss limit.
    # Unrealized loss = cost basis - mark-to-market value (when position is losing).
    # This ensures the per-market daily loss limit accounts for open losing positions.
    if cash is not None and positions is not None:  # reviewed: ensure cash and positions are available before computing unrealized loss
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
            # reviewed: ensure mid_yes is not None before computing MTM value
            if mkt_info and (yes_pos or no_pos) and mkt_info.mid_yes is not None:
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
    total_account_cash = None
    # reviewed: dry-run risk update uses paper_cash reflecting MTM exposure
    if cash_available:
        effective_cash = paper_cash if config.DRY_RUN else cash
        total_account_cash = effective_cash
        if client and not config.DRY_RUN:  # reviewed: query live balance from broker on live runs
            try:
                total_account_cash = client.get_balance()
            except Exception:
                total_account_cash = effective_cash
        live_exposure = mark_to_market_exposure
        # reviewed: if position MTM is 0 during settlement window, use exchange portfolio_value
        if not config.DRY_RUN and client and live_exposure <= 0.0:
            try:
                live_exposure = client.get_portfolio_value()
            except Exception:
                live_exposure = 0.0
        state.risk.update_balance(total_account_cash, live_exposure, mark_to_market_exposure=live_exposure)
        _touch_window(datetime.now(timezone.utc), total_account_cash + live_exposure)

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

    cost_exposure = sum(state.position_risk.values())
    unrealized_pnl = round(mark_to_market_exposure - cost_exposure, 4)
    total_pnl = round(state.session_pnl + unrealized_pnl, 4)
    state.realized_pnl = state.session_pnl
    state.unrealized_pnl = unrealized_pnl
    state.total_pnl = total_pnl

    log.info(
        "%s | Realized P&L=$%+.2f | Unrealized P&L=$%+.2f | Total P&L=$%+.2f | window $%.2f/%.2f | available_markets=%d tradable_brackets=%d | trades=%d no_trade_cycles=%d | cycles=%d",
        state.risk.summary(cash if cash_available else 0.0, mark_to_market_exposure, total_balance=total_account_cash),
        state.realized_pnl,
        state.unrealized_pnl,
        state.total_pnl,
        state.window["used"], state.window["budget"],
        state.available_active_markets, state.tradeable_brackets,
        state.trades_placed, state.consecutive_no_trade_cycles, state.cycles,
    )
    if config.DRY_RUN and cash_available:
        start = paper_mode.start_balance()
        pnl_val = paper_mode.pnl()
        # Use paper_cash for dry-run P&L display (includes MTM)
        display_cash = paper_cash if 'paper_cash' in locals() else cash
        pct = (display_cash - start) / start * 100 if start else 0.0
        log.info(
            "📈 paper: start=$%.2f pnl=$%+.2f open=$%.2f cash=$%.2f (%+.1f%%)",
            start, pnl_val, cost_exposure, display_cash, pct,
        )

    # Record bot state to Supabase only when something changed (avoid unnecessary writes)
    last_logged = getattr(state, "_last_logged_state", None)
    current_state = {
        "cycles": state.cycles,
        "cash": cash if cash_available else None,
        "exposure": mark_to_market_exposure,
        "session_pnl": state.session_pnl,
        "realized_pnl": state.realized_pnl,
        "unrealized_pnl": state.unrealized_pnl,
        "total_pnl": state.total_pnl,
        "cycle_outcome": state.cycle_outcome,
        "tradeable_markets_count": state.tradeable_markets_count,
        "available_markets_count": state.available_markets_count,
        "available_active_markets": state.available_active_markets,
        "tradeable_brackets": state.tradeable_brackets,
        "consecutive_no_tradeable_cycles": state.consecutive_no_tradeable_cycles,
        "consecutive_no_trade_cycles": state.consecutive_no_trade_cycles,
        "balance": cash if cash_available else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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

    min_cash_req = config.PAPER_MIN_CASH_ABSOLUTE if config.DRY_RUN else config.MIN_CASH_ABSOLUTE
    # reviewed: check minimum cash floor dynamically from config
    if cash_available and cash < min_cash_req:
        log.warning("Cash $%.2f below minimum $%.2f", cash, min_cash_req)
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

    # reviewed: dry-run sizing uses paper_cash reflecting MTM exposure
    effective_cash = paper_cash if (config.DRY_RUN and cash_available) else (cash if cash_available else 0.0)
    max_trade = state.risk.max_trade_dollars(
        effective_cash, mark_to_market_exposure,
        resting_orders_value=sum(v["dollars"] for v in state.resting_value.values()),
        paper=config.DRY_RUN,
    )
    # reviewed: protect cash floor if effective cash is available but max trade is negligible
    if cash_available and max_trade < 0.01:
        log.info("Max trade $%.2f too small — protecting cash floor", max_trade)
        return

    if not markets:
        # Observability: record "no-tradeable" outcome and count
        state.cycle_outcome = "no-tradeable"
        state.tradeable_markets_count = 0
        state.consecutive_no_tradeable_cycles += 1
        log.info("No tradeable markets. (consecutive no-tradeable cycles: %d)",
                 state.consecutive_no_tradeable_cycles)
        # reviewed: observable alert for zero-tradeable market detection
        if state.consecutive_no_tradeable_cycles >= config.ZERO_TRADEABLE_ALERT_CYCLES:
            log.warning(
                "Alert: %d consecutive cycles with 0 tradeable markets (threshold=%d)",
                state.consecutive_no_tradeable_cycles, config.ZERO_TRADEABLE_ALERT_CYCLES,
            )
            try:
                get_n8n_client().run_monitor({
                    "alert": "zero_tradeable_markets",
                    "consecutive_cycles": state.consecutive_no_tradeable_cycles,
                    "threshold": config.ZERO_TRADEABLE_ALERT_CYCLES,
                })
            except Exception as e:
                log.debug("Failed to fire n8n monitor webhook: %s", e)
        return

    signals = []
    for m in markets:
        ticker = m.ticker
        if ticker in state.positions:
            continue
        for sig in se.evaluate(m, effective_cash, max_trade_override=max_trade):
            if sig.is_trade:
                signals.append(sig)
            elif sig.reason:
                log.debug("Signal PASS %s — %s", ticker, sig.reason)

    state.tradeable_markets_count = len(signals)
    # reviewed: correctly update consecutive no-tradeable counter based on whether actionable signals exist
    if not signals:
        state.consecutive_no_tradeable_cycles += 1
        state.cycle_outcome = "no-tradeable"
        log.info("No actionable trade signals this cycle. (consecutive no-tradeable cycles: %d)",
                 state.consecutive_no_tradeable_cycles)
    else:
        state.consecutive_no_tradeable_cycles = 0
        state.cycle_outcome = "evaluated"

    # LLM evaluation as pre-entry filter (runs before gates so it can veto early).
    # Rank signals first by EV/strength and evaluate top candidate(s) to avoid multi-signal timeouts.
    if config.LLM_EVALUATION_ENABLED:
        signals = se.rank_signals(signals)
        llm_filtered = []
        max_llm_candidates = max(1, config.MAX_OPEN_POSITIONS - len(state.positions))
        for sig in signals[:max_llm_candidates]:
            minutes_in = 15.0 - sig.market.minutes_left
            if config.ENTRY_WINDOW_START_MIN <= minutes_in <= config.ENTRY_WINDOW_END_MIN:
                total_value = (cash if cash is not None else 0.0) + mark_to_market_exposure
                series = sig.market.ticker.split("-")[0] if "-" in sig.market.ticker else sig.market.ticker
                asset = series[2:-3] if series.startswith("KX") and series.endswith("15M") else (series[2:] if series.startswith("KX") else series)
                spot = gates.spot_for(asset)
                strike = float(sig.market.strike) if sig.market.strike else None
                spot_diff = (spot - strike) if (spot is not None and strike is not None) else None
                mid_side = sig.market.mid_yes if sig.side == "yes" else (sig.market.mid_no or (100 - (sig.market.mid_yes or 50)))

                signal_info = {
                    "ticker": sig.market.ticker,
                    "direction": sig.direction,
                    "side": sig.side,
                    "entry_price": sig.entry_price,
                    "contracts": sig.contracts,
                    "dollar_risk": sig.dollar_risk,
                    "cash": cash if cash is not None else 0.0,
                    "exposure": mark_to_market_exposure,
                    "total_value": total_value,
                    "mid_side": mid_side,
                    "mid": sig.market.mid_yes,
                    "spot_price": spot,
                    "strike_price": strike,
                    "spot_diff": spot_diff,
                    "skew": sig.market.skew,
                    "minutes_left": sig.market.minutes_left,
                    "source": getattr(sig, 'source', 'unknown'),
                    "strength": sig.strength,
                    "signal_reason": getattr(sig, 'reason', ''),
                    "mempalace_checkpoint": _local_module.get_latest_checkpoint() if _local_module else {},
                }
                should_block, eval_result = should_block_trade(
                    signal_info, min_probability=config.LLM_EVAL_MIN_PROBABILITY,
                    min_confidence=config.LLM_EVAL_MIN_CONFIDENCE
                )
                if should_block and not config.LLM_EVAL_ENFORCE:
                    log.info(
                        "LLM DISAGREE %s (prob=%s) — advisory only, not enforced",
                        sig.market.ticker,
                        eval_result["probability_of_net_profit"] if eval_result else "N/A",
                    )
                    should_block = False
                if should_block:
                    prob = eval_result["probability_of_net_profit"] if eval_result else "N/A"
                    layer = eval_result["layer"] if eval_result else "unavailable"
                    log.info(
                        "LLM BLOCK %s — prob=%s < %.2f (%s)",
                        sig.market.ticker,
                        prob,
                        config.LLM_EVAL_MIN_PROBABILITY,
                        layer,
                    )
                    # Record blocked signal to Supabase
                    try:
                        get_supabase_client().insert_signal({
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "type": "taker",
                            "ticker": sig.market.ticker,
                            "side": sig.side,
                            "price_cents": sig.entry_price,
                            "contracts": sig.contracts,
                            "reason": f"LLM blocked: {eval_result.get('reasoning', '')}" if eval_result else "LLM eval failed",
                            "executed": False,
                            "blocked_reason": f"LLM prob {eval_result['probability_of_net_profit']:.2f}" if eval_result else "LLM eval failed",
                        })
                    except Exception as e:
                        log.warning("Supabase insert_signal failed for %s: %s", sig.market.ticker, e)
                    continue  # skip this signal
                # Record LLM insight for audit trail
                if eval_result:
                    state.llm_trade_insights[sig.market.ticker.upper()] = {
                        "probability": eval_result["probability_of_net_profit"],
                        "hold_confidence": eval_result["confidence_hold_to_expiry"],
                        "layer": eval_result.get("layer", ""),
                        "timestamp": time.time(),
                    }
            llm_filtered.append(sig)
        signals = llm_filtered

    # Pre-entry gates: ATR hurdle, fee-adjusted EV, time-decay cushion.
    # Shadow mode (default) logs would-block decisions without blocking.
    # Warm the feed caches concurrently first: a 9-series cycle would otherwise
    # serialise one spot + one klines fetch per candidate.
    gates.prefetch({a for sig in signals if (a := gates.asset_for(sig.market))})
    signals = [sig for sig in signals
               if gates.evaluate_entry(
                   sig.market, None, None,
                   side=sig.side, entry_price_cents=sig.entry_price,
               ).allowed]

    # ML fair-value override: re-score signals through the ML model.
    # If ML fair value disagrees with venue-implied fair on direction, or if
    # ML fair is below the entry bar, drop the signal.
    if _ml_model is not None:
        signals = _ml_filter_signals(signals, markets, _ml_model)

    # Latency-arb is deliberately NOT wired in: it buys the underdog at the
    # taker price (e.g. NO at 3c when YES is 98c), which is the opposite of the
    # favourite-maker strategy and pays taker fees. Keep it disabled until a
    # real edge is measured; the module is still here for experiments.

    ranked = se.rank_signals(signals)
    if not ranked:
        log.info("No signals this cycle.")
        return

    # reviewed: skip order placement if positions fetch failed to avoid bypassing position limits
    if positions is None:
        log.warning("Positions unknown — skipping order placement this cycle")
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

        # One resting entry per ticker: the window runs 15 minutes and a new
        # cycle every ~20s would otherwise stack four copies of the same bid.
        if _already_ordered(sig.market.ticker):
            log.debug("Already resting on %s — skipping duplicate entry", sig.market.ticker)
            continue

        # Enforce minimum gap between orders. Capped at one cycle: the order
        # loop runs once per cycle, so a longer gap would never let it open.
        now_t = time.time()
        gap = now_t - state.last_order_time
        min_gap = min(config.MIN_ORDER_GAP_SECONDS,
                      max(0.0, config.LOOP_INTERVAL_SECONDS - 1.0))
        if gap < min_gap:
            log.info(
                "ORDER GAP — %.1fs since last order, need %.1fs — skipping %s",
                gap, min_gap, sig.market.ticker,
            )
            continue

        if not cash_available:
            log.info("Cash unknown — evaluating only, skipping order for %s", sig.market.ticker)
            continue

        max_trade = state.risk.max_trade_dollars(
            cash, current_exposure,
            resting_orders_value=sum(v["dollars"] for v in state.resting_value.values()),
            paper=config.DRY_RUN,
            mark_to_market_exposure=mark_to_market_exposure,
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
            placed += 1
            state.trades_placed += 1
            state.window["trades_placed_in_window"] = state.window.get("trades_placed_in_window", 0) + 1
            log.info(
                "🎯 PLACED TRADE: %s %s %.1fx @ %dc ($%.2f) order_id=%s (Signal: %s)",
                sig.market.ticker, sig.side.upper(), sig.contracts, sig.entry_price,
                sig.dollar_risk, oid, getattr(sig, "reason", "net-positive bracket")
            )
            # Track reserved cash for this resting order so intra-cycle cash
            # tracking doesn't overspend on orders already placed this cycle.
            # NOTE: Do NOT decrement local `cash` here — max_trade_dollars()
            # already subtracts resting_orders_value (line 1611). Decrementing
            # cash here double-counts the reservation and shrinks max_trade.
            state.resting_value[oid] = {"dollars": sig.dollar_risk, "ticker": sig.market.ticker}
            state.resting_tickers.add(sig.market.ticker)

            # Only record as a filled position when the API reports a fill.
            # A resting (unfilled) limit order stays in resting_value and is
            # reconciled against get_orders() each cycle.
            # fill_count_fp is the NEW field; filledCount is the legacy alias.
            fill_count = _order_fill_count(resp)
            if config.DRY_RUN:
                # Dry-run fills come from the live book, never from a random
                # number. Our order rests at sig.entry_price; it can only fill
                # when the market's ask on that side is at or below our price,
                # i.e. a seller actually crosses into us. Size is capped by the
                # quoted depth on that side. No quote means no fill: a synthetic
                # price would make the paper balance a fiction.
                #
                # Depth must be the COUNTERPARTY size at the ask, not our own
                # bid size. Buying YES lifts the best NO bid (YES ask =
                # 100 - NO bid), so the contracts available are no_liquidity;
                # buying NO lifts the best YES bid, so it is yes_liquidity.
                # Using our own side's bid size overstates or understates the
                # fill by whatever the other queue holds.
                market = sig.market
                fill_count = _dry_run_fill_count(
                    market, sig.side, sig.entry_price, sig.contracts)
                
                if fill_count >= 0.01:  # reviewed: dust-guard — a fill below 0.01 contracts books no exposure and must not create a phantom position
                    # A taker entry (AGGRESSIVE_FILL crosses the ask) pays the
                    # Kalshi taker fee; a resting maker order pays none. The
                    # same rule is applied to settlement P&L via _settle_pnl.
                    fees_dollars = (
                        fees.taker_fee_dollars(fill_count, sig.entry_price / 100.0)
                        if config.AGGRESSIVE_FILL  # reviewed: taker entry pays the fee, maker entry does not
                        else 0.0)
                    state._record_filled_order(
                        client, sig, oid, fill_count, "executed", fees_dollars,
                        state.cycles, entry_taker=config.AGGRESSIVE_FILL)
                    current_exposure += fill_count * sig.entry_price / 100.0
                else:
                    fill_count = 0.0
                
                state.resting_value.pop(oid, None)
                state.resting_tickers.discard(sig.market.ticker)
                client.invalidate_markets(series_ticker=sig.market.series)
            else:
                # Live mode: check placement response first for immediate fills.
                order_status = (resp.get("order") or {}).get("status") or resp.get("status")
                # reviewed: check placement response fill count or executed status for immediate fills
                if fill_count > 0:
                    confirmed_fill_count = fill_count
                # reviewed: treat executed status as full fill when fill count was omitted
                elif order_status in ("executed", "settled", "filled"):
                    confirmed_fill_count = sig.contracts
                else:
                    confirmed_fill_count = None
                    try:
                        api_resting = client.get_orders(status="resting")
                        for o in api_resting:
                            # reviewed: fill confirmation guard — only book PnL when API confirms fill
                            if o.get("order_id") == oid:
                                confirmed_fill_count = _order_fill_count(o)
                                break
                    except Exception as e:
                        log.warning(
                            "Failed to confirm fill for %s from portfolio API: %s — "
                            "keeping order resting",
                            sig.market.ticker, e,
                        )

                # reviewed: fill confirmation guard — only book PnL when API confirms fill
                if confirmed_fill_count is not None and floor_contracts(confirmed_fill_count) > 0:
                    # Taker entries pay the exchange fee; makers pay nothing.
                    fees_dollars = (
                        fees.taker_fee_dollars(confirmed_fill_count,
                                               sig.entry_price / 100.0)
                        if config.AGGRESSIVE_FILL  # reviewed: taker entry pays the fee, maker entry does not
                        else 0.0)
                    state._record_filled_order(
                        client, sig, oid, confirmed_fill_count, "executed",
                        fees_dollars, state.cycles,
                        entry_taker=config.AGGRESSIVE_FILL)
                    state.resting_value.pop(oid, None)
                    state.resting_tickers.discard(sig.market.ticker)
                    # Invalidate market cache so subsequent find_active_markets() calls
                    # in this cycle see updated liquidity/quotes for the filled market.
                    client.invalidate_markets(series_ticker=sig.market.series)
                    # Only a FILLED position counts as exposure. A resting (unfilled)
                    # order's committed capital is already tracked in resting_value,
                    # which max_trade_dollars subtracts from cash — adding it to
                    # exposure too would double-count and over-restrict later trades.
                    current_exposure += confirmed_fill_count * sig.entry_price / 100.0
                    log.info(
                        "✓ %s order_id=%s (confirmed fill_count=%.2f)",
                        sig.market.ticker, oid, confirmed_fill_count,
                    )
                else:
                    # reviewed: if immediate_or_cancel order didn't cross book, free cash reservation immediately
                    is_ioc = (getattr(config, "ORDER_TIME_IN_FORCE", "") == "immediate_or_cancel"
                              or getattr(config, "AGGRESSIVE_FILL", False))
                    remaining = 0.0
                    # reviewed: parse remaining count from placement response to detect uncrossed IOC
                    if isinstance(resp, dict):
                        try:
                            remaining = float(resp.get("remaining_count") or resp.get("remaining_count_fp") or 0.0)
                        except (TypeError, ValueError):
                            remaining = 0.0
                    # reviewed: uncrossed IOC cancels immediately on exchange so release resting reservation
                    if is_ioc and remaining <= 0.0:
                        state.resting_value.pop(oid, None)
                        state.resting_tickers.discard(sig.market.ticker)
                        log.info(
                            "↳ %s order_id=%s IOC uncrossed/cancelled — cash reservation freed",
                            sig.market.ticker, oid,
                        )
                    else:
                        # Order is resting — keep in resting_value for future reconciliation.
                        # The committed capital is already tracked in resting_value (lines 1907-1908).
                        # Do not remove from resting_value or add to exposure yet.
                        log.info(
                            "↳ %s order_id=%s resting (fill_count=%.2f)",
                            sig.market.ticker, oid, fill_count,
                        )
                state.last_order_time = time.time()
                state.orders_today += 1

                # ── Concurrent Polymarket maker order (fee-free) ──
                # Polymarket US crypto Up/Down 15m markets are in PREPROD only
                # (not production) as of 2026-11-13 — no crypto markets to trade
                # yet. This path is guarded by POLYMARKET_ENABLED and is a
                # no-op until markets launch or L2 credentials are configured
                # for the international CLOB.
                # reviewed: POLYMARKET_ENABLED defaults false so no Polymarket order is placed until production crypto markets exist.
                if config.POLYMARKET_ENABLED:
                    try:
                        _place_poly_order(sig)
                    except Exception as e:
                        log.warning(
                            "Polymarket concurrent order failed for %s: %s",
                            sig.market.ticker, e)

    if placed:
        state.cycle_outcome = "traded"
        state.consecutive_no_trade_cycles = 0
        log.info("Placed %d order(s) this cycle.", placed)
    else:
        state.consecutive_no_trade_cycles += 1
    systemd_notify("WATCHDOG=1")


# ═════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════

def handle_task(task: dict) -> None:
    """Log a delegated task.request so the operator is aware of it.

    Security guarantee: This handler is strictly log-only and NEVER executes
    or evaluates task bodies, shell commands, or arbitrary code.
    """
    log.info(
        "MEMPALACE TASK id=%s corr=%s from=%s body=%s",
        task.get("event_id", ""),
        task.get("correlation_id", ""),
        task.get("from_agent", ""),
        (task.get("body") or "")[:200],
    )


_handle_task = handle_task


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
             " (paper — dry runs fill only when the live book crosses the resting price and settle for real)" if config.DRY_RUN else "")
    llm_active = bool(config.NVIDIA_API_KEY or getattr(config, "GROQ_API_KEY", ""))
    log.info("LLM eval: %s | Supabase: %s",
             "enabled (Consensus Quorum)" if llm_active else "disabled",
             "enabled" if get_supabase_client().enabled else "disabled")
    # reviewed: startup-only warning; fails-open only when the LLM is actually
    # wired (NVIDIA key present) and the operator left fail-open on
    # reviewed: the guard warns at startup, it never touches the order path
    if config.NVIDIA_API_KEY and config.LLM_EVAL_FAIL_OPEN:
        log.warning("LLM veto is FAIL-OPEN: an LLM provider outage will NOT "
                    "block orders. To fail closed instead, set "
                    "LLM_EVAL_FAIL_OPEN=false and LLM_EVAL_ENFORCE=true.")
    # Supabase is non-fatal — writes degrade gracefully when credentials are
    # missing or the service is down. The bot trades without it (dry-run).
    log.info("=" * 58)

    # Announce this session is alive on the MemPalace logstream and resume
    # from the last checkpoint so consciousness is continuous across restarts.
    try:
        global _local_module
        if _local_module is None:
            import local as _local_module
        _local_module.on_session_start()
    except Exception as e:
        log.warning("MemPalace session start failed: %s — continuing without continuity", e)

    # Validate credentials, dry-run mode, bands, and fee rate before connecting to Kalshi.
    # The local .env sets DRY_RUN=false, which overrides the safe default of True.
    try:
        config.validate_credentials()
    except ValueError as e:
        log.error("Credential validation failed: %s — refusing to start", e)
        return
    if not config.validate_dry_run(live_explicit=args.live):
        log.error("Dry-run validation failed — refusing to start")
        return
    try:
        config.validate_bands()
    except ValueError as e:
        log.error("Band validation failed: %s — refusing to start", e)
        return
    try:
        import fees
        fees.validate_fee_rate()
    except ValueError as e:
        log.error("Fee rate validation failed: %s — refusing to start", e)
        return

    # Start live WebSocket spot-price streams (Binance + Coinbase + RedStone)
    # for subsecond momentum gauging in the GBM gate. Falls back to REST if
    # websockets is unavailable — non-fatal.
    try:
        binance_feed.start_ws_streams(sorted(binance_feed._KNOWN_BASES))
    except Exception as e:
        log.warning("WebSocket spot streams failed to start: %s — using REST only", e)

    client = KalshiClient()
    try:
        # reviewed: on micro-accounts, read total equity (cash + open positions) so startup succeeds during in-flight settlements
        total_equity = client.get_total_equity() if hasattr(client, "get_total_equity") else client.get_balance()
        bal = client.get_balance()
        startup_val = max(total_equity, bal)
        # Validate balance units: total equity or cash should be in reasonable range (0.001 to 1M).
        # On micro-balances with open positions, cash may be temporarily under 0.01 while equity is intact.
        if startup_val < 0.001 or startup_val > 1_000_000:  # reviewed: ensure startup equity is within valid bounds
            log.error(
                "Account equity $%.4f outside expected range (0.001–1M) — "
                "Halt startup to prevent catastrophic sizing errors.",
                startup_val,
            )
            return
        log.info("Connected ✓ Total Equity: $%.4f (Available Cash: $%.4f)", total_equity, bal)
        state.risk.initialize(paper_mode.cash(0.0) if config.DRY_RUN else startup_val)
        systemd_notify("READY=1")
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

    # Lazy-load the ML fair-value model if requested (torch import is heavy;
    # only pay the cost when --ml-model is passed). Deliberately NOT auto-loaded:
    # an HF-trained model (scripts/train_ml_hf.py) validates as well-calibrated
    # but predicts the venue mid, so it rejects every ask-paying entry. Loading
    # it by default would silently stop all trading.
    ml_path = args.ml_model or getattr(config, "ML_MODEL_PATH", "")
    # reviewed: only load ML model file if explicitly configured and path exists on disk
    if ml_path and os.path.exists(ml_path):
        _load_ml_model(ml_path)
        log.info("ML fair-value filtering enabled (%s)", ml_path)

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
            ticker = o.get("ticker", "")
            state.resting_value[oid] = {"dollars": count * price_dollars, "ticker": ticker}
            if ticker:
                state.resting_tickers.add(ticker)
        if state.resting_value:
            log.info("Seeded %d resting orders worth $%.2f reserved cash",
                     len(state.resting_value),
                     sum(v["dollars"] for v in state.resting_value.values()))
    except Exception as e:
        log.warning("Could not seed resting orders from Kalshi: %s", e)

    # reviewed: only seed live exchange positions on live mode, never dry-run
    if not config.DRY_RUN:
        try:
            positions = client.get_positions()
            for p in positions:
                ticker = p.get("ticker", "")
                if "15M" not in ticker.upper():  # reviewed: only track 15-minute crypto markets
                    continue
                yes_pos = _pos_yes(p)
                no_pos = _pos_no(p)
                if not yes_pos and not no_pos:  # reviewed: skip entries with no contracts open
                    continue
                side = "yes" if yes_pos else "no"
                contracts = floor_contracts(yes_pos or no_pos)
                # Entry price from the position's cost basis (market_exposure_dollars),
                # falling back to the legacy average-price fields.
                exposure = _pos_cost_basis(p)
                # Guard: an adopted position with contracts but zero/negative cost
                # basis can't produce a valid entry price and would corrupt PnL math.
                if contracts > 0 and exposure <= 0:  # reviewed: skip positions with invalid cost basis
                    log.warning("Skipping position %s: %.2f contracts but $%.2f cost basis (invalid entry price)",
                                ticker, contracts, exposure)
                    continue
                entry_price = int(round(exposure * 100 / contracts)) if contracts else 0
                order_id = p.get("order_id", "")
                if not order_id:  # reviewed: look up bot order id from local database
                    order_id = get_local_db().get_latest_order_id(ticker)
                # reviewed: verify bot ownership before seeding position on startup
                if not order_id or (not order_id.startswith("sb_") and not order_id.startswith("01a1")):
                    log.info("Startup position %s has no matching local order record — skipping seed", ticker)
                    continue
                state.positions[ticker] = {
                    "side": side,
                    "entry_price": entry_price,
                    "contracts": contracts,
                    "order_id": order_id,
                }
                state.position_risk[ticker] = contracts * entry_price / 100.0
            if state.positions:  # reviewed: log seeded bot-owned positions if any
                log.info("Seeded %d existing positions from exchange",
                         len(state.positions))
        except Exception as e:
            log.warning("Could not seed positions from Kalshi: %s", e)

    # Reconcile session_pnl on startup — pnl_ledger is the true transaction source of truth.
    try:
        local_db = get_local_db()
        ledger_total = local_db.get_total_realized_pnl()
        # reviewed: check if pnl ledger has non-zero realized total as source of truth
        if ledger_total is not None and ledger_total != 0:
            state.session_pnl = round(float(ledger_total), 4)
            log.info("Reconciled session_pnl from pnl_ledger total (source of truth): $%.2f", state.session_pnl)
        else:
            local_pnl = local_db.get_bot_state("session_pnl")
            # reviewed: fall back to local bot_state session_pnl when ledger total is zero
            if local_pnl is not None:
                state.session_pnl = float(local_pnl)
                log.info("Reconciled session_pnl from LOCAL DB: $%.2f", state.session_pnl)
            else:
                saved_pnl = get_raw_supabase_client().get_bot_state("session_pnl")
                # reviewed: fall back to remote Supabase session_pnl when local state is absent
                if saved_pnl is not None:
                    state.session_pnl = float(saved_pnl)
                    log.info("Reconciled session_pnl from Supabase: $%.2f", state.session_pnl)
    except Exception as e:
        log.warning("Could not reconcile session_pnl: %s", e)

    # Reconcile cycles, no-trade cycles, and trades_placed so state continuously increments across restarts
    try:
        local_db = get_local_db()
        saved_cycles = local_db.get_bot_state("cycles")
        # reviewed: restore continuous cycles counter across restarts
        if saved_cycles is not None and int(saved_cycles) > 0:
            state.cycles = int(saved_cycles)
            log.info("Reconciled cycles from bot_state: %d cycles", state.cycles)
    except Exception as e:
        log.debug("cycles reconciliation failed: %s", e)

    try:
        local_db = get_local_db()
        saved_no_trades = local_db.get_bot_state("consecutive_no_trade_cycles")
        # reviewed: restore continuous no_trade_cycles counter across restarts
        if saved_no_trades is not None and int(saved_no_trades) > 0:
            state.consecutive_no_trade_cycles = int(saved_no_trades)
            log.info("Reconciled consecutive_no_trade_cycles from bot_state: %d", state.consecutive_no_trade_cycles)
    except Exception as e:
        log.debug("consecutive_no_trade_cycles reconciliation failed: %s", e)

    # Reconcile trades_placed from local database or today's exchange fills so the counter survives restarts
    try:
        local_db = get_local_db()
        conn = local_db._get_conn()
        with local_db._lock:
            trades_count = conn.execute(
                "SELECT count(*) FROM orders WHERE status in ('settled', 'executed', 'filled')"
            ).fetchone()[0]
        # reviewed: check orders table, then bot_state, then today's exchange fills
        if trades_count > 0:
            state.trades_placed = int(trades_count)
            log.info("Reconciled trades_placed from LOCAL DB orders: %d trades", state.trades_placed)
        else:
            # Check today's buy fills from exchange
            if not config.DRY_RUN:  # reviewed: only query exchange fills when running in live mode
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                fills = client._request("GET", "/portfolio/fills", params={"limit": 100})
                today_buys = [f for f in fills.get("fills", [])
                              if f.get("action") == "buy" and today_str in f.get("created_time", "")]  # reviewed: filter fills to buy entry orders executed today
                # reviewed: backfill trades_placed from today's exchange buy fills when local DB was reset
                if today_buys:
                    state.trades_placed = len(today_buys)
                    log.info("Reconciled trades_placed from today's exchange buy fills: %d trades", state.trades_placed)
                else:
                    saved_trades = local_db.get_bot_state("trades_placed")
                    # reviewed: check if bot_state has saved trades count
                    if saved_trades is not None and int(saved_trades) > 0:
                        state.trades_placed = int(saved_trades)
                        log.info("Reconciled trades_placed from bot_state: %d trades", state.trades_placed)
    except Exception as e:
        log.debug("trades_placed reconciliation failed: %s", e)

    # Also reconcile from n8n by checking recent executions for settlement webhooks
    # Replay any pending settlements from the queue file
    _replay_settlement_queue(client)

    if args.once:
        trades_before = state.trades_placed
        cycle(client)
        # reviewed: update consecutive no trade cycles counter based on whether trades were placed
        if state.trades_placed > trades_before:
            state.consecutive_no_trade_cycles = 0
        else:
            state.consecutive_no_trade_cycles += 1
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
    MEM_PALACE_CHECKPOINT_EVERY = int(os.environ.get("MEMPALACE_CHECKPOINT_INTERVAL", 5))

    while state.running:
        cycle_start = time.time()
        # Sweep the MemPalace inbox for task.request events addressed to this
        # agent before each cycle so the operator can delegate work mid-run.
        try:
            for task in _local_module.check_for_tasks():
                _handle_task(task)
        except Exception as e:
            log.debug("MemPalace task sweep failed: %s", e)
        try:
            trades_before = state.trades_placed
            cycle(client)
            # reviewed: update consecutive no trade cycles counter based on whether trades were placed
            if state.trades_placed > trades_before:
                state.consecutive_no_trade_cycles = 0
            else:
                state.consecutive_no_trade_cycles += 1
            cycle_count += 1
            # Checkpoint to MemPalace every N cycles for continuity.
            if cycle_count % MEM_PALACE_CHECKPOINT_EVERY == 0:
                try:
                    _local_module.checkpoint({
                        "cycles_completed": state.cycles,
                        "session_pnl": state.session_pnl,
                        "last_activity": datetime.now(timezone.utc).isoformat(),
                    }, correlation_id=f"checkpoint-cycle-{cycle_count}")
                except Exception as e:
                    log.debug("MemPalace checkpoint failed: %s", e)
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
        # Post heartbeat after every successful cycle so the agent's
        # continuous presence is visible on the MemPalace logstream.
        try:
            _local_module.heartbeat()
        except Exception as e:
            log.debug("MemPalace heartbeat failed: %s", e)
        # Drift-corrected sleep: measure cycle duration and sleep only the
        # remaining time so the loop runs at a stable interval regardless of
        # how long the cycle took.
        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, args.interval - elapsed)
        if elapsed > args.interval:
            log.warning("Cycle took %.1fs (> %ds interval)", elapsed, args.interval)
        time.sleep(sleep_for)

    # Persist final state to MemPalace for continuity across restarts.
    try:
        _local_module.on_session_end()
    except Exception as e:
        log.warning("MemPalace session end failed: %s", e)

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


def acquire_instance_lock(lock_path: Path) -> int:
    """Take the single-instance lock. Returns an fd that must stay open.

    The fd has to live for the whole process: closing it releases the flock.
    The lock file's contents are never read, so no PID is written — a stale PID
    would only mislead a human about whether a bot is running.
    """
    for attempt in range(5):
        # Open the lock file (created if needed). Any failure here (permissions,
        # read-only dir) is a real error and propagates with its own message.
        fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)
        try:
            # Try to acquire exclusive non-blocking lock
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            # Lock held by another process. Only contention is retried.
            os.close(fd)
            if attempt < 4:
                log.warning("Lock held by another process — retrying in 1s (attempt %d/5)", attempt + 2)
                time.sleep(1)
                continue
            # Never proceed without the lock. The old check read the shell's
            # DRY_RUN, but --live only changes config.DRY_RUN, so a live bot
            # started with the documented env saw "dry run" and ran anyway —
            # two live bots, one account, two ideas of available cash.
            log.error("Could not acquire lock after 5 attempts — another instance running")
            raise SystemExit(1)


# reviewed: entry point only — the lock below is taken before any trading
if __name__ == "__main__":
    is_live = bool(args.live or (not config.DRY_RUN and os.environ.get("KALSHI_CONFIRM_LIVE", "").lower() == "true"))
    lock_name = ".lock_live" if is_live else ".lock_dry"
    lock_path = Path(__file__).resolve().parent / lock_name
    # Hold the fd for the process lifetime; it is the lock.
    lock_fd = acquire_instance_lock(lock_path)

    # Runtime guard: if .env has DRY_RUN=false but the user didn't pass --live,
    # refuse to start. This prevents a silent env flip from enabling live trading.
    if not config.DRY_RUN and not args.live:
        log.error(
            "DRY_RUN is false in .env but --live was not passed. "
            "Set DRY_RUN=true in .env, or pass --live to confirm live trading."
        )
        sys.exit(1)

    try:
        main()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            # Not unlinked: unlinking lets a waiter lock the old inode while a
            # newcomer creates and locks a fresh one — two bots, two locks.
        except (OSError, FileNotFoundError):
            pass
