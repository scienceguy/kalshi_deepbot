"""Settlement P&L math — money path with no direct unit coverage until now.

`_settle_pnl` decides the realized P&L booked for every position held to
expiry, so a mistake here flips the paper curve and the live ledger without
any test failing. These cases pin the fee model: a maker entry pays nothing,
a taker entry pays the effective fee (incl. Kalshi's cent-grid rounding), and
a losing YES or a winning NO is signed correctly.
"""
import sys

import pytest

# trader parses argv at import time.
sys.argv = ["trader.py"]

import trader


@pytest.mark.parametrize("entry_taker", [False, True])
def test_winning_yes_fee_model(entry_taker):
    """1 YES at 95c settling at 1.00: maker +$0.05, taker +$0.04 (1c fee)."""
    pnl, entry_fee = trader._settle_pnl(1, 95, 1.0, entry_taker)
    if entry_taker:
        assert pnl == pytest.approx(0.04)
        assert entry_fee == pytest.approx(0.01)
    else:
        assert pnl == pytest.approx(0.05)
        assert entry_fee == pytest.approx(0.0)


def test_losing_yes_maker_loses_full_stake():
    """Maker entry pays no fee, so a loss is exactly the 95c stake."""
    pnl, entry_fee = trader._settle_pnl(1, 95, 0.0, False)
    assert pnl == pytest.approx(-0.95)
    assert entry_fee == pytest.approx(0.0)


def test_winning_no_pays_one_dollar_per_contract():
    """2 NO at 90c. Callers pass our side's payout price (1 - yes), so a NO
    win is exit 1.00: +$0.20, not -$1.80 from passing the raw YES 0.00."""
    pnl, _ = trader._settle_pnl(2, 90, 1.0, False)
    assert pnl == pytest.approx(0.20)


def test_entry_fee_dollars_maker_is_zero():
    assert trader._entry_fee_dollars(3, 92, False) == 0.0


def test_entry_fee_dollars_taker_is_cost_above_stake():
    """The effective fee is what leaves the balance above contracts * price."""
    fee = trader._entry_fee_dollars(1, 95, True)
    assert fee == pytest.approx(0.01)
