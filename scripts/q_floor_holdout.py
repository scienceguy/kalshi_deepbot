#!/usr/bin/env python3
"""Recompute the measured-q floor from settled fills, in- and out-of-sample.

GATES_MEASURED_Q=0.9928 came from n=557 in-sample QUOTE rows (553 wins) at
95-99c: quotes, not fills, and nothing held out, so its Wilson 95% lower bound
sits ~+1.2c below the constant. This script measures the YES-side win rate
from settled FILLS in the local DB instead, on a time-ordered train/holdout
split (first half train, second half holdout), and reports the holdout rate
with its Wilson 95% lower bound and n. It refuses to print a number without
both.

The gate (gates.py) applies the floor only when the measurement file says
n >= the gate's own minimum; below that the gate refuses the floor.

Offline; reads only local files. Exit 0 when a holdout number is printed,
1 when refused.

    python scripts/q_floor_holdout.py [--db PATH] [--out PATH]
                                      [--band-min 95] [--band-max 99]
                                      [--holdout-frac 0.5] [--min-n 100]
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "local_data" / "superbot_local.db"
DEFAULT_OUT = ROOT / "local_data" / "q_floor.json"

# Two-sided 95% Wilson score interval z (alpha = 0.05).
Z95 = 1.959964


def wilson_lower(wins: int, n: int, z: float = Z95) -> float | None:
    """Wilson 95% lower bound on a win rate; None when there is no sample."""
    if n <= 0:
        return None
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return centre - half


def yes_settlement(side: str, outcome_won: int) -> int:
    """YES-side outcome of a fill: YES wins when (yes & won) or (no & lost)."""
    if side == "yes":
        return int(bool(outcome_won))
    return 1 - int(bool(outcome_won))


def load_settled_fills(db_path) -> list[dict]:
    """In-band eligible fills, time-ordered, deduped by order_id (keep last)."""
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT side, fill_price_cents, outcome_won, settled_at, order_id
            FROM orders
            WHERE settled_at IS NOT NULL
              AND outcome_won IS NOT NULL
              AND fill_price_cents IS NOT NULL
            ORDER BY settled_at ASC, order_id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    by_order: dict = {}
    for side, price, won, settled_at, order_id in rows:
        # A replay writes the same order again; the newest row wins.
        by_order[order_id or (settled_at, side, price)] = (settled_at, side, price, won)
    fills = []
    for settled_at, side, price, won in sorted(by_order.values(), key=lambda r: r[0]):
        yes_equiv = int(price) if side == "yes" else 100 - int(price)
        fills.append({"settled_at": settled_at, "yes_equiv": yes_equiv,
                      "yes_outcome": yes_settlement(side, won)})
    return fills


def holdout_report(db_path, *, band_min: int = 95, band_max: int = 99,
                   holdout_frac: float = 0.5) -> dict:
    """Train/holdout rate on the band; holdout is the newest holdout_frac rows."""
    fills = [f for f in load_settled_fills(db_path)
             if band_min <= f["yes_equiv"] <= band_max]
    n = len(fills)
    if n == 0:
        return {"train_n": 0, "train_rate": None, "holdout_n": 0,
                "holdout_wins": 0, "holdout_rate": None, "wilson_lower": None,
                "band_cents": [band_min, band_max], "holdout_frac": holdout_frac}
    split = int(round(n * (1.0 - holdout_frac)))
    train, holdout = fills[:split], fills[split:]
    train_rate = sum(f["yes_outcome"] for f in train) / len(train) if train else None
    hw = sum(f["yes_outcome"] for f in holdout)
    holdout_rate = hw / len(holdout) if holdout else None
    return {
        "train_n": len(train),
        "train_rate": train_rate,
        "holdout_n": len(holdout),
        "holdout_wins": hw,
        "holdout_rate": holdout_rate,
        "wilson_lower": wilson_lower(hw, len(holdout)),
        "band_cents": [band_min, band_max],
        "holdout_frac": holdout_frac,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="local SQLite holding the orders table (read-only)")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="q_floor.json for gates.py to consume")
    parser.add_argument("--band-min", type=int, default=95)
    parser.add_argument("--band-max", type=int, default=99)
    parser.add_argument("--holdout-frac", type=float, default=0.5)
    parser.add_argument("--min-n", type=int, default=100,
                        help="gate minimum holdout n (warning only; the gate enforces its own)")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"refusing: no settled-fills database at {db}", file=sys.stderr)
        return 1
    repo_db = db.resolve()
    rep = holdout_report(repo_db, band_min=args.band_min, band_max=args.band_max,
                         holdout_frac=args.holdout_frac)
    source = f"orders.settled_fills:{db.name}"
    hn = rep["holdout_n"]
    common = {"q_floor_n": hn, "q_floor_source": source,
              "band_cents": rep["band_cents"], "holdout_frac": rep["holdout_frac"],
              "train_n": rep["train_n"], "train_rate": rep["train_rate"],
              "generated_at": datetime.now(timezone.utc).isoformat()}
    if hn == 0:
        print("refusing to print a holdout rate: n=0 in-band settled fills "
              "(no number without an n)")
        Path(args.out).write_text(json.dumps({**common, "q_floor": None}, indent=2) + "\n")
        return 1
    out_f = rep["wilson_lower"]
    payload = {**common,
               "q_floor": out_f,
               "wilson_lower_95": out_f,
               "holdout_rate": rep["holdout_rate"],
               "holdout_wins": rep["holdout_wins"]}
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
    print(f"q_floor_holdout band {args.band_min}-{args.band_max}c (yes-equiv) "
          f"time-ordered split {1 - args.holdout_frac:.2f}/{args.holdout_frac:.2f}")
    print(f"train   n={rep['train_n']} rate={rep['train_rate']}")
    print(f"holdout n={hn} wins={rep['holdout_wins']} rate={rep['holdout_rate']} "
          f"wilson95_lower={out_f}")
    print(f"wrote {args.out}; gate applies floor only when holdout n >= {args.min_n}")
    if hn < args.min_n:
        print(f"note: holdout n={hn} < {args.min_n}; the gate will refuse the floor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
