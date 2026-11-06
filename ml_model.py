"""
ml_model.py — tiny torch fair-value model for Kalshi ladder rungs.

A single linear layer maps ladder features to P(settles YES). It learns the
residual the plain lognormal fair value misses (volatility smile, basis drift,
venue quoting quirks) and its output plugs into plan_orders as `fair`.

Read-only training from settled Kalshi history; inference is one forward pass.
No orders are placed anywhere.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import List, Optional

import numpy as np
import torch
from torch import nn

FEATURES = [
    "moneyness",       # log(spot / strike) — how far in/out of the money
    "minutes_left",    # time to expiry
    "sigma_min",       # per-minute realised vol
    "kalshi_mid",      # Kalshi venue mid (probability)
    "spread",          # Kalshi book width (liquidity / adverse-selection hint)
    "poly_mid",        # Polymarket mid (cross-venue check)
    "fair_lognormal",  # baseline spot-implied fair value
]


def row_to_features(row: dict, spot: float, sigma_min: float,
                    minutes_left: float) -> np.ndarray:
    """Flatten one `three_way` row into the fixed feature vector.

    Required keys: `strike`, `kalshi`, `fair`. `spread` and `poly` default to 0
    if absent (historical snapshots don't carry them).
    """
    strike = row.get("strike")
    kalshi = row.get("kalshi")
    fair = row.get("fair")
    if strike is None or kalshi is None or fair is None:
        missing = [k for k, v in (("strike", strike), ("kalshi", kalshi),
                                  ("fair", fair)) if v is None]
        raise ValueError(f"row missing required feature key(s): {missing}")
    moneyness = math.log(spot / strike) if spot > 0 and strike > 0 else 0.0
    return np.array([
        moneyness,
        float(minutes_left),
        float(sigma_min),
        float(kalshi),
        float(row.get("spread", 0.0)),
        float(row.get("poly", kalshi)),
        float(fair),
    ], dtype=np.float32)


class MLFairValue(nn.Module):
    """Logistic regression over ladder features -> P(settles YES).

    Outputs raw logits; :meth:`predict_proba` applies a sigmoid. Using
    ``BCEWithLogitsLoss`` (sigmoid fused into the loss) keeps gradients stable
    instead of saturating like a separate ``Sigmoid`` + ``BCELoss`` would.
    """

    def __init__(self, n_in: int = len(FEATURES)):
        super().__init__()
        self.net = nn.Linear(n_in, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)

    @torch.no_grad()
    def predict_proba(self, row: dict, spot: float, sigma_min: float,
                      minutes_left: float) -> float:
        x = torch.from_numpy(row_to_features(row, spot, sigma_min, minutes_left))
        return float(torch.sigmoid(self.forward(x)))

    def fit(self, X: np.ndarray, y: np.ndarray, epochs: int = 1000,
            lr: float = 0.05, verbose: bool = False) -> List[float]:
        X_t = torch.from_numpy(X.astype(np.float32))
        y_t = torch.from_numpy(y.astype(np.float32))
        opt = torch.optim.SGD(self.parameters(), lr=lr)
        loss_fn = nn.BCEWithLogitsLoss()
        losses: List[float] = []
        for ep in range(epochs):
            opt.zero_grad()
            pred = self.forward(X_t)
            loss = loss_fn(pred, y_t)
            loss.backward()
            opt.step()
            losses.append(float(loss.detach()))
            if verbose and (ep == 0 or ep % 250 == 249 or ep == epochs - 1):
                print(f"  epoch {ep+1}/{epochs} loss {losses[-1]:.4f}")
        return losses

    def save(self, path: str) -> None:
        torch.save({
            "state_dict": self.state_dict(),
            "features": FEATURES,
        }, path)

    @classmethod
    def load(cls, path: str) -> "MLFairValue":
        # weights_only=True: checkpoint holds only tensors (state_dict) and a
        # list of strings (features) — both safe under the restricted loader.
        ckpt = torch.load(path, weights_only=True)
        model = cls(n_in=len(ckpt.get("features", FEATURES)))
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        return model


def train_labeled(rows: List[dict], spot: Optional[float] = None,
                  sigma_min: Optional[float] = None,
                  epochs: int = 300, lr: float = 0.1) -> MLFairValue:
    """Train on rows that each carry a `settled_yes` label (0 or 1).

    Each row may carry its own `spot` and `sigma`; otherwise the scalar
    fallbacks are used (useful for quick synthetic tests).
    """
    model = MLFairValue()
    X = np.stack([
        row_to_features(
            r,
            r.get("spot") if r.get("spot") is not None else spot or 0.0,
            r.get("sigma") if r.get("sigma") is not None else sigma_min or 0.0,
            float(r["minutes_left"]),
        )
        for r in rows
    ])
    y = np.array([float(r["settled_yes"]) for r in rows], dtype=np.float32)
    if len(np.unique(y)) < 2:
        raise ValueError("need both YES and NO settled outcomes to train")
    model.fit(X, y, epochs=epochs, lr=lr)
    return model


def train_from_history(series: str = "KXBTCD", events: int = 6,
                       minutes: int = 60, epochs: int = 300,
                       out: str = "ml_model.pt") -> Optional[MLFairValue]:
    """Pull settled Kalshi history, label each rung by the settlement, train,
    and save to `out`. Returns the model or None if there is no data."""
    import crossvenue_history as ch
    from binance_feed import prob_above

    evs = ch.settled_events(series, limit=events * 4)[: events]
    if not evs:
        print("no settled events — nothing to train on")
        return None

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

        # Only strikes near the settlement value carry information; the wings
        # sit at 0/1 and cannot be inverted or learned from.
        near = {s: t for s, t in ev["strikes"].items()
                if abs(s - settle) / settle < 0.02}
        if len(near) < 4:
            continue

        # Collect per-minute sigma once (shared across strikes at a minute).
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
                    "poly": mid,            # no separate PM history; mirror
                    "spread": ask - bid,
                    "fair": prob_above(spot, float(strike), sigma, mins_left),
                    "minutes_left": mins_left,
                    "settled_yes": 1.0 if settle > float(strike) else 0.0,
                })

    if len(rows) < 20:
        print(f"only {len(rows)} labeled rows — need ~20+ to train")
        return None

    spot = float(np.median(spots_all))
    sigma = float(np.median(sigmas)) if sigmas else 0.0007
    model = train_labeled(rows, spot, sigma, epochs=epochs, lr=lr)
    model.save(out)
    print(f"trained on {len(rows)} rows from {len(evs)} events -> {out}")
    return model
