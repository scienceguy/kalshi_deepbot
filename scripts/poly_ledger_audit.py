#!/usr/bin/env python3
"""Reconcile the Polymarket US paper ledger against the state file.

Answers, from the files themselves: how many positions were opened, how many
reached a recorded settlement, how many were rotated out, how many are still
open — and whether the P&L the ledger records matches the realised P&L the
engine believes it has.

Any open that is neither settled, rotated nor still held is a position whose
outcome was never tracked (for example a bankroll reset that dropped it).

    python scripts/poly_ledger_audit.py --state local_data/poly_us_state.json \
        --ledger local_data/poly_us_paper_ledger.jsonl

Exit code is 1 when state and ledger disagree, so it can gate a check.
"""

import argparse
import collections
import json
from pathlib import Path


def load_ledger(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def audit(state_path, ledger_path):
    state = json.loads(Path(state_path).read_text())
    rows = load_ledger(ledger_path)

    counts = collections.Counter(r.get("event") for r in rows)
    opens = [r for r in rows if r.get("event") == "open"]
    settled = [r for r in rows if r.get("event") == "settlement"]
    rotated = [r for r in rows if r.get("event") == "rotate_exit"]

    settled_slugs = collections.Counter(r.get("slug") for r in settled)
    rotated_slugs = collections.Counter(r.get("slug") for r in rotated)
    held_slugs = collections.Counter(p.get("slug") for p in state.get("positions", []))

    unmatched = []
    seen = collections.Counter()
    for r in opens:
        slug = r.get("slug")
        seen[slug] += 1
        # Each open needs its own exit; consume settlements/rotations/held qty.
        claim = (settled_slugs[slug] + rotated_slugs[slug] + held_slugs[slug])
        if seen[slug] > claim:
            unmatched.append(r)

    ledger_realized = round(sum(r.get("pnl") or 0.0 for r in settled)
                            + sum(r.get("pnl") or 0.0 for r in rotated), 4)
    state_realized = round(float(state.get("realized_pnl") or 0.0), 4)
    state_lifetime = round(float(state.get("lifetime_realized_pnl") or 0.0), 4)
    mtm = round(sum(p.get("mtm_value", p.get("cost", 0.0)) for p in state.get("positions", [])), 4)
    equity = round(float(state.get("cash") or 0.0) + mtm, 4)

    by_day = collections.defaultdict(lambda: {"settled": 0, "pnl": 0.0})
    for r in settled:
        day = str(r.get("timestamp") or "")[:10]
        by_day[day]["settled"] += 1
        by_day[day]["pnl"] = round(by_day[day]["pnl"] + (r.get("pnl") or 0.0), 4)

    report = {
        "events": dict(counts),
        "opens": len(opens),
        "settlements": len(settled),
        "rotated": len(rotated),
        "held": len(state.get("positions", [])),
        "unsettled_opens": len(unmatched),
        "ledger_realized": ledger_realized,
        "state_realized": state_realized,
        "state_lifetime_realized": state_lifetime,
        "equity": equity,
        "cash": round(float(state.get("cash") or 0.0), 4),
        "mtm": mtm,
        "start_balance": state.get("start_balance"),
        "by_day": dict(sorted(by_day.items())),
        "unsettled_examples": [
            {"slug": r.get("slug"), "side": r.get("side"), "cost": r.get("cost"),
             "opened_at": r.get("timestamp")}
            for r in unmatched[-10:]
        ],
    }
    report["state_matches_ledger"] = abs(state_realized - ledger_realized) < 0.005
    return report


def main():
    ap = argparse.ArgumentParser(description="Audit the Polymarket US paper ledger")
    ap.add_argument("--state", default="local_data/poly_us_state.json")
    ap.add_argument("--ledger", default="local_data/poly_us_paper_ledger.jsonl")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()

    rep = audit(args.state, args.ledger)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(f"opens {rep['opens']} | settlements {rep['settlements']} | "
              f"rotated {rep['rotated']} | held {rep['held']} | "
              f"UNSETTLED OPENS {rep['unsettled_opens']}")
        print(f"ledger realized P&L {rep['ledger_realized']:+.2f} | "
              f"state realized {rep['state_realized']:+.2f} | "
              f"state lifetime {rep['state_lifetime_realized']:+.2f} | "
              f"{'MATCH' if rep['state_matches_ledger'] else 'MISMATCH'}")
        print(f"equity {rep['equity']:.2f} (cash {rep['cash']:.2f} + MTM {rep['mtm']:.2f}) "
              f"| start {rep['start_balance']}")
        for day, d in rep["by_day"].items():
            print(f"  {day}: {d['settled']} settlements, P&L {d['pnl']:+.2f}")
        if rep["unsettled_examples"]:
            print("last unsettled opens:")
            for e in rep["unsettled_examples"]:
                print(f"  {e['opened_at']} {e['slug']} {e['side']} cost={e['cost']}")
    return 0 if rep["state_matches_ledger"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
