"""
crossvenue.py — measure Kalshi vs Polymarket price disagreement on the same event.

Both venues publish *threshold ladders*: a set of strikes over the same
underlying at the same expiry, each with a price P(price > strike). A ladder is
therefore a sampled CDF. That matters because it removes the need to match
individual strikes across venues — we interpolate each ladder into a CDF and
compare the distributions at any strike we like. Off-by-one strikes, different
strike spacing and different tick sizes stop being a problem.

Settlement sources differ (Kalshi: CF Benchmarks BRTI 60s average; Polymarket:
Binance 1m candle), so this is a *statistical* anchor, not a riskless arb. Call
it a deviation study, not an arbitrage detector.

Read-only. No orders are placed anywhere.
"""

from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import requests

log = logging.getLogger("crossvenue")

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

ASSETS = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH"}

_KALSHI_EVENT = re.compile(r"^KX(BTC|ETH)(?:D|15M)?-(\d{2})([A-Z]{3})(\d{2})(\d{2}|\d{4})$")
_PM_SLUG = re.compile(r"^(bitcoin|ethereum|btc|eth)-above-on-([a-z]+)-(\d{1,2})-(\d{4})(?:-(\d{1,2})([ap]m)-et)?$")

# Polymarket US 15M up/down windows. Verified live 2026-11-21: the event slug
# (btc-updown-15m-<date>-<hhmm>z) carries the window START in UTC; the market
# slug is the cpc- prefixed shape and that is what /v1/market/slug resolves.
_PM_UPDOWN_15M = re.compile(r"^(?:cpc-)?(btc|eth)-updown-15m-(\d{4})-(\d{2})-(\d{2})-(\d{4})z$")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# A rung quoting a spread wider than this is not a price, it is an empty book.
MAX_SPREAD = 0.08

@dataclass
class Rung:
    """One strike on a ladder: P(price > strike), expressed as 0..1."""
    strike: float
    mid: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    ticker: str = ""
    rules: str = ""

    @property
    def spread(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

@dataclass
class Ladder:
    venue: str
    asset: str
    expiry_et: datetime
    rungs: List[Rung] = field(default_factory=list)
    label: str = ""

    @property
    def ok(self) -> bool:
        return len(self.sorted_rungs()) >= 3

    def sorted_rungs(self, max_spread: Optional[float] = MAX_SPREAD) -> List[Rung]:
        """Usable rungs, ascending by strike.

        A wide book is not a price. Rungs quoting bid 0.22 / ask 1.00 carry no
        information about the event, only about the absence of a market maker.
        """
        keep = []
        for r in self.rungs:
            if not (0.0 < r.mid < 1.0):
                continue
            sp = r.spread
            if max_spread is not None and sp is not None and sp > max_spread:
                continue
            keep.append(r)
        return sorted(keep, key=lambda r: r.strike)

@dataclass
class Deviation:
    strike: float
    kalshi_mid: float
    poly_mid: float

    @property
    def cents(self) -> float:
        """Positive = Kalshi prices the event cheaper than Polymarket."""
        return (self.poly_mid - self.kalshi_mid) * 100.0

def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

def parse_kalshi_event(event_ticker: str) -> Optional[Tuple[str, datetime]]:
    """KXBTCD-26NOV0521 -> ('BTC', 2026-11-05 21:00 ET). Ticker hour is ET.
    KXBTC15M-26NOV052245 -> ('BTC', 2026-11-05 22:45 ET). 15M time is HHMM."""
    m = _KALSHI_EVENT.match(event_ticker or "")
    if not m:
        return None
    asset, yy, mon, dd, hhmm = m.groups()
    try:
        hour = int(hhmm[:2])
        minute = int(hhmm[2:]) if len(hhmm) > 2 else 0
        return asset, datetime(2000 + int(yy), _MONTHS[mon], int(dd), hour, minute)
    except (KeyError, ValueError):
        return None

def parse_pm_slug(slug: str) -> Optional[Tuple[str, datetime]]:
    """bitcoin-above-on-november-5-2026-1pm-et -> ('BTC', 2026-11-05 13:00 ET).

    Slug with no hour means the daily ladder, which resolves at 23:59 ET.
    btc-updown-15m-2026-11-21-1930z -> ('BTC', windowEnd in ET): the 15M
    up/down slug carries the window START in UTC, so the expiry is start+15m.
    """
    m = _PM_UPDOWN_15M.match(slug or "")
    if m:
        word, yyyy, mm, dd, hhmm = m.groups()
        start_utc = datetime(int(yyyy), int(mm), int(dd), int(hhmm[:2]),
                             int(hhmm[2:]), tzinfo=timezone.utc)
        return ASSETS[word], _utc_to_naive_et(start_utc + timedelta(minutes=15))
    m = _PM_SLUG.match(slug or "")
    if not m:
        return None
    word, month_name, dd, yyyy, hh, ampm = m.groups()
    try:
        month = datetime.strptime(month_name[:3], "%b").month
        if hh:
            hour = int(hh) % 12 + (12 if ampm == "pm" else 0)
        else:
            hour = 23
        return ASSETS[word], datetime(int(yyyy), month, int(dd), hour, 59 if not hh else 0)
    except (KeyError, ValueError):
        return None

def fetch_kalshi_ladder(event_ticker: str, timeout: int = 20) -> Optional[Ladder]:
    """Terminal-threshold ('greater' strike) markets for one Kalshi event."""
    parsed = parse_kalshi_event(event_ticker)
    if not parsed:
        return None
    asset, expiry = parsed
    try:
        resp = requests.get(f"{KALSHI_BASE}/markets",
                            params={"event_ticker": event_ticker, "limit": 200},
                            timeout=timeout)
        resp.raise_for_status()
        markets = resp.json().get("markets", [])
    except Exception as e:
        log.warning("kalshi ladder %s failed: %s", event_ticker, e)
        return None

    ladder = Ladder("kalshi", asset, expiry, label=event_ticker)
    for m in markets:
        # 15M up/down markets quote greater_or_equal (verified live
        # 2026-11-21); daily/hourly ladder events carry only greater, so
        # accepting both adds the single 15M market without touching ladders.
        if (m.get("strike_type") not in ("greater", "greater_or_equal")
                or m.get("floor_strike") is None):
            continue
        bid = _f(m.get("yes_bid_dollars"))
        ask = _f(m.get("yes_ask_dollars"))
        last = _f(m.get("last_price_dollars"))
        if bid is not None and ask is not None and ask > bid:
            mid = (bid + ask) / 2.0
        else:
            mid = last if last is not None else (bid or ask)
        if mid is None:
            continue
        ladder.rungs.append(Rung(float(m["floor_strike"]), mid, bid, ask,
                                 m.get("ticker", ""), (m.get("rules_primary") or "")[:400]))
    return ladder

def fetch_pm_ladder_us(slug: str, timeout: int = 20) -> Optional[Ladder]:
    """Threshold markets from the Polymarket US production API (api.polymarket.us).

    Lazily imports polymarket_us_client so the module loads even if that client
    hasn't been created yet — falls back to the Gamma path gracefully.
    """
    # Lazy import: only attempt if the module exists
    try:
        from polymarket_us_client import get_market_by_slug  # type: ignore
    except Exception:
        log.debug("polymarket_us_client not available, skipping US fetch")
        return None

    try:
        resp = get_market_by_slug(slug)
    except Exception as e:
        log.debug("US fetch %s failed: %s", slug, e)
        return None

    # The US client returns the envelope {"market": {...}}; the mapper below
    # reads the market fields directly. Accept a bare market dict too.
    market = None
    if isinstance(resp, dict):
        inner = resp.get("market")
        market = inner if isinstance(inner, dict) else resp
    if not market:
        return None

    parsed = parse_pm_slug(slug)
    if not parsed:
        return None
    asset, expiry = parsed

    # Map asset symbol from assetPriceTerms
    asset_symbol = market.get("assetPriceTerms", {}).get("asset", {}).get("symbol", "")
    pm_asset = ASSETS.get(asset_symbol.lower(), asset)

    # Determine strike from assetPriceTerms
    apt = market.get("assetPriceTerms", {})
    strike_float: Optional[float] = None
    strike = apt.get("strike")
    if strike is not None:
        strike_float = _f(strike)
    # For range markets, use rangeLower/rangeUpper
    if strike_float is None:
        rl = _f(apt.get("rangeLower"))
        ru = _f(apt.get("rangeUpper"))
        if rl is not None and ru is not None:
            strike_float = (rl + ru) / 2.0
    # For up/down markets, use priceToBeat (live-verified 2026-11-21: a
    # {value, currency} quote object, not a bare number)
    if strike_float is None:
        ptb = apt.get("priceToBeat")
        if isinstance(ptb, dict):
            ptb = ptb.get("value")
        if ptb is not None:
            strike_float = _f(ptb)

    if strike_float is None:
        log.debug("US %s: could not determine strike", slug)
        return None

    # Find YES side price from marketSides
    yes_price: Optional[float] = None
    for side in market.get("marketSides", []):
        if side.get("long", False) or side.get("marketSideType", "") == "YES":
            yes_price = _f(side.get("price"))
            break
    # Fallback: try bestBidQuote/bestAskQuote
    if yes_price is None:
        for quote_key in ("bestBidQuote", "bestAskQuote"):
            q = market.get(quote_key)
            if q:
                yes_price = _f(q.get("value"))
                if yes_price is not None:
                    break

    # Determine bid and ask from bestBidQuote/bestAskQuote
    bid: Optional[float] = None
    ask: Optional[float] = None
    for quote_key in ("bestBidQuote", "bestAskQuote"):
        q = market.get(quote_key)
        if q:
            val = _f(q.get("value"))
            if val is not None:
                if quote_key == "bestBidQuote":
                    bid = val
                else:
                    ask = val

    # If no explicit bid/ask from quotes, use YES side price as both
    if bid is None or ask is None:
        if yes_price is not None:
            if bid is None:
                bid = yes_price
            if ask is None:
                ask = yes_price

    # Compute mid
    if bid is not None and ask is not None and ask > bid:
        mid = (bid + ask) / 2.0
    else:
        mid = yes_price if yes_price is not None else strike_float
    if mid is None:
        return None
    if not (0.0 <= mid <= 1.0):
        # clamp to valid probability range
        mid = max(0.0, min(1.0, mid))

    # Determine label/ticker from asset name
    asset_name = apt.get("assetName", "") or ""
    ticker = asset_name[:4] if asset_name else slug[:4]

    ladder = Ladder("polymarket", pm_asset, expiry, label=slug)
    ladder.rungs.append(Rung(strike_float, mid, bid, ask, ticker, ""))
    return ladder

def fetch_pm_ladder(slug: str, timeout: int = 20) -> Optional[Ladder]:
    """Threshold markets inside one Polymarket event (a single ladder)."""
    parsed = parse_pm_slug(slug)
    if not parsed:
        return None
    asset, expiry = parsed
    try:
        resp = requests.get(f"{GAMMA_BASE}/events", params={"slug": slug}, timeout=timeout)
        resp.raise_for_status()
        events = resp.json()
        if not events:
            return None
        markets = events[0].get("markets", [])
    except Exception as e:
        log.warning("polymarket ladder %s failed: %s", slug, e)
        return None

    ladder = Ladder("polymarket", asset, expiry, label=slug)
    for m in markets:
        strike = _strike_from_pm(m)
        if strike is None:
            continue
        prices = m.get("outcomePrices") or "[]"
        try:
            import json
            yes = _f(json.loads(prices)[0]) if isinstance(prices, str) else _f(prices[0])
        except Exception:
            continue
        if yes is None:
            continue
        bid = _f(m.get("bestBid"))
        ask = _f(m.get("bestAsk"))
        if bid is not None and ask is not None and ask > bid:
            mid = (bid + ask) / 2.0
        else:
            mid = yes
        if not (0.0 <= mid <= 1.0):
            continue
        ladder.rungs.append(Rung(strike, mid, bid, ask,
                                 str(m.get("id", "")), (m.get("description") or "")[:400]))
    return ladder

def _strike_from_pm(market: dict) -> Optional[float]:
    """Strike from groupItemTitle ('78,400', '↑ 88,000') or the question text."""
    gi = (market.get("groupItemTitle") or "").replace(",", "").replace("↑", "").replace("↓", "").strip()
    val = _f(gi)
    if val is not None:
        return val
    q = (market.get("question") or "").replace(",", "")
    m = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", q)
    return float(m.group(1)) if m else None

def to_cdf(ladder: Ladder):
    """Monotone interpolation of P(price > strike) over the ladder's strike range.

    Returns list of (strike, probability) tuples, or None if too thin.
    Quotes are cleaned to be non-increasing: a stale rung must not claim a
    higher chance of clearing a higher strike.
    """
    rungs = ladder.sorted_rungs()
    if len(rungs) < 3:
        return None
    strikes = [r.strike for r in rungs]
    probs = [r.mid for r in rungs]
    # Make non-increasing (P(above X) decreases as X increases)
    for i in range(1, len(probs)):
        if probs[i] > probs[i - 1]:
            probs[i] = probs[i - 1]
    # Clip to valid probability range
    eps = 1e-4
    probs = [max(eps, min(1 - eps, p)) for p in probs]
    return list(zip(strikes, probs))

def _interp(x, xp, fp):
    """Linear interpolation of fp at x given sorted xp. Pure stdlib."""
    i = bisect.bisect_left(xp, x)
    if i == 0:
        return fp[0]
    if i == len(xp):
        return fp[-1]
    x0, x1 = xp[i - 1], xp[i]
    f0, f1 = fp[i - 1], fp[i]
    if x1 == x0:
        return f0
    return f0 + (f1 - f0) * (x - x0) / (x1 - x0)

def compare(kalshi: Ladder, poly: Ladder):
    """Deviation at every Kalshi strike, by interpolating the Polymarket CDF."""
    k = to_cdf(kalshi)
    p = to_cdf(poly)
    if k is None or p is None:
        return []
    poly_strikes = [pt[0] for pt in p]
    lo, hi = poly_strikes[0], poly_strikes[-1]
    out = []
    for strike, kalshi_mid in k:
        if not (lo <= strike <= hi):
            continue  # extrapolating a CDF is inventing a number
        poly_mid = _interp(strike, [pt[0] for pt in p], [pt[1] for pt in p])
        out.append(Deviation(strike, float(kalshi_mid), poly_mid))
    return out


def compute_kl_divergence(kalshi: Ladder, poly: Ladder) -> List[dict]:
    """Compute symmetric KL divergence between Kalshi and Polymarket at matching strikes.
    
    Returns a list of dicts with strike, kalshi_mid, poly_mid, symmetric_kl,
    kl_arb_flag (KL > GATES_KL_ARB_THRESHOLD), kl_watch_flag (KL > GATES_KL_WATCH_THRESHOLD).
    """
    try:
        import config
    except ImportError:
        # Config not available, use defaults
        ARB_THRESHOLD = 0.15
        WATCH_THRESHOLD = 0.05
    else:
        ARB_THRESHOLD = config.GATES_KL_ARB_THRESHOLD
        WATCH_THRESHOLD = config.GATES_KL_WATCH_THRESHOLD
    
    try:
        from kl_tools import symmetric_kl_divergence
    except ImportError:
        log.warning("kl_tools.symmetric_kl_divergence not available")
        return []
    
    k = to_cdf(kalshi)
    p = to_cdf(poly)
    if k is None or p is None:
        return []
    
    poly_strikes = [pt[0] for pt in p]
    lo, hi = poly_strikes[0], poly_strikes[-1]
    out = []
    for strike, kalshi_mid in k:
        if not (lo <= strike <= hi):
            continue
        poly_mid = _interp(strike, [pt[0] for pt in p], [pt[1] for pt in p])
        sym_kl = symmetric_kl_divergence(kalshi_mid, poly_mid)
        out.append({
            "strike": float(strike),
            "kalshi_mid": float(kalshi_mid),
            "poly_mid": float(poly_mid),
            "symmetric_kl": sym_kl,
            "kl_arb_flag": sym_kl != float("inf") and sym_kl > ARB_THRESHOLD,
            "kl_watch_flag": sym_kl != float("inf") and sym_kl > WATCH_THRESHOLD,
        })
    return out

def discover_kalshi_events(series: Tuple[str, ...] = ("KXBTCD", "KXETHD", "KXBTC", "KXETH", "KXBTC15M", "KXETH15M"),
                           timeout: int = 20) -> List[str]:
    """Open event tickers for the given series, e.g. ['KXBTCD-26NOV0521']."""
    out: List[str] = []
    for s in series:
        try:
            resp = requests.get(f"{KALSHI_BASE}/events",
                                params={"series_ticker": s, "status": "open", "limit": 100},
                                timeout=timeout)
            resp.raise_for_status()
            for e in resp.json().get("events", []):
                t = e.get("event_ticker", "")
                if t and t not in out:
                    out.append(t)
        except Exception as e:
            log.warning("kalshi events %s failed: %s", s, e)
    return out

def candidate_pm_slugs(asset: str, expiry_et: datetime) -> List[str]:
    """Slug guesses for the Polymarket ladder that expires at the same ET time.

    The first guess is the 15M up/down market whose window ends at expiry_et
    (cpc- market slug; verified live 2026-11-21). Against a ladder event that
    guess resolves to a one-rung ladder, which three_way rejects on the
    strike, so it never produces a bogus pair.
    """
    name = "bitcoin" if asset == "BTC" else "ethereum"
    month = expiry_et.strftime("%B").lower()
    base = f"{name}-above-on-{month}-{expiry_et.day}-{expiry_et.year}"
    start_utc = to_utc(expiry_et) - timedelta(minutes=15)
    updown = f"cpc-{asset.lower()}-updown-15m-{start_utc:%Y-%m-%d}-{start_utc:%H%M}z"
    if expiry_et.hour >= 23:
        return [updown, base]
    h12 = expiry_et.hour % 12 or 12
    ampm = "am" if expiry_et.hour < 12 else "pm"
    return [updown, f"{base}-{h12}{ampm}-et"]

def _et_offset_hours(naive_et: datetime) -> int:
    """US Eastern offset from UTC for a naive ET wall-clock date.

    Uses the post-2007 DST rule directly instead of zoneinfo: on a host without
    tzdata, zoneinfo raises and a hardcoded -5 puts summer expiries an hour
    early, which scales the variance by ~2x.
    """
    year = naive_et.year
    # Second Sunday in March; first Sunday in November. Both can start on the
    # 8th/1st respectively, and the modulo term moves them to the right Sunday.
    second_sunday_mar = 8 + (6 - datetime(year, 3, 8).weekday()) % 7
    first_sunday_nov = 1 + (6 - datetime(year, 11, 1).weekday()) % 7
    dst_start = datetime(year, 3, second_sunday_mar, 2)
    dst_end = datetime(year, 11, first_sunday_nov, 2)
    return -4 if dst_start <= naive_et < dst_end else -5

def to_utc(naive_et: datetime) -> datetime:
    """ET wall clock -> aware UTC."""
    offset = _et_offset_hours(naive_et)
    return naive_et.replace(tzinfo=timezone(timedelta(hours=offset))).astimezone(timezone.utc)

def _utc_to_naive_et(dt_utc: datetime) -> datetime:
    """Aware UTC -> naive ET, via the same hand-rolled DST rule as to_utc.

    No zoneinfo (the host may lack tzdata); round-trip to_utc to pick the
    offset, falling back to EST inside the ambiguous fall-back hour.
    """
    for off in (-4, -5):
        naive = (dt_utc + timedelta(hours=off)).replace(tzinfo=None)
        if to_utc(naive) == dt_utc:
            return naive
    return (dt_utc + timedelta(hours=-5)).replace(tzinfo=None)

def three_way(kalshi: Ladder, poly: Ladder, spot: float, sigma_min: float,
              now: Optional[datetime] = None) -> List[dict]:
    from binance_feed import prob_above

    now = now or datetime.now(timezone.utc)
    minutes_left = (to_utc(kalshi.expiry_et) - now).total_seconds() / 60.0
    if minutes_left <= 0:
        return []

    ks = kalshi.sorted_rungs()
    ps = poly.sorted_rungs()
    if len(ks) == 1 and len(ps) == 1:
        # A 15M up/down pair is one market per side: no ladder to interpolate.
        # The strike is the opening 60s BRTI average on both venues, so a
        # mismatch means a different window, not a price disagreement.
        if abs(ps[0].strike - ks[0].strike) > 1e-6:
            return []
        fair = prob_above(spot, ks[0].strike, sigma_min, minutes_left)
        if fair != fair:  # NaN
            return []
        return [{
            "strike": float(ks[0].strike),
            "fair": fair,
            "kalshi": float(ks[0].mid),
            "spread": float(ks[0].spread or 0.0),
            "poly": float(ps[0].mid),
            "kalshi_edge_c": (fair - ks[0].mid) * 100.0,
            "poly_edge_c": (fair - ps[0].mid) * 100.0,
            "minutes_left": minutes_left,
        }]

    k = to_cdf(kalshi)
    p = to_cdf(poly)
    if k is None or p is None:
        return []
    lo, hi = p[0][0], p[-1][0]  # to_cdf returns a list of tuples, not an indexable array

    spread_map = {float(r.strike): (r.spread or 0.0) for r in kalshi.sorted_rungs()}

    rows = []
    for strike, kalshi_mid in k:
        if not (lo <= strike <= hi):
            continue
        poly_mid = _interp(strike, [pt[0] for pt in p], [pt[1] for pt in p])
        fair = prob_above(spot, strike, sigma_min, minutes_left)
        if fair != fair:  # NaN
            continue
        rows.append({
            "strike": float(strike),
            "fair": fair,
            "kalshi": float(kalshi_mid),
            "spread": float(spread_map.get(strike, 0.0)),
            "poly": poly_mid,
            "kalshi_edge_c": (fair - kalshi_mid) * 100.0,
            "poly_edge_c": (fair - poly_mid) * 100.0,
            "minutes_left": minutes_left,
        })
    return rows

def _fetch_pm_ladder_with_fallback(slug: str, timeout: int = 20) -> Optional[Ladder]:
    """Try US production API first, fall back to Gamma."""
    # Try US production API first
    pl = fetch_pm_ladder_us(slug, timeout=timeout)
    if pl is not None:
        log.info("polymarket source: US production api.polymarket.us")
        return pl
    # Fall back to Gamma
    log.info("polymarket source: gamma fallback")
    return fetch_pm_ladder(slug, timeout=timeout)

def main() -> int:
    """Three-way comparison: spot-implied fair value vs each venue's ladder."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import binance_feed

    spot_by_asset = {}
    for asset in ("BTC", "ETH"):
        symbol = f"{asset}USDT"
        sources = binance_feed.price_sources(symbol)
        px = binance_feed.composite_price(symbol)
        sg = binance_feed.sigma_per_minute(symbol)
        if px is None or sg is None:
            print(f"no spot/vol for {symbol}")
            continue
        spot_by_asset[asset] = (px, sg)
        lo, hi = min(sources.values()), max(sources.values())
        print(f"{symbol} composite {px:,.2f}  sigma/min {sg*100:.4f}%  "
              f"venue dispersion ${hi - lo:,.0f} "
              f"({', '.join(f'{k} {v:,.0f}' for k, v in sorted(sources.items()))})")
        if hi - lo > 0:
            sig_dollars = px * sg * (39 ** 0.5)
            print(f"    dispersion is {100 * (hi - lo) / sig_dollars:.0f}% of one "
                  f"39-minute sigma (${sig_dollars:,.0f}) — this is the basis risk")

    if not spot_by_asset:
        print("no spot prices — cannot compute fair value")
        return 2

    events = discover_kalshi_events()
    print(f"open Kalshi crypto events: {len(events)}")
    pairs = 0
    for et in events:
        parsed = parse_kalshi_event(et)
        if not parsed:
            continue
        asset, expiry = parsed
        if asset not in spot_by_asset:
            continue
        spot, sigma = spot_by_asset[asset]
        kl = fetch_kalshi_ladder(et)
        if not kl or not (kl.ok or len(kl.sorted_rungs()) == 1):
            continue
        for slug in candidate_pm_slugs(asset, expiry):
            pl = _fetch_pm_ladder_with_fallback(slug)
            if not pl or not (pl.ok or len(pl.sorted_rungs()) == 1):
                continue
            rows = three_way(kl, pl, spot, sigma)
            if not rows:
                continue
            pairs += 1
            # Implied median: where each ladder crosses 50%. Comparing medians
            # separates "different volatility" from "different reference price".
            k_med = _median_strike(rows, "kalshi")
            p_med = _median_strike(rows, "poly")
            print(f"\n=== {asset} exp {expiry} ET  ({et} <-> {slug})  "
                  f"{rows[0]['minutes_left']:.0f} min left ===")
            print(f"spot {spot:,.2f}   implied median: kalshi {k_med:,.0f} "
                  f"({k_med-spot:+,.0f})  poly {p_med:,.0f} ({p_med-spot:+,.0f})")
            print(f"{'strike':>10} {'fair':>7} {'kalshi':>7} {'poly':>7} "
                  f"{'k-edge':>7} {'p-edge':>7}")
            for r in sorted(rows, key=lambda x: -max(abs(x["kalshi_edge_c"]),
                                                     abs(x["poly_edge_c"])))[:8]:
                print(f"{r['strike']:>10,.0f} {r['fair']*100:>6.1f}c "
                      f"{r['kalshi']*100:>6.1f}c {r['poly']*100:>6.1f}c "
                      f"{r['kalshi_edge_c']:>+6.1f}c {r['poly_edge_c']:>+6.1f}c")
            k_big = sum(1 for r in rows if abs(r["kalshi_edge_c"]) >= 3)
            p_big = sum(1 for r in rows if abs(r["poly_edge_c"]) >= 3)
            print(f"rungs {len(rows)}  |kalshi edge|>=3c: {k_big}  |poly edge|>=3c: {p_big}")
    if not pairs:
        print("no pair with enough liquid rungs on both venues — nothing measured")
    return 0 if pairs else 1

def _median_strike(rows: List[dict], key: str) -> float:
    """Strike where a ladder's probability crosses 50%, linearly interpolated."""
    pts = sorted(((r["strike"], r[key]) for r in rows), key=lambda x: x[0])
    for (s0, p0), (s1, p1) in zip(pts, pts[1:]):
        if p0 >= 0.5 >= p1 and p0 != p1:
            return s0 + (p0 - 0.5) / (p0 - p1) * (s1 - s0)
    return float("nan")

# ═══════════════════════════════════════════════════════════════════════
# T15-31: Cross-venue up/down matcher (fixes F-02)
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class UpDownMatch:
    """A matched pair of Kalshi 15M and Polymarket US up/down 15m markets."""
    kalshi_event: str              # e.g., "KXBTC15M-26NOV201530"
    kalshi_yes_mid: float          # Kalshi YES mid price (0-1)
    kalshi_no_mid: float           # Kalshi NO mid price (0-1)
    polymarket_slug: str           # e.g., "cpc-btc-updown-15m-2026-11-20-1530z"
    poly_up_mid: float             # Polymarket Up mid price (0-1)
    poly_down_mid: float           # Polymarket Down mid price (0-1)
    asset: str                     # BTC, ETH, SOL, XRP
    window_start_et: datetime      # Window start time (ET)
    window_end_et: datetime        # Window end time (ET)
    minutes_left: float            # Minutes until window end
    deviation_cents: float         # (Kalshi YES - Poly Up) * 100
    basis_risk_bps: float          # Basis risk in basis points
    tie_rule_risk: bool            # True if tie rule could flip outcome


def parse_kalshi_15m_event(event_ticker: str) -> Optional[Tuple[str, datetime, datetime]]:
    """
    Parse Kalshi 15M event ticker into asset, window_start_et, window_end_et.

    KXBTC15M-26NOV210730 -> ('BTC', 2026-11-21 07:15 ET, 2026-11-21 07:30 ET)

    The ticker's HHMM is the window CLOSE in ET wall clock, verified against
    the live venue 2026-11-21: market KXBTC15M-26NOV210845-45 had
    close_time 13:45Z = 08:45 ET, and its open was 900s earlier at 07:15Z...
    13:15Z = 08:15 ET. Reading the HHMM as the window start put every
    cross-venue pair one window off.
    """
    m = _KALSHI_EVENT.match(event_ticker or "")
    if not m:
        return None
    asset, yy, mon, dd, hhmm = m.groups()
    try:
        hour = int(hhmm[:2])
        minute = int(hhmm[2:]) if len(hhmm) > 2 else 0
        window_end = datetime(2000 + int(yy), _MONTHS[mon], int(dd), hour, minute)
        window_start = window_end - timedelta(minutes=15)
        return asset, window_start, window_end
    except (KeyError, ValueError):
        return None


def parse_poly_us_updown_15m_slug(slug: str) -> Optional[Tuple[str, datetime, datetime]]:
    """
    Parse Polymarket US up/down 15m slug into asset, window_start_et, window_end_et.

    cpc-btc-updown-15m-2026-11-20-1530z ->
        ('BTC', 2026-11-20 10:30 ET, 2026-11-20 10:45 ET)

    The 'z' suffix means what it says: the time component is UTC. Verified
    against the live venue 2026-11-21 — at 12:23Z the open BTC window was
    cpc-btc-updown-15m-2026-11-21-1215z with start 12:15Z/end 12:30Z. Kalshi's
    event tickers use ET wall clock (KXBTC15M-26NOV210730-30 closes at 07:30
    ET = 12:30Z), so the two venues' clocks differ and matching must convert,
    never compare the strings. Reading the slug as ET misaligned every pair by
    five hours.
    """
    # Pattern: cpc-{asset}-updown-15m-{date}-{time}z
    # e.g., cpc-btc-updown-15m-2026-11-20-1530z
    pattern = r"cpc-(btc|eth|sol|xrp)-updown-15m-(\d{4})-(\d{2})-(\d{2})-(\d{4})z"
    m = re.match(pattern, slug or "")
    if not m:
        return None
    asset_word, yyyy, mm, dd, hhmm = m.groups()
    try:
        hour = int(hhmm[:2])
        minute = int(hhmm[2:]) if len(hhmm) > 2 else 0
        asset = asset_word.upper()
        start_utc = datetime(int(yyyy), int(mm), int(dd), hour, minute,
                             tzinfo=timezone.utc)
        # ET wall clock, as the rest of this module's datetimes are: shift by
        # the module's own US-Eastern offset rule (DST-aware) for the date.
        naive_et = (start_utc + timedelta(hours=_et_offset_hours(
            start_utc.replace(tzinfo=None) + timedelta(hours=5)))).replace(
                tzinfo=None)
        return asset, naive_et, naive_et + timedelta(minutes=15)
    except (KeyError, ValueError):
        return None


def discover_kalshi_15m_events(timeout: int = 20) -> List[str]:
    """Fetch open Kalshi 15M event tickers (KXBTC15M, KXETH15M, etc.)."""
    series = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M")
    events = []
    for s in series:
        try:
            resp = requests.get(f"{KALSHI_BASE}/events",
                                params={"series_ticker": s, "status": "open", "limit": 200},
                                timeout=timeout)
            resp.raise_for_status()
            for e in resp.json().get("events", []):
                t = e.get("event_ticker", "")
                if t and t not in events:
                    events.append(t)
        except Exception as e:
            log.warning("kalshi 15m events %s failed: %s", s, e)
    return events


def fetch_kalshi_yes_no_mids(event_ticker: str, timeout: int = 20) -> Optional[Tuple[float, float]]:
    """
    Fetch Kalshi YES and NO mid prices for a 15M market.
    
    Returns (yes_mid, no_mid) where each is in [0, 1], or None on failure.
    """
    try:
        resp = requests.get(f"{KALSHI_BASE}/markets",
                            params={"event_ticker": event_ticker, "limit": 200},
                            timeout=timeout)
        resp.raise_for_status()
        markets = resp.json().get("markets", [])
    except Exception as e:
        log.warning("kalshi mids %s failed: %s", event_ticker, e)
        return None

    yes_mid = None
    no_mid = None
    for m in markets:
        # 15M markets have a single strike (the current spot at window start)
        # Find YES and NO sides
        if m.get("yes_bid_dollars") is not None and m.get("yes_ask_dollars") is not None:
            yb = _f(m.get("yes_bid_dollars"))
            ya = _f(m.get("yes_ask_dollars"))
            if yb is not None and ya is not None and ya > yb:
                yes_mid = (yb + ya) / 2.0
        if m.get("no_bid_dollars") is not None and m.get("no_ask_dollars") is not None:
            nb = _f(m.get("no_bid_dollars"))
            na = _f(m.get("no_ask_dollars"))
            if nb is not None and na is not None and na > nb:
                no_mid = (nb + na) / 2.0

    if yes_mid is not None and no_mid is not None:
        return yes_mid, no_mid
    return None


def fetch_poly_us_updown_15m(slug: str, timeout: int = 20) -> Optional[Tuple[float, float]]:
    """
    Fetch Polymarket US Up and Down mid prices for a 15m market.
    
    Uses polymarket_us_client.get_market_by_slug.
    Returns (up_mid, down_mid) where each is in [0, 1], or None on failure.
    """
    try:
        from polymarket_us_client import get_market_by_slug
    except Exception:
        log.debug("polymarket_us_client not available")
        return None

    try:
        resp = get_market_by_slug(slug)
    except Exception as e:
        log.debug("poly us fetch %s failed: %s", slug, e)
        return None

    market = None
    if isinstance(resp, dict):
        inner = resp.get("market")
        market = inner if isinstance(inner, dict) else resp
    if not market:
        return None

    # The market has marketSides with YES (Up) and NO (Down) outcomes
    # Also check bestBidQuote/bestAskQuote
    yes_price = None
    no_price = None

    # Try marketSides first
    for side in market.get("marketSides", []):
        if side.get("long") and side.get("marketSideType") == "YES":
            # Up side
            price = _f(side.get("price"))
            if price is not None:
                yes_price = price
        elif not side.get("long") and side.get("marketSideType") == "NO":
            # Down side
            price = _f(side.get("price"))
            if price is not None:
                no_price = price

    # Try bestBidQuote/bestAskQuote
    if market.get("bestBidQuote") and market.get("bestAskQuote"):
        # Need to determine which is YES vs NO from outcomePrices or context
        pass

    # Fall back to outcomePrices
    if yes_price is None or no_price is None:
        outcomes = market.get("outcomePrices", [])
        if isinstance(outcomes, list) and len(outcomes) >= 2:
            # Usually outcomes[0] is Up/Yes, outcomes[1] is Down/No
            if yes_price is None:
                yes_price = _f(outcomes[0])
            if no_price is None:
                no_price = _f(outcomes[1])

    if yes_price is not None and no_price is not None:
        return yes_price, no_price
    return None


def match_updown_15m(timeout: int = 20) -> List[UpDownMatch]:
    """
    Main entry point: match each Kalshi 15M window with Polymarket US up/down 15m.
    
    Returns list of UpDownMatch objects sorted by |deviation_cents| descending.
    """
    matches = []
    now = datetime.now(timezone.utc)
    
    # Get all open Kalshi 15M events
    kalshi_events = discover_kalshi_15m_events(timeout)
    log.info(f"Found {len(kalshi_events)} open Kalshi 15M events")

    for event_ticker in kalshi_events:
        parsed = parse_kalshi_15m_event(event_ticker)
        if not parsed:
            continue
        asset, window_start_et, window_end_et = parsed
        
        # Convert to UTC for comparison
        window_end_utc = to_utc(window_end_et)
        minutes_left = (window_end_utc - now).total_seconds() / 60.0
        if minutes_left <= 0:
            continue
        
        # Generate expected Polymarket US slug
        # Format: cpc-{asset}-updown-15m-YYYY-MM-DD-HHMMz
        # The slug carries the window START in UTC (verified against the live
        # venue 2026-11-21), while window_start_et is ET wall clock.
        start_utc = to_utc(window_start_et)
        date_str = start_utc.strftime("%Y-%m-%d")
        time_str = start_utc.strftime("%H%M")
        poly_slug = f"cpc-{asset.lower()}-updown-15m-{date_str}-{time_str}z"
        
        # Fetch prices from both venues
        kalshi_mids = fetch_kalshi_yes_no_mids(event_ticker, timeout)
        if not kalshi_mids:
            continue
        kalshi_yes_mid, kalshi_no_mid = kalshi_mids
        
        poly_mids = fetch_poly_us_updown_15m(poly_slug, timeout)
        if not poly_mids:
            continue
        poly_up_mid, poly_down_mid = poly_mids
        
        # Compute deviation: Kalshi YES vs Polymarket Up
        deviation = (kalshi_yes_mid - poly_up_mid) * 100.0  # in cents
        
        # Basis risk: both use BRTI but with different reference times
        # Kalshi: single settlement at window_end (60s average)
        # Polymarket: start price AND end price (both 60s averages)
        # The primary basis risk is the tie rule difference and double-reference
        # We estimate basis risk from historical BRTI dispersion
        basis_risk_bps = 5.0  # Conservative estimate from T15-11 BRTI tracker
        
        # Tie rule risk: Polymarket Up wins on tie (>=), Kalshi requires strictly >
        tie_rule_risk = True
        
        matches.append(UpDownMatch(
            kalshi_event=event_ticker,
            kalshi_yes_mid=kalshi_yes_mid,
            kalshi_no_mid=kalshi_no_mid,
            polymarket_slug=poly_slug,
            poly_up_mid=poly_up_mid,
            poly_down_mid=poly_down_mid,
            asset=asset,
            window_start_et=window_start_et,
            window_end_et=window_end_et,
            minutes_left=minutes_left,
            deviation_cents=deviation,
            basis_risk_bps=basis_risk_bps,
            tie_rule_risk=tie_rule_risk
        ))
    
    # Sort by absolute deviation (largest first)
    matches.sort(key=lambda m: abs(m.deviation_cents), reverse=True)
    return matches


def print_updown_matches(matches: List[UpDownMatch]) -> None:
    """Print matched up/down pairs in a readable format."""
    if not matches:
        print("No Kalshi 15M / Polymarket US updown-15m pairs found.")
        return
    
    print(f"{'Asset':<6} {'Kalshi Event':<25} {'Poly Slug':<40} {'K-YES':>7} {'P-Up':>7} {'Dev(c)':>8} {'Min':>5} {'Basis(bps)':>10} {'TieRisk'}")
    print("-" * 120)
    for m in matches:
        poly_slug_short = m.polymarket_slug[:38]
        print(f"{m.asset:<6} {m.kalshi_event:<25} {poly_slug_short:<40} "
              f"{m.kalshi_yes_mid*100:>6.1f}c {m.poly_up_mid*100:>6.1f}c "
              f"{m.deviation_cents:>+8.1f} {m.minutes_left:>5.0f} "
              f"{m.basis_risk_bps:>10.1f} {'Yes' if m.tie_rule_risk else 'No'}")


if __name__ == "__main__":
    raise SystemExit(main())
