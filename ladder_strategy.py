"""
ladder_strategy.py — maker-entry, hold-to-settlement on Kalshi threshold ladders.

The structure:

    buy as a maker  ->  pay no fee
    hold to settlement  ->  pay no fee, no exit spread

so the only cost is the price paid versus the true probability. Everything that
matters therefore reduces to one question: **is my resting order filled only
when I am wrong?**

Kalshi charges the taker `0.07 * p * (1-p)` per contract. A taker who crosses
to hit my resting bid believes the true probability is below my price, and they
will not do it unless that difference beats the fee they just paid. So the
taker fee is a *lower bound on the informational edge of whoever fills me*.
That is the whole adverse-selection model, and it is why a resting favourite
filled at 49.5c won only 45.3%: the fill carried roughly the fee plus the
informed edge.

Hence the entry rule is not "price below fair". It is:

    fair - price > required_edge(price, volatility)

where required_edge starts at the taker fee and grows with the volatility
regime. A rung whose discount does not clear that bar is a rung where being
filled is bad news.

Rules from the maker playbook that are encoded here:
  * layered sizing — several small rungs rather than one block, so a sweep only
    reaches the first layer;
  * a volatility-scaled edge requirement, so quotes demand more when the
    underlying is moving;
  * a time gate, so we never hold a resting order into the settlement race.

This module only *plans*. It places nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

import crossvenue as cv
import binance_feed

TAKER_FEE_RATE = 0.07  # matches fees.FEE_RATE

# How much of the taker-fee barrier to demand as discount. 1.0 means "only trade
# where being filled implies the taker had no edge beyond their own fee".
MIN_SAFETY_MULTIPLE = 1.0

# Cap the volatility multiplier so a spike widens quotes instead of shutting us
# down entirely; the time gate handles the extreme cases.
MAX_VOL_MULTIPLE = 3.0

# Do not hold a resting order into the last minutes; settlement races are pure
# adverse selection and the lognormal is meaningless there.
MIN_MINUTES_TO_HOLD = 5.0
MAX_MINUTES_TO_HOLD = 240.0

# Rungs at 0c or 100c cannot be entered as a maker with any edge.
PRICE_FLOOR_C = 1.0
PRICE_CEIL_C = 99.0


def taker_fee_cents(price_cents: float) -> float:
    """Kalshi taker fee per contract, in cents. Peaks at 1.75c at 50c."""
    p = price_cents / 100.0
    if not 0.0 < p < 1.0:
        return 0.0
    return TAKER_FEE_RATE * p * (1.0 - p) * 100.0


def vol_multiple(sigma_now: float, sigma_base: float) -> float:
    """Current volatility relative to its baseline, clamped to [1, MAX]."""
    if sigma_base <= 0 or sigma_now <= 0:
        return MAX_VOL_MULTIPLE
    return float(np.clip(sigma_now / sigma_base, 1.0, MAX_VOL_MULTIPLE))


def required_edge_cents(price_cents: float, vol_mult: float = 1.0,
                        safety: float = MIN_SAFETY_MULTIPLE) -> float:
    """Discount to fair demanded before a resting bid at this price is worth it."""
    return safety * taker_fee_cents(price_cents) * vol_mult


def implied_spot(rows: List[dict], sigma_min: float, minutes_left: float) -> Optional[float]:
    """The spot price at which our model best reproduces the observed ladder.

    A ladder is 20-200 quotes on one underlying; inverting it gives the price the
    market is actually centred on. Comparing that to the composite spot exposes
    the basis between Kalshi's reference and ours.

    Without this, a uniform offset between the two references shows up as a
    large edge on *every* rung — which is a reference error wearing an edge's
    clothes.
    """
    if not rows or sigma_min <= 0 or minutes_left <= 0:
        return None
    strikes = np.array([r["strike"] for r in rows], dtype=float)
    probs = np.array([r["kalshi"] for r in rows], dtype=float)
    lo, hi = float(strikes.min()), float(strikes.max())
    if hi <= lo:
        return None

    grid = np.linspace(lo, hi, 600)
    # For each candidate spot, model probability at every strike, then sum
    # squared error against the observed ladder.
    errs = np.array([
        np.sum((np.array([binance_feed.prob_above(s, k, sigma_min, minutes_left)
                          for k in strikes]) - probs) ** 2)
        for s in grid
    ])
    return float(grid[int(np.argmin(errs))])


@dataclass
class Plan:
    strike: float
    side: str
    price_c: float
    fair_c: float
    gross_edge_c: float
    required_c: float
    net_edge_c: float
    tradeable: bool
    reason: str = ""


def plan_orders(rows: List[dict], *, vol_mult: float = 1.0,
                safety: float = MIN_SAFETY_MULTIPLE,
                min_minutes: float = MIN_MINUTES_TO_HOLD,
                max_minutes: float = MAX_MINUTES_TO_HOLD) -> List[Plan]:
    """Rank candidate maker orders from `crossvenue.three_way` rows.

    `rows` each carry strike, fair, kalshi (mid), minutes_left. Buying YES and
    buying NO are exact mirrors on a binary, so one signed edge covers both:
        fair > price  ->  buy YES;  fair < price  ->  buy NO.
    """
    plans: List[Plan] = []
    for r in rows:
        minutes = r["minutes_left"]
        price_c = r["kalshi"] * 100.0
        fair_c = r["fair"] * 100.0
        signed = fair_c - price_c
        side = "YES" if signed >= 0 else "NO"

        if not (PRICE_FLOOR_C <= price_c <= PRICE_CEIL_C):
            plans.append(Plan(r["strike"], side, price_c, fair_c, signed, 0.0, 0.0,
                              False, "price at boundary"))
            continue
        if not (min_minutes <= minutes <= max_minutes):
            plans.append(Plan(r["strike"], side, price_c, fair_c, signed, 0.0, 0.0,
                              False, f"{minutes:.0f} min outside hold window"))
            continue

        required = required_edge_cents(price_c, vol_mult, safety)
        net = abs(signed) - required
        plans.append(Plan(r["strike"], side, price_c, fair_c, abs(signed), required,
                          net, net > 0, "ok" if net > 0 else "below adverse-selection bar"))
    return plans


def layered(plans: List[Plan], layers: int = 3) -> List[Plan]:
    """Keep the best tradeable rungs, spread across strikes.

    Several small rungs rather than one block: a sweep only reaches the front
    layer, and a bad fill cannot take the whole intended position.
    """
    tradeable = sorted((p for p in plans if p.tradeable), key=lambda p: -p.net_edge_c)
    return tradeable[:layers]


def summarise(plans: List[Plan]) -> dict:
    tradeable = [p for p in plans if p.tradeable]
    return {
        "rungs": len(plans),
        "tradeable": len(tradeable),
        "best_net_edge_c": max((p.net_edge_c for p in tradeable), default=0.0),
        "median_required_c": float(np.median([p.required_c for p in plans])) if plans else 0.0,
    }


def main() -> int:
    import argparse
    import logging

    ap = argparse.ArgumentParser(description="plan ladder orders; places nothing")
    ap.add_argument("--layers", type=int, default=3)
    ap.add_argument("--safety", type=float, default=MIN_SAFETY_MULTIPLE)
    ap.add_argument("--basis-dollars", type=float, default=None,
                    help="historical median (Kalshi implied spot - composite spot). "
                         "Omitting it means the reference basis is unestimated, so "
                         "no rung is tradeable.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    found = 0
    for asset in ("BTC", "ETH"):
        symbol = f"{asset}USDT"
        spot = binance_feed.composite_price(symbol)
        sigma_now = binance_feed.sigma_per_minute(symbol, lookback=60)
        sigma_base = binance_feed.sigma_per_minute(symbol, lookback=1440)
        if spot is None or sigma_now is None:
            print(f"{symbol}: no spot/vol")
            continue
        vm = vol_multiple(sigma_now, sigma_base or sigma_now)

        for event in cv.discover_kalshi_events():
            parsed = cv.parse_kalshi_event(event)
            if not parsed or parsed[0] != asset:
                continue
            kalshi = cv.fetch_kalshi_ladder(event)
            if not kalshi or not kalshi.ok:
                continue
            for slug in cv.candidate_pm_slugs(asset, parsed[1]):
                poly = cv.fetch_pm_ladder(slug)
                if not poly or not poly.ok:
                    continue
                rows = cv.three_way(kalshi, poly, spot, sigma_now)
                if not rows:
                    continue
                minutes = rows[0]["minutes_left"]
                impl = implied_spot(rows, sigma_now, minutes)
                basis = (impl - spot) if impl is not None else float("nan")

                print(f"\n=== {asset} {event}  spot {spot:,.2f}  "
                      f"{minutes:.0f} min  vol x{vm:.2f} ===")
                print(f"ladder-implied spot {impl:,.2f}   "
                      f"basis vs composite {basis:+,.2f}  "
                      f"({100 * basis / (spot * sigma_now * max(minutes, 1) ** 0.5):+.0f}% "
                      f"of one sigma)")

                if args.basis_dollars is None:
                    print("NO TRADE: basis unestimated. Every rung shows the same "
                          "offset, which is the basis, not an edge. Pass "
                          "--basis-dollars once the collector has measured it.")
                    continue

                # Price fair value against Kalshi's own reference, not ours.
                rows = cv.three_way(kalshi, poly, spot + args.basis_dollars, sigma_now)
                plans = plan_orders(rows, vol_mult=vm, safety=args.safety)
                s = summarise(plans)
                found += s["tradeable"]
                print(f"rungs {s['rungs']}  tradeable {s['tradeable']}  "
                      f"best net {s['best_net_edge_c']:+.1f}c  "
                      f"median barrier {s['median_required_c']:.2f}c")
                for p in sorted(plans, key=lambda p: -p.net_edge_c)[:4]:
                    print(f"  {p.strike:>10,.0f} {p.side:>3} @ {p.price_c:>5.1f}c  "
                          f"fair {p.fair_c:>5.1f}c  edge {p.gross_edge_c:>5.1f}c  "
                          f"need {p.required_c:>4.2f}c  net {p.net_edge_c:>+5.1f}c")
                for p in layered(plans, args.layers):
                    print(f"  -> LAYER {p.side} {p.strike:,.0f} "
                          f"net {p.net_edge_c:+.1f}c")

    print(f"\ntradeable rungs across all ladders: {found}")
    if not found:
        print("nothing clears the adverse-selection bar — correctly, no trade")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
