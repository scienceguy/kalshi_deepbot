"""Regression tests for Binance WebSocket endpoint construction."""

from types import SimpleNamespace

import binance_feed


def test_combined_feed_uses_stream_path_for_raw_ws_base(monkeypatch):
    """Combined subscriptions require /stream, not the raw /ws path."""
    attempted = []

    def stop_after_url(url):
        attempted.append(url)
        raise RuntimeError("stop after capturing URL")

    monkeypatch.setattr(
        binance_feed, "BINANCE_WS", "wss://data-stream.binance.vision/ws"
    )
    monkeypatch.setattr(binance_feed, "_binance_ws_async", stop_after_url)
    monkeypatch.setattr(binance_feed, "_BINANCE_WS_MAX_RETRIES", 1)
    monkeypatch.setattr(
        binance_feed.asyncio,
        "new_event_loop",
        lambda: SimpleNamespace(run_until_complete=None),
    )
    monkeypatch.setattr(binance_feed.asyncio, "set_event_loop", lambda loop: None)

    binance_feed._binance_ws_loop(["BTCUSDT"])

    assert attempted == [
        "wss://data-stream.binance.vision/stream?streams="
        "btcusdt@bookTicker/btcusdt@aggTrade"
    ]
