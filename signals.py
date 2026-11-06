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
from typing import Literal, Optional

import config
from market_finder import MarketInfo

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
                or (100 - market.yes_bid if market.yes_bid else market.yes_ask or 50)
            )
        result = base + buffer
    else:
        yes_bid, yes_ask, no_bid, no_ask = (
            market.yes_bid, market.yes_ask, market.no_bid, market.no_ask
        )
        if bullish:
            if yes_bid and yes_ask:
                target = yes_bid + 1 if (yes_ask - yes_bid) > 1 else yes_bid
                # Deeper quote: 1c under ask for ~85% live fill rate
                result = min(target, max(1, yes_ask - 1))
            else:
                result = yes_ask or yes_bid or 1
        else:
            if no_bid and no_ask:
                target = no_bid + 1 if (no_ask - no_bid) > 1 else no_bid
                result = min(target, max(1, no_ask - 1))
            elif no_ask:
                result = no_ask - 1
            elif 0 < yes_bid < 100:
                result = 100 - yes_bid
            else:
                result = yes_ask or 50

    # Kalshi prices must be 1-99; clamp edge cases (0/100) before returning.
    return max(1, min(99, result))


# ═════════════════════════════════════════════════════════════════════
# STABILITY CHECK (bid history tracking — doubles EV per kalshi_live_bot)
# ═════════════════════════════════════════════════════════════════════

_bid_hist: dict[str, list[tuple[float, int]]] = {}
_bid_hist_dirty = False
_last_bid_hist_save = 0.0
_BID_HIST_SAVE_INTERVAL = 30.0

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


def _load_bid_hist():
    global _bid_hist
    try:
        hist_file = _bid_hist_file()
        if hist_file.exists():
            with open(hist_file) as f:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                data = json.load(f)
            _bid_hist = {k: [(float(t), int(b)) for t, b in v] for k, v in data.items()}
    except FileNotFoundError:
        _bid_hist = {}
    except json.JSONDecodeError as e:
        log.error("Bid history file corrupted: %s — starting fresh", e)
        _bid_hist = {}
    except Exception as e:
        log.error("Failed to load bid history: %s — starting fresh", e)
        _bid_hist = {}

# Load bid history at module import so it's available immediately on startup.
_load_bid_hist()


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


def _maybe_save_bid_hist(now: float):
    global _bid_hist_dirty, _last_bid_hist_save
    if _bid_hist_dirty and (now - _last_bid_hist_save) >= _BID_HIST_SAVE_INTERVAL:
        try:
            _save_bid_hist()
            _bid_hist_dirty = False
        except Exception as e:
            log.warning("Failed to save bid history: %s — retrying next cycle", e)
        _last_bid_hist_save = now


def _force_save_bid_hist():
    global _bid_hist_dirty, _last_bid_hist_save
    if _bid_hist_dirty:
        _save_bid_hist()
        _bid_hist_dirty = False
        _last_bid_hist_save = time.time()


def _track_bid(ticker: str, yes_bid_cents: int, now: float):
    global _bid_hist_dirty
    if not _bid_hist:
        _load_bid_hist()
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
    _bid_hist_dirty = True
    return h


def _stability_ok(ticker: str, now: float):
    """
    Only quote when the underlying has stayed clearly away from the strike
    for the lookback window. Per kalshi_live_bot backtests this roughly
    doubles EV/ct and removes most losses.
    """
    if not getattr(config, "STABILITY_ENABLED", True):
        return True, ""
    h = _bid_hist.get(ticker) or []
    cutoff = now - config.STABILITY_LOOKBACK_MIN * 60
    window = [(t, b) for t, b in h if t >= cutoff]
    if not window:
        # Fresh ticker: allow with warning (fail-open on fresh start)
        # Only warn once per ticker per session to avoid log spam.
        warn_key = f"{ticker}:fresh"
        if warn_key not in _stability_warned:
            _stability_warned.add(warn_key)
            log.warning("Stability check: no history for %s — allowing trade (fresh start)", ticker)
        return True, "fresh start: no history"
    coverage = now - window[0][0]
    if coverage < config.STABILITY_MIN_COVERAGE_MIN * 60:
        # Insufficient coverage: allow with warning (fail-open)
        # Only warn once per ticker per session.
        warn_key = f"{ticker}:coverage"
        if warn_key not in _stability_warned:
            _stability_warned.add(warn_key)
            log.warning("Stability check: only %.1fmin history for %s (need %.1fmin) — allowing trade",
                        coverage/60, ticker, config.STABILITY_MIN_COVERAGE_MIN)
        return True, f"insufficient coverage: {coverage/60:.1f}min"
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
    implied_prob: int,
    source: str,
    strength: float,
    skip_stability: bool = False,
):
    """
    Shared evaluation logic. Returns TradeSignal or None.
    """
    def _pass(r):
        return TradeSignal("PASS", "", 0, 0, 0.0, r, market)

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
    # Contract count must be an integer: the API order uses int(count), so a
    # fractional size would over-reserve cash (resting_value/dollar_risk) and
    # mis-book PnL at settlement.
    n = floor_contracts(n)
    if n <= 0:
        return _pass(f"Budget ${max_dollars:.2f} too low for {ep}c entry")

    direction = "YES" if bullish else "NO"
    side = "yes" if bullish else "no"
    risk_ = n * ep / 100

    # Confidence scoring (from kalshi_live_bot)
    if source in ("SKEW", "TREND"):
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
    else:
        confidence = float(implied_prob) / 100.0

    reason = (
        f"{direction} ({source}) str={strength:.0f} conf={confidence:.2f} "
        f"{minutes_in:.1f}min {n:.4f}x@{ep}c=${risk_:.2f}"
    )
    log.info("SIGNAL: %s — %s", market.ticker, reason)
    return TradeSignal(direction, side, ep, n, risk_, reason, market, strength, confidence)


# ═════════════════════════════════════════════════════════════════════
# HEDGE_NO (from kalshi_15m_bot)
# ═════════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
# PANIC FADE (mean reversion — TurbineFi research: 93/96 variants profitable)
# ════════════════════════════════════════════════════════════════════════════

def _evaluate_panic_fade(market: MarketInfo, max_dollars: float, max_contracts: int = 0):
    """
    Panic fade strategy: bet opposite side when price moves sharply in short window.
    
    When YES price crashes sharply (e.g., 90c→40c in <2min), fade by buying YES
    at maker prices in 75-95c band, expecting mean reversion up.
    When YES spiked sharply, fade by buying NO at 5-25c.
    """
    results = []
    
    def _pass(r):
        results.append(TradeSignal("PASS", "", 0, 0, 0.0, r, market))
    
    now = time.time()
    ticker = market.ticker
    
    h = _bid_hist.get(ticker, []) if _bid_hist else []
    if len(h) < 3:
        return results
    
    cutoff = now - config.PANIC_FADE_WINDOW_SECONDS
    window = [(t, b) for t, b in h if t >= cutoff]
    if len(window) < 2:
        return results
    
    max_price = max(b for _, b in window)
    min_price = min(b for _, b in window)
    price_move = max_price - min_price
    
    if price_move < config.PANIC_FADE_MOVE_CENTS:
        return results
    
    first_price = window[0][1]
    last_price = window[-1][1]
    price_change = last_price - first_price
    
    if price_change < -config.PANIC_FADE_MOVE_CENTS:
        # YES dropped sharply — fade by buying YES at maker prices in favourite-maker band
        yes_ask = market.yes_ask
        if not yes_ask:
            return results
        
        ep = yes_ask - 1
        ep = max(config.ENTRY_BAND_MIN, min(config.ENTRY_BAND_MAX, ep))
        
        n = _size(ep, max_dollars, max_contracts)
        avail = market.yes_liquidity
        if avail and n > avail:
            n = min(n, float(avail))
        n = floor_contracts(n)
        if n <= 0:
            return results
        
        dollar_risk = n * ep / 100.0
        results.append(TradeSignal(
            "YES", "yes", ep, n, dollar_risk,
            f"Panic fade YES: price dropped {abs(price_change)}c in {config.PANIC_FADE_WINDOW_SECONDS}s",
            market, abs(price_change), confidence=float(ep)/100.0
        ))
    
    elif price_change > config.PANIC_FADE_MOVE_CENTS:
        # YES spiked sharply — fade by buying NO at 5-25c
        no_ask = market.no_ask
        if not no_ask:
            return results
        
        ep = no_ask - 1
        ep = max(5, min(25, ep))
        
        n = _size(ep, max_dollars, max_contracts)
        avail = market.no_liquidity
        if avail and n > avail:
            n = min(n, float(avail))
        n = floor_contracts(n)
        if n <= 0:
            return results
        
        dollar_risk = n * ep / 100.0
        results.append(TradeSignal(
            "NO", "no", ep, n, dollar_risk,
            f"Panic fade NO: YES spiked {price_change}c in {config.PANIC_FADE_WINDOW_SECONDS}s",
            market, abs(price_change), confidence=float(ep)/100.0
        ))
    
    return results


# ═════════════════════════════════════════════════════════════════════
# HEDGE_NO (from kalshi_15m_bot)
# ═════════════════════════════════════════════════════════════════════

# TWO-SIDED FAVOURITE MAKER (Approach D — from kalshi_live_bot)
# ═════════════════════════════════════════════════════════════════════

# HEDGE_NO (from kalshi_15m_bot)
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
    # If NO price is complemented (legacy), derive from YES bid
    if not no_ask and market.yes_bid and config.NO_PRICE_IS_COMPLEMENT:
        no_ask = 100 - market.yes_bid
    if not no_ask:
        _pass("No NO ask")
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
    n = floor_contracts(n)
    if n <= 0:
        _pass(f"Budget ${max_dollars:.2f} too low for {ep}c entry")
        return results

    risk_ = n * ep / 100
    confidence = float(ep) / 100.0
    reason = (
        f"HEDGE_NO str=0 conf={confidence:.2f} "
        f"{minutes_in:.1f}min {n:.4f}x@{ep}c=${risk_:.2f}"
    )
    log.info("SIGNAL: %s — %s", market.ticker, reason)
    results.append(TradeSignal("NO", "no", ep, n, risk_, reason, market, 0.0, confidence))
    return results


def _evaluate_two_sided(market: MarketInfo, max_dollars: float, max_contracts: int = 0):
    """
    Two-sided favourite maker strategy (Approach D).

    Emit the favourite side (YES or NO) when it is in range (ENTRY_BAND_MIN-ENTRY_BAND_MAX),
    posting 1c under its ask so the order is always a maker.
    Returns a list of TradeSignal (0 or 1 per market — when both sides are
    in range, only the more extreme favourite is kept).
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

    ok_stable, why = _stability_ok(market.ticker, time.time())
    if config.STABILITY_ENABLED and not ok_stable:
        _pass(why)
        return results

    no_ask = market.no_ask
    # If NO price is complemented (legacy), derive from YES bid
    if not no_ask and market.yes_bid and config.NO_PRICE_IS_COMPLEMENT:
        no_ask = 100 - market.yes_bid
    yes_ask = market.yes_ask
    if yes_ask == 0 and market.no_bid and config.NO_PRICE_IS_COMPLEMENT:
        yes_ask = 100 - market.no_bid
    if no_ask <= 0 and yes_ask <= 0:
        _pass("No quotes")
        return results

    no_pay = _maker_price(market, bullish=False) if no_ask > 0 else 0
    yes_pay = _maker_price(market, bullish=True) if yes_ask > 0 else 0

    cands = []
    entry_min = getattr(config, "ENTRY_BAND_MIN", config.MIN_NO_ENTRY_CENTS)
    entry_max = getattr(config, "ENTRY_BAND_MAX", config.MAX_NO_ENTRY_CENTS)
    if entry_min <= no_pay <= entry_max:
        cands.append(("NO", "no", no_pay, no_ask))
    if config.ALLOW_YES_BUYS and entry_min <= yes_pay <= entry_max:
        cands.append(("YES", "yes", yes_pay, yes_ask))

    # If both sides are in range, keep only the more extreme favourite
    # (further from 50c) to avoid betting both sides of the same market.
    if len(cands) == 2:
        if abs(no_pay - 50) >= abs(yes_pay - 50):
            cands = [c for c in cands if c[0] == "NO"]
        else:
            cands = [c for c in cands if c[0] == "YES"]

    if not cands:
        _pass(f"No favourite in {entry_min}-{entry_max}c (no={no_pay}c yes={yes_pay}c)")
        return results

    for direction, side, entry_c, implied in cands:
        bullish = (direction == "YES")
        sig = _evaluate_common(
            market, max_dollars, max_contracts, bullish,
            entry_min, entry_max, minutes_in, implied,
            "FAVOURITE", float(implied),
            skip_stability=True,
        )
        if sig and sig.is_trade:
            results.append(sig)
        elif sig:
            _pass(sig.reason)
    return results


# ═════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════

def evaluate(market: MarketInfo, cash: float = 10.0,
             max_trade_override: Optional[float] = None):
    """
    Evaluate a market and return a list of TradeSignal (0 or 1 per market).
    """
    now = time.time()
    _maybe_save_bid_hist(now)
    max_dollars = max_trade_override if max_trade_override is not None \
                  else cash * config.BALANCE_PCT_PER_TRADE

    effective_cash = max_dollars / config.BALANCE_PCT_PER_TRADE \
                     if max_trade_override is not None else cash

    _track_bid(market.ticker, int(round(market.yes_bid or 0)), now)

    if market.bracket_role == "HEDGE_NO":
        result = _evaluate_hedge(market, max_dollars)
    else:
        # Two-sided favourite maker strategy (Approach D)
        result = _evaluate_two_sided(market, max_dollars,
                                     max_contracts_for_cash(effective_cash))

    # Panic-fade: mean-reversion when price moves sharply in short window
    result.extend(_evaluate_panic_fade(market, max_dollars,
                                       max_contracts_for_cash(effective_cash)))

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