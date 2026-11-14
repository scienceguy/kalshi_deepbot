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
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make scripts/ importable for bs_calibration (Task 2: Brier calibration + vol scaling).
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from bs_calibration import RollingCalibrationMonitor, AdaptiveVolatilityScaler

import polymarket_us_client
import crossvenue_edge
import config
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
    def __init__(self, initial_cash: float = 35.0, reset: bool = False, live_mode: bool = False):
        self.state_file = STATE_FILE
        self.ledger_file = LEDGER_FILE
        self.max_open_positions = 25
        self.live_mode = live_mode
        if reset:
            self.state = self.reset_state(initial_cash)
        else:
            self.state = self._load_state(initial_cash)
        self.client = polymarket_us_client._get_client()
        self.auth_client = None
        try:
            self.auth_client = polymarket_us_client._get_authenticated_client()
        except Exception as e:
            log.warning("Polymarket US authenticated client not available: %s", e)

        # Wire in gateway health check at startup
        if polymarket_us_client.health_check():
            log.info("Polymarket US gateway health check: OK")
        else:
            log.warning("Polymarket US gateway health check: FAILED or unreachable")

        # Task 2: continuous Brier calibration + adaptive volatility scaling.
        self.cal_monitor = RollingCalibrationMonitor(
            ledger_path="local_data/bs_calibration_ledger.jsonl")
        self.vol_scaler = AdaptiveVolatilityScaler(
            base_sigma=float(getattr(config, "MODEL_VOLATILITY", 0.80)),
            base_min_z=float(getattr(config, "GATES_MIN_CUSHION_Z", 1.80)),
        )
        self.adjusted_sigma = self.vol_scaler.base_sigma
        self.adjusted_min_z = self.vol_scaler.base_min_z
        self.calibration_halted = False
        self.calibration_reason = "not started"

    def reset_state(self, initial_cash: float = 35.0) -> dict:
        """Reset paper bankroll to starting balance with clean position ledger."""
        state = {
            "start_balance": initial_cash,
            "cash": initial_cash,
            "positions": [],
            "realized_pnl": 0.0,
            "trades_count": 0,
            "milestones_reached": [float(initial_cash)],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state(state)
        self.state = state
        self._append_ledger({
            "event": "reset",
            "start_balance": initial_cash,
            "cash": initial_cash,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        log.info("Reset Polymarket US paper state with initial bankroll $%.2f", initial_cash)
        return state

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

        # 3. Fetch active fast-expiring crypto threshold & up/down markets via events
        for symbol in ("BTC", "ETH", "SOL"):
            try:
                events = polymarket_us_client.search_events(symbol)
                for ev in events[:4]:  # Top 4 events per symbol
                    ev_slug = ev.get("slug")
                    if not ev_slug or "updown-5m" in ev_slug or "-5m-" in ev_slug:
                        continue
                    ev_full = polymarket_us_client.get_event_by_slug(ev_slug)
                    for m in ev_full.get("event", {}).get("markets", []):
                        m_slug = m.get("slug")
                        if m_slug and not m.get("closed", False):
                            markets_by_slug[m_slug] = m
            except Exception as e:
                log.debug("Failed %s event search: %s", symbol, e)

        return list(markets_by_slug.values())

    def preview_order(self, slug: str, side: str, price_dollars: float, qty: int) -> bool:
        """Validate order against Polymarket US production order preview API."""
        if not self.auth_client:
            return True
        try:
            res = polymarket_us_client.preview_order(slug, side, price_dollars, qty)
            return res is not None
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

            # Check if market has closed, resolved, or passed its windowEnd/endDate
            is_closed = m_info.get("closed", False) if m_info else False
            if m_info and not is_closed:
                status = str(m_info.get("status") or "").upper()
                if status in ("RESOLVED", "RESOLVING", "CLOSED"):
                    is_closed = True
                else:
                    terms = m_info.get("assetPriceTerms") or {}
                    end_date_str = terms.get("windowEnd") or m_info.get("endDate") or m_info.get("gameStartTime")
                    if end_date_str:
                        try:
                            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            if now >= end_dt:
                                is_closed = True
                        except Exception:
                            pass

            # If market is still actively trading and not past end date, keep holding
            if not is_closed and m_info is not None:
                remaining_positions.append(pos)
                continue

            try:
                time.sleep(0.05)
                settle_res = polymarket_us_client.get_market_settlement(slug)
                if settle_res is not None and "settlement" in settle_res:
                    settlement = settle_res["settlement"]
                    payout_per_contract = float(settlement) if pos["side"] == "yes" else (1.0 - float(settlement))
                    total_payout = round(payout_per_contract * pos["qty"], 4)
                    cost = pos["cost"]
                    pnl = round(total_payout - cost, 4)
                    # Task 2: reconcile settlement for rolling Brier calibration.
                    outcome = 1 if float(settlement) >= 0.5 else 0  # 1 = Yes won
                    try:
                        self.cal_monitor.reconcile_settlement(slug, outcome)
                    except Exception:
                        pass
                    self.state["cash"] = round(self.state["cash"] + total_payout, 4)
                    self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 4)
                    log.info(
                        "🏆 SETTLED %s %s: outcome=%.2f payout=$%.2f cost=$%.2f PnL=$%+.2f | Cash=$%.2f",
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

        terms = market.get("assetPriceTerms") or {}
        end_date = terms.get("windowEnd") or market.get("endDate") or market.get("gameStartTime") or ""
        hours_left = None
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
            except Exception:
                pass

        # Only trade markets expiring within max_hours_to_expiry (default 24h) and not already expired
        max_h = getattr(self, "max_hours_to_expiry", 24.0)
        if hours_left is not None:
            if hours_left <= 0.0 or hours_left > max_h:
                return None

        # Filter out 5-minute micro-noise contracts (focus on 15m, hourly, and daily markets with strike cushion)
        if "updown-5m" in slug or "-5m-" in slug:
            return None

        # 1. Evaluate cross-venue spot price & distributional fair value edge
        cv_edge = crossvenue_edge.evaluate_crossvenue_edge(market)
        # High-EV favorite hurdle: ref_prob >= MODEL_PROB_MIN (0.96) and favorite band (92c-99c)
        if (cv_edge
                and cv_edge["best_edge_cents"] >= 0.5
                and cv_edge["ref_prob"] >= getattr(config, "MODEL_PROB_MIN", 0.96)
                and 0.92 <= cv_edge["best_price"] <= 0.99):
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
                "end_date": end_date,
                "hours_left": hours_left,
            }

        long_p = None
        short_p = None

        # Check marketSides first for unambiguous side assignment
        market_sides = market.get("marketSides") or []
        for side_info in market_sides:
            p_val = side_info.get("price")
            if p_val is not None:
                try:
                    price_float = float(p_val)
                    is_long = side_info.get("long") is True or str(side_info.get("description")).lower() in ("yes", "long")
                    if is_long:
                        long_p = price_float
                        if short_p is None:
                            short_p = round(1.0 - long_p, 4)
                    else:
                        short_p = price_float
                        if long_p is None:
                            long_p = round(1.0 - short_p, 4)
                except (ValueError, TypeError):
                    pass

        # Fall back to outcomePrices if marketSides gave no quote
        if long_p is None or short_p is None:
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
                    p0 = float(prices[0])
                    long_p = p0
                    short_p = round(1.0 - p0, 4)
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
        # Check Long (Yes) favorite in high-certainty favorite band (92c-99c)
        if 0.92 <= long_p <= 0.99:
            candidates.append(("yes", long_p))

        # Check Short (No) favorite in high-certainty favorite band (92c-99c)
        if 0.92 <= short_p <= 0.99:
            candidates.append(("no", short_p))

        if not candidates:
            return None

        side, price = candidates[0]

        # Enforce directional strike cushion hurdle: spot must be safely away from strike (>2.0% distance)
        strike_val = None
        strike_dict = terms.get("strike") or terms.get("priceToBeat")
        if strike_dict and isinstance(strike_dict, dict):
            try:
                strike_val = float(strike_dict.get("value", 0))
            except Exception:
                pass

        symbol = (terms.get("asset") or {}).get("symbol", "").upper()
        if not symbol:
            slug_lower = slug.lower()
            if "btc" in slug_lower:
                symbol = "BTC"
            elif "eth" in slug_lower:
                symbol = "ETH"
            elif "sol" in slug_lower:
                symbol = "SOL"

        spot = crossvenue_edge.get_cached_spot(symbol) if symbol else None
        if strike_val and spot and spot > 0:
            pct_distance = abs(spot - strike_val) / spot
            is_above_below = "above" in slug.lower() or terms.get("marketType") == "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW"
            if is_above_below:
                # If betting NO on "BTC above K", spot must be at least 2.0% BELOW strike
                if side == "no" and (spot >= strike_val or pct_distance < 0.020):
                    return None
                # If betting YES on "BTC above K", spot must be at least 2.0% ABOVE strike
                if side == "yes" and (spot <= strike_val or pct_distance < 0.020):
                    return None

        # Task 2: adaptive strike-cushion hurdle (vol-scaled z).
        # z = |ln(S/K)| / (sigma_per_sqrt_min * sqrt(minutes_left)); require z >= adjusted_min_z.
        # When Brier drifts up (calibration degrading), vol_scaler raises sigma and
        # min_z, so fewer entries clear the hurdle -> more conservative. When Brier
        # improves, sigma falls and the hurdle narrows.
        if strike_val and spot and spot > 0 and hours_left is not None:
            minutes_left = hours_left * 60.0
            if minutes_left > 0:
                sigma_per_sqrt_min = self.adjusted_sigma / math.sqrt(525600.0)
                denom = sigma_per_sqrt_min * math.sqrt(minutes_left)
                if denom > 0:
                    z = abs(math.log(spot / strike_val)) / denom
                    if z < self.adjusted_min_z:
                        log.debug("Cushion z %.2f < min_z %.2f for %s — denying entry",
                                  z, self.adjusted_min_z, slug)
                        return None

        return {
            "slug": slug,
            "question": market.get("question", slug),
            "side": side,
            "price": price,
            "category": market.get("category", ""),
            "spot": spot,
            "strike": strike_val,
            "end_date": end_date,
            "hours_left": hours_left,
        }

    @staticmethod
    def compute_tier_size(total_balance: float) -> float:
        """Dynamic position sizing: 3% of total equity per window, re-anchored at each milestone.

        Implements the compounding ladder: per-window budget = 3% of total equity,
        which scales 100x at every milestone ($35->$100: $1.05; $100->$1K: $3.00;
        $1K->$10K: $30.00; $10K->$100K: $300.00; $100K->$1M: $3,000.00). The number
        of contracts is floor(budget / entry_price). No per-tier caps: the 3% rule is
        applied uniformly so milestone 4 sizes at $3,000 (not $2,000).
        """
        return round(total_balance * 0.03, 2)

    def execute_paper_trade(self, opp: dict) -> bool:
        """Size and execute a paper trade from the bankroll."""
        slug = opp["slug"]
        side = opp["side"]
        price = opp["price"]

        # Check if already in this market
        if any(p["slug"] == slug for p in self.state["positions"]):
            return False

        # Cap max concurrent open positions (default 25)
        if len(self.state["positions"]) >= getattr(self, "max_open_positions", 25):
            return False

        # Enforce 30% cash reserve floor
        total_mtm = sum(p.get("mtm_value", p.get("cost", 0.0)) for p in self.state["positions"])
        total_balance = round(self.state["cash"] + total_mtm, 2)
        min_cash_floor = total_balance * 0.30
        if self.state["cash"] <= min_cash_floor:
            return False

        # Enforce 3% max per window budget
        window_budget = self.state.get("window_budget", round(total_balance * 0.03, 2))
        window_spent = self.state.get("window_spent", 0.0)
        available_window_budget = max(0.0, window_budget - window_spent)
        if available_window_budget < 0.90:
            log.debug("Window budget exhausted ($%.2f / $%.2f spent)", window_spent, window_budget)
            return False

        # Sizing: strictly limited to remaining window budget and available cash
        available_cash = max(0.0, self.state["cash"] - min_cash_floor)
        max_trade_dollars = self.compute_tier_size(total_balance)
        max_trade_dollars = min(max_trade_dollars, available_window_budget, available_cash, round(total_balance * 0.03, 2))
        if max_trade_dollars < 0.90:  # Allow 1 contract at 92-99c on starting $35 bankrolls
            return False

        qty = int(max_trade_dollars / price)
        if qty < 1:
            return False

        cost = round(qty * price, 4)
        if cost > available_window_budget or cost > available_cash:
            return False

        # Validate order syntax with Polymarket US production API
        valid = self.preview_order(slug, side, price, qty)
        if not valid:
            return False

        order_id = None
        if getattr(self, "live_mode", False):
            resp = polymarket_us_client.place_maker_order(slug, side, price, qty)
            if not resp or not resp.get("id"):
                log.warning("Live order placement failed for %s %s @ %.2f", slug, side, price)
                return False
            order_id = resp.get("id")
            log.info("Live maker order placed: %s (id=%s)", slug, order_id)

        # Execute simulated or live fill
        self.state["cash"] = round(self.state["cash"] - cost, 4)
        self.state["window_spent"] = round(window_spent + cost, 4)
        self.state["trades_count"] += 1
        pos = {
            "slug": slug,
            "question": opp["question"],
            "side": side,
            "price": price,
            "qty": qty,
            "cost": cost,
            "order_id": order_id,
            "category": opp.get("category", ""),
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state["positions"].append(pos)
        # Task 2: record entry for rolling Brier calibration (model p, z, price band).
        try:
            minutes_left = opp.get("hours_left")
            minutes_left = minutes_left * 60.0 if minutes_left is not None else None
            sigma_per_sqrt_min = self.adjusted_sigma / math.sqrt(525600.0)
            denom = sigma_per_sqrt_min * math.sqrt(minutes_left) if minutes_left else 0.0
            spot = opp.get("spot")
            strike = opp.get("strike")
            z = (abs(math.log(spot / strike)) / denom
                 if (spot and strike and denom > 0) else None)
            self.cal_monitor.record_entry(
                slug=slug,
                side=side,
                price_dollars=price,
                predicted_p=opp.get("ref_prob"),
                z=z,
                strike=strike,
                minutes_left=minutes_left,
                sigma=self.adjusted_sigma,
            )
        except Exception as e:
            log.debug("Calibration record_entry failed for %s: %s", slug, e)
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

        # Ensure market data is available for all open positions efficiently by batching through event parents
        missing_slugs = [p["slug"] for p in self.state.get("positions", []) if p["slug"] not in market_map]
        if missing_slugs:
            events_to_fetch = set()
            for s in missing_slugs:
                parts = s.split("-")
                # cpc-btc-above-hr-2026-11-14-1400z-66400 -> btc-above-hr-2026-11-14-1400z
                if len(parts) >= 7 and parts[0].startswith("cpc"):
                    ev_slug = "-".join(parts[1:-1])
                    events_to_fetch.add(ev_slug)
            for ev_slug in events_to_fetch:
                try:
                    ev_full = polymarket_us_client.get_event_by_slug(ev_slug)
                    for m in ev_full.get("event", {}).get("markets", []):
                        m_slug = m.get("slug")
                        if m_slug:
                            market_map[m_slug] = m
                except Exception:
                    pass
            # For any still missing, fetch individually with spacing
            for s in missing_slugs:
                if s not in market_map:
                    try:
                        time.sleep(0.05)
                        fresh_m = polymarket_us_client.get_market_by_slug(s)
                        if fresh_m and isinstance(fresh_m, dict):
                            m_data = fresh_m.get("market") or fresh_m
                            if m_data and "slug" in m_data:
                                market_map[s] = m_data
                    except Exception:
                        pass

        self.check_settlements(market_map)

        # In paper mode, rotate out any stale long-dated positions (>24h) to free cash for active intraday compounding
        now = datetime.now(timezone.utc)
        active_positions = []
        for pos in self.state.get("positions", []):
            slug = pos["slug"]
            m_info = market_map.get(slug)
            end_date_str = (m_info.get("endDate") or m_info.get("gameStartTime")) if m_info else None
            is_stale_long_dated = False
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    h_left = (end_dt - now).total_seconds() / 3600.0
                    if h_left > getattr(self, "max_hours_to_expiry", 24.0):
                        is_stale_long_dated = True
                except Exception:
                    pass
            if is_stale_long_dated:
                exit_price = pos.get("current_price", pos["price"])
                payout = round(exit_price * pos["qty"], 4)
                pnl = round(payout - pos["cost"], 4)
                self.state["cash"] = round(self.state["cash"] + payout, 4)
                self.state["realized_pnl"] = round(self.state["realized_pnl"] + pnl, 4)
                log.info(
                    "🔄 ROTATED long-dated position %s (>24h) | Payout=$%.2f PnL=$%+.2f -> Freed cash=$%.2f",
                    slug, payout, pnl, self.state["cash"]
                )
                self._append_ledger({
                    "event": "rotate_exit",
                    "slug": slug,
                    "side": pos["side"],
                    "qty": pos["qty"],
                    "exit_price": exit_price,
                    "payout": payout,
                    "pnl": pnl,
                    "cash_after": self.state["cash"],
                    "timestamp": now.isoformat(),
                })
            else:
                active_positions.append(pos)
        self.state["positions"] = active_positions

        # Compute mark-to-market valuation and unrealized P&L per position
        total_mtm = 0.0
        total_unrealized = 0.0
        for pos in self.state.get("positions", []):
            slug = pos["slug"]
            m_info = market_map.get(slug)
            cur_price = pos["price"]
            if m_info:
                # Check marketSides first for unambiguous side quote
                market_sides = m_info.get("marketSides") or []
                found_quote = False
                for s in market_sides:
                    p_val = s.get("price")
                    if p_val is not None:
                        try:
                            px = float(p_val)
                            is_long = s.get("long") is True or str(s.get("description")).lower() in ("yes", "long")
                            if is_long:
                                cur_price = px if pos["side"] == "yes" else round(1.0 - px, 4)
                                found_quote = True
                                break
                            else:
                                cur_price = px if pos["side"] == "no" else round(1.0 - px, 4)
                                found_quote = True
                                break
                        except Exception:
                            pass
                if not found_quote:
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

        # Auto-reset rule: if post-settlement equity drops to <= 34.95, reset bankroll to $35.00 while preserving history
        if total_equity <= 34.95:
            prior_eq = total_equity
            log.info("Post-settlement equity $%.2f <= $34.95 — resetting bankroll to $35.00 while preserving ledger history", prior_eq)
            self.state["cash"] = 35.0
            self.state["start_balance"] = 35.0
            self.state["positions"] = []
            self.state["realized_pnl"] = 0.0
            self.state["unrealized_pnl"] = 0.0
            self.state["total_pnl"] = 0.0
            self.state["window_budget"] = 1.05
            self.state["window_spent"] = 0.0
            total_equity = 35.0
            total_mtm = 0.0
            total_unrealized = 0.0
            realized_pnl = 0.0
            total_pnl = 0.0
            self._save_state(self.state)
            self._append_ledger({
                "event": "auto_reset_threshold",
                "prior_equity": prior_eq,
                "threshold": 34.95,
                "cash": 35.0,
                "timestamp": now.isoformat(),
            })

        self.state["unrealized_pnl"] = round(total_unrealized, 4)
        self.state["total_pnl"] = total_pnl

        # Track compounding milestones ($35, $100, $1K, $10K, $100K, $1M)
        milestones = [35.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0]
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

        # Check 15-minute window transition (900 seconds)
        window_interval = getattr(self, "window_seconds", 900)
        current_window_ts = int(time.time() // window_interval) * window_interval
        window_dt = datetime.fromtimestamp(current_window_ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if self.state.get("current_window_ts") != current_window_ts:
            self.state["current_window_ts"] = current_window_ts
            self.state["current_window_name"] = window_dt
            self.state["window_budget"] = round(total_equity * 0.03, 2)
            self.state["window_spent"] = 0.0
            log.info(
                "── New Window [%s] ── Total Equity: $%.2f | Window Budget (3%%): $%.2f | Spent: $0.00",
                window_dt, total_equity, self.state["window_budget"]
            )

        self._save_state(self.state)

        # ── Task 2: continuous calibration monitor + circuit breaker ──
        snap = self.cal_monitor.snapshot_rolling()
        scale, self.adjusted_sigma, self.adjusted_min_z, drift = self.vol_scaler.scale(
            snap.get("rolling_brier"))
        halted, reason = self.cal_monitor.circuit_breaker()
        self.calibration_halted = halted
        self.calibration_reason = reason
        band_gap = None
        if snap.get("price_band_92_99c"):
            band_gap = snap["price_band_92_99c"].get("calibration_gap_pp")
        log.info(
            "CALIBRATION rolling_brier=%s ece=%s trailing10_wr=%s "
            "band92_99_gap_pp=%s vol_scale=%s adj_sigma=%.4f adj_min_z=%.2f drift=%s -> %s",
            snap.get("rolling_brier"), snap.get("ece"),
            snap.get("trailing_10_win_rate"), band_gap,
            scale, self.adjusted_sigma, self.adjusted_min_z, drift, reason)

        opportunities = []
        for m in markets:
            opp = self.evaluate_opportunity(m)
            if opp:
                opportunities.append(opp)

        self.state["available_markets_count"] = len(markets)
        self.state["tradeable_brackets_count"] = len(opportunities)
        self._save_state(self.state)

        log.info(
            "── PolyUS Cycle ── Equity: $%.2f (Cash: $%.2f, MTM: $%.2f) | Realized P&L: %+.2f | Unrealized P&L: %+.2f | Total P&L: %+.2f | Window Budget: $%.2f ($%.2f spent) | Positions: %d | Available Markets: %d | Tradable Brackets: %d",
            total_equity, self.state["cash"], total_mtm, realized_pnl, total_unrealized, total_pnl,
            self.state.get("window_budget", round(total_equity * 0.03, 2)),
            self.state.get("window_spent", 0.0),
            len(self.state["positions"]), len(markets), len(opportunities)
        )

        current_slugs = {p["slug"] for p in self.state.get("positions", [])}
        new_opps = [opp for opp in opportunities if opp["slug"] not in current_slugs]

        # Prioritize opportunities closing soonest so settlements compound in real-time
        now_ts = time.time()

        def _opp_sort_key(opp):
            ed = opp.get("end_date") or ""
            if not ed:
                return float("inf")
            try:
                dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                ts = dt.timestamp()
                # Deprioritize already-expired markets so active markets trade first
                if ts <= now_ts:
                    return float("inf")
                return ts
            except Exception:
                return float("inf")

        new_opps.sort(key=_opp_sort_key)
        log.info(
            "Found %d high-certainty (92-99c) opportunity brackets (%d unheld)",
            len(opportunities), len(new_opps)
        )
        trades_placed = 0
        if self.calibration_halted:
            log.info("CALIBRATION HALTED: %s — no new entries this cycle", self.calibration_reason)
        else:
            for opp in new_opps:
                if trades_placed >= 2:
                    break
                if self.execute_paper_trade(opp):
                    trades_placed += 1

        systemd_notify("WATCHDOG=1")


def main():
    parser = argparse.ArgumentParser(description="Polymarket US Paper Trader ($35 Bankroll)")
    parser.add_argument("--once", action="store_true", help="Run single cycle and exit")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between cycles (default 15)")
    parser.add_argument("--status", action="store_true", help="Show current paper state and exit")
    parser.add_argument("--reset", nargs="?", const=35.0, type=float, default=None, help="Reset paper bankroll to starting balance (default: 35.0)")
    parser.add_argument("--live", action="store_true", help="Enable live trading mode (places real maker orders via Polymarket US API)")
    args = parser.parse_args()

    init_cash = args.reset if args.reset is not None else 35.0
    trader = PolyUSPaperTrader(initial_cash=init_cash, reset=args.reset is not None, live_mode=args.live)

    if args.status:
        state = trader.state
        positions = state.get("positions", [])
        total_mtm = sum(p.get("mtm_value", p.get("cost", 0.0)) for p in positions)
        total_equity = round(state.get("cash", init_cash) + total_mtm, 2)
        realized = state.get("realized_pnl", 0.0)
        unrealized = state.get("unrealized_pnl", 0.0)
        total_pnl = state.get("total_pnl", round(realized + unrealized, 4))
        print("=== Polymarket US Paper Trader ===")
        print(f"Start Balance:  ${state.get('start_balance', init_cash):.2f}")
        print(f"Total Equity:   ${total_equity:.2f}")
        print(f"Available Cash: ${state.get('cash', init_cash):.2f}")
        print(f"Open Positions: {len(positions)} (${total_mtm:.2f} MTM)")
        print(f"Available Markets: {state.get('available_markets_count', 0)}")
        print(f"Tradable Brackets: {state.get('tradeable_brackets_count', 0)}")
        w_budget = state.get('window_budget', round(total_equity * 0.03, 2))
        w_spent = state.get('window_spent', 0.0)
        print(f"Window Budget:  ${w_budget:.2f} (Spent: ${w_spent:.2f}, Left: ${max(0.0, w_budget - w_spent):.2f})")
        print(f"Realized P&L:   ${realized:+.2f}")
        print(f"Unrealized P&L: ${unrealized:+.2f}")
        print(f"Total Net P&L:  ${total_pnl:+.2f}")
        print(f"Trades Count:   {state.get('trades_count', 0)}")
        print(f"Milestones Reached: {state.get('milestones_reached', [init_cash])}")
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
