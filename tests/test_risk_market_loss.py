"""B4: per-market loss limits are keyed by asset over a rolling window.

Losses used to be keyed by the unique 15-minute ticker, so one window's loss
never counted against the next, and the dict grew without pruning. Now they
are bucketed by asset/series with timestamps and entries older than
MARKET_LOSS_WINDOW_HOURS expire.
"""
import time

import pytest

import risk


@pytest.fixture
def rm():
    return risk.RiskManager()


def test_two_btc_contracts_count_toward_one_btc_limit(rm):
    rm.record_market_loss("KXBTC15M-A", 5.0)
    rm.record_market_loss("KXBTC15M-B", 5.0)
    assert rm.market_loss_total("KXBTC15M-C") == 10.0
    assert rm.market_loss_exceeded("KXBTC15M-C", 9.99)
    assert not rm.market_loss_exceeded("KXBTC15M-C", 10.01)


def test_eth_loss_does_not_count_toward_btc_limit(rm):
    rm.record_market_loss("KXETH15M-A", 50.0)
    assert not rm.market_loss_exceeded("KXBTC15M-A", 10.0)
    assert rm.market_loss_exceeded("KXETH15M-B", 10.0)


def test_losses_expire_after_window(monkeypatch, rm):
    now = time.time()
    monkeypatch.setattr(risk.time, "time", lambda: now)
    rm.record_market_loss("KXBTC15M-A", 5.0)
    monkeypatch.setattr(risk.time, "time", lambda: now + 25 * 3600)
    rm._prune_losses()
    assert rm.market_loss_total("KXBTC15M-Z") == 0.0
    assert "BTC" not in rm.state.market_daily_losses  # bucket pruned away


def test_unrealized_snapshot_replaces_not_accumulates(rm):
    """S5 fix: record_unrealized_loss stores peak (max) loss, not current snapshot."""
    rm.record_unrealized_loss("KXBTC15M-A", 3.0)
    rm.record_unrealized_loss("KXBTC15M-A", 1.0)  # same ticker: refresh with smaller loss
    entries = rm.state.market_daily_losses["KXBTC15M-A:unrealized"]
    # Peak loss is stored (3.0), not the latest snapshot (1.0)
    assert sum(e["loss"] for e in entries) == 3.0
    # unrealized loss counts toward the asset bucket's limit
    assert rm.market_loss_exceeded("KXBTC15M-Z", 0.5)


def test_unrealized_across_positions_sums_by_asset(rm):
    rm.record_unrealized_loss("KXBTC15M-A", 2.0)
    rm.record_unrealized_loss("KXBTC15M-B", 3.0)
    assert rm.market_loss_exceeded("KXBTC15M-C", 4.99)
    assert not rm.market_loss_exceeded("KXBTC15M-C", 5.01)


def test_size_stays_bounded(monkeypatch, rm):
    now = time.time()
    monkeypatch.setattr(risk.time, "time", lambda: now)
    for i in range(50):
        rm.record_market_loss(f"KXBTC15M-{i}", 0.1)
    assert len(rm.state.market_daily_losses) == 1  # one asset bucket
    monkeypatch.setattr(risk.time, "time", lambda: now + 25 * 3600)
    rm.record_market_loss("KXETH15M-1", 0.1)  # prunes the stale BTC entries
    assert len(rm.state.market_daily_losses) == 1
    assert "ETH" in rm.state.market_daily_losses
    assert "BTC" not in rm.state.market_daily_losses
