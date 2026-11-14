"""gates.py — three pre-entry evaluation gates for 15-minute crypto binaries.

Each candidate contract is scored before an order is placed:

1. ATR hurdle   — is the strike reachable given recent 1-minute volatility?
2. Fee-adjusted EV — does the model probability beat the price by more than
   the entry fee plus a buffer?
3. Time-decay cushion — arithmetic-Brownian P(contract wins) from spot/strike,
   with a high-gamma flag in the final minutes.

Gates only allow or deny and always emit diagnostics. With
``GATES_SHADOW_MODE`` (default False) a deny is logged as ``would_block`` and
the order proceeds unchanged; with it False the deny blocks the entry.

Read-only: this module places nothing and touches no auth code.
"""

from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import binance_feed
import config
import fees

log = logging.getLogger(__name__)

# Series handled by the gates. Every one of these has a Binance spot and
# 1-minute klines feed (binance_feed._KNOWN_BASES), so all 9 live series are
# scored; an unlisted series fails closed at gate0.
SERIES_ASSET = {
    "KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL",
    "KXXRP15M": "XRP", "KXDOGE15M": "DOGE", "KXBNB15M": "BNB",
    "KXHYPE15M": "HYPE", "KXZEC15M": "ZEC", "KXNEAR15M": "NEAR",
}

# One fetch per asset per TTL, so the gates do not add an HTTP call per
# candidate inside the 30s cycle. The cached tuple is (last_good_ts,
# last_attempt_ts, value): last_good_ts is pinned to the time of the last
# SUCCESSFUL fetch (a failure never moves it, so a long outage still ages out
# and eventually fails closed); last_attempt_ts is the last time fn() was
# called, so a dead feed is retried at most once per TTL, not every cycle.
_spot_cache: dict[str, tuple[float, float, float | None]] = {}
_atr_cache: dict[str, tuple[float, float, float | None]] = {}
_sigma_cache: dict[str, tuple[float, float, float | None]] = {}

# Last logged decision per candidate, so unchanged cycles add no log line.
_last_decision: dict[tuple[str, str], str] = {}


# ═════════════════════════════════════════════════════════════════════
# MARKET DATA (reuses binance_feed — no second feed)
# ═════════════════════════════════════════════════════════════════════

def asset_for(market) -> str | None:
    """Underlying asset for a market, or None if it is not a supported series."""
    series = getattr(market, "series", "") or ""
    if series in SERIES_ASSET:
        return SERIES_ASSET[series]
    # Fallback: KXBTC15M / KXBTC-... -> BTC
    ticker = getattr(market, "ticker", "") or ""
    for candidate in (series, ticker):
        base = candidate.split("-")[0]
        if base.startswith("KX"):
            base = base[2:]
        if base.endswith("15M"):
            base = base[:-3]
        if base in SERIES_ASSET.values():
            return base
    return None


def _cached(cache: dict, key: str, ttl: float, fn):
    """Fetch `fn()` once per TTL, returning the last good value on failure.

    A dead feed must not knock the bot out of the market: a transient failure
    keeps the previous non-None value (stale-ok) so the gate still scores with
    the last known spot/ATR. Only once the stale value is older than
    GATES_STALE_OK_SECONDS does it truly fail closed (None), so a prolonged
    outage is still visible to the operator in the gate metrics.
    """
    now = time.time()
    hit = cache.get(key)
    # Fresh (value fetched within TTL) -> return it, no fetch.
    # reviewed: TTL-gated return is additive; the guard only skips a refetch
    if hit and now - hit[1] < ttl:
        return hit[2]
    try:
        value = fn()
    except Exception as e:  # feed failure -> stale-ok, never a crash
        log.warning("gate feed failed for %s: %s", key, e)
        # reviewed: stale-ok fallback reuses a real good value inside its window
        if hit and hit[2] is not None and now - hit[0] < config.GATES_STALE_OK_SECONDS:
            # Reuse the last good value, but record this attempt so the dead
            # feed is not hammered every cycle (next retry after one TTL).
            cache[key] = (hit[0], now, hit[2])
            return hit[2]
        # reviewed: over-stale path fails closed (None) instead of trading
        if hit and hit[2] is not None:
            # Stale value too old: fail closed rather than trade on a price
            # that could be long gone.
            log.warning("gate feed stale %s for %.0fs — failing closed", key, now - hit[0])
        value = None
    cache[key] = (now, now, value)
    return value


def spot_for(asset: str) -> float | None:
    """Median venue spot price, cached for GATES_SPOT_CACHE_SECONDS.

    Prefers the live WebSocket price (subsecond) when a stream is running,
    falling back to the REST composite. Cached so the gate does not add an
    HTTP call per candidate inside the cycle.
    """
    return _cached(
        _spot_cache, asset, config.GATES_SPOT_CACHE_SECONDS,
        lambda: binance_feed.spot_price(f"{asset}USDT"),
    )


def atr_1m(asset: str) -> float | None:
    """Dollar average true range of recent 1-minute candles.

    Wilder-style TR over GATES_ATR_LOOKBACK candles: max(high-low,
    |high-prev_close|, |low-prev_close|), simple mean. None when the feed
    returns fewer than lookback+1 candles (warm-up => fail closed).
    """
    lookback = max(1, int(config.GATES_ATR_LOOKBACK))

    def _fetch():
        klines = binance_feed.get_klines(
            f"{asset}USDT", "1m", limit=lookback + 1, timeout=5)
        if not klines or len(klines) < lookback + 1:
            return None
        trs = []
        prev_close = float(klines[0][4])
        for k in klines[1:]:
            high, low, close = float(k[2]), float(k[3]), float(k[4])
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            prev_close = close
        window = trs[-lookback:]
        if not window:
            return None
        return sum(window) / len(window)

    return _cached(_atr_cache, asset, config.GATES_ATR_CACHE_SECONDS, _fetch)


def sigma_per_min(asset: str) -> float | None:
    """Per-minute log-return volatility of the underlying, cached.

    Std dev of 1-minute closes (binance_feed.sigma_per_minute, OKX fallback).
    None when the feed is empty so callers fall back to a fixed model vol.
    """
    return _cached(
        _sigma_cache, asset, config.GATES_SIGMA_CACHE_SECONDS,
        lambda: binance_feed.composite_sigma(f"{asset}USDT"),
    )


_LAST_DECISION_MAX = 2000


def _prune_last_decision() -> None:
    """Drop the oldest cached decisions once the cache exceeds its cap."""
    excess = len(_last_decision) - _LAST_DECISION_MAX
    if excess > 0:
        for key in list(_last_decision)[:excess]:
            _last_decision.pop(key, None)


def prefetch(assets) -> None:
    """Warm the spot/ATR caches concurrently before scoring candidates.

    Nine series would otherwise serialise up to 2 feed calls each (5s timeout)
    inside a 30s cycle. Failures land in the cache as None, so a dead feed
    still fails closed rather than costing another timeout per candidate.
    """
    # Bound the decision cache. Its keys are (ticker, side) and prefetch() only
    # receives asset symbols, so pruning by "tickers seen this cycle" compared
    # BTC against KXBTC15M-... and wiped the whole cache every cycle, which
    # defeated the log-on-change dedupe. Prune by size instead (dicts keep
    # insertion order, so this drops the oldest decisions first).
    _prune_last_decision()

    unique = [a for a in dict.fromkeys(assets) if a]
    if len(unique) < 2:
        for asset in unique:
            spot_for(asset)
            atr_1m(asset)
        return
    with ThreadPoolExecutor(max_workers=min(4, len(unique))) as pool:
        futures = [pool.submit(fn, asset)
                   for asset in unique for fn in (spot_for, atr_1m)]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                log.debug("prefetch future failed: %s", e)


# ═════════════════════════════════════════════════════════════════════
# PURE MATH
# ═════════════════════════════════════════════════════════════════════

def net_ev(q: float, p: float, fee: float = 0.0) -> float:
    """Per-contract net EV of buying at price p with win probability q."""
    return q * (1.0 - p) - (1.0 - q) * p - fee


def breakeven_q(p: float, fee: float = 0.0) -> float:
    """Win probability at which net_ev() is zero.

    q*(1-p) - (1-q)*p - fee = 0  =>  q = p + fee, since (1-p) + p = 1.
    """
    return p + fee


def entry_fee(p: float) -> float:
    """Per-contract entry fee per the Kalshi schedule via fees.py.

    Maker limit orders pay nothing; taking pays 0.07*p*(1-p). "Taker" is the
    conservative default because an exit may cross the book.
    """
    if not 0.0 < p < 1.0:
        return 0.0
    return fees.taker_fee_dollars(1.0, p) if config.GATES_FEE_TAKER else 0.0


def cushion_z(spot: float, strike: float, sigma: float, minutes: float,
              side: str) -> float | None:
    """Signed Brownian cushion in standard deviations, or None if undefined.

    YES wins when spot > strike, so z = (spot-strike)/(sigma*sqrt(t)); NO is
    the mirror. sigma is dollars per sqrt-minute.
    """
    if spot <= 0 or strike <= 0 or sigma <= 0:
        return None
    if minutes <= 0:
        return None
    denom = sigma * math.sqrt(minutes)
    if denom <= 0:
        return None
    z = (spot - strike) / denom
    return z if side.lower() == "yes" else -z


def normal_cdf(z: float) -> float:
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ═════════════════════════════════════════════════════════════════════
# RESULT
# ═════════════════════════════════════════════════════════════════════

@dataclass
class GateResult:
    allowed: bool
    reasons: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


# ═════════════════════════════════════════════════════════════════════
# EVALUATION
# ═════════════════════════════════════════════════════════════════════

def _hurdle_for(asset: str | None) -> float:
    return float(getattr(config, f"GATES_ATR_HURDLE_{asset or ''}",
                         config.GATES_ATR_HURDLE))


def _write_log(rec: dict) -> None:
    """Append one line, but only when a candidate's decision changes.

    At a 30s cycle a 9-series scan wrote roughly 10 MB/day, and gates_join
    only keeps the last record per (ticker, side) anyway. One line per
    decision change keeps the calibration file small; a restart re-logs once.
    """
    key = (rec.get("ticker", ""), rec.get("side", ""), rec.get("entry_price_cents"))
    if _last_decision.get(key) == rec.get("decision"):
        return
    try:
        path = Path(config.GATES_LOG_FILE)
        # reviewed: rotate gates log file when size reaches 1GB cap
        if path.exists() and path.stat().st_size >= config.LOG_MAX_BYTES:
            backup = path.with_suffix(".jsonl.1")
            try:
                # reviewed: remove existing backup before rotating log file
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
            except OSError:
                pass
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        _last_decision[key] = rec.get("decision")
    except Exception:
        log.warning("Failed to write gates log for %s", rec.get("ticker", "?"))


def evaluate_entry(market, spot_state=None, orderbook=None, *,
                   side: str | None = None,
                   entry_price_cents: float | None = None) -> GateResult:
    """Score one candidate against all enabled gates.

    ``spot_state`` may carry already-fetched ``spot`` / ``atr_1m`` /
    ``sigma_per_min`` (dollars per sqrt-minute); anything missing is fetched
    from binance_feed and cached. ``orderbook`` is accepted for callers that
    have one; the gates only need prices, which live on ``market``.

    In shadow mode a deny is returned as allowed=True with a ``would_block``
    metric, and the reason is still logged.
    """
    metrics: dict = {
        "ticker": getattr(market, "ticker", ""),
        "event_ticker": (getattr(market, "raw", None) or {}).get("event_ticker")
                        or getattr(market, "ticker", ""),
        "close_time": (getattr(market, "close_time", None).isoformat()
                       if hasattr(getattr(market, "close_time", None), "isoformat")
                       else None),
        "asset": None,
        "side": None,
        "entry_price_cents": None,
        "strike": getattr(market, "strike", 0.0),
        "spot": None,
        "atr_1m": None,
        "minutes_left": getattr(market, "minutes_left", 0.0),
        "hurdle_ratio": None,
        "hurdle_threshold": None,
        "cushion_z": None,
        "q": None,
        "fee": None,
        "net_ev": None,
        "breakeven_q": None,
        "high_gamma_zone": False,
        "shadow_mode": bool(config.GATES_SHADOW_MODE),
    }
    block_reasons: list[str] = []

    if not config.GATES_ENABLED:
        metrics["decision"] = "allow"
        metrics["reasons"] = ["gates disabled"]
        return GateResult(True, ["gates disabled"], metrics)

    asset = asset_for(market)
    metrics["asset"] = asset
    minutes = float(getattr(market, "minutes_left", 0.0) or 0.0)
    strike = float(getattr(market, "strike", 0.0) or 0.0)

    if side is None:
        mid = getattr(market, "mid_yes", None)
        side = "no" if (mid is not None and mid < 50) else "yes"
    side = side.lower()
    metrics["side"] = side

    if entry_price_cents is None:
        ask = getattr(market, "yes_ask" if side == "yes" else "no_ask", 0) or 0
        entry_price_cents = max(1, ask - 1) if ask else None
    p = (float(entry_price_cents) / 100.0) if entry_price_cents else None
    metrics["entry_price_cents"] = entry_price_cents

    # ── resolve spot / volatility ────────────────────────────────────────
    spot_state = spot_state or {}
    spot = spot_state.get("spot")
    sigma = spot_state.get("sigma_per_sqrt_min",
                           spot_state.get("sigma_per_min"))
    atr = spot_state.get("atr_1m")
    if asset is None:
        block_reasons.append("gate0: unsupported underlying")
    else:
        if spot is None:
            spot = spot_for(asset)
        if atr is None:
            atr = atr_1m(asset)
    metrics["spot"] = spot
    metrics["atr_1m"] = atr

    # ── Gate 1: ATR hurdle ──────────────────────────────────────────────
    if config.GATES_ATR_ENABLED:
        if strike <= 0:
            block_reasons.append("gate1: no strike on market")
        elif spot is None or atr is None or atr <= 0:
            block_reasons.append("gate1: insufficient candles/spot (fail closed)")
        elif minutes <= 0:
            block_reasons.append("gate1: no minutes remaining")
        else:
            expected_move = atr * math.sqrt(minutes)
            if config.GATES_ATR_SIDE_AWARE:
                # Distance the underlying must travel AGAINST us to lose. A
                # favourite already past the strike needs 0; its cushion is
                # scored by Gate 2. The spec-literal |strike-spot| would flag
                # every in-the-money favourite and make shadow data useless.
                required_move = (max(0.0, strike - spot) if side == "yes"
                                 else max(0.0, spot - strike))
            else:
                required_move = abs(strike - spot)
            ratio = required_move / expected_move if expected_move > 0 else float("inf")
            hurdle = _hurdle_for(asset)
            metrics["hurdle_ratio"] = ratio
            metrics["hurdle_threshold"] = hurdle
            if ratio > hurdle:
                block_reasons.append(
                    f"gate1: required {_price(required_move)} > {hurdle:.2f}x "
                    f"ATR-scaled move {_price(expected_move)} (ratio {ratio:.2f})"
                )

    # ── Gate 3: time-decay cushion (q feeds Gate 2) ─────────────────────
    q = None
    if config.GATES_TIME_DECAY_ENABLED:
        if minutes <= 0:
            block_reasons.append("gate3: no time remaining")
        elif sigma is None:
            # atr_1m is an average *true range* (E|X| over one candle, ~0.798*sigma),
            # not a standard deviation, so scale by sqrt(pi/2)=1.2533 to make it one.
            # NOTE: this widens the cone, and a wider sigma LOWERS q, so the gate
            # rejects MORE. Measured: ETH q 0.8502 -> 0.7961 (EV -0.066 -> -0.120);
            # all three logged favourites still blocked. The band, not this factor,
            # is what decides whether a trade is admissible.
            sigma = (atr * math.sqrt(math.pi / 2) if atr is not None
                     else (spot * 0.0007 if spot else None))
        if minutes <= config.GATES_HIGH_GAMMA_MINUTES:
            metrics["high_gamma_zone"] = True
            if config.GATES_DENY_HIGH_GAMMA:
                block_reasons.append(
                    f"gate3: high-gamma zone ({minutes:.2f} <= "
                    f"{config.GATES_HIGH_GAMMA_MINUTES:.1f} min)"
                )
        if minutes > 0 and sigma is not None and spot is not None and strike > 0:
            z = cushion_z(spot, strike, sigma, minutes, side)
            metrics["cushion_z"] = z
            if z is None:
                block_reasons.append("gate3: cushion undefined (fail closed)")
            else:
                q = normal_cdf(z)
                # The Brownian model has no drift, so at a tail price it
                # reproduces the venue quote and EV lands on ~0 by construction.
                # Inside the band we measured the realized win rate instead, and
                # it is higher than the quote; floor q with it so a genuine tail
                # edge is not modelled away. Outside the band q is untouched.
                if entry_price_cents is not None:
                    # GATES_MEASURED_Q is a YES-side win rate: it answers "P(YES
                    # wins | YES priced in this band)". A NO fill's price is the
                    # NO contract's own price, not a YES price, so comparing it
                    # to the band directly would floor a NO @ 97c -- a contract
                    # that settles NO only ~3% of the time -- with a 99.28%
                    # YES win rate. Gate on the YES-equivalent price on both
                    # sides: NO @ 97c == YES @ 3c, which is outside the band.
                    yes_equiv = (entry_price_cents if side == "yes"
                                 else 100 - entry_price_cents)
                    metrics["yes_equiv_price_cents"] = yes_equiv
                    if (config.GATES_MEASURED_FLOOR_ENABLED
                            and config.GATES_MEASURED_BAND_MIN_CENTS
                            <= yes_equiv <= config.GATES_MEASURED_BAND_MAX_CENTS):
                        q = max(q, config.GATES_MEASURED_Q)
                        metrics["q_measured_floor"] = True
                metrics["q"] = q

    # ── Gate 2: fee-adjusted EV ─────────────────────────────────────────
    # q comes from Gate 3; without it there is no model probability to score,
    # so EV is skipped rather than blocking on a disabled upstream gate.
    if config.GATES_EV_ENABLED and config.GATES_TIME_DECAY_ENABLED:
        if p is None or not 0.0 < p < 1.0:
            block_reasons.append("gate2: invalid entry price")
        elif q is None:
            block_reasons.append("gate2: no model probability")
        else:
            fee = entry_fee(p)
            ev = net_ev(q, p, fee)
            be = breakeven_q(p, fee)
            metrics["fee"] = fee
            metrics["net_ev"] = ev
            metrics["breakeven_q"] = be
            if ev < config.GATES_MIN_EDGE:
                block_reasons.append(
                    f"gate2: net_EV ${ev:.4f} < min_edge ${config.GATES_MIN_EDGE:.4f} "
                    f"(q {q:.3f} vs break-even {be:.3f})"
                )

    # ── decision (shadow only logs) ─────────────────────────────────────
    would_block = bool(block_reasons)
    if would_block and config.GATES_SHADOW_MODE:
        decision = "would_block"
        allowed = True
    else:
        decision = "block" if would_block else "allow"
        allowed = not would_block
    metrics["decision"] = decision
    metrics["reasons"] = block_reasons

    log.info(
        "GATE %s %s %s @%sc: ratio=%s z=%s q=%s fee=%s ev=%s be=%s -> %s%s",
        metrics["ticker"], side, asset, entry_price_cents,
        _fmt(metrics["hurdle_ratio"]), _fmt(metrics["cushion_z"]),
        _fmt(metrics["q"]), _fmt(metrics["fee"]), _fmt(metrics["net_ev"]),
        _fmt(metrics["breakeven_q"]), decision,
        (" — " + "; ".join(block_reasons)) if block_reasons else "",
    )

    rec = {"ts": datetime.now(timezone.utc).isoformat(), **metrics}
    _write_log(rec)
    return GateResult(allowed, block_reasons, metrics)


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _price(v: float) -> str:
    """Dollar move formatted at a precision that suits its magnitude.

    DOGE trades near $0.07 and XRP near $1, where 2 decimals round the
    required move to 0.00 and make the gate's reason unreadable.
    """
    a = abs(v)
    if a < 0.01:
        return f"{v:.7f}"
    if a < 1.0:
        return f"{v:.4f}"
    return f"{v:.2f}"
