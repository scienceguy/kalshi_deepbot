"""Paper (dry-run) ledger + paper sizing.

Without these the dry run never fills, never settles, and never compounds:
with a 70% cash floor and a $1.00 absolute floor, a $1.22 bankroll cannot
afford one 90c favourite contract.
"""
import json

import pytest

import paper_mode
from risk import RiskManager


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "paper_state.json"
    monkeypatch.setattr(paper_mode, "STATE_FILE", path)
    monkeypatch.setattr(paper_mode, "_state", None)
    return path


def test_start_balance_is_persisted_once(ledger):
    assert paper_mode.start_balance(1.22) == 1.22
    # A later real-balance change must not reset the compounding curve.
    assert paper_mode.start_balance(9.99) == 1.22
    assert json.loads(ledger.read_text())["start_balance"] == 1.22


def test_cash_accounts_for_paper_pnl_and_open_risk(ledger):
    paper_mode.start_balance(1.22)
    # Entry: cash drops by the capital locked in the position.
    assert paper_mode.cash(1.22, 0.90) == pytest.approx(0.32)
    # Settlement: risk released and paper P&L credited.
    paper_mode.add_pnl(1.22, 0.10)
    assert paper_mode.cash(1.22, 0.0) == pytest.approx(1.32)
    assert json.loads(ledger.read_text())["pnl"] == pytest.approx(0.10)


def test_paper_sizing_caps_single_trade_risk():
    """12% per trade, so one loss cannot take the bankroll (was 95%)."""
    rm = RiskManager()
    # At a $10 bankroll the sizing rule is what binds: $1.20 per trade.
    assert rm.max_trade_dollars(10.0, 0.0, paper=True) == pytest.approx(1.20, abs=0.01)
    assert rm.max_trade_dollars(1.22, 0.0, paper=True) == pytest.approx(1.22 * 0.12, abs=0.01)

    # A 90c favourite needs 74% of a $1.22 bankroll — refused as drawdown risk.
    allowed, reason = rm.check(1.22, 0.0, 0.90, "KXBTC15M-TEST", paper=True)
    assert not allowed, reason

    # At $10 a whole 90c favourite fits inside the 12% cap.
    allowed, reason = rm.check(10.0, 0.0, 0.90, "KXBTC15M-TEST", paper=True)
    assert allowed, reason


def test_live_sizing_still_blocks_an_oversized_favourite():
    rm = RiskManager()
    max_trade = rm.max_trade_dollars(1.22, 0.0, paper=False)
    assert max_trade < 0.90, "live keeps the conservative cash floor"
    allowed, _ = rm.check(1.22, 0.0, 0.90, "KXBTC15M-TEST", paper=False)
    assert not allowed
