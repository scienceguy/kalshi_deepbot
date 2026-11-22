"""Locally simulated paper trades against recorded live quotes, PnL tracked.

The dry-run lane: replay an entry rule over the recorded crossvenue rows
(both venues' top-of-book bid/ask, the model fair value, the strike and
minutes_left), simulate fills, settle against Kalshi's own API result, and
track realized and unrealized PnL with the reality tax: the venue fee from
fees.py (never re-derived here), the per-trade cash cap, and a drawdown
halt.

Taker: fill at the recorded ask. Maker: rest at fair - MAKER_OFFSET and fill
when a later row in the same window shows the ask at or below the resting
bid (the conservative cross-the-spread proxy scripts/hf_gate_sweep.py also
uses). No fill means no trade -- the missed fill is an honest opportunity
cost, not a PnL.

Settlement comes from the Kalshi public API (result + close_time, never from
a parsed ticker label). A window with no result yet is marked unrealized at
the last recorded mid and never touches the realized PnL or the balance.
"""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

# scripts/ lands on sys.path when run directly; fees.py lives at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fees

KALSHI_MARKETS_URL = "https://external-api.kalshi.com/trade-api/v2/markets"

# A rung quoting a spread wider than this is not a price (crossvenue.MAX_SPREAD).
MAX_SPREAD = 0.08


def minutes_elapsed(minutes_left: float) -> int:
    """Minutes into the 15M window; matches hf_gate_sweep.load()'s minute."""
    return int(round(15.0 - float(minutes_left)))


def row_ts(row: dict) -> float:
    return datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).timestamp()


def quote(row: dict, venue: str) -> tuple:
    """(bid, ask, mid) for the venue, or None when the book had no price."""
    bid, ask, mid = row.get(f"{venue}_bid"), row.get(f"{venue}_ask"), row.get(venue)
    if bid is None or ask is None or mid is None:
        return None
    return float(bid), float(ask), float(mid)


def entry_row(rows: list, cfg) -> dict:
    """First row per window where the rule fires (as eval_cfg does: .first())."""
    for row in sorted(rows, key=row_ts):
        q = quote(row, cfg.venue)
        if q is None:
            continue
        bid, ask, mid = q
        if ask - bid > MAX_SPREAD:  # reviewed: wider than MAX_SPREAD is an empty book, not a fillable price (B6)
            continue
        if not (cfg.min_m <= minutes_elapsed(row["minutes_left"]) <= cfg.max_m):
            continue
        if not (cfg.band_lo <= ask * 100.0 <= cfg.band_hi):
            continue
        if (float(row["fair"]) - ask) * 100.0 < cfg.gate:
            continue
        return row
    return None


def maker_fill(rows: list, entry: dict, resting: float, cfg) -> dict:
    """First later row whose ask trades through the resting bid, else None."""
    for row in sorted(rows, key=row_ts):
        if row_ts(row) <= row_ts(entry):
            continue
        q = quote(row, cfg.venue)
        if q is None:
            continue
        if q[1] <= resting + 1e-12:  # reviewed: fill only when the ask comes to our bid -- adverse selection is the maker's tax (B6)
            return row
    return None


def simulate(rows: list, cfg, settle) -> dict:
    """Replay the rule; settle(event) -> {"result": "yes"/"no"/None}."""
    trades, dropped, halted = [], 0, False
    balance, peak, max_dd = cfg.balance, cfg.balance, 0.0
    windows = {}
    for row in rows:
        # reviewed: only 15M up/down windows belong in this ledger -- the collector's daily ladder rows (KXBTCD/KXETHD, strikes spanning hundreds of dollars, venue mids diverging by up to 30c) are a different market family and must never book a paper trade as if they were the strategy under test (B6)
        if "15M" not in row["event"]:
            continue
        windows.setdefault(row["event"], []).append(row)

    for event, wrows in sorted(windows.items()):
        entry = entry_row(wrows, cfg)
        if entry is None:
            continue
        if halted:
            dropped += 1  # reviewed: a halted book opens nothing -- that is what a drawdown halt means (B6)
            continue
        q = quote(entry, cfg.venue)
        if cfg.mode == "taker":
            price = q[1]
        else:
            price = round(float(entry["fair"]) - cfg.maker_offset, 2)
            filled_at = maker_fill(wrows, entry, price, cfg)
            if filled_at is None:
                continue  # no fill, no trade
        if price <= 0:
            continue
        contracts = int(math.floor(cfg.pct * balance / price))
        if contracts < 1:  # reviewed: unaffordable paper trades are dropped, matching the trader's own paper drop (B6)
            dropped += 1
            continue
        series = event.split("-")[0]
        fee = (fees.taker_fee_dollars(contracts, price, series=series)
               if cfg.mode == "taker"
               else fees.maker_fee_dollars(contracts, price, series=series))
        cash = fees.buy_cash_out(contracts, price, taker=(cfg.mode == "taker"))
        out = settle(event)
        result = out.get("result") if isinstance(out, dict) else out
        settled = result in ("yes", "no")
        win = result == "yes"
        realized = (contracts * (1.0 if win else 0.0) - cash) if settled else 0.0
        last_mid = quote(sorted(wrows, key=row_ts)[-1], cfg.venue)[2]
        unrealized = (contracts * last_mid - cash) if not settled else 0.0
        balance += realized
        peak = max(peak, balance)
        max_dd = max(max_dd, (peak - balance) / peak if peak > 0 else 0.0)
        if max_dd >= cfg.dd_halt:
            halted = True
        trades.append({
            "event": event, "ts": entry["ts"], "mode": cfg.mode,
            "venue": cfg.venue, "minutes_left": entry["minutes_left"],
            "price": price, "contracts": contracts, "fee": fee, "cash": cash,
            "strike": entry.get("strike"), "result": result, "settled": settled,
            "realized": round(realized, 4), "unrealized": round(unrealized, 4),
            "balance_after": round(balance, 4), "halted_after": halted,
        })

    settled_trades = [t for t in trades if t["settled"]]
    wins = [t for t in settled_trades if t["result"] == "yes"]
    entry_ts = [row_ts({"ts": t["ts"]}) for t in trades]
    days = max((max(entry_ts) - min(entry_ts)) / 86400.0, 1e-9) if len(entry_ts) > 1 else 0.0
    daily = ((balance / cfg.balance) ** (1.0 / days) - 1.0) if days > 0 and balance > 0 else 0.0
    return {
        "trades": trades,
        "summary": {
            "rule": {"band": [cfg.band_lo, cfg.band_hi], "minutes": [cfg.min_m, cfg.max_m],
                     "gate": cfg.gate, "mode": cfg.mode, "venue": cfg.venue,
                     "pct": cfg.pct, "dd_halt": cfg.dd_halt},
            "entries": len(trades) + dropped, "trades": len(trades),
            "dropped_unaffordable_or_halted": dropped,
            "settled": len(settled_trades), "wins": len(wins),
            "win_rate": (len(wins) / len(settled_trades)) if settled_trades else None,
            "realized_pnl": round(sum(t["realized"] for t in trades), 4),
            "unrealized_pnl": round(sum(t["unrealized"] for t in trades), 4),
            "balance": round(balance, 4),
            "max_drawdown": round(max_dd, 4), "halted": halted,
            "days_observed": round(days, 4),
            "daily_rate": round(daily, 6),
        },
    }


def settle_api(event: str) -> dict:
    """Kalshi's own record for a window: result + close_time. Never a label parse."""
    import requests

    r = requests.get(KALSHI_MARKETS_URL,
                     params={"event_ticker": event, "limit": 5}, timeout=20)
    ms = r.json().get("markets", [])
    if not ms:
        return {"result": None, "close_time": None, "strike": None}
    return {"result": ms[0].get("result"), "close_time": ms[0].get("close_time"),
            "strike": float(ms[0]["floor_strike"]) if ms[0].get("floor_strike") else None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True, help="crossvenue jsonl to replay")
    ap.add_argument("--mode", choices=("taker", "maker"), default="taker")
    ap.add_argument("--venue", choices=("kalshi", "poly"), default="kalshi")
    ap.add_argument("--balance", type=float, default=10.34)
    ap.add_argument("--pct", type=float, default=0.08, help="per-trade cap on balance")
    ap.add_argument("--band-lo", type=float, default=85.0)
    ap.add_argument("--band-hi", type=float, default=99.0)
    ap.add_argument("--min-m", type=int, default=3, help="earliest minute into the window")
    ap.add_argument("--max-m", type=int, default=5, help="latest minute into the window")
    ap.add_argument("--gate", type=float, default=3.0, help="fair-minus-ask edge in cents")
    ap.add_argument("--maker-offset", type=float, default=0.01, help="maker rest below fair")
    ap.add_argument("--dd-halt", type=float, default=0.25, help="halt trading at this drawdown")
    ap.add_argument("--settle", choices=("api", "none"), default="api",
                    help="settle against the Kalshi API or leave every window unrealized")
    ap.add_argument("--out", default="/tmp/opencode/paper_trades.jsonl")
    A = ap.parse_args()

    cfg = argparse.Namespace(**{k: getattr(A, k) for k in
                                ("mode", "venue", "balance", "pct", "band_lo",
                                 "band_hi", "min_m", "max_m", "gate",
                                 "maker_offset", "dd_halt")})
    rows = [json.loads(l) for l in open(A.file) if l.strip()]
    settle = settle_api if A.settle == "api" else (lambda ev: {"result": None})
    out = simulate(rows, cfg, settle)

    with open(A.out, "w") as fh:
        for t in out["trades"]:
            fh.write(json.dumps(t) + "\n")
    s = out["summary"]
    print(f"rule: {s['rule']}")
    print(f"entries={s['entries']} trades={s['trades']} settled={s['settled']} "
          f"wins={s['wins']} win_rate={s['win_rate']} dropped={s['dropped_unaffordable_or_halted']}")
    print(f"realized={s['realized_pnl']} unrealized={s['unrealized_pnl']} "
          f"balance={s['balance']} (start {cfg.balance})")
    print(f"max_drawdown={s['max_drawdown']:.1%} halted={s['halted']} "
          f"days={s['days_observed']:.3f} daily_rate={s['daily_rate']:+.2%}")
    print(f"ledger: {A.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())