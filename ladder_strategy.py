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

import math
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
# adverse selection, and the lognormal plus a dollar-denominated basis error both
# degenerate as the horizon shrinks.
MIN_MINUTES_TO_HOLD = 10.0
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
                        safety: float = MIN_SAFETY_MULTIPLE,
                        basis_noise_c: float = 0.0) -> float:
    """Discount to fair demanded before a resting bid at this price is worth it.

    Two independent sources of error have to be beaten:

      * the taker fee, which bounds the informed edge of whoever fills us;
      * the reference basis noise, because our fair value is computed from a
        spot composite while Kalshi settles on CF Benchmarks BRTI.

    They are combined in quadrature: both are errors in our estimate of the
    true probability, and either one alone can manufacture a fake edge.
    """
    fee = safety * taker_fee_cents(price_cents) * vol_mult
    return math.hypot(fee, basis_noise_c)


def dp_ds(spot: float, strike: float, sigma_min: float, minutes: float) -> float:
    """dP(price > strike)/dSpot: converts a dollar reference error to probability.

    Near the money a dollar of spot moves the probability a lot; deep in the
    wings it barely moves it. That is why basis noise hurts most in the strikes
    we would actually want to trade.
    """
    if spot <= 0 or strike <= 0 or sigma_min <= 0 or minutes <= 0:
        return 0.0
    denom = sigma_min * math.sqrt(minutes)
    if denom <= 0:
        return 0.0
    d2 = (math.log(spot / strike) - 0.5 * denom * denom) / denom
    return math.exp(-0.5 * d2 * d2) / (math.sqrt(2.0 * math.pi) * spot * denom)


def basis_noise_cents(spot: float, strike: float, sigma_min: float, minutes: float,
                      basis_stdev_dollars: float) -> float:
    """Reference-error uncertainty at this strike, in cents of probability."""
    return basis_stdev_dollars * dp_ds(spot, strike, sigma_min, minutes) * 100.0


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
    noise_c: float = 0.0


def plan_orders(rows: List[dict], *, vol_mult: float = 1.0,
                safety: float = MIN_SAFETY_MULTIPLE,
                basis_stdev_dollars: float = 0.0,
                spot: Optional[float] = None,
                sigma_min: Optional[float] = None,
                ml: Optional[object] = None,
                min_minutes: float = MIN_MINUTES_TO_HOLD,
                max_minutes: float = MAX_MINUTES_TO_HOLD) -> List[Plan]:
    """Rank candidate maker orders from `crossvenue.three_way` rows.

    `rows` each carry strike, fair, kalshi (mid), minutes_left. Buying YES and
    buying NO are exact mirrors on a binary, so one signed edge covers both:
        fair > price  ->  buy YES;  fair < price  ->  buy NO.

    Pass `basis_stdev_dollars` (with spot and sigma) to charge each rung for the
    reference-error noise at its own moneyness. Deep wings are cheap to trade
    because a dollar of reference error barely moves their probability.

    Pass an `ml` model (anything with `predict_proba(row, spot, sigma, minutes)`)
    to override the lognormal `fair` with a learned probability. The model is
    trained on settled history (see ml_model.train_from_history).
    """
    plans: List[Plan] = []
    for r in rows:
        minutes = r["minutes_left"]
        price_c = r["kalshi"] * 100.0

        fair_c = r["fair"] * 100.0
        if ml is not None and spot is not None and sigma_min is not None:
            fair_c = ml.predict_proba(r, spot, sigma_min, minutes) * 100.0

        signed = fair_c - price_c
        side = "YES" if signed >= 0 else "NO"

        noise_c = 0.0
        if basis_stdev_dollars and spot and sigma_min:
            noise_c = basis_noise_cents(spot, r["strike"], sigma_min, minutes,
                                        basis_stdev_dollars)

        if not (PRICE_FLOOR_C <= price_c <= PRICE_CEIL_C):
            plans.append(Plan(r["strike"], side, price_c, fair_c, signed, 0.0, 0.0,
                              False, "price at boundary", noise_c))
            continue
        if not (min_minutes <= minutes <= max_minutes):
            plans.append(Plan(r["strike"], side, price_c, fair_c, signed, 0.0, 0.0,
                              False, f"{minutes:.0f} min outside hold window", noise_c))
            continue

        required = required_edge_cents(price_c, vol_mult, safety, noise_c)
        net = abs(signed) - required
        plans.append(Plan(r["strike"], side, price_c, fair_c, abs(signed), required,
                          net, net > 0, "ok" if net > 0 else "below adverse-selection bar",
                          noise_c))
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
    ap.add_argument("--basis", type=str, action="append", default=[],
                    metavar="ASSET=DOLLARS",
                    help="historical median (Kalshi implied spot - composite spot) "
                         "for one asset, e.g. --basis BTC=13.46. Repeat per asset. "
                         "An asset with no basis is not traded (the offset would be "
                         "indistinguishable from an edge).")
    ap.add_argument("--basis-noise", type=str, action="append", default=[],
                    metavar="ASSET=DOLLARS",
                    help="historical stdev of that basis for one asset, e.g. "
                         "--basis-noise BTC=21.02. Charged per rung at its own "
                         "moneyness and combined with the fee barrier.")
    ap.add_argument("--ml-model", type=str, default=None,
                    help="path to a saved torch fair-value model (ml_model.py). "
                         "When given, its predict_proba overrides the lognormal "
                         "fair value on every rung.")
    args = ap.parse_args()

    basis_dollars: dict = {}
    for item in args.basis:
        if "=" in item:
            k, v = item.split("=", 1)
            basis_dollars[k.strip().upper()] = float(v)
    basis_noise_dollars: dict = {}
    for item in args.basis_noise:
        if "=" in item:
            k, v = item.split("=", 1)
            basis_noise_dollars[k.strip().upper()] = float(v)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    found = 0
    ml = None
    if args.ml_model:
        import ml_model
        ml = ml_model.MLFairValue.load(args.ml_model)
        print(f"ML model loaded from {args.ml_model}")
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

                basis_d = basis_dollars.get(asset)
                if basis_d is None:
                    print(f"NO TRADE: basis unestimated for {asset}. Every rung "
                          f"shows the same offset, which is the basis, not an edge. "
                          f"Pass --basis {asset}=<dollars> once the collector has "
                          f"measured it.")
                    continue

                # Price fair value against Kalshi's own reference, not ours.
                spot_ref = spot + basis_d
                noise_d = basis_noise_dollars.get(asset, 0.0)
                rows = cv.three_way(kalshi, poly, spot_ref, sigma_now)
                plans = plan_orders(rows, vol_mult=vm, safety=args.safety,
                                    basis_stdev_dollars=noise_d,
                                    spot=spot_ref, sigma_min=sigma_now,
                                    ml=ml)
                s = summarise(plans)
                found += s["tradeable"]
                print(f"rungs {s['rungs']}  tradeable {s['tradeable']}  "
                      f"best net {s['best_net_edge_c']:+.1f}c  "
                      f"median barrier {s['median_required_c']:.2f}c")
                for p in sorted(plans, key=lambda p: -p.net_edge_c)[:4]:
                    print(f"  {p.strike:>10,.0f} {p.side:>3} @ {p.price_c:>5.1f}c  "
                          f"fair {p.fair_c:>5.1f}c  edge {p.gross_edge_c:>5.1f}c  "
                          f"noise {p.noise_c:>4.2f}c  need {p.required_c:>4.2f}c  "
                          f"net {p.net_edge_c:>+5.1f}c")
                for p in layered(plans, args.layers):
                    print(f"  -> LAYER {p.side} {p.strike:,.0f} "
                          f"net {p.net_edge_c:+.1f}c")

    print(f"\ntradeable rungs across all ladders: {found}")
    if not found:
        print("nothing clears the adverse-selection bar — correctly, no trade")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
