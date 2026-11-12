#!/usr/bin/env python3
"""Project the dry-run balance if the live strategy compounded over history.

Reads the HF 15m set, walks each settled window in chronological order, and
takes the first in-band favourite (default 95-99c) inside the entry window
(6-11 min) that clears the model gate. Sizing is PAPER_PER_TRADE_PCT of the
current balance; the entry pays the ask and the taker fee — the same reality
tax the dry run charges (trader.py:1923). Output is the compounded final
balance, so a "$10k dry balance" claim can be checked against real quotes
instead of asserted.

Not the bot's own paper ledger (that is paper_state.json); this is the
projection you compare it to.

Usage:
  python scripts/dry_compound.py --days 25 --balance 100
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config  # noqa: E402
import fees  # noqa: E402
from binance_feed import prob_above  # noqa: E402
import train_ml_hf as T  # noqa: E402


def run(df, balance, pct, lo, hi, w_start, w_end, min_edge, gate=True):
    start = balance
    n = wins = days = 0
    max_balance = balance
    for day in sorted(df.day.unique()):
        days += 1
        sub = df[df.day == day]
        for ticker, g in sub.groupby("ticker", sort=False):
            g = g.sort_values("minutes_remaining", ascending=False)
            for r in g.itertuples():
                minutes_in = 15.0 - r.minutes_remaining
                if minutes_in < w_start or minutes_in > w_end:
                    continue
                yb = int(round(r.kalshi_yes_bid * 100))
                ya = int(round(r.kalshi_yes_ask * 100))
                fav = T._favourite(yb, ya, lo, hi)
                if fav is None:
                    continue
                side, entry_c = fav
                if gate:
                    p_yes = prob_above(r.current_price, r.strike_price,
                                       r.volatility_per_min, r.minutes_remaining)
                    if p_yes is None or p_yes != p_yes:  # None or NaN
                        continue
                    trade_p = p_yes if side == "YES" else 1.0 - p_yes
                    fee1 = fees.taker_fee_cents(1.0, entry_c / 100.0)
                    if trade_p * 100.0 - entry_c - fee1 < min_edge:
                        continue
                stake = balance * pct
                contracts = stake / (entry_c / 100.0)
                if contracts <= 0:
                    continue
                cost = fees.buy_cash_out(contracts, entry_c / 100.0, taker=True)
                won = (r.yes_win == 1.0) if side == "YES" else (r.yes_win == 0.0)
                balance += (contracts if won else 0.0) - cost
                if balance <= 0:
                    return start, 0.0, n, wins, days, 0.0
                n += 1
                wins += 1 if won else 0
                max_balance = max(max_balance, balance)
                break  # one entry per market
    return start, balance, n, wins, days, max_balance


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=25)
    ap.add_argument("--balance", type=float, default=100.0)
    ap.add_argument("--pct", type=float, default=config.PAPER_PER_TRADE_PCT)
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--target", type=float, default=10000.0)
    a = ap.parse_args(argv)

    fv, res = T.load_frames(a.days)
    df = T.prepare(fv, res)
    # prepare() may not carry ts ordering; days are file stems (sorted = dated).
    print(f"data: {len(df)} rows, {df.ticker.nunique()} windows, "
          f"{df.day.nunique()} days ({min(df.day)}..{max(df.day)})")

    args = (a.balance, a.pct, config.ENTRY_BAND_MIN, config.ENTRY_BAND_MAX,
            config.ENTRY_WINDOW_START_MIN, config.ENTRY_WINDOW_END_MIN,
            config.MODEL_MIN_EDGE_CENTS, not a.no_gate)
    start, end, n, wins, days, peak = run(df, *args)
    wr = 100.0 * wins / n if n else 0.0
    per_day = n / days if days else 0
    print(f"\nband {config.ENTRY_BAND_MIN}-{config.ENTRY_BAND_MAX}c, "
          f"minutes {config.ENTRY_WINDOW_START_MIN}-{config.ENTRY_WINDOW_END_MIN}, "
          f"taker at ask + fee, gate={not a.no_gate}:")
    print(f"  start ${start:.2f} -> end ${end:.2f}  ({end / start:.2f}x)  "
          f"peak ${peak:.2f}")
    print(f"  trades {n} ({wr:.1f}% win), {per_day:.1f}/day over {days} days")
    if end > start and n:
        per_trade = (end / start) ** (1.0 / n)
        print(f"  compound/trade {(per_trade - 1) * 100:+.3f}%")
        if per_trade > 1.0:
            need = __import__("math").log(a.target / start) / __import__("math").log(per_trade)
            print(f"  to reach ${a.target:,.0f} from ${start:.2f}: "
                  f"~{need:,.0f} more trades = ~{need / per_day:,.0f} more days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
