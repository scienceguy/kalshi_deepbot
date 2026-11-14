#!/usr/bin/env python3
"""Market capacity tester across 5M, 15M, 1H, 1D cadences on Kalshi and Polymarket.

Queries:
- Kalshi: 15M (KXBTC15M, etc.), Hourly (KXBTCH, etc.), Daily (KXBTCD, etc.)
- Polymarket US: categories=crypto or events list to inspect available market families

Reports:
- Active market count per cadence
- Top-of-book spread and depth (contracts available at bid/ask)
- Liquidity and open interest capacity
"""
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kalshi_client import KalshiClient
import polymarket_us_client as pm_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("capacity_test")

def inspect_kalshi():
    client = KalshiClient()
    import config
    series_by_cadence = {
        "15M": config.TARGET_SERIES,
        "1H/W": ["KXZECH", "KXNEARH", "KXTONH", "KXNEARW", "KXTONW"],
        "1D":  ["KXBTCD", "KXETHD", "KXSOLD", "KXDOGED", "KXBNBD", "KXNEARD", "KXZECD", "KXBCHD"],
    }
    
    report = {}
    for cadence, series_list in series_by_cadence.items():
        cadence_stats = {"series_tested": len(series_list), "active_markets": 0, "total_depth_contracts": 0, "markets": []}
        for s in series_list:
            try:
                resp = client.get_markets(series_ticker=s, status="open", limit=10)
                mkts = resp if isinstance(resp, list) else resp.get("markets", [])
                cadence_stats["active_markets"] += len(mkts)
                for m in mkts:
                    yb = m.get("yes_bid") or 0
                    ya = m.get("yes_ask") or 0
                    vol = m.get("volume") or 0
                    oi = m.get("open_interest") or 0
                    cadence_stats["total_depth_contracts"] += vol + oi
                    cadence_stats["markets"].append({
                        "ticker": m.get("ticker"),
                        "bid": yb,
                        "ask": ya,
                        "spread": (ya - yb) if ya and yb else None,
                        "volume": vol,
                        "oi": oi,
                    })
            except Exception as e:
                log.warning("Kalshi series %s error: %s", s, e)
        report[cadence] = cadence_stats
    return report

def inspect_polymarket():
    report = {"status": "checked", "markets": [], "total_crypto_markets": 0}
    try:
        client = pm_client._get_client()
        if not client:
            report["error"] = "Polymarket client credentials not configured"
            return report
        res = client.markets.list()
        d = res.model_dump() if hasattr(res, "model_dump") else dict(res)
        mkts = d.get("markets", [])
        # Check for any crypto markets
        crypto_mkts = [m for m in mkts if "btc" in str(m.get("slug","")).lower() or "eth" in str(m.get("slug","")).lower() or "crypto" in str(m.get("categories","")).lower()]
        report["total_crypto_markets"] = len(crypto_mkts)
        report["total_active_markets_sample"] = len(mkts)
        for m in (crypto_mkts if crypto_mkts else mkts[:5]):
            report["markets"].append({
                "slug": m.get("slug", ""),
                "title": m.get("title", ""),
                "active": m.get("active", True),
            })
    except Exception as e:
        report["error"] = str(e)
    return report

def main():
    log.info("Starting market capacity analysis on VPS...")
    k_rep = inspect_kalshi()
    p_rep = inspect_polymarket()
    
    print("\n" + "="*60)
    print("KALSHI MARKET CAPACITY SUMMARY:")
    for cad, stats in k_rep.items():
        print(f"  Cadence {cad:>3}: {stats['active_markets']} active markets, {stats['total_depth_contracts']} contracts vol+OI across {stats['series_tested']} series")
        for m in stats["markets"][:3]:
            print(f"    - {m['ticker']}: bid={m['bid']} ask={m['ask']} spread={m['spread']} vol={m['volume']} oi={m['oi']}")
            
    print("\nPOLYMARKET US CAPACITY SUMMARY:")
    print(f"  Total crypto markets found on api.polymarket.us: {p_rep.get('total_crypto_markets', 0)}")
    if p_rep.get("error"):
        print(f"  Error / Note: {p_rep['error']}")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
