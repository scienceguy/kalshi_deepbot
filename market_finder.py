import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import config
from kalshi_client import KalshiClient

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
        if self.yes_bid and self.yes_ask: return (self.yes_bid + self.yes_ask) / 2
        return float(self.yes_ask or self.yes_bid or 50)

    @property
    def skew(self): return self.yes_bid - self.no_bid

    @property
    def spread(self):
        return (self.yes_ask - self.yes_bid) if (self.yes_ask and self.yes_bid) else 99

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
        return int(float(v))
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

    return MarketInfo(
        ticker=mkt.get("ticker",""), series=series, close_time=ct,
        strike=float(mkt.get("floor_strike") or mkt.get("strike") or 0),
        strike_type=str(mkt.get("strike_type") or ""),
        yes_bid=yb, yes_ask=ya, no_bid=nb, no_ask=na,
        yes_liquidity=yq, no_liquidity=nq,
        minutes_left=ml, bracket_role=role, raw=mkt)

def _fetch_orderbook(client: KalshiClient, ticker: str) -> dict:
    """Fetch orderbook for a single market, returning {} on failure."""
    try:
        return client.get_orderbook(ticker, depth=5)
    except Exception:
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
        return [], []

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
        mid = (yb + ya) / 2 if (yb and ya) else (ya or yb or 0)

        log.debug("  %s ml=%.1f mid=%.1fc ya=%dc yb=%dc", ticker, ml, mid, ya, yb)

        if MIN_P <= mid <= MAX_P:
            mains.append((mkt, ct, ml, mid))
        elif 1 <= ya <= HMAX:
            hedges.append((mkt, ct, ml, ya))

    return mains, hedges


def find_active_markets(client: KalshiClient):
    now = datetime.now(timezone.utc)
    result = []
    MIN_P = config.MIN_YES_PRICE_CENTS
    MAX_P = config.MAX_YES_PRICE_CENTS
    HMAX  = config.HEDGE_YES_ASK_MAX

    # Fetch markets for all series concurrently (bounded pool), then
    # fetch orderbooks concurrently below. This avoids spending several
    # seconds on sequential per-series fetches inside the 30s cycle timeout.
    all_mains = []; all_hedges = []; seen = set()
    seen_lock = threading.Lock()

    def _fetch_one(series: str):
        mains, hedges = _fetch_series_markets(client, series, now,
                                              MIN_P, MAX_P, HMAX, seen, seen_lock)
        return mains, hedges

    # Fetch markets for all series concurrently (bounded pool), then
    # fetch orderbooks concurrently below. This avoids spending several
    # seconds on sequential per-series fetches inside the 30s cycle timeout.
    with ThreadPoolExecutor(max_workers=min(len(config.TARGET_SERIES), 4)) as ex:
        for mains, hedges in ex.map(_fetch_one, config.TARGET_SERIES):
            all_mains.extend(mains)
            all_hedges.extend(hedges)

    # Fetch orderbooks concurrently for all rungs passing the band filter,
    fetch_list = []  # (mkt, ct, ml, role)
    for mkt, ct, ml, mid in all_mains:
        fetch_list.append((mkt, ct, ml, "MAIN"))

    all_hedges.sort(key=lambda x: x[3])
    for mkt, ct, ml, ya in all_hedges[:config.MAX_HEDGE_PER_SERIES * len(config.TARGET_SERIES)]:
        fetch_list.append((mkt, ct, ml, "HEDGE_NO"))

    # Fetch all orderbooks concurrently instead of one HTTP call per market.
    # Cap workers so we don't burst past Kalshi's ~10 req/s rate limit and
    # trigger 429s when many markets are candidates.
    ob_map = {}
    if fetch_list:
        with ThreadPoolExecutor(max_workers=min(len(fetch_list), 4)) as ex:
            futs = {ex.submit(_fetch_orderbook, client, m["ticker"]): (m, ct, ml, role)
                    for m, ct, ml, role in fetch_list}
            for fut in as_completed(futs):
                m, ct, ml, role = futs[fut]
                try:
                    ob_map[m["ticker"]] = fut.result()
                except Exception:
                    ob_map[m["ticker"]] = {}

    # Process ALL mains passing the entry band (multi-market mode).
    # Earlier versions picked the single closest to 50c; now we keep all
    # that pass the entry band to maximize opportunity.
    for mkt, ct, ml, mid in all_mains:
        ob = ob_map.get(mkt["ticker"], {})
        series = mkt.get("series_ticker", "")
        info = _build(mkt, series, ct, ml, ob, "MAIN")
        if MIN_P <= info.mid_yes <= MAX_P:
            result.append(info)
            log.info("✓ MAIN  %s %.1fmin mid=%.0fc bid=%dc ask=%dc skew=%+dc",
                     info.ticker, ml, info.mid_yes, info.yes_bid, info.yes_ask, info.skew)
        else:
            log.debug("MAIN failed real-price check mid=%.0fc: %s", info.mid_yes, mkt["ticker"])

    # Process HEDGE_NO
    for mkt, ct, ml, ya in all_hedges[:config.MAX_HEDGE_PER_SERIES * len(config.TARGET_SERIES)]:
        ob = ob_map.get(mkt["ticker"], {})
        series = mkt.get("series_ticker", "")
        info = _build(mkt, series, ct, ml, ob, "HEDGE_NO")
        if 0 < info.no_ask <= config.HEDGE_NO_ASK_MAX:
            result.append(info)
            log.info("✓ HEDGE %s %.1fmin yes_ask=%dc no_ask=%dc prob≈%d%%",
                     info.ticker, ml, info.yes_ask, info.no_ask, 100 - info.no_ask)
        else:
            log.debug("Hedge filtered no_ask=%dc: %s", info.no_ask, mkt["ticker"])

    log.info("Found %d tradeable brackets", len(result))
    return result
