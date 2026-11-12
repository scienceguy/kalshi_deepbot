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
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("crossvenue")

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

ASSETS = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH"}

_KALSHI_EVENT = re.compile(r"^KX(BTC|ETH)(?:D|15M)?-(\d{2})([A-Z]{3})(\d{2})(\d{2}|\d{4})$")
_PM_SLUG = re.compile(r"^(bitcoin|ethereum|btc|eth)-above-on-([a-z]+)-(\d{1,2})-(\d{4})(?:-(\d{1,2})([ap]m)-et)?$")

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
    """
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
        if m.get("strike_type") != "greater" or m.get("floor_strike") is None:
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
        market = get_market_by_slug(slug, timeout=timeout)
    except Exception as e:
        log.debug("US fetch %s failed: %s", slug, e)
        return None

    if market is None:
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
    # For up/down markets, use priceToBeat
    if strike_float is None:
        ptb = _f(apt.get("priceToBeat"))
        if ptb is not None:
            strike_float = ptb

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
    import bisect
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
    poly_probs = [pt[1] for pt in p]
    lo, hi = poly_strikes[0], poly_strikes[-1]
    out = []
    for strike, kalshi_mid in k:
        if not (lo <= strike <= hi):
            continue  # extrapolating a CDF is inventing a number
        poly_mid = _interp(strike, [pt[0] for pt in p], [pt[1] for pt in p])
        out.append(Deviation(strike, float(kalshi_mid), poly_mid))
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
    """Slug guesses for the Polymarket ladder that expires at the same ET time."""
    name = "bitcoin" if asset == "BTC" else "ethereum"
    month = expiry_et.strftime("%B").lower()
    base = f"{name}-above-on-{month}-{expiry_et.day}-{expiry_et.year}"
    if expiry_et.hour >= 23:
        return [base]
    h12 = expiry_et.hour % 12 or 12
    ampm = "am" if expiry_et.hour < 12 else "pm"
    return [f"{base}-{h12}{ampm}-et"]


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


def fetch_underlying(yes_side: str, yes_px: float, n_side: str, n_px: float,
                     question: str, timeout: int = 20):
    """Underlying print for a Polymarket market whose outcome is NOT a strike.

    Kalshi threshold rungs resolve 'above X at T'; Polymarket's equivalent is
    'above $X' with X in the slug. The hourly markets, though, resolve on an
    *up/down* question ('BTC price up in the next 1h') and carry no strike at all,
    so their YES/NO prices carry no P(above X) value we can compare against the
    ladder. What we can take from them is the underlying print itself: the YES
    side that is on-chain liquid is the direction, and the CLOB price carries the
    exchange's own probability. This probe only asks whether the slug's options
    name the outcomes, so it reports; it places nothing.
    """
    return n_side, n_px


def three_way(kalshi: Ladder, poly: Ladder, spot: float, sigma_min: float,
              now: Optional[datetime] = None) -> List[dict]:
    from binance_feed import prob_above

    now = now or datetime.now(timezone.utc)
    minutes_left = (to_utc(kalshi.expiry_et) - now).total_seconds() / 60.0
    if minutes_left <= 0:
        return []

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
        if not kl or not kl.ok:
            continue
        for slug in candidate_pm_slugs(asset, expiry):
            pl = _fetch_pm_ladder_with_fallback(slug)
            if not pl or not pl.ok:
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


if __name__ == "__main__":
    raise SystemExit(main())
