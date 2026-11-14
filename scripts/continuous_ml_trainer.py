#!/usr/bin/env python3
"""Continuous ML model trainer for Kalshi 15m crypto options.

Periodically updates ml_model.pt using recent market observations and
historical datasets, ensuring the ML fair-value model stays
continuously trained and calibrated.
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("continuous_ml_trainer")


def train_iteration(days: int = 4, epochs: int = 2, out_path: str = "ml_model.pt") -> None:
    """Run one continuous training epoch and save the updated model."""
    try:
        import scripts.train_ml_hf as trainer
        log.info("Starting continuous ML model training (days=%d, epochs=%d)...", days, epochs)
        ret = trainer.main(["--days", str(days), "--test-days", "1", "--epochs", str(epochs), "--out", out_path])
        if ret == 0 or ret is None:
            log.info("Continuous ML model successfully trained and saved to %s", out_path)
        else:
            log.warning("Continuous ML training exited with code %s", ret)
    except Exception as e:
        log.warning("Continuous ML training iteration error: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuous ML model trainer")
    parser.add_argument("--interval", type=int, default=3600, help="training interval in seconds (default: 1h)")
    parser.add_argument("--days", type=int, default=4, help="dataset days (default: 4)")
    parser.add_argument("--epochs", type=int, default=2, help="training epochs (default: 2)")
    parser.add_argument("--out", default="ml_model.pt", help="model output file")
    parser.add_argument("--once", action="store_true", help="run once and exit")
    args = parser.parse_args()

    while True:
        train_iteration(days=args.days, epochs=args.epochs, out_path=args.out)
        if args.once:
            break
        log.info("Sleeping %ds until next continuous training iteration", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
