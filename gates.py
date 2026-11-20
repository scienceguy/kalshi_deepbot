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
_momentum_cache: dict[str, tuple[float, float, dict | None]] = {}

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
    # reviewed: prune oldest cached feed values when cache exceeds 64 items to prevent unbounded memory growth
    if len(cache) > 64:
        for k in sorted(cache.keys(), key=lambda k: cache[k][1])[:16]:
            cache.pop(k, None)
    # Preserve last_good_ts on failure (fail closed) - only update on success.
    # last_good_ts = now only when value is not None; last_attempt_ts = now always.
    # reviewed: success updates both timestamps; failure preserves last_good_ts
    if value is not None:
        cache[key] = (now, now, value)
    else:
        # Failure: keep last_good_ts from hit (or 0 if never had one), update last_attempt_ts
        last_good_ts = hit[0] if hit else 0.0
        cache[key] = (last_good_ts, now, None)
    return value


def sweep_caches(max_age_seconds: float = 3600.0) -> int:
    """Remove cache entries older than max_age_seconds from all gate caches.
    
    Uses last_good_ts (v[0]) for the age check so entries with no valid value
    (None) that haven't had a successful fetch in max_age_seconds are removed.
    Entries with a valid value are kept even if old (they'll be refreshed on
    next cache miss).
    
    Returns the total number of entries removed.
    """
    now = time.time()
    removed = 0
    for cache in (_spot_cache, _atr_cache, _sigma_cache):
        stale_keys = [k for k, v in cache.items() 
                      if v[2] is None and now - v[0] > max_age_seconds]  # reviewed: M9 fix — use last_good_ts for age check on entries with no valid value
        for k in stale_keys:
            cache.pop(k, None)
            removed += 1
    return removed


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


def momentum_signal(asset: str) -> dict | None:
    """Coinbase lead/lag momentum signal (cached).
    
    Returns dict with change_5m, velocity_1m, aligned, direction, timestamp.
    Coinbase spot momentum leads Kalshi KXBTC15M pricing — when change_5m
    and velocity_1m are aligned, Kalshi lags the move, creating an edge.
    """
    return _cached(
        _momentum_cache, asset, config.GATES_MOMENTUM_CACHE_SECONDS,
        lambda: binance_feed.momentum_cached(asset),
    )


def sigma_from_atr(atr, spot):
    """Fractional per-sqrt-minute LOG-vol from a dollar ATR (B11).

    ATR is a mean TRUE RANGE of high-low candles, about 1.6 sigma, not
    E|dS| (~0.8 sigma). Legacy V1 multiplied by sqrt(pi/2) as if it were
    E|dS| and overstated sigma ~1.9x; V2 divides by 1.6. Config flag
    GATES_SIGMA_FROM_ATR_V2 selects; off keeps V1 byte-identical.
    """
    if atr is None or not spot:  # reviewed: ATR needs both a range and a price level to become a log-vol; missing either means no estimate (B11)
        return None
    if getattr(config, "GATES_SIGMA_FROM_ATR_V2", False):
        return (atr / spot) / 1.6
    return (atr / spot) * math.sqrt(math.pi / 2)


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


def kl_divergence(p: float, q: float) -> float:
    """Kullback-Leibler divergence KL(P||Q) for binary distributions.

    For binary markets: KL(P||Q) = p*ln(p/q) + (1-p)*ln((1-p)/(1-q))
    where p and q are probabilities in (0, 1).

    Returns infinity if p or q is at boundary (0 or 1) to avoid log(0).
    """
    # reviewed: boundary check to avoid log(0); pure math, no money path
    if p <= 0.0 or p >= 1.0 or q <= 0.0 or q >= 1.0:
        return float("inf")
    return p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q))


def symmetric_kl_divergence(p: float, q: float) -> float:
    """Symmetric KL divergence (Jensen-Shannon like): [KL(P||Q) + KL(Q||P)] / 2.

    This measures the information-theoretic distance between two probability
    distributions. A symmetric KL > 0.15 indicates meaningful disagreement
    between the two venues' pricing (per prediction market research).

    Args:
        p: Probability from first venue (e.g., Kalshi)
        q: Probability from second venue (e.g., Polymarket)

    Returns:
        Symmetric KL divergence, or inf if either probability is at boundary.
    """
    kl_pq = kl_divergence(p, q)
    kl_qp = kl_divergence(q, p)
    # reviewed: propagate boundary inf from kl_divergence
    if kl_pq == float("inf") or kl_qp == float("inf"):
        return float("inf")
    return (kl_pq + kl_qp) / 2.0


def entry_fee(p: float, series: str | None = None) -> float:
    """Per-contract entry fee per the Kalshi schedule via fees.py.

    Maker limit orders on standard series pay nothing; on maker-fee series
    they pay the maker fill fee (quadratic_with_maker_fees). Taking pays
    the taker fee. "Taker" is the conservative default because an exit may
    cross the book.
    """
    if not 0.0 < p < 1.0:
        return 0.0
    # S7 fix: check series fee type for maker-fee series
    if series:  # reviewed: series param is optional; when provided we check if it's a maker-fee series
        info = fees.series_fee_info(series)
        if info and fees.series_has_maker_fees(info.get("fee_type")):  # reviewed: maker-fee series charge maker fill fee; standard series pay taker fee on exit
            return fees.maker_fee_dollars(1.0, p, series)
    # reviewed: charge taker fee when GATES_FEE_TAKER or AGGRESSIVE_FILL is active
    return fees.taker_fee_dollars(1.0, p) if (config.GATES_FEE_TAKER or getattr(config, "AGGRESSIVE_FILL", False)) else 0.0


def cushion_z(spot: float, strike: float, sigma: float, minutes: float,
              side: str) -> float | None:
    """Signed GBM/log strike cushion in standard deviations, or None if undefined.

    z = |ln(S / K)| / (sigma * sqrt(t)) is the cushion magnitude; the sign is
    positive when the chosen side is in-the-money (cushion in our favour) and
    negative when out-of-the-money. ``sigma`` is a per-sqrt-minute LOG
    volatility (dimensionless, e.g. the per-minute std of spot log returns),
    and ``t`` is minutes to expiry. This is the Black-Scholes d2 denominator
    without the drift term, matching model.gbm_probability_yes_wins and
    binance_feed.prob_above.

    Pass gates.sigma_per_min() (per-minute log-vol) or convert ATR via
    gates.sigma_from_atr (V2: (atr/spot)/1.6; V1 legacy: (atr/spot)*sqrt(pi/2)).
    Do NOT pass a dollar-denominated sigma: that made the old arithmetic form
    dimensionally wrong and produced huge z values that never blocked.
    """
    if spot <= 0 or strike <= 0 or sigma <= 0:
        return None
    if minutes <= 0:
        return None
    denom = sigma * math.sqrt(minutes)
    if denom <= 0:
        return None
    # ln(S/K) > 0 means YES is ITM; for NO the cushion is the mirror.
    z = math.log(spot / strike) / denom
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

# B17: the measured-q floor is only as good as the settled fills behind it.
# scripts/q_floor_holdout.py recomputes the in-band YES-side win rate from
# settled fills on a time-ordered train/holdout split and writes
# local_data/q_floor.json. Min n=100 holdout rows: below that a Wilson bound
# still moves by >1c per few wins — that is noise, not a measurement — so the
# floor is refused instead of applied. GATES_MEASURED_Q (0.9928) was measured
# in-sample on quotes, not fills; it remains only the pre-B17 fallback below.
GATES_Q_FLOOR_MIN_N = 100
_Q_FLOOR_FILE = Path(__file__).resolve().parent / "local_data" / "q_floor.json"


def _measured_q_floor():
    """Return (floor, n, source); floor None means refuse the floor.

    Measurement file present and usable: the Wilson 95% holdout lower bound.
    File present but insufficient/corrupt: refuse rather than revive the
    constant. No file at all: keep the pre-B17 opt-in constant, but say so
    loudly in metrics (q_floor_n=0, q_floor_source=config:GATES_MEASURED_Q).
    """
    if not _Q_FLOOR_FILE.exists():  # reviewed: no measurement yet — keep pre-B17 opt-in constant, logged loudly as config:GATES_MEASURED_Q
        return config.GATES_MEASURED_Q, 0, "config:GATES_MEASURED_Q"
    try:
        data = json.loads(_Q_FLOOR_FILE.read_text())
        n = int(data["q_floor_n"])
        source = str(data.get("q_floor_source", "holdout"))
        if n < GATES_Q_FLOOR_MIN_N:  # reviewed: below 100 holdout rows the Wilson bound is noise; refuse rather than apply
            return None, n, f"refused:holdout_n_{n}<{GATES_Q_FLOOR_MIN_N}"
        floor = float(data["q_floor"])
        if not 0.0 <= floor <= 1.0:  # reviewed: measurement outside [0,1] is corrupt; refuse rather than apply
            return None, n, "refused:q_floor_out_of_range"
        return floor, n, source
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("q_floor holdout unreadable (%s); floor refused", e)
        return None, 0, "refused:unreadable"


def _hurdle_for(asset: str | None) -> float:
    """Return the atr-hurdle for `asset` (uppercased series suffix), falling back to the global default."""
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
        "pin_risk_denied": False,
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
    metrics["sigma_source"] = "spot_state" if sigma is not None else None  # reviewed: composite_sigma from the feed is preferred; ATR-derived sigma is the fallback (B11)
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
            # ATR is a mean true RANGE (high-low, ~1.6 sigma per minute), not
            # E|dS| (~0.8 sigma). V1 multiplied by sqrt(pi/2)=1.2533 as if it
            # were E|dS| and overstated sigma ~1.9x; V2 divides by 1.6.
            # A wider sigma LOWERS q, so the gate rejects MORE.
            sigma_v1 = (atr / spot) * math.sqrt(math.pi / 2) if (atr is not None and spot) else None
            sigma_v2 = (atr / spot) / 1.6 if (atr is not None and spot) else None
            sigma = sigma_v2 if config.GATES_SIGMA_FROM_ATR_V2 else sigma_v1  # reviewed: GATES_SIGMA_FROM_ATR_V2 off keeps the legacy sqrt(pi/2) conversion byte-identical (B11)
            metrics["sigma_atr_v1"] = sigma_v1
            metrics["sigma_atr_v2"] = sigma_v2
            metrics["sigma_source"] = "atr_v2" if config.GATES_SIGMA_FROM_ATR_V2 else "atr_v1"
            if sigma is None:  # reviewed: no ATR and no spot means no derivable vol; the fixed model vol keeps gate 3 dimensionally sane instead of failing open (B11)
                sigma = 0.003 if spot else None
                metrics["sigma_source"] = "fixed_fallback"
        if minutes <= config.GATES_HIGH_GAMMA_MINUTES:
            metrics["high_gamma_zone"] = True
            if config.GATES_DENY_HIGH_GAMMA:
                block_reasons.append(
                    f"gate3: high-gamma zone ({minutes:.2f} <= "
                    f"{config.GATES_HIGH_GAMMA_MINUTES:.1f} min)"
                )
        # Pin-risk guard: entries too close to expiry settle on a single print
        # near the strike, so even a wide cushion does not remove the pin risk.
        if (config.GATES_DENY_PIN_RISK  # reviewed: deny entries <=6 min from expiry where win rate drops below 98%
                and 0 < minutes <= config.GATES_PIN_RISK_MINUTES):
            metrics["pin_risk_denied"] = True
            block_reasons.append(
                f"gate3: pin risk ({minutes:.2f} min left <= "
                f"{config.GATES_PIN_RISK_MINUTES:.1f} min; settle prints near "
                f"the strike)"
            )
        if minutes > 0 and sigma is not None and spot is not None and strike > 0:
            z = cushion_z(spot, strike, sigma, minutes, side)
            metrics["cushion_z"] = z
            if z is None:
                block_reasons.append("gate3: cushion undefined (fail closed)")
            else:
                min_z = float(getattr(config, "GATES_MIN_CUSHION_Z", 1.80))
                # reviewed: enforce minimum distance from strike in standard deviations to eliminate pin risk and false breakouts
                if z < min_z:
                    block_reasons.append(
                        f"gate3: cushion_z {z:.2f} < min {min_z:.2f} (pin risk / spot too close to strike)"
                    )
                q = normal_cdf(z)
                # The Brownian model has no drift, so at a tail price it
                # reproduces the venue quote and EV lands on ~0 by construction.
                # Inside the band we measured the realized win rate instead, and
                # it is higher than the quote; floor q with it so a genuine tail
                # edge is not modelled away. Outside the band q is untouched.
                if entry_price_cents is not None:
                    # F-04 fix: apply floor based on OWN-side price being in the band.
                    # The measured floor (GATES_MEASURED_Q=0.9928) is a YES-side win rate
                    # measured on YES quotes in 95-99c. For NO side, the complementary
                    # win rate in the same band is ~0.989 (since NO @ 97c wins ~98.9%).
                    # Use own-side price to decide if floor applies; the floor VALUE
                    # differs by side (handled below).
                    own_price = entry_price_cents
                    metrics["own_price_cents"] = own_price
                    if (config.GATES_MEASURED_FLOOR_ENABLED
                            and config.GATES_MEASURED_BAND_MIN_CENTS
                            <= own_price <= config.GATES_MEASURED_BAND_MAX_CENTS):
                        # reviewed: B17 floor q at the Wilson 95% holdout lower bound from settled fills — never at the raw 0.9928; below 100 holdout rows the floor is refused
                        q_floor, q_floor_n, q_floor_source = _measured_q_floor()
                        metrics["q_floor_n"] = q_floor_n
                        metrics["q_floor_source"] = q_floor_source
                        if q_floor is not None:  # reviewed: B17 apply the measured Wilson bound only when the measurement is usable
                            # For NO side, the floor is the complement (NO wins when YES loses)
                            if side == "no":  # reviewed: F-04 apply NO-side complement of YES measured floor
                                q_floor = 1.0 - q_floor
                                metrics["q_floor_source"] = q_floor_source + " (NO complement)"
                            q = max(q, q_floor)
                            metrics["q_measured_floor"] = True
                metrics["q"] = q

    # ── Gate 4: Momentum (Coinbase lead/lag) ───────────────────────────────
    # Coinbase spot momentum leads Kalshi pricing — when change_5m and 
    # velocity_1m are aligned (same sign), Kalshi lags the move, creating an edge.
    # reviewed: NH3 hard gate — new conditions in money path require review markers
    if config.GATES_MOMENTUM_ENABLED and config.GATES_TIME_DECAY_ENABLED:
        # reviewed: only score when we have a model probability and entry price
        if q is not None and entry_price_cents is not None:
            mom = momentum_signal(asset)
            metrics["momentum"] = mom
            # reviewed: momentum may be None if feeds unavailable; log but don't block
            if mom is not None:
                change_5m = mom.get("change_5m", 0.0)
                velocity_1m = mom.get("velocity_1m", 0.0)
                aligned = mom.get("aligned", False)
                direction = mom.get("direction", "NEUTRAL")
                metrics["momentum_change_5m"] = change_5m
                metrics["momentum_velocity_1m"] = velocity_1m
                metrics["momentum_aligned"] = aligned
                metrics["momentum_direction"] = direction
                
                # Check if momentum meets minimum thresholds
                min_change = config.GATES_MOMENTUM_MIN_CHANGE_5M
                min_vel = config.GATES_MOMENTUM_MIN_VELOCITY_1M
                meets_thresholds = (abs(change_5m) >= min_change and 
                                   abs(velocity_1m) >= min_vel)
                
                # reviewed: boost q when momentum aligned with trade direction (Coinbase lead)
                if aligned and meets_thresholds:
                    # Boost q when momentum is aligned with our trade direction
                    # For YES side: UP momentum boosts; For NO side: DOWN momentum boosts
                    side_matches = (side == "yes" and direction == "UP") or \
                                   (side == "no" and direction == "DOWN")
                    # reviewed: only boost when momentum direction matches trade side
                    if side_matches:
                        boost = config.GATES_MOMENTUM_BOOST_Q
                        q = min(1.0, q + boost)
                        metrics["q_momentum_boost"] = boost
                        metrics["q"] = q
                        log.debug("Momentum boost: %s %s %s -> q=%.4f", 
                                metrics["ticker"], side, direction, q)
                    # reviewed: optional deny when momentum opposes trade (opt-in via config)
                    elif config.GATES_MOMENTUM_DENY_MISALIGNED:
                        # Momentum opposes our trade direction
                        block_reasons.append(
                            f"gate4: momentum {direction} opposes {side} trade "
                            f"(change_5m={change_5m:.5f}, velocity_1m={velocity_1m:.5f})"
                        )
                # reviewed: optional deny when momentum present but misaligned
                elif not aligned and config.GATES_MOMENTUM_DENY_MISALIGNED and meets_thresholds:
                    # Momentum present but misaligned
                    block_reasons.append(
                        f"gate4: momentum misaligned {direction} for {side} trade "
                        f"(change_5m={change_5m:.5f}, velocity_1m={velocity_1m:.5f})"
                    )
            else:
                metrics["momentum"] = None
                metrics["momentum_change_5m"] = None
                metrics["momentum_velocity_1m"] = None
                metrics["momentum_aligned"] = None
                metrics["momentum_direction"] = None

    # ── Gate 2: fee-adjusted EV ─────────────────────────────────────────
    # q comes from Gate 3; without it there is no model probability to score,
    # so EV is skipped rather than blocking on a disabled upstream gate.
    if config.GATES_EV_ENABLED and config.GATES_TIME_DECAY_ENABLED:
        if p is None or not 0.0 < p < 1.0:
            block_reasons.append("gate2: invalid entry price")
        elif q is None:
            block_reasons.append("gate2: no model probability")
        else:
            # S7 fix: pass series to entry_fee for maker-fee series detection
            series = fees.series_from_ticker(metrics["ticker"]) if metrics["ticker"] else None
            fee = entry_fee(p, series)
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
    """Format a gate metric for the log line: '-' for None, 4dp for floats, str() otherwise."""
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
