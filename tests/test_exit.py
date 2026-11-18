"""Early-exit tests: sell orders that close a position before settlement."""
import sys
from unittest.mock import ANY, MagicMock

import pytest

# Set sys.argv before importing trader to avoid argparse SystemExit
sys.argv = ["trader.py"]

import kalshi_client
import trader
from kalshi_client import KalshiClient


def _pos(entry, side="yes", contracts=2):
    return {
        "side": side, "entry_price": entry, "contracts": contracts,
        "order_id": "ord-1", "paper": False,
    }


@pytest.fixture(autouse=True)
def paper_fills(monkeypatch):
    """Pin paper mode and the maker-entry assumption: these tests assert the
    dry-run fill path and the maker-entry/taker-exit fee pair.

    The host .env decides DRY_RUN and AGGRESSIVE_FILL. A host with
    DRY_RUN=false flipped the exit path onto the live fill-count branch, and
    AGGRESSIVE_FILL=true added an entry taker fee (0.37 instead of 0.40) —
    the same 4 tests failed only on the VPS.
    """
    monkeypatch.setattr(trader.config, "DRY_RUN", True)
    monkeypatch.setattr(trader.config, "AGGRESSIVE_FILL", False)


@pytest.fixture
def exits_on(monkeypatch):
    monkeypatch.setattr(trader.config, "EXIT_ENABLED", True)
    monkeypatch.setattr(trader.config, "EXIT_TAKE_PROFIT_CENTS", 20)
    monkeypatch.setattr(trader.config, "EXIT_STOP_LOSS_CENTS", 10)
    monkeypatch.setattr(trader.config, "EXIT_MIN_MINUTES_LEFT", 1.0)
    saved = dict(trader.state.positions)
    trader.state.positions.clear()
    yield
    trader.state.positions.clear()
    trader.state.positions.update(saved)


def _run(monkeypatch, client, quote):
    monkeypatch.setattr(trader, "_exit_quote", lambda c, t: quote)
    booked = []
    monkeypatch.setattr(trader, "_book_exit", lambda *a: booked.append(a))
    trader._manage_exits(client)
    return booked


def test_take_profit_sells_yes_at_bid(exits_on, monkeypatch):
    client = MagicMock()
    trader.state.positions["KXBTC15M-1"] = _pos(30)
    booked = _run(monkeypatch, client, (52, 47, 5.0))
    client.sell_position.assert_called_once_with(
        "KXBTC15M-1", "yes", 2, 52, client_order_id=ANY)
    assert len(booked) == 1
    assert "take profit" in booked[0][5]


def test_stop_loss_sells_yes_at_bid(exits_on, monkeypatch):
    client = MagicMock()
    trader.state.positions["KXBTC15M-1"] = _pos(30)
    booked = _run(monkeypatch, client, (18, 81, 5.0))
    client.sell_position.assert_called_once_with(
        "KXBTC15M-1", "yes", 2, 18, client_order_id=ANY)
    assert "stop loss" in booked[0][5]


def test_no_side_uses_no_bid(exits_on, monkeypatch):
    client = MagicMock()
    trader.state.positions["KXBTC15M-1"] = _pos(60, side="no")
    booked = _run(monkeypatch, client, (38, 80, 5.0))
    client.sell_position.assert_called_once_with(
        "KXBTC15M-1", "no", 2, 80, client_order_id=ANY)
    assert len(booked) == 1


def test_holds_when_inside_the_band(exits_on, monkeypatch):
    client = MagicMock()
    trader.state.positions["KXBTC15M-1"] = _pos(30)
    booked = _run(monkeypatch, client, (40, 59, 5.0))  # +10c, neither target nor stop
    client.sell_position.assert_not_called()
    assert booked == []


def test_holds_near_settlement(exits_on, monkeypatch):
    client = MagicMock()
    trader.state.positions["KXBTC15M-1"] = _pos(30)
    booked = _run(monkeypatch, client, (55, 44, 0.5))  # target hit but < 1 min left
    client.sell_position.assert_not_called()
    assert booked == []


def test_unfilled_live_exit_is_retried_not_booked(exits_on, monkeypatch):
    client = MagicMock()
    client.sell_position.return_value = {"order_id": "x", "fill_count_fp": "0.00"}
    monkeypatch.setattr(trader.config, "DRY_RUN", False)
    trader.state.positions["KXBTC15M-1"] = _pos(30)
    booked = _run(monkeypatch, client, (52, 47, 5.0))
    assert booked == []
    assert "KXBTC15M-1" in trader.state.positions


class TestSellPayload:
    """A sell must quote the YES leg: sell YES = ask, sell NO = bid at 100-price."""

    def _client(self):
        c = KalshiClient.__new__(KalshiClient)
        sent = {}
        c._request = lambda method, path, json=None, **kw: (sent.update(json) or {"order_id": "x"})
        return c, sent

    def test_sell_yes_is_ask_at_price(self, monkeypatch):
        monkeypatch.setattr(kalshi_client.config, "DRY_RUN", False)
        c, sent = self._client()
        c.sell_position("T", "yes", 2, 52)
        assert sent["side"] == "ask"
        assert sent["price"] == "0.5200"
        assert sent["count"] == "2.00"  # Kalshi fixed-point contract count
        # Default exit must cross, or it can never fill at the bid.
        assert sent["time_in_force"] == "immediate_or_cancel"
        assert sent["post_only"] is False

    def test_sell_no_is_bid_at_complement(self, monkeypatch):
        monkeypatch.setattr(kalshi_client.config, "DRY_RUN", False)
        c, sent = self._client()
        c.sell_position("T", "no", 3, 80)
        assert sent["side"] == "bid"
        assert sent["price"] == "0.2000"  # 100 - 80 on the YES leg
        assert sent["count"] == "3.00"

    def test_rejects_bad_input(self):
        c, _ = self._client()
        with pytest.raises(ValueError):
            c.sell_position("T", "yes", 0, 50)
        with pytest.raises(ValueError):
            c.sell_position("T", "yes", 1, 0)
        with pytest.raises(ValueError):
            c.sell_position("T", "maybe", 1, 50)


class TestBookExit:
    """Money math for a booked exit: maker entry, taker sell, once-only ledger."""

    def _wire(self, monkeypatch, applied=True, raises=False):
        ldb = MagicMock()
        if raises:
            ldb.apply_settlement_pnl.side_effect = RuntimeError("database is locked")
        else:
            ldb.apply_settlement_pnl.return_value = applied
        n8n = MagicMock()
        monkeypatch.setattr(trader, "get_local_db", lambda: ldb)
        monkeypatch.setattr(trader, "get_n8n_client", lambda: n8n)
        monkeypatch.setattr(trader.paper_mode, "add_pnl", lambda pnl: None)
        risk = MagicMock()
        monkeypatch.setattr(trader.state, "risk", risk)
        monkeypatch.setattr(trader.state, "session_pnl", 0.0)
        return ldb, n8n, risk

    def test_maker_entry_plus_taker_exit(self, monkeypatch):
        ldb, n8n, risk = self._wire(monkeypatch)
        trader._book_exit(MagicMock(), "KXBTC15M-1", _pos(30, contracts=2), 52, 2, "take profit")

        # 2x30c = $0.60 in; 2x52c = $1.04 out less the 7% taker fee -> $1.00
        risk.record_trade_outcome.assert_called_once()
        assert risk.record_trade_outcome.call_args[0][0] is True
        assert risk.record_trade_outcome.call_args[0][1] == pytest.approx(0.40)
        assert trader.state.session_pnl == pytest.approx(0.40)
        assert ldb.apply_settlement_pnl.call_args[0][0] == "exit:KXBTC15M-1:ord-1:2:52"
        assert n8n.execute_webhook.call_args[0][0] == "superbot-trade"
        assert n8n.execute_webhook.call_args[0][1]["action"] == "exit"

    def test_duplicate_exit_does_not_double_book(self, monkeypatch):
        self._wire(monkeypatch, applied=False)
        trader._book_exit(MagicMock(), "KXBTC15M-1", _pos(30, contracts=2), 52, 2, "take profit")
        assert trader.state.session_pnl == 0.0

    def test_ledger_error_does_not_book_the_pnl(self, monkeypatch):
        # When the local ledger write fails, PnL must NOT be credited —
        # crediting without recording the idempotency key would double-count
        # on replay/restart (audit finding #1.1).
        self._wire(monkeypatch, raises=True)
        trader._book_exit(MagicMock(), "KXBTC15M-1", _pos(30, contracts=2), 52, 2, "take profit")
        assert trader.state.session_pnl == 0.0

    def test_ledger_none_write_enqueues_the_exit_for_replay(self, monkeypatch):
        # apply_settlement_pnl returns None (write failed) WITHOUT raising, so
        # the caller never re-queues — _book_exit must enqueue the exit itself
        # or the realized P&L is lost for good.
        self._wire(monkeypatch, applied=None)
        enq = []
        monkeypatch.setattr(trader, "_enqueue_settlement", lambda p: enq.append(p))
        trader._book_exit(MagicMock(), "KXBTC15M-1", _pos(30, contracts=2), 52, 2, "take profit")
        assert trader.state.session_pnl == 0.0
        assert len(enq) == 1
        assert enq[0]["action"] == "exit"
        assert enq[0]["order_id"] == "ord-1"
        assert enq[0]["pnl_dollars"] == pytest.approx(0.40)

    def test_stop_loss_is_recorded_as_a_losing_trade(self, monkeypatch):
        _, _, risk = self._wire(monkeypatch)
        trader._book_exit(MagicMock(), "KXBTC15M-1", _pos(30, contracts=2), 18, 2, "stop loss")
        risk.record_trade_outcome.assert_called_once()
        assert risk.record_trade_outcome.call_args[0][0] is False
        risk.record_market_loss.assert_called_once()


# ── B1: exit replay must book the exit price, not the settlement price ──────

def test_failed_exit_enqueues_computed_pnl_with_exit_key(monkeypatch, tmp_path):
    """A _book_exit failure must enqueue the exit P&L computed at the exit bid.

    Regression (B1): the enqueue used pnl_dollars=None, so replay resolved the
    entry at the market's *settlement* price under a settle: key instead of the
    exit bid under the exit: key — wrong P&L and a different idempotency key.
    """
    import json
    import fees

    qfile = tmp_path / "q.json"
    monkeypatch.setattr(trader, "SETTLEMENT_QUEUE_FILE", qfile)
    monkeypatch.setattr(trader.config, "EXIT_ENABLED", True)
    monkeypatch.setattr(trader.config, "EXIT_TAKE_PROFIT_CENTS", 20)
    monkeypatch.setattr(trader.config, "EXIT_MIN_MINUTES_LEFT", 1.0)
    monkeypatch.setattr(trader, "_exit_quote", lambda c, t: (52, 47, 5.0))

    def _boom(*a, **k):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(trader, "_book_exit", _boom)
    trader.state.positions.clear()
    trader.state.positions["KXBTC15M-1"] = _pos(30)

    client = MagicMock()
    client.sell_position.return_value = {"order": {"order_id": "exit-oid",
                                                   "status": "executed"}}
    trader._manage_exits(client)
    trader.state.positions.clear()

    queue = json.loads(qfile.read_text())
    assert len(queue) == 1
    payload = queue[0]["payload"]
    expected = (fees.sell_cash_in(2, 0.52, taker=True)
                - fees.buy_cash_out(2, 0.30, taker=False))
    assert payload["action"] == "exit"
    assert payload["pnl_dollars"] == pytest.approx(expected, abs=1e-9)
    assert payload["outcome_won"] is True
    assert payload["fill_price_cents"] == 52
    assert payload["entry_price_cents"] == 30
    assert payload["contracts"] == 2
    assert payload["entry_was_taker"] is False


def test_replay_exit_books_at_exit_key_not_settlement(monkeypatch, tmp_path):
    """Replaying an exit uses the exit: idempotency key and never fetches the
    settlement price (B1)."""
    import json
    import fees

    expected = (fees.sell_cash_in(2, 0.52, taker=True)
                - fees.buy_cash_out(2, 0.30, taker=False))
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"order_id": "ord-1", "payload": {
        "action": "exit", "order_id": "ord-1", "exit_order_id": "exit-oid",
        "status": "settled", "fill_price_cents": 52, "outcome_won": True,
        "pnl_dollars": expected, "fee_dollars": 0.04,
        "ticker": "KXBTC15M-1", "side": "yes", "contracts": 2,
        "entry_price_cents": 30, "entry_was_taker": False, "paper": False}}]))
    monkeypatch.setattr(trader, "SETTLEMENT_QUEUE_FILE", qfile)
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    monkeypatch.setattr(trader.state, "session_pnl", 0.0)
    client = MagicMock()
    client.get_market.side_effect = AssertionError(
        "exit replay must not fetch the settlement price")
    ldb = MagicMock()
    ldb.apply_settlement_pnl.return_value = True
    monkeypatch.setattr(trader, "get_local_db", lambda: ldb)

    trader._replay_settlement_queue(client)

    key = ldb.apply_settlement_pnl.call_args[0][0]
    assert key.startswith("exit:KXBTC15M-1:exit-oid:2:52")
    assert trader.state.session_pnl == pytest.approx(expected, abs=1e-9)
    assert json.loads(qfile.read_text()) == []  # drained


def test_manage_exits_confirms_venue_contracts_before_drop(monkeypatch):
    """K7: _manage_exits confirms venue contracts are zero before dropping position."""
    client = MagicMock()
    # Mock sell_position to return a response with fill_count_fp = contracts
    client.sell_position.return_value = {"order": {"fill_count_fp": 2.0, "status": "executed"}}
    # Mock get_positions to return empty for first call (venue confirms zero contracts)
    client.get_positions.return_value = []
    trader.state.positions["KXBTC15M-1"] = _pos(30)
    booked = []
    monkeypatch.setattr(trader, "_exit_quote", lambda c, t: (18, 81, 5.0))
    monkeypatch.setattr(trader, "_book_exit", lambda *a: booked.append(a))
    # K7 only runs in live mode (not DRY_RUN)
    monkeypatch.setattr(trader.config, "DRY_RUN", False)
    monkeypatch.setattr(trader.config, "EXIT_ENABLED", True)

    # First call: venue says 0 contracts -> position dropped
    monkeypatch.setattr(trader, "_venue_contracts", lambda c, t, s: 0.0)
    trader._manage_exits(client)
    assert "KXBTC15M-1" not in trader.state.positions
    assert len(booked) == 1

    # Second call: venue says None (unreachable) -> position kept for retry
    trader.state.positions["KXBTC15M-2"] = _pos(30)
    booked.clear()
    monkeypatch.setattr(trader, "_venue_contracts", lambda c, t, s: None)
    trader._manage_exits(client)
    # Position should still exist (not dropped)
    assert "KXBTC15M-2" in trader.state.positions
    # No exit booked yet
    assert len(booked) == 0


def test_replay_legacy_exit_none_pnl_resolves_from_payload(monkeypatch, tmp_path):
    """A pre-fix queue row with pnl_dollars=None still books from its own exit
    fields, never from the venue settlement (B1)."""
    import json
    import fees

    expected = (fees.sell_cash_in(2, 0.52, taker=True)
                - fees.buy_cash_out(2, 0.30, taker=False))
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps([{"order_id": "ord-1", "payload": {
        "action": "exit", "order_id": "ord-1", "exit_order_id": "exit-oid",
        "status": "settled", "fill_price_cents": 52, "outcome_won": None,
        "pnl_dollars": None, "ticker": "KXBTC15M-1", "side": "yes",
        "contracts": 2, "entry_price_cents": 30, "entry_was_taker": False,
        "paper": False}}]))
    monkeypatch.setattr(trader, "SETTLEMENT_QUEUE_FILE", qfile)
    monkeypatch.setattr(trader, "get_n8n_client", lambda: MagicMock())
    monkeypatch.setattr(trader.state, "session_pnl", 0.0)
    client = MagicMock()
    client.get_market.side_effect = AssertionError(
        "legacy exit replay must not fetch the settlement price")
    ldb = MagicMock()
    ldb.apply_settlement_pnl.return_value = True
    monkeypatch.setattr(trader, "get_local_db", lambda: ldb)

    trader._replay_settlement_queue(client)

    key = ldb.apply_settlement_pnl.call_args[0][0]
    assert key.startswith("exit:KXBTC15M-1:exit-oid:2:52")
    assert trader.state.session_pnl == pytest.approx(expected, abs=1e-9)
    assert json.loads(qfile.read_text()) == []  # drained


def test_manage_exits_confirms_venue_contracts_before_drop(monkeypatch):
    """K7: _manage_exits confirms venue contracts are zero before dropping position."""
    client = MagicMock()
    # Mock sell_position to return a response with fill_count_fp = contracts
    client.sell_position.return_value = {"order": {"fill_count_fp": 2.0, "status": "executed"}}
    # Mock get_positions to return empty for first call (venue confirms zero contracts)
    client.get_positions.return_value = []
    trader.state.positions["KXBTC15M-1"] = _pos(30)
    booked = []
    monkeypatch.setattr(trader, "_exit_quote", lambda c, t: (18, 81, 5.0))
    monkeypatch.setattr(trader, "_book_exit", lambda *a: booked.append(a))
    # K7 only runs in live mode (not DRY_RUN)
    monkeypatch.setattr(trader.config, "DRY_RUN", False)
    monkeypatch.setattr(trader.config, "EXIT_ENABLED", True)

    # First call: venue says 0 contracts -> position dropped
    monkeypatch.setattr(trader, "_venue_contracts", lambda c, t, s: 0.0)
    trader._manage_exits(client)
    assert "KXBTC15M-1" not in trader.state.positions
    assert len(booked) == 1

    # Second call: venue says None (unreachable) -> position kept for retry
    trader.state.positions["KXBTC15M-2"] = _pos(30)
    booked.clear()
    monkeypatch.setattr(trader, "_venue_contracts", lambda c, t, s: None)
    trader._manage_exits(client)
    # Position should still exist (not dropped)
    assert "KXBTC15M-2" in trader.state.positions
    # No exit booked yet
    assert len(booked) == 0
