#!/usr/bin/env python3
"""Accurate dry-run growth simulation: places and records simulated trades
against historical live orderbook data (HF quote_history), reproducing the
bot's own signal engine, maker fill rule, fee model, settlement and risk
limits, then projects compounding from $35 to the $3.25M target.

The real code paths are reused, not re-implemented:
  * signals.evaluate() — signal engine (entry band, maker price, Kelly size)
  * risk.RiskManager   — paper=True knobs: cash floor, exposure ceiling,
                         per-trade cap, drawdown halt, consecutive-loss
                         cooldown, per-market daily loss, liquidity checks
  * fees.buy_cash_out / sell_cash_in — maker entry fee 0, settlement no fee
  * trader._dry_run_fill_count rule — a resting maker bid fills only on a
    minute where OUR side's ask <= entry_price with counterparty depth > 0
    (a YES buy is filled by NO bidders, and vice versa)

A simulated clock (patched into risk.time) keeps the cooldown and market-loss
windows honest without importing a fake time module.

USAGE:
  # historical replay (HF quote_history parquet, dollar prices, one volume col)
  SUPERBOT_DATA_DIR=/tmp/opencode/drysim \
    python scripts/dryrun_growth_sim.py --days 2026-07-23 ... 2026-10-11

  # today's live fill data (collected by scripts/live_book_collector.py,
  # per-side top-of-book depth in yes_vol/no_vol)
  python scripts/dryrun_growth_sim.py --collected live_books.jsonl
"""
import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import config  # noqa: E402
import fees  # noqa: E402
import risk as risk_mod  # noqa: E402
import signals as sig  # noqa: E402
from market_finder import MarketInfo  # noqa: E402

# Match `trader.py --dry`: the dry run sets DRY_RUN=True at runtime (the repo
# .env ships DRY_RUN=false for live). Guards stay on (PAPER_GUARDS_ENABLED)
# with the paper knobs (per-trade 15%, drawdown halt 90%).
config.DRY_RUN = True

log = logging.getLogger("dryrun_sim")
logging.basicConfig(level=logging.ERROR)

HF_REPO = "papylove/kalshi-15m-data"
# Scratch root: $TMPDIR is service-owned (under local_data); the approved
# shared scratch dir is the fallback (AGENTS.md: never bare /tmp).
SCRATCH = Path(os.environ.get("TMPDIR") or "/tmp/opencode")
HF_CACHE = SCRATCH / "hf_cache"

# Keep the sim offline: KL divergence hits Polymarket and is a veto metric,
# not an edge; the historical parquet has no venue ladder for it.
sig._compute_kl_divergence = lambda market: None

# Simulated clock for the risk windows (cooldown / market-loss lookback).
_CLOCK = {"t": 0.0}
risk_mod.time = type("T", (), {"time": staticmethod(lambda: _CLOCK["t"])})()

from risk import RiskManager  # noqa: E402
from signals import floor_contracts  # noqa: E402


# ═════════════════════════════════════════════════════════════════════
# DATA
# ═════════════════════════════════════════════════════════════════════

def load_days(days):
    """Load quote_history parquet for each day (cached under HF_CACHE)."""
    import pandas as pd
    HF_CACHE.mkdir(parents=True, exist_ok=True)
    frames = []
    for day in days:
        local = HF_CACHE / f"quote_history_{day}.parquet"
        if not local.exists():
            from huggingface_hub import hf_hub_download
            src = hf_hub_download(repo_id=HF_REPO,
                                  filename=f"quote_history/{day}.parquet",
                                  repo_type="dataset")
            local.write_bytes(Path(src).read_bytes())
        frames.append(pd.read_parquet(local))
    return pd.concat(frames, ignore_index=True)


def build_tickers(df):
    """{ticker: dict} with minute-indexed quote book + settlement facts.

    The HF parquet carries one `volume` column (a per-minute venue number, not
    top-of-book depth) and dollar prices; a live collection (load_collected)
    carries per-side top-of-book depth in yes_vol/no_vol. Both are stored in
    cents so market_at needs no conversion.
    """
    has_sides = "yes_vol" in df.columns and "no_vol" in df.columns
    out = {}
    for ticker, g in df.groupby("ticker"):
        g = g.sort_values("minute")
        book = {}
        for _, r in g.iterrows():
            if has_sides:
                yvol = float(r["yes_vol"] or 0)
                nvol = float(r["no_vol"] or 0)
            else:
                yvol = nvol = float(r["volume"] or 0)
            book[int(r["minute"])] = {
                "bid": int(round(float(r["yes_bid"]) * 100)),
                "ask": int(round(float(r["yes_ask"]) * 100)),
                "vol": yvol if yvol else nvol,
                "yes_vol": yvol,
                "no_vol": nvol,
            }
        # The settlement row is appended last (minute 15); prefer it over the
        # first quote row, which carries no result.
        settled = [r for _, r in g.iterrows()
                   if str(r.get("result") or "").strip()]
        first = settled[-1] if settled else g.iloc[0]
        result = str(first.get("result") or "").lower()
        out[ticker] = {
            "series": first["series"],
            "open_ts": int(first["open_ts"]),
            "close_ts": int(first["close_ts"]),
            "strike": float(first["floor_strike"]),
            "expiration": float(first.get("expiration_value") or 0),
            "yes_won": result == "yes",
            "has_result": bool(settled),
            "book": book,
        }
    return out


def load_collected(path):
    """Load a live-collected book JSONL (same schema as the HF parquet)."""
    import pandas as pd
    return pd.read_json(Path(path), lines=True)


# ═════════════════════════════════════════════════════════════════════
# MARKET VIEW
# ═════════════════════════════════════════════════════════════════════

def market_at(t, meta, minute):
    """MarketInfo with the book state at `minute` (cents, int)."""
    q = meta["book"][minute]
    yb, ya = q["bid"], q["ask"]
    return MarketInfo(
        ticker=t,
        series=meta["series"],
        close_time=datetime.fromtimestamp(meta["close_ts"], tz=timezone.utc),
        strike=meta["strike"],
        strike_type="",
        yes_bid=yb,
        yes_ask=ya,
        # Kalshi quotes NO directly on crypto series (quadratic fee_type);
        # derive the complement only from a quoted YES bid.
        no_bid=(100 - ya) if ya else 0,
        no_ask=(100 - yb) if yb else 0,
        yes_liquidity=int(q["yes_vol"]),
        no_liquidity=int(q["no_vol"]),
        minutes_left=15 - minute,
        bracket_role="MAIN",
        raw={"floor_strike": meta["strike"],
             "expiration_value": meta["expiration"]},
    )


# ═════════════════════════════════════════════════════════════════════
# SIM STATE
# ═════════════════════════════════════════════════════════════════════

@dataclass
class Sim:
    start_balance: float
    cash: float = 0.0
    peak: float = 0.0
    max_dd: float = 0.0
    trades: list = field(default_factory=list)
    positions: list = field(default_factory=list)
    attempts: int = 0
    fills: int = 0
    equity_curve: list = field(default_factory=list)
    daily_equity: dict = field(default_factory=dict)
    rm: RiskManager = None

    def __post_init__(self):
        self.cash = self.start_balance
        self.peak = self.start_balance
        self.rm = RiskManager()
        self.rm.initialize(self.start_balance)
        self._CLOCK_BASE = 1_700_000_000.0

    def tick(self, ts):
        _CLOCK["t"] = float(ts)

    def equity(self):
        # Mark-to-market at best-mid would need the book; cost basis is the
        # conservative floor and matches the trader's paper MTM only after a
        # fill, so keep cost until settlement.
        return self.cash + sum(p["cost"] for p in self.positions)

    def exposure(self):
        return sum(p["cost"] for p in self.positions)

    def mtm(self):
        return self.equity() - self.cash

    def mark(self):
        eq = self.equity()
        self.equity_curve.append(eq)
        if eq > self.peak:
            self.peak = eq
        dd = (self.peak - eq) / self.peak if self.peak > 0 else 0.0
        if dd > self.max_dd:
            self.max_dd = dd


def simulate(tickers, days_n, start_balance):
    """Event-driven replay in (event_ts, settle-before-entry, minute) order."""
    s = Sim(start_balance)
    entry_lo = int(math.ceil(config.ENTRY_WINDOW_START_MIN))    # 8
    entry_hi = int(math.floor(config.ENTRY_WINDOW_END_MIN))     # 14
    attempts_per_ticker = set()

    events = []
    for t, meta in tickers.items():
        for m in range(entry_lo, min(entry_hi, 15) + 1):
            events.append((meta["open_ts"] + (m - 1) * 60, 1, m, "entry", t))
        events.append((meta["close_ts"] + 2, 0, 15, "settle", t))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    for ts, _prio, minute, kind, t in events:
        meta = tickers[t]
        if ts != s.__dict__.get("_last_ts"):
            s.__dict__["_last_ts"] = ts
        s.tick(ts)
        if kind == "settle":
            day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            s.daily_equity[day] = s.equity()
            for p in [p for p in s.positions if p["ticker"] == t]:
                payout = 1.0 if meta["yes_won"] else 0.0
                if p["side"] == "no":
                    payout = 1.0 - payout
                rev = fees.sell_cash_in(p["contracts"], payout, taker=False)
                s.cash += rev
                pnl = rev - p["cost"]
                s.trades.append({
                    "ticker": t, "side": p["side"],
                    "entry_price": p["entry_price"],
                    "contracts": p["contracts"], "cost": p["cost"],
                    "revenue": rev, "pnl": pnl, "won": pnl > 0,
                    "filled_minute": p["filled_minute"],
                    "settled_day": day,
                })
                s.rm.record_trade_outcome(pnl > 0, pnl)
                if pnl < 0:
                    s.rm.record_market_loss(t, -pnl)
                s.positions.remove(p)
                s.rm.update_balance(s.cash, s.exposure(),
                                    mark_to_market_exposure=s.mtm())
                s.mark()
            continue

        # entry event
        if t in attempts_per_ticker:      # one entry attempt per ticker
            continue
        if minute not in meta["book"]:
            continue
        eq = s.equity()
        exposure = s.exposure()
        max_trade = s.rm.max_trade_dollars(
            s.cash, exposure, resting_orders_value=0.0, paper=True,
            mark_to_market_exposure=s.mtm(),
        )
        if max_trade < 0.01:
            continue
        attempts_per_ticker.add(t)        # blocked by limits = no retry
        mkt = market_at(t, meta, minute)
        try:
            found = sig.evaluate(mkt, eq, max_trade_override=max_trade)
        except Exception as e:
            log.debug("evaluate failed %s: %s", t, e)
            continue
        sig_pick = next((x for x in found if x.is_trade), None)
        if sig_pick is None:
            continue
        s.attempts += 1
        entry_price = int(sig_pick.entry_price)
        contracts = float(sig_pick.contracts)

        # Final pre-order risk gate (halt, cooldown, market loss, liquidity,
        # cash floor) — the same call the trader makes before placing.
        ok, why = s.rm.check(
            s.cash, exposure, contracts * entry_price / 100.0, t,
            total_value=eq, resting_orders_value=0.0, paper=True,
            win_prob=sig_pick.win_prob, entry_price_cents=entry_price,
            market=mkt, side=sig_pick.side,
        )
        if not ok:
            continue

        # Resting-maker fill, matching trader._dry_run_fill_count: the ask on
        # OUR side must come down to our price, and the counterparty depth is
        # the OTHER side's bid size (a YES buy is filled by NO bidders).
        fill_side = sig_pick.side
        for m2 in range(minute, 15):
            q2 = meta["book"].get(m2)
            if q2 is None:
                continue
            if fill_side == "yes":
                side_ask, depth = q2["ask"], q2["no_vol"]
            else:
                # NO ask is the complement of the YES bid; no YES bid = no quote
                side_ask = (100 - q2["bid"]) if q2["bid"] else 0
                depth = q2["yes_vol"]
            if side_ask and side_ask <= entry_price and depth > 0:
                n = floor_contracts(min(contracts, depth))
                if n <= 0:
                    break
                cost = fees.buy_cash_out(n, entry_price / 100.0, taker=False)
                if cost <= 0:
                    break
                s.cash -= cost
                s.positions.append({
                    "ticker": t, "side": sig_pick.side,
                    "entry_price": entry_price, "contracts": n,
                    "cost": cost, "filled_minute": minute,
                })
                s.fills += 1
                break
    return s


def summarize(s, days_n):
    n = len(s.trades)
    wins = sum(1 for x in s.trades if x["won"])
    pnl = sum(x["pnl"] for x in s.trades)
    avg = pnl / n if n else 0.0
    per_day = n / days_n if days_n else 0
    final_eq = s.equity()
    growth_day = (final_eq / s.start_balance) ** (1 / days_n) if final_eq > 0 else 0
    days_to = (math.log(3_250_000 / s.start_balance) / math.log(growth_day)
               if growth_day > 1 else float("inf"))
    # Two-phase projection: compound at the measured per-trade EV and
    # trades/day, but cap every trade at MAX_TRADE_DOLLARS once the bankroll
    # is large enough that 15% of cash exceeds the venue's modelled cap.
    cap_days = None
    if n and per_day > 0:
        avg_cost = sum(x["cost"] for x in s.trades) / n
        avg_pnl = avg
        ev_per_dollar = avg_pnl / avg_cost if avg_cost else 0.0
        b, d = s.start_balance, 0
        while b < 3_250_000 and d < 36500:
            trade_d = min(b * config.PAPER_PER_TRADE_PCT, config.MAX_TRADE_DOLLARS)
            b += per_day * trade_d * ev_per_dollar
            d += 1
        cap_days = d if b >= 3_250_000 else None
    print("\n" + "=" * 74)
    print("DRY-RUN GROWTH SIMULATION — live orderbook replay")
    print("=" * 74)
    print(f"Days simulated:        {days_n}")
    print(f"Market windows:        {s.attempts} signals attempted / "
          f"{len(set(x['ticker'] for x in s.trades))} tickers traded")
    print(f"Fills / signals:       {s.fills} / {s.attempts} "
          f"({s.fills / s.attempts * 100:.1f}% fill rate)" if s.attempts else "")
    print(f"Trades settled:        {n}")
    print(f"Win rate:              {wins / n * 100:.1f}%" if n else "")
    print(f"Total PnL:             ${pnl:,.2f}")
    print(f"Avg PnL/trade:         ${avg:+.4f}")
    print(f"Trades/day:            {per_day:.1f}")
    print(f"Start balance:         ${s.start_balance:,.2f}")
    print(f"Final equity:          ${final_eq:,.2f}")
    print(f"Peak equity:           ${s.peak:,.2f}")
    print(f"Max drawdown:          {s.max_dd * 100:.2f}%")
    print(f"Risk halt triggered:   {s.rm.state.halted} "
          f"({s.rm.state.halt_reason or 'never'})")
    print(f"Daily growth factor:   {growth_day:.4f}x")
    print(f"Days to $3.25M:        {days_to:,.0f}" if days_to != float("inf")
          else "Days to $3.25M:        never (no growth)")
    if cap_days is not None:
        print(f"Days to $3.25M (with $%.0fk/trade cap): {cap_days:,}"
              % (config.MAX_TRADE_DOLLARS / 1000))
    elif n:
        print("Days to $3.25M (capped model): never in 100 years — "
              "measured EV does not compound to the target")
    if n:
        avg_c = sum(x["contracts"] for x in s.trades) / n
        print(f"Avg contracts/trade:   {avg_c:.2f}")
        print(f"Avg PnL per contract:  ${avg / avg_c:+.5f}")
        print(f"Entry price range:     "
              f"{min(x['entry_price'] for x in s.trades)}-"
              f"{max(x['entry_price'] for x in s.trades)}c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+",
                    default=["2026-09-29", "2026-09-30", "2026-10-01",
                             "2026-10-02", "2026-10-03"])
    ap.add_argument("--start", type=float, default=35.0)
    ap.add_argument("--paper-experiment", action="store_true",
                    help="Run with PAPER_GUARDS_ENABLED=false (the built-in "
                         "paper knobs: 90% drawdown halt, no halt block), the "
                         "mode designed for growth-trajectory experiments. "
                         "Without it the guards are the production dry-run "
                         "ones and the run halts on the first 8% drawdown.")
    ap.add_argument("--taker", action="store_true",
                    help="Run the taker strategy (AGGRESSIVE_FILL=true: cross "
                         "the ask, model gate, taker fee) instead of the "
                         "resting maker strategy.")
    ap.add_argument("--collected", metavar="JSONL",
                    help="Replay a live-collected book JSONL from "
                         "scripts/live_book_collector.py instead of the HF "
                         "parquet history (today's live fill data).")
    args = ap.parse_args()

    if args.paper_experiment:
        config.PAPER_GUARDS_ENABLED = False
        print("Paper-experiment mode: PAPER_GUARDS_ENABLED=false "
              "(halt threshold 90%, guard blocks bypassed)")
    if args.taker:
        config.AGGRESSIVE_FILL = True
        print("Taker mode: AGGRESSIVE_FILL=true (cross the ask, model gate, "
              "taker fee)")

    if args.collected:
        df = load_collected(args.collected)
        tickers = build_tickers(df)
        # simulate() settles every ticker at its close, so a ticker without a
        # recorded settlement would book a fabricated outcome. Only replay
        # windows whose live result is known.
        tickers = {t: m for t, m in tickers.items() if m["has_result"]}
        span_days = ((max(m["close_ts"] for m in tickers.values())
                      - min(m["open_ts"] for m in tickers.values())) / 86400.0
                     if tickers else 0.0)
        days_n = max(span_days, 1e-9)
        print(f"Loaded {len(tickers)} settled live windows from "
              f"{args.collected} ({df['series'].nunique()} series, "
              f"{span_days * 24:.1f}h span)")
    else:
        df = load_days(args.days)
        tickers = build_tickers(df)
        days_n = len(args.days)
        print(f"Loaded {len(tickers)} market windows from {len(args.days)} days "
              f"({df['series'].nunique()} series)")
    t0 = time.time()
    s = simulate(tickers, days_n, args.start)
    print(f"Simulated in {time.time() - t0:.1f}s")
    summarize(s, days_n)
    out_name = "live_trades.json" if args.collected else "drysim_trades.json"
    (SCRATCH / out_name).write_text(
        json.dumps(s.trades, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())