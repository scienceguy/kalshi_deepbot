#!/usr/bin/env python3
"""Train + validate the torch fair-value model from the Hugging Face Kalshi 15m set.

Why HF instead of the live API: papylove/kalshi-15m-data carries ~15k settled
15-minute windows with per-minute quotes, per-minute realized volatility and the
outcome, so training and validation are reproducible offline and never touch the
live account.

Trains MLFairValue (ml_model.py) on the older days, then validates the *entry
gate* on the newest held-out days. The gate is applied exactly like the bot's
(signals._evaluate_taker_favourite): pick the favourite in band 95-99c, compute
edge = p*100 - ask - fee, enter when edge >= MODEL_MIN_EDGE_CENTS, taker at the
ask, hold to settlement. Numbers are the same shape as the "reality tax"
measurement in AGENTS.md.

Usage:
  python scripts/train_ml_hf.py --days 14 --test-days 3 --out data/ml_model.pt
"""
import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import fees  # noqa: E402
from binance_feed import prob_above  # noqa: E402
from ml_model import MLFairValue, row_to_features  # noqa: E402
from model import gbm_probability_yes_wins  # noqa: E402

DATASET = "papylove/kalshi-15m-data"
_MIN_PER_YEAR = 365 * 24 * 60
_FEATURE_COLS = ["kalshi_yes_bid", "kalshi_yes_ask", "current_price",
                 "strike_price", "minutes_remaining", "volatility_per_min"]


# ── data ────────────────────────────────────────────────────────────────────

def _hf_files(prefix: str):
    from huggingface_hub import HfApi
    api = HfApi()
    return sorted(
        f.path for f in api.list_repo_tree(DATASET, repo_type="dataset", recursive=True)
        if f.path.startswith(prefix) and f.path.endswith(".parquet"))


def load_frames(days: int):
    """Return (feature frame with a `day` column, {ticker: 'yes'|'no'})."""
    from huggingface_hub import hf_hub_download

    parts = []
    for p in _hf_files("fair_value_log/")[-days:]:
        x = pd.read_parquet(hf_hub_download(DATASET, p, repo_type="dataset"))
        x["day"] = Path(p).stem
        parts.append(x)
    fv = pd.concat(parts, ignore_index=True)

    res: dict[str, str] = {}
    for p in _hf_files("quote_history/")[-days:]:
        x = pd.read_parquet(hf_hub_download(DATASET, p, repo_type="dataset"),
                            columns=["ticker", "result"])
        for tk, r in zip(x.ticker, x.result):
            r = str(r).strip().lower()
            if r in ("yes", "no"):
                res[tk] = r
    return fv, res


def prepare(fv: pd.DataFrame, res: dict) -> pd.DataFrame:
    """Keep usable rows, attach outcome + derived features, drop bad rows."""
    df = fv[fv.ticker.isin(res)].copy()
    df = df.dropna(subset=_FEATURE_COLS)
    df = df[(df.minutes_remaining > 0) & (df.strike_price > 0) & (df.current_price > 0)]
    df["yes_win"] = df.ticker.map(lambda t: 1.0 if res[t] == "yes" else 0.0)
    df["kalshi_mid"] = (df.kalshi_yes_bid + df.kalshi_yes_ask) / 2.0
    df["spread"] = (df.kalshi_yes_ask - df.kalshi_yes_bid).clip(lower=0.0)
    df["fair"] = [
        prob_above(s, k, v, m)
        for s, k, v, m in zip(df.current_price, df.strike_price,
                              df.volatility_per_min, df.minutes_remaining)
    ]
    df = df[df.fair.notna()]
    return df.reset_index(drop=True)


def feature_matrix(df: pd.DataFrame) -> np.ndarray:
    return np.stack([
        row_to_features({"strike": s, "kalshi": km, "fair": f, "spread": sp},
                        spot, sig, ml)
        for s, km, f, sp, spot, sig, ml in zip(
            df.strike_price, df.kalshi_mid, df.fair, df.spread,
            df.current_price, df.volatility_per_min, df.minutes_remaining)
    ])


# ── entry simulation (mirrors signals._evaluate_taker_favourite) ────────────

def _favourite(yb_c: int, ya_c: int, lo: int, hi: int):
    """(side, entry_cents) for the more-extreme favourite in band, else None."""
    cands = []
    if lo <= ya_c <= hi:
        cands.append(("YES", ya_c))
    no = 100 - yb_c
    if lo <= no <= hi:
        cands.append(("NO", no))
    if not cands:
        return None
    if len(cands) == 2:
        return cands[0] if abs(cands[0][1] - 50) >= abs(cands[1][1] - 50) else cands[1]
    return cands[0]


def simulate(df: pd.DataFrame, prob_of_yes, min_edge: float, lo: int, hi: int,
             w_start: float, w_end: float, gate: bool):
    """Scan each market's window; enter at the first minute the gate lets through.

    prob_of_yes(row_features) -> P(YES) for a row dict, or None for "no gate"
    (enter at the first in-band minute).
    """
    n = wins = 0
    pnl = 0.0
    for _, g in df.groupby("ticker"):
        g = g.sort_values("minutes_remaining", ascending=False)
        for r in g.itertuples():
            minutes_in = 15.0 - r.minutes_remaining
            if minutes_in < w_start or minutes_in > w_end:
                continue
            yb, ya = int(round(r.kalshi_yes_bid * 100)), int(round(r.kalshi_yes_ask * 100))
            fav = _favourite(yb, ya, lo, hi)
            if fav is None:
                continue
            side, entry_c = fav
            if gate:
                p_yes = prob_of_yes(r)
                if p_yes is None:
                    continue
                trade_p = p_yes if side == "YES" else 1.0 - p_yes
                fee = fees.taker_fee_cents(1.0, entry_c / 100.0)
                edge = trade_p * 100.0 - entry_c - fee
                if edge < min_edge:
                    continue
            won = (r.yes_win == 1.0) if side == "YES" else (r.yes_win == 0.0)
            cost = fees.buy_cash_out(1.0, entry_c / 100.0, taker=True)
            n += 1
            pnl += (1.0 if won else 0.0) - cost
            wins += 1 if won else 0
            break  # one entry per market
    ev = 100.0 * pnl / n if n else 0.0
    wr = 100.0 * wins / n if n else 0.0
    return n, ev, wr


def calibration(df: pd.DataFrame, p_yes: np.ndarray, label=""):
    """Brier + AUC of P(YES) against realized YES, for a calibration read."""
    y = df.yes_win.to_numpy()
    brier = float(np.mean((p_yes - y) ** 2))
    order = np.argsort(p_yes)
    y_sorted = y[order]
    n_pos, n_neg = y_sorted.sum(), (1 - y_sorted).sum()
    if n_pos and n_neg:
        ranks = np.empty(len(y), dtype=float)
        ranks[order] = np.arange(1, len(y) + 1)
        auc = (ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    else:
        auc = float("nan")
    print(f"  {label:<22} Brier={brier:.4f}  AUC={auc:.4f}")
    return brier, auc


def split_train_test(df: pd.DataFrame, test_days: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split prepared dataframe into train and test sets by day."""
    days = sorted(df.day.unique())
    if len(days) <= test_days:
        raise ValueError(f"need more than {test_days} days, have {len(days)}")
    train_days, test_days_list = days[:-test_days], days[-test_days:]
    tr = df[df.day.isin(train_days)].reset_index(drop=True)
    te = df[df.day.isin(test_days_list)].reset_index(drop=True)
    return tr, te


def train(train_df: pd.DataFrame, results: dict = None, epochs: int = 800, lr: float = 0.1) -> MLFairValue:
    """Train MLFairValue model on training feature matrix."""
    model = MLFairValue()
    Xtr, ytr = feature_matrix(train_df), train_df.yes_win.to_numpy().astype(np.float32)
    model.fit(Xtr, ytr, epochs=epochs, lr=lr)
    return model


# ── main ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--test-days", type=int, default=3)
    ap.add_argument("--epochs", type=int, default=800)
    ap.add_argument("--lr", type=float, default=0.1)
    ap.add_argument("--out", default="data/ml_model.pt")
    a = ap.parse_args(argv)

    fv, res = load_frames(a.days)
    df = prepare(fv, res)
    try:
        tr, te = split_train_test(df, test_days=a.test_days)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"days: train {tr.day.iloc[0]}..{tr.day.iloc[-1]} ({len(tr)} rows) | "
          f"test {te.day.iloc[0]}..{te.day.iloc[-1]} ({len(te)} rows)")

    model = train(tr, res, epochs=a.epochs, lr=a.lr)

    import torch
    with torch.no_grad():
        p_ml = torch.sigmoid(model(torch.from_numpy(feature_matrix(te)))).numpy()
    ann = np.clip(te.volatility_per_min.to_numpy() * math.sqrt(_MIN_PER_YEAR),
                  config.MODEL_VOL_MIN, config.MODEL_VOL_MAX)
    p_gbm_rz = np.array([prob_above(s, k, v, m) for s, k, v, m in
                         zip(te.current_price, te.strike_price, te.volatility_per_min,
                             te.minutes_remaining)])
    p_gbm_fx = np.array([gbm_probability_yes_wins(s, k, m, 0.80) for s, k, m in
                         zip(te.current_price, te.strike_price, te.minutes_remaining)])

    print("\n=== calibration of P(YES) on held-out days ===")
    calibration(te, p_gbm_fx, "gbm fixed 0.80")
    calibration(te, p_gbm_rz, "gbm realized vol")
    calibration(te, p_ml, "ML (trained)")

    lo, hi = config.ENTRY_BAND_MIN, config.ENTRY_BAND_MAX
    ws, we = config.ENTRY_WINDOW_START_MIN, config.ENTRY_WINDOW_END_MIN
    me = config.MODEL_MIN_EDGE_CENTS

    pcols = {"gbm fixed 0.80": p_gbm_fx, "gbm realized vol": p_gbm_rz, "ML": p_ml}
    te = te.reset_index(drop=True)

    print(f"\n=== entry gate on test ({lo}-{hi}c, {ws}-{we}min in, taker at ask) ===")
    print(f"  {'gate':<22} {'n':>6} {'EV/trade':>9} {'win%':>6}")
    n, ev, wr = simulate(te, None, 0.0, lo, hi, ws, we, gate=False)
    print(f"  {'none':<22} {n:>6} {ev:>8.2f}c {wr:>5.1f}%")
    results = {}
    for name, col in pcols.items():
        probs = pd.Series(col, index=te.index)
        n, ev, wr = simulate(te, lambda r, pr=probs: float(pr.loc[r.Index]),
                             me, lo, hi, ws, we, gate=True)
        results[name] = ev
        print(f"  {name:<22} {n:>6} {ev:>8.2f}c {wr:>5.1f}%")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.save(str(out))
    best = max(results, key=results.get)
    print(f"\nsaved {out} ({out.stat().st_size} bytes); best gate: {best} "
          f"({results[best]:+.2f}c/trade)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
