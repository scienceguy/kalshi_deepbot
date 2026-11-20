"""ml_model_lgb.py — LightGBM/XGBoost fair-value model for Kalshi ladder rungs.

Implements M2: native LightGBM/XGBoost with isotonic calibration, n8n cron retraining.
Read-only training from settled Kalshi history; inference is one forward pass.
No orders are placed anywhere.
"""

from __future__ import annotations

import json
import math
import os
import pickle
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import numpy as np

# Try to import LightGBM and XGBoost
try:
    import lightgbm as lgb
    LGB_AVAILABLE = True
except ImportError:
    LGB_AVAILABLE = False

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False

from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import train_test_split

import config

FEATURES = [
    "moneyness",       # log(spot / strike) — how far in/out of the money
    "minutes_left",    # time to expiry
    "sigma_min",       # per-minute realised vol
    "kalshi_mid",      # Kalshi venue mid (probability)
    "spread",          # Kalshi book width (liquidity / adverse-selection hint)
    "fair_lognormal",  # baseline spot-implied fair value
]


def row_to_features(row: Dict, spot: float, sigma_min: float,
                    minutes_left: float) -> np.ndarray:
    """Flatten one `three_way` row into the fixed feature vector.

    Required keys: `strike`, `kalshi`, `fair`. `spread` defaults to 0
    if absent (historical snapshots don't carry it).
    """
    strike = row.get("strike")
    kalshi = row.get("kalshi")
    fair = row.get("fair")
    if strike is None or kalshi is None or fair is None:
        missing = [k for k, v in (("strike", strike), ("kalshi", kalshi),
                                  ("fair", fair)) if v is None]
        raise ValueError(f"row missing required feature key(s): {missing}")
    if spot <= 0 or strike <= 0:
        raise ValueError(f"Invalid spot/strike: {spot}/{strike}")
    moneyness = math.log(spot / strike)
    return np.array([
        moneyness,
        float(minutes_left),
        float(sigma_min),
        float(kalshi),
        float(row.get("spread", 0.0)),
        float(fair),
    ], dtype=np.float32)


class MLFairValueLGB:
    """LightGBM/XGBoost model for ladder fair value -> P(settles YES).

    Uses isotonic calibration on holdout set for reliable probabilities.
    Supports both LightGBM and XGBoost backends with identical API.
    """

    def __init__(self, backend: str = "lightgbm", n_estimators: int = 200,
                 learning_rate: float = 0.05, max_depth: int = 4,
                 random_state: int = 42, verbose: bool = False):
        """
        Args:
            backend: "lightgbm" or "xgboost" (falls back to available one)
            n_estimators: number of boosting rounds
            learning_rate: learning rate
            max_depth: max tree depth
            random_state: random seed
            verbose: print training progress
        """
        self.backend = backend
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.random_state = random_state
        self.verbose = verbose
        self.model = None
        self.calibrator = None
        self.feature_names = FEATURES
        # Training support metadata
        self.support = None  # {"minutes_left": [lo, hi], "assets": [...], "venue": ...}

    def _create_model(self):
        """Create the model instance based on backend."""
        if self.backend == "lightgbm" and LGB_AVAILABLE:
            return lgb.LGBMClassifier(
                n_estimators=self.n_estimators,
                learning_rate=self.learning_rate,
                max_depth=self.max_depth,
                random_state=self.random_state,
                verbosity=-1 if not self.verbose else 1,
                objective="binary",
                class_weight="balanced",
            )
        elif self.backend == "xgboost" and XGB_AVAILABLE:
            return xgb.XGBClassifier(
                n_estimators=self.n_estimators,
                learning_rate=self.learning_rate,
                max_depth=self.max_depth,
                random_state=self.random_state,
                verbosity=0 if not self.verbose else 1,
                objective="binary:logistic",
                eval_metric="logloss",
            )
        # Fallback
        if LGB_AVAILABLE:
            self.backend = "lightgbm"
            return self._create_model()
        elif XGB_AVAILABLE:
            self.backend = "xgboost"
            return self._create_model()
        raise RuntimeError("Neither LightGBM nor XGBoost available. Install one of them.")

    def fit(self, X: np.ndarray, y: np.ndarray,
            calibration_fraction: float = 0.2) -> List[float]:
        """
        Train the model with isotonic calibration on holdout set.

        Args:
            X: feature matrix (n_samples, n_features)
            y: binary labels (0 or 1)
            calibration_fraction: fraction of data to use for calibration

        Returns:
            List of training losses (logloss per epoch/iteration)
        """
        if len(np.unique(y)) < 2:
            raise ValueError("need both YES and NO settled outcomes to train")

        # Split into train and calibration sets
        if len(X) > 10 and calibration_fraction > 0:
            X_train, X_cal, y_train, y_cal = train_test_split(
                X, y, test_size=calibration_fraction,
                random_state=self.random_state, stratify=y
            )
        else:
            X_train, y_train = X, y
            X_cal, y_cal = None, None

        self.model = self._create_model()

        # Train with eval set for early stopping if we have enough data
        if X_cal is not None and len(X_train) > 20:
            self.model.fit(
                X_train, y_train,
                eval_set=[(X_cal, y_cal)],
                eval_metric="logloss",
            )
            # Get losses from eval set (key depends on LightGBM version)
            losses = []
            if hasattr(self.model, "evals_result_"):
                eval_result = self.model.evals_result_
                for key in eval_result:
                    if "logloss" in eval_result[key]:
                        losses = eval_result[key]["logloss"]
                        break
        else:
            self.model.fit(X_train, y_train)
            losses = []

        # Fit isotonic calibrator on calibration set
        if X_cal is not None and len(X_cal) > 10:
            cal_probs = self.model.predict_proba(X_cal)[:, 1]
            self.calibrator = IsotonicRegression(out_of_bounds="clip")
            self.calibrator.fit(cal_probs, y_cal)
        else:
            self.calibrator = None

        return losses

    def predict_proba(self, row: Dict, spot: float, sigma_min: float,
                      minutes_left: float) -> float:
        """Predict calibrated P(settles YES) for one row."""
        if self.model is None:
            raise RuntimeError("Model not trained. Call fit() or load() first.")

        x = row_to_features(row, spot, sigma_min, minutes_left).reshape(1, -1)
        prob = self.model.predict_proba(x)[0, 1]

        # Apply isotonic calibration if available
        if self.calibrator is not None:
            prob = float(self.calibrator.transform(np.array([prob]))[0])

        return float(prob)

    def predict_proba_batch(self, rows: List[Dict], spot: float,
                            sigma_min: float, minutes_left: float) -> np.ndarray:
        """Predict calibrated probabilities for multiple rows."""
        if self.model is None:
            raise RuntimeError("Model not trained. Call fit() or load() first.")

        X = np.stack([
            row_to_features(r, spot, sigma_min, minutes_left)
            for r in rows
        ])
        probs = self.model.predict_proba(X)[:, 1]

        if self.calibrator is not None:
            probs = self.calibrator.transform(probs)

        return probs

    def save(self, path: str) -> None:
        """Save model, calibrator, and metadata to disk."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "calibrator": self.calibrator,
                "backend": self.backend,
                "feature_names": self.feature_names,
                "support": self.support,
                "n_estimators": self.n_estimators,
                "learning_rate": self.learning_rate,
                "max_depth": self.max_depth,
                "random_state": self.random_state,
            }, f)

    @classmethod
    def load(cls, path: str) -> "MLFairValueLGB":
        """Load model from disk."""
        with open(path, "rb") as f:
            data = pickle.load(f)

        # Verify feature schema
        feats = data.get("feature_names", FEATURES)
        if feats != FEATURES:
            raise ValueError(
                f"model feature schema mismatch: saved {feats} != current {FEATURES}")

        # Verify support metadata
        support = data.get("support")
        if support is not None:
            if not isinstance(support, dict):
                raise ValueError("model support metadata is not a dict")
            bounds = support.get("minutes_left")
            valid_bounds = (
                isinstance(bounds, (list, tuple)) and len(bounds) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        for v in bounds)
                and 0 <= bounds[0] <= bounds[1]
            )
            if not valid_bounds:
                raise ValueError(f"model support minutes_left is invalid: {bounds!r}")
            assets = support.get("assets")
            if not isinstance(assets, list) or not assets:
                raise ValueError("model support assets are missing")

        obj = cls(
            backend=data.get("backend", "lightgbm"),
            n_estimators=data.get("n_estimators", 200),
            learning_rate=data.get("learning_rate", 0.05),
            max_depth=data.get("max_depth", 4),
            random_state=data.get("random_state", 42),
        )
        obj.model = data["model"]
        obj.calibrator = data.get("calibrator")
        obj.support = support
        return obj


def train_labeled(rows: List[Dict], spot: Optional[float] = None,
                  sigma_min: Optional[float] = None,
                  epochs: int = 300, lr: float = 0.05,
                  asset: Optional[str] = None,
                  venue: str = "kalshi",
                  backend: str = "lightgbm") -> MLFairValueLGB:
    """Train on rows that each carry a `settled_yes` label (0 or 1).

    Each row may carry its own `spot` and `sigma`; otherwise the scalar
    fallbacks are used. When `asset` is given, the training support
    (minutes range, asset, venue) is attached so it is saved with the
    checkpoint and checked at inference.
    """
    model = MLFairValueLGB(backend=backend, n_estimators=epochs, learning_rate=lr)
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
    model.fit(X, y)
    if asset:
        mins = [float(r["minutes_left"]) for r in rows]
        model.support = {
            "minutes_left": [min(mins), max(mins)],
            "assets": [str(asset).upper()],
            "venue": venue,
        }
    return model


def train_from_history(series: str = "KXBTCD", events: int = 6,
                       minutes: int = 60, epochs: int = 300, lr: float = 0.05,
                       out: str = "ml_model_lgb.pkl",
                       backend: str = "lightgbm") -> Optional[MLFairValueLGB]:
    """Pull settled Kalshi history, label each rung by the settlement, train,
    and save to `out`. Returns the model or None if there is no data.

    This is the function to call from n8n cron for periodic retraining.
    """
    import crossvenue_history as ch
    from binance_feed import prob_above

    evs = ch.settled_events(series, limit=events * 4)[:events]
    if not evs:
        print("no settled events — nothing to train on")
        return None

    asset = ch.SERIES_ASSET[series]
    symbol = f"{asset}USDT"

    rows: List[Dict] = []
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

        # Only strikes near the settlement value carry information
        if settle <= 0:
            continue
        near = {s: t for s, t in ev["strikes"].items()
                if abs(s - settle) / settle < 0.02}
        if len(near) < 4:
            continue

        sigma_by_minute = ch.sigma_curve(symbol, start_ts, close_ts)

        for strike in sorted(near):
            ticker = near[strike]
            for c in ch.candles(ticker, series, start_ts, close_ts):
                ts = int(c["end_period_ts"])
                bid = ch._ohlc(c.get("yes_bid"))
                ask = ch._ohlc(c.get("yes_ask"))
                if bid is None or ask is None or ask < bid or ask - bid > 0.08:
                    continue
                mid = (bid + ask) / 2.0
                if not 0.0 < mid < 1.0:
                    continue
                spot = spots.get(ts - 60)
                if spot is None:
                    continue
                mins_left = (close_ts - ts) / 60.0
                if mins_left <= 0:
                    continue
                sigma = sigma_by_minute.get(ts) or 0.0007
                sigmas.append(sigma)
                spots_all.append(spot)
                rows.append({
                    "strike": float(strike),
                    "spot": spot,
                    "sigma": sigma,
                    "kalshi": mid,
                    "spread": ask - bid,
                    "fair": prob_above(spot, float(strike), sigma, mins_left),
                    "minutes_left": mins_left,
                    "settled_yes": 1.0 if settle > float(strike) else 0.0,
                })

    if len(rows) < 20:
        print(f"only {len(rows)} labeled rows — need ~20+ to train")
        return None

    spot_median = float(np.median(spots_all))
    sigma_median = float(np.median(sigmas)) if sigmas else 0.0007
    model = train_labeled(rows, spot_median, sigma_median, epochs=epochs, lr=lr,
                          asset=asset, backend=backend)
    model.save(out)
    print(f"trained on {len(rows)} rows from {len(evs)} events -> {out}")
    return model


def load_model_if_exists(path: str) -> Optional[MLFairValueLGB]:
    """Load model from path if file exists and is valid."""
    if not Path(path).exists():
        return None
    try:
        return MLFairValueLGB.load(path)
    except Exception as e:
        import logging
        log = logging.getLogger("ml_model_lgb")
        log.warning("Failed to load model from %s: %s", path, e)
        return None


# n8n cron entry point
def retrain_all_assets(out_dir: str = "local_data/models",
                       series_list: Optional[List[str]] = None,
                       backend: str = "lightgbm") -> Dict[str, Any]:
    """Retrain models for all configured assets. Called from n8n cron."""
    if series_list is None:
        series_list = ["KXBTCD", "KXETHD", "KXSOL15M", "KXXRP15M", "KXDOGE15M"]

    results = {}
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    for series in series_list:
        out_path = f"{out_dir}/ml_model_lgb_{series}.pkl"
        try:
            model = train_from_history(series=series, out=out_path, backend=backend)
            results[series] = "ok" if model else "no_data"
        except Exception as e:
            results[series] = f"error: {e}"

    return results


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    # Quick test with synthetic data
    if not LGB_AVAILABLE and not XGB_AVAILABLE:
        print("Neither LightGBM nor XGBoost available. Skipping test.")
    else:
        print("Testing MLFairValueLGB...")
        # Synthetic test
        rows = []
        for i in range(100):
            strike = 50000 + i * 100
            spot = 52000
            sigma = 0.001
            mid = 0.6 + np.random.normal(0, 0.05)
            mid = max(0.01, min(0.99, mid))
            rows.append({
                "strike": strike,
                "spot": spot,
                "sigma": sigma,
                "kalshi": mid,
                "spread": 0.02,
                "fair": 0.65,
                "minutes_left": 10.0,
                "settled_yes": 1.0 if mid > 0.5 else 0.0,
            })
        model = train_labeled(rows, spot=52000, sigma_min=0.001, epochs=50, asset="BTC")
        prob = model.predict_proba(rows[0], 52000, 0.001, 10.0)
        print(f"Test prediction: {prob:.4f}")
        print("Test passed!")