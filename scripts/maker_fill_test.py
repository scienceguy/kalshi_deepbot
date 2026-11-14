"""Live-book maker fill study: no fees, real quotes, real settlements.

Tests the fee-free maker variant end to end against the live Kalshi book:

* every candidate rests a limit order 1c under the ask on its side,
* the order fills only when a LATER snapshot shows the ask at or below that
  price (a seller actually crossed into us), capped by the quoted counterparty
  depth — the same rule the paper trader uses (`trader._dry_run_fill_count`),
* P&L is settled from the exchange result via `trader._settle_pnl` with
  `AGGRESSIVE_FILL = False`, which must book exactly zero fee.

It writes one JSONL row per event and prints a summary. It places no orders and
does not touch the live paper ledger.

Usage (on the VPS):
    python scripts/maker_fill_test.py --minutes 20 --interval 15
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# trader.py builds its own argparse parser at import time, so hand it a safe
# argv and keep ours aside (same trick the test suite uses).
_ARGV = sys.argv[1:]
sys.argv = ["maker_fill_test.py"]

import config  # noqa: E402
import trader  # noqa: E402
from kalshi_client import KalshiClient  # noqa: E402
from market_finder import find_active_markets, _settle_yes_cents  # noqa: E402

OUT = Path("maker_fill_test.jsonl")


def _log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with OUT.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _in_window(minutes_in: float) -> bool:
    return (config.ENTRY_WINDOW_START_MIN
            <= minutes_in <= config.ENTRY_WINDOW_END_MIN)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--interval", type=float, default=15)
    ap.add_argument("--contracts", type=float, default=10)
    args = ap.parse_args(_ARGV)

    # The whole point: a maker entry pays no fee, and only a crossing ask fills.
    config.AGGRESSIVE_FILL = False
    config.DRY_RUN = True

    client = KalshiClient()
    pending: dict = {}
    filled: list = []
    settled: list = []
    posted = 0
    cycles = 0
    end_at = time.time() + args.minutes * 60
    print(f"maker fill study: {args.minutes:.0f} min, every {args.interval:.0f}s, "
          f"AGGRESSIVE_FILL={config.AGGRESSIVE_FILL}")

    try:
        while time.time() < end_at:
            cycles += 1
            now = time.time()
            markets = find_active_markets(client)
            live = {m.ticker: m for m in markets}

            # 1) settle anything whose market has a result
            for order in filled[:]:
                try:
                    mkt = client.get_market(order["ticker"])
                except Exception:
                    mkt = None
                yes = _settle_yes_cents(mkt) if mkt else None
                if yes is None:
                    continue
                exit_price = (yes / 100.0 if order["side"] == "yes"
                              else (100 - yes) / 100.0)
                pnl, fee = trader._settle_pnl(
                    order["contracts"], order["entry"], exit_price)
                row = {**order, "event": "settle", "settled_yes_cents": yes,
                       "pnl": round(pnl, 6), "fee": round(fee, 9),
                       "won": pnl > 0}
                settled.append(row)
                _log(row)
                print(f"SETTLE {order['ticker']} {order['side']} "
                      f"entry={order['entry']}c pnl=${pnl:+.4f} fee=${fee:.4f}")
                filled.remove(order)

            # 2) fill any resting order the book has crossed into. Fetch the
            # market fresh: a market that drifted out of the entry band still
            # has a live book, and leaving it out would hide real fills.
            for key, order in list(pending.items()):
                m = live.get(order["ticker"])
                if m is None:
                    try:
                        raw = client.get_market(order["ticker"])
                    except Exception:
                        raw = None
                    if raw:
                        from market_finder import _build, _parse_api_time
                        ct = _parse_api_time(raw.get("close_time", ""))
                        ml = ((ct - datetime.now(timezone.utc)).total_seconds() / 60.0
                              if ct else 0.0)
                        m = _build(raw, raw.get("series_ticker", ""), ct, ml, {})
                if m is None:
                    if (time.time() - order["posted_at"]) > order["max_wait_s"]:
                        pending.pop(key, None)  # never crossed: unfilled
                    continue
                got = trader._dry_run_fill_count(
                    m, order["side"], order["entry"], order["contracts"])
                if got > 0:
                    row = {**order, "event": "fill", "contracts": got,
                           "fill_ask": getattr(m, f"{order['side']}_ask")}
                    filled.append(row)
                    pending.pop(key, None)
                    _log(row)
                    print(f"FILL {order['ticker']} {order['side']} "
                          f"@{order['entry']}c ask={row['fill_ask']} x{got:.0f}")
                elif m.minutes_left <= 0:
                    pending.pop(key, None)  # expired unfilled

            # 3) post new maker orders inside the entry window
            for m in markets:
                minutes_in = 15.0 - m.minutes_left
                if not _in_window(minutes_in):
                    continue
                for side in ("yes", "no"):
                    ask = m.yes_ask if side == "yes" else m.no_ask
                    if not ask or ask < config.ENTRY_BAND_MIN or ask > config.ENTRY_BAND_MAX:
                        continue
                    key = (m.ticker, side)
                    if key in pending:
                        continue
                    entry = max(1, ask - 1)
                    pending[key] = {
                        "event": "post", "ticker": m.ticker, "side": side,
                        "entry": entry, "ask_at_post": ask,
                        "contracts": args.contracts,
                        "minutes_in": round(minutes_in, 2),
                        "yes_liq": m.yes_liquidity, "no_liq": m.no_liquidity,
                        "posted_at": now,
                        # Rest until the window closes; then it was unfilled.
                        "max_wait_s": max(60.0, (m.minutes_left + 1.0) * 60.0),
                    }
                    posted += 1
                    _log({**pending[key]})

            print(f"[{cycles}] pending={len(pending)} filled_open={len(filled)} "
                  f"settled={len(settled)}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    n = len(settled)
    wins = sum(1 for r in settled if r["won"])
    total_pnl = sum(r["pnl"] for r in settled)
    fees = sum(r["fee"] for r in settled)
    summary = {
        "event": "summary",
        "cycles": cycles,
        "orders_posted": posted,
        "fills": len(filled) + n,
        "fill_rate": round((len(filled) + n) / posted, 3) if posted else 0.0,
        "settled": n,
        "wins": wins,
        "win_rate": round(wins / n, 3) if n else 0.0,
        "total_pnl": round(total_pnl, 6),
        "per_trade_pnl": round(total_pnl / n, 6) if n else 0.0,
        "fees_charged": round(fees, 9),
        "open_unsettled": len(filled),
    }
    _log(summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
