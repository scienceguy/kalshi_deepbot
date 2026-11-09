"""
config.py — Kalshi SuperBot configuration.

Consolidated from kalshi_15m_bot, kalshi_live_bot, kalshi_gemini_bot,
kalshi_exp_bot, FlashBot.v6, Momentum.v15, kalshi_1230.

Consolidation strategy: take the best-performing parameters from each bot's
backtests, with environment-variable overrides for live tuning.
"""

import logging
import os
import socket
from pathlib import Path

log = logging.getLogger(__name__)


def get_local_ip() -> str:
    """Get the bot's local IP address without relying on netlink sockets.

    On Android 11+, AF_NETLINK sockets (used by `ip`, `ss`) are blocked for
    regular apps. This fallback opens a UDP socket to an external host and
    reads the source address — it works without special permissions.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

_MODE = os.environ.get("KALSHI_MODE", "live")
_load_done = False


def _load_env():
    """Load .env files into os.environ.

    Precedence: existing environment variables win over .env values.
    A key already set in the environment is NOT overwritten by .env.
    Files are loaded in order: .env, then .env.{MODE} (later files can
    override earlier ones only if the key isn't already in the environment).

    Supports: simple `KEY=VALUE` lines. Existing environment variables win
    over .env values.
    """
    global _load_done
    if _load_done:
        return
    _load_done = True
    base = Path(__file__).resolve().parent
    env_files = [base / ".env", base / f".env.{_MODE}"]
    for env_path in env_files:
        if not env_path.exists():
            continue
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                # Strip optional `export ` prefix
                if line.startswith("export "):
                    line = line[len("export "):]
                k, _, v = line.partition("=")
                k = k.strip(); v = v.strip().strip('"').strip("'")
                if not k or not v:
                    continue
                if k not in os.environ:
                    os.environ[k] = v


_load_env()


def _env_float(name, default):
    val = os.environ.get(name, "")
    if not val:
        return default
    try:
        return float(val)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid float value for {name}: '{val}'. {e}")


def _env_int(name, default):
    val = os.environ.get(name, "")
    if not val:
        return default
    try:
        return int(val)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid int value for {name}: '{val}'. {e}")


def _env_bool(name, default):
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    if val not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        raise ValueError(
            f"Unrecognized boolean value for {name}: '{val}'. "
            f"Expected one of: 1, true, yes, on, 0, false, no, off"
        )
    return val in ("1", "true", "yes", "on")


def _env_str(name, default):
    """Read a string env var, returning default if unset or empty."""
    return os.environ.get(name, default) or default


# ═════════════════════════════════════════════════════════════════════
# CREDENTIALS
# ═════════════════════════════════════════════════════════════════════
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
KALSHI_KEY_FILE = os.environ.get("KALSHI_KEY_FILE", "")
KALSHI_PRIVATE_KEY_CONTENT = os.environ.get("KALSHI_PRIVATE_KEY", "")
KALSHI_BASE_URL = os.environ.get("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
KALSHI_ALLOW_NONSTANDARD_URL = _env_bool("KALSHI_ALLOW_NONSTANDARD_URL", False)

# ═════════════════════════════════════════════════════════════════════
# MARKET TARGETING (from kalshi_live_bot — expanded series list)
# ═════════════════════════════════════════════════════════════════════
TARGET_SERIES_DEFAULT = [
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M",
    "KXBNB15M", "KXHYPE15M", "KXZEC15M", "KXNEAR15M",
]
TARGET_SERIES_ENV = os.environ.get("TARGET_SERIES", "")
if TARGET_SERIES_ENV.strip():
    TARGET_SERIES = [s.strip() for s in TARGET_SERIES_ENV.split(",") if s.strip()]
else:
    TARGET_SERIES = list(TARGET_SERIES_DEFAULT)

# ═════════════════════════════════════════════════════════════════════
# SIGNAL THRESHOLDS (optimized from backtests - 2026-11-03)
# Backtest best: MIN_SKEW=3, MIN_TREND=8, window=6-11min, band=15-55c
# EV=0.58/trade, win_rate=52.8%, maxDD=84.6% (needs risk management)
# Real backtest best: EV=0.077, win_rate=48.2%, maxDD=68.4%
# ═════════════════════════════════════════════════════════════════════
MIN_SKEW_CENTS = _env_int("MIN_SKEW_CENTS", 3)
MIN_TREND_CENTS = _env_int("MIN_TREND_CENTS", 8)
SKEW_ENTRY_MIN = _env_int("SKEW_ENTRY_MIN", 3)
SKEW_ENTRY_MAX = _env_int("SKEW_ENTRY_MAX", 97)
# Entry window: cover markets from when they're discovered (~4 min elapsed)
# through near-final minute (14 min elapsed). The GBM model filter then
# selects only those with positive edge after fees. Research shows GBM spot-vs-
# strike edge exists whenever spot is meaningfully ITM vs strike, not just in
# the final 60 seconds.
ENTRY_WINDOW_START_MIN = _env_float("ENTRY_WINDOW_START_MIN", 4.0)
ENTRY_WINDOW_END_MIN = _env_float("ENTRY_WINDOW_END_MIN", 14.0)
# Final-minute taker price bands: pay the ask on the favourite (75-99c)
MIN_YES_PRICE_CENTS = _env_int("MIN_YES_PRICE_CENTS", 75)
MAX_YES_PRICE_CENTS = _env_int("MAX_YES_PRICE_CENTS", 99)
MID_TARGET_CENTS = _env_int("MID_TARGET_CENTS", 50)
MID_TOLERANCE_CENTS = _env_int("MID_TOLERANCE_CENTS", 2)

# ═════════════════════════════════════════════════════════════════════
# PRE-ENTRY GATES (gates.py) — ATR hurdle, fee-adjusted EV, time-decay
# ═════════════════════════════════════════════════════════════════════
GATES_ENABLED = _env_bool("GATES_ENABLED", True)
# Shadow mode logs would-block decisions and never blocks an order. Set
# GATES_SHADOW_MODE=false to let the gates actually deny entries.
GATES_SHADOW_MODE = _env_bool("GATES_SHADOW_MODE", False)

# Gate 1 — ATR hurdle
GATES_ATR_ENABLED = _env_bool("GATES_ATR_ENABLED", True)
GATES_ATR_LOOKBACK = _env_int("GATES_ATR_LOOKBACK", 14)
GATES_ATR_HURDLE = _env_float("GATES_ATR_HURDLE", 1.0)
GATES_ATR_HURDLE_BTC = _env_float("GATES_ATR_HURDLE_BTC", GATES_ATR_HURDLE)
GATES_ATR_HURDLE_ETH = _env_float("GATES_ATR_HURDLE_ETH", GATES_ATR_HURDLE)
GATES_ATR_HURDLE_SOL = _env_float("GATES_ATR_HURDLE_SOL", GATES_ATR_HURDLE)
GATES_ATR_HURDLE_XRP = _env_float("GATES_ATR_HURDLE_XRP", GATES_ATR_HURDLE)
GATES_ATR_HURDLE_DOGE = _env_float("GATES_ATR_HURDLE_DOGE", GATES_ATR_HURDLE)
GATES_ATR_HURDLE_BNB = _env_float("GATES_ATR_HURDLE_BNB", GATES_ATR_HURDLE)
GATES_ATR_HURDLE_HYPE = _env_float("GATES_ATR_HURDLE_HYPE", GATES_ATR_HURDLE)
GATES_ATR_HURDLE_ZEC = _env_float("GATES_ATR_HURDLE_ZEC", GATES_ATR_HURDLE)
GATES_ATR_HURDLE_NEAR = _env_float("GATES_ATR_HURDLE_NEAR", GATES_ATR_HURDLE)
# True: Gate 1 measures the move needed *against* the position (0 for a
# favourite already past the strike). False: the spec-literal |strike-spot|,
# which flags nearly every in-the-money favourite.
GATES_ATR_SIDE_AWARE = _env_bool("GATES_ATR_SIDE_AWARE", True)

# Gate 2 — fee-adjusted EV (dollars per contract)
GATES_EV_ENABLED = _env_bool("GATES_EV_ENABLED", True)
GATES_MIN_EDGE = _env_float("GATES_MIN_EDGE", 0.005)
# Taker fee by default (conservative: an exit may cross the book).
# Set false when every fill and exit is a maker order.
GATES_FEE_TAKER = _env_bool("GATES_FEE_TAKER", True)

# Gate 3 — time-decay cushion
GATES_TIME_DECAY_ENABLED = _env_bool("GATES_TIME_DECAY_ENABLED", True)
GATES_HIGH_GAMMA_MINUTES = _env_float("GATES_HIGH_GAMMA_MINUTES", 2.0)
GATES_DENY_HIGH_GAMMA = _env_bool("GATES_DENY_HIGH_GAMMA", False)

# Feed cache + per-candidate JSONL diagnostics
GATES_SPOT_CACHE_SECONDS = _env_int("GATES_SPOT_CACHE_SECONDS", 30)
GATES_ATR_CACHE_SECONDS = _env_int("GATES_ATR_CACHE_SECONDS", 60)
# Local DB → Supabase background sync interval. Trading bots need fast crash
# protection: a 5-minute sync window can lose up to 5 minutes of P&L/state on
# a crash. 30s keeps the local→online gap small without hammering Supabase.
LOCAL_DB_SYNC_INTERVAL = _env_float("LOCAL_DB_SYNC_INTERVAL", 30.0)
GATES_LOG_FILE = Path(
    os.environ.get("GATES_LOG_FILE")
    or Path(__file__).resolve().parent / "gates_log.jsonl"
)

# ═════════════════════════════════════════════════════════════════════
# ORDER BEHAVIOR (final-minute taker — research-optimized)
# Research (Turbine, Bot for Kalshi, PredictionMarketsPicks) shows edge only
# in the final 60 seconds. Pay the ask on the favourite, hold to BRTI settle.
# ═════════════════════════════════════════════════════════════════════
AGGRESSIVE_FILL = _env_bool("AGGRESSIVE_FILL", True)
AGGRESSIVE_ENTRY_BUFFER_CENTS = _env_int("AGGRESSIVE_ENTRY_BUFFER_CENTS", 0)
ORDER_TIME_IN_FORCE = "immediate_or_cancel"
# Final-minute favourite taker band: 75-99c (pay the ask on the favourite).
# Defaults match the documented band and market_finder's [MIN_YES_PRICE_CENTS,
# MAX_YES_PRICE_CENTS] so a tradeable bracket found by discovery is not rejected
# again by a narrower signal band (which previously blocked every ask in
# [75, 85) or (89, 99], e.g. an 84c favourite with positive GBM edge).
ENTRY_BAND_MIN = _env_int("ENTRY_BAND_MIN", 75)
ENTRY_BAND_MAX = _env_int("ENTRY_BAND_MAX", 99)
MAX_NO_ENTRY_CENTS = _env_int("MAX_NO_ENTRY_CENTS", 99)
MIN_NO_ENTRY_CENTS = _env_int("MIN_NO_ENTRY_CENTS", 75)
ALLOW_YES_BUYS = _env_bool("ALLOW_YES_BUYS", True)

# NO *market data* format only: Kalshi returns NO prices directly (not
# complemented), so this stays False. It does NOT affect order pricing —
# KalshiClient.no_price_to_api_price always sends the YES-leg complement
# (buy NO at 85c -> ask YES at 15c) for both buys and sells.
NO_PRICE_IS_COMPLEMENT = _env_bool("NO_PRICE_IS_COMPLEMENT", False)

# ════════════════════════════════════════════════════════════════════════════
# PREDICTIVE MODEL FILTER (research-backed: blind taker favorite buying is
# net-negative EV because taker fee ~0.79c at 87c exceeds raw edge ~0.67c.
# Only trade when model-implied true probability > ask price + fee + min edge.)
# ════════════════════════════════════════════════════════════════════════════
# Model type: "gbm" (Geometric Brownian Motion from spot vs strike),
# "momentum" (recent price direction), "locked_share" (BRTI average lock-in),
# "raw_mid" (no filter — market mid as fair value, negative EV expected).
MODEL_TYPE = _env_str("MODEL_TYPE", "gbm")
# Minimum edge in cents per contract after fees required to take a trade.
# At 87c ask, fee ≈ 0.79c; set min edge ≥ 0.5c to require model_prob > ask+fee+0.5c.
# Started lenient so positive-edge trades can flow and compound; the model filter
# still blocks any negative-edge trade.
MODEL_MIN_EDGE_CENTS = _env_float("MODEL_MIN_EDGE_CENTS", 0.5)
# Annualized volatility for GBM model (crypto ≈ 0.80 = 80%).
MODEL_VOLATILITY = _env_float("MODEL_VOLATILITY", 0.80)

# ═════════════════════════════════════════════════════════════════════
# HEDGE SETTINGS
# ═════════════════════════════════════════════════════════════════════
HEDGE_YES_ASK_MAX = _env_int("HEDGE_YES_ASK_MAX", 12)
MAX_HEDGE_PER_SERIES = _env_int("MAX_HEDGE_PER_SERIES", 0)

# Hedge entry band: when YES is cheap (≤HEDGE_YES_ASK_MAX), NO is expensive (≥88c).
# Separate from favourite-maker band so both can coexist.
HEDGE_MIN_NO_ENTRY_CENTS = _env_int("HEDGE_MIN_NO_ENTRY_CENTS", 82)
HEDGE_MAX_NO_ENTRY_CENTS = _env_int("HEDGE_MAX_NO_ENTRY_CENTS", 88)

# ═════════════════════════════════════════════════════════════════════
# EARLY EXIT (sell before settlement so capital compounds intra-window)
# DISABLED for final-minute taker strategy: hold to BRTI settlement.
# The 60-second BRTI average is the alpha source; exiting early loses it.
# ═════════════════════════════════════════════════════════════════════
EXIT_ENABLED = _env_bool("EXIT_ENABLED", False)
EXIT_TAKE_PROFIT_CENTS = _env_int("EXIT_TAKE_PROFIT_CENTS", 20)  # bid moved +20c vs entry
EXIT_STOP_LOSS_CENTS = _env_int("EXIT_STOP_LOSS_CENTS", 8)      # bid moved -8c vs entry
EXIT_MIN_MINUTES_LEFT = _env_float("EXIT_MIN_MINUTES_LEFT", 1.0)  # don't dump into a dead book

# ═════════════════════════════════════════════════════════════════════
# POSITION SIZING (Kelly criterion from kalshi_codex_bot momentum backtests)
# Best: MOM_88-93_dynTrue_netTrue_kelly2 → Kelly=0.10, Sharpe 6.60
# Survival-first: max 3 contracts, 1 per $10 cash (avoids wiping account)
# ═════════════════════════════════════════════════════════════════════
MAX_TRADE_DOLLARS = _env_float("MAX_TRADE_DOLLARS", 5.00)
MAX_CONTRACTS_CAP = _env_int("MAX_CONTRACTS_CAP", 3)
CONTRACTS_PER_BALANCE_UNIT = _env_float("CONTRACTS_PER_BALANCE_UNIT", 10.0)
# 2, not 3+: BTC/ETH/SOL/XRP/DOGE move together, so concurrent 15M positions are
# one correlated crypto bet, not diversification.
MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 2)
MAX_ORDERS_PER_CYCLE = _env_int("MAX_ORDERS_PER_CYCLE", 2)
MIN_ORDER_GAP_SECONDS = _env_float("MIN_ORDER_GAP_SECONDS", 10.0)
MAX_ORDERS_PER_DAY = _env_int("MAX_ORDERS_PER_DAY", 50)
BALANCE_PCT_PER_TRADE = _env_float("BALANCE_PCT_PER_TRADE", 0.08)  # 8% per trade - backtest optimal
WINDOW_TRADE_PCT = _env_float("WINDOW_TRADE_PCT", 1.00)
CASH_RESERVED_PCT = _env_float("CASH_RESERVED_PCT", 0.02)
TOTAL_CASH_TRADABLE_PCT = _env_float("TOTAL_CASH_TRADABLE_PCT", 0.70)

# ═════════════════════════════════════════════════════════════════════
# RISK LIMITS
# ═════════════════════════════════════════════════════════════════════
CASH_FLOOR_PCT = _env_float("CASH_FLOOR_PCT", 0.70)
EXPOSURE_CEILING_PCT = _env_float("EXPOSURE_CEILING_PCT", 0.30)
PER_TRADE_PCT = _env_float("PER_TRADE_PCT", 0.05)  # quarter-Kelly-ish, up to 5%
MIN_CASH_ABSOLUTE = _env_float("MIN_CASH_ABSOLUTE", 1.00)

# Paper-mode sizing (dry runs only). Dry runs deploy the whole paper balance so
# a favourite at 75-95c can actually be bought: with a 70% cash floor and a
# $1.00 absolute floor, a $1.22 bankroll can never afford one 90c contract and
# no trade — and no compounding — ever happens. Live keeps the conservative
# knobs above.
# Paper runs use the same shape as live but sized so one bad tail cannot take
# the bankroll: 15% per trade, 45% total exposure, 55% never at risk. The old
# 0.95/0.95/0.02 profile put ~95% of cash in a single trade, so one loss was a
# ~95% drawdown — which is what the dry-run curve was actually measuring.
# One contract at the cheap end of the 15-55c entry band is ~12% of a $1.22
# bankroll, so below that the bot simply cannot trade — the granularity, not
# the risk appetite, sets the floor here. Kelly sizing on a near-fair binary
# is 2-5% per position, so 12% is the maximum this bankroll can express.
PAPER_CASH_FLOOR_PCT = _env_float("PAPER_CASH_FLOOR_PCT", 0.70)
PAPER_EXPOSURE_CEILING_PCT = _env_float("PAPER_EXPOSURE_CEILING_PCT", 0.30)
PAPER_PER_TRADE_PCT = _env_float("PAPER_PER_TRADE_PCT", 0.12)
PAPER_MIN_CASH_ABSOLUTE = _env_float("PAPER_MIN_CASH_ABSOLUTE", 0.02)
DRAWDOWN_HALT_PCT = _env_float("DRAWDOWN_HALT_PCT", 0.08)
MAX_DRAWDOWN_PCT = _env_float("MAX_DRAWDOWN_PCT", 0.12)
STALE_ORDER_MINS = _env_float("STALE_ORDER_MINS", 5.0)
ADVERSE_MOVE_CENTS = _env_int("ADVERSE_MOVE_CENTS", 10)
MARKET_DAILY_LOSS_PCT = _env_float("MARKET_DAILY_LOSS_PCT", 0.10)

# ═════════════════════════════════════════════════════════════════════
# STABILITY CHECK (from kalshi_live_bot — doubles EV per backtests)
# ═════════════════════════════════════════════════════════════════════
STABILITY_ENABLED = _env_bool("STABILITY_ENABLED", True)
STABILITY_LOOKBACK_MIN = _env_int("STABILITY_LOOKBACK_MIN", 3)
STABILITY_MIN_COVERAGE_MIN = _env_float("STABILITY_MIN_COVERAGE_MIN", 2.5)
STABILITY_MAX_YES_BID_CENTS = _env_int("STABILITY_MAX_YES_BID_CENTS", 8)
# Fail-open on fresh start (no bid history) — set false to require history before trading
STABILITY_FAIL_OPEN = _env_bool("STABILITY_FAIL_OPEN", True)
HEDGE_STABILITY_ENABLED = _env_bool("HEDGE_STABILITY_ENABLED", True)

# ═════════════════════════════════════════════════════════════════════
# CONFIDENCE SCORING (from kalshi_live_bot)
# ═════════════════════════════════════════════════════════════════════
MIN_CONFIDENCE = _env_float("MIN_CONFIDENCE", 0.4)
# Strength is in cents (typically 1-50 for skew/trend). Scale should match
# the actual range so strength_score reaches 1.0 at typical max strength.
CONFIDENCE_STRENGTH_SCALE = _env_float("CONFIDENCE_STRENGTH_SCALE", 50.0)
CONFIDENCE_MID_WEIGHT = _env_float("CONFIDENCE_MID_WEIGHT", 0.4)
CONFIDENCE_STRENGTH_WEIGHT = _env_float("CONFIDENCE_STRENGTH_WEIGHT", 0.6)

# ═════════════════════════════════════════════════════════════════════
# LOOP
# ═════════════════════════════════════════════════════════════════════
LOOP_INTERVAL_SECONDS = max(5, _env_int("LOOP_INTERVAL_SECONDS", 15))

# ═════════════════════════════════════════════════════════════════════
# DRY RUN — defaults to True for safety. Must explicitly set DRY_RUN=false
# in .env or pass --live to trade real money.
# WARNING: the local .env file overrides this safe default. If .env sets
# DRY_RUN=false, the bot will trade with real money on startup. Always
# verify DRY_RUN before running from a directory whose .env you did not write.
# ══════════════════════════════════════════════════════════════════════
DRY_RUN = _env_bool("DRY_RUN", True)
DRY_STARTING_BALANCE = _env_float("DRY_STARTING_BALANCE", 100.0)
DRY_TARGET_PROFIT_CENTS = _env_int("DRY_TARGET_PROFIT_CENTS", 4)
DRY_MAX_PROFIT_CENTS = _env_int("DRY_MAX_PROFIT_CENTS", 4)
DRY_STRENGTH_SCALE_CENTS = _env_int("DRY_STRENGTH_SCALE_CENTS", 10)
DRY_EXIT_DELAY_CYCLES = _env_int("DRY_EXIT_DELAY_CYCLES", 0)

# ═════════════════════════════════════════════════════════════════════
# RESILIENCE (from kalshi_live_bot)
# ═════════════════════════════════════════════════════════════════════
ERROR_BACKOFF_SECONDS = _env_int("ERROR_BACKOFF_SECONDS", 5)
ERROR_BACKOFF_MAX_SECONDS = _env_int("ERROR_BACKOFF_MAX_SECONDS", 120)
CLIENT_REBUILD_THRESHOLD = _env_int("CLIENT_REBUILD_THRESHOLD", 5)
REQUEST_TIMEOUT_SECONDS = _env_int("REQUEST_TIMEOUT_SECONDS", 10)
# Kalshi allows ~10 req/s; the old 1.0s gap serialised discovery into 18s per
# cycle (longer than the 15s loop interval). 0.2s = 5 req/s with headroom, and
# 429s are still retried with backoff.
REQUEST_MIN_GAP_SECONDS = _env_float("REQUEST_MIN_GAP_SECONDS", 0.2)
KALSHI_POOL_MAXSIZE = _env_int("KALSHI_POOL_MAXSIZE", 20)
CYCLE_TIMEOUT_SECONDS = _env_int("CYCLE_TIMEOUT_SECONDS", 30)
CLIENT_MAX_REBUILDS = _env_int("CLIENT_MAX_REBUILDS", 20)

# ═════════════════════════════════════════════════════════════════════
# LLM (optional, NVIDIA-only via integrate.api.nvidia.com)
# ═════════════════════════════════════════════════════════════════════
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3.5-bolt-e4b")
LLM_EVALUATION_ENABLED = _env_bool("LLM_EVALUATION_ENABLED", True)
LLM_EVAL_MIN_PROBABILITY = _env_float("LLM_EVAL_MIN_PROBABILITY", 0.65)
LLM_EVAL_MIN_CONFIDENCE = _env_float("LLM_EVAL_MIN_CONFIDENCE", 0.45)
# Fail OPEN by default: the LLM is an optional extra check, so a provider
# outage must not silently stop the bot from trading. Fail-closed turned a
# single unreachable endpoint into "no orders ever placed".
LLM_EVAL_FAIL_OPEN = _env_bool("LLM_EVAL_FAIL_OPEN", True)
# Off by default. Every verdict is still requested, logged and stored, but the
# LLM does not veto orders unless this is turned on. Evidence for an LLM filter
# helping short-horizon binaries is absent (the one published test found the
# model just re-picked the favourite and lost after fees), so enforcement is an
# explicit opt-in rather than the default.
LLM_EVAL_ENFORCE = _env_bool("LLM_EVAL_ENFORCE", False)
# Deadline for the full LLM evaluation so a slow/hung LLM can't
# blow the cycle timeout. Must be < CYCLE_TIMEOUT_SECONDS.
LLM_EVAL_DEADLINE_SECONDS = _env_float("LLM_EVAL_DEADLINE_SECONDS", 25.0)
# LLM rate limiting
LLM_REQUEST_TIMEOUT = _env_int("LLM_REQUEST_TIMEOUT", 20)
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", 2)
LLM_BACKOFF_BASE = _env_float("LLM_BACKOFF_BASE", 2.0)
LLM_MIN_REQUEST_GAP = _env_float("LLM_MIN_REQUEST_GAP", 3.0)  # seconds between LLM requests

# ═════════════════════════════════════════════════════════════════════
# N8N
# ═════════════════════════════════════════════════════════════════════
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
N8N_BASE_URL = os.environ.get("N8N_BASE_URL", "http://127.0.0.1:5678")

# EXCHANGE / ORACLE APIS (cross-reference price feeds)
# Public endpoints — no auth required. Used to compare Kalshi prediction
# market prices against underlying spot/oracle prices for arbitrage signals.
BINANCE_ENABLED = _env_bool("BINANCE_ENABLED", False)
COINBASE_ENABLED = _env_bool("COINBASE_ENABLED", False)
REDSTONE_ENABLED = _env_bool("REDSTONE_ENABLED", False)
EXCHANGE_PRICE_CACHE_SECONDS = _env_int("EXCHANGE_PRICE_CACHE_SECONDS", 30)

# ═════════════════════════════════════════════════════════════════════
# LOGGING / VERSION
# ═════════════════════════════════════════════════════════════════════
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_FILE = Path(__file__).resolve().parent / "bot.log"
__version__ = "2.0.0-superbot"


def validate_credentials():
    """Validate that Kalshi credentials are present and non-empty.

    Raises ValueError with actionable instructions if KALSHI_KEY_ID is missing
    or no private key source (KALSHI_PRIVATE_KEY or KALSHI_KEY_FILE) is set.
    Intended to be called by trader.py at startup — not automatically on import.
    """
    errors = []

    if not KALSHI_KEY_ID:
        errors.append(
            "KALSHI_KEY_ID is empty. Set it in your environment or .env file:\n"
            "  export KALSHI_KEY_ID='your_key_id_here'\n"
            "  (Get your key ID from the Kalshi dashboard → API Keys)"
        )

    if not KALSHI_PRIVATE_KEY_CONTENT and not KALSHI_KEY_FILE:
        errors.append(
            "No Kalshi private key found. Provide one of:\n"
            "  KALSHI_PRIVATE_KEY — paste the full PEM key content directly:\n"
            "    export KALSHI_PRIVATE_KEY='-----BEGIN RSA PRIVATE KEY-----...'\n"
            "  KALSHI_KEY_FILE — path to a PEM key file:\n"
            "    export KALSHI_KEY_FILE='/path/to/kalshi_key.pem'\n"
            "  (Download your private key from the Kalshi dashboard → API Keys)"
        )

    if KALSHI_KEY_FILE and not Path(KALSHI_KEY_FILE).exists():
        errors.append(
            f"KALSHI_KEY_FILE is set to '{KALSHI_KEY_FILE}' but the file does not exist.\n"
            "  Check the path and ensure the file is present, or unset KALSHI_KEY_FILE\n"
            "  and use KALSHI_PRIVATE_KEY instead:\n"
            "    export KALSHI_KEY_FILE=''\n"
            "    export KALSHI_PRIVATE_KEY='-----BEGIN RSA PRIVATE KEY-----...'"
        )

    if errors:
        raise ValueError(
            "Kalshi credentials are missing or incomplete.\n\n" + "\n".join(errors)
        )


def validate_dry_run(live_explicit: bool = False) -> bool:
    """Block live trading unless --live is explicitly passed AND KALSHI_CONFIRM_LIVE=true.

    Returns True if dry-run mode is OK to proceed, False if live trading is
    blocked and the caller should exit.

    Intended to be called by trader.py at startup — not automatically on import.
    """
    if not DRY_RUN and not live_explicit:
        import logging
        logging.getLogger(__name__).error(
            "DRY_RUN is False but --live was not passed. "
            "Refusing to start in live mode. "
            "Set DRY_RUN=true in .env, or pass --live to confirm live trading."
        )
        return False

    if live_explicit and os.environ.get("KALSHI_CONFIRM_LIVE", "").strip().lower() != "true":
        import logging
        logging.getLogger(__name__).error(
            "--live passed but KALSHI_CONFIRM_LIVE is not set to 'true'. "
            "Refusing to start in live mode. "
            "Set KALSHI_CONFIRM_LIVE=true to confirm you intend live trading."
        )
        return False

    return True


def validate_bands():
    """Validate entry band configuration invariants.

    Raises ValueError if ENTRY_BAND_MIN > ENTRY_BAND_MAX or
    MIN_NO_ENTRY_CENTS > MAX_NO_ENTRY_CENTS, which would cause the entry band
    check in signals.py to reject all prices.

    Intended to be called by trader.py at startup alongside validate_credentials().
    """
    errors = []
    if MIN_YES_PRICE_CENTS > MAX_YES_PRICE_CENTS:
        errors.append(
            f"MIN_YES_PRICE_CENTS ({MIN_YES_PRICE_CENTS}) > MAX_YES_PRICE_CENTS ({MAX_YES_PRICE_CENTS}). "
            f"All prices will be rejected by the YES price band check. "
            f"Set MIN_YES_PRICE_CENTS <= MAX_YES_PRICE_CENTS."
        )
    if ENTRY_BAND_MIN > ENTRY_BAND_MAX:
        errors.append(
            f"ENTRY_BAND_MIN ({ENTRY_BAND_MIN}) > ENTRY_BAND_MAX ({ENTRY_BAND_MAX}). "
            f"All prices will be rejected by the entry band check. "
            f"Set ENTRY_BAND_MIN <= ENTRY_BAND_MAX."
        )
    if MIN_NO_ENTRY_CENTS > MAX_NO_ENTRY_CENTS:
        errors.append(
            f"MIN_NO_ENTRY_CENTS ({MIN_NO_ENTRY_CENTS}) > "
            f"MAX_NO_ENTRY_CENTS ({MAX_NO_ENTRY_CENTS}). "
            f"Set MIN_NO_ENTRY_CENTS <= MAX_NO_ENTRY_CENTS."
        )
    # Note: MIN_SKEW_CENTS and MIN_TREND_CENTS can be independently set.
    # Skew = yes_bid - no_bid (spread between sides)
    # Trend = yes_bid - 50 (distance from midpoint)
    # Both can be satisfied simultaneously (e.g., yes_bid=88, no_bid=12 gives skew=76, trend=38)
    if ENTRY_WINDOW_START_MIN >= ENTRY_WINDOW_END_MIN:
        errors.append(
            f"ENTRY_WINDOW_START_MIN ({ENTRY_WINDOW_START_MIN}) >= "
            f"ENTRY_WINDOW_END_MIN ({ENTRY_WINDOW_END_MIN}). "
            f"Empty entry window — no time to trade. "
            f"Set ENTRY_WINDOW_START_MIN < ENTRY_WINDOW_END_MIN."
        )
    # Risk config: cash floor + exposure ceiling must not exceed 1.0
    _SUM_EPSILON = 1e-9
    if CASH_FLOOR_PCT + EXPOSURE_CEILING_PCT > 1.0 + _SUM_EPSILON:
        errors.append(
            f"CASH_FLOOR_PCT ({CASH_FLOOR_PCT:.2f}) + EXPOSURE_CEILING_PCT ({EXPOSURE_CEILING_PCT:.2f}) > 1.0. "
            f"Sum must be <= 1.0 to avoid over-reserving cash."
        )
    if PAPER_CASH_FLOOR_PCT + PAPER_EXPOSURE_CEILING_PCT > 1.0 + _SUM_EPSILON:
        errors.append(
            f"PAPER_CASH_FLOOR_PCT ({PAPER_CASH_FLOOR_PCT:.2f}) + PAPER_EXPOSURE_CEILING_PCT ({PAPER_EXPOSURE_CEILING_PCT:.2f}) > 1.0. "
            f"Sum must be <= 1.0 to avoid over-reserving cash."
        )
    # Pre-entry gates: a non-positive hurdle or lookback below 2 makes every
    # candidate fail closed (or makes ATR undefined), which looks like a dead bot.
    for asset, hurdle in (("BTC", GATES_ATR_HURDLE_BTC),
                          ("ETH", GATES_ATR_HURDLE_ETH),
                          ("SOL", GATES_ATR_HURDLE_SOL),
                          ("XRP", GATES_ATR_HURDLE_XRP),
                          ("DOGE", GATES_ATR_HURDLE_DOGE),
                          ("BNB", GATES_ATR_HURDLE_BNB),
                          ("HYPE", GATES_ATR_HURDLE_HYPE),
                          ("ZEC", GATES_ATR_HURDLE_ZEC),
                          ("NEAR", GATES_ATR_HURDLE_NEAR)):
        if hurdle <= 0:
            errors.append(
                f"GATES_ATR_HURDLE_{asset} ({hurdle}) must be > 0."
            )
    if GATES_ATR_LOOKBACK < 2:
        errors.append(f"GATES_ATR_LOOKBACK ({GATES_ATR_LOOKBACK}) must be >= 2.")
    if GATES_MIN_EDGE < 0:
        errors.append(f"GATES_MIN_EDGE ({GATES_MIN_EDGE}) must be >= 0.")
    if GATES_HIGH_GAMMA_MINUTES < 0:
        errors.append(
            f"GATES_HIGH_GAMMA_MINUTES ({GATES_HIGH_GAMMA_MINUTES}) must be >= 0."
        )
    if errors:
        raise ValueError("Configuration invalid:\n" + "\n".join(errors))

# Validation is called explicitly by trader.py at startup.
# Do not run on import to allow graceful handling of config errors.
