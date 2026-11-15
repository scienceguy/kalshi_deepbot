"""B6: per-series fee type and multiplier in the Kalshi fee model.

Kalshi exposes fee_type and fee_multiplier per series (GET /series). Series
whose fee_type includes maker fees (quadratic_with_maker_fees) charge resting
orders a maker fee ON FILL, not on cancel. Series may use a multiplier other
than 1. Unknown fee types refuse a live start (dry runs continue with the
standard taker model). Standard series are unchanged.
"""
import math
import sys

import pytest

import fees


@pytest.fixture(autouse=True)
def _clean_series_cache():
    fees.clear_series_fees()
    yield
    fees.clear_series_fees()


def test_standard_series_unchanged():
    raw = math.ceil((0.07 * 1 * 0.95 * 0.05 + 1e-12) * 1_000_000) / 1_000_000
    assert fees.taker_fee_dollars(1, 0.95) == raw
    # No series data at all and an unknown series: both equal the raw model.
    assert fees.taker_fee_dollars(1, 0.95, series="KXBTC15M") == raw


def test_multiplier_halves_taker_fee():
    fees.set_series_fees({"KXTEST15M": {"fee_type": "quadratic", "fee_multiplier": 0.5}})
    base = fees.taker_fee_dollars(1, 0.95)
    half = fees.taker_fee_dollars(1, 0.95, series="KXTEST15M")
    assert half == pytest.approx(base / 2, abs=1e-6)


def test_maker_fee_only_on_maker_fee_series():
    fees.set_series_fees({"KXMQ15M": {"fee_type": "quadratic_with_maker_fees",
                                      "fee_multiplier": 1.0}})
    # Maker fee exists on that series at fill time...
    assert fees.maker_fee_dollars(1, 0.95, series="KXMQ15M") > 0
    # ...and is exactly the 0.0175 quadratic coefficient...
    raw = math.ceil((0.0175 * 1 * 0.95 * 0.05 + 1e-12) * 1_000_000) / 1_000_000
    assert fees.maker_fee_dollars(1, 0.95, series="KXMQ15M") == raw
    # ...zero on the standard series...
    assert fees.maker_fee_dollars(1, 0.95, series="KXBTC15M") == 0.0
    # ...and zero for zero contracts (a cancelled order books nothing).
    assert fees.maker_fee_dollars(0, 0.95, series="KXMQ15M") == 0.0


def test_unknown_fee_type_refuses_live():
    with pytest.raises(ValueError):
        fees.require_known_fee_type("KXBAD15M", "mystery_type", live=True)
    # Dry run only warns, never raises.
    fees.require_known_fee_type("KXBAD15M", "mystery_type", live=False)


def test_known_fee_types_do_not_raise():
    for t in ("quadratic", "quadratic_with_maker_fees", "no_fee", "none", ""):
        fees.require_known_fee_type("KXS15M", t, live=True)


def test_series_from_ticker():
    assert fees.series_from_ticker("KXBTC15M-26NOV1515-ABC") == "KXBTC15M"
    assert fees.series_from_ticker("KXETH15M") == "KXETH15M"
    assert fees.series_from_ticker("") == ""


def test_get_series_fee_models_parses(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["trader.py"])
    from kalshi_client import KalshiClient
    with __import__("unittest.mock", fromlist=["patch", "MagicMock"]).patch.object(
            KalshiClient, "_load_private_key") as m, \
        __import__("unittest.mock", fromlist=["patch", "MagicMock"]).patch.object(
            KalshiClient, "_verify_key"):
        from unittest.mock import MagicMock
        m.return_value = MagicMock()
        client = KalshiClient()
    client.get_series = lambda limit=400: [
        {"ticker": "KXMQ15M", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 2.0},
        {"ticker": "KXNOFEE", "fee_multiplier": "0.5"},     # multiplier coerced from str
        {"series_ticker": "KXALT15M", "fee_type": "quadratic"},  # alt key name
        {"fee_type": "quadratic"},                            # no name -> skipped
    ]
    out = client.get_series_fee_models()
    assert out["KXMQ15M"] == {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 2.0}
    assert out["KXNOFEE"]["fee_multiplier"] == 0.5
    assert out["KXALT15M"]["fee_multiplier"] == 1.0
    assert len(out) == 3


def test_entry_fee_maker_fill_on_maker_series(monkeypatch):
    """A resting maker entry on a maker-fee series pays the maker fee on fill;
    a standard series still pays nothing for a maker entry."""
    monkeypatch.setattr(sys, "argv", ["trader.py"])
    import trader
    fees.set_series_fees({"KXMQ15M": {"fee_type": "quadratic_with_maker_fees",
                                      "fee_multiplier": 1.0}})
    f = trader._entry_fee_dollars(1, 95, False, series="KXMQ15M")
    assert f == fees.maker_fee_dollars(1, 0.95, series="KXMQ15M") > 0
    assert trader._entry_fee_dollars(1, 95, False) == 0.0               # standard series
    assert trader._entry_fee_dollars(1, 95, False, series="KXBTC15M") == 0.0
    # Taker entries are unaffected by the maker path.
    assert trader._entry_fee_dollars(1, 95, True, series="KXMQ15M") > 0


def test_refresh_refuses_live_on_unknown_type(monkeypatch, caplog):
    monkeypatch.setattr(sys, "argv", ["trader.py"])
    import trader

    class FakeClient:
        def get_series_fee_models(self):
            return {"KXBAD15M": {"fee_type": "mystery", "fee_multiplier": 1.0}}

    monkeypatch.setattr(trader.config, "TARGET_SERIES", ["KXBAD15M"])
    with pytest.raises(ValueError):
        trader._refresh_series_fee_models(FakeClient(), live=True)
    # Dry run logs a warning and survives.
    trader._refresh_series_fee_models(FakeClient(), live=False)
