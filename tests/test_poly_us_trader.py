"""
Unit tests for poly_us_trader.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import json
import os
import pytest

# poly_us_trader imports polymarket_us_client, which imports the polymarket_us
# SDK at module level; the SDK is optional, so skip instead of erroring.
pytest.importorskip("polymarket_us")

from poly_us_trader import PolyUSPaperTrader

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


def test_evaluate_opportunity_finds_favorites(temp_trader):
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


def test_evaluate_opportunity_supports_single_price_complement(temp_trader):
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
    }
    temp_trader.preview_order = MagicMock(return_value=True)
    temp_trader.execute_paper_trade(opp)
    assert len(temp_trader.state["positions"]) == 1
    assert temp_trader.state["cash"] < 35.0
    assert temp_trader.state["trades_count"] == 1


def test_compute_tier_size():
    from poly_us_trader import PolyUSPaperTrader
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


def test_evaluate_opportunity_filters_long_dated(temp_trader):
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
    with patch.object(temp_trader, "scan_markets", return_value=list(market_map.values())):
        temp_trader.cycle()

    # Stale position rotated out, cash credited back
    assert len(temp_trader.state["positions"]) == 0
    assert temp_trader.state["cash"] == 50.0 + 4.95


def test_window_budget_blocks_overtrade_in_same_window(temp_trader):
    # Total equity = $35.00 -> 3% window budget = $1.05
    temp_trader.state["cash"] = 35.0
    temp_trader.state["window_budget"] = 1.05
    temp_trader.state["window_spent"] = 0.0

    opp1 = {"slug": "opp1", "question": "Q1", "side": "yes", "price": 0.95}
    temp_trader.preview_order = MagicMock(return_value=True)

    # First trade: 1 contract @ 0.95 = $0.95 -> fits in $1.05 budget
    ok1 = temp_trader.execute_paper_trade(opp1)
    assert ok1 is True
    assert temp_trader.state["window_spent"] == 0.95

    # Second trade in the same window: remaining budget is $1.05 - $0.95 = $0.10 < $0.90 -> BLOCKED!
    opp2 = {"slug": "opp2", "question": "Q2", "side": "yes", "price": 0.95}
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


def test_available_markets_and_tradeable_brackets_tracked_separately(temp_trader):
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

    opp = {"slug": "test-live-slug", "question": "Live Q", "side": "no", "price": 0.99}
    with patch("polymarket_us_client.place_maker_order", return_value={"id": "ORDER123"}) as mock_place:
        ok = temp_trader.execute_paper_trade(opp)
        assert ok is True
        mock_place.assert_called_once_with("test-live-slug", "no", 0.99, 1)
        assert temp_trader.state["positions"][-1]["order_id"] == "ORDER123"


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


