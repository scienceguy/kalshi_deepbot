"""
polymarket_arb.py — Polymarket/Kalshi cross-market arbitrage detector.

Polymarket (https://polymarket.com) runs a CLOB for prediction markets on crypto,
politics, and events. Each market has outcome tokens whose price (0-1 USDC)
represents the implied probability of that outcome.

Kalshi runs a similar binary market system. When both platforms list the same
event (e.g., "BTC price up in next 15 mins?"), we can arbitrage:

    Buy YES on Kalshi at price_p  (implied prob p)
    Buy NO  on Polymarket at price_q  (implied prob 1-q)

If p + q < 1, we have an arbitrage opportunity:
    - Buy YES on Kalshi at p
    - Buy NO on Polymarket at q (= 1 - implied_probability_NO)
    - Total cost: p + q < 1
    - Guaranteed payout: 1.0
    - Profit: 1.0 - (p + q) per contract pair

Usage:
    arb = PolymarketArb(kalshi_client, polymarket_client)
    opportunities = arb.find_arbitrage(kalshi_markets, polymarket_markets)
"""

import logging
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger("polymarket_arb")

# Minimum edge (in dollars) required to consider an arbitrage viable.
# Accounts for taker fees on both platforms (~7% Kalshi, ~2% Polymarket).
MIN_ARB_EDGE_DOLLARS = 0.02

# Maximum slippage tolerance (as fraction of price). If the order book depth
# at the quoted price is too small, skip the opportunity.
MAX_SLIPPAGE_PCT = 0.05


class PolymarketArb:
    """Cross-market arbitrage detector between Kalshi and Polymarket.

    For each Kalshi market, finds the corresponding Polymarket market by
    matching on series/ticker or question text. Computes the no-arbitrage
    bound: p_kalshi + q_polymarket <= 1. If violated, an arbitrage exists.

    The arb direction is always:
        - Buy YES on the cheaper market
        - Buy NO on the more expensive market
    """

    def __init__(self, kalshi_client=None, polymarket_client=None):
        self.kalshi_client = kalshi_client
        self.polymarket_client = polymarket_client

    def find_arbitrage(
        self,
        kalshi_markets: List[Dict],
        polymarket_markets: Optional[List[Dict]] = None,
    ) -> List[Dict[str, Any]]:
        """Find arbitrage opportunities between Kalshi and Polymarket markets.

        Args:
            kalshi_markets: List of Kalshi market dicts (from market_finder).
            polymarket_markets: Optional list of Polymarket market dicts.
                If None, fetches from PolymarketClient.

        Returns:
            List of arbitrage opportunity dicts, sorted by edge descending.
        """
        if polymarket_markets is None:
            try:
                from polymarket_client import get_client as get_pm_client
                pm_client = get_pm_client()
                pm_markets = pm_client.get_markets()
                polymarket_markets = pm_markets.get("markets", [])
            except Exception as e:
                log.warning("Could not fetch Polymarket markets: %s", e)
                polymarket_markets = []

        # Build lookup: polymarket market by series/ticker or question text
        pm_by_ticker: Dict[str, Dict] = {}
        pm_by_question: Dict[str, Dict] = {}
        for pm in polymarket_markets:
            ticker = pm.get("ticker") or pm.get("condition_id") or ""
            question = pm.get("question", "")
            if ticker:
                pm_by_ticker[ticker] = pm
            if question:
                pm_by_question[question.lower()] = pm

        opportunities = []

        for km in kalshi_markets:
            ticker = km.ticker if hasattr(km, "ticker") else km.get("ticker", "")
            question = getattr(km, "title", "") or km.get("title", "")

            pm = None
            if ticker in pm_by_ticker:
                pm = pm_by_ticker[ticker]
            elif question.lower() in pm_by_question:
                pm = pm_by_question[question.lower()]

            if pm is None:
                continue

            sig = self._evaluate_arbitrage(km, pm)
            if sig:
                opportunities.append(sig)

        opportunities.sort(key=lambda x: x["edge_dollars"], reverse=True)
        return opportunities

    def _evaluate_arbitrage(
        self,
        kalshi_market: Any,
        polymarket_market: Dict,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate a single Kalshi market vs its Polymarket counterpart.

        Returns an arbitrage opportunity dict or None if no edge.
        """
        # Get Kalshi YES price (0-1 probability)
        yes_bid = getattr(kalshi_market, "yes_bid", None) or kalshi_market.get("yes_bid", 0)
        yes_ask = getattr(kalshi_market, "yes_ask", None) or kalshi_market.get("yes_ask", 0)
        no_bid = getattr(kalshi_market, "no_bid", None) or kalshi_market.get("no_bid", 0)
        no_ask = getattr(kalshi_market, "no_ask", None) or kalshi_market.get("no_ask", 0)

        if not yes_bid or not yes_ask:
            return None

        kalshi_yes_price = (yes_bid + yes_ask) / 200.0  # Convert cents to probability
        kalshi_no_price = (no_bid + no_ask) / 200.0 if no_bid and no_ask else 1.0 - kalshi_yes_price

        # Get Polymarket prices
        pm_token_id = polymarket_market.get("token_id") or polymarket_market.get("id", "")
        pm_prices = self._get_polymarket_prices(pm)
        if pm_prices is None:
            return None

        # Polymarket prices are per-outcome-token (0-1)
        pm_yes_price = pm_prices.get("yes", pm_prices.get("1", 0.5))
        pm_no_price = pm_prices.get("no", pm_prices.get("0", 0.5))

        # Normalize: pm_yes + pm_no should be ~1 (fee spread)
        pm_total = pm_yes_price + pm_no_price
        if pm_total > 0:
            pm_yes_price /= pm_total
            pm_no_price /= pm_total

        # Arbitrage detection:
        # - If Kalshi YES is cheaper than Polymarket NO: buy YES on Kalshi, buy NO on Polymarket
        #   Edge = 1 - (kalshi_yes_price + pm_no_price)
        # - If Polymarket YES is cheaper than Kalshi NO: buy YES on Polymarket, buy NO on Kalshi
        #   Edge = 1 - (pm_yes_price + kalshi_no_price)

        edge_k_yes_pm_no = 1.0 - (kalshi_yes_price + pm_no_price)
        edge_pm_yes_k_no = 1.0 - (pm_yes_price + kalshi_no_price)

        best_edge = max(edge_k_yes_pm_no, edge_pm_yes_k_no)

        if best_edge <= MIN_ARB_EDGE_DOLLARS:
            return None

        if edge_k_yes_pm_no >= edge_pm_yes_k_no:
            # Buy YES on Kalshi, buy NO on Polymarket
            entry_k = yes_ask / 100.0  # Buy YES at Kalshi ask
            entry_pm = pm_no_price     # Buy NO on Polymarket at its price
            direction = "KALSHI_YES_POLYMARKET_NO"
        else:
            # Buy YES on Polymarket, buy NO on Kalshi
            entry_k = no_ask / 100.0   # Buy NO at Kalshi ask
            entry_pm = pm_yes_price    # Buy YES on Polymarket
            direction = "POLYMARKET_YES_KALSHI_NO"

        # Size: limited by smaller position size
        max_contracts = min(config.MAX_TRADE_DOLLARS / max(entry_k, entry_pm), 1.0)
        if max_contracts < 0.01:
            return None

        expected_pnl = best_edge * max_contracts

        return {
            "ticker": getattr(kalshi_market, "ticker", "") or kalshi_market.get("ticker", ""),
            "direction": direction,
            "kalshi_entry_price": entry_k,
            "polymarket_entry_price": entry_pm,
            "edge_dollars": best_edge,
            "edge_pct": best_edge * 100,
            "contracts": max_contracts,
            "expected_pnl_dollars": expected_pnl,
            "kalshi_yes_price": kalshi_yes_price,
            "kalshi_no_price": kalshi_no_price,
            "polymarket_yes_price": pm_yes_price,
            "polymarket_no_price": pm_no_price,
        }