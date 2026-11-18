"""H6/H7: sub-second spot for the guard; no REST calls from the guard tick.

H6 — the guard reads the live WebSocket tick via spot_with_age(), never the
     5-second slow-loop cache.
H7 — the guard's BBO lookup is cache-only: a disconnected stream must not
     trigger a REST get_market_book() call from inside the guard tick.
"""
import binance_feed
import poly_us_trader


def test_spot_with_age_absent_then_fresh():
    binance_feed._ws_prices.clear()
    assert binance_feed.spot_with_age("BTCUSDT") == (None, None)
    binance_feed._ws_set("BTCUSDT", 12345.0)
    price, age = binance_feed.spot_with_age("BTCUSDT")
    assert price == 12345.0
    assert age is not None and age >= 0.0
    binance_feed._ws_prices.clear()


def test_guard_bbo_is_cache_only(monkeypatch):
    """A disconnected stream returns (None, None) and never calls REST."""
    called = {"n": 0}

    class _Stream:
        def is_connected(self):
            return False

    monkeypatch.setattr(poly_us_trader.poly_us_stream, "get_poly_stream",
                        lambda: _Stream())
    monkeypatch.setattr(poly_us_trader.polymarket_us_client, "get_market_book",
                        lambda slug: called.__setitem__("n", called["n"] + 1) or {})
    bid, ask = poly_us_trader._bbo_for_slug_cached("any-slug")
    assert (bid, ask) == (None, None)
    assert called["n"] == 0  # H7: no REST fallback from the guard path


def test_guard_spot_uses_live_tick_not_rest(monkeypatch):
    """get_spot_with_age reads the WS tick and does not fetch REST itself."""
    fetched = {"n": 0}
    monkeypatch.setattr(binance_feed, "spot_price",
                        lambda sym: fetched.__setitem__("n", fetched["n"] + 1) or 1.0)
    binance_feed._ws_prices.clear()
    binance_feed._ws_set("BTCUSDT", 100.0)
    import crossvenue_edge
    price, age = crossvenue_edge.get_spot_with_age("BTC", max_age_s=1.5)
    assert price == 100.0
    assert age is not None
    assert fetched["n"] == 0
    binance_feed._ws_prices.clear()
