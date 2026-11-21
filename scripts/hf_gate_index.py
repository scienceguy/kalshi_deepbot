"""Index-priced replay of the gate sweep: is the candidate edge basis or slowness?

Reuses scripts/hf_gate_sweep.py's own load()/eval_cfg() and hf_fairvalue.py's
documented fair-value formula, swapping ONE input -- the spot price. The
composite feed measured +6.6..+26bps rich against the CF Benchmarks BRTI
settlement index on 2026-11-21 (live adjudication, n=4 windows), so a fair
value computed on the composite can manufacture edge that does not exist
against the index the contracts actually settle on.

For every fair_value_log row: index_px = last BRTI tick at or before the row
timestamp; fair_index = P(YES) from the same formula with index_px. Re-runs
the config grid and reports composite-EV vs index-EV side by side on the
held-out test days (2026-10-09..10-11), plus the row-level basis distribution.
Nothing is synthesised: spot, strike, vol, minutes, quotes and the settlement
result are recorded values; only the spot source changes.
"""
import argparse
import importlib.util
import json
import re

import numpy as np
import polars as pl
from scipy.special import ndtr

spec = importlib.util.spec_from_file_location(
    "hf_gate_sweep", "/opt/superbot/scripts/hf_gate_sweep.py")
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)

ap = argparse.ArgumentParser()
ap.add_argument("--days", default="2026-10-09,2026-10-10,2026-10-11",
                help="comma-separated UTC days of the fair_value_log to replay")
ap.add_argument("--brti", default="/tmp/opencode/brti_testdays.jsonl",
                help="jsonl of settlement-index ticks from fetch_brti_history.py")
ap.add_argument("--label", default="TEST", help="split name used in the report")
A = ap.parse_args()
TEST_DAYS = tuple(d for d in A.days.split(",") if d)
BRTI_PATH = A.brti
LABEL = A.label

j = sweep.load()
_DAY = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})\d{4}")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL",
           "AUG", "SEP", "OCT", "NOV", "DEC"]
_days = [f"20{m.group(1)}-{_MONTHS.index(m.group(2))+1:02d}-{m.group(3)}"
         if (m := _DAY.search(t)) else "" for t in j["ticker"].to_list()]
j = j.with_columns(pl.Series("day", _days))
test = j.filter(pl.col("day").is_in(list(TEST_DAYS)))
print(f"{LABEL} rows: {test.height}  tickers: {test['ticker'].n_unique()}")

# --- last settlement-index tick at or before each row ts, PER COIN ---------
# KXBTC15M settles on BRTI, KXETH15M on ETHUSD_RTI, etc. Mapping every row to
# BRTI makes a $0.08 DOGE spot look ~-99.9% below the ~$82k BTC index (caught
# by the basis distribution's median of -9989bps in the first run). The
# fetcher's "asset" column carries the coin name, which is also the settlement
# index's asset, so coin -> tick dict is a direct lookup.
by_asset = {}
for line in open(BRTI_PATH):
    line = line.strip()
    if not line:
        continue
    t = json.loads(line)
    if t.get("value") is None:
        continue
    by_asset.setdefault(t["asset"], []).append((int(t["time"]), float(t["value"])))
tick = {a: (np.array([x[0] for x in v], dtype=np.int64),
            np.array([x[1] for x in v], dtype=float))
        for a, v in by_asset.items()}
for a in sorted(tick):
    print(f"  {a:12s} {len(tick[a][0]):>8d} ticks")

coins = test["coin"].to_list()
ts = test["ts"].to_list()
ts_ms = np.array([int(t) if t > 1e12 else int(float(t) * 1000) for t in ts],
                 dtype=np.int64)
index_px = np.full(len(ts_ms), np.nan)
for coin in sorted(set(coins)):
    if coin not in tick:
        print(f"  WARNING: no index ticks for coin {coin} -- its rows stay NaN")
        continue
    rows = np.array([i for i, c in enumerate(coins) if c == coin], dtype=np.int64)
    tt, tv = tick[coin]
    pos = np.searchsorted(tt, ts_ms[rows], side="right") - 1
    have = pos >= 0
    index_px[rows[have]] = tv[pos[have]]

spot = test["current_price"].to_numpy().astype(float)
strike = test["strike_price"].to_numpy().astype(float)
vol = test["volatility_per_min"].to_numpy().astype(float)
mins = test["minutes_remaining"].to_numpy().astype(float)

# confirm the log's own fair value matches the documented formula at scale 1.0
d = vol * 1.0 * np.sqrt(mins)
with np.errstate(divide="ignore", invalid="ignore"):
    z_c = (np.log(spot / strike) - 0.5 * d * d) / d
fair_recomp = ndtr(z_c)
fv_log = test["fair_value_prob_up"].to_numpy().astype(float)
both = ~np.isnan(fair_recomp) & ~np.isnan(fv_log)
corr = float(np.corrcoef(fair_recomp[both], fv_log[both])[0, 1])
print(f"log fair_value_prob_up vs formula@scale=1.0 on composite: corr={corr:.6f}, "
      f"max|diff|={float(np.max(np.abs(fair_recomp[both] - fv_log[both]))):.2e}")

# index-priced fair value, same formula, index spot
with np.errstate(divide="ignore", invalid="ignore"):
    z_i = (np.log(index_px / strike) - 0.5 * d * d) / d
fair_index = ndtr(z_i)

# row-level basis: composite vs settlement index at the row timestamp
ok_basis = ~np.isnan(index_px) & (index_px > 0)
print(f"rows with index: {int(ok_basis.sum())}/{len(ts)}")
if int(ok_basis.sum()) == 0:
    raise SystemExit("no rows carry an index price -- per-coin mapping failed")
gap_bps = 10000.0 * (spot[ok_basis] - index_px[ok_basis]) / index_px[ok_basis]
print(f"rows with index: {int(ok_basis.sum())}/{len(ts)}")
print(f"composite-minus-index basis (bps): median={np.median(gap_bps):+.1f} "
      f"mean={gap_bps.mean():+.1f} p10={np.percentile(gap_bps, 10):+.1f} "
      f"p90={np.percentile(gap_bps, 90):+.1f} "
      f"share>0={float((gap_bps > 0).mean()):.1%}")
coin_arr = np.array(coins, dtype=object)
for coin in sorted(set(coins)):
    m = ok_basis & (coin_arr == coin)
    if int(m.sum()):
        g = 10000.0 * (spot[m] - index_px[m]) / index_px[m]
        print(f"  {coin:5s} n={int(m.sum()):>6d} median={np.median(g):+7.1f}bps "
              f"mean={g.mean():+7.1f}bps p10={np.percentile(g, 10):+7.1f} "
              f"p90={np.percentile(g, 90):+7.1f} "
              f"share>0={float((g > 0).mean()):.1%}")

# --- re-run the grid with both fair values --------------------------------
j_test = test.with_columns([
    pl.Series("index_px", index_px),
    pl.Series("fair_index_c", fair_index * 100.0),
])

FULL_TOP = [((85, 99), (0, 5), 0.05), ((85, 99), (0, 5), 0.03),
            ((85, 99), (0, 5), 0.02), ((85, 99), (0, 5), 0.0),
            ((93, 99), (4, 12), 0.05), ((93, 99), (6, 11), 0.05),
            ((95, 99), (6, 11), 0.01), ((93, 99), (1, 10), 0.02)]

print("\n=== top cells on test days: composite fair vs index fair (taker) ===")
print("%9s %7s %6s | %17s | %17s" %
      ("band", "win", "margin", "COMPOSITE", "INDEX"))
for band, window, margin in FULL_TOP:
    cells = {}
    for label, frame in (
            ("COMPOSITE", j_test),  # fv_c from sweep.load() is the composite fair
            ("INDEX", j_test.with_columns(pl.col("fair_index_c").alias("fv_c")))):
        m = sweep.eval_cfg(frame, band[0], band[1], window[0], window[1],
                           margin, "taker")
        cells[label] = (f"n={m['n']:>3} ev={m['ev_c']:>7.3f} "
                        f"win={100*m['win_rate']:>5.1f}" if m else "n=  0")
    print("%9s %7s %6.3f | %17s | %17s" %
          (str(list(band)), str(list(window)), margin,
           cells["COMPOSITE"], cells["INDEX"]))

# full-grid positive counts with the index fair value (taker, n>=20)
rows_i = []
for band in sweep.BANDS:
    for window in sweep.WINDOWS:
        for margin in sweep.MARGINS:
            frame = j_test.with_columns(pl.col("fair_index_c").alias("fv_c"))
            m = sweep.eval_cfg(frame, band[0], band[1], window[0],
                               window[1], margin, "taker")
            if m and m["n"] >= 20:
                rows_i.append({"band": band, "window": window,
                               "margin": margin, **m})
pos_i = [r for r in rows_i if r["ev_c"] > 0]
print(f"\nindex-priced taker configs positive (n>=20, test days): "
      f"{len(pos_i)} / {len(rows_i)}")
rows_i.sort(key=lambda r: -r["ev_c"])
print("index-priced taker top 6:")
print("%9s %7s %6s %5s %8s %7s %9s" %
      ("band", "win", "margin", "n", "ev_c", "win%", "tot$"))
for r in rows_i[:6]:
    print("%9s %7s %6.3f %5d %8.3f %7.1f %9.1f" % (
        str(list(r["band"])), str(list(r["window"])), r["margin"], r["n"],
        r["ev_c"], 100 * r["win_rate"], r["total_c"] / 100))