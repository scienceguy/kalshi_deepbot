#!/usr/bin/env python3
"""Dry run of the validated favourite-taker edge, starting from the real paper balance.

This is the goal artifact: replay the recorded Kalshi 15m ticks day by day,
compound a bankroll through taker fills at the venue ask, charge the Kalshi
taker fee on every fill, and report the moment the balance crosses $10,000.

Nothing is synthesised. Quotes are the recorded book, settlement is the
recorded outcome (last yes_ask >= 50, validated 203/203 elsewhere), and the
entry filter is the BRTI fair-value gate from naru_sweep (spot vs strike priced
from trailing realised 1s volatility).

Reality tax is itemised: fees paid, fills, and the honest taker price.

Usage:
  python scripts/dryrun_10k.py --start 104.48 --frac 0.70 --target 10000
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT / "scripts"))

import naru_sweep as N  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, default=104.48)
    ap.add_argument("--frac", type=float, default=0.70)
    ap.add_argument("--target", type=float, default=10000.0)
    ap.add_argument("--band", default="95,99")
    ap.add_argument("--window", default="0,15")
    ap.add_argument("--edge", default="0.0",
                    help="fair-value edge in cents, or 'none' for a band-only run")
    ap.add_argument("--ledger", default=str(Path.home() / "tmp/opencode/dryrun_ledger.jsonl"))
    ap.add_argument("--slip", type=float, default=0.0,
                    help="cents added to the recorded ask (adverse fill)")
    ap.add_argument("--fill", type=float, default=1.0,
                    help="share of candidates actually filled, deterministic per ticker")
    a = ap.parse_args()

    b0, b1 = (int(x) for x in a.band.split(","))
    w0, w1 = (int(x) for x in a.window.split(","))

    days = N.usable_dates() if a.edge != "none" else N.all_dates()
    N.all_dates = lambda: days
    mt = N.build_minute_table(attach_fv=a.edge != "none")
    t = N.trades(mt, b0, b1, w0, w1, ("yes",),
                 edge=(None if a.edge == "none" else float(a.edge)))
    t = t.sort_values("close_s").reset_index(drop=True)
    assert len(t) > 0, "no trades passed the filter"

    bal = a.start
    peak = bal
    maxdd = 0.0
    fees = 0.0
    filled = 0
    wiped = False
    cross = None
    ledger = []

    for row in t.itertuples():
        # Deterministic partial-fill: a stable hash of the ticker decides, so a
        # rerun selects the same markets instead of resampling.
        if a.fill < 1.0 and (hash(str(row.ticker)) % 1000) / 1000.0 >= a.fill:
            continue
        price = min(0.99, float(row.price) + a.slip / 100.0)
        stake = bal * a.frac
        contracts = stake / price
        if contracts <= 0:
            continue
        cost = contracts * price
        fee = contracts * 0.07 * price * (1.0 - price)  # Kalshi taker fee on the paid price
        if cost + fee > bal:
            continue
        bal -= cost + fee
        fees += fee
        filled += 1
        if bool(row.yes_win):
            bal += contracts * 1.0
        peak = max(peak, bal)
        dd = 1.0 - bal / peak
        maxdd = max(maxdd, dd)
        if bal < 1.0 and not wiped:
            wiped = True
            break
        if cross is None and bal >= a.target:
            cross = row.close_s
        ledger.append({"ts": int(row.close_s), "ticker": row.ticker,
                       "price": round(price, 4),
                       "contracts": round(contracts, 2),
                       "fee": round(fee, 4), "win": bool(row.yes_win),
                       "balance": round(bal, 2)})

    out = Path(a.ledger)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(x) for x in ledger) + "\n")

    import datetime as _dt

    def day(ts):
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")

    print(f"dry run: band {b0}-{b1} window {w0}-{w1} yes edge {a.edge} frac {a.frac}")
    print(f"  start ${a.start:.2f} -> final ${bal:.2f}  ({bal / a.start:.2f}x)")
    print(f"  fills {filled} / candidates {len(t)}  win {t.yes_win.mean() * 100:.2f}%")
    print(f"  reality tax: ${fees:.2f} fees total (${fees / max(filled, 1):.4f}/fill)")
    print(f"  max drawdown {maxdd * 100:.1f}%  wiped={wiped}")
    print(f"  ledger {out} ({len(ledger)} rows)")
    if cross:
        first = day(t.close_s.iloc[0])
        print(f"  TARGET ${a.target:,.0f} CROSSED on {day(cross)} (from {first})")
    else:
        print(f"  TARGET ${a.target:,.0f} NOT reached (final ${bal:.2f})")
    return 0 if cross else 1


if __name__ == "__main__":
    raise SystemExit(main())