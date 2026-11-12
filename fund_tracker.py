"""fund_tracker.py — READ-ONLY balance + manual-transfer ledger for
Polymarket US <-> Kalshi.

Both venues hold USD cash; there is NO direct venue-to-venue transfer and
no API write endpoint on either. Real moves are two manual bank legs done
in the apps. This tool only:
  - reads both venues' balances (Kalshi API + Polymarket US, both read-only)
  - prints a consolidated view of where idle cash sits
  - appends manual move entries to a local JSONL ledger for a paper trail

It places NO orders, initiates NO transfers, and never writes secrets.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

LEDGER_FILE = Path(__file__).resolve().parent / "fund_ledger.jsonl"


def _read_kalshi_balance() -> dict:
    from kalshi_client import KalshiClient

    client = KalshiClient()
    try:
        bal = client.get_balance()
        return {"venue": "kalshi", "cash_dollars": round(bal, 2), "ok": True}
    except Exception as e:  # noqa: BLE001 - surface read failure, keep going
        return {"venue": "kalshi", "cash_dollars": None, "ok": False, "error": str(e)}
    finally:
        client.close()


def _read_pm_balance() -> dict:
    import polymarket_us_client as pm

    try:
        data = pm.get_account_balances()
        total = pm.usd_cash_balance(data)
        return {"venue": "polymarket_us", "cash_dollars": round(total, 2), "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"venue": "polymarket_us", "cash_dollars": None, "ok": False,
                "error": str(e)}
    finally:
        pm.close()


def _load_ledger() -> list:
    if not LEDGER_FILE.exists():
        return []
    out = []
    for line in LEDGER_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append_ledger(entry: dict) -> None:
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    with LEDGER_FILE.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def cmd_balance(_args) -> int:
    rows = [_read_kalshi_balance(), _read_pm_balance()]
    print("Polymarket US <-> Kalshi  (READ-ONLY cash view)")
    print("=" * 50)
    total = 0.0
    for r in rows:
        if r["ok"] and r["cash_dollars"] is not None:
            total += r["cash_dollars"]
            print(f"{r['venue']:<14}{('$'+format(r['cash_dollars'], ',.2f')):>13}")
        else:
            print(f"{r['venue']:<14}  ERROR: {r.get('error','no balance field')}")
    print("-" * 50)
    print(f"{'TOTAL':<14}{('$'+format(total, ',.2f')):>13}")
    print("\nNote: no direct venue-to-venue transfer exists. Moves are two")
    print("manual bank legs. Record them with: python fund_tracker.py move")
    return 0


def cmd_move(args) -> int:
    # Validate amount.
    if args.amount is None or args.amount <= 0:
        print("ERROR: --amount must be a positive number", file=sys.stderr)
        return 1
    direction = args.direction
    if direction not in ("kalshi_to_pm", "pm_to_kalshi"):
        print("ERROR: --direction must be kalshi_to_pm or pm_to_kalshi",
              file=sys.stderr)
        return 1
    entry = {
        "event": "manual_transfer",
        "direction": direction,
        "amount_dollars": round(args.amount, 2),
        "note": args.note or "",
    }
    _append_ledger(entry)
    print(f"Recorded: {direction} ${entry['amount_dollars']:.2f} {args.note or ''}")
    return 0


def cmd_ledger(_args) -> int:
    entries = _load_ledger()
    if not entries:
        print("Ledger empty. Record a move with: python fund_tracker.py move")
        return 0
    print("Transfer ledger:")
    print("=" * 60)
    for e in entries:
        print(f"{e.get('ts','?')}  {e.get('direction','?'):<14} "
              f"${e.get('amount_dollars',0):>10,.2f}  {e.get('note','')}")
    return 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Polymarket US <-> Kalshi READ-ONLY fund tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_b = sub.add_parser("balance", help="Show both venues' cash balances")
    p_b.set_defaults(fn=cmd_balance)

    p_m = sub.add_parser("move", help="Record a manual transfer in the ledger")
    p_m.add_argument("--direction", required=True,
                     choices=["kalshi_to_pm", "pm_to_kalshi"])
    p_m.add_argument("--amount", type=float, required=True)
    p_m.add_argument("--note", default="")
    p_m.set_defaults(fn=cmd_move)

    p_l = sub.add_parser("ledger", help="Show recorded transfers")
    p_l.set_defaults(fn=cmd_ledger)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
