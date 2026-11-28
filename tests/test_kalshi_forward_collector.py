import hashlib
import json
import shutil
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.kalshi_forward_collector as collector_module
from scripts.kalshi_forward_collector import (
    ForwardCollector, WebSocketCollector, WindowWriter, append_trades,
    executable_book, snapshot_to_raw, validate_dataset,
)


def _raw_book():
    return {"orderbook_fp": {
        "yes_dollars": [["0.123456", "2.50"]],
        "no_dollars": [["0.765432", "4.25"]],
    }}


def test_ws_snapshot_preserves_dollar_strings_and_writes_full_book(tmp_path):
    snapshot = type("Snapshot", (), {
        "ticker": "KXBTC15M-TEST",
        "yes_levels": [["0.123456", "2.50"]],
        "no_levels": [["0.765432", "4.25"]],
    })()
    raw = snapshot_to_raw(snapshot)
    assert raw == {"orderbook_fp": {
        "yes_dollars": [["0.123456", "2.50"]],
        "no_dollars": [["0.765432", "4.25"]],
    }}
    collector = WebSocketCollector.__new__(WebSocketCollector)
    collector.writers = {"KXBTC15M-TEST": WindowWriter(tmp_path, "KXBTC15M-TEST")}
    collector._on_snapshot(snapshot)
    stored = json.loads((collector.writers[snapshot.ticker].directory / "orderbooks.jsonl").read_text())
    assert stored["raw"] == raw
    assert stored["transport"] == "websocket"


def test_ordinary_delta_does_not_request_full_resync():
    collector = WebSocketCollector.__new__(WebSocketCollector)
    collector.ws = MagicMock()
    collector.ws._loop = object()

    collector._on_delta(type("Delta", (), {"ticker": "KXBTC15M-TEST"})())

    collector.ws._request_orderbook_resync.assert_not_called()


@pytest.mark.parametrize("result", [None, "", "pending", "undetermined"])
def test_missing_or_empty_outcome_remains_unsettled(tmp_path, result):
    ticker = "KXBTC15M-TEST"
    collector = ForwardCollector.__new__(ForwardCollector)
    collector.closes = {ticker: 0.0}
    collector.settled = set()
    collector.writers = {ticker: WindowWriter(tmp_path, ticker)}
    collector.client = type("Client", (), {"get": lambda self, path, **params: {
        "market": {"ticker": ticker, "result": result, "expiration_value": ""}
    }})()

    collector.capture_settlements()

    assert ticker not in collector.settled
    assert not (collector.writers[ticker].directory / "settlements.jsonl").exists()


def test_executable_depth_uses_exact_decimal_complements():
    book = executable_book(_raw_book())
    assert book == {
        "yes": {"bids": [["0.123456", "2.50"]],
                "asks": [["0.234568", "4.25"]]},
        "no": {"bids": [["0.765432", "4.25"]],
               "asks": [["0.876544", "2.50"]]},
    }


def test_window_manifest_tracks_appended_raw_records(tmp_path):
    writer = WindowWriter(tmp_path, "KXBTC15M-TEST", {"ticker": "KXBTC15M-TEST"})
    writer.append("orderbooks", _raw_book(), executable=executable_book(_raw_book()))
    manifest = (writer.directory / "manifest.sha256").read_text().splitlines()
    target = writer.directory / "orderbooks.jsonl"
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert f"{expected}  orderbooks.jsonl" in manifest
    record = json.loads(target.read_text())
    assert record["raw"] == _raw_book()
    assert record["received_ts_ns"] > 0


def test_trade_append_is_idempotent_across_writer_restarts(tmp_path):
    trade = {"trade_id": "trade-1", "ticker": "KXBTC15M-TEST"}
    first = WindowWriter(tmp_path, "KXBTC15M-TEST")
    assert append_trades(first, [trade]) == 1
    second = WindowWriter(tmp_path, "KXBTC15M-TEST")
    assert append_trades(second, [trade]) == 0
    assert append_trades(second, [{"trade_id": "trade-2"}]) == 1
    target = first.directory / "trades.jsonl"
    assert len(target.read_text().splitlines()) == 2
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert f"{expected}  trades.jsonl" in (first.directory / "manifest.sha256").read_text()


def test_trade_batch_refreshes_manifest_once(tmp_path, monkeypatch):
    writer = WindowWriter(tmp_path, "KXBTC15M-TEST")
    refreshes = []
    monkeypatch.setattr(writer, "refresh_manifest", lambda: refreshes.append(1))
    trades = [{"trade_id": f"trade-{i}", "ticker": "KXBTC15M-TEST"}
              for i in range(100)]
    assert append_trades(writer, trades) == 100
    assert len(refreshes) == 1


def test_repeated_appends_do_not_reread_jsonl_or_rewrite_prior_bytes(tmp_path, monkeypatch):
    writer = WindowWriter(tmp_path, "KXBTC15M-TEST")
    assert append_trades(writer, [{"trade_id": "trade-1"}]) == 1
    target = writer.directory / "trades.jsonl"
    original_bytes = target.read_bytes()

    def unexpected_read(*args, **kwargs):
        raise AssertionError("full-file read during append")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    monkeypatch.setattr(Path, "read_text", unexpected_read)
    assert append_trades(writer, [{"trade_id": "trade-1"}, {"trade_id": "trade-2"}]) == 1
    assert append_trades(writer, [{"trade_id": "trade-2"}]) == 0
    writer.append("incidents", {"error": "test"})

    assert target.open("rb").read().startswith(original_bytes)
    manifest = (writer.directory / "manifest.sha256").open().read()
    assert f"{hashlib.sha256(target.open('rb').read()).hexdigest()}  trades.jsonl" in manifest


def test_window_byte_cap_rejects_entire_batch(tmp_path, monkeypatch):
    writer = WindowWriter(tmp_path, "KXBTC15M-TEST")
    monkeypatch.setattr(collector_module, "MAX_WINDOW_BYTES", 1, raising=False)
    trades = [{"trade_id": "trade-1"}, {"trade_id": "trade-2"}]

    with pytest.raises(collector_module.CapacityError, match="window byte cap"):
        append_trades(writer, trades)

    assert not (writer.directory / "trades.jsonl").exists()
    assert not (writer.directory / "manifest.sha256").exists()
    monkeypatch.setattr(collector_module, "MAX_WINDOW_BYTES", 1_000_000)
    assert append_trades(writer, trades) == 2


def test_free_space_reserve_rejects_entire_batch(tmp_path, monkeypatch):
    writer = WindowWriter(tmp_path, "KXBTC15M-TEST")
    monkeypatch.setattr(collector_module, "MIN_FREE_SPACE_BYTES", 100, raising=False)
    usage = type("Usage", (), {"free": 99})
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage())

    with pytest.raises(collector_module.CapacityError, match="free-space reserve"):
        writer.append_many("trades", [{"trade_id": "trade-1"}, {"trade_id": "trade-2"}])

    assert not (writer.directory / "trades.jsonl").exists()


def test_second_writer_fails_closed_without_corrupting_existing_data(tmp_path, monkeypatch):
    entered = threading.Event()
    usage = type("Usage", (), {"free": 10**12})
    monkeypatch.setattr(shutil, "disk_usage", lambda path: (entered.set(), usage)[1])
    first = WindowWriter(tmp_path, "KXBTC15M-TEST")
    second = WindowWriter(tmp_path, "KXBTC15M-TEST")
    first_error = []

    def append_first():
        try:
            first.append("trades", {"trade_id": "first"})
        except Exception as error:
            first_error.append(error)

    thread = threading.Thread(target=append_first)
    thread.start()
    assert entered.wait(1.0)
    second_error = None
    try:
        second.append("trades", {"trade_id": "second"})
    except Exception as error:
        second_error = error
    thread.join(2.0)

    assert isinstance(second_error, collector_module.CapacityError)
    assert "writer active" in str(second_error)
    assert not first_error
    assert thread.is_alive() is False
    target = first.directory / "trades.jsonl"
    records = target.read_text().splitlines()
    assert len(records) == 1
    assert json.loads(records[0])["raw"]["trade_id"] == "first"
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert f"{expected}  trades.jsonl" in (first.directory / "manifest.sha256").read_text()


def test_sequential_stale_writer_fails_without_corrupting_manifest(tmp_path):
    first = WindowWriter(tmp_path, "KXBTC15M-TEST")
    second = WindowWriter(tmp_path, "KXBTC15M-TEST")
    first.append("trades", {"trade_id": "first"})

    with pytest.raises(collector_module.CapacityError, match="stale dataset writer"):
        second.append("trades", {"trade_id": "second"})

    target = first.directory / "trades.jsonl"
    records = target.read_text().splitlines()
    assert [json.loads(line)["raw"]["trade_id"] for line in records] == ["first"]
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    assert f"{expected}  trades.jsonl" in (first.directory / "manifest.sha256").read_text()


def test_capacity_failure_releases_writer_lock(tmp_path, monkeypatch):
    writer = WindowWriter(tmp_path, "KXBTC15M-TEST")
    monkeypatch.setattr(collector_module, "MAX_WINDOW_BYTES", 1, raising=False)
    with pytest.raises(collector_module.CapacityError):
        writer.append("trades", {"trade_id": "rejected"})

    monkeypatch.setattr(collector_module, "MAX_WINDOW_BYTES", 1_000_000)
    assert writer.append("trades", {"trade_id": "accepted"}) is None
    assert (writer.directory / "trades.jsonl").exists()


def test_validator_accepts_contiguous_fresh_capture(tmp_path):
    writer = WindowWriter(tmp_path, "KXBTC15M-TEST")
    writer.append("orderbooks", _raw_book(), executable=executable_book(_raw_book()))
    result = validate_dataset(tmp_path, expected_interval=60.0)
    assert result["ok"] is True
    assert result["windows"][0]["orderbooks"] == 1
    assert result["windows"][0]["gaps"] == []
    assert result["windows"][0]["interpolated_records"] == 0


def test_validator_rejects_polling_gap_and_manifest_mismatch(tmp_path):
    writer = WindowWriter(tmp_path, "KXBTC15M-TEST")
    path = writer.directory / "orderbooks.jsonl"
    records = []
    for ts_ns in (1_000_000_000, 31_000_000_000):
        record = {"received_ts": "2026-11-25T00:00:00Z", "received_ts_ns": ts_ns,
                  "ticker": "KXBTC15M-TEST", "interpolated": False,
                  "raw": _raw_book(), "executable": executable_book(_raw_book())}
        records.append(json.dumps(record))
    path.write_text("\n".join(records) + "\n")
    writer.refresh_manifest()
    result = validate_dataset(tmp_path, expected_interval=2.0)
    assert result["ok"] is False
    assert result["windows"][0]["gaps"] == [30.0]

    with path.open("a") as handle:
        handle.write("\n")
    result = validate_dataset(tmp_path, expected_interval=2.0)
    assert "orderbooks.jsonl" in result["windows"][0]["hash_mismatches"]
