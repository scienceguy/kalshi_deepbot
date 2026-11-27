#!/usr/bin/env python3
"""Momentum-scalper sweep on the real 77-day Kalshi 15m tick set (naru data).

Signal: over L minutes the favourite's mid has risen by >= thr cents and the
ask sits in the band -> buy the ask, exit H minutes later at the then-current
bid (both legs pay the Kalshi taker fee). A hold that runs past the window
close holds to settlement instead. Without recorded depth this script reports
per-contract observations only; it does not simulate fills or a bankroll.

Inputs are the recorded yes_ask / no_ask per minute and the recorded outcome;
yes_bid is taken as 100 - no_ask, which is exact under a one-tick book. Nothing
is synthesised.

Usage:
  python scripts/naru_momentum.py --start 104.48
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import naru_sweep as N  # noqa: E402

def simulate(mt: pd.DataFrame, look: int, hold: int, thr: int,
             b0: int, b1: int, start: float, frac: float) -> dict:
    """One momentum config as per-contract, production-fee observations."""
    wide = mt.pivot_table(index="ticker", columns="minute",
                          values=["yes_ask_cents", "no_ask_cents"], aggfunc="last")
    if wide.empty:
        return {}
    ya = wide["yes_ask_cents"]
    na = wide["no_ask_cents"]
    yb = 100.0 - na                      # exact under a one-tick book
    mid = (ya + yb) / 2.0

    buy = ya                                   # entry pays the ask
    # exit at the bid `hold` minutes later
    bid = yb.shift(-hold, axis=1)

    mom = mid - mid.shift(look, axis=1)
    shot = mt.pivot_table(index="ticker", columns="minute",
                          values="result_yes", aggfunc="last")

    rows = []
    for m in range(0, 15 - hold):
        ok = (buy[m].between(b0, b1)) & (mom[m] >= thr) & bid[m].notna() & (bid[m] > 0)
        if not ok.any():
            continue
        rows.append(pd.DataFrame({
            "entry_min": m,
            "price": buy[m][ok] / 100.0,
            "exit": bid[m][ok] / 100.0,
            "win_settle": shot[15 - hold][ok] if (15 - hold) in shot else np.nan,
        }))
    if not rows:
        return {"n": 0}
    t = pd.concat(rows)
    t["ticker"] = t.index
    t["fee_in"] = N._entry_fees(t.ticker.to_numpy(), t.price.to_numpy())
    t["fee_out"] = [N.production_sell_cash(1.0, row.exit, N._series(row.ticker)) - row.exit
                    for row in t.itertuples()]
    t["fee"] = t.fee_in + t.fee_out
    t["net"] = [N.production_sell_cash(1.0, row.exit, N._series(row.ticker))
                - N.production_buy_cash(1.0, row.price, N._series(row.ticker))
                for row in t.itertuples()]
    t = t.sort_values("entry_min").reset_index(drop=True)
    return {"n": len(t), "net_c": t.net.mean() * 100}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=104.48)
    ap.add_argument("--frac", type=float, default=0.08)
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args()

    days = N.all_dates()
    N.all_dates = lambda: days
    mt = N.build_minute_table(attach_fv=False)
    print(f"minute table {len(mt)} rows / {mt.ticker.nunique()} tickers / {len(days)} days",
          flush=True)

    rows = []
    for look in [1, 2, 3]:
        for hold in [2, 4]:
            for thr in [2, 4, 6, 8]:
                for b0, b1 in [(75, 99), (85, 99), (95, 99), (60, 95)]:
                    r = simulate(mt, look, hold, thr, b0, b1, a.start, a.frac)
                    if not r or not r.get("n"):
                        continue
                    r["cfg"] = (look, hold, thr, b0, b1)
                    rows.append(r)

    rows.sort(key=lambda r: -r["net_c"])
    hdr = (f"{'look':>4} {'hold':>4} {'thr':>3} {'band':>7} {'n':>6} {'net_c':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:a.top]:
        look, hold, thr, b0, b1 = r["cfg"]
        print(f"{look:>4} {hold:>4} {thr:>3} {b0:>3}-{b1:<3} {r['n']:>6} "
              f"{r['net_c']:>+7.2f}")
    assert rows, "no momentum configs traded"
    pos = [r for r in rows if r["net_c"] > 0]
    print(f"\n{len(rows)} configs tested, {len(pos)} positive per-contract observations")
    print(f"best per-contract net {rows[0]['net_c']:+.3f}c")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())