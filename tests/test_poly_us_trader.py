"""
Unit tests for poly_us_trader.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import json
import os
import subprocess
import sys
import pytest

# poly_us_trader imports polymarket_us_client, which imports the polymarket_us
# SDK at module level; the SDK is optional, so skip instead of erroring.
pytest.importorskip("polymarket_us")

from poly_us_trader import PolyUSPaperTrader, _taker_fee

import config

# ML model is optional (torch may not be installed everywhere).
try:
    from ml_model import MLFairValue
except Exception:  # pragma: no cover
    MLFairValue = None


@pytest.fixture
def temp_trader(tmp_path):
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    with patch("poly_us_trader.STATE_FILE", state_file), patch("poly_us_trader.LEDGER_FILE", ledger_file):
        trader = PolyUSPaperTrader(initial_cash=35.0, reset=True)
        yield trader


def test_initial_state(temp_trader):
    assert temp_trader.state["cash"] == 35.0
    assert temp_trader.state["start_balance"] == 35.0
    assert temp_trader.state["positions"] == []


def test_reset_state(temp_trader):
    temp_trader.state["positions"] = [{"slug": "old", "cost": 10.0}]
    state = temp_trader.reset_state(35.0)
    assert state["start_balance"] == 35.0
    assert state["cash"] == 35.0
    assert state["positions"] == []
    assert state["milestones_reached"] == [35.0]


def test_evaluate_opportunity_finds_favorites(temp_trader, monkeypatch):
    monkeypatch.setattr(config, "PM_ALLOW_UNMODELLED", True)
    market = {
        "slug": "test-crypto-favorite",
        "question": "Will BTC reach strike?",
        "outcomePrices": ["0.95", "0.05"],
    }
    opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert opp["side"] == "yes"
    assert opp["price"] == 0.95


def test_evaluate_opportunity_ignores_unpriceable(temp_trader):
    # No parseable prices -> no bracket, regardless of entry cap.
    market = {
        "slug": "test-crypto-unpriceable",
        "question": "Will ETH reach strike?",
        "outcomePrices": ["not-a-price", "also-bad"],
    }
    opp = temp_trader.evaluate_opportunity(market)
    assert opp is None


def test_evaluate_opportunity_supports_single_price_complement(temp_trader, monkeypatch):
    monkeypatch.setattr(config, "PM_ALLOW_UNMODELLED", True)
    market = {
        "slug": "cpc-btc-150k-11-30-2026",
        "question": "When will Bitcoin hit $150k?",
        "outcomePrices": ["0.0100"],
    }
    opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert opp["side"] == "no"
    assert opp["price"] == 0.99


def test_execute_paper_trade_updates_bankroll(temp_trader):
    opp = {
        "slug": "test-market-1",
        "question": "Sample Question",
        "side": "yes",
        "price": 0.95,
        "ref_prob": 0.98,   # model prob high enough to clear fee+margin at fill price
    }
    temp_trader.preview_order = MagicMock(return_value=True)
    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        temp_trader.execute_paper_trade(opp)
    assert len(temp_trader.state["positions"]) == 1
    assert temp_trader.state["cash"] < 35.0
    assert temp_trader.state["trades_count"] == 1


def test_compute_tier_size():
    from poly_us_trader import PolyUSPaperTrader, _taker_fee
    assert PolyUSPaperTrader.compute_tier_size(20.0) == 0.60
    assert PolyUSPaperTrader.compute_tier_size(100.0) == 3.0
    assert PolyUSPaperTrader.compute_tier_size(500.0) == 15.0
    assert PolyUSPaperTrader.compute_tier_size(2000.0) == 60.0
    assert PolyUSPaperTrader.compute_tier_size(20000.0) == 600.0

    # Compound ladder milestones: 3% of equity at each tier (Task 3 spec).
    assert PolyUSPaperTrader.compute_tier_size(35.0) == 1.05      # start tier: 1 contract @~95c
    assert PolyUSPaperTrader.compute_tier_size(100.0) == 3.00     # milestone 1: 3 contracts
    assert PolyUSPaperTrader.compute_tier_size(1_000.0) == 30.00  # milestone 2: 30 contracts
    assert PolyUSPaperTrader.compute_tier_size(10_000.0) == 300.00  # milestone 3
    assert PolyUSPaperTrader.compute_tier_size(100_000.0) == 3_000.00  # milestone 4 (was $2,000 under old 2% branch)
    assert PolyUSPaperTrader.compute_tier_size(200_000.0) == 6_000.00


def test_opp_sorting_prioritizes_soonest_active():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    future_10m = (now + timedelta(minutes=10)).isoformat()
    future_60m = (now + timedelta(minutes=60)).isoformat()
    expired_5m = (now - timedelta(minutes=5)).isoformat()

    opps = [
        {"slug": "future-60m", "end_date": future_60m},
        {"slug": "expired-5m", "end_date": expired_5m},
        {"slug": "future-10m", "end_date": future_10m},
    ]

    now_ts = now.timestamp()

    def _key(opp):
        ed = opp.get("end_date") or ""
        if not ed:
            return float("inf")
        try:
            dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            ts = dt.timestamp()
            if ts <= now_ts:
                return float("inf")
            return ts
        except Exception:
            return float("inf")

    sorted_opps = sorted(opps, key=_key)
    assert sorted_opps[0]["slug"] == "future-10m"
    assert sorted_opps[1]["slug"] == "future-60m"
    assert sorted_opps[2]["slug"] == "expired-5m"


def test_cash_reserve_floor_blocks_overtrade(temp_trader):
    # Total equity = $7 cash + $28 positions = $35
    temp_trader.state["positions"] = [{"cost": 28.0, "slug": "p1"}]
    temp_trader.state["cash"] = 7.0  # Cash is 20% of equity, below 30% floor
    opp = {
        "slug": "test-market-low-cash",
        "question": "Sample",
        "side": "yes",
        "price": 0.95,
    }
    temp_trader.preview_order = MagicMock(return_value=True)
    temp_trader.execute_paper_trade(opp)
    assert len(temp_trader.state["positions"]) == 1  # No new position added


def test_check_settlements_yes_win(temp_trader):
    temp_trader.state["positions"] = [{
        "slug": "test-settle-yes",
        "side": "yes",
        "price": 0.95,
        "qty": 5,
        "cost": 4.75,
    }]
    temp_trader.state["cash"] = 95.25
    temp_trader.state["realized_pnl"] = 0.0

    with patch("poly_us_trader.polymarket_us_client.get_market_settlement", return_value={"slug": "test-settle-yes", "settlement": 1.0}):
        temp_trader.check_settlements({"test-settle-yes": {"closed": True}})

    assert len(temp_trader.state["positions"]) == 0
    assert temp_trader.state["cash"] == 95.25 + 5.00  # Payout is $5.00
    assert temp_trader.state["realized_pnl"] == round(5.00 - 4.75, 4)  # +$0.25


def test_check_settlements_no_win(temp_trader):
    temp_trader.state["positions"] = [{
        "slug": "test-settle-no",
        "side": "no",
        "price": 0.98,
        "qty": 10,
        "cost": 9.80,
    }]
    temp_trader.state["cash"] = 90.20
    temp_trader.state["realized_pnl"] = 0.0

    # Settlement 0.0 means YES lost, so NO won: payout = (1.0 - 0.0) * 10 = $10.00
    with patch("poly_us_trader.polymarket_us_client.get_market_settlement", return_value={"slug": "test-settle-no", "settlement": 0.0}):
        temp_trader.check_settlements({"test-settle-no": {"closed": True}})

    assert len(temp_trader.state["positions"]) == 0
    assert temp_trader.state["cash"] == 90.20 + 10.00  # Payout is $10.00
    assert temp_trader.state["realized_pnl"] == round(10.00 - 9.80, 4)  # +$0.20


def test_settlement_auto_close_credits_payout_once(temp_trader):
    """Settled market credits payout exactly once; re-run is idempotent."""
    temp_trader.state["positions"] = [{
        "slug": "settle-once-slug",
        "side": "yes",
        "price": 0.95,
        "qty": 5,
        "cost": 4.75,
        "unrealized_pnl": 0.25,
    }]
    temp_trader.state["cash"] = 95.25
    temp_trader.state["realized_pnl"] = 0.0
    temp_trader.state["unrealized_pnl"] = 0.25
    temp_trader.state["total_pnl"] = 0.25

    with patch("poly_us_trader.polymarket_us_client.get_market_settlement",
               return_value={"slug": "settle-once-slug", "settlement": 1.0}):
        temp_trader.check_settlements({"settle-once-slug": {"closed": True}})
        cash_after_first = temp_trader.state["cash"]
        # Idempotency: position already removed — a second pass must not credit again.
        temp_trader.check_settlements({"settle-once-slug": {"closed": True}})

    assert len(temp_trader.state["positions"]) == 0
    assert cash_after_first == 100.25          # 95.25 + 5 x $1.00
    assert temp_trader.state["cash"] == 100.25  # no double credit
    assert temp_trader.state["realized_pnl"] == round(5.00 - 4.75, 4)
    assert temp_trader.state["total_pnl"] == round(5.00 - 4.75, 4)

    events = [json.loads(l) for l in temp_trader.ledger_file.read_text().splitlines()]
    settle_events = [e for e in events if e.get("event") == "settlement"]
    assert len(settle_events) == 1
    ev = settle_events[0]
    assert ev["slug"] == "settle-once-slug"
    assert ev["payout"] == 5.00
    assert ev["pnl"] == round(5.00 - 4.75, 4)
    assert ev["cash_after"] == 100.25


def test_polymarket_us_client_module_attributes():
    import polymarket_us_client
    expected_attrs = [
        "get_market_by_slug",
        "get_market_book",
        "get_market_settlement",
        "get_events",
        "get_event_by_slug",
        "search_events",
        "get_portfolio_positions",
        "get_account_balances",
        "usd_cash_balance",
        "preview_order",
        "place_maker_order",
        "get_open_orders",
        "cancel_order",
        "close_position",
    ]
    for attr in expected_attrs:
        assert hasattr(polymarket_us_client, attr), f"Missing attribute {attr} on polymarket_us_client"


def test_evaluate_opportunity_filters_long_dated(temp_trader, monkeypatch):
    monkeypatch.setattr(config, "PM_ALLOW_UNMODELLED", True)
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Market expiring in 48 hours should be rejected
    market_48h = {
        "slug": "mkt-48h",
        "question": "Will BTC reach strike?",
        "outcomePrices": ["0.95", "0.05"],
        "endDate": (now + timedelta(hours=48)).isoformat(),
    }
    assert temp_trader.evaluate_opportunity(market_48h) is None

    # Market expiring in 2 hours should be accepted
    market_2h = {
        "slug": "mkt-2h",
        "question": "Will BTC reach strike?",
        "outcomePrices": ["0.95", "0.05"],
        "endDate": (now + timedelta(hours=2)).isoformat(),
    }
    opp = temp_trader.evaluate_opportunity(market_2h)
    assert opp is not None
    assert opp["slug"] == "mkt-2h"
    assert opp["hours_left"] is not None
    assert 1.9 <= opp["hours_left"] <= 2.1


# ── R6: entry gates apply to both paths; unmodelled fallback is gated ────────

def test_crossvenue_candidate_below_z_gate_rejected(temp_trader):
    """A cross-venue candidate whose adaptive z is below adjusted_min_z is rejected.

    Regression: the cross-venue branch used to return before the z-gate, so the
    cushion check only ever ran on fallback candidates.
    """
    from datetime import datetime, timezone, timedelta
    temp_trader.adjusted_min_z = 2.0
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    market = {
        "slug": "btc-strike-test",
        "question": "BTC vs strike?",
        "outcomePrices": ["0.95", "0.05"],
        "assetPriceTerms": {
            "asset": {"symbol": "BTC"},
            "strike": {"value": 100000},
            "windowEnd": end,
        },
    }
    cv_edge = {"best_edge_cents": 10.0, "ref_prob": 0.97, "best_price": 0.95,
               "best_side": "yes", "spot": 100001.0, "strike": 100000}
    with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge", return_value=cv_edge), \
         patch("poly_us_trader.crossvenue_edge.get_cached_spot", return_value=100001.0):
        # Spot nearly at the strike -> z ~ 0 -> below the 2.0 hurdle.
        assert temp_trader.evaluate_opportunity(market) is None
        # Same candidate with a 30% cushion clears the z-gate and is accepted.
        cv_edge["spot"] = 130000.0
        with patch("poly_us_trader.crossvenue_edge.get_cached_spot", return_value=130000.0):
            opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert opp["source"] == "crossvenue"


def test_entry_gate_uses_edge_sigma(temp_trader):
    """A7: the z-gate is scored with the same sigma the edge model used."""
    from datetime import datetime, timezone, timedelta
    temp_trader.adjusted_min_z = 0.0
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    market = {
        "slug": "btc-sigma-test",
        "question": "BTC vs strike?",
        "outcomePrices": ["0.95", "0.05"],
        "assetPriceTerms": {
            "asset": {"symbol": "BTC"},
            "strike": {"value": 100000},
            "windowEnd": end,
        },
    }
    cv_edge = {"best_edge_cents": 10.0, "ref_prob": 0.97, "best_price": 0.95,
               "best_side": "yes", "spot": 130000.0, "strike": 100000, "vol": 1.25}
    seen = {}
    orig = temp_trader._passes_entry_gates

    def spy(slug, side, terms, strike_val, spot, hours_left, sigma=None):
        seen["sigma"] = sigma
        return orig(slug, side, terms, strike_val, spot, hours_left, sigma=sigma)

    with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge", return_value=cv_edge), \
         patch("poly_us_trader.crossvenue_edge.get_cached_spot", return_value=130000.0), \
         patch.object(temp_trader, "_passes_entry_gates", side_effect=spy):
        opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert seen["sigma"] == 1.25


def test_sports_favourite_rejected_unless_unmodelled_allowed(temp_trader, monkeypatch):
    """The fallback path rejects unscoreable markets unless PM_ALLOW_UNMODELLED=true."""
    from datetime import datetime, timezone, timedelta
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    market = {
        "slug": "soccer-final-winner",
        "question": "Will the favourite win?",
        "category": "sports",
        "outcomePrices": ["0.95", "0.05"],
        "endDate": end,
    }
    with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge", return_value=None):
        assert temp_trader.evaluate_opportunity(market) is None
        monkeypatch.setattr(config, "PM_ALLOW_UNMODELLED", True)
        opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert opp["side"] == "yes"
    assert opp["price"] == 0.95
    assert opp["source"] == "fallback"


def test_cycle_rotates_stale_long_dated_positions(temp_trader):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Open position with endDate 30 days in future
    future_30d = (now + timedelta(days=30)).isoformat()
    temp_trader.state["positions"] = [{
        "slug": "stale-dec-pos",
        "side": "no",
        "price": 0.99,
        "qty": 5,
        "cost": 4.95,
        "current_price": 0.99,
    }]
    temp_trader.state["cash"] = 50.0

    market_map = {
        "stale-dec-pos": {
            "slug": "stale-dec-pos",
            "endDate": future_30d,
            "outcomePrices": ["0.01", "0.99"],
        }
    }
    # R5: rotation exits at the venue NO bid (= 1 - YES ask) with the fee model.
    book = {"bbo": {"yes_bid": "0.00", "yes_ask": "0.02"}}
    with patch.object(temp_trader, "scan_markets", return_value=list(market_map.values())), \
         patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        temp_trader.cycle()

    # Stale position rotated out at the NO bid minus slippage (0.98 -> 0.97),
    # net of the taker fee (R5 + A2 slippage).
    assert len(temp_trader.state["positions"]) == 0
    assert temp_trader.state["cash"] == round(50.0 + 0.97 * 5 - _taker_fee(5, 0.97), 4)


def test_window_budget_blocks_overtrade_in_same_window(temp_trader):
    # Total equity = $35.00 -> 3% window budget = $1.05
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0

    opp1 = {"slug": "opp1", "question": "Q1", "side": "yes", "price": 0.95,
            "ref_prob": 0.98}
    temp_trader.preview_order = MagicMock(return_value=True)
    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        # First trade: 1 contract @ 0.95 = $0.95 -> fits in $1.05 budget
        ok1 = temp_trader.execute_paper_trade(opp1)
    assert ok1 is True
    assert temp_trader.state["window_spent"] == round(0.97 + _taker_fee(1, 0.97), 4)

    # Second trade in the same window: remaining budget is $1.05 - $0.97 = $0.08 < $0.90 -> BLOCKED!
    opp2 = {"slug": "opp2", "question": "Q2", "side": "yes", "price": 0.95,
            "ref_prob": 0.98}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        ok2 = temp_trader.execute_paper_trade(opp2)
    assert ok2 is False
    assert len(temp_trader.state["positions"]) == 1


def test_mtm_dip_does_not_reset_or_drop_positions(temp_trader):
    """A $0.10 MTM dip must not reset the bankroll, drop positions, or touch
    realized_pnl (R4). The old 34.95 auto-reset silently erased losing runs."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    market = {
        "slug": "dip-market",
        "question": "Q",
        "outcomePrices": ["0.85", "0.15"],
        "endDate": (now + timedelta(hours=2)).isoformat(),
    }
    temp_trader.state["positions"] = [{
        "slug": "dip-market",
        "side": "yes",
        "price": 0.95,
        "qty": 1,
        "cost": 0.95,
        "unrealized_pnl": -0.05,
    }]
    temp_trader.state["cash"] = 34.05   # equity dips to ~34.90 (< old 34.95 trigger)
    temp_trader.state["realized_pnl"] = -3.0

    with patch.object(temp_trader, "scan_markets", return_value=[market]):
        temp_trader.cycle()

    assert temp_trader.state["positions"] != []
    assert temp_trader.state["positions"][0]["slug"] == "dip-market"
    assert temp_trader.state["realized_pnl"] == -3.0
    assert temp_trader.state["cash"] == 34.05
    ledger = temp_trader.ledger_file.read_text()
    assert "auto_reset_threshold" not in ledger


def test_reset_preserves_lifetime_stats(temp_trader):
    """--reset writes a ledger (and forfeit) event but never clears lifetime stats (R4)."""
    temp_trader.state["lifetime_realized_pnl"] = -5.5
    temp_trader.state["realized_pnl"] = -2.0
    temp_trader.state["peak_equity"] = 40.0
    temp_trader.state["max_drawdown"] = 8.0
    temp_trader.state["ruin_events"] = 1
    temp_trader.state["trades_count"] = 7
    temp_trader.state["positions"] = [{
        "slug": "forfeit-market",
        "side": "yes",
        "price": 0.50,
        "qty": 2,
        "cost": 1.0,
        "current_price": 0.6,
    }]

    temp_trader.reset_state(35.0)

    assert temp_trader.state["cash"] == 35.0
    assert temp_trader.state["trades_count"] == 7
    assert temp_trader.state["ruin_events"] == 1
    assert temp_trader.state["peak_equity"] == 40.0
    assert temp_trader.state["max_drawdown"] == 8.0
    # lifetime absorbs prior realized + the forfeited position's +$0.20 mark-to-book
    assert temp_trader.state["lifetime_realized_pnl"] == round(-5.5 - 2.0 + 0.2, 4)

    ledger = temp_trader.ledger_file.read_text()
    assert '"event": "reset"' in ledger
    assert '"event": "forfeit"' in ledger


def test_ruin_halt_blocks_entries_after_realized_loss(temp_trader):
    """A realized loss beyond the ruin limit halts entries until manual --reset (R4)."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    market = {
        "slug": "mkt",
        "question": "Q",
        "outcomePrices": ["0.95", "0.05"],
        "endDate": (now + timedelta(hours=2)).isoformat(),
    }
    temp_trader.state["realized_pnl"] = -15.0
    temp_trader.state["cash"] = 20.0
    with patch.object(temp_trader, "scan_markets", return_value=[market]):
        temp_trader.cycle()
    assert temp_trader.state.get("ruin_halted") is True
    assert int(temp_trader.state.get("ruin_events", 0)) == 1

    temp_trader.preview_order = MagicMock(return_value=True)
    ok = temp_trader.execute_paper_trade({"slug": "new", "question": "Q", "side": "yes", "price": 0.95})
    assert ok is False
    assert "ruin_halt" in temp_trader.ledger_file.read_text()


def test_available_markets_and_tradeable_brackets_tracked_separately(temp_trader, monkeypatch):
    monkeypatch.setattr(config, "PM_ALLOW_UNMODELLED", True)
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    future_2h = (now + timedelta(hours=2)).isoformat()

    # 3 available markets, all priceable. Under the 65c entry cap (HF top regime)
    # every binary market yields at least one bracket: the cheaper side is always
    # <= 50c <= 65c. m1=0.95/0.05 -> NO@0.05; m2=0.50/0.50 -> YES@0.50;
    # m3=0.98/0.02 -> NO@0.02. So all 3 qualify as tradeable brackets.
    mock_markets = [
        {"slug": "m1", "question": "Q1", "outcomePrices": ["0.95", "0.05"], "endDate": future_2h},
        {"slug": "m2", "question": "Q2", "outcomePrices": ["0.50", "0.50"], "endDate": future_2h},
        {"slug": "m3", "question": "Q3", "outcomePrices": ["0.98", "0.02"], "endDate": future_2h},
    ]

    with patch.object(temp_trader, "scan_markets", return_value=mock_markets):
        temp_trader.cycle()

    assert temp_trader.state["available_markets_count"] == 3
    assert temp_trader.state["tradeable_brackets_count"] == 2


def test_live_mode_places_real_order(temp_trader):
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)

    opp = {"slug": "test-live-slug", "question": "Live Q", "side": "no", "price": 0.99,
           "ref_prob": 0.98}
    # NO side: maker price = NO bid + tick = (1 - YES ask) + tick.
    # YES bid=0.90, YES ask=0.93 → NO bid=0.07, NO maker = 0.08, capped at NO ask - tick = 0.09 → 0.08
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.93"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.place_maker_order", return_value={"id": "ORDER123"}) as mock_place:
        ok = temp_trader.execute_paper_trade(opp)
        assert ok is True
        mock_place.assert_called_once_with("test-live-slug", "no", 0.08, 1, preview=False)
        # Live orders sit in a `resting` record with cash reserved until confirmed fill (R3)
        assert temp_trader.state["resting_orders"][-1]["order_id"] == "ORDER123"
        assert temp_trader.state["resting_orders"][-1]["state"] == "resting"
        assert temp_trader.state["positions"] == []


# ── R3: live resting-order reconcile + confirm gate ──────────────────────────

def test_live_resting_order_cancelled_after_ttl_releases_cash(temp_trader):
    """An order that never fills is cancelled after the TTL and the reservation
    is released; cash and positions end unchanged."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    opp = {"slug": "ttl-test", "question": "Q", "side": "yes", "price": 0.95,
           "ref_prob": 0.98}
    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.place_maker_order", return_value={"id": "OID-TTL-1"}):
        assert temp_trader.execute_paper_trade(opp) is True
    assert temp_trader.state["resting_orders"] and not temp_trader.state["positions"]
    reserved = temp_trader.state["resting_orders"][0]["cost"]
    cash_after_place = temp_trader.state["cash"]
    assert cash_after_place == round(35.0 - reserved, 4)
    # Force the order to look stale (placed well past the TTL).
    temp_trader.state["resting_orders"][0]["placed_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=910)).isoformat()
    with patch("polymarket_us_client.get_open_orders",
               return_value=[{"id": "OID-TTL-1", "marketSlug": "ttl-test"}]), \
         patch("polymarket_us_client.cancel_order", return_value=True) as mock_cancel, \
         patch("polymarket_us_client.get_portfolio_positions", return_value={"positions": []}):
        assert temp_trader.reconcile_live_orders() is True
    assert mock_cancel.called
    assert temp_trader.state["cash"] == pytest.approx(35.0)
    assert temp_trader.state["positions"] == []
    assert temp_trader.state["resting_orders"] == []


def test_live_partial_fill_books_filled_qty_only(temp_trader):
    """Only the venue-confirmed filled quantity is booked; the unfilled
    reservation is released."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 100.0
    temp_trader.state["window_budget"] = 3.0
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    opp = {"slug": "partial-fill-1", "question": "Q", "side": "no", "price": 0.95,
           "ref_prob": 0.98}
    # NO side: maker price = (1 - YES ask) + tick. YES bid=0.90, YES ask=0.93
    # → NO maker = 0.08 (capped at NO ask - tick = 0.09).
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.93"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.place_maker_order", return_value={"id": "OID-PART-1"}):
        assert temp_trader.execute_paper_trade(opp) is True
    reserved = temp_trader.state["resting_orders"][0]["cost"]
    # Venue resolved the order, but only 1 contract is actually held.
    with patch("polymarket_us_client.get_open_orders", return_value=[]), \
         patch("polymarket_us_client.get_portfolio_positions",
               return_value={"positions": [{"marketSlug": "partial-fill-1", "qty": 1}]}):
        assert temp_trader.reconcile_live_orders() is True
    pos = [p for p in temp_trader.state["positions"] if p["order_id"] == "OID-PART-1"]
    assert pos and pos[0]["qty"] == 1
    fill_price = pos[0]["price"]
    assert temp_trader.state["cash"] == pytest.approx(100.0 - pos[0]["cost"])
    assert temp_trader.state["resting_orders"] == []
    # Unfilled part of the reservation was released back into available cash.
    assert temp_trader.state["cash"] == round(100.0 - reserved + (reserved - pos[0]["cost"]), 4)


def test_main_refuses_live_without_confirm_env(monkeypatch):
    """--live without POLY_CONFIRM_LIVE=true exits non-zero (R3)."""
    import sys as _sys
    import poly_us_trader
    monkeypatch.setattr(config, "POLY_CONFIRM_LIVE", False)
    monkeypatch.setattr(_sys, "argv", ["poly_us_trader.py", "--live"])
    with pytest.raises(SystemExit) as exc:
        poly_us_trader.main()
    assert exc.value.code != 0


def test_live_reconcile_bails_when_open_orders_fail(temp_trader):
    """A failed open-order listing must not look like 'every order cancelled':
    the resting order and its reserved cash stay untouched."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    opp = {"slug": "venue-down-1", "question": "Q", "side": "yes", "price": 0.95,
           "ref_prob": 0.98}
    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.place_maker_order", return_value={"id": "OID-DOWN-1"}):
        assert temp_trader.execute_paper_trade(opp) is True
    reserved = temp_trader.state["resting_orders"][0]["cost"]
    with patch("polymarket_us_client.get_open_orders",
               side_effect=RuntimeError("timeout")):
        assert temp_trader.reconcile_live_orders() is False
    assert temp_trader.state["cash"] == round(35.0 - reserved, 4)
    assert len(temp_trader.state["resting_orders"]) == 1
    assert temp_trader.state["resting_orders"][0]["state"] == "resting"


def test_held_position_qty_distinguishes_failure_and_ignores_unknown_slug(temp_trader):
    """None means 'venue did not answer'; slugless rows are not evidence for
    this market and must not be counted as held."""
    with patch("polymarket_us_client.get_portfolio_positions",
               side_effect=RuntimeError("boom")):
        assert temp_trader._held_position_qty("sl-1") is None
    with patch("polymarket_us_client.get_portfolio_positions",
               return_value={"positions": [{"qty": 7}, {"marketSlug": "sl-1", "qty": 2}]}):
        assert temp_trader._held_position_qty("sl-1") == 2


def test_daily_realized_loss_day_rollover_precedes_win_short_circuit(temp_trader):
    """The first event of a new UTC day resets yesterday's loss even when that
    event is a win, so the next loss cannot halt entries early."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    temp_trader.state["daily_loss_date"] = yesterday
    temp_trader.state["daily_realized_loss"] = 6.0
    temp_trader.state["daily_loss_halted"] = True
    temp_trader._track_daily_realized(0.5)  # a win, first event of the day
    assert temp_trader.state["daily_realized_loss"] == 0.0
    assert temp_trader.state["daily_loss_halted"] is False
    temp_trader._track_daily_realized(-4.0)
    assert temp_trader.state["daily_realized_loss"] == 4.0
    assert temp_trader.state["daily_loss_halted"] is False


def test_daily_realized_loss_limit_halts_entries(temp_trader):
    """A realized loss crossing POLY_DAILY_LOSS_LIMIT_DOLLARS halts new entries
    until the next UTC day."""
    temp_trader.state["daily_loss_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    temp_trader.state["daily_realized_loss"] = 4.0
    temp_trader._track_daily_realized(-1.5)  # crosses the 5.0 limit
    assert temp_trader._daily_loss_halted() is True
    # A win does not re-open entries; only the day rollover does.
    temp_trader._track_daily_realized(1.0)
    assert temp_trader._daily_loss_halted() is True
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    temp_trader.state["daily_loss_date"] = tomorrow
    assert temp_trader._daily_loss_halted() is False
    assert temp_trader.state["daily_realized_loss"] == 0.0


def test_preview_order_always_calls_api_even_without_auth_client(temp_trader):
    """preview_order() must call the live orders.preview endpoint even in paper mode
    when auth_client is None — no silent short-circuit to True."""
    temp_trader.auth_client = None
    with patch("poly_us_trader.polymarket_us_client.preview_order",
               return_value={"order": {"state": "ORDER_STATE_PENDING_NEW"}}) as mock_preview:
        ok = temp_trader.preview_order("test-slug", "yes", 0.95, 1)
        assert ok is True
        mock_preview.assert_called_once_with("test-slug", "yes", 0.95, 1)


def test_preview_order_returns_false_on_api_failure(temp_trader):
    """preview_order() returns False (not True) when the preview API raises."""
    temp_trader.auth_client = None
    with patch("poly_us_trader.polymarket_us_client.preview_order",
               side_effect=RuntimeError("no credentials")):
        ok = temp_trader.preview_order("test-slug", "yes", 0.95, 1)
        assert ok is False


def test_import_creates_no_files(tmp_path):
    """Importing poly_us_trader must not print, read argv, or create files (R13)."""
    import os
    import subprocess
    import sys
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo_root))
    r = subprocess.run(
        [sys.executable, "-c", "import poly_us_trader"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "", r.stdout
    assert list(tmp_path.iterdir()) == [], list(tmp_path.iterdir())


def test_corrupt_state_file_is_quarantined(tmp_path):
    """A corrupt state file is renamed *.corrupt-<ts> and a fresh bankroll starts."""
    state_file = tmp_path / "state.json"
    state_file.write_text("{not valid json")
    ledger_file = tmp_path / "ledger.jsonl"
    with patch("poly_us_trader.STATE_FILE", state_file), patch("poly_us_trader.LEDGER_FILE", ledger_file):
        trader = PolyUSPaperTrader(initial_cash=35.0)
    assert trader.state["cash"] == 35.0
    quarantined = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1, list(tmp_path.iterdir())
    assert quarantined[0].read_text() == "{not valid json"


def test_instance_lock_second_instance_exits(tmp_path):
    """A second instance against a held lock exits non-zero (R12)."""
    import fcntl
    from poly_us_trader import acquire_instance_lock
    lock = tmp_path / ".lock_poly"
    fd1 = acquire_instance_lock(lock, attempts=1)
    assert fd1 is not None
    try:
        with pytest.raises(SystemExit) as exc:
            acquire_instance_lock(lock, attempts=1)
        assert exc.value.code != 0
    finally:
        fcntl.flock(fd1, fcntl.LOCK_UN)
        os.close(fd1)


def test_instance_lock_flock_failure_is_nonfatal(tmp_path, monkeypatch):
    """flock unsupported (FUSE/overlay) → warn and proceed, never crash (R12)."""
    from poly_us_trader import acquire_instance_lock
    import poly_us_trader

    def _boom(*a, **k):
        raise OSError("flock not supported here")
    monkeypatch.setattr(poly_us_trader.fcntl, "flock", _boom)
    assert acquire_instance_lock(tmp_path / ".lock_poly", attempts=1) is None


def test_market_end_dt_prefers_windowEnd(temp_trader):
    """market_end_dt: windowEnd wins over endDate; gameStartTime is never an end (R5)."""
    from datetime import datetime, timezone, timedelta
    from poly_us_trader import market_end_dt
    now = datetime.now(timezone.utc)
    m = {
        "assetPriceTerms": {"windowEnd": (now + timedelta(minutes=20)).isoformat()},
        "endDate": (now + timedelta(days=3)).isoformat(),
        "gameStartTime": (now - timedelta(hours=6)).isoformat(),
    }
    dt = market_end_dt(m)
    assert dt is not None
    assert abs((dt - now).total_seconds() - 1200) < 60

    # gameStartTime alone must not be read as an end time.
    m2 = {"gameStartTime": (now + timedelta(hours=2)).isoformat()}
    assert market_end_dt(m2) is None


def test_cycle_does_not_rotate_windowEnd_market(temp_trader):
    """A market whose windowEnd is in 20 min but endDate is 3 days out is NOT stale (R5)."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    market = {
        "slug": "short-window",
        "question": "Q",
        "outcomePrices": ["0.95", "0.05"],
        "endDate": (now + timedelta(days=3)).isoformat(),
        "assetPriceTerms": {"windowEnd": (now + timedelta(minutes=20)).isoformat()},
    }
    temp_trader.state["positions"] = [{
        "slug": "short-window", "side": "yes", "price": 0.95, "qty": 5, "cost": 4.75,
    }]
    temp_trader.state["cash"] = 50.0
    with patch.object(temp_trader, "scan_markets", return_value=[market]):
        temp_trader.cycle()
    assert len(temp_trader.state["positions"]) == 1
    assert temp_trader.state["cash"] == 50.0


def test_buy_fill_records_best_ask(temp_trader):
    """A buy at displayed 0.95 with best ask 0.96 records a 0.97 fill (R5 + A2 slippage)."""
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    opp = {"slug": "ask-market", "question": "Q", "side": "yes", "price": 0.95,
           "source": "crossvenue", "ref_prob": 0.98}
    temp_trader.preview_order = MagicMock(return_value=True)
    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        ok = temp_trader.execute_paper_trade(opp)
    assert ok is True
    pos = temp_trader.state["positions"][0]
    # fill_price = best ask (0.96) + 1 slippage tick (0.01) = 0.97
    assert pos["fill_price"] == 0.97
    # Cost is the stake plus the venue taker fee C*p*(1-p)*rate.
    assert pos["cost"] == round(0.97 + _taker_fee(1, 0.97), 4)
    assert temp_trader.state["cash"] == round(35.0 - 0.97 - _taker_fee(1, 0.97), 4)
    ledger = temp_trader.ledger_file.read_text()
    assert '"source": "crossvenue"' in ledger


# ── A2: edge at fill price + unreadable book ─────────────────────────────

def test_a2_edge_rejected_at_fill_price_when_spread_eats_it(temp_trader):
    """3c edge on displayed mid (0.92 vs ref_prob 0.95) but 3c spread means
    the fill at ask+slippage has no edge left — trade rejected at execution."""
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    # ref_prob=0.95, displayed=0.92 → 3c edge on mid. But ask=0.95, fill=0.96.
    # net_edge at fill = (0.95 - 0.96)*100 = -1c → rejected.
    opp = {"slug": "spread-eats-edge", "question": "Q", "side": "yes", "price": 0.92,
           "ref_prob": 0.95}
    temp_trader.preview_order = MagicMock(return_value=True)
    book = {"bbo": {"yes_bid": "0.93", "yes_ask": "0.95"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        ok = temp_trader.execute_paper_trade(opp)
    assert ok is False
    assert len(temp_trader.state["positions"]) == 0


def test_a2_unreadable_book_no_trade_plus_counter(temp_trader):
    """Unreadable book → no trade and book_unreadable state counter incremented."""
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.state["book_unreadable"] = 0
    opp = {"slug": "unreadable-book", "question": "Q", "side": "yes", "price": 0.95,
           "ref_prob": 0.98}
    temp_trader.preview_order = MagicMock(return_value=True)
    # Book missing bbo → _book_yes_prices returns (None, None) → ask=None → fill_price=None
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value={}):
        ok = temp_trader.execute_paper_trade(opp)
    assert ok is False
    assert len(temp_trader.state["positions"]) == 0
    assert temp_trader.state["book_unreadable"] == 1
    # Second unreadable book increments the counter again.
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value={}):
        temp_trader.execute_paper_trade(opp)
    assert temp_trader.state["book_unreadable"] == 2


def test_a2_fill_price_includes_slippage_ticks(temp_trader):
    """fill_price = best ask + slippage tick (1c), not the displayed mark."""
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    opp = {"slug": "slippage-ticks", "question": "Q", "side": "yes", "price": 0.92,
           "ref_prob": 0.99}   # high ref_prob clears hurdle even at ask+slippage
    temp_trader.preview_order = MagicMock(return_value=True)
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.93"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        ok = temp_trader.execute_paper_trade(opp)
    assert ok is True
    pos = temp_trader.state["positions"][0]
    # fill_price = 0.93 (ask) + 0.01 (slippage) = 0.94
    assert pos["fill_price"] == 0.94
    assert pos["displayed_price"] == 0.92
    # net_edge = (0.99 - 0.94)*100 = 5c >= hurdle (~0.7c) → trade taken


def test_crossvenue_edge_below_modelled_costs_rejected(temp_trader):
    """A 0.5c edge is rejected when modelled costs (slippage + margin) are 1.5c (R5)."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=1)).isoformat()
    market = {
        "slug": "cv-edge-market",
        "question": "Q",
        "endDate": end,
        "assetPriceTerms": {
            "windowEnd": end,
            "strike": {"value": 100000.0},
            "asset": {"symbol": "BTC"},
        },
    }
    cv = {
        "best_edge_cents": 0.5, "ref_prob": 0.97, "best_price": 0.95,
        "best_side": "yes", "spot": 100000.0, "strike": 99000.0,
    }
    with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge", return_value=cv):
        opp = temp_trader.evaluate_opportunity(market)
    assert opp is None


def test_check_settlements_tracks_stuck_position_fail_count(temp_trader):
    """A position whose market never settles accumulates settle_fail_count instead
    of silently stranding."""
    temp_trader.state["positions"] = [{
        "slug": "stranded-slug",
        "side": "yes",
        "price": 0.95,
        "qty": 5,
        "cost": 4.75,
    }]
    temp_trader.state["cash"] = 30.25

    with patch("poly_us_trader.polymarket_us_client.get_market_settlement", return_value=None):
        temp_trader.check_settlements({"stranded-slug": {"closed": True}})

    # Position still open, fail count incremented
    assert len(temp_trader.state["positions"]) == 1
    assert temp_trader.state["positions"][0]["settle_fail_count"] == 1
    assert temp_trader.state["realized_pnl"] == 0.0  # nothing credited


# ── ML gate tests (opt-in via config.ML_MODEL_PATH) ──────────────────────────

def _ml_market(minutes_ahead=120):
    """A market with strike+spot data that reaches the candidate entry path."""
    end = (datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "slug": "test-ml-candidate",
        "question": "Will BTC exceed strike?",
        "outcomePrices": ["0.95", "0.05"],
        "assetPriceTerms": {
            "asset": {"symbol": "BTC"},
            "strike": {"value": 90000},
            "windowEnd": end,
        },
    }


def _ml_support():
    """Training support that covers the helper's 120-minute market."""
    return {"minutes_left": [1.0, 240.0], "assets": ["BTC"], "venue": "kalshi"}


def _fake_cv_edge():
    """Cross-venue edge that is present but fails the edge hurdle (< 0.5c),
    so evaluate_opportunity falls through to the candidate path with cv_edge set."""
    return {
        "best_edge_cents": 0.1,   # below the 0.5c crossvenue hurdle
        "ref_prob": 0.97,         # >= MODEL_PROB_MIN (0.96)
        "best_price": 0.95,       # inside the 92-99c band
        "best_side": "yes",
        "spot": 100000.0,
        "strike": 90000,
    }


def test_ml_gate_denies_low_probability(temp_trader):
    """ML gate denies entry when ML probability for the chosen side is below ML_PROB_MIN."""
    temp_trader.ml_model = MagicMock()
    temp_trader.ml_model.support = _ml_support()
    temp_trader.ml_model.predict_proba.return_value = 0.40
    temp_trader.adjusted_min_z = 0.0  # bypass cushion-z to isolate the ML gate
    market = _ml_market()
    with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge",
               return_value=_fake_cv_edge()), \
         patch("poly_us_trader.crossvenue_edge.get_cached_spot", return_value=100000.0):
        opp = temp_trader.evaluate_opportunity(market)
    assert opp is None


def test_ml_gate_allows_high_probability(temp_trader):
    """ML gate allows entry when ML probability for the chosen side is above ML_PROB_MIN."""
    temp_trader.ml_model = MagicMock()
    temp_trader.ml_model.support = _ml_support()
    temp_trader.ml_model.predict_proba.return_value = 0.80
    temp_trader.adjusted_min_z = 0.0
    market = _ml_market()
    with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge",
               return_value=_fake_cv_edge()), \
         patch("poly_us_trader.crossvenue_edge.get_cached_spot", return_value=100000.0):
        opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert opp["side"] == "yes"
    assert opp["price"] == 0.95


def test_ml_gate_skips_out_of_support_market(temp_trader, caplog):
    """A6: a market outside the training support skips the gate and logs why.

    The mock would deny if scored (p=0.40 < ML_PROB_MIN), so the entry can only
    survive when the gate is skipped as out-of-support.
    """
    temp_trader.ml_model = MagicMock()
    temp_trader.ml_model.support = _ml_support()
    temp_trader.ml_model.predict_proba.return_value = 0.40
    temp_trader.adjusted_min_z = 0.0
    market = _ml_market(minutes_ahead=600)  # far outside the 1-240 minute support
    with caplog.at_level("INFO"):
        with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge",
                   return_value=_fake_cv_edge()), \
             patch("poly_us_trader.crossvenue_edge.get_cached_spot", return_value=100000.0):
            opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert "ml_out_of_support" in caplog.text
    temp_trader.ml_model.predict_proba.assert_not_called()


def test_ml_gate_skips_without_support_metadata(temp_trader):
    """A6: a model with no support metadata is never scored (conservative skip)."""
    temp_trader.ml_model = MagicMock()
    temp_trader.ml_model.support = None
    temp_trader.ml_model.predict_proba.return_value = 0.40  # would deny if scored
    temp_trader.adjusted_min_z = 0.0
    market = _ml_market()
    with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge",
               return_value=_fake_cv_edge()), \
         patch("poly_us_trader.crossvenue_edge.get_cached_spot", return_value=100000.0):
        opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    temp_trader.ml_model.predict_proba.assert_not_called()


def test_ml_gate_skipped_when_no_model(temp_trader):
    """With no ML model loaded, candidate-path entries proceed as before (graceful degradation)."""
    temp_trader.ml_model = None
    temp_trader.adjusted_min_z = 0.0
    market = _ml_market()
    with patch("poly_us_trader.crossvenue_edge.evaluate_crossvenue_edge",
               return_value=_fake_cv_edge()), \
         patch("poly_us_trader.crossvenue_edge.get_cached_spot", return_value=100000.0):
        opp = temp_trader.evaluate_opportunity(market)
    assert opp is not None
    assert opp["side"] == "yes"


def test_ml_model_loads_from_checkpoint():
    """The trained checkpoint loads via MLFairValue.load (opt-in ML_MODEL_PATH)."""
    if MLFairValue is None:
        pytest.skip("ml_model/torch unavailable")
    path = Path(__file__).resolve().parent.parent / "data" / "ml_model.pt"
    if not path.exists():
        pytest.skip("data/ml_model.pt not present")
    model = MLFairValue.load(str(path))
    assert model is not None
    # Output is a valid probability.
    row = {"strike": 90000.0, "kalshi": 0.95, "fair": 0.97, "spread": 0.0}
    p = model.predict_proba(row, 100000.0, 0.0007, 120.0)
    assert 0.0 <= p <= 1.0




def _pos(slug, side="no", qty=1, price=0.95):
    return {"slug": slug, "side": side, "qty": qty, "price": price,
            "cost": round(qty * price, 4)}


def test_unknown_market_end_does_not_strand_position(temp_trader):
    """A market with no parseable end time is 'unknown', not 'open': the
    settlement lookup must still be attempted, or the position is stranded."""
    temp_trader.state["positions"] = [_pos("no-end-slug")]
    with patch("polymarket_us_client.get_market_settlement",
               return_value={"slug": "no-end-slug", "settlement": 0.0}):
        temp_trader.check_settlements(
            {"no-end-slug": {"slug": "no-end-slug", "status": "MARKET_STATUS_OPEN"}})
    assert temp_trader.state["positions"] == []
    assert temp_trader.state["settlements_count"] == 1
    assert temp_trader.state["settled_realized_pnl"] == pytest.approx(0.05, abs=1e-6)


def test_known_open_market_with_future_end_is_held(temp_trader):
    """The complement: a market we KNOW is open must not be queried or closed."""
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    temp_trader.state["positions"] = [_pos("still-open-slug")]
    with patch("polymarket_us_client.get_market_settlement") as mock_settle:
        temp_trader.check_settlements(
            {"still-open-slug": {"slug": "still-open-slug", "endDate": future,
                                 "status": "MARKET_STATUS_OPEN"}})
    assert not mock_settle.called
    assert [p["slug"] for p in temp_trader.state["positions"]] == ["still-open-slug"]
    assert temp_trader.state.get("settlements_count", 0) == 0


def test_startup_reconciles_state_against_ledger(tmp_path):
    """State realized P&L and the ledger must not silently diverge: startup
    records the ledger's figure, the settlement count and the gap."""
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    state_file.write_text(json.dumps({
        "cash": 34.1, "positions": [], "realized_pnl": 0.0, "start_balance": 35.0}))
    lines = [
        {"event": "open", "slug": "s1", "cost": 0.95},
        {"event": "settlement", "slug": "s1", "pnl": -0.95},
        {"event": "settlement", "slug": "s2", "pnl": 0.05},
    ]
    ledger_file.write_text("\n".join(json.dumps(r) for r in lines) + "\n{ torn write\n")

    with patch("poly_us_trader.STATE_FILE", state_file), \
         patch("poly_us_trader.LEDGER_FILE", ledger_file):
        trader = PolyUSPaperTrader(initial_cash=35.0)

    assert trader.state["ledger_realized_pnl"] == pytest.approx(-0.90)
    assert trader.state["ledger_settlements"] == 2
    # The gap is the invariant state_realized + pre_epoch - ledger: a fresh
    # epoch folds the whole prior ledger into pre_epoch, so the gap is zero,
    # not the -0.90 that used to be reported as an unexplained mismatch.
    assert trader.state["pre_epoch_realized_pnl"] == pytest.approx(-0.90)
    assert trader.state["ledger_realized_gap"] == pytest.approx(0.0)
    assert trader.state["settlements_count"] == 2

    # Reloading must not re-baseline: pre_epoch is frozen at first reconcile.
    with patch("poly_us_trader.STATE_FILE", state_file), \
         patch("poly_us_trader.LEDGER_FILE", ledger_file):
        again = PolyUSPaperTrader(initial_cash=35.0)
    assert again.state["epoch_started_at"] == trader.state["epoch_started_at"]
    assert again.state["pre_epoch_realized_pnl"] == pytest.approx(-0.90)
    assert again.state["ledger_realized_gap"] == pytest.approx(0.0)


def test_taker_fee_matches_us_tier_table():
    """Polymarket US taker fee: Theta x C x p x (1-p), Tier 1 Theta=0.095
    (docs.polymarket.us/fees, effective 2026-11-14), rounded to the cent."""
    assert _taker_fee(100, 0.95) == 0.45
    assert _taker_fee(100, 0.50) == 2.38
    assert _taker_fee(100, 0.99) == 0.09
    # Tiny trades can round to $0.00 per the venue schedule.
    assert _taker_fee(1, 0.95) == 0.0
    # No fee at the extremes, none at zero size.
    assert _taker_fee(100, 1.0) == 0.0
    assert _taker_fee(0, 0.95) == 0.0


def test_fee_rounding_is_bankers_to_the_cent():
    """US schedule rounds fees to the nearest cent, half-to-even."""
    from poly_us_trader import _round_cents_half_even
    assert _round_cents_half_even(2.375) == 2.38   # half -> even (8)
    assert _round_cents_half_even(2.385) == 2.38   # half -> even (8), not 2.39
    assert _round_cents_half_even(0.005) == 0.0    # half -> even (0)
    assert _round_cents_half_even(0.015) == 0.02   # half -> even (2)
    assert _round_cents_half_even(1.005) == 1.00   # half -> even (0)
    assert _round_cents_half_even(0.4549) == 0.45


def test_legacy_rate_model_keeps_old_constant(monkeypatch):
    """PM_FEE_MODEL=legacy_rate + PM_FEE_RATE=0.07 reproduces the old international number."""
    monkeypatch.setattr(config, "PM_FEE_MODEL", "legacy_rate")
    monkeypatch.setattr(config, "PM_FEE_RATE", 0.07)
    assert round(_taker_fee(100, 0.95), 4) == round(100 * 0.07 * 0.95 * 0.05, 4)


def test_us_taker_coefficient_override(monkeypatch):
    """PM_TAKER_COEFFICIENT picks the trailing-30d tier."""
    monkeypatch.setattr(config, "PM_FEE_MODEL", "us_tier")
    monkeypatch.setattr(config, "PM_TAKER_COEFFICIENT", 0.0695)  # Tier 4 / days 1-7
    assert _taker_fee(100, 0.50) == round(100 * 0.0695 * 0.25, 2)


def test_unknown_fee_model_fails_startup():
    """An unknown PM_FEE_MODEL must fail config import (startup), not trade."""
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        env={**os.environ, "PM_FEE_MODEL": "bogus_model"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "PM_FEE_MODEL" in result.stderr


# ── A3: paper fills (taker) vs live orders (maker) ────────────────────────

def test_a3_maker_price_always_below_ask():
    """price_for_maker_entry for YES is always below the ask (never at/through
    it), and for NO is always above the NO bid (1 - YES ask)."""
    from poly_us_trader import price_for_maker_entry

    # Wide spread: YES bid=0.90, ask=0.93 → YES maker = 0.91 (bid+tick),
    # capped at 0.92 (ask-tick).
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.93"}}
    p = price_for_maker_entry(book, "yes")
    assert p is not None
    assert p == 0.91
    assert p < 0.93  # strictly below the ask

    # Tight spread (1 tick): bid=0.94, ask=0.95 → YES maker = 0.95, cap = 0.94
    # → price > cap → None (no valid maker price).
    book_tight = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.95"}}
    assert price_for_maker_entry(book_tight, "yes") is None

    # NO side: YES bid=0.90, ask=0.93 → NO bid=0.07, NO ask=0.10
    # NO maker = 0.07 + 0.01 = 0.08, capped at 0.10 - 0.01 = 0.09 → 0.08
    p_no = price_for_maker_entry(book, "no")
    assert p_no is not None
    assert p_no == 0.08
    assert p_no > 1.0 - 0.93  # above the NO bid (1 - YES ask)

    # Unreadable book → None.
    assert price_for_maker_entry({}, "yes") is None
    assert price_for_maker_entry({"bbo": {}}, "no") is None


def test_a3_two_cycles_one_order_per_slug(temp_trader):
    """In live mode, two cycles on the same slug place one order — the second
    is blocked because the resting order counts as 'already in this market'."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.93"}}

    opp = {"slug": "same-slug", "question": "Q", "side": "yes", "price": 0.92,
           "ref_prob": 0.98}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.place_maker_order",
               side_effect=[{"id": "ORDER1"}, {"id": "ORDER2"}]) as mock_place:
        ok1 = temp_trader.execute_paper_trade(opp)
        ok2 = temp_trader.execute_paper_trade(opp)

    assert ok1 is True
    assert ok2 is False  # second cycle blocked by resting order on same slug
    assert mock_place.call_count == 1
    assert len(temp_trader.state["resting_orders"]) == 1


def test_a3_earlier_position_does_not_book_never_filled_order(temp_trader):
    """Reconcile attributes fills per order: an earlier filled position on the
    same slug must not cause a later never-filled order to be booked as filled."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)

    # Order 1 filled (position already booked from a prior reconcile).
    temp_trader.state["positions"] = [{
        "slug": "dup-slug", "question": "Q", "side": "yes", "price": 0.91,
        "fill_price": 0.91, "displayed_price": 0.91, "fee": 0.0,
        "source": "", "category": "", "qty": 5, "cost": 4.55,
        "order_id": "ORDER-1", "opened_at": "2026-01-01T00:00:00+00:00",
        "paper_fill_model": "taker",
    }]
    temp_trader.state["cash"] = 30.45

    # Order 2 never filled — it's still resting but the venue shows 5 held
    # (all from order 1). Order 2's qty (3) must not be booked as filled.
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.93"}}
    temp_trader.state["resting_orders"] = [{
        "order_id": "ORDER-2", "slug": "dup-slug", "question": "Q", "side": "yes",
        "price": 0.91, "qty": 3, "cost": 2.73, "fee": 0.0,
        "source": "", "category": "", "state": "resting",
        "placed_at": "2026-01-01T00:00:00+00:00",
    }]

    with patch("poly_us_trader.polymarket_us_client.get_open_orders", return_value=[]), \
         patch("poly_us_trader.polymarket_us_client.get_portfolio_positions",
               return_value={"positions": [{"marketSlug": "dup-slug", "qty": 5}]}):
        assert temp_trader.reconcile_live_orders() is True

    # ORDER-2 resolved with no *additional* held position → cancelled, not filled.
    # Cancelled orders are removed from resting_orders by the reconcile filter;
    # ORDER-2's absence means it was resolved. No new position was created.
    resting_slug_orders = [o for o in temp_trader.state["resting_orders"] if o.get("order_id") == "ORDER-2"]
    assert not resting_slug_orders, "ORDER-2 should have been cancelled and removed"
    # Only one position (from ORDER-1).
    positions = [p for p in temp_trader.state["positions"] if p.get("slug") == "dup-slug"]
    assert len(positions) == 1
    assert positions[0]["order_id"] == "ORDER-1"


def test_a3_maker_paper_fill_only_on_trade_through(temp_trader):
    """Paper maker mode (PM_PAPER_FILL_MODEL=maker): execute_paper_trade
    records a resting order, not a position. It fills only when the book
    trades through the resting price."""
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    # Force the maker fill model on this trader instance.
    temp_trader.paper_fill_model = "maker"
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.93"}}

    opp = {"slug": "maker-paper", "question": "Q", "side": "yes", "price": 0.92,
           "ref_prob": 0.98}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        ok = temp_trader.execute_paper_trade(opp)
    assert ok is True
    # No position booked immediately — resting order instead.
    assert len(temp_trader.state["positions"]) == 0
    assert len(temp_trader.state["resting_orders"]) == 1
    resting = temp_trader.state["resting_orders"][0]
    assert resting["state"] == "resting"
    assert resting["paper_fill_model"] == "maker"
    assert resting["price"] == 0.91  # YES bid + tick = 0.90 + 0.01

    # Book hasn't traded through yet (ask=0.93 > resting 0.91) → still resting.
    temp_trader.reconcile_paper_maker_orders({"maker-paper": {"book": book}})
    still_resting = [o for o in temp_trader.state["resting_orders"] if o.get("order_id") == resting["order_id"]]
    assert still_resting and still_resting[0]["state"] == "resting"
    assert len(temp_trader.state["positions"]) == 0

    # Book trades through: ask drops to 0.91 (<= resting price 0.91).
    book_filled = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.91"}}
    temp_trader.reconcile_paper_maker_orders({"maker-paper": {"book": book_filled}})
    positions = [p for p in temp_trader.state["positions"] if p.get("slug") == "maker-paper"]
    assert len(positions) == 1
    assert positions[0]["price"] == 0.91
    assert positions[0]["fee"] == 0.0  # no maker fee
    # The resting order dict was updated in place; its state is now "filled".
    assert resting["state"] == "filled"


def test_a3_maker_price_capped_at_ask_minus_tick():
    """When bid + tick would exceed ask - tick (spread ≤ 2 ticks), no maker
    price exists — the order cannot rest without crossing."""
    from poly_us_trader import price_for_maker_entry
    # Spread = 2 ticks: bid=0.90, ask=0.92 → YES maker = 0.91, cap = 0.91 → 0.91 <= 0.91 → valid
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.92"}}
    assert price_for_maker_entry(book, "yes") == 0.91
    # Spread = 1 tick: no valid maker price
    book_tight = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.91"}}
    assert price_for_maker_entry(book_tight, "yes") is None
    # Crossed book: bid >= ask → None
    book_crossed = {"bbo": {"yes_bid": "0.95", "yes_ask": "0.94"}}
    assert price_for_maker_entry(book_crossed, "yes") is None


# ── A5: correlated exposure caps ──────────────────────────────────────────

def _make_btc_position(i, cost=1.00, side="yes", end_date="2026-01-01T00:00:00Z"):
    return {
        "slug": f"btc-{i}", "question": f"Q{i}", "side": side, "price": 0.95,
        "fill_price": 0.95, "displayed_price": 0.95, "fee": 0.0,
        "source": "", "category": "crypto", "symbol": "BTC",
        "qty": 1, "cost": cost, "order_id": f"oid-btc-{i}",
        "opened_at": "2026-01-01T00:00:00+00:00", "end_date": end_date,
        "paper_fill_model": "taker",
    }


def test_a5_asset_cap_rejects_btc_when_cap_exceeded(temp_trader):
    """A5: BTC entry rejected when 25% of equity is already in BTC positions."""
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    # 11 BTC positions @ $1.00 = $11.00 exposure; total_balance = $46.00;
    # cap = 25% of $46.00 = $11.50. New trade ~$0.97 → $11.97 > $11.50 → reject.
    temp_trader.state["positions"] = [_make_btc_position(i) for i in range(11)]

    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    opp = {"slug": "btc-new", "question": "New BTC", "side": "yes", "price": 0.95,
           "ref_prob": 0.98, "symbol": "BTC", "end_date": "2026-01-01T00:00:00Z"}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        ok = temp_trader.execute_paper_trade(opp)

    assert ok is False  # $11.00 + ~$0.97 > $11.50 cap → rejected
    assert len(temp_trader.state["positions"]) == 11  # no new position added


def test_a5_eth_accepted_when_btc_at_cap(temp_trader):
    """A5: ETH entry accepted when BTC is at the 25% asset cap (different asset)."""
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    temp_trader.state["positions"] = [_make_btc_position(i) for i in range(11)]

    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    opp = {"slug": "eth-new", "question": "New ETH", "side": "yes", "price": 0.95,
           "ref_prob": 0.98, "symbol": "ETH", "end_date": "2026-01-02T00:00:00Z"}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        ok = temp_trader.execute_paper_trade(opp)

    assert ok is True  # ETH exposure is $0.00 → well under cap
    assert len(temp_trader.state["positions"]) == 12


def test_a5_resting_order_counts_toward_asset_cap(temp_trader):
    """A5: a resting maker order counts toward the asset cap (no double-spend)."""
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    temp_trader.state["positions"] = [_make_btc_position(i) for i in range(11)]

    book = {"bbo": {"yes_bid": "0.94", "yes_ask": "0.96"}}
    opp = {"slug": "btc-resting", "question": "BTC resting", "side": "yes", "price": 0.95,
           "ref_prob": 0.98, "symbol": "BTC", "end_date": "2026-01-01T00:00:00Z"}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book):
        ok = temp_trader.execute_paper_trade(opp)

    assert ok is False
    assert len(temp_trader.state["positions"]) == 11
    assert len(temp_trader.state.get("resting_orders", [])) == 0


def test_a5_exposure_by_asset_includes_resting_orders():
    """A5: exposure_by_asset sums positions + resting orders grouped by symbol."""
    from poly_us_trader import exposure_by_asset
    state = {
        "positions": [_make_btc_position(0, cost=3.00)],
        "resting_orders": [{
            "order_id": "oid-rest-1", "slug": "btc-rest", "side": "yes",
            "price": 0.95, "qty": 1, "cost": 4.00, "fee": 0.0,
            "symbol": "BTC", "end_date": "2026-01-01T00:00:00Z",
            "state": "resting", "placed_at": "2026-01-01T00:00:00Z",
        }],
    }
    exp = exposure_by_asset(state)
    assert exp["BTC"] == 7.00  # 3.00 (position) + 4.00 (resting order)


def test_a5_exposure_by_direction():
    """A5: exposure_by_direction sums positions + resting orders per side."""
    from poly_us_trader import exposure_by_direction
    state = {
        "positions": [_make_btc_position(0, cost=2.00, side="yes")],
        "resting_orders": [{
            "order_id": "oid-rest-2", "slug": "btc-rest", "side": "no",
            "price": 0.95, "qty": 1, "cost": 3.00, "fee": 0.0,
            "symbol": "BTC", "end_date": "2026-01-01T00:00:00Z",
            "state": "resting", "placed_at": "2026-01-01T00:00:00Z",
        }],
    }
    exp = exposure_by_direction(state)
    assert exp["yes"] == 2.00
    assert exp["no"] == 3.00


def test_a5_exposure_by_expiry():
    """A5: exposure_by_expiry sums positions + resting orders per end_date."""
    from poly_us_trader import exposure_by_expiry
    state = {
        "positions": [_make_btc_position(0, cost=2.00, end_date="2026-01-15T00:00:00Z")],
        "resting_orders": [{
            "order_id": "oid-rest-3", "slug": "btc-rest", "side": "yes",
            "price": 0.95, "qty": 1, "cost": 3.00, "fee": 0.0,
            "symbol": "BTC", "end_date": "2026-01-15T00:00:00Z",
            "state": "resting", "placed_at": "2026-01-01T00:00:00Z",
        }],
    }
    exp = exposure_by_expiry(state)
    assert exp["2026-01-15T00:00:00Z"] == 5.00


# ── A8: paginated scan, event slug from payload, settle-fail only when closed ──

class _FakeScanClient:
    """Fake SDK client serving /v1/markets from per-category page lists."""

    def __init__(self, pages_crypto, pages_all):
        self.pages = {"crypto": pages_crypto, "all": pages_all}
        self.calls = []

    def get(self, path, *, query=None, authenticated=False):
        self.calls.append(dict(query or {}))
        q = query or {}
        offset = int(q.get("offset", 0))
        limit = max(int(q.get("limit", 100)), 1)
        key = "crypto" if "crypto" in str(q) else "all"
        page_idx = offset // limit
        rows = self.pages.get(key, [])
        if page_idx >= len(rows):
            return {"markets": []}
        return {"markets": rows[page_idx]}


def test_scan_markets_paginates_until_short_page(temp_trader, monkeypatch):
    """A8: the scan keeps paging until a short page instead of stopping at 100."""
    pages_crypto = [[{"slug": f"cm{i}"} for i in range(100)],
                    [{"slug": f"cm{i}"} for i in range(100, 200)],
                    [{"slug": f"cm{i}"} for i in range(200, 230)]]
    monkeypatch.setattr(temp_trader, "client", _FakeScanClient(pages_crypto, [[]]))
    monkeypatch.setattr("polymarket_us_client.search_events", lambda q: [])
    markets = temp_trader.scan_markets()
    assert len({m["slug"] for m in markets}) == 230


def test_scan_markets_warns_when_pagination_capped(temp_trader, monkeypatch, caplog):
    """A8: hitting the page cap with full pages warns about truncation."""
    import poly_us_trader
    monkeypatch.setattr(poly_us_trader, "_SCAN_MAX_PAGES", 2)
    full = [[{"slug": f"p{p}-{i}"} for i in range(100)] for p in range(5)]
    monkeypatch.setattr(temp_trader, "client", _FakeScanClient(full, [[]]))
    monkeypatch.setattr("polymarket_us_client.search_events", lambda q: [])
    with caplog.at_level("WARNING"):
        markets = temp_trader.scan_markets()
    assert len(markets) == 200
    assert "truncat" in caplog.text.lower()


def test_cycle_missing_market_uses_position_event_slug(temp_trader):
    """A8: the parent event slug comes from the stored payload field, not slug parsing."""
    end = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pos = {"slug": "odd_market_format", "side": "yes", "price": 0.95, "qty": 5,
           "cost": 4.75, "event_slug": "market-ev-parent"}
    temp_trader.state["positions"] = [pos]
    child = {"slug": "odd_market_format", "closed": False, "endDate": end}
    with patch.object(temp_trader, "scan_markets", return_value=[]), \
         patch("polymarket_us_client.get_event_by_slug",
               return_value={"event": {"slug": "market-ev-parent", "markets": [child]}}) as ev_mock, \
         patch("polymarket_us_client.get_market_by_slug") as direct_mock, \
         patch("polymarket_us_client.get_market_settlement") as settle_mock:
        temp_trader.cycle()
    ev_mock.assert_called_once_with("market-ev-parent")
    direct_mock.assert_not_called()
    settle_mock.assert_not_called()  # known-open market is held, no settlement lookup


def test_check_settlements_only_counts_fail_when_known_closed(temp_trader):
    """A8: an open market missing from the scan does not accrue settle_fail_count."""
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    temp_trader.state["positions"] = [
        {"slug": "open-unknown", "side": "yes", "price": 0.95, "qty": 5, "cost": 4.75},
        {"slug": "past-end", "side": "yes", "price": 0.95, "qty": 5, "cost": 4.75,
         "end_date": past},
    ]
    with patch("polymarket_us_client.get_market_settlement", return_value=None):
        temp_trader.check_settlements({})
    by_slug = {p["slug"]: p for p in temp_trader.state["positions"]}
    assert by_slug["open-unknown"].get("settle_fail_count", 0) == 0
    assert by_slug["past-end"]["settle_fail_count"] == 1


def test_cycle_warns_over_request_budget(temp_trader, monkeypatch, caplog):
    """A8: per-cycle request count is budgeted; over-budget warns."""
    monkeypatch.setattr(config, "PM_REQUEST_BUDGET_PER_CYCLE", 1, raising=False)
    pos = {"slug": "open-unknown", "side": "yes", "price": 0.95, "qty": 5, "cost": 4.75,
           "event_slug": "ev-open-unknown"}
    temp_trader.state["positions"] = [pos]
    with patch.object(temp_trader, "scan_markets", return_value=[]), \
         patch("polymarket_us_client.get_event_by_slug",
               return_value={"event": {"markets": []}}), \
         patch("polymarket_us_client.get_market_by_slug", return_value={}), \
         patch("polymarket_us_client.get_market_settlement", return_value=None):
        with caplog.at_level("WARNING"):
            temp_trader.cycle()
    assert "request budget" in caplog.text.lower()


# ── A9: lock before state, strict read-only --status, refuse --reset ─────────

def _seed_state(path, **overrides):
    data = {"start_balance": 35.0, "cash": 12.5, "positions": [],
            "realized_pnl": 0.0, "trades_count": 0}
    data.update(overrides)
    path.write_text(json.dumps(data))
    return data


def test_reset_refused_when_lock_held_and_state_unchanged(tmp_path, monkeypatch):
    """A9: --reset must take the lock first and refuse when a bot holds it."""
    import fcntl
    import sys as _sys
    import poly_us_trader
    state = tmp_path / "state.json"
    _seed_state(state)
    monkeypatch.setattr(poly_us_trader, "STATE_FILE", state)
    monkeypatch.setattr(poly_us_trader, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(poly_us_trader, "LOCK_FILE", tmp_path / ".lock_poly")
    monkeypatch.setattr(poly_us_trader, "POLY_US_LOG_FILE", tmp_path / "dry.log")
    get_client = MagicMock()
    monkeypatch.setattr(poly_us_trader.polymarket_us_client, "_get_client", get_client)
    fd = poly_us_trader.acquire_instance_lock(poly_us_trader.LOCK_FILE, attempts=1)
    try:
        monkeypatch.setattr(_sys, "argv", ["poly_us_trader.py", "--reset"])
        with pytest.raises(SystemExit) as exc:
            poly_us_trader.main()
        assert exc.value.code != 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert json.loads(state.read_text())["cash"] == 12.5
    get_client.assert_not_called()


def test_run_refuses_when_lock_held_before_touching_state(tmp_path, monkeypatch):
    """A9: the run path takes the lock before the constructor rewrites state."""
    import fcntl
    import sys as _sys
    import poly_us_trader
    state = tmp_path / "state.json"
    _seed_state(state)
    before = state.read_bytes()
    monkeypatch.setattr(poly_us_trader, "STATE_FILE", state)
    monkeypatch.setattr(poly_us_trader, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(poly_us_trader, "LOCK_FILE", tmp_path / ".lock_poly")
    monkeypatch.setattr(poly_us_trader, "_LOCK_ATTEMPTS", 1)
    monkeypatch.setattr(poly_us_trader, "POLY_US_LOG_FILE", tmp_path / "dry.log")
    get_client = MagicMock()
    monkeypatch.setattr(poly_us_trader.polymarket_us_client, "_get_client", get_client)
    fd = poly_us_trader.acquire_instance_lock(poly_us_trader.LOCK_FILE, attempts=1)
    try:
        monkeypatch.setattr(_sys, "argv", ["poly_us_trader.py"])
        with pytest.raises(SystemExit) as exc:
            poly_us_trader.main()
        assert exc.value.code != 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert state.read_bytes() == before
    get_client.assert_not_called()
    assert not (tmp_path / "ledger.jsonl").exists()


def test_status_on_corrupt_state_is_read_only(tmp_path, monkeypatch, capsys):
    """A9: --status never quarantines, never saves, never builds a client."""
    import sys as _sys
    import poly_us_trader
    state = tmp_path / "state.json"
    state.write_text("{not json")
    monkeypatch.setattr(poly_us_trader, "STATE_FILE", state)
    monkeypatch.setattr(poly_us_trader, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(poly_us_trader, "LOCK_FILE", tmp_path / ".lock_poly")
    monkeypatch.setattr(poly_us_trader, "POLY_US_LOG_FILE", tmp_path / "dry.log")
    get_client = MagicMock()
    monkeypatch.setattr(poly_us_trader.polymarket_us_client, "_get_client", get_client)
    monkeypatch.setattr(_sys, "argv", ["poly_us_trader.py", "--status"])
    poly_us_trader.main()
    assert state.read_text() == "{not json"
    assert list(tmp_path.glob("*.corrupt-*")) == []
    assert not (tmp_path / "ledger.jsonl").exists()
    get_client.assert_not_called()
    assert "state unreadable" in capsys.readouterr().out.lower()


def test_status_does_not_take_the_lock(tmp_path, monkeypatch, capsys):
    """A9: a read-only --status works while another instance holds the lock."""
    import fcntl
    import sys as _sys
    import poly_us_trader
    state = tmp_path / "state.json"
    _seed_state(state)
    monkeypatch.setattr(poly_us_trader, "STATE_FILE", state)
    monkeypatch.setattr(poly_us_trader, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(poly_us_trader, "POLY_US_LOG_FILE", tmp_path / "dry.log")
    lock = tmp_path / ".lock_poly"
    monkeypatch.setattr(poly_us_trader, "LOCK_FILE", lock)
    fd = poly_us_trader.acquire_instance_lock(lock, attempts=1)
    try:
        monkeypatch.setattr(_sys, "argv", ["poly_us_trader.py", "--status"])
        poly_us_trader.main()  # must not raise SystemExit despite the held lock
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    out = capsys.readouterr().out
    assert "Total Equity" in out


# ── A10: watchdog pings during long backoff sleeps ──────────────────────────

def test_backoff_sleep_pings_watchdog(monkeypatch):
    """A10: a 300s backoff sleep pings WATCHDOG=1 at least once per 60s."""
    import poly_us_trader
    events = []
    monkeypatch.setattr(poly_us_trader.time, "sleep", lambda s: events.append(("sleep", s)))
    monkeypatch.setattr(poly_us_trader, "systemd_notify", lambda m: events.append(("ping", m)))
    poly_us_trader._sleep_with_watchdog(300)
    since_ping = 0.0
    pings = 0
    for kind, val in events:
        if kind == "sleep":
            since_ping += val
            assert since_ping <= 60.0, "no watchdog ping within 60s of sleeping"
        else:
            assert val == "WATCHDOG=1"
            since_ping = 0.0
            pings += 1
    assert pings >= 1
    assert sum(v for k, v in events if k == "sleep") == pytest.approx(300.0)


# ── A13: accounting & reconciliation ─────────────────────────────────────────

def test_ruin_rule_uses_bid_based_mtm(temp_trader, monkeypatch):
    """A13: entries halt when the BID-based equity drawdown hits the limit.

    Displayed-mid equity stays flat at 109 (bid-based equity is 20), so the
    pre-A13 mid-MTM rule would not have halted.
    """
    pos = {"slug": "bidmtm-test", "side": "yes", "price": 0.99, "current_price": 0.99,
           "qty": 100, "cost": 10.0, "event_slug": "ev-bidmtm"}
    temp_trader.state["positions"] = [pos]
    temp_trader.state["cash"] = 10.0
    temp_trader.state["peak_equity"] = 40.0
    book = {"bbo": {"yes_bid": "0.10", "yes_ask": "0.12"}}
    with patch.object(temp_trader, "scan_markets", return_value=[]), \
         patch("polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.get_event_by_slug",
               return_value={"event": {"markets": []}}), \
         patch("polymarket_us_client.get_market_by_slug", return_value={}), \
         patch("polymarket_us_client.get_market_settlement", return_value=None):
        temp_trader.cycle()
    assert temp_trader.state["ruin_halted"] is True
    assert temp_trader.state["ruin_events"] == 1


def test_ruin_rule_not_tripped_by_healthy_bid(temp_trader):
    """A13: with a healthy bid, bid-based drawdown is zero — no halt."""
    pos = {"slug": "bidmtm-ok", "side": "yes", "price": 0.99, "current_price": 0.99,
           "qty": 100, "cost": 10.0, "event_slug": "ev-bidmtm-ok"}
    temp_trader.state["positions"] = [pos]
    temp_trader.state["cash"] = 10.0
    temp_trader.state["peak_equity"] = 40.0
    book = {"bbo": {"yes_bid": "0.99", "yes_ask": "1.00"}}
    with patch.object(temp_trader, "scan_markets", return_value=[]), \
         patch("polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.get_event_by_slug",
               return_value={"event": {"markets": []}}), \
         patch("polymarket_us_client.get_market_by_slug", return_value={}), \
         patch("polymarket_us_client.get_market_settlement", return_value=None):
        temp_trader.cycle()
    assert temp_trader.state.get("ruin_halted") is False


def test_live_venue_reconcile_mismatch_halts_entries(temp_trader, caplog):
    """A13: venue cash vs state cash mismatch logs WARNING and halts entries."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 30.0
    temp_trader.state["positions"] = [{"slug": "recon-1", "side": "no", "qty": 2,
                                        "price": 0.95, "cost": 1.9, "symbol": "ETH"}]
    with patch("polymarket_us_client.get_account_balances",
               return_value={"balances": [{"currency": "USD", "currentBalance": 10.0}]}), \
         patch("polymarket_us_client.get_portfolio_positions",
               return_value={"positions": [{"marketSlug": "recon-1", "qty": 2}]}):
        with caplog.at_level("WARNING"):
            assert temp_trader.reconcile_venue_state() is False
    assert temp_trader.state["venue_recon_halted"] is True
    assert "mismatch" in caplog.text.lower()
    # Entries are refused while the halt latch is set.
    assert temp_trader.execute_paper_trade({"slug": "recon-1", "question": "Q",
                                            "side": "no", "price": 0.95,
                                            "ref_prob": 0.98}) is False


def test_live_venue_reconcile_position_mismatch_halts(temp_trader):
    """A13: position qty gap (cash fine) also halts entries."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 30.0
    temp_trader.state["positions"] = [{"slug": "recon-2", "side": "yes", "qty": 3,
                                        "price": 0.95, "cost": 2.85, "symbol": "SOL"}]
    with patch("polymarket_us_client.get_account_balances",
               return_value={"balances": [{"currency": "USD", "currentBalance": 30.0}]}), \
         patch("polymarket_us_client.get_portfolio_positions", return_value={"positions": []}):
        assert temp_trader.reconcile_venue_state() is False
    assert temp_trader.state["venue_recon_halted"] is True


def test_live_venue_reconcile_matching_state_does_not_halt(temp_trader):
    """A13: matching cash and positions reconcile clean."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 30.0
    temp_trader.state["positions"] = [{"slug": "recon-3", "side": "yes", "qty": 3,
                                        "price": 0.95, "cost": 2.85, "symbol": "SOL"}]
    with patch("polymarket_us_client.get_account_balances",
               return_value={"balances": [{"currency": "USD", "currentBalance": 30.0}]}), \
         patch("polymarket_us_client.get_portfolio_positions",
               return_value={"positions": [{"marketSlug": "recon-3", "qty": 3}]}):
        assert temp_trader.reconcile_venue_state() is True
    assert not temp_trader.state.get("venue_recon_halted")


def test_live_trades_count_increments_on_fill_not_placement(temp_trader):
    """A13: live resting order placement does not count; a confirmed fill does."""
    temp_trader.live_mode = True
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 5.0
    temp_trader.state["window_spent"] = 0.0
    temp_trader.preview_order = MagicMock(return_value=True)
    opp = {"slug": "tc-fill-1", "question": "Q", "side": "no", "price": 0.95, "ref_prob": 0.98}
    book = {"bbo": {"yes_bid": "0.90", "yes_ask": "0.93"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.place_maker_order", return_value={"id": "OID-TC-1"}):
        assert temp_trader.execute_paper_trade(opp) is True
    assert temp_trader.state["trades_count"] == 0
    with patch("polymarket_us_client.get_open_orders", return_value=[]), \
         patch("polymarket_us_client.get_portfolio_positions",
               return_value={"positions": [{"marketSlug": "tc-fill-1", "qty": 1}]}):
        assert temp_trader.reconcile_live_orders() is True
    assert temp_trader.state["trades_count"] == 1


def test_status_prints_a1_breaker_and_a5_exposure(temp_trader, capsys):
    """A13: --status reports the A1 breaker and A5 per-asset exposure."""
    import poly_us_trader
    temp_trader.state["positions"] = [{"slug": "s1", "side": "yes", "price": 0.95,
                                        "qty": 2, "cost": 1.9, "symbol": "BTC"}]
    temp_trader.state["a1_breaker"] = {"halted": True, "probing": False, "reason": "brier high"}
    temp_trader.state["venue_recon_halted"] = True
    poly_us_trader._print_status(temp_trader.state, 35.0)
    out = capsys.readouterr().out
    assert "A1 Calibration Breaker" in out
    assert "HALTED" in out
    assert "A5 Exposure by asset" in out
    assert "BTC" in out
