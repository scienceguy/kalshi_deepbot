"""
poly_us_trader.py — Polymarket US Paper Trading Engine ($100 Starting Bankroll).

Monitors Polymarket US production API (api.polymarket.us), scans active markets across
crypto and event categories, evaluates high-probability favorite pricing (92c-99c),
validates order parameters with real orders.preview calls, simulates paper fills,
and tracks portfolio mark-to-market equity and venue settlements.

Usage:
    python poly_us_trader.py --once
    python poly_us_trader.py --interval 15
    python poly_us_trader.py --status
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import polymarket_us_client
import crossvenue_edge
from resilience import systemd_notify

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("poly_us_dry.log", mode="a"),
    ],
)
log = logging.getLogger("poly_us_trader")

STATE_FILE = Path("local_data/poly_us_state.json")
LEDGER_FILE = Path("local_data/poly_us_paper_ledger.jsonl")


class PolyUSPaperTrader:
    def __init__(self, initial_cash: float = 100.0):
        self.state_file = STATE_FILE
        self.ledger_file = LEDGER_FILE
        self.state = self._load_state(initial_cash)
        self.client = polymarket_us_client._get_client()
        self.auth_client = None
        try:
            self.auth_client = polymarket_us_client._get_authenticated_client()
        except Exception as e:
            log.warning("Polymarket US authenticated client not available: %s", e)

    def _load_state(self, initial_cash: float) -> dict:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                if "cash" in data and "positions" in data:
                    return data
            except Exception as e:
                log.warning("Failed to load paper state, starting fresh: %s", e)
        state = {
            "start_balance": initial_cash,
            "cash": initial_cash,
            "positions": [],
            "realized_pnl": 0.0,
            "trades_count": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state(state)
        return state

    def _save_state(self, state: dict):
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write to prevent file corruption
        tmp_file = self.state_file.with_name(f"{self.state_file.name}.{os.getpid()}.tmp")
        tmp_file.write_text(json.dumps(state, indent=2))
        os.replace(tmp_file, self.state_file)

    def _append_ledger(self, record: dict):
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_file, "a") as f:
            f.write(json.dumps(record) + "\n")

    def scan_markets(self) -> list:
        """Scan open active markets across crypto and active categories."""
        if not self.client:
            return []
        markets_by_slug = {}
        # 1. Fetch active crypto price markets
        try:
            res_crypto = self.client.get("/v1/markets?active=true&closed=false&categories=crypto&limit=100", authenticated=False)
            for m in res_crypto.get("markets", []):
                slug = m.get("slug")
                if slug:
                    markets_by_slug[slug] = m
        except Exception as e:
            log.debug("Failed crypto scan: %s", e)

        # 2. Fetch general active markets
        try:
            res_gen = self.client.get("/v1/markets?active=true&closed=false&limit=100", authenticated=False)
            for m in res_gen.get("markets", []):
                slug = m.get("slug")
                if slug:
                    markets_by_slug[slug] = m
        except Exception as e:
            log.debug("Failed general scan: %s", e)

        return list(markets_by_slug.values())

    def preview_order(self, slug: str, side: str, price_dollars: float, qty: int) -> bool:
        """Validate order against Polymarket US production order preview API."""
        if not self.auth_client:
            return True
        intent = "ORDER_INTENT_BUY_LONG" if side == "yes" else "ORDER_INTENT_BUY_SHORT"
        params = {
            "marketSlug": slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price_dollars:.2f}", "currency": "USD"},
            "quantity": qty,
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "participateDontInitiate": True,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        try:
            self.auth_client.orders.preview({"request": params})
            return True
        except Exception as e:
            log.debug("Order preview failed for %s %s @ $%.2f: %s", slug, side, price_dollars, e)
            return False

    def check_settlements(self, market_map: dict):
        """Check settlement status for closed paper positions."""
        remaining_positions = []
        now = datetime.now(timezone.utc)
        for pos in self.state.get("positions", []):
            slug = pos["slug"]
            m_info = market_map.get(slug)
            # Only poll settlement endpoint if market is known closed or not found in active list
            is_closed = m_info.get("closed", False) if m_info else False
            if not is_closed and m_info is not None:
                remaining_positions.append(pos)
                continue

            try:
                settle_res = self.client.get(f"/v1/markets/{slug}/settlement", authenticated=False)
                settlement = settle_res.get("settlement")
                if settlement is not None:
                    payout_per_contract = float(settlement) if pos["side"] == "yes" else (1.0 - float(settlement))
                    total_payout = round(payout_per_contract * pos["qty"], 4)
                    cost = pos["cost"]
                    pnl = round(total_payout - cost, 4)
                    self.state["cash"] = round(self.state["cash"] + total_payout, 4)
                    self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 4)
                    log.info(
                        "🏆 SETTLED %s %s: outcome=%.2f payout=$%.2f cost=$%.2f PnL=%+$.2f | Cash=$%.2f",
                        slug, pos["side"].upper(), payout_per_contract, total_payout, cost, pnl, self.state["cash"]
                    )
                    self._append_ledger({
                        "event": "settlement",
                        "slug": slug,
                        "side": pos["side"],
                        "qty": pos["qty"],
                        "entry_price": pos["price"],
                        "payout": total_payout,
                        "pnl": pnl,
                        "cash_after": self.state["cash"],
                        "timestamp": now.isoformat(),
                    })
                    continue
            except Exception as e:
                log.debug("Error checking settlement for %s: %s", slug, e)
            remaining_positions.append(pos)
        self.state["positions"] = remaining_positions
        self._save_state(self.state)

    def evaluate_opportunity(self, market: dict) -> dict | None:
        """Find high-probability favourite opportunities (92c-99c) directly from market data."""
        slug = market.get("slug")
        if not slug:
            return None

        # 1. Evaluate cross-venue spot price & distributional fair value edge
        cv_edge = crossvenue_edge.evaluate_crossvenue_edge(market)
        if cv_edge and cv_edge["best_edge_cents"] >= 0.5 and 0.92 <= cv_edge["best_price"] <= 0.99:
            return {
                "slug": slug,
                "question": market.get("question", slug),
                "side": cv_edge["best_side"],
                "price": cv_edge["best_price"],
                "category": "crypto",
                "cross_edge_cents": cv_edge["best_edge_cents"],
                "ref_prob": cv_edge["ref_prob"],
                "spot": cv_edge["spot"],
                "strike": cv_edge["strike"],
            }

        outcome_raw = market.get("outcomePrices")
        if not outcome_raw:
            return None

        prices = []
        if isinstance(outcome_raw, str):
            try:
                prices = json.loads(outcome_raw)
            except Exception:
                return None
        elif isinstance(outcome_raw, list):
            prices = outcome_raw

        if len(prices) == 1:
            try:
                long_p = float(prices[0])
                short_p = round(1.0 - long_p, 4)
            except (ValueError, TypeError):
                return None
        elif len(prices) >= 2:
            try:
                long_p = float(prices[0])
                short_p = float(prices[1])
            except (ValueError, TypeError):
                return None
        else:
            return None

        candidates = []
        # Check Long (Yes) favorite
        if 0.92 <= long_p <= 0.99:
            candidates.append(("yes", long_p))

        # Check Short (No) favorite
        if 0.92 <= short_p <= 0.99:
            candidates.append(("no", short_p))

        if not candidates:
            return None

        side, price = candidates[0]
        return {
            "slug": slug,
            "question": market.get("question", slug),
            "side": side,
            "price": price,
            "category": market.get("category", ""),
        }

    @staticmethod
    def compute_tier_size(cash: float) -> float:
        """Dynamic Kelly position sizing across compounding milestones ($100 -> $1M)."""
        if cash < 100.0:
            return max(1.0, round(cash * 0.05, 2))
        elif cash < 1_000.0:
            return min(50.0, round(cash * 0.05, 2))
        elif cash < 10_000.0:
            return min(400.0, round(cash * 0.04, 2))
        elif cash < 100_000.0:
            return min(3000.0, round(cash * 0.03, 2))
        else:
            return min(20000.0, round(cash * 0.02, 2))

    def execute_paper_trade(self, opp: dict) -> bool:
        """Size and execute a paper trade from the bankroll."""
        slug = opp["slug"]
        side = opp["side"]
        price = opp["price"]

        # Check if already in this market
        if any(p["slug"] == slug for p in self.state["positions"]):
            return False

        # Cap max concurrent open positions (default 10)
        if len(self.state["positions"]) >= getattr(self, "max_open_positions", 10):
            return False

        # Enforce 30% cash reserve floor
        total_equity = self.state["cash"] + sum(p.get("cost", 0.0) for p in self.state["positions"])
        min_cash_floor = total_equity * 0.30
        if self.state["cash"] <= min_cash_floor:
            return False

        # Sizing: Dynamic Kelly scaling across milestones ($100 -> $1M)
        available_cash = max(0.0, self.state["cash"] - min_cash_floor)
        max_trade_dollars = self.compute_tier_size(self.state["cash"])
        max_trade_dollars = min(max_trade_dollars, available_cash)
        if max_trade_dollars < 1.00:
            return False

        qty = int(max_trade_dollars / price)
        if qty < 1:
            return False

        cost = round(qty * price, 4)
        if cost > available_cash:
            return False

        # Validate order syntax with Polymarket US production API
        valid = self.preview_order(slug, side, price, qty)
        if not valid:
            return False

        # Execute simulated fill
        self.state["cash"] = round(self.state["cash"] - cost, 4)
        self.state["trades_count"] += 1
        pos = {
            "slug": slug,
            "question": opp["question"],
            "side": side,
            "price": price,
            "qty": qty,
            "cost": cost,
            "category": opp.get("category", ""),
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state["positions"].append(pos)
        self._save_state(self.state)

        edge_tag = f" [CrossEdge: {opp['cross_edge_cents']:+.1f}c]" if "cross_edge_cents" in opp else ""
        log.info(
            "⚡ PAPER BUY [%s] %s | %s %dx @ $%.2f = $%.2f%s | Cash Remaining: $%.2f",
            opp.get("category", "gen"), slug, side.upper(), qty, price, cost, edge_tag, self.state["cash"]
        )
        self._append_ledger({
            "event": "open",
            "slug": slug,
            "side": side,
            "qty": qty,
            "price": price,
            "cost": cost,
            "cash_after": self.state["cash"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return True

    def cycle(self):
        """Run one full cycle."""
        markets = self.scan_markets()
        market_map = {m["slug"]: m for m in markets if "slug" in m}
        self.check_settlements(market_map)

        # Compute mark-to-market valuation and unrealized P&L per position
        total_mtm = 0.0
        total_unrealized = 0.0
        for pos in self.state.get("positions", []):
            slug = pos["slug"]
            m_info = market_map.get(slug)
            cur_price = pos["price"]
            if m_info:
                outcome_raw = m_info.get("outcomePrices") or []
                if isinstance(outcome_raw, str):
                    try:
                        outcome_raw = json.loads(outcome_raw)
                    except Exception:
                        outcome_raw = []
                if len(outcome_raw) == 1:
                    try:
                        long_p = float(outcome_raw[0])
                        cur_price = long_p if pos["side"] == "yes" else round(1.0 - long_p, 4)
                    except Exception:
                        pass
                elif len(outcome_raw) >= 2:
                    try:
                        cur_price = float(outcome_raw[0]) if pos["side"] == "yes" else float(outcome_raw[1])
                    except Exception:
                        pass
            pos_mtm = round(cur_price * pos["qty"], 4)
            pos_unrealized = round(pos_mtm - pos["cost"], 4)
            pos["current_price"] = cur_price
            pos["mtm_value"] = pos_mtm
            pos["unrealized_pnl"] = pos_unrealized
            total_mtm += pos_mtm
            total_unrealized += pos_unrealized

        realized_pnl = float(self.state.get("realized_pnl", 0.0))
        total_pnl = round(realized_pnl + total_unrealized, 4)
        total_equity = round(self.state["cash"] + total_mtm, 2)
        self.state["unrealized_pnl"] = round(total_unrealized, 4)
        self.state["total_pnl"] = total_pnl

        # Track compounding milestones ($100, $1K, $10K, $100K, $1M)
        milestones = [100.0, 1000.0, 10000.0, 100000.0, 1000000.0]
        reached = self.state.setdefault("milestones_reached", [])
        for m in milestones:
            if total_equity >= m and m not in reached:
                reached.append(m)
                log.info("🎉 MILESTONE REACHED: Total Equity $%.2f crossed $%.2f milestone!", total_equity, m)
                self._append_ledger({
                    "event": "milestone",
                    "milestone": m,
                    "total_equity": total_equity,
                    "cash": self.state["cash"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

        self._save_state(self.state)

        log.info(
            "── PolyUS Cycle ── Equity: $%.2f (Cash: $%.2f, MTM: $%.2f) | Realized P&L: %+.2f | Unrealized P&L: %+.2f | Total P&L: %+.2f | Positions: %d | Markets: %d",
            total_equity, self.state["cash"], total_mtm, realized_pnl, total_unrealized, total_pnl, len(self.state["positions"]), len(markets)
        )

        opportunities = []
        for m in markets:
            opp = self.evaluate_opportunity(m)
            if opp:
                opportunities.append(opp)

        current_slugs = {p["slug"] for p in self.state.get("positions", [])}
        new_opps = [opp for opp in opportunities if opp["slug"] not in current_slugs]
        log.info(
            "Found %d high-certainty (92-99c) opportunity brackets (%d unheld)",
            len(opportunities), len(new_opps)
        )
        trades_placed = 0
        for opp in new_opps:
            if trades_placed >= 2:
                break
            if self.execute_paper_trade(opp):
                trades_placed += 1

        systemd_notify("WATCHDOG=1")


def main():
    parser = argparse.ArgumentParser(description="Polymarket US Paper Trader ($100 Bankroll)")
    parser.add_argument("--once", action="store_true", help="Run single cycle and exit")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between cycles (default 15)")
    parser.add_argument("--status", action="store_true", help="Show current paper state and exit")
    args = parser.parse_args()

    trader = PolyUSPaperTrader(initial_cash=100.0)

    if args.status:
        state = trader.state
        positions = state.get("positions", [])
        total_mtm = sum(p.get("mtm_value", p.get("cost", 0.0)) for p in positions)
        total_equity = round(state.get("cash", 100.0) + total_mtm, 2)
        realized = state.get("realized_pnl", 0.0)
        unrealized = state.get("unrealized_pnl", 0.0)
        total_pnl = state.get("total_pnl", round(realized + unrealized, 4))
        print(f"=== Polymarket US Paper Trader ===")
        print(f"Start Balance:  ${state.get('start_balance', 100.0):.2f}")
        print(f"Total Equity:   ${total_equity:.2f}")
        print(f"Available Cash: ${state.get('cash', 100.0):.2f}")
        print(f"Open Positions: {len(positions)} (${total_mtm:.2f} MTM)")
        print(f"Realized P&L:   ${realized:+.2f}")
        print(f"Unrealized P&L: ${unrealized:+.2f}")
        print(f"Total Net P&L:  ${total_pnl:+.2f}")
        print(f"Trades Count:   {state.get('trades_count', 0)}")
        print(f"Milestones Reached: {state.get('milestones_reached', [100.0])}")
        for p in positions:
            cat = f"[{p['category']}] " if p.get("category") else ""
            cur_p = p.get("current_price", p["price"])
            u_pnl = p.get("unrealized_pnl", 0.0)
            print(f"  - {cat}{p['slug']} | {p['side'].upper()} {p['qty']}x @ ${p['price']:.2f} -> Cur: ${cur_p:.2f} (cost: ${p['cost']:.2f}, MTM: ${p.get('mtm_value', p['cost']):.2f}, Unrealized: ${u_pnl:+.2f})")
        return

    log.info("Starting Polymarket US Paper Trader with $%.2f Bankroll", trader.state["start_balance"])
    if args.once:
        trader.cycle()
        return

    while True:
        try:
            trader.cycle()
        except KeyboardInterrupt:
            log.info("Polymarket US paper trader stopped by user.")
            break
        except Exception as e:
            log.error("Cycle error: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
