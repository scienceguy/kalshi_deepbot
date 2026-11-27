#!/usr/bin/env python3
"""T19-11: compare real venue fill fees against the model.

Read-only portfolio call. No orders are placed. Raw production payloads are
stored in the local `fills` table (gitignored); the public report contains only
counts and error statistics, never balances, account IDs, or order IDs.

Two granularities are reported:
  * per fill, against the tolerant direct-grid ceiling model;
  * per order, because Kalshi applies trade fee, rounding fee, and rebate across
    all fills of an order so the total converges to one equivalent fill. A
    single fill may therefore sit one grid unit under the ceiling; the order
    total is the figure that must match.
"""
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import fees
from kalshi_client import KalshiClient
from local_db import get_local_db


def _number(obj, names):
    for name in names:
        value = obj.get(name)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def _actual_fee(fill):
    for key in ("fee", "fee_dollars", "fee_cost", "total_fee", "commission", "commission_dollars"):
        value = _number(fill, (key,))
        if value is not None:
            return value
    return None


def _raw_model_fee(contracts, price, is_taker, series):
    """Unrounded model fee used for the per-order accumulator comparison."""
    if not is_taker:
        info = fees.series_fee_info(series)
        if not info or not fees.series_has_maker_fees(info.get("fee_type")):
            return 0.0
        mult = float(info.get("fee_multiplier", 1.0) or 1.0)
        return fees.MAKER_FEE_RATE * mult * contracts * price * (1.0 - price)
    mult = fees.taker_multiplier(series) if series else 1.0
    return fees.FEE_RATE * mult * contracts * price * (1.0 - price)


def main():
    client = KalshiClient()
    data = client._request("GET", "/portfolio/fills", params={"limit": 100})
    fills = data.get("fills", []) if isinstance(data, dict) else []
    compared = []
    unclassified = []
    mismatches = []
    records = []
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        ticker = fill.get("ticker") or fill.get("market_ticker") or ""
        price = _number(fill, ("price", "price_dollars", "fill_price", "fill_price_dollars"))
        if price is None:
            price = _number(fill, ("yes_price_dollars", "no_price_dollars"))
        contracts = _number(fill, ("count", "count_fp", "contracts", "fill_count", "fill_count_fp"))
        actual = _actual_fee(fill)
        if price is None or contracts is None or actual is None or not ticker:
            unclassified.append(fill)
            continue
        is_taker = fill.get("is_taker", fill.get("taker"))
        if not isinstance(is_taker, bool):
            unclassified.append(fill)
            continue
        series = fees.series_from_ticker(ticker)
        model = (fees.taker_fee_dollars(contracts, price, series=series)
                 if is_taker else fees.maker_fee_dollars(contracts, price, series=series))
        compared.append((model, actual))
        if abs(model - actual) > 1e-6:
            mismatches.append((model, actual))
        raw = dict(fill)
        raw["model_fee_dollars"] = model
        raw["actual_fee_dollars"] = actual
        raw["comparison_diff_dollars"] = actual - model
        recorded = get_local_db().record_fill_unified({
            "ts": (fill.get("created_time") or fill.get("ts")
                   or datetime.now(timezone.utc).isoformat()),
            "order_id": fill.get("order_id", ""),
            "ticker": ticker,
            "side": str(fill.get("side", "")),
            "price_cents": int(round(price * 100)),
            "qty": contracts,
            "fee_dollars": actual,
            "maker_taker": "taker" if is_taker else "maker",
            "settled": 0,
            "raw": raw,
        }, venue="kalshi")
        if not recorded:
            raise sqlite3.OperationalError("could not persist unified fill evidence")
        records.append((fill.get("order_id") or "",
                        _raw_model_fee(contracts, price, is_taker, series),
                        actual))
    grid = 0.000001
    order_groups = {}
    for order_id, raw_model, actual in records:
        group = order_groups.setdefault(order_id, [0.0, 0.0])
        group[0] += raw_model
        group[1] += actual
    order_exact = sum(
        1 for raw_model, actual in order_groups.values()
        if abs(math.ceil(raw_model / grid - 1e-9) * grid - actual) < 1e-9
    )
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "fills_returned": len(fills),
        "compared": len(compared),
        "unclassified": len(unclassified),
        "mismatches_over_1e-6": len(mismatches),
        "orders_compared": len(order_groups),
        "order_level_exact": order_exact,
        "max_abs_diff": max((abs(model - actual) for model, actual in compared), default=0.0),
        "mean_abs_diff": (
            sum(abs(model - actual) for model, actual in compared) / len(compared)
            if compared else 0.0
        ),
        "status": (
            "PASS"
            if compared and not unclassified
            and order_groups and order_exact == len(order_groups)
            else "UNVERIFIED"
        ),
    }
    lines = [
        "# T19-11 Real Fee Verification",
        "",
        f"Checked at: {report['checked_at']}",
        "",
        f"- fills returned: {report['fills_returned']}",
        f"- compared with complete side/price/fee fields: {report['compared']}",
        f"- unclassified (missing fields or unknown taker/maker): {report['unclassified']}",
        f"- per-fill mismatches over 1e-6 dollars: {report['mismatches_over_1e-6']}",
        f"- orders compared at accumulator granularity: {report['orders_compared']}",
        f"- order-level exact matches: {report['order_level_exact']}",
        f"- max per-fill absolute difference: {report['max_abs_diff']:.8f}",
        f"- mean per-fill absolute difference: {report['mean_abs_diff']:.8f}",
        f"- status: {report['status']}",
        "",
        "Raw fill payloads are stored only in the local `fills` table. This report",
        "contains counts and error statistics, never balances, account IDs, or order IDs.",
        "",
        "Kalshi applies trade fee, rounding fee, and rebate across all fills of an",
        "order, so one fill may sit one grid unit under the per-fill ceiling while",
        "the order total converges to the model. The per-fill count is diagnostic;",
        "the order-level comparison is the proof, and it is the figure the status",
        "line requires to be exact.",
        "",
        "Reproduce:",
        "",
        "```bash",
        ".venv/bin/python scripts/verify_real_fees.py",
        "```",
        "",
    ]
    out = Path("docs/FEE_VERIFICATION.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())