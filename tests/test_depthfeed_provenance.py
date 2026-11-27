"""Tests for MCP client integrations (DepthFeed + Kalshi MCP).

These test the optional cross-venue data enrichment functions added to
signals.py. They verify the fail-closed behavior when clients are
unavailable and the correct data extraction when they are.
"""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import signals
import depthfeed_client
import config


@pytest.fixture(autouse=True)
def _depthfeed_enabled(monkeypatch):
    monkeypatch.setattr(config, "DEPTHFEED_ENABLED", True)


def test_depthfeed_orderbook_selects_latest_valid_timestamp():
    rows = [
        {"market_id": "market-1", "time": "2026-11-25 12:00:00", "price_up": 0.40, "price_down": 0.60},
        {"market_id": "market-1", "price_up": 0.20, "price_down": 0.80},  # missing timestamp must not win
        {"market_id": "market-1", "time": "2026-11-25 13:00:00", "price_up": 0.60, "price_down": 0.40},
    ]
    with patch.object(depthfeed_client, "get_polymarket_snapshots", return_value=rows):
        book = depthfeed_client.get_polymarket_orderbook("market-1")
    assert book["mid"] == 0.60
    assert book["raw_timestamp"] == "2026-11-25 13:00:00"
    expected_ms = int(datetime(2026, 11, 25, 13, tzinfo=timezone.utc).timestamp() * 1000)
    assert book["ts_ms"] == expected_ms


def test_depthfeed_snapshots_request_and_preserve_full_ladder():
    payload = {"data": [{
        "market_id": "market-1", "time": "2026-11-25 13:00:00", "price_up": 0.5, "price_down": 0.5,
        "orderbook_up": {"bids": [[0.49, 10]], "asks": [[0.51, 20]]},
        "orderbook_down": {"bids": [[0.48, 30]], "asks": [[0.52, 40]]},
    }]}
    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    with patch.object(depthfeed_client, "_mcp_call", return_value=result) as call:
        book = depthfeed_client.get_polymarket_orderbook("market-1")
    assert call.call_args.args[1]["include_orderbook"] is True
    assert book["orderbook_up"]["asks"] == [[0.51, 20]]
    assert book["orderbook_down"]["bids"] == [[0.48, 30]]


def test_depthfeed_rejects_wrong_market_instead_of_using_newer_book():
    rows = [
        {"market_id": "market-1", "time": "2026-11-25 12:00:00", "price_up": 0.40},
        {"market_id": "market-2", "time": "2026-11-25 13:00:00", "price_up": 0.90},
    ]
    with patch.object(depthfeed_client, "get_polymarket_snapshots", return_value=rows):
        book = depthfeed_client.get_polymarket_orderbook("market-1")
    assert book["mid"] == 0.40
    assert book["raw_timestamp"] == "2026-11-25 12:00:00"


def test_depthfeed_rejects_wrong_expiry_market_without_fallback():
    rows = [{
        "market_id": "next-window", "time": "2026-11-25 13:00:00",
        "price_up": 0.90, "slug": "btc-updown-15m-next",
    }]
    with patch.object(depthfeed_client, "get_polymarket_snapshots", return_value=rows):
        assert depthfeed_client.get_polymarket_orderbook("current-window") is None


def test_depthfeed_rejects_missing_or_inconsistent_timestamps():
    missing = [{"market_id": "market-1", "price_up": 0.40}]
    with patch.object(depthfeed_client, "get_polymarket_snapshots", return_value=missing):
        assert depthfeed_client.get_polymarket_orderbook("market-1") is None
    inconsistent = [{
        "market_id": "market-1", "time": "2026-11-25 12:00:00",
        "timestamp": "2026-11-25 13:00:00", "price_up": 0.40,
    }]
    with patch.object(depthfeed_client, "get_polymarket_snapshots", return_value=inconsistent):
        assert depthfeed_client.get_polymarket_orderbook("market-1") is None


def test_polymarket_raw_timestamp_preserves_timestamp_only_field():
    rows = [{"market_id": "market-1", "timestamp": "2026-11-25 13:00:00", "price_up": 0.60}]
    with patch.object(depthfeed_client, "get_polymarket_snapshots", return_value=rows):
        book = depthfeed_client.get_polymarket_orderbook("market-1")
    assert book["raw_timestamp"] == "2026-11-25 13:00:00"
    assert book["ts_ms"] == int(datetime(2026, 11, 25, 13, tzinfo=timezone.utc).timestamp() * 1000)


def test_polymarket_raw_timestamp_preserves_ts_only_field():
    rows = [{"market_id": "market-1", "ts": "2026-11-25 13:00:00", "price_up": 0.60}]
    with patch.object(depthfeed_client, "get_polymarket_snapshots", return_value=rows):
        book = depthfeed_client.get_polymarket_orderbook("market-1")
    assert book["raw_timestamp"] == "2026-11-25 13:00:00"
    assert book["ts_ms"] == int(datetime(2026, 11, 25, 13, tzinfo=timezone.utc).timestamp() * 1000)


def test_polymarket_raw_timestamp_uses_time_precedence_when_all_fields_agree():
    rows = [{
        "market_id": "market-1", "time": "2026-11-25 13:00:00",
        "timestamp": 1795611600000, "ts": 1795611600, "price_up": 0.60,
    }]
    with patch.object(depthfeed_client, "get_polymarket_snapshots", return_value=rows):
        book = depthfeed_client.get_polymarket_orderbook("market-1")
    assert book["raw_timestamp"] == "2026-11-25 13:00:00"
    assert book["ts_ms"] == 1795611600000


def test_kalshi_snapshot_requires_identity_and_uses_capture_timestamp():
    rows = [
        {"ticker": "OTHER-MARKET", "timestamp": "2026-11-25 13:00:00", "yes": [], "no": []},
        {"ticker": "KXTEST", "timestamp": "2026-11-25 12:00:00", "yes": [], "no": []},
    ]
    payload = {"content": [{"type": "text", "text": json.dumps(rows)}]}
    with patch.object(depthfeed_client, "_mcp_call", return_value=payload):
        book = depthfeed_client.get_orderbook_snapshot("KXTEST")
    assert book["raw_timestamp"] == "2026-11-25 12:00:00"
    assert book["ts_ms"] == int(datetime(2026, 11, 25, 12, tzinfo=timezone.utc).timestamp() * 1000)
