"""
Unit tests for poly_us_trader.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import json
import os
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
    # NO side: fill at 1 - YES bid + slippage ticks. YES bid=0.98 → NO fill = 1-0.98+0.01 = 0.03
    book = {"bbo": {"yes_bid": "0.98", "yes_ask": "0.99"}}
    with patch("poly_us_trader.polymarket_us_client.get_market_book", return_value=book), \
         patch("polymarket_us_client.place_maker_order", return_value={"id": "ORDER123"}) as mock_place:
        ok = temp_trader.execute_paper_trade(opp)
        assert ok is True
        mock_place.assert_called_once_with("test-live-slug", "no", 0.03, 1, preview=False)
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
    book = {"bbo": {"yes_bid": "0.98", "yes_ask": "0.99"}}
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

def _ml_market():
    """A market with strike+spot data that reaches the candidate entry path."""
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def test_taker_fee_matches_official_table():
    """Polymarket crypto taker fee is C x 0.07 x p x (1-p) (100-share table)."""
    assert round(_taker_fee(100, 0.95), 2) == 0.33
    assert round(_taker_fee(100, 0.50), 2) == 1.75
    assert round(_taker_fee(100, 0.99), 2) == 0.07
    # No fee at the extremes, none at zero size.
    assert _taker_fee(100, 1.0) == 0.0
    assert _taker_fee(0, 0.95) == 0.0
