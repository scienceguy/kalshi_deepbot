"""
train_xgboost.py — train an XGBoost fair-value model for Kalshi ladder rungs.

Designed to run under the proot-distro debian wrapper:

    ./ml_python train_xgboost.py --series KXBTCD --events 6 --minutes 60 \
        --out xgb_btc_model.json

Produces an XGBoost model file (JSON) that can be loaded for inference under
the same wrapper. This is an offline training tool — the native Termux Python
cannot import xgboost (glibc vs Bionic), so training and xgboost inference
must run through ./ml_python.

The torch model in ml_model.py runs natively and is used for live inference.
This script exists for comparison / potentially better gradient-boosted models.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from typing import List, Optional

try:
    import xgboost as xgb
except ImportError:
    xgb = None

import numpy as np

FEATURES = [
    "moneyness",
    "minutes_left",
    "sigma_min",
    "kalshi_mid",
    "spread",
    "poly_mid",
    "fair_lognormal",
]


def row_to_features(row: dict, spot: float, sigma_min: float,
                    minutes_left: float) -> np.ndarray:
    strike = float(row["strike"])
    moneyness = math.log(spot / strike) if spot > 0 and strike > 0 else 0.0
    return np.array([
        moneyness,
        float(minutes_left),
        float(sigma_min),
        float(row["kalshi"]),
        float(row.get("spread", 0.0)),
        float(row.get("poly", row["kalshi"])),
        float(row["fair"]),
    ], dtype=np.float32)


def train_from_history(series: str = "KXBTCD", events: int = 6,
                       minutes: int = 60, out: str = "xgb_model.json") -> None:
    """Train an XGBoost model on settled Kalshi history and save to `out`."""
    if xgb is None:
        raise SystemExit("xgboost not installed — run under ./ml_python")

    import crossvenue_history as ch
    from binance_feed import prob_above

    evs = ch.settled_events(series, limit=events * 4)[: events]
    if not evs:
        raise SystemExit("no settled events — nothing to train on")

    asset = ch.SERIES_ASSET[series]
    symbol = f"{asset}USDT"

    rows: List[dict] = []
    spots_all: List[float] = []
    sigmas: List[float] = []
    for ev in evs:
        close_ts = int(datetime.fromisoformat(
            ev["close_time"].replace("Z", "+00:00")).timestamp())
        start_ts = close_ts - minutes * 60
        settle = float(ev["expiration_value"])

        spots = ch.spot_closes(symbol, start_ts, close_ts)
        if not spots:
            continue

        near = {s: t for s, t in ev["strikes"].items()
                if abs(s - settle) / settle < 0.02}
        if len(near) < 4:
            continue

        sigma_by_minute: dict = {}
        for ts in sorted(spots):
            s = ch.sigma_at(ts, symbol)
            if s:
                sigma_by_minute[ts] = s

        for strike in sorted(near):
            ticker = near[strike]
            for c in ch.candles(ticker, series, start_ts, close_ts):
                ts = int(c["end_period_ts"])
                bid = ch._f(c.get("yes_bid"))
                ask = ch._f(c.get("yes_ask"))
                if bid is None or ask is None or ask < bid or ask - bid > 0.08:
                    continue
                mid = (bid + ask) / 2.0
                if not 0.0 < mid < 1.0:
                    continue
                spot = spots.get(ts - ts % 60)
                if spot is None:
                    continue
                mins_left = (close_ts - ts) / 60.0
                if mins_left <= 0:
                    continue
                sigma = sigma_by_minute.get(ts, 0.0007)
                sigmas.append(sigma)
                spots_all.append(spot)
                rows.append({
                    "strike": float(strike),
                    "spot": spot,
                    "sigma": sigma,
                    "kalshi": mid,
                    "poly": mid,
                    "spread": ask - bid,
                    "fair": prob_above(spot, float(strike), sigma, mins_left),
                    "minutes_left": mins_left,
                    "settled_yes": 1.0 if settle > float(strike) else 0.0,
                })

    if len(rows) < 20:
        raise SystemExit(f"only {len(rows)} labeled rows — need ~20+ to train")

    X = np.stack([row_to_features(r, r.get("spot"), r.get("sigma"),
                                  float(r["minutes_left"])) for r in rows])
    y = np.array([float(r["settled_yes"]) for r in rows], dtype=np.float32)

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        objective="binary:logistic",
        eval_metric="logloss",
        n_jobs=1,
    )
    model.fit(X, y)
    model.save_model(out)
    print(f"trained XGBoost on {len(rows)} rows from {len(evs)} events -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description="train XGBoost fair-value model")
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--events", type=int, default=6)
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--out", default="xgb_model.json")
    args = ap.parse_args()
    train_from_history(args.series, args.events, args.minutes, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
