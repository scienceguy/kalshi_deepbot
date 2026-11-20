"""box_arbitrage.py — M5: Box arbitrage detection within and across venues.

A "box" in binary options is a set of complementary positions that guarantees
a fixed payoff at expiry. If the sum of bid prices > 1.0 or sum of ask prices < 1.0,
there is an arbitrage opportunity.

Within-venue box: Combining YES and NO contracts on the same event/strike.
Cross-venue box: Combining YES on one venue with NO on another for the same underlying.

Also checks resolution equivalence: Kalshi uses CF Benchmarks BRTI 60s average,
Polymarket uses Binance 1m candle. If resolution sources differ significantly,
the box is a statistical deviation study, not a riskless arb.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import config

log = logging.getLogger("box_arbitrage")

# Minimum edge in cents to consider a box viable after fees
MIN_BOX_EDGE_CENTS = float(config.GATES_MIN_EDGE) * 100 if hasattr(config, 'GATES_MIN_EDGE') else 2.0


@dataclass
class BoxOpportunity:
    """A detected box arbitrage opportunity."""
    venue_a: str
    venue_b: str
    market_a_id: str
    market_b_id: str
    strike: float
    side_a: str  # "yes" or "no"
    side_b: str
    price_a: float  # in dollars (0-1)
    price_b: float
    total_cost: float  # price_a + price_b
    edge_cents: float  # (1.0 - total_cost) * 100 for long box, or (total_cost - 1.0) * 100 for short box
    box_type: str  # "within_venue" or "cross_venue"
    resolution_equiv: bool
    notes: str = ""


@dataclass
class MarketSnapshot:
    """Snapshot of a market's YES/NO prices."""
    venue: str
    market_id: str
    strike: float
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    yes_mid: Optional[float] = None
    no_mid: Optional[float] = None
    timestamp: float = 0.0


def extract_kalshi_snapshot(market_data: dict) -> Optional[MarketSnapshot]:
    """Extract YES/NO prices from Kalshi market data."""
    try:
        ticker = market_data.get("ticker", "")
        strike = float(market_data.get("floor_strike", 0))

        yes_bid = market_data.get("yes_bid_dollars")
        yes_ask = market_data.get("yes_ask_dollars")
        no_bid = market_data.get("no_bid_dollars")
        no_ask = market_data.get("no_ask_dollars")

        # Compute mids
        yes_mid = None
        if yes_bid is not None and yes_ask is not None and yes_ask > yes_bid:
            yes_mid = (yes_bid + yes_ask) / 2.0
        elif yes_bid is not None:
            yes_mid = yes_bid
        elif yes_ask is not None:
            yes_mid = yes_ask

        no_mid = None
        if no_bid is not None and no_ask is not None and no_ask > no_bid:
            no_mid = (no_bid + no_ask) / 2.0
        elif no_bid is not None:
            no_mid = no_bid
        elif no_ask is not None:
            no_mid = no_ask

        # Complement check: if one side missing, derive from other
        if yes_mid is not None and no_mid is None:
            no_mid = 1.0 - yes_mid
            no_bid = 1.0 - yes_ask if yes_ask else None
            no_ask = 1.0 - yes_bid if yes_bid else None
        elif no_mid is not None and yes_mid is None:
            yes_mid = 1.0 - no_mid
            yes_bid = 1.0 - no_ask if no_ask else None
            yes_ask = 1.0 - no_bid if no_bid else None

        return MarketSnapshot(
            venue="kalshi",
            market_id=ticker,
            strike=strike,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_mid=yes_mid,
            no_mid=no_mid,
        )
    except Exception as e:
        log.debug("Failed to extract Kalshi snapshot: %s", e)
        return None


def extract_poly_snapshot(market_data: dict) -> Optional[MarketSnapshot]:
    """Extract YES/NO prices from Polymarket US market data."""
    try:
        slug = market_data.get("slug", "")
        # Extract strike from assetPriceTerms
        apt = market_data.get("assetPriceTerms", {})
        strike_val = apt.get("strike")
        if strike_val is None:
            ptb = apt.get("priceToBeat")
            if ptb is not None:
                strike_val = float(ptb)
        strike = float(strike_val) if strike_val is not None else 0.0

        # Get prices from marketSides
        yes_price = None
        no_price = None
        for side in market_data.get("marketSides", []):
            if side.get("long", False) or side.get("marketSideType", "") == "YES":
                yes_price = float(side.get("price", 0))
            elif side.get("marketSideType", "") == "NO":
                no_price = float(side.get("price", 0))

        # Fallback to quotes
        if yes_price is None:
            for quote_key in ("bestBidQuote", "bestAskQuote"):
                q = market_data.get(quote_key)
                if q:
                    yes_price = float(q.get("value", 0))
                    break

        # For Polymarket US, we often only have YES side
        # NO is complement
        yes_mid = yes_price
        no_mid = 1.0 - yes_price if yes_price is not None else None

        yes_bid = yes_mid
        yes_ask = yes_mid
        no_bid = no_mid
        no_ask = no_mid

        return MarketSnapshot(
            venue="polymarket_us",
            market_id=slug,
            strike=strike,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_mid=yes_mid,
            no_mid=no_mid,
        )
    except Exception as e:
        log.debug("Failed to extract Polymarket snapshot: %s", e)
        return None


def check_within_venue_box(snapshot: MarketSnapshot) -> Optional[BoxOpportunity]:
    """Check for box arbitrage within a single venue (YES + NO on same market).

    Long box: Buy YES at ask + Buy NO at ask. Cost = yes_ask + no_ask.
    If cost < 1.0, riskless profit = 1.0 - cost.

    Short box: Sell YES at bid + Sell NO at bid. Revenue = yes_bid + no_bid.
    If revenue > 1.0, riskless profit = revenue - 1.0.
    """
    if snapshot.yes_ask is None or snapshot.no_ask is None:
        return None
    if snapshot.yes_bid is None or snapshot.no_bid is None:
        return None

    # Long box: buy both sides at ask
    long_cost = snapshot.yes_ask + snapshot.no_ask
    long_edge = (1.0 - long_cost) * 100.0  # cents

    # Short box: sell both sides at bid
    short_revenue = snapshot.yes_bid + snapshot.no_bid
    short_edge = (short_revenue - 1.0) * 100.0  # cents

    best_edge = max(long_edge, short_edge)
    if best_edge <= MIN_BOX_EDGE_CENTS:
        return None

    if long_edge >= short_edge:
        return BoxOpportunity(
            venue_a=snapshot.venue,
            venue_b=snapshot.venue,
            market_a_id=snapshot.market_id,
            market_b_id=snapshot.market_id,
            strike=snapshot.strike,
            side_a="yes",
            side_b="no",
            price_a=snapshot.yes_ask,
            price_b=snapshot.no_ask,
            total_cost=long_cost,
            edge_cents=long_edge,
            box_type="within_venue",
            resolution_equiv=True,  # Same venue = same resolution
            notes=f"Long box: buy YES@{snapshot.yes_ask:.4f} + NO@{snapshot.no_ask:.4f} = {long_cost:.4f} < 1.0"
        )
    else:
        return BoxOpportunity(
            venue_a=snapshot.venue,
            venue_b=snapshot.venue,
            market_a_id=snapshot.market_id,
            market_b_id=snapshot.market_id,
            strike=snapshot.strike,
            side_a="yes",
            side_b="no",
            price_a=snapshot.yes_bid,
            price_b=snapshot.no_bid,
            total_cost=short_revenue,
            edge_cents=short_edge,
            box_type="within_venue",
            resolution_equiv=True,
            notes=f"Short box: sell YES@{snapshot.yes_bid:.4f} + NO@{snapshot.no_bid:.4f} = {short_revenue:.4f} > 1.0"
        )


def check_cross_venue_box(
    snap_a: MarketSnapshot,
    snap_b: MarketSnapshot,
    check_resolution: bool = True
) -> Optional[BoxOpportunity]:
    """Check for box arbitrage across two venues.

    We can combine:
    - YES on venue A + NO on venue B
    - NO on venue A + YES on venue B

    Both should sum to ~1.0 if they refer to the same event resolution.
    """
    # Must be same strike
    if abs(snap_a.strike - snap_b.strike) > 0.01 * max(snap_a.strike, 1):
        return None

    # Need prices on both sides
    prices_a = {
        "yes": (snap_a.yes_bid, snap_a.yes_ask, snap_a.yes_mid),
        "no": (snap_a.no_bid, snap_a.no_ask, snap_a.no_mid),
    }
    prices_b = {
        "yes": (snap_b.yes_bid, snap_b.yes_ask, snap_b.yes_mid),
        "no": (snap_b.no_bid, snap_b.no_ask, snap_b.no_mid),
    }

    # Check all four combinations
    best_opportunity = None
    best_edge = -float("inf")

    for side_a in ("yes", "no"):
        for side_b in ("yes", "no"):
            bid_a, ask_a, mid_a = prices_a[side_a]
            bid_b, ask_b, mid_b = prices_b[side_b]

            if ask_a is None or ask_b is None:
                continue
            if bid_a is None or bid_b is None:
                continue

            # Long box: buy at ask on both
            long_cost = ask_a + ask_b
            long_edge = (1.0 - long_cost) * 100.0

            # Short box: sell at bid on both
            short_revenue = bid_a + bid_b
            short_edge = (short_revenue - 1.0) * 100.0

            if long_edge > best_edge and long_edge > MIN_BOX_EDGE_CENTS:
                best_edge = long_edge
                best_opportunity = BoxOpportunity(
                    venue_a=snap_a.venue,
                    venue_b=snap_b.venue,
                    market_a_id=snap_a.market_id,
                    market_b_id=snap_b.market_id,
                    strike=snap_a.strike,
                    side_a=side_a,
                    side_b=side_b,
                    price_a=ask_a,
                    price_b=ask_b,
                    total_cost=long_cost,
                    edge_cents=long_edge,
                    box_type="cross_venue",
                    resolution_equiv=check_resolution and are_resolutions_equivalent(snap_a, snap_b),
                    notes=f"Long cross-venue box: buy {side_a}@{ask_a:.4f} ({snap_a.venue}) + {side_b}@{ask_b:.4f} ({snap_b.venue}) = {long_cost:.4f} < 1.0"
                )

            if short_edge > best_edge and short_edge > MIN_BOX_EDGE_CENTS:
                best_edge = short_edge
                best_opportunity = BoxOpportunity(
                    venue_a=snap_a.venue,
                    venue_b=snap_b.venue,
                    market_a_id=snap_a.market_id,
                    market_b_id=snap_b.market_id,
                    strike=snap_a.strike,
                    side_a=side_a,
                    side_b=side_b,
                    price_a=bid_a,
                    price_b=bid_b,
                    total_cost=short_revenue,
                    edge_cents=short_edge,
                    box_type="cross_venue",
                    resolution_equiv=check_resolution and are_resolutions_equivalent(snap_a, snap_b),
                    notes=f"Short cross-venue box: sell {side_a}@{bid_a:.4f} ({snap_a.venue}) + {side_b}@{bid_b:.4f} ({snap_b.venue}) = {short_revenue:.4f} > 1.0"
                )

    return best_opportunity


def are_resolutions_equivalent(snap_a: MarketSnapshot, snap_b: MarketSnapshot) -> bool:
    """Check if two venues use equivalent resolution sources.

    Kalshi: CF Benchmarks BRTI 60s average
    Polymarket US: Binance 1m candle close

    These are different sources but both track the same underlying (BTC/ETH/USDT).
    The difference is basis risk, not a fundamental mismatch.
    """
    # For now, consider BTC/ETH crypto markets as equivalent
    # since both reference USDT spot prices
    if "BTC" in str(snap_a.market_id).upper() or "BTC" in str(snap_b.market_id).upper():
        return True
    if "ETH" in str(snap_a.market_id).upper() or "ETH" in str(snap_b.market_id).upper():
        return True
    if "SOL" in str(snap_a.market_id).upper() or "SOL" in str(snap_b.market_id).upper():
        return True
    return False


def scan_kalshi_for_boxes(markets: List[dict]) -> List[BoxOpportunity]:
    """Scan a list of Kalshi markets for within-venue box opportunities."""
    opportunities = []
    for market in markets:
        if market.get("strike_type") != "greater" or market.get("floor_strike") is None:
            continue
        snapshot = extract_kalshi_snapshot(market)
        if snapshot:
            opp = check_within_venue_box(snapshot)
            if opp:
                opportunities.append(opp)
    return opportunities


def scan_polymarket_for_boxes(markets: List[dict]) -> List[BoxOpportunity]:
    """Scan a list of Polymarket markets for within-venue box opportunities."""
    opportunities = []
    for market in markets:
        snapshot = extract_poly_snapshot(market)
        if snapshot:
            opp = check_within_venue_box(snapshot)
            if opp:
                opportunities.append(opp)
    return opportunities


def find_cross_venue_boxes(
    kalshi_markets: List[dict],
    polymarket_markets: List[dict]
) -> List[BoxOpportunity]:
    """Find cross-venue box arbitrage between Kalshi and Polymarket."""
    # Build snapshots by strike
    kalshi_by_strike = {}
    for m in kalshi_markets:
        if m.get("strike_type") != "greater" or m.get("floor_strike") is None:
            continue
        snap = extract_kalshi_snapshot(m)
        if snap:
            kalshi_by_strike[round(snap.strike, 2)] = snap

    poly_by_strike = {}
    for m in polymarket_markets:
        snap = extract_poly_snapshot(m)
        if snap:
            poly_by_strike[round(snap.strike, 2)] = snap

    # Find matching strikes
    opportunities = []
    for strike in kalshi_by_strike:
        if strike in poly_by_strike:
            opp = check_cross_venue_box(
                kalshi_by_strike[strike],
                poly_by_strike[strike]
            )
            if opp:
                opportunities.append(opp)

    return opportunities


def scan_all_venues_for_boxes(
    kalshi_ws=None,
    poly_stream=None,
    kalshi_client=None,
    poly_client=None
) -> List[BoxOpportunity]:
    """Main entry point: scan all available venues for box arbitrage.

    Args:
        kalshi_ws: KalshiWebSocket instance for live orderbook data
        poly_stream: PolyStream instance for live Polymarket data
        kalshi_client: Kalshi REST client for fetching markets
        poly_client: Polymarket US client for fetching markets

    Returns:
        List of BoxOpportunity objects, sorted by edge (highest first)
    """
    all_opportunities = []

    # Get Kalshi markets
    kalshi_markets = []
    if kalshi_client:
        try:
            resp = kalshi_client.get_markets(series="KXBTC15M", status="open", limit=100)
            kalshi_markets = resp.get("markets", [])
            # Also get ETH
            resp = kalshi_client.get_markets(series="KXETH15M", status="open", limit=100)
            kalshi_markets.extend(resp.get("markets", []))
        except Exception as e:
            log.warning("Failed to fetch Kalshi markets: %s", e)

    # Get Polymarket markets
    polymarket_markets = []
    if poly_client:
        try:
            # This would need a method to list active crypto markets
            pass
        except Exception as e:
            log.warning("Failed to fetch Polymarket markets: %s", e)

    # Scan within-venue
    all_opportunities.extend(scan_kalshi_for_boxes(kalshi_markets))
    all_opportunities.extend(scan_polymarket_for_boxes(polymarket_markets))

    # Scan cross-venue
    all_opportunities.extend(find_cross_venue_boxes(kalshi_markets, polymarket_markets))

    # Sort by edge (highest first)
    all_opportunities.sort(key=lambda x: x.edge_cents, reverse=True)

    return all_opportunities


def format_opportunity(opp: BoxOpportunity) -> str:
    """Format a box opportunity for logging."""
    res_str = "equiv" if opp.resolution_equiv else "diff-res"
    return (f"BOX [{opp.box_type}] {opp.venue_a}/{opp.venue_b} "
            f"strike={opp.strike:,.0f} "
            f"{opp.side_a}@{opp.price_a:.4f}+{opp.side_b}@{opp.price_b:.4f} "
            f"edge={opp.edge_cents:.2f}c [{res_str}]")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    # Quick test
    snap1 = MarketSnapshot(
        venue="kalshi",
        market_id="KXBTC15M-...",
        strike=50000,
        yes_bid=0.94, yes_ask=0.95,
        no_bid=0.04, no_ask=0.05,
    )
    opp = check_within_venue_box(snap1)
    if opp:
        print(format_opportunity(opp))
    else:
        print("No box opportunity found (expected for normal spreads)")