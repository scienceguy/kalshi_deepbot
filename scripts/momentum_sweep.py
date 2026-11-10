#!/usr/bin/env python3
"""Sweep momentum-scalper variants over the historical candle corpus.

Diagnostic only. For each variant it replays the corpus through the UNCHANGED
trend_maker engine (via scripts/replay_trend.main), then reports net-of-fee
mean P&L per round trip with a bootstrap 95% CI. The corpus is split by time
into a DISCOVER half and a HOLDOUT half; a variant is only interesting if the
CI lower bound clears zero on the holdout it never searched -- searching the
discover half and promoting the winner is overfitting, not an edge.

Nothing here touches trader.py, config defaults, risk limits, or any live path:
it monkeypatches config in-process for the duration of one variant and restores
it. No orders are possible -- the engine holds no Kalshi client.

Usage:
  python scripts/momentum_sweep.py --corpus data/candles_kxbtc15m.jsonl
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# /tmp is not writable here; use the approved scratch dir for split corpora.
SCRATCH = Path(os.path.expanduser("~/tmp/opencode"))
SCRATCH.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(SCRATCH)

import config  # noqa: E402
import replay_trend  # noqa: E402
import trend_report  # noqa: E402

# Each variant is a dict of TREND_MAKER_* overrides. Only the momentum/edge
# knobs are swept; the fee-relevant exit kinds are left to the engine as-is.
VARIANTS = [
    ("shipped",           {}),
    ("wide_edge_6",       {"TREND_MAKER_MIN_EDGE_CENTS": 6.0}),
    ("thin_edge_2",       {"TREND_MAKER_MIN_EDGE_CENTS": 2.0}),
    ("big_tp_5",          {"TREND_MAKER_TP_CENTS": 5}),
    ("tight_stop_4",      {"TREND_MAKER_STOP_CENTS": 4}),
    ("wide_band_20_80",   {"TREND_MAKER_BAND_MIN": 20, "TREND_MAKER_BAND_MAX": 80}),
    ("no_slope_gate",     {"TREND_MAKER_MIN_SLOPE": 0.0}),
    ("min_samples_4",     {"TREND_MAKER_MIN_SAMPLES": 4}),  # diagnostic only
]
KNOBS = sorted({k for _, v in VARIANTS for k in v})


def split_corpus(path: Path, fraction: float = 0.7):
    """Write discover/holdout corpus files split by open_ts. Returns paths."""
    recs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
    recs.sort(key=lambda r: r["open_ts"])
    cut = int(len(recs) * fraction)
    out = []
    for name, chunk in (("discover", recs[:cut]), ("holdout", recs[cut:])):
        p = SCRATCH / f"sweep_{name}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in chunk))
        out.append((name, p, len(chunk)))
    return out


def run_variant(overrides: dict, corpus: Path):
    """Replay one variant; return exit rows (each carries 'pnl')."""
    saved = {k: getattr(config, k) for k in KNOBS}
    for k, v in overrides.items():
        setattr(config, k, v)
    out = SCRATCH / "sweep_trip.log"
    out.unlink(missing_ok=True)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            rc = replay_trend.main(["--corpus", str(corpus), "--out", str(out)])
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
    if rc != 0 or not out.exists():
        return []
    rows = []
    for line in out.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("event") == "exit":
            rows.append(rec)
    return rows


def summarise(pnl):
    import statistics
    if not pnl:
        return None
    mean = statistics.fmean(pnl)
    lo, hi = trend_report.bootstrap_ci(pnl) if len(pnl) > 1 else (mean, mean)
    wins = sum(1 for p in pnl if p > 0)
    return {"n": len(pnl), "win": wins / len(pnl), "mean": mean, "lo": lo, "hi": hi}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="data/candles_kxbtc15m.jsonl")
    ap.add_argument("--min-trips", type=int, default=30)
    args = ap.parse_args(argv)

    corpus = Path(args.corpus)
    if not corpus.exists():
        print(f"missing corpus: {corpus}", file=sys.stderr)
        return 2
    splits = split_corpus(corpus)
    for name, p, n in splits:
        print(f"# {name}: {n} markets -> {p}", file=sys.stderr)

    hdr = f"{'variant':18} {'split':9} {'n':>4} {'win%':>6} {'mean$':>9} {'CI_lo$':>9} {'CI_hi$':>9}  verdict"
    print(hdr)
    print("-" * len(hdr))
    any_ok = False
    for label, overrides in VARIANTS:
        for name, path, _ in splits:
            s = summarise([r["pnl"] for r in run_variant(overrides, path)])
            if s is None:
                print(f"{label:18} {name:9} {'-':>4} {'-':>6} {'-':>9} {'-':>9} {'-':>9}  no trips")
                continue
            ok = s["lo"] > 0 and s["n"] >= args.min_trips and name == "holdout"
            any_ok = any_ok or ok
            verdict = ("CI>0 on HOLDOUT" if ok else
                       "ci>0 (discover only)" if s["lo"] > 0 and name == "discover" else
                       "not proven")
            print(f"{label:18} {name:9} {s['n']:>4} {s['win']*100:>6.1f} "
                  f"{s['mean']:>9.4f} {s['lo']:>9.4f} {s['hi']:>9.4f}  {verdict}")
    print()
    print("verdict:", "a momentum variant cleared zero on holdout"
          if any_ok else "NO momentum variant cleared zero on holdout")
    return 0 if any_ok else 2


if __name__ == "__main__":
    sys.exit(main())
