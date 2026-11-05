"""
config.py — Kalshi SuperBot configuration.

Consolidated from kalshi_15m_bot, kalshi_live_bot, kalshi_gemini_bot,
kalshi_exp_bot, FlashBot.v6, Momentum.v15, kalshi_1230.

Consolidation strategy: take the best-performing parameters from each bot's
backtests, with environment-variable overrides for live tuning.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

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
ENTRY_WINDOW_START_MIN = _env_float("ENTRY_WINDOW_START_MIN", 6.0)
ENTRY_WINDOW_END_MIN = _env_float("ENTRY_WINDOW_END_MIN", 11.0)
MIN_YES_PRICE_CENTS = _env_int("MIN_YES_PRICE_CENTS", 1)
MAX_YES_PRICE_CENTS = _env_int("MAX_YES_PRICE_CENTS", 99)
MID_TARGET_CENTS = _env_int("MID_TARGET_CENTS", 3)
MID_TOLERANCE_CENTS = _env_int("MID_TOLERANCE_CENTS", 2)

# ═════════════════════════════════════════════════════════════════════
# ORDER BEHAVIOR (favourite maker - optimized from backtests)
# Backtest optimal: 15-55c entry band (wider = more trades, earlier window)
# ═════════════════════════════════════════════════════════════════════
AGGRESSIVE_FILL = _env_bool("AGGRESSIVE_FILL", False)
AGGRESSIVE_ENTRY_BUFFER_CENTS = _env_int("AGGRESSIVE_ENTRY_BUFFER_CENTS", 0)
ORDER_TIME_IN_FORCE = "good_till_canceled"
# Entry band: favourite maker enters at 15-55c range (backtest optimal)
ENTRY_BAND_MIN = _env_int("ENTRY_BAND_MIN", 15)
ENTRY_BAND_MAX = _env_int("ENTRY_BAND_MAX", 55)
MAX_NO_ENTRY_CENTS = _env_int("MAX_NO_ENTRY_CENTS", 55)
MIN_NO_ENTRY_CENTS = _env_int("MIN_NO_ENTRY_CENTS", 15)
ALLOW_YES_BUYS = _env_bool("ALLOW_YES_BUYS", True)

# NO *market data* format only: Kalshi returns NO prices directly (not
# complemented), so this stays False. It does NOT affect order pricing —
# KalshiClient.no_price_to_api_price always sends the YES-leg complement
# (buy NO at 85c -> ask YES at 15c) for both buys and sells.
NO_PRICE_IS_COMPLEMENT = _env_bool("NO_PRICE_IS_COMPLEMENT", False)

# ═════════════════════════════════════════════════════════════════════
# HEDGE SETTINGS
# ═════════════════════════════════════════════════════════════════════
HEDGE_YES_ASK_MAX = _env_int("HEDGE_YES_ASK_MAX", 12)
MAX_HEDGE_PER_SERIES = _env_int("MAX_HEDGE_PER_SERIES", 0)

# Hedge entry band: when YES is cheap (≤HEDGE_YES_ASK_MAX), NO is expensive (≥88c).
# Separate from favourite-maker band so both can coexist.
HEDGE_MIN_NO_ENTRY_CENTS = _env_int("HEDGE_MIN_NO_ENTRY_CENTS", 82)
HEDGE_MAX_NO_ENTRY_CENTS = _env_int("HEDGE_MAX_NO_ENTRY_CENTS", 88)

# Latency-arb taker strategy. OFF by default: the edge model is unvalidated for
# live fills (the 2s edge decay is only ~33% survivable per FINDINGS), so it
# should not place real-money taker orders until it has a passing backtest.
LATENCY_ARB_ENABLED = _env_bool("LATENCY_ARB_ENABLED", False)

# Panic fade (mean reversion) strategy. Bet opposite side when price moves
# sharply in short window — TurbineFi research found this was the ONLY
# consistently profitable strategy group (93/96 variants, mean +4.90% ROI).
PANIC_FADE_ENABLED = _env_bool("PANIC_FADE_ENABLED", True)
PANIC_FADE_MOVE_CENTS = _env_int("PANIC_FADE_MOVE_CENTS", 15)  # min cents move to trigger
PANIC_FADE_WINDOW_SECONDS = _env_int("PANIC_FADE_WINDOW_SECONDS", 120)  # lookback window
# Uses ENTRY_BAND_MIN/MAX (15-55c) for fade entries

# ═════════════════════════════════════════════════════════════════════
# EARLY EXIT (sell before settlement so capital compounds intra-window)
# Without this every position is held to expiry and cash is locked for the
# rest of the 15m window. Exit at the current bid (taker) when either the
# profit target or the stop is hit.
# ═════════════════════════════════════════════════════════════════════
EXIT_ENABLED = _env_bool("EXIT_ENABLED", True)
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
POSITION_FRACTION = _env_float("POSITION_FRACTION", 0.30)
WINDOW_TRADE_PCT = _env_float("WINDOW_TRADE_PCT", 1.00)
CASH_RESERVED_PCT = _env_float("CASH_RESERVED_PCT", 0.02)
TOTAL_CASH_TRADABLE_PCT = _env_float("TOTAL_CASH_TRADABLE_PCT", 0.70)

# ═════════════════════════════════════════════════════════════════════
# RISK LIMITS
# ═════════════════════════════════════════════════════════════════════
CASH_FLOOR_PCT = _env_float("CASH_FLOOR_PCT", 0.70)
EXPOSURE_CEILING_PCT = _env_float("EXPOSURE_CEILING_PCT", 0.30)
# Small epsilon for floating-point comparison: 0.70 + 0.30 == 1.0000000000000001
_SUM_EPSILON = 1e-9
if CASH_FLOOR_PCT + EXPOSURE_CEILING_PCT > 1.0 + _SUM_EPSILON:
    raise ValueError(
        f"CASH_FLOOR_PCT ({CASH_FLOOR_PCT:.2f}) + EXPOSURE_CEILING_PCT ({EXPOSURE_CEILING_PCT:.2f}) > 1.0. "
        f"Sum must be <= 1.0 to avoid over-reserving cash."
    )
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

# ═════════════════════════════════════════════════════════════════════
# CONFIDENCE SCORING (from kalshi_live_bot)
# ═════════════════════════════════════════════════════════════════════
MIN_CONFIDENCE = _env_float("MIN_CONFIDENCE", 0.4)
CONFIDENCE_STRENGTH_SCALE = _env_float("CONFIDENCE_STRENGTH_SCALE", 6.0)
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
DRY_STARTING_BALANCE = _env_float("DRY_STARTING_BALANCE", 0.0)
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
REQUEST_MIN_GAP_SECONDS = _env_float("REQUEST_MIN_GAP_SECONDS", 1.0)
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
    if MIN_SKEW_CENTS > MIN_TREND_CENTS:
        errors.append(
            f"MIN_SKEW_CENTS ({MIN_SKEW_CENTS}) > MIN_TREND_CENTS ({MIN_TREND_CENTS}). "
            f"Contradictory signal requirements — no price can satisfy both. "
            f"Set MIN_SKEW_CENTS <= MIN_TREND_CENTS."
        )
    if ENTRY_WINDOW_START_MIN >= ENTRY_WINDOW_END_MIN:
        errors.append(
            f"ENTRY_WINDOW_START_MIN ({ENTRY_WINDOW_START_MIN}) >= "
            f"ENTRY_WINDOW_END_MIN ({ENTRY_WINDOW_END_MIN}). "
            f"Empty entry window — no time to trade. "
            f"Set ENTRY_WINDOW_START_MIN < ENTRY_WINDOW_END_MIN."
        )
    if errors:
        raise ValueError("Entry band configuration invalid:\n" + "\n".join(errors))
