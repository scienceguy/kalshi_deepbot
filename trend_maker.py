"""trend_maker.py - model-gated maker round trips on 15-minute binaries (PAPER ONLY).

What it does each cycle, for markets whose YES mid sits in the trend band:

1. Track the YES mid over a short lookback and fit a least-squares slope
   (cents/minute). That is the "trend gauge".
2. Ask the enabled models for a fair YES probability: ``gbm`` (spot vs strike
   with realised 1-minute close-to-close volatility), ``momentum``
   (model.momentum_adjusted_probability on the slope) and optionally ``ml``
   (the torch model, when one is loaded). Fair value is their mean, and the
   engine fails closed if fewer than TREND_MAKER_MIN_MODELS produce one.
3. Join the best bid on the side the models and the trend agree on, only when
   fair value beats that price by TREND_MAKER_MIN_EDGE_CENTS. Entries are
   resting bids, never taker orders.
4. After a fill, rest a sell TREND_MAKER_TP_CENTS above the entry. A taker exit
   (with its fee) happens only on the stop or when the flatten clock hits.

Why paper only: nothing in this repository shows a measured edge for this
strategy, and a maker round trip on a 15-minute binary earns 2-4c while a
stop-out costs the spread plus a taker fee (about 1.75c per contract at 50c).
The engine therefore simulates fills with a conservative price-through rule,
writes every decision to trend_maker_log.jsonl, and scripts/trend_report.py
turns that log into a confidence interval. Promote to live only if that
interval clears zero over a meaningful number of round trips.

Fill model (conservative): a resting bid fills only when the market ask comes
down to or through it; a resting sell fills only when the market bid comes up
to or through it. There is no queue priority, so real fills would be fewer.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import binance_feed
import config
import fees
import gates
import model

log = logging.getLogger(__name__)

PAPER_DEFAULT_EQUITY = 100.0


# ═════════════════════════════════════════════════════════════════════
# TREND GAUGE
# ═════════════════════════════════════════════════════════════════════

class TrendTracker:
    """Rolling YES-mid history per ticker with a least-squares slope."""

    def __init__(self, maxlen: int = 600):
        self._maxlen = maxlen
        self._h: dict[str, deque] = {}

    def add(self, ticker: str, ts: float, mid: Optional[float]) -> None:
        if mid is None:
            return
        self._h.setdefault(ticker, deque(maxlen=self._maxlen)).append((ts, float(mid)))

    def slope(self, ticker: str, now: float, lookback: float):
        """Return (cents_per_minute | None, n_samples) over the lookback."""
        pts = [(t, m) for t, m in self._h.get(ticker, ()) if now - t <= lookback]
        n = len(pts)
        if n < 2:
            return None, n
        t0 = pts[0][0]
        xs = [(t - t0) / 60.0 for t, _ in pts]
        ys = [m for _, m in pts]
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 1e-9:
            return None, n
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx, n

    def prune(self, keep) -> None:
        keep = set(keep)
        for t in [t for t in self._h if t not in keep]:
            del self._h[t]


# ═════════════════════════════════════════════════════════════════════
# MODELS
# ═════════════════════════════════════════════════════════════════════

_sigma_cache: dict = {}


def realized_sigma_frac(asset: str) -> Optional[float]:
    """Std-dev of 1-minute log returns (fraction of price per sqrt-minute).

    Close-to-close standard deviation, measured directly, so there is no
    ATR-to-sigma conversion factor to get wrong.
    """
    n = max(5, int(config.TREND_MAKER_SIGMA_CANDLES))

    def _fetch():
        kl = binance_feed.get_klines(f"{asset}USDT", "1m", limit=n + 1, timeout=5)
        if not kl or len(kl) < n + 1:
            return None
        closes = [float(k[4]) for k in kl]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 5:
            return None
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)

    return gates._cached(_sigma_cache, asset, 60, _fetch)


def gbm_yes(spot, strike, minutes, sigma_frac) -> Optional[float]:
    """P(spot >= strike at expiry) with zero drift, or None if undefined."""
    if not spot or not strike or not sigma_frac or minutes <= 0:
        return None
    if spot <= 0 or strike <= 0 or sigma_frac <= 0:
        return None
    z = (spot - strike) / (spot * sigma_frac * math.sqrt(minutes))
    return gates.normal_cdf(z)


def _model_names() -> list:
    return [m.strip().lower() for m in str(config.TREND_MAKER_MODELS).split(",") if m.strip()]


def fair_yes(market, slope, *, spot=None, sigma=None,
             ml_fair: Optional[Callable] = None):
    """Mean fair P(YES) across enabled models -> (prob | None, per-model dict)."""
    names = _model_names()
    parts: dict = {}
    mid = market.mid_yes
    if "gbm" in names:
        asset = gates.asset_for(market)
        if asset:
            if spot is None:
                spot = gates.spot_for(asset)
            if sigma is None:
                sigma = realized_sigma_frac(asset)
        q = gbm_yes(spot, market.strike, market.minutes_left, sigma)
        if q is not None:
            parts["gbm"] = q
    if "momentum" in names and mid is not None and slope is not None:
        parts["momentum"] = model.momentum_adjusted_probability(
            mid, market.minutes_left, slope)
    if "ml" in names and ml_fair is not None:
        try:
            q = ml_fair(market)
        except Exception as e:  # a broken model is a missing model
            log.warning("trend ml model failed for %s: %s", market.ticker, e)
            q = None
        if q is not None:
            parts["ml"] = q
    if not parts or len(parts) < config.TREND_MAKER_MIN_MODELS:
        return None, parts
    return sum(parts.values()) / len(parts), parts


# ═════════════════════════════════════════════════════════════════════
# DECISION
# ═════════════════════════════════════════════════════════════════════

@dataclass
class Decision:
    side: Optional[str] = None
    price: Optional[int] = None
    target: Optional[int] = None
    edge: Optional[float] = None
    fair_side: Optional[float] = None
    reasons: list = field(default_factory=list)


def _side_quote(market, side):
    if side == "yes":
        return market.yes_bid, market.yes_ask
    return market.no_bid, market.no_ask


def decide(market, fair_yes_prob, slope, n_samples) -> Decision:
    """Pick a side to join, or explain why not. Pure function of its inputs."""
    d = Decision()
    cfg = config
    if fair_yes_prob is None:
        d.reasons.append("not enough models")
        return d
    if slope is None or n_samples < cfg.TREND_MAKER_MIN_SAMPLES:
        d.reasons.append(f"trend warm-up ({n_samples}/{cfg.TREND_MAKER_MIN_SAMPLES})")
        return d
    if market.minutes_left < cfg.TREND_MAKER_MIN_MINUTES_LEFT:
        d.reasons.append("too close to expiry")
        return d

    best = None
    for side in ("yes", "no"):
        bid, ask = _side_quote(market, side)
        if not bid or not ask or ask - bid < 0:
            continue
        if ask - bid > cfg.TREND_MAKER_MAX_SPREAD:
            d.reasons.append(f"{side}: spread {ask - bid}c too wide")
            continue
        price = int(bid)                      # join the bid; never cross
        if price >= ask:
            price = int(ask) - 1
        if not 1 <= price <= 98:
            continue
        if not cfg.TREND_MAKER_BAND_MIN <= price <= cfg.TREND_MAKER_BAND_MAX:
            d.reasons.append(f"{side}: {price}c outside band")
            continue
        fair_side = (fair_yes_prob if side == "yes" else 1.0 - fair_yes_prob) * 100.0
        edge = fair_side - price
        signed_slope = slope if side == "yes" else -slope
        if signed_slope < cfg.TREND_MAKER_MIN_SLOPE:
            d.reasons.append(f"{side}: trend {signed_slope:+.2f}c/min not with us")
            continue
        if edge < cfg.TREND_MAKER_MIN_EDGE_CENTS:
            d.reasons.append(f"{side}: edge {edge:.1f}c < {cfg.TREND_MAKER_MIN_EDGE_CENTS}c")
            continue
        target = min(98, price + cfg.TREND_MAKER_TP_CENTS)
        if target <= price:
            continue
        if best is None or edge > best[0]:
            best = (edge, side, price, target, fair_side)
    if best:
        d.edge, d.side, d.price, d.target, d.fair_side = best
        d.reasons = []
    return d


def size_contracts(equity: float, price_cents: int) -> int:
    """Fractional-bankroll size, hard-capped. Compounds only as equity grows."""
    if equity <= 0 or price_cents <= 0:
        return 0
    n = int(equity * config.TREND_MAKER_BANKROLL_FRACTION / (price_cents / 100.0))
    return max(0, min(config.TREND_MAKER_MAX_CONTRACTS, n))


# ═════════════════════════════════════════════════════════════════════
# ENGINE (paper)
# ═════════════════════════════════════════════════════════════════════

class TrendMakerEngine:
    """Paper executor. Never places or cancels a real order."""

    def __init__(self, *, ml_fair: Optional[Callable] = None,
                 log_path=None, now_fn: Callable[[], float] = time.time):
        self.tracker = TrendTracker()
        self.ml_fair = ml_fair
        self._now = now_fn
        self.log_path = Path(log_path) if log_path else Path(config.TREND_MAKER_LOG_FILE)
        self.entries: dict = {}     # resting paper bids
        self.positions: dict = {}   # filled, waiting to sell
        self.base_equity: Optional[float] = None
        self.realized_total = 0.0
        self.realized_today = 0.0
        self.consec_losses = 0
        self.round_trips = 0
        self._day = None

    # -- bookkeeping ---------------------------------------------------
    def _log(self, event: str, **kw) -> None:
        rec = {"ts": datetime.fromtimestamp(self._now(), timezone.utc).isoformat(),
               "event": event, "paper": True, **kw}
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            log.warning("trend_maker: could not write %s", self.log_path)

    def equity(self) -> float:
        return (self.base_equity or PAPER_DEFAULT_EQUITY) + self.realized_total

    def halted(self) -> Optional[str]:
        if self.realized_today <= -abs(config.TREND_MAKER_DAILY_LOSS_STOP):
            return f"daily loss {self.realized_today:.2f}"
        if self.consec_losses >= config.TREND_MAKER_MAX_CONSEC_LOSSES:
            return f"{self.consec_losses} consecutive losses"
        return None

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).date()
        if self._day != day:
            self._day = day
            self.realized_today = 0.0
            self.consec_losses = 0

    def _close(self, ticker: str, pos: dict, price: int, kind: str, taker: bool) -> None:
        c = pos["contracts"]
        fee = fees.taker_fee_dollars(c, price / 100.0) if taker else 0.0
        pnl = (price - pos["entry_price"]) * c / 100.0 - fee
        self.realized_total += pnl
        self.realized_today += pnl
        self.consec_losses = self.consec_losses + 1 if pnl < 0 else 0
        self.round_trips += 1
        self._log("exit", ticker=ticker, side=pos["side"], kind=kind,
                  entry=pos["entry_price"], exit=price, contracts=c,
                  fee=round(fee, 6), pnl=round(pnl, 6),
                  held_s=round(self._now() - pos["filled_ts"], 1))
        self.positions.pop(ticker, None)

    # -- one cycle -----------------------------------------------------
    def step(self, markets, balance: Optional[float] = None) -> dict:
        now = self._now()
        self._roll_day(now)
        if self.base_equity is None:
            self.base_equity = float(balance) if balance else PAPER_DEFAULT_EQUITY
        by_ticker = {m.ticker: m for m in markets}
        for m in markets:
            self.tracker.add(m.ticker, now, m.mid_yes)
        self.tracker.prune(set(by_ticker) | set(self.positions) | set(self.entries))

        self._manage_positions(by_ticker, now)
        self._manage_entries(by_ticker, now)
        opened = self._open_new(markets, now)
        return {"entries": len(self.entries), "positions": len(self.positions),
                "opened": opened, "equity": round(self.equity(), 2),
                "halted": self.halted()}

    def _manage_positions(self, by_ticker, now) -> None:
        for ticker, pos in list(self.positions.items()):
            m = by_ticker.get(ticker)
            if m is None:
                # Quote disappeared: close conservatively at the last bid we saw.
                self._close(ticker, pos, max(1, pos.get("last_bid", pos["entry_price"])),
                            "lost_quote", taker=True)
                continue
            bid, _ask = _side_quote(m, pos["side"])
            if bid:
                pos["last_bid"] = int(bid)
            if bid and bid >= pos["target"]:
                self._close(ticker, pos, pos["target"], "take_profit_maker", taker=False)
            elif bid and bid <= pos["entry_price"] - config.TREND_MAKER_STOP_CENTS:
                self._close(ticker, pos, int(bid), "stop_taker", taker=True)
            elif m.minutes_left < config.TREND_MAKER_FLATTEN_MINUTES:
                self._close(ticker, pos, int(bid or 1), "flatten_taker", taker=True)

    def _manage_entries(self, by_ticker, now) -> None:
        for ticker, e in list(self.entries.items()):
            m = by_ticker.get(ticker)
            if m is None or m.minutes_left < config.TREND_MAKER_MIN_MINUTES_LEFT:
                self.entries.pop(ticker, None)
                self._log("entry_cancel", ticker=ticker, reason="expiry/quote gone")
                continue
            _bid, ask = _side_quote(m, e["side"])
            if ask and ask <= e["price"]:
                self.entries.pop(ticker, None)
                self.positions[ticker] = {
                    **e, "entry_price": e["price"], "filled_ts": now,
                    "last_bid": e["price"]}
                self._log("fill", ticker=ticker, side=e["side"], price=e["price"],
                          contracts=e["contracts"], target=e["target"])
            elif now - e["ts"] > config.TREND_MAKER_ENTRY_TTL_SECONDS:
                self.entries.pop(ticker, None)
                self._log("entry_cancel", ticker=ticker, reason="ttl")

    def _open_new(self, markets, now) -> int:
        if self.halted():
            return 0
        slots = config.TREND_MAKER_MAX_POSITIONS - len(self.entries) - len(self.positions)
        if slots <= 0:
            return 0
        scored = []
        for m in markets:
            if m.ticker in self.entries or m.ticker in self.positions:
                continue
            slope, n = self.tracker.slope(
                m.ticker, now, config.TREND_MAKER_LOOKBACK_SECONDS)
            fair, parts = fair_yes(m, slope, ml_fair=self.ml_fair)
            d = decide(m, fair, slope, n)
            if d.side:
                scored.append((d.edge, m, d, slope, parts))
        scored.sort(key=lambda x: -x[0])
        opened = 0
        for edge, m, d, slope, parts in scored[:1]:        # one new entry per cycle
            c = size_contracts(self.equity(), d.price)
            if c < 1:
                continue
            self.entries[m.ticker] = {
                "side": d.side, "price": d.price, "target": d.target,
                "contracts": c, "ts": now}
            self._log("entry", ticker=m.ticker, side=d.side, price=d.price,
                      target=d.target, contracts=c, edge=round(edge, 2),
                      fair_side=round(d.fair_side, 2), slope=round(slope, 3),
                      models={k: round(v, 4) for k, v in parts.items()},
                      minutes_left=round(m.minutes_left, 2))
            opened += 1
        return opened

    def summary(self) -> dict:
        return {"round_trips": self.round_trips,
                "realized_total": round(self.realized_total, 4),
                "equity": round(self.equity(), 2),
                "open_entries": len(self.entries),
                "open_positions": len(self.positions),
                "halted": self.halted()}
