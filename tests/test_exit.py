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
        "order_id": "ord-1", "paper": True,
    }


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
        sb = MagicMock()
        sb.set_bot_state_batch.return_value = True
        n8n = MagicMock()
        monkeypatch.setattr(trader, "get_local_db", lambda: ldb)
        monkeypatch.setattr(trader, "get_supabase_client", lambda: sb)
        monkeypatch.setattr(trader, "get_n8n_client", lambda: n8n)
        monkeypatch.setattr(trader.paper_mode, "add_pnl", lambda bal, pnl: None)
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
        assert ldb.apply_settlement_pnl.call_args[0][0] == "exit:KXBTC15M-1:ord-1"
        assert n8n.execute_webhook.call_args[0][0] == "superbot-trade"
        assert n8n.execute_webhook.call_args[0][1]["action"] == "exit"

    def test_duplicate_exit_does_not_double_book(self, monkeypatch):
        self._wire(monkeypatch, applied=False)
        trader._book_exit(MagicMock(), "KXBTC15M-1", _pos(30, contracts=2), 52, 2, "take profit")
        assert trader.state.session_pnl == 0.0

    def test_ledger_error_still_books_the_pnl(self, monkeypatch):
        self._wire(monkeypatch, raises=True)
        trader._book_exit(MagicMock(), "KXBTC15M-1", _pos(30, contracts=2), 52, 2, "take profit")
        assert trader.state.session_pnl == pytest.approx(0.40)

    def test_stop_loss_is_recorded_as_a_losing_trade(self, monkeypatch):
        _, _, risk = self._wire(monkeypatch)
        trader._book_exit(MagicMock(), "KXBTC15M-1", _pos(30, contracts=2), 18, 2, "stop loss")
        risk.record_trade_outcome.assert_called_once()
        assert risk.record_trade_outcome.call_args[0][0] is False
        risk.record_market_loss.assert_called_once()
