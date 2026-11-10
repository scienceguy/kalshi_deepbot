#!/usr/bin/env python3
"""Retrain the torch fair-value model from settled Kalshi history.

Thin wrapper over ml_model.train_from_history. Paper-only: it writes a model
file (gitignored via `*.pt`) and nothing else. The model is NOT wired into
trader.py -- trader.py loads it only when `--ml-model` is passed, so retraining
cannot change live behaviour on its own. Continuous retraining only matters once
the model gates a measured decision; until then it just keeps the artifact fresh.

Usage:
  python scripts/train_ml.py --series KXBTCD --events 6 --out data/ml_model.pt
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ml_model  # noqa: E402


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--events", type=int, default=6)
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--out", default="data/ml_model.pt")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    model = ml_model.train_from_history(
        series=a.series, events=a.events, minutes=a.minutes,
        epochs=a.epochs, out=str(out))
    if model is None:
        print("no model produced (insufficient settled history)", file=sys.stderr)
        return 1
    print(f"ok: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
