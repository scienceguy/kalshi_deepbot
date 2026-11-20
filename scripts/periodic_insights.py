#!/usr/bin/env python3
"""
periodic_insights.py — Periodic web search for market insights + MemPalace heartbeat.

Runs every 10 minutes:
1. Web search for actionable market insights (crypto, prediction markets, volatility)
2. MemPalace logstream heartbeat/checkpoint for peer agent coordination
3. Posts findings to MemPalace for shared knowledge

This is designed to run as a background daemon or from systemd timer.
"""

import os
import sys
import json
import time
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Add repo to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mempalace_bridge
import agent_identity

log = logging.getLogger(__name__)

# Configuration
SEARCH_INTERVAL_SECONDS = 600  # 10 minutes
HEARTBEAT_INTERVAL_SECONDS = 300  # 5 minutes
MAX_SEARCH_RESULTS = 5

# Search queries tailored to our trading goals
SEARCH_QUERIES = [
    "Kalshi 15-minute binary crypto markets volume skew",
    "Polymarket US crypto updown 15min settlement price",
    "Bitcoin 15-minute volatility prediction market edge",
    "Ethereum short-term binary options pricing anomaly",
    "crypto prediction market maker strategy 95-99 cent",
    "CF Benchmarks BRTI 15min settlement methodology",
]


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def web_search(query: str, max_results: int = MAX_SEARCH_RESULTS) -> list:
    """Perform a web search using the websearch tool via subprocess."""
    # Use the opencode websearch capability via a helper script
    # For now, we'll use a simple curl to a search API or duckduckgo
    # This is a placeholder - in practice, this would call the actual websearch tool
    try:
        # Use python to do a simple web search via duckduckgo html
        import urllib.request
        import urllib.parse
        import re

        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib.request.urlopen(req, timeout=10)
        html = response.read().decode('utf-8', errors='ignore')

        # Extract result snippets
        snippets = re.findall(r'class="result__snippet">(.*?)</a>', html, re.DOTALL)
        snippets = [re.sub('<[^>]+>', '', s).strip()[:200] for s in snippets]
        return snippets[:max_results]
    except Exception as e:
        log.warning("Web search failed for '%s': %s", query, e)
        return []


def post_insight_to_mempalace(query: str, results: list, source: str = "periodic_insights"):
    """Post search results to MemPalace as a drawer entry."""
    try:
        bridge = mempalace_bridge.get_default_bridge(agent_identity.KALSHI_TRADER)
        content = f"Web search: {query}\n\nResults:\n" + "\n".join(f"- {r}" for r in results)
        bridge.drawer(
            content=content,
            metadata={"query": query, "source": source, "ts": datetime.now(timezone.utc).isoformat()},
        )
        log.info("Posted insight to MemPalace: %s", query[:50])
    except Exception as e:
        log.warning("Failed to post to MemPalace: %s", e)


def send_heartbeat(cycles_completed: int = 0):
    """Send a heartbeat to MemPalace logstream for peer coordination."""
    try:
        bridge = mempalace_bridge.get_default_bridge(agent_identity.KALSHI_TRADER)
        bridge.heartbeat(cycles_completed=cycles_completed)
        log.debug("Heartbeat sent to MemPalace logstream")
    except Exception as e:
        log.warning("Failed to send heartbeat: %s", e)


def checkpoint_state():
    """Save a checkpoint of current trading state to MemPalace."""
    try:
        from local_db import get_local_db, get_poly_db
        import config

        db = get_local_db()
        poly_db = get_poly_db('dry')

        kalshi_balance = float(db.get_bot_state("balance") or 0)
        kalshi_pnl = float(db.get_bot_state("session_pnl") or 0)
        kalshi_cycles = int(db.get_bot_state("cycles") or 0)

        poly_state = poly_db.get_bot_state("poly_us_state:dry")
        poly_equity = 35.0
        poly_pnl = 0.0
        if poly_state:
            if isinstance(poly_state, str):
                state = json.loads(poly_state)
            else:
                state = poly_state
            poly_equity = state.get("cash", 0) + sum(p.get("mtm_value", 0) for p in state.get("positions", []))
            poly_pnl = state.get("realized_pnl", 0)

        bridge = mempalace_bridge.get_default_bridge(agent_identity.KALSHI_TRADER)
        checkpoint = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kalshi": {
                "balance": kalshi_balance,
                "session_pnl": kalshi_pnl,
                "cycles": kalshi_cycles,
            },
            "polymarket_us": {
                "equity": poly_equity,
                "realized_pnl": poly_pnl,
            },
        }
        bridge.checkpoint(checkpoint)
        bridge.drawer(
            content=json.dumps(checkpoint, indent=2),
            metadata={"type": "periodic_checkpoint", "ts": datetime.now(timezone.utc).isoformat()},
        )
        log.info("Checkpoint saved: Kalshi=$%.2f (PnL=$%.2f), Poly=$%.2f (PnL=$%.2f)",
                 kalshi_balance, kalshi_pnl, poly_equity, poly_pnl)
    except Exception as e:
        log.warning("Failed to save checkpoint: %s", e)


def sweep_inbox():
    """Sweep MemPalace inbox for messages addressed to this agent."""
    try:
        bridge = mempalace_bridge.get_default_bridge(agent_identity.KALSHI_TRADER)
        tasks = bridge.drain_tasks()
        for ev in tasks:
            log.info("Inbox task: type=%s from=%s correlation=%s", ev.get("type"), ev.get("from_agent"), ev.get("correlation_id"))
            # Process specific event types
            if ev.get("type") == "task.request":
                log.info("Task request received: %s", str(ev.get("body", ""))[:100])
            elif ev.get("type") == "status":
                log.info("Status update from %s: %s", ev.get("from_agent"), str(ev.get("body", ""))[:100])
    except Exception as e:
        log.warning("Failed to sweep inbox: %s", e)


def run_cycle(cycles_completed: int = 0):
    """Run one cycle of searches, heartbeat, checkpoint, and inbox sweep."""
    log.info("=== Periodic insights cycle started ===")

    # 1. Send heartbeat
    send_heartbeat(cycles_completed)

    # 2. Sweep inbox
    sweep_inbox()

    # 3. Run web searches (rotate through queries)
    import random
    query = random.choice(SEARCH_QUERIES)
    log.info("Searching: %s", query)
    results = web_search(query)
    if results:
        post_insight_to_mempalace(query, results)
        for r in results:
            log.info("  Result: %s", r[:100])
    else:
        log.info("  No results")

    # 4. Save checkpoint
    checkpoint_state()

    log.info("=== Periodic insights cycle completed ===")


def main():
    setup_logging()
    log.info("Starting periodic insights daemon (search every %ds, heartbeat every %ds)",
             SEARCH_INTERVAL_SECONDS, HEARTBEAT_INTERVAL_SECONDS)

    last_search = 0
    last_heartbeat = 0
    cycles = 0

    while True:
        now = time.time()

        # Run heartbeat more frequently
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            send_heartbeat(cycles)
            sweep_inbox()
            last_heartbeat = now

        # Run search and checkpoint less frequently
        if now - last_search >= SEARCH_INTERVAL_SECONDS:
            query = SEARCH_QUERIES[int(now / SEARCH_INTERVAL_SECONDS) % len(SEARCH_QUERIES)]
            log.info("Searching: %s", query)
            results = web_search(query)
            if results:
                post_insight_to_mempalace(query, results)
                for r in results:
                    log.info("  Result: %s", r[:100])
            checkpoint_state()
            last_search = now

        cycles += 1
        # Sleep for a bit
        time.sleep(30)


if __name__ == "__main__":
    main()