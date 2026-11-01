#!/usr/bin/env python3
"""
latency_arb.py — Latency arbitrage strategy for Kalshi 15-minute markets.

Strategy (from FINDINGS.md audit):
  - Market: 15-min crypto binary markets (KXBTC15M, KXETH15M, etc.)
  - Entry window: last 10 minutes before close (minutes_left <= 10)
  - Size: 1 contract
  - Type: Taker order (market order)
  - Edge threshold: MIN_EDGE 0.04 (4 cents)
  - Fee model: Kalshi 7% quadratic taker fee included
  - Latency constraint: edge survives 2s only 33% of time — must execute fast

Entry logic:
  1. Fetch market data (bid/ask, close_time, expiration_value)
  2. Check minutes_left <= 10
  3. Compute mid = (yes_bid + yes_ask) / 2
  4. Compute edge = |mid - 50| - fee_estimate
  5. If edge >= MIN_EDGE (0.04), place taker order:
     - If mid > 50: BUY YES at yes_ask
     - If mid < 50: BUY NO at no_ask (equiv: sell YES at yes_bid)
  6. Record settlement outcome when market closes

Returns dict with signal info for Supabase/n8n logging.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import config
from fees import buy_cash_out, sell_cash_in
from kalshi_client import KalshiClient

log = logging.getLogger("latency_arb")

MIN_EDGE_CENTS = float(os.environ.get("LATENCY_ARB_MIN_EDGE_CENTS", "4.0"))
ENTRY_WINDOW_MIN = float(os.environ.get("LATENCY_ARB_ENTRY_WINDOW_MIN", "10.0"))
MAX_CONTRACTS = int(os.environ.get("LATENCY_ARB_MAX_CONTRACTS", "1"))


def evaluate_market(market: dict) -> dict | None:
    """
    Evaluate a market for latency arbitrage opportunity.

    Returns dict with signal info or None if no trade.
    """
    ticker = market.get("ticker", "")
    series = market.get("series_ticker", "")

    # Parse prices
    yb = market.get("yes_bid", 0) or 0
    ya = market.get("yes_ask", 0) or 0
    nb = market.get("no_bid", 0) or 0
    na = market.get("no_ask", 0) or 0

    if ya <= 0 or nb <= 0:
        return None  # No valid quotes

    # Check timing window
    ct = market.get("close_time")
    if ct:
        try:
            ct_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            minutes_left = (ct_dt - now).total_seconds() / 60
        except (ValueError, AttributeError):
            minutes_left = market.get("minutes_left", 999)
    else:
        minutes_left = market.get("minutes_left", 999)

    if minutes_left > ENTRY_WINDOW_MIN:
        return None  # Not in entry window yet

    if minutes_left <= 0:
        return None  # Market already closed

    # Compute mid price
    mid = (yb + ya) / 2.0

    # Compute edge: distance from 50c minus fee
    # Kalshi taker fee = 0.07 * contracts * price * (1 - price)
    # For 1 contract at price p: fee = 0.07 * p * (1 - p)
    # Edge must exceed fee to be profitable
    fee_rate = 0.07
    # Estimate fee at mid price
    est_fee = fee_rate * (mid / 100.0) * (1.0 - mid / 100.0) * 100  # in cents
    edge_cents = abs(mid - 50.0) - est_fee

    if edge_cents < MIN_EDGE_CENTS:
        return None  # Edge too small

    # Determine direction: buy the side closer to 50c (cheaper to enter)
    # If YES mid > 50, buy YES at yes_ask
    # If YES mid < 50, buy NO at no_ask (equiv: sell YES at yes_bid)
    if mid > 50:
        side = "yes"
        entry_price = ya  # Buy YES at ask
        direction = "YES"
    else:
        side = "no"
        entry_price = na  # Buy NO at ask
        direction = "NO"

    # Verify entry price is valid
    if entry_price <= 0 or entry_price >= 100:
        return None

    return {
        "ticker": ticker,
        "series": series,
        "direction": direction,
        "side": side,
        "entry_price": entry_price,
        "entry_price_cents": int(round(entry_price)),
        "contracts": MAX_CONTRACTS,
        "mid": mid,
        "edge_cents": edge_cents,
        "minutes_left": minutes_left,
        "close_time": ct,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": "latency_arb",
    }


def simulate_settlement(market: dict, signal: dict) -> dict | None:
    """
    Simulate settlement outcome for a closed market.
    Returns P&L info or None if cannot determine.
    """
    exp_val = market.get("expiration_value")
    if exp_val is None:
        return None

    settled_yes = int(round(float(exp_val) * 100))  # 0 or 100

    contracts = signal["contracts"]
    entry_price = signal["entry_price"] / 100.0  # cents to dollars
    side = signal["side"]

    # Calculate P&L
    if side == "yes":
        # Bought YES at entry_price, settles at settled_yes/100
        buy_cost = buy_cash_out(contracts, entry_price)
        # Sell at settlement: settled_yes/100 per contract
        sell_price = settled_yes / 100.0
        sell_revenue = sell_cash_in(contracts, sell_price)
    else:
        # Bought NO at entry_price, settles at (100 - settled_yes)/100
        buy_cost = buy_cash_out(contracts, entry_price)
        # Sell NO at settlement: (100 - settled_yes)/100 per contract
        sell_price = (100 - settled_yes) / 100.0
        sell_revenue = sell_cash_in(contracts, sell_price)

    realized_pnl = sell_revenue - buy_cost
    won = sell_revenue > buy_cost

    return {
        "order_id": signal.get("order_id", ""),
        "ticker": signal["ticker"],
        "side": side,
        "entry_price": signal["entry_price"],
        "exit_price": sell_price,
        "contracts": contracts,
        "settled_yes": settled_yes,
        "won": won,
        "realized_pnl": round(realized_pnl, 2),
        "buy_cost": round(buy_cost, 2),
        "sell_revenue": round(sell_revenue, 2),
    }


def run(markets=None):
    """
    Run latency arb evaluation on a list of markets.
    Returns list of trade signals.
    """
    if markets is None:
        markets = []

    signals = []
    for m in markets:
        sig = evaluate_market(m)
        if sig:
            signals.append(sig)

    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
    client = KalshiClient()
    markets = []
    for series in config.TARGET_SERIES:
        try:
            mkts = client.get_markets(series_ticker=series, status="open", limit=50)
            markets.extend(mkts)
        except Exception as e:
            log.warning("%s fetch failed: %s", series, e)

    signals = run(markets)
    log.info("Latency arb: %d signals from %d markets", len(signals), len(markets))
    for sig in signals:
        log.info("Signal: %s %s @ %dc edge=%.2fc ml=%.1fmin",
                 sig["ticker"], sig["direction"], sig["entry_price_cents"],
                 sig["edge_cents"], sig["minutes_left"])