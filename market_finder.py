import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import config
from kalshi_client import KalshiClient, KalshiAPIError

log = logging.getLogger(__name__)


def _settle_yes_cents(market: dict) -> int | None:
    """YES payout in cents (0 or 100) from a market record, or None if unknown.

    NEW schema: ``settlement_value_dollars`` ("1.0000"/"0.0000") or ``result``
    ("yes"/"no"). The legacy ``expiration_value`` is the raw underlying price
    (e.g. a BTC price like "78029.99"), NOT a payout — reading it as 0/100 made
    every settlement look invalid and silently dropped realized PnL. A raw
    ``expiration_value`` that already equals 0 or 100 is accepted as a fallback.
    """
    sv = market.get("settlement_value_dollars")
    if sv not in (None, ""):
        try:
            return int(round(float(sv) * 100))
        except (TypeError, ValueError):
            pass
    r = market.get("result")
    if r == "yes":
        return 100
    if r == "no":
        return 0
    # Legacy fallback: expiration_value is the raw underlying price, NOT a payout.
    # Only accept it if it already reads as 0 or 100 (a payout in disguise).
    ev = market.get("expiration_value")
    if ev not in (None, ""):
        try:
            v = int(round(float(ev) * 100))
            if v in (0, 100):
                return v
        except (TypeError, ValueError):
            pass
    return None


@dataclass
class MarketInfo:
    ticker: str; series: str; close_time: datetime
    strike: float; strike_type: str
    yes_bid: int; yes_ask: int; no_bid: int; no_ask: int
    yes_liquidity: int; no_liquidity: int
    minutes_left: float = 0.0
    bracket_role: str = "MAIN"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def mid_yes(self):
        # Treat 0 as "no quote" (valid prices are 1-99)
        yb = self.yes_bid if self.yes_bid else None
        ya = self.yes_ask if self.yes_ask else None
        if yb is not None and ya is not None:
            return (yb + ya) / 2
        if ya is not None:
            return float(ya)
        if yb is not None:
            return float(yb)
        return None

    @property
    def mid_no(self):
        # Treat 0 as "no quote" (valid prices are 1-99)
        nb = self.no_bid if self.no_bid else None
        na = self.no_ask if self.no_ask else None
        if nb is not None and na is not None:
            return (nb + na) / 2
        if na is not None:
            return float(na)
        if nb is not None:
            return float(nb)
        return None

    @staticmethod
    def compute_mid(yes_bid: int, yes_ask: int):
        """Shared mid-price helper: matches MarketInfo.mid_yes exactly."""
        # Treat 0 as "no quote" (valid prices are 1-99)
        yb = yes_bid if yes_bid else None
        ya = yes_ask if yes_ask else None
        if yb is not None and ya is not None:
            return (yb + ya) / 2
        if ya is not None:
            return float(ya)
        if yb is not None:
            return float(yb)
        return None

    @property
    def skew(self):
        yb = self.yes_bid if self.yes_bid else None
        nb = self.no_bid if self.no_bid else None
        if yb is not None and nb is not None:
            return yb - nb
        return 0

    @property
    def spread(self):
        yb = self.yes_bid if self.yes_bid else None
        ya = self.yes_ask if self.yes_ask else None
        return (ya - yb) if (ya is not None and yb is not None) else 99

def _parse_api_time(v):
    if not v: return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

def _dollars_to_cents(v):
    """Kalshi v2 *_dollars fields are decimal strings, always dollars.

    Parsing by magnitude (``if f < 1``) silently turned a "1.0000" dollar field
    into 1 cent, which made an unsellable side look like a ~50c market.
    """
    if v in (None, ""):
        return 0
    try:
        return int(round(float(v) * 100))
    except (ValueError, TypeError):
        return 0


def _price_field(mkt, dollars_key, legacy_key):
    """Prefer the dollar-denominated field; fall back to the legacy cents int."""
    v = mkt.get(dollars_key)
    if v not in (None, ""):
        return _dollars_to_cents(v)
    return _q(mkt.get(legacy_key))


def _q(v):
    if v in (None, ""): return 0
    try:
        f = float(v)
        if f < 1.0:  # looks like dollars
            return int(round(f * 100))
        return int(f)  # looks like cents
    except (ValueError, TypeError):
        return 0

def _parse_market_prices(mkt):
    """
    Extract yes/no bid/ask in cents from a raw market dict.
    Kalshi v2 uses _dollars suffix fields with string decimal values.
    """
    # Primary: *_dollars fields (confirmed from live API)
    yb = _price_field(mkt, "yes_bid_dollars", "yes_bid")
    ya = _price_field(mkt, "yes_ask_dollars", "yes_ask")
    nb = _price_field(mkt, "no_bid_dollars",  "no_bid")
    na = _price_field(mkt, "no_ask_dollars",  "no_ask")

    yq = _q(mkt.get("yes_bid_size_fp") or mkt.get("yes_bid_size"))
    nq = _q(mkt.get("no_bid_size_fp")  or mkt.get("no_bid_size"))
    return yb, ya, nb, na, yq, nq

def _build(mkt, series, ct, ml, ob, role="MAIN"):
    # Start with raw market prices
    yb, ya, nb, na, yq, nq = _parse_market_prices(mkt)

    # Override with orderbook if available (more accurate)
    ys = ob.get("yes", []); ns = ob.get("no", [])
    if ys:
        yb = ys[0][0]; yq = ys[0][1]
    if ns:
        nb = ns[0][0]; nq = ns[0][1]

    # Validate / clamp: prices must be 0-100, liquidity must be >= 0
    clamped = []
    for name, val, lo, hi in (
        ("yes_bid", yb, 0, 100), ("yes_ask", ya, 0, 100),
        ("no_bid",  nb, 0, 100), ("no_ask",  na, 0, 100),
    ):
        if val < lo or val > hi:
            clamped.append(f"{name}={val}→{max(lo, min(hi, val))}")
    for name, val in (("yes_liquidity", yq), ("no_liquidity", nq)):
        if val < 0:
            clamped.append(f"{name}={val}→0")
    if clamped:
        log.warning("Clamped invalid values for %s: %s", mkt.get("ticker", "?"), ", ".join(clamped))

    yb = max(0, min(100, yb)); ya = max(0, min(100, ya))
    nb = max(0, min(100, nb)); na = max(0, min(100, na))
    yq = max(0, yq); nq = max(0, nq)

    strike_val = mkt.get("floor_strike")
    if strike_val is None:
        strike_val = mkt.get("strike", 0)
    strike=float(strike_val)

    return MarketInfo(
        ticker=mkt.get("ticker",""), series=series, close_time=ct,
        strike=strike,
        strike_type=str(mkt.get("strike_type") or ""),
        yes_bid=yb, yes_ask=ya, no_bid=nb, no_ask=na,
        yes_liquidity=yq, no_liquidity=nq,
        minutes_left=ml, bracket_role=role, raw=mkt)

def _fetch_orderbook(client: KalshiClient, ticker: str) -> dict:
    """Fetch orderbook for a single market, returning {} on failure."""
    try:
        return client.get_orderbook(ticker, depth=5)
    except KalshiAPIError as e:
        log.warning("Orderbook fetch failed for %s: %s", ticker, e)
        return {}
    except Exception as e:
        log.warning("Unexpected error fetching orderbook for %s: %s", ticker, e)
        return {}


def _fetch_series_markets(client: KalshiClient, series: str, now: datetime,
                          MIN_P: int, MAX_P: int, HMAX: int,
                          seen: set, seen_lock: threading.Lock) -> tuple:
    """Fetch and filter markets for a single series."""
    markets = None
    for attempt in range(3):
        try:
            markets = client.get_markets(series_ticker=series, status="open", limit=50)
            break
        except Exception as e:
            if attempt < 2:
                wait = 0.5 * (2 ** attempt)
                log.warning("Fetch failed %s (attempt %d/3): %s — retrying in %.1fs",
                            series, attempt + 1, e, wait)
                time.sleep(wait)
            else:
                log.warning("Fetch failed %s: %s", series, e)

    if not markets:
        return [], [], 0

    log.debug("Series %s: %d markets returned", series, len(markets))

    mains = []; hedges = []
    for mkt in markets:
        ticker = mkt.get("ticker","")
        with seen_lock:
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)

        status = str(mkt.get("status","")).lower()
        if status not in ("active", "open"): continue

        ct = _parse_api_time(mkt.get("close_time"))
        if not ct: continue
        ml = (ct - now).total_seconds() / 60
        if ml <= 0 or ml > 15.5: continue

        yb, ya, nb, na, yq, nq = _parse_market_prices(mkt)
        # Float mid matches MarketInfo.mid_yes so the pre-filter and the final
        # band check agree on the same value (no rounding boundary flips).
        mid = MarketInfo.compute_mid(yb, ya)

        log.debug("  %s ml=%.1f mid=%.1fc ya=%dc yb=%dc", ticker, ml, mid, ya, yb)

        if mid is not None and MIN_P <= mid <= MAX_P:
            mains.append((mkt, ct, ml, mid))
        elif yb is not None and yb <= 100 - MIN_P:
            # NO side may be the favourite (YES bid low -> NO ask high).
            # YES mid isn't in band but NO ask = 100 - YES bid could be >= 75c.
            mains.append((mkt, ct, ml, mid))
        
        # Hedge check is independent of main check
        # Skip if yes_ask is 0 or missing (no liquidity to hedge against)
        if ya <= 0:
            pass
        elif 1 <= ya <= HMAX:
            hedges.append((mkt, ct, ml, ya))

    return mains, hedges, len(markets)


class _MarketResult(list):
    """list of tradeable MarketInfo with the availability counts attached.

    A plain list subclass keeps every existing caller working unchanged
    (``if not markets``, ``len(markets)``, iteration, ``== []``) while also
    exposing ``.total_available`` and ``.tradable_count``.
    """

    def __init__(self, iterable=(), total_available=0, tradable_count=0, extra=()):
        super().__init__(iterable)
        self.total_available = total_available
        self.tradable_count = tradable_count
        # Markets whose YES mid sits in the caller's extra_band. Kept apart from
        # the list itself so the favourite strategy never sees them.
        self.extra = list(extra)


def find_active_markets(client: KalshiClient, extra_band=None):
    now = datetime.now(timezone.utc)
    result = []
    total_available = 0
    MIN_P = config.MIN_YES_PRICE_CENTS
    MAX_P = config.MAX_YES_PRICE_CENTS
    HMAX  = config.HEDGE_YES_ASK_MAX
    # extra_band=(lo, hi) widens only the *fetch* stage so mid-range markets get
    # an orderbook; the favourite entry band (MIN_P/MAX_P) is untouched.
    FETCH_MIN_P, FETCH_MAX_P = MIN_P, MAX_P
    if extra_band:
        FETCH_MIN_P = min(MIN_P, int(extra_band[0]))
        FETCH_MAX_P = max(MAX_P, int(extra_band[1]))

    # Fetch markets for all series concurrently (bounded pool), then
    # fetch orderbooks concurrently below. This avoids spending several
    # seconds on sequential per-series fetches inside the 30s cycle timeout.
    all_mains = []; all_hedges = []; seen = set()
    seen_lock = threading.Lock()

    def _fetch_one(series: str):
        mains, hedges, n = _fetch_series_markets(client, series, now,
                                                  FETCH_MIN_P, FETCH_MAX_P, HMAX, seen, seen_lock)
        return mains, hedges, n

    # Fetch markets for all series concurrently (bounded pool), then
    # fetch orderbooks concurrently below. This avoids spending several
    # seconds on sequential per-series fetches inside the 30s cycle timeout.
    with ThreadPoolExecutor(max_workers=min(len(config.TARGET_SERIES), 4)) as ex:
        futs = {ex.submit(_fetch_one, s): s for s in config.TARGET_SERIES}
        for fut in as_completed(futs):
            mains, hedges, n = fut.result()
            total_available += n
            all_mains.extend(mains)
            all_hedges.extend(hedges)

    # Fetch orderbooks for ALL mains from the /markets fetch (not just those
    # passing the stale band filter). Stale prices from /markets can mis-filter:
    # a market that passes on stale data gets an orderbook fetch; one that fails
    # on stale data is dropped before the orderbook can correct it.
    # Use a permissive initial band (1-99) for orderbook fetching, then apply
    # the real entry band after fresh prices are available.
    FETCH_BAND_MIN = 1
    FETCH_BAND_MAX = 99
    fetch_list = []  # (mkt, ct, ml, role, ya)
    for mkt, ct, ml, mid in all_mains:
        if mid is not None and FETCH_BAND_MIN <= mid <= FETCH_BAND_MAX:
            fetch_list.append((mkt, ct, ml, "MAIN", None))

    all_hedges.sort(key=lambda x: x[3])
    for mkt, ct, ml, ya in all_hedges[:config.MAX_HEDGE_PER_SERIES * len(config.TARGET_SERIES)]:
        fetch_list.append((mkt, ct, ml, "HEDGE_NO", ya))

    # Fetch orderbooks for all rungs passing the permissive fetch band.
    # Cap workers so we don't burst past Kalshi's ~10 req/s rate limit.
    ob_map = {}
    if fetch_list:
        with ThreadPoolExecutor(max_workers=min(len(fetch_list), 4)) as ex:
            futs = {ex.submit(_fetch_orderbook, client, m["ticker"]): (m, ct, ml, role, ya)
                    for m, ct, ml, role, ya in fetch_list}
            for fut in as_completed(futs):
                m, ct, ml, role, ya = futs[fut]
                try:
                    ob_map[m["ticker"]] = (fut.result(), ya)
                except Exception:
                    ob_map[m["ticker"]] = ({}, ya)

    extra = []
    # Process ALL mains passing the REAL entry band (using fresh orderbook prices).
    # A market is tradeable if YES ask is in band OR NO ask is in band
    # (final-minute taker pays the ask on the favourite side).
    for mkt, ct, ml, mid in all_mains:
        ob = ob_map.get(mkt["ticker"], ({}, None))[0]
        series = mkt.get("series_ticker", "")
        info = _build(mkt, series, ct, ml, ob, "MAIN")
        yes_ask = info.yes_ask
        no_ask = info.no_ask
        if extra_band and info.mid_yes is not None \
                and extra_band[0] <= info.mid_yes <= extra_band[1]:
            extra.append(info)
        yes_tradeable = yes_ask and MIN_P <= yes_ask <= MAX_P
        no_tradeable = no_ask and MIN_P <= no_ask <= MAX_P
        if yes_tradeable or no_tradeable:
            result.append(info)
            side = "YES" if yes_tradeable else "NO"
            price = yes_ask if yes_tradeable else no_ask
            log.info("✓ MAIN  %s %.1fmin %s ask=%dc mid=%.0fc bid=%dc skew=%+dc",
                     info.ticker, ml, side, price, info.mid_yes or 0, info.yes_bid, info.skew)
        else:
            log.debug("MAIN failed real-price check yes_ask=%dc no_ask=%dc: %s",
                      yes_ask or 0, no_ask or 0, mkt["ticker"])

    # Process HEDGE_NO
    for mkt, ct, ml, ya in all_hedges[:config.MAX_HEDGE_PER_SERIES * len(config.TARGET_SERIES)]:
        ob, hedge_ya = ob_map.get(mkt["ticker"], ({}, ya))
        series = mkt.get("series_ticker", "")
        info = _build(mkt, series, ct, ml, ob, "HEDGE_NO")
        # Use the pre-filter yes_ask (ya) for hedge check, not the orderbook yes_ask
        # which might be 0/stale if orderbook fetch failed
        if 0 < hedge_ya <= config.HEDGE_YES_ASK_MAX:
            result.append(info)
            log.info("✓ HEDGE %s %.1fmin yes_ask=%dc no_ask=%dc prob≈%d%%",
                     info.ticker, ml, info.yes_ask, info.no_ask, 100 - info.no_ask)
        else:
            log.debug("Hedge filtered hedge_ya=%dc: %s", hedge_ya, mkt["ticker"])

    tradable_count = len(result)
    log.info("Found %d tradeable brackets out of %d total available markets",
             tradable_count, total_available)
    return _MarketResult(result, total_available=total_available,
                         tradable_count=tradable_count, extra=extra)
