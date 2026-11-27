"""Execution-realism arithmetic for candidate backtests.

Only timestamped real books are valid strategy inputs. Synthetic fixtures belong
in arithmetic tests, never in an edge claim. There is no price interpolation and
no partial-depth or one-legged fill fallback.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from datetime import date

import pandas as pd

import fees


BOOK_FRESHNESS_SECONDS = 1.0
BOOK_SYNC_TOLERANCE_SECONDS = 0.25
DELAY_SECONDS = 1.0
DELAY_TOLERANCE_SECONDS = 0.25


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    venue: str
    side: str
    ts: float
    levels: list[Level]


@dataclass(frozen=True)
class LegFill:
    quantity: float
    total_cost: float
    fee: float
    levels_used: int
    proceeds: float | None = None


@dataclass(frozen=True)
class PairResult:
    quantity: float
    left: LegFill
    right: LegFill
    pnl: float


@dataclass(frozen=True)
class PairEvaluation:
    fill: PairResult


@dataclass(frozen=True)
class DelayValidation:
    base: PairEvaluation
    delay: PairEvaluation | None


@dataclass(frozen=True)
class SizingCaps:
    available_cash: float
    max_exposure: float
    current_exposure: float
    current_drawdown: float
    max_drawdown: float


@dataclass(frozen=True)
class SizedPair:
    fill: PairResult
    tail_exposure: float
    turnover: float
    drawdown_after: float


def bounded_fixed_fraction(equity: float, fraction: float, caps: SizingCaps) -> float:
    """Dollar budget after existing cash, exposure and drawdown limits."""
    if equity <= 0 or not 0 < fraction <= 1:
        return 0.0
    exposure_room = max(0.0, caps.max_exposure - caps.current_exposure)
    drawdown_room = max(0.0, equity * (caps.max_drawdown - caps.current_drawdown))
    return max(0.0, min(equity * fraction, caps.available_cash,
                        exposure_room, drawdown_room))


def depth_aware_size(left: Book, right: Book, equity: float, fraction: float,
                     caps: SizingCaps, depth_cap_fraction: float = 1.0,
                     kalshi_series: str | None = None,
                     now: float | None = None) -> SizedPair | None:
    """Size a positive-edge pair without exceeding any existing risk cap."""
    budget = bounded_fixed_fraction(equity, fraction, caps)
    if budget <= 0 or not left.levels or not right.levels:
        return None
    unit_cost = left.levels[0].price + right.levels[0].price
    if unit_cost <= 0:
        return None
    depth = min(sum(level.size for level in left.levels),
                sum(level.size for level in right.levels)) * depth_cap_fraction
    requested = min(depth, budget / unit_cost)
    fill = evaluate_pair(left, right, requested, depth_cap_fraction, kalshi_series, now)
    if fill is None or fill.pnl <= 0:
        return None  # Sizing must never rescue a nonpositive per-contract edge.
    cost = fill.left.total_cost + fill.right.total_cost
    exposure_room = max(0.0, caps.max_exposure - caps.current_exposure)
    if cost > caps.available_cash + 1e-9 or cost > exposure_room + 1e-9:
        return None
    drawdown_after = caps.current_drawdown + cost / equity
    if drawdown_after > caps.max_drawdown + 1e-9:
        return None
    return SizedPair(fill, cost, cost / equity, drawdown_after)


def summarize_validation(rows: list[dict], equity: float,
                         confidence: float = 0.95, samples: int = 10000,
                         seed: int = 20261125) -> dict:
    """Clustered net LB plus path drawdown, worst tail exposure and turnover."""
    running = peak = equity
    max_drawdown = 0.0
    for row in rows:
        running += float(row["pnl"])
        peak = max(peak, running)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - running) / peak)
    return {
        "clustered_lb": clustered_lower_bound(rows, confidence, samples, seed),
        "max_drawdown": max_drawdown,
        "tail_exposure": max((float(row["tail_exposure"]) for row in rows), default=0.0),
        "turnover": sum(float(row["cost"]) for row in rows) / equity if equity > 0 else 0.0,
    }


def walk_book(book: Book, quantity: float, depth_fraction: float = 1.0,
              kalshi_series: str | None = None,
              action: str = "buy") -> LegFill | None:
    """Walk every needed level; outcome side never implies order action."""
    if quantity <= 0 or not 0 < depth_fraction <= 1.0:
        return None
    order_action = action.lower()
    if order_action not in ("buy", "sell"):
        return None
    valid = [(float(level.price), float(level.size) * depth_fraction)
             for level in book.levels
             if 0 < level.price < 1 and level.size > 0]
    valid.sort(key=lambda x: x[0], reverse=order_action == "sell")
    if sum(size for _, size in valid) + 1e-9 < quantity:
        return None
    remaining = quantity
    cost = fee = 0.0
    poly_fills = []
    used = 0
    for price, size in valid:
        take = min(remaining, size)
        if take <= 0:
            continue
        used += 1
        cost += take * price
        if book.venue == "kalshi":
            fee += fees.taker_fee_dollars(take, price, series=kalshi_series)
        elif book.venue == "polymarket_us":
            poly_fills.append((take, price))
        else:
            return None
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9 or used == 0:
        return None
    if book.venue == "polymarket_us":  # reviewed: aggregate cap only reduces per-fill rounded fees
        fee = fees.poly_taker_fills_fee_dollars(poly_fills)
    proceeds = cost - fee if order_action == "sell" else None
    return LegFill(quantity, cost + fee, fee, used, proceeds)


def _books_fresh_and_synchronized(left: Book, right: Book, now: float) -> bool:
    return (all(0.0 <= now - book.ts <= BOOK_FRESHNESS_SECONDS
                for book in (left, right))
            and abs(left.ts - right.ts) <= BOOK_SYNC_TOLERANCE_SECONDS)


def evaluate_pair(left: Book, right: Book, requested: float,
                  depth_fraction: float = 1.0,
                  kalshi_series: str | None = None,
                  now: float | None = None) -> PairResult | None:
    """Choose the depth-supported quantity with maximum net pair PnL."""
    decision_ts = time.time() if now is None else float(now)
    if requested <= 0 or not _books_fresh_and_synchronized(left, right, decision_ts):
        return None
    capacities = {float(requested)}
    for book in (left, right):
        total = 0.0
        for level in book.levels:
            if 0 < level.price < 1 and level.size > 0:
                total += level.size * depth_fraction
                capacities.add(min(float(requested), total))
    best = None
    for quantity in sorted(capacities):
        lf = walk_book(left, quantity, depth_fraction, kalshi_series)
        rf = walk_book(right, quantity, depth_fraction, kalshi_series)
        if lf is None or rf is None:
            continue
        result = PairResult(quantity, lf, rf, quantity - lf.total_cost - rf.total_cost)
        if result.pnl > 0 and (best is None or result.pnl > best.pnl):
            best = result
    return best


def validate_candidate(left: Book, right: Book, requested: float,
                       delayed_left: Book, delayed_right: Book,
                       now: float | None = None) -> DelayValidation:
    """Evaluate entry and a bounded, synchronized one-second delay."""
    decision_ts = time.time() if now is None else float(now)
    base = evaluate_pair(left, right, requested, now=decision_ts)
    if base is None:
        return DelayValidation(PairEvaluation(None), None)
    delay = None
    delayed_ts = decision_ts + DELAY_SECONDS
    delays = (delayed_left.ts - left.ts, delayed_right.ts - right.ts)
    if (_books_fresh_and_synchronized(delayed_left, delayed_right, delayed_ts)
            and all(abs(value - DELAY_SECONDS) <= DELAY_TOLERANCE_SECONDS
                    for value in delays)):
        delayed = evaluate_pair(delayed_left, delayed_right, requested, now=delayed_ts)
        if delayed is not None:
            delay = PairEvaluation(delayed)
    return DelayValidation(PairEvaluation(base), delay)


def clustered_lower_bound(rows: list[dict], confidence: float = 0.95,
                          samples: int = 10000, seed: int = 20261125) -> float | None:
    """Percentile bootstrap of mean PnL with markets, not quotes, resampled."""
    by_market: dict[str, list[float]] = {}
    for row in rows:
        by_market.setdefault(str(row["market"]), []).append(float(row["pnl"]))
    if len(by_market) < 30:
        return None
    clusters = [sum(values) / len(values) for values in by_market.values()]
    rng = random.Random(seed)
    n = len(clusters)
    means = sorted(sum(rng.choice(clusters) for _ in range(n)) / n
                   for _ in range(samples))
    index = max(0, int((1.0 - confidence) * (len(means) - 1)))
    return means[index]


def protocol_clustered_lower_bound(rows: list[dict], *, exact_m: int) -> float | None:
    """Frozen-protocol net-EV bound, or None without real-fill eligibility.

    Each row must prove a real production fill or an explicitly book-simulated
    dry fill held to settlement, with finite quantity/PnL and a UTC settle day.
    """
    if isinstance(exact_m, bool) or not isinstance(exact_m, int) or exact_m <= 0:
        return None
    by_market: dict[str, list] = {}
    market_days: dict[str, str] = {}
    for row in rows:
        provenance = row.get("fill_provenance", "production")
        if provenance == "book_simulated":
            if row.get("real_fill") is not False:
                return None
        elif provenance == "production":
            if row.get("real_fill") is not True:
                return None
        else:
            return None
        if row.get("held_to_settlement") is not True:
            return None
        if str(row.get("side", "")).lower() not in ("no", "down"):
            return None
        try:
            market = row["market"]
            if not isinstance(market, str) or not market.strip():
                return None
            settled_day = row["settled_day"]
            date.fromisoformat(settled_day)
            quantity = float(row["quantity"])
            net_pnl = float(row["pnl"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(quantity) or quantity <= 0 or not math.isfinite(net_pnl):
            return None
        if market in market_days and market_days[market] != settled_day:
            return None
        market_days[market] = settled_day
        by_market.setdefault(market, [0.0, 0.0])
        by_market[market][0] += net_pnl
        by_market[market][1] += quantity
    if len(by_market) < 300 or len(set(market_days.values())) < 14:
        return None

    # (net PnL, filled quantity) stays together for every resampled market.
    clusters = [(values[0], values[1]) for values in by_market.values()]
    rng = random.Random(17008)
    repetitions = 10000
    bounds = []
    for _ in range(repetitions):
        selected = [rng.choice(clusters) for _ in clusters]
        quantity = sum(values[1] for values in selected)
        bounds.append(sum(values[0] for values in selected) / quantity)
    bounds.sort()
    alpha = 0.05 / exact_m
    index = max(0, math.ceil(alpha * repetitions) - 1)
    return bounds[index]


def official_poly_outcomes(df: pd.DataFrame) -> pd.Series:
    """Map only agreeing immutable official binary outcomes."""
    def binary(value):
        if pd.isna(value):
            return None
        text = str(value).strip().lower()
        if text in ("1", "1.0", "yes", "up", "true"):
            return True
        if text in ("0", "0.0", "no", "down", "false"):
            return False
        return None

    columns = [column for column in
               ("settlement", "settlement_price", "settlementPrice", "result")
               if column in df]
    outcomes = {}
    for slug, group in df.groupby("slug"):
        values = [binary(row[column]) for _, row in group.iterrows()
                  for column in columns]
        values = [value for value in values if value is not None]
        if values and all(value == values[0] for value in values):
            outcomes[slug] = values[0]
    return df.slug.map(outcomes)


def evaluate_poly_quotes(df: pd.DataFrame, price_col: str,
                         band: tuple[float, float], win: tuple[float, float],
                         slip_c: float, frac: float, start: float,
                         price_cap: float) -> dict:
    """Shared same-row Poly execution and production-fee accounting."""
    ask_c = df[price_col] * 100.0
    selected = df[(df.minute >= win[0]) & (df.minute <= win[1])
                  & ask_c.between(*band)]
    if selected.empty:
        return {"n": 0}
    t = selected.sort_values(["slug", "t"]).drop_duplicates("slug", keep="first")
    p = ((t[price_col] * 100 + slip_c) / 100.0).clip(0.01, price_cap)
    gross = t.up_won.astype(float) * (1 - p) - (~t.up_won.astype(bool)).astype(float) * p
    depth = pd.to_numeric(t.ask_depth_usdc, errors="coerce")
    result = {"n": len(t), "win": t.up_won.mean() * 100}
    if not bool((depth.notna() & (depth > 0)).all()):
        charges = pd.Series([fees.poly_taker_fee_dollars(1.0, pr) for pr in p],
                            index=p.index)
        return {**result, "net_c": (gross - charges).mean() * 100,
                "final": None, "dd": None, "fees": None}

    bal, peak, dd, total_fees = start, start, 0.0, 0.0
    per_contract = []
    for net, price, available in zip(gross, p, depth):
        stake = min(bal * frac, float(available))
        quantity = stake / price
        charge = fees.poly_taker_fee_dollars(quantity, price)
        bal += quantity * net - charge
        per_contract.append(net - charge / quantity)
        total_fees += charge
        peak = max(peak, bal)
        dd = max(dd, 1 - bal / peak)
        if bal <= 1:
            break
    return {**result, "net_c": sum(per_contract) / len(per_contract) * 100,
            "final": bal, "dd": dd * 100, "fees": total_fees}
