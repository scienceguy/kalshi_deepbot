#!/usr/bin/env python3
"""Join gate shadow decisions to settlement markouts.

`gates.py` writes one record per evaluated candidate to `gates_log.jsonl`
(whether or not the candidate ever filled). `trader.py` writes one record per
settled fill to `markout_log.jsonl`. Joining them on (ticker, side) answers the
only question that matters for calibration:

    did the candidates the gates would have blocked actually settle worse?

USAGE
    python scripts/gates_join.py
    python scripts/gates_join.py --gates gates_log.jsonl --markout markout_log.jsonl
    python scripts/gates_join.py --json

`markout_cents = payout - fill price` for a YES fill, mirrored for NO, so a
positive markout is a good fill. Read-only: no orders, no writes.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GATES = ROOT / "gates_log.jsonl"
DEFAULT_MARKOUT = ROOT / "markout_log.jsonl"


def load_jsonl(path) -> list[dict]:
    """Read valid JSON objects from a JSONL file, skipping blank/corrupt lines."""
    records = []
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return records
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def join(gate_records: list[dict], markout_records: list[dict]) -> list[dict]:
    """One row per candidate: its last gate decision plus its settled fills.

    A ticker can be evaluated on several cycles; the last decision is the one
    that governed any order. Multiple fills on one (ticker, side) are kept
    because a partial fill/exit can settle twice.
    """
    latest_gate: dict = {}
    for rec in gate_records:
        key = (rec.get("ticker"), rec.get("side"))
        latest_gate[key] = rec

    fills_by_key = defaultdict(list)
    for rec in markout_records:
        key = (rec.get("ticker"), rec.get("side"))
        fills_by_key[key].append(rec)

    return [
        {"gate": gate, "fills": fills_by_key.get(key, [])}
        for key, gate in latest_gate.items()
    ]


def summarize(rows: list[dict]) -> dict:
    """Per-decision cohort stats: candidate count, fill rate, markout quality."""
    cohorts: dict = defaultdict(
        lambda: {"candidates": 0, "fills": 0, "markouts_cents": []}
    )
    for row in rows:
        cohort = cohorts[row["gate"].get("decision", "unknown")]
        cohort["candidates"] += 1
        markouts = [
            float(f["markout_cents"])
            for f in row["fills"]
            if f.get("markout_cents") is not None
        ]
        cohort["fills"] += len(markouts)
        cohort["markouts_cents"].extend(markouts)

    out = {}
    for name, cohort in cohorts.items():
        markouts = cohort.pop("markouts_cents")
        cohort["mean_markout_cents"] = statistics.mean(markouts) if markouts else None
        cohort["median_markout_cents"] = statistics.median(markouts) if markouts else None
        cohort["win_rate"] = (
            sum(1 for m in markouts if m > 0) / len(markouts) if markouts else None
        )
        cohort["fill_rate"] = (
            cohort["fills"] / cohort["candidates"] if cohort["candidates"] else None
        )
        out[name] = cohort
    return out


def _fmt(value, pct=False) -> str:
    if value is None:
        return "-"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:+.2f}"


def print_report(cohorts: dict) -> None:
    if not cohorts:
        print("No gate records yet — run the bot in shadow mode first.")
        return
    header = (
        f"{'decision':<12} {'candidates':>10} {'fills':>6} {'fill rate':>9} "
        f"{'mean mk':>8} {'median mk':>9} {'win rate':>8}"
    )
    print(header)
    print("-" * len(header))
    for name in sorted(cohorts, key=lambda n: (n != "would_block", n)):
        c = cohorts[name]
        print(
            f"{name:<12} {c['candidates']:>10} {c['fills']:>6} "
            f"{_fmt(c['fill_rate'], pct=True):>9} "
            f"{_fmt(c['mean_markout_cents']):>8} "
            f"{_fmt(c['median_markout_cents']):>9} "
            f"{_fmt(c['win_rate'], pct=True):>8}"
        )
    print(
        "\nA useful gate: would_block mean/median markout clearly below allow, "
        "with enough fills to trust it. Markout is cents per contract."
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gates", default=str(DEFAULT_GATES), help="gates JSONL path")
    ap.add_argument("--markout", default=str(DEFAULT_MARKOUT), help="markout JSONL path")
    ap.add_argument("--json", action="store_true", help="print cohorts as JSON")
    args = ap.parse_args(argv)

    rows = join(load_jsonl(args.gates), load_jsonl(args.markout))
    cohorts = summarize(rows)
    if args.json:
        print(json.dumps(cohorts, indent=2, sort_keys=True))
    else:
        print_report(cohorts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
