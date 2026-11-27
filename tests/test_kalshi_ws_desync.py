import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi_ws import KalshiWebSocket

class TestKalshiWebSocketSeqGapResync:
    """T19-16: orderbook seq gap detection and get_snapshot resync."""

    @pytest.fixture
    def ws_client(self, monkeypatch):
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_ID", "test-key-id")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_KEY_FILE", "/tmp/test.pem")
        monkeypatch.setattr("kalshi_ws.config.KALSHI_PRIVATE_KEY_CONTENT", None)
        with patch("kalshi_ws.Path.exists", return_value=False):
            ws = KalshiWebSocket()
        ws._privkey = MagicMock()
        ws._privkey.sign.return_value = b"fake-sig"
        ws._subscriptions["orderbook_delta"] = {"KXBTC15M-TEST"}
        return ws

    def _delta(self, seq):
        return {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": seq,
            "msg": {"market_ticker": "KXBTC15M-TEST", "yes": [], "no": []},
        }

    def _snapshot(self, seq):
        return {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": seq,
            "msg": {"market_ticker": "KXBTC15M-TEST", "yes": [], "no": []},
        }

    def test_contiguous_seq_no_gap(self, ws_client):
        import asyncio
        asyncio.run(ws_client._handle_orderbook_delta(self._delta(1)))
        asyncio.run(ws_client._handle_orderbook_delta(self._delta(2)))
        assert ws_client._ob_gaps_detected == 0
        assert ws_client.orderbook_desynced() is False

    def test_gap_marks_desynced_and_requests_resync(self, ws_client):
        import asyncio
        sent = []
        ws_client._ws = MagicMock()

        async def fake_resync():
            sent.append(True)
        ws_client._request_orderbook_resync = fake_resync

        asyncio.run(ws_client._handle_orderbook_delta(self._delta(1)))
        asyncio.run(ws_client._handle_orderbook_delta(self._delta(5)))  # gap 2-4
        assert ws_client._ob_gaps_detected == 1
        assert ws_client.orderbook_desynced() is True
        assert sent  # resync requested

    def test_resync_uses_official_update_subscription_command(self, ws_client):
        import asyncio
        ws_client._ws = AsyncMock()
        ws_client._ob_sid = 7

        asyncio.run(ws_client._request_orderbook_resync())

        message = json.loads(ws_client._ws.send.await_args.args[0])
        assert message == {
            "cmd": "update_subscription",
            "params": {
                "sid": 7,
                "action": "get_snapshot",
                "market_tickers": ["KXBTC15M-TEST"],
            },
        }

    def test_repeated_gaps_cannot_flood_resync_requests(self, ws_client):
        import asyncio
        ws_client._ws = AsyncMock()
        ws_client._ob_sid = 7

        for seq in (1, 5, 9, 13):
            asyncio.run(ws_client._handle_orderbook_delta(self._delta(seq)))

        assert ws_client._ws.send.await_count == 1
        ws_client._ob_resync_inflight = True
        asyncio.run(ws_client._request_orderbook_resync())
        assert ws_client._ws.send.await_count == 1

    def test_snapshot_resets_desync(self, ws_client):
        import asyncio
        ws_client._request_orderbook_resync = MagicMock()
        asyncio.run(ws_client._handle_orderbook_delta(self._delta(1)))
        asyncio.run(ws_client._handle_orderbook_delta(self._delta(9)))  # gap
        assert ws_client.orderbook_desynced() is True
        asyncio.run(ws_client._handle_orderbook_snapshot(self._snapshot(9)))
        assert ws_client.orderbook_desynced() is False
        # Deltas after the snapshot validate against the snapshot seq.
        asyncio.run(ws_client._handle_orderbook_delta(self._delta(10)))
        assert ws_client._ob_gaps_detected == 1  # no new gap

    def test_two_market_gap_withholds_only_affected_deltas_until_each_snapshot(self, ws_client):
        import asyncio
        emitted = []
        ws_client.set_orderbook_delta_callback(emitted.append)
        ws_client._subscriptions["orderbook_delta"] = {"MARKET-A", "MARKET-B"}
        ws_client._request_orderbook_resync = MagicMock()

        def delta(ticker, seq):
            return {"type": "orderbook_delta", "sid": 1, "seq": seq,
                    "msg": {"market_ticker": ticker, "yes": [], "no": []}}

        def snapshot(ticker, seq):
            return {"type": "orderbook_snapshot", "sid": 1, "seq": seq,
                    "msg": {"market_ticker": ticker, "yes_dollars_fp": [], "no_dollars_fp": []}}

        asyncio.run(ws_client._handle_orderbook_delta(delta("MARKET-A", 1)))
        asyncio.run(ws_client._handle_orderbook_delta(delta("MARKET-B", 2)))
        asyncio.run(ws_client._handle_orderbook_delta(delta("MARKET-A", 4)))  # A gap at seq 3
        assert ws_client.orderbook_desynced() is True
        asyncio.run(ws_client._handle_orderbook_delta(delta("MARKET-B", 5)))
        asyncio.run(ws_client._handle_orderbook_delta(delta("MARKET-A", 6)))
        assert [item.ticker for item in emitted] == [
            "MARKET-A", "MARKET-B", "MARKET-B",
        ]  # A's gapped and still-desynced deltas are withheld
        asyncio.run(ws_client._handle_orderbook_snapshot(snapshot("MARKET-B", 7)))
        assert ws_client.orderbook_desynced() is True  # A still has no fresh snapshot
        asyncio.run(ws_client._handle_orderbook_delta(delta("MARKET-B", 8)))
        asyncio.run(ws_client._handle_orderbook_snapshot(snapshot("MARKET-A", 9)))
        assert ws_client.orderbook_desynced() is False
        asyncio.run(ws_client._handle_orderbook_delta(delta("MARKET-A", 10)))

        assert [item.ticker for item in emitted] == [
            "MARKET-A", "MARKET-B", "MARKET-B", "MARKET-B", "MARKET-A",
        ]

    def test_resubscribe_resets_seq_baseline(self, ws_client):
        import asyncio
        asyncio.run(ws_client._handle_orderbook_delta(self._delta(1)))
        # A resubscribe (new connection/subscription) resets the baseline.
        asyncio.run(ws_client._handle_message(json.dumps({
            "type": "subscribed",
            "msg": {"channel": "orderbook_delta", "sid": 2},
        })))
        assert ws_client._ob_sid == 2
        assert ws_client._ob_last_seq is None
        assert ws_client.orderbook_desynced() is False
        # First delta of the new subscription must not be read as a gap.
        asyncio.run(ws_client._handle_orderbook_delta({
            "type": "orderbook_delta", "sid": 2, "seq": 1,
            "msg": {"market_ticker": "KXBTC15M-TEST", "yes": [], "no": []},
        }))
        assert ws_client._ob_gaps_detected == 0
