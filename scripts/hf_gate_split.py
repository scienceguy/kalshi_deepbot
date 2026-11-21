"""Train/test split of the GBM-gate sweep: is the positive EV out-of-sample?

Reuses scripts/hf_gate_sweep.py's own load()/eval_cfg() (no new logic) and
re-runs the config grid on the training days (fair_value_log 2026-09-29..
10-08, the days the fair-value log was produced on) versus the held-out test
days (10-09..10-11). A config that is positive only on train days is an
in-sample artifact; one that survives on test days with workable n is the
first honest candidate edge.
"""
import importlib.util

spec = importlib.util.spec_from_file_location(
    "hf_gate_sweep", "/opt/superbot/scripts/hf_gate_sweep.py")
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)

j = sweep.load()
import re
import polars as pl

_DAY = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})\d{4}")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL",
           "AUG", "SEP", "OCT", "NOV", "DEC"]
_days = []
for t in j["ticker"].to_list():
    m = _DAY.search(t)
    _days.append(f"20{m.group(1)}-{_MONTHS.index(m.group(2)) + 1:02d}-{m.group(3)}"
                 if m else "")
j = j.with_columns(pl.Series("day", _days))
print(f"joined rows: {j.height}  tickers: {j['ticker'].n_unique()}")

SPLIT_DAY = "2026-10-09"  # first held-out test day (per train_ml_hf.py split)
train = j.filter(__import__("polars").col("day") < SPLIT_DAY)
test = j.filter(__import__("polars").col("day") >= SPLIT_DAY)
days_tr = sorted(train["day"].unique().to_list())
days_te = sorted(test["day"].unique().to_list())
print(f"train days: {days_tr[0]}..{days_tr[-1]} ({len(days_tr)}d, {train.height} rows)")
print(f"test  days: {days_te[0]}..{days_te[-1]} ({len(days_te)}d, {test.height} rows)")


def grid(frame, label):
    rows = []
    for band in sweep.BANDS:
        for window in sweep.WINDOWS:
            for margin in sweep.MARGINS:
                for mode in ("taker", "maker"):
                    m = sweep.eval_cfg(frame, band[0], band[1],
                                       window[0], window[1], margin, mode)
                    if m and m["n"] > 0:
                        rows.append({"band": band, "window": window,
                                     "margin": margin, "mode": mode, **m})
    t = [r for r in rows if r["mode"] == "taker" and r["n"] >= 20]
    mk = [r for r in rows if r["mode"] == "maker" and r["n"] >= 20]
    print(f"\n=== {label}: positive-EV configs (n>=20) ===")
    print(f"taker: {sum(1 for r in t if r['ev_c'] > 0)} / {len(t)}   "
          f"maker: {sum(1 for r in mk if r['ev_c'] > 0)} / {len(mk)}")
    for mode, sub in (("taker", t), ("maker", mk)):
        sub.sort(key=lambda r: -r["ev_c"])
        print(f"--- {label} {mode} top 8 ---")
        print("%9s %7s %6s %5s %8s %7s %9s" %
              ("band", "win", "margin", "n", "ev_c", "win%", "tot$"))
        for r in sub[:8]:
            print("%9s %7s %6.3f %5d %8.3f %7.1f %9.1f" % (
                str(list(r["band"])), str(list(r["window"])), r["margin"],
                r["n"], r["ev_c"], 100 * r["win_rate"], r["total_c"] / 100))
    return rows


rows_tr = grid(train, "TRAIN")
rows_te = grid(test, "TEST")

# Cross-check: the full-sample top cells, re-scored on each split
print("\n=== full-sample top cells by split ===")
FULL_TOP = [((85, 99), (0, 5), 0.05), ((85, 99), (0, 5), 0.03),
            ((85, 99), (0, 5), 0.02), ((85, 99), (0, 5), 0.0),
            ((93, 99), (1, 10), 0.02), ((95, 99), (6, 11), 0.01),
            ((93, 99), (6, 11), 0.02)]
print("%9s %7s %6s | %18s | %18s" %
      ("band", "win", "margin", "TRAIN n/ev/win%", "TEST n/ev/win%"))
for band, window, margin in FULL_TOP:
    cells = {}
    for label, frame in (("TRAIN", train), ("TEST", test)):
        m = sweep.eval_cfg(frame, band[0], band[1], window[0], window[1],
                           margin, "taker")
        cells[label] = (f"n={m['n']:>4} ev={m['ev_c']:>7.3f} "
                        f"win={100*m['win_rate']:>5.1f}" if m else "n=0")
    print("%9s %7s %6.3f | %18s | %18s" %
          (str(list(band)), str(list(window)), margin,
           cells["TRAIN"], cells["TEST"]))