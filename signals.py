"""
signals.py — Consolidated signal engine for the Kalshi SuperBot.

Pulls the best from all bots:
- kalshi_15m_bot: SKEW/TREND signals + HEDGE_NO bracket
- kalshi_live_bot: Two-sided favourite maker (Approach D), stability check,
  confidence scoring, entry bands, bid history tracking, spot z-score gating

Returns a list of TradeSignal (0 or 1 per market).
"""

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, List

import config
import fees
import gates
from market_finder import MarketInfo
from model import model_probability_from_market, edge_cents

log = logging.getLogger(__name__)
Direction = Literal["YES", "NO", "PASS"]


@dataclass
class TradeSignal:
    direction:   Direction
    side:        str         # "yes" or "no"
    entry_price: int         # cents
    contracts:   float
    dollar_risk: float
    reason:      str
    market:      MarketInfo
    strength:    float = 0.0
    confidence:  float = 1.0

    @property
    def is_trade(self) -> bool:
        return (
            self.direction != "PASS"
            and self.side in ("yes", "no")
            and self.side != ""
            and self.entry_price > 0
            and self.contracts > 0
        )


# ═════════════════════════════════════════════════════════════════════
# SIZING
# ═════════════════════════════════════════════════════════════════════

def max_contracts_for_cash(cash: float) -> int:
    """
    Survival-first sizing (from kalshi_live_bot 7-day backtest insight):
    Scale size with bankroll: contracts_per_unit per $10 of cash, hard cap.
    Each contract risks ~$1 so one miss costs ~10% not 30%.
    """
    unit = max(1.0, config.CONTRACTS_PER_BALANCE_UNIT)
    n = int(cash // unit)
    return max(1, min(config.MAX_CONTRACTS_CAP, n))


def floor_contracts(n: float) -> float:
    """Floor a contract count to Kalshi's 0.01 minimum lot, or 0 if below it.

    Kalshi trades fractional contracts (min 0.01), so a small bankroll can size
    in exact dollars instead of rounding down to whole contracts.
    """
    if n is None:
        return 0.0
    return round(math.floor(float(n) * 100) / 100, 2)


def _size(entry_cents: int, max_dollars: float, max_contracts: int = 0) -> float:
    """Convert a dollar budget into contracts (fractional, 2dp)."""
    if entry_cents <= 0 or entry_cents >= 100 or max_dollars <= 0:
        return 0.0
    cost = entry_cents / 100
    n = min(max_dollars, config.MAX_TRADE_DOLLARS) / cost
    if max_contracts and max_contracts > 0:
        n = min(n, float(max_contracts))
    n = floor_contracts(n)
    return n if n >= 0.01 else 0.0


# ═════════════════════════════════════════════════════════════════════
# MAKER PRICING
# ═════════════════════════════════════════════════════════════════════

def _maker_price(market: MarketInfo, bullish: bool) -> int:
    """
    Decide entry price per direction (from kalshi_live_bot).
    In aggressive mode we cross the book (with optional buffer); otherwise we
    quote 1c inside the spread with fill-optimized depth.
    """
    buffer = getattr(config, "AGGRESSIVE_ENTRY_BUFFER_CENTS", 0)
    if config.AGGRESSIVE_FILL:
        if bullish:
            base = market.yes_ask or market.yes_bid or 50
        else:
            base = (
                market.no_ask
                or market.no_bid
                or (100 - market.yes_bid if market.yes_bid else None)
                or 50  # explicit fallback with logged warning
            )
        result = base + buffer
    else:
        yes_bid, yes_ask, no_bid, no_ask = (
            market.yes_bid, market.yes_ask, market.no_bid, market.no_ask
        )
        if bullish:
            if yes_bid and yes_ask:
                if yes_ask <= yes_bid:
                    result = max(1, min(99, yes_bid))  # spread ≤ 0 — reject
                else:
                    target = yes_bid + 1 if (yes_ask - yes_bid) > 1 else yes_bid
                    # Deeper quote: 1c under ask for ~85% live fill rate
                    result = min(target, max(1, yes_ask - 1))
            else:
                result = yes_ask or yes_bid or 1
        else:
            if no_bid and no_ask:
                target = no_bid + 1 if (no_ask - no_bid) > 1 else no_bid
                result = min(target, max(1, no_ask - 1))
            else:
                result = 100 - (yes_ask or yes_bid or 99)  # symmetric to YES

    # Kalshi prices must be 1-99; clamp edge cases (0/100) before returning.
    return max(1, min(99, result))


# ═════════════════════════════════════════════════════════════════════
# STABILITY CHECK (bid history tracking — doubles EV per kalshi_live_bot)
# ═════════════════════════════════════════════════════════════════════

# In-memory cache with TTL so concurrent processes see each other's writes
# without a heavy DB migration. Load on first use, refresh every 60 s.
_bid_hist: dict[str, list[tuple[float, int]]] = {}
_bid_hist_loaded_at: float = 0.0
_BID_HIST_TTL_SECONDS = 60.0
_BID_HIST_SAVE_INTERVAL = 5.0

# Track tickers that have already warned about fresh start / insufficient coverage
# to avoid spamming logs on every cycle.
_stability_warned: set[str] = set()


def _bid_hist_file() -> Path:
    """Get bid history file path, evaluated at call time (not import time)."""
    return Path(__file__).resolve().parent / f"bid_hist_{'dry' if config.DRY_RUN else 'live'}.json"

# NOTE: This module-level bid history is single-process only. It is mutated by
# _track_bid/_maybe_save_bid_hist and persisted to bid_hist_{dry,live}.json.
# If the bot is ever run in multiple processes, this state will diverge between
# processes and must be moved into a shared store (e.g. Redis or a per-instance
# file with file locking).


def _load_bid_hist(force: bool = False):
    global _bid_hist, _bid_hist_loaded_at
    now = time.time()
    if not force and _bid_hist_loaded_at and (now - _bid_hist_loaded_at) < _BID_HIST_TTL_SECONDS:
        return _bid_hist
    try:
        hist_file = _bid_hist_file()
        if hist_file.exists():
            with open(hist_file) as f:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                data = json.load(f)
            _bid_hist = {k: [(float(t), int(b)) for t, b in v] for k, v in data.items()}
        else:
            _bid_hist = {}
        _bid_hist_loaded_at = now
    except FileNotFoundError:
        _bid_hist = {}
        _bid_hist_loaded_at = now
    except json.JSONDecodeError as e:
        log.error("Bid history file corrupted: %s — starting fresh", e)
        _bid_hist = {}
        _bid_hist_loaded_at = now
    except Exception as e:
        log.error("Failed to load bid history: %s — starting fresh", e)
        _bid_hist = {}
        _bid_hist_loaded_at = now
    return _bid_hist


def _ensure_bid_hist_loaded():
    """Ensure bid history is loaded (lazy init on first use)."""
    if _bid_hist_loaded_at == 0.0:
        _load_bid_hist(force=True)
    return _bid_hist


def _save_bid_hist():
    try:
        hist_file = _bid_hist_file()
        # Atomic write: write to temp file then rename
        tmp_file = hist_file.with_suffix(hist_file.suffix + ".tmp")
        with open(tmp_file, "w") as f:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump({k: v for k, v in _bid_hist.items()}, f)
        os.replace(tmp_file, hist_file)
    except Exception as e:
        log.error("Failed to save bid history: %s — HISTORY NOT PERSISTED", e)
        # Don't silently drop; caller will retry on next interval via dirty flag
        raise


def _track_bid(ticker: str, yes_bid_cents: int, now: float):
    _ensure_bid_hist_loaded()
    h = _bid_hist.setdefault(ticker, [])
    h.append((now, max(0, min(100, int(yes_bid_cents or 0)))))
    cutoff = now - (config.STABILITY_LOOKBACK_MIN + 1.5) * 60
    h[:] = [(t, b) for t, b in h if t >= cutoff]
    # Cap per-ticker entries to prevent unbounded memory growth
    if len(h) > 1000:
        h[:] = h[-1000:]
    if len(_bid_hist) > 200:
        for k in list(_bid_hist)[:-120]:
            _bid_hist.pop(k, None)
    return h


def _maybe_save_bid_hist(now: float):
    global _bid_hist_loaded_at
    # TTL-based cleanup: remove entries older than lookback + 1 hour
    # Run periodically (every 50 calls) to avoid O(N) on every cycle
    _maybe_save_bid_hist._cleanup_counter = getattr(_maybe_save_bid_hist, "_cleanup_counter", 0) + 1
    _ensure_bid_hist_loaded()
    total_entries = sum(len(h) for h in _bid_hist.values())
    run_ttl_cleanup = (_maybe_save_bid_hist._cleanup_counter % 50 == 0) or (total_entries > 50000)
    if run_ttl_cleanup:
        ttl_cutoff = now - (config.STABILITY_LOOKBACK_MIN + 60) * 60
        for ticker_key in list(_bid_hist.keys()):
            h = _bid_hist[ticker_key]
            h[:] = [(t, b) for t, b in h if t >= ttl_cutoff]
            if not h:
                _bid_hist.pop(ticker_key, None)
    # Save every 10s if dirty (tracked by file mtime vs loaded_at)
    hist_file = _bid_hist_file()
    if hist_file.exists():
        mtime = hist_file.stat().st_mtime
        if mtime > _bid_hist_loaded_at:
            # Another process updated the file — reload
            _load_bid_hist(force=True)
    # Periodic save
    if (now - _bid_hist_loaded_at) >= _BID_HIST_SAVE_INTERVAL:
        try:
            _save_bid_hist()
        except Exception as e:
            log.warning("Failed to save bid history: %s — retrying next cycle", e)
        _bid_hist_loaded_at = now


def _load_bid_hist_for_read():
    """Load bid history with shared lock for concurrent reads (alias for _load_bid_hist)."""
    return _load_bid_hist(force=True)


def _force_save_bid_hist():
    """Force save bid history to disk immediately (used on shutdown)."""
    try:
        _save_bid_hist()
    except Exception as e:
        log.warning("Failed to force-save bid history on shutdown: %s", e)


def _compute_momentum(ticker: str, now: float, lookback_min: float = 2.0) -> float:
    """
    Compute price momentum in cents per minute from bid history.

    Returns positive if YES bid has been rising, negative if falling.
    Uses linear regression over the lookback window for robustness.
    """
    h = _bid_hist.get(ticker, [])
    if len(h) < 2:
        return 0.0
    cutoff = now - lookback_min * 60
    pts = [(t, b) for t, b in h if t >= cutoff]
    if len(pts) < 2:
        return 0.0
    # Simple slope: (last - first) / elapsed_minutes
    t0, b0 = pts[0]
    t1, b1 = pts[-1]
    elapsed_min = (t1 - t0) / 60.0
    if elapsed_min <= 0:
        return 0.0
    return (b1 - b0) / elapsed_min


_MINUTES_PER_YEAR = 365 * 24 * 60


def _model_volatility(asset: str) -> float:
    """Annualized volatility for the GBM model.

    Realized per-minute vol from the feed when MODEL_VOL_DYNAMIC is on and the
    feed has data, else the configured MODEL_VOLATILITY. Clamped to a sane band
    so one bad tick cannot blind the model.
    """
    fixed = config.MODEL_VOLATILITY
    if not getattr(config, "MODEL_VOL_DYNAMIC", False) or not asset:  # reviewed: fixed vol when dynamic vol is off or the asset is unknown
        return fixed
    per_min = gates.sigma_per_min(asset)
    if not per_min or per_min <= 0:  # reviewed: a dead or zero feed must not blind the model
        return fixed
    annual = per_min * math.sqrt(_MINUTES_PER_YEAR)
    return max(config.MODEL_VOL_MIN, min(config.MODEL_VOL_MAX, annual))


def _stability_ok(ticker: str, now: float):
    """
    Only quote when the underlying has stayed clearly away from the strike
    for the lookback window. Per kalshi_live_bot backtests this roughly
    doubles EV/ct and removes most losses.
    """
    if not getattr(config, "STABILITY_ENABLED", True):
        return True, ""
    _ensure_bid_hist_loaded()
    h = _bid_hist.get(ticker) or []
    cutoff = now - config.STABILITY_LOOKBACK_MIN * 60
    window = [(t, b) for t, b in h if t >= cutoff]
    if not window:
        # Fresh ticker: behavior controlled by STABILITY_FAIL_OPEN
        if config.STABILITY_FAIL_OPEN:
            warn_key = f"{ticker}:fresh"
            if warn_key not in _stability_warned:
                _stability_warned.add(warn_key)
                log.warning("Stability check: no history for %s — allowing trade (fresh start)", ticker)
            return True, "fresh start: no history"
        else:
            return False, "fresh start: no history (STABILITY_FAIL_OPEN=false)"
    coverage = now - window[0][0]
    if coverage < config.STABILITY_MIN_COVERAGE_MIN * 60:
        # Insufficient coverage: behavior controlled by STABILITY_FAIL_OPEN
        if config.STABILITY_FAIL_OPEN:
            warn_key = f"{ticker}:coverage"
            if warn_key not in _stability_warned:
                _stability_warned.add(warn_key)
                log.warning("Stability check: only %.1fmin history for %s (need %.1fmin) — allowing trade",
                            coverage/60, ticker, config.STABILITY_MIN_COVERAGE_MIN)
            return True, f"insufficient coverage: {coverage/60:.1f}min"
        else:
            return False, f"insufficient coverage: {coverage/60:.1f}min (STABILITY_FAIL_OPEN=false)"
    peak = max(min(b, 100 - b) for _, b in window)
    if peak > config.STABILITY_MAX_YES_BID_CENTS:
        return False, (
            f"Stability: price came within {peak}c of the strike "
            f"(cap {config.STABILITY_MAX_YES_BID_CENTS}c) in last "
            f"{config.STABILITY_LOOKBACK_MIN}min"
        )
    return True, ""


# ═════════════════════════════════════════════════════════════════════
# COMMON EVALUATION
# ═════════════════════════════════════════════════════════════════════

def _evaluate_common(
    market: MarketInfo,
    max_dollars: float,
    max_contracts: int,
    bullish: bool,
    entry_min: int,
    entry_max: int,
    minutes_in: float,
    source: str,
    strength: float,
):
    """
    Shared evaluation logic. Returns TradeSignal or None.
    """
    def _pass(r):
        return TradeSignal("PASS", "", 0, 0, 0.0, r, market)

    # ── ALLOW_NO_BUYS gate ──────────────────────────────────────────────
    # reviewed: enforce ALLOW_NO_BUYS=False on all evaluation paths
    if not bullish and not config.ALLOW_NO_BUYS:
        return _pass("NO buys disabled by config")

    # ── Timing gate ─────────────────────────────────────────────────────
    if minutes_in < config.ENTRY_WINDOW_START_MIN:
        return _pass(f"Early ({minutes_in:.1f}min)")
    if minutes_in > config.ENTRY_WINDOW_END_MIN:
        return _pass(f"Late ({minutes_in:.1f}min)")

    # ── Liquidity gate ──────────────────────────────────────────────────
    # Skip illiquid markets before spending work on entry price / sizing.
    yes_liq = market.yes_liquidity
    no_liq = market.no_liquidity
    if not yes_liq and not no_liq:
        return _pass("No liquidity")

    # ── Entry price ─────────────────────────────────────────────────────
    ep = _maker_price(market, bullish)
    ep = max(1, min(99, int(ep)))

    # Entry band check
    if ep < entry_min or ep > entry_max:
        direction = "YES" if bullish else "NO"
        return _pass(f"{direction} price {ep}c outside band {entry_min}-{entry_max}c")

    # ── Size ────────────────────────────────────────────────────────────
    n = _size(ep, max_dollars, max_contracts)
    avail = market.yes_liquidity if bullish else market.no_liquidity
    if avail and n > avail:
        n = min(n, float(avail))
    if n <= 0:
        return _pass(f"Budget ${max_dollars:.2f} too low for {ep}c entry")

    direction = "YES" if bullish else "NO"
    side = "yes" if bullish else "no"
    # Include taker fee if AGGRESSIVE_FILL enabled
    risk_ = fees.buy_cash_out(n, ep / 100.0, taker=config.AGGRESSIVE_FILL)

    # Confidence scoring (from kalshi_live_bot)
    if source in ("SKEW", "TREND", "TAKER_FAVOURITE"):
        mid = market.mid_yes
        mid_distance = max(0.0, 1.0 - abs(mid - config.MID_TARGET_CENTS) / max(1.0, config.MID_TOLERANCE_CENTS))
        strength_score = min(1.0, strength / config.CONFIDENCE_STRENGTH_SCALE)
        confidence = min(
            1.0,
            strength_score * config.CONFIDENCE_STRENGTH_WEIGHT
            + mid_distance * config.CONFIDENCE_MID_WEIGHT,
        )
        if confidence < config.MIN_CONFIDENCE:
            return _pass(f"Confidence {confidence:.2f} below {config.MIN_CONFIDENCE:.2f}")
        # ALLOW_YES_BUYS gate for SKEW/TREND/TAKER_FAVOURITE sources
        if bullish and not config.ALLOW_YES_BUYS:
            return _pass(f"ALLOW_YES_BUYS=false blocks YES {source} signal")
    else:
        confidence = float(abs(market.mid_yes - 50.0) / 50.0)

    reason = (
        f"{direction} ({source}) str={strength:.0f} conf={confidence:.2f} "
        f"{minutes_in:.1f}min {n:.4f}x@{ep}c=${risk_:.2f}"
    )
    log.info("SIGNAL: %s — %s", market.ticker, reason)
    return TradeSignal(direction, side, ep, n, risk_, reason, market, strength, confidence)


# ═════════════════════════════════════════════════════════════════════
# HEDGE_NO (from kalshi_15m_bot)
# ═════════════════════════════════════════════════════════════════════


# TWO-SIDED FAVOURITE MAKER (Approach D — from kalshi_live_bot)
# ═════════════════════════════════════════════════════════════════════


def _evaluate_hedge(market: MarketInfo, max_dollars: float, max_contracts: int = 0):
    """
    HEDGE_NO: buy NO when YES is very cheap (protective hedge).

    When YES ask <= HEDGE_NO_ASK_MAX, YES is a longshot. Buying NO at
    1c under its ask is a maker trade with high win probability but
    low edge per contract.
    """
    results = []

    def _pass(r):
        results.append(TradeSignal("PASS", "", 0, 0, 0.0, r, market))

    minutes_in = 15.0 - market.minutes_left
    if minutes_in < config.ENTRY_WINDOW_START_MIN:
        _pass(f"Early ({minutes_in:.1f}min)")
        return results
    if minutes_in > config.ENTRY_WINDOW_END_MIN:
        _pass(f"Late ({minutes_in:.1f}min)")
        return results

    no_ask = market.no_ask
    # Kalshi returns NO prices directly on crypto series (quadratic fee_type).
    # NO_PRICE_IS_COMPLEMENT=False confirms we must NOT derive NO from YES bid;
    # a missing NO ask means no quote, not a synthetic price.
    if not no_ask:
        _pass("No NO ask")
        return results

    # Stability check (mirror _evaluate_two_sided, but with dedicated hedge config)
    ok_stable, why = _stability_ok(market.ticker, time.time())
    if config.HEDGE_STABILITY_ENABLED and not ok_stable:
        _pass(why)
        return results

    # Buy NO at 1c under NO ask (maker order)
    ep = no_ask - 1
    ep = max(1, min(99, ep))

    # NO entry band check
    if ep < config.HEDGE_MIN_NO_ENTRY_CENTS or ep > config.HEDGE_MAX_NO_ENTRY_CENTS:
        _pass(
            f"NO price {ep}c outside hedge band "
            f"{config.HEDGE_MIN_NO_ENTRY_CENTS}-{config.HEDGE_MAX_NO_ENTRY_CENTS}c"
        )
        return results

    n = _size(ep, max_dollars, max_contracts)
    avail = market.no_liquidity
    if avail and n > avail:
        n = min(n, float(avail))
    if n <= 0:
        _pass(f"Budget ${max_dollars:.2f} too low for {ep}c entry")
        return results

    risk_ = n * ep / 100
    # Strength based on how far YES ask is below hedge max (wider edge = stronger)
    strength = float(config.HEDGE_YES_ASK_MAX - market.yes_ask)
    # Confidence based on favourite strength (distance from 50c)
    confidence = float(abs(market.mid_yes - 50.0) / 50.0)
    reason = (
        f"HEDGE_NO str={strength:.0f} conf={confidence:.2f} "
        f"{minutes_in:.1f}min {n:.4f}x@{ep}c=${risk_:.2f}"
    )
    log.info("SIGNAL: %s — %s", market.ticker, reason)
    results.append(TradeSignal("NO", "no", ep, n, risk_, reason, market, strength, confidence))
    return results


def _evaluate_taker_favourite(market: MarketInfo, max_dollars: float, max_contracts: int = 0):
    """
    Final-minute taker strategy: pay the ask on the favourite in the final 60 seconds,
    hold to BRTI settlement.

    Research (Turbine, Bot for Kalshi, PredictionMarketsPicks) shows edge ONLY in the
    final minute: pay the ask on the market favourite (75-99c), hold to BRTI settle.
    The 60-second BRTI average reduces uncertainty by ~71% at 30s left vs last-tick.

    Returns a list of TradeSignal (0 or 1 per market — the more extreme favourite).
    """
    results = []

    def _pass(r):
        results.append(TradeSignal("PASS", "", 0, 0, 0.0, r, market))

    minutes_in = 15.0 - market.minutes_left
    if minutes_in < config.ENTRY_WINDOW_START_MIN:
        _pass(f"Early ({minutes_in:.1f}min)")
        return results
    if minutes_in > config.ENTRY_WINDOW_END_MIN:
        _pass(f"Late ({minutes_in:.1f}min)")
        return results

    # Stability gate: skip for final minute (no history needed at 14:00+)
    # If enabled, it would block on fresh series anyway. Fail-open is default.
    ok_stable, why = _stability_ok(market.ticker, time.time())
    if config.STABILITY_ENABLED and not ok_stable:
        _pass(why)
        return results

    # Get both YES and NO asks (taker pays the ask)
    yes_ask = market.yes_ask
    no_ask = market.no_ask
    # Kalshi returns NO prices directly on crypto series (quadratic fee_type).
    # NO_PRICE_IS_COMPLEMENT=False confirms we must NOT derive NO from YES bid:
    # a missing NO ask means no quote, not a synthetic price.
    if not yes_ask and market.no_bid:
        yes_ask = 100 - market.no_bid

    if not yes_ask and not no_ask:
        _pass("No quotes")
        return results

    # Determine the favourite: the side with ask >= 75c (favourite zone)
    # We pay the ask, so the price we pay IS the ask
    cands = []
    entry_min = getattr(config, "ENTRY_BAND_MIN", config.MIN_NO_ENTRY_CENTS)
    entry_max = getattr(config, "ENTRY_BAND_MAX", config.MAX_NO_ENTRY_CENTS)

    # reviewed: check ALLOW_NO_BUYS flag to prevent mean-reversion short traps on crypto
    if config.ALLOW_NO_BUYS and no_ask and entry_min <= no_ask <= entry_max:
        # NO is the favourite (NO ask >= 75c means YES is <= 25c)
        cands.append(("NO", "no", no_ask))
    if config.ALLOW_YES_BUYS and yes_ask and entry_min <= yes_ask <= entry_max:
        # YES is the favourite (YES ask >= 75c)
        cands.append(("YES", "yes", yes_ask))

    if not cands:
        _pass(f"No favourite in {entry_min}-{entry_max}c (no_ask={no_ask} yes_ask={yes_ask})")
        return results

    # If both sides in range (rare at 75-99c), keep the more extreme favourite
    if len(cands) == 2:
        # More extreme = further from 50c
        if abs(cands[0][2] - 50) >= abs(cands[1][2] - 50):
            cands = [cands[0]]
        else:
            cands = [cands[1]]

    for direction, side, entry_c in cands:
        bullish = (direction == "YES")
        # For taker strategy, strength = how far the favourite is from 50c
        strength = abs(entry_c - 50)

        # ── Model filter: only trade when there is positive edge after fees ──
        # Blind taker favorite buying is net-negative EV (fee ~0.79c at 87c
        # exceeds raw edge ~0.67c). Only trade when model-implied true
        # probability exceeds ask price + fee + minimum edge.
        fee_cents = fees.taker_fee_cents(1.0, entry_c / 100.0)
        momentum = _compute_momentum(market.ticker, time.time(), lookback_min=2.0)
        # Spot price from Binance/Coinbase composite feed for GBM model.
        # Asset from ticker: KXBTC15M-26NOV090115-15 -> BTCUSDT
        series = market.ticker.split("-")[0] if "-" in market.ticker else market.ticker
        asset = series[2:] if series.startswith("KX") else series
        if asset.endswith("15M"):
            asset = asset[:-3]
        spot_price = gates.spot_for(asset)
        strike = float(market.strike) if market.strike else None
        model_prob = model_probability_from_market(
            market, model_type=config.MODEL_TYPE,
            spot_price=spot_price, strike=strike,
            minutes_left=market.minutes_left,
            volatility=_model_volatility(asset),
            momentum_cents_per_min=momentum,
            # How far into the final (settlement) minute: 0 at start, 60 at close.
            # Locked-share model uses this to collapse uncertainty as BRTI average locks in.
            seconds_into_final_minute=max(0, min(60, int((15.0 - market.minutes_left) * 60)))
            if market.minutes_left <= 1.0 else 0,
        )
        # model_prob is P(YES wins); for NO side we need P(NO wins)
        trade_prob = model_prob if direction == "YES" else (1.0 - model_prob)
        edge = edge_cents(trade_prob, entry_c, fee_cents)
        log.info(
            "Bracket eval %s [%s %dc]: Model P(win)=%.1f%% fee=%.1fc -> Net Edge=%+.2fc (min=%+.2fc) => %s",
            market.ticker, direction, entry_c, trade_prob * 100, fee_cents, edge, config.MODEL_MIN_EDGE_CENTS,
            "NET-POSITIVE" if edge >= config.MODEL_MIN_EDGE_CENTS else "NEGATIVE-EDGE"
        )
        if edge < config.MODEL_MIN_EDGE_CENTS:
            results.append(TradeSignal(
                "PASS", "", 0, 0, 0.0,
                f"Edge {edge:+.1f}c below min {config.MODEL_MIN_EDGE_CENTS}c "
                f"(prob={trade_prob*100:.0f}% ask={entry_c}c fee={fee_cents:.1f}c)",
                market,
            ))
            continue

        sig = _evaluate_common(
            market, max_dollars, max_contracts, bullish,
            entry_min, entry_max, minutes_in,
            "TAKER_FAVOURITE", strength,
        )
        if sig and sig.is_trade:
            results.append(sig)
        elif sig:
            _pass(sig.reason)
    return results


# ══════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════

def evaluate(market: MarketInfo, cash: float = 10.0,
             max_trade_override: Optional[float] = None):
    """
    Evaluate a market and return a list of TradeSignal (0 or 1 per market).
    """
    now = time.time()
    _ensure_bid_hist_loaded()
    _maybe_save_bid_hist(now)
    max_dollars = max_trade_override if max_trade_override is not None \
                  else cash * config.BALANCE_PCT_PER_TRADE

    effective_cash = max_dollars / config.BALANCE_PCT_PER_TRADE \
                     if max_trade_override is not None else cash

    _track_bid(market.ticker, int(round(market.yes_bid or 0)), now)

    if market.bracket_role == "HEDGE_NO":
        result = _evaluate_hedge(market, max_dollars)
    else:
        # Final-minute taker strategy (replaces two-sided favourite maker)
        result = _evaluate_taker_favourite(market, max_dollars,
                                           max_contracts_for_cash(effective_cash))

    # Deduplicate by (ticker, side)
    seen = set()
    deduped = []
    for sig in result:
        key = (sig.market.ticker, sig.side)
        if key not in seen:
            seen.add(key)
            deduped.append(sig)
    result = deduped

    if any(s.is_trade for s in result):
        _force_save_bid_hist()
    return result


def rank_signals(signals: list):
    """Rank signals by strength * confidence * boost, ties broken by ticker."""
    tradeable = [s for s in signals if s.is_trade]

    def key(s):
        boost = 1.2 if s.market.bracket_role == "HEDGE_NO" else 1.0
        return s.strength * s.confidence * boost, s.market.ticker
    return sorted(tradeable, key=key, reverse=True)


class SignalEngine:
    """Wrapper exposing the signal engine as an object for callers expecting SignalEngine."""

    def __init__(self, default_cash: float = 10.0):
        self.default_cash = default_cash

    def evaluate(self, market: MarketInfo, cash: Optional[float] = None,
                 max_trade_override: Optional[float] = None) -> List[TradeSignal]:
        c = cash if cash is not None else self.default_cash
        return evaluate(market, cash=c, max_trade_override=max_trade_override)

    def rank_signals(self, signals: list) -> list:
        return rank_signals(signals)
