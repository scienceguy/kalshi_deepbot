import json
import sqlite3
from pathlib import Path

import pyarrow.parquet as pq
import pytest

import historical_ingestion_catalog as catalog
from historical_ingestion_catalog import (
    CONTRACT_BAR_SCHEMA, INDEX_BAR_SCHEMA, MARKET_CATALOG_SCHEMA, TRADE_PRINT_SCHEMA,
    CatalogStore, OfficialCatalogClient,
    normalize_kalshi_market, normalize_polymarket_market, write_parquet_partition,
)
from execution_realism import walk_book


def test_catalog_store_creates_missing_parent(tmp_path):
    store = CatalogStore(tmp_path / "new" / "nested" / "ingestion.db")
    assert (tmp_path / "new" / "nested").is_dir()
    store.close()


def test_schemas_use_int64_utc_seconds():
    for schema in (MARKET_CATALOG_SCHEMA, CONTRACT_BAR_SCHEMA,
                   TRADE_PRINT_SCHEMA, INDEX_BAR_SCHEMA):
        for name in ("open_ts", "close_ts", "window_start_ts", "window_end_ts",
                     "settlement_ts", "ts"):
            if name in schema:
                assert schema[name] == "int64"


def test_sqlite_upserts_dedup_and_ingestion_cursor(tmp_path):
    store = CatalogStore(tmp_path / "ingestion.db")
    row = normalize_kalshi_market({
        "ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M",
        "open_time": "2026-11-01T00:00:00Z", "close_time": "2026-11-01T00:15:00Z",
        "result": "yes", "settlement_value_dollars": "1.0000",
        "settlement_ts": "2026-11-01T00:16:00Z",
    })
    store.upsert_markets([row, row])
    assert store.connection.execute("SELECT count(*) FROM markets_catalog").fetchone()[0] == 1
    store.upsert_trades([{"venue": "kalshi", "series": "KXBTC15M",
                          "market_id": "KXBTC15M-TEST", "trade_id": "t1", "ts": 1,
                          "price": 0.5, "quantity": 2, "raw_json": "{}"}] * 2)
    assert store.connection.execute("SELECT count(*) FROM trade_prints").fetchone()[0] == 1
    store.save_cursor("kalshi", "KXBTC15M-TEST", "markets", "cursor-1", 100)
    store.save_cursor("kalshi", "KXBTC15M-TEST", "markets", "cursor-2", 200)
    assert store.load_cursor("kalshi", "KXBTC15M-TEST", "markets") == ("cursor-2", 200)
    store.close()


def test_parquet_partition_is_idempotent(tmp_path):
    row = normalize_polymarket_market({
        "slug": "btc-updown-15m-test", "endDate": "2026-11-01T00:15:00Z",
        "assetPriceTerms": {"windowStart": "2026-11-01T00:00:00Z",
                            "windowEnd": "2026-11-01T00:15:00Z",
                            "priceToBeat": {"value": "60000"},
                            "settlementPrice": 1.0},
    })
    paths = write_parquet_partition(tmp_path, [row, row])
    path = tmp_path / "data" / "polymarket_us" / "btc" / "2026-11-01.parquet"
    assert paths == [path]
    write_parquet_partition(tmp_path, [row])
    table = pq.read_table(path)
    assert table.num_rows == 1
    stored = json.loads(table.column("raw_json")[0].as_py())
    assert stored["assetPriceTerms"]["priceToBeat"] == {"value": "60000"}
    assert table.column("window_end_ts")[0].as_py() == 1793492100


def test_parquet_partition_groups_utc_days_without_duplicates(tmp_path):
    rows = [normalize_polymarket_market({
        "slug": f"btc-updown-15m-{day}", "endDate": f"2026-11-{day:02d}T00:15:00Z",
        "assetPriceTerms": {"windowStart": f"2026-11-{day:02d}T00:00:00Z",
                            "windowEnd": f"2026-11-{day:02d}T00:15:00Z",
                            "priceToBeat": {"value": "60000"}},
    }) for day in (1, 2)]
    paths = write_parquet_partition(tmp_path, [rows[0], rows[1], rows[0]])
    assert paths == [tmp_path / "data" / "polymarket_us" / "btc" / f"2026-11-{day:02d}.parquet"
                     for day in (1, 2)]
    assert write_parquet_partition(tmp_path, list(reversed(rows))) == paths
    for path, row in zip(paths, rows):
        table = pq.read_table(path)
        assert table.num_rows == 1
        assert table.column("market_id")[0].as_py() == row["market_id"]


def test_official_pages_retry_after_and_cursor(monkeypatch):
    client = OfficialCatalogClient()
    responses = []
    class Response:
        def __init__(self, status, payload, headers=None):
            self.status_code = status
            self.payload = payload
            self.headers = headers or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)
        def json(self):
            return self.payload
    calls = []
    def get(url, params, timeout):
        calls.append((url, params))
        if len(calls) == 1:
            return Response(429, {}, {"Retry-After": "2"})
        if len(calls) == 2:
            return Response(200, {"markets": [{"ticker": "T"}], "cursor": "next"})
        return Response(200, {"markets": [], "cursor": ""})
    monkeypatch.setattr(client.session, "get", get)
    sleeps = []
    monkeypatch.setattr("historical_ingestion_catalog.time.sleep", sleeps.append)
    monkeypatch.setattr("historical_ingestion_catalog.random.uniform", lambda a, b: 0.25)
    pages = list(client.kalshi_pages("/markets", {"series_ticker": "KXBTC15M"}))
    assert len(pages) == 2
    assert sleeps == [2.25]
    assert calls[-1][1]["cursor"] == "next"


def _write_kalshi_window(root, ticker="KXBTC15M-TEST", empty=False,
                         venue_timestamps=True):
    directory = root / ticker
    directory.mkdir(parents=True)
    raw = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
    executable = {"yes": {"bids": [], "asks": []}, "no": {"bids": [], "asks": []}}
    if not empty:
        raw = {"orderbook_fp": {"yes_dollars": [["0.4000", "7.25"]],
                                "no_dollars": [["0.3500", "12.50"]]}}
        executable = {
            "yes": {"bids": [["0.4000", "7.25"]], "asks": [["0.6500", "12.50"]]},
            "no": {"bids": [["0.3500", "12.50"]], "asks": [["0.6000", "7.25"]]},
        }
    if venue_timestamps:
        raw["sending_ts_ms"] = 0
    record = {"schema_version": 1, "kind": "orderbooks", "ticker": ticker,
              "received_ts": "2026-11-27T04:50:20Z", "received_ts_ns": 1_000,
              "interpolated": False, "raw": raw, "request": {"depth": 0},
              "executable": executable, "transport": "websocket"}
    if not empty:
        second = dict(record, received_ts="2026-11-27T04:50:22Z", received_ts_ns=2_000)
        (directory / "orderbooks.jsonl").write_text(
            json.dumps(record, separators=(",", ":")) + "\n"
            + json.dumps(second, separators=(",", ":")) + "\n")
        settlement = dict(record, kind="settlements", raw={"ticker": ticker, "result": "yes",
                                                            "settlement_ts": "2026-11-27T04:51:00Z"})
        (directory / "settlements.jsonl").write_text(json.dumps(settlement) + "\n")
    manifest = ""
    import hashlib
    for name in ("orderbooks.jsonl", "settlements.jsonl"):
        path = directory / name
        if path.exists():
            manifest += hashlib.sha256(path.read_bytes()).hexdigest() + "  " + name + "\n"
    (directory / "manifest.sha256").write_text(manifest)
    return directory


def _write_poly_settlements(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n" if rows else "")
    return path


def test_sealed_past_ws_captures_are_historical_depth_and_missing_is_ineligible(tmp_path):
    kalshi = _write_kalshi_window(tmp_path / "kalshi")
    missing = _write_kalshi_window(tmp_path / "missing", ticker="KXBTC15M-MISSING", empty=True)
    poly = tmp_path / "poly.jsonl"
    poly_record = {"ts": 100.25, "books": {"btc-test": {
        "updated_at": 100.25, "yes_bids": [[0.83, 3.0]], "yes_asks": [[0.84, 2.0]],
        "no_bids": [], "no_asks": [], "result": "yes"}}}
    second = {"ts": 101.25, "books": {"btc-test": dict(poly_record["books"]["btc-test"],
                                                        updated_at=101.25)}}
    poly.write_text(json.dumps(poly_record) + "\n" + json.dumps(second) + "\n")
    settlements = _write_poly_settlements(
        tmp_path / "poly_settlements.jsonl", [{"slug": "btc-test", "settlement": 1.0}])
    ingest_local_l2 = getattr(catalog, "ingest_local_l2", None)
    execution_book = getattr(catalog, "execution_book", None)
    assert callable(ingest_local_l2) and callable(execution_book)
    store = CatalogStore(tmp_path / "out" / "ingestion.db")
    result = ingest_local_l2(store, tmp_path / "out", [kalshi, missing], [poly], [settlements])
    assert result["evidence_label"] == "locally recorded historical full depth"
    assert result["eligible_windows"] == 2
    assert result["ineligible_windows"] == 1

    first = execution_book(store, "kalshi", "KXBTC15M-TEST", 1_000, "yes", "asks")
    second = execution_book(store, "polymarket_us", "btc-test", 100_250_000_000,
                            "yes", "asks")
    assert walk_book(first, 1.0).levels_used == 1
    assert walk_book(second, 1.0).levels_used == 1
    assert execution_book(store, "kalshi", "KXBTC15M-TEST", 2_000, "yes", "asks") is not None
    assert execution_book(store, "kalshi", "KXBTC15M-MISSING", 2_000, "yes", "asks") is None

    levels = store.connection.execute(
        "SELECT price, quantity, raw_sha256, transport FROM l2_book_levels "
        "WHERE venue='kalshi' AND side='yes' AND action='asks'").fetchall()
    assert levels[0][0:2] == ("0.6500", "12.50")
    assert levels[0][2] and levels[0][3] == "websocket"
    poly_levels = store.connection.execute(
        "SELECT price, quantity, venue_ts_ns FROM l2_book_levels "
        "WHERE venue='polymarket_us' AND side='yes' AND action='asks'").fetchall()
    assert poly_levels[0][0:2] == ("0.84", "2.0")
    assert poly_levels[0][2] == 100_250_000_000
    parquet_files = list((tmp_path / "out" / "data").rglob("*.parquet"))
    assert parquet_files
    first_manifest = (tmp_path / "out" / "manifest.json").read_bytes()
    ingest_local_l2(store, tmp_path / "out", [kalshi, missing], [poly], [settlements])
    assert (tmp_path / "out" / "manifest.json").read_bytes() == first_manifest
    assert store.connection.execute("SELECT count(*) FROM l2_book_levels").fetchone()[0] == 12
    store.close()


def test_execution_book_exact_bounded_previous_prefirst_and_stale(tmp_path):
    execution_book = catalog.execution_book
    ingest_local_l2 = catalog.ingest_local_l2
    kalshi = _write_kalshi_window(tmp_path / "kalshi")
    poly = _write_poly_rows(tmp_path / "poly.jsonl", _poly_rows())
    settlements = _write_poly_settlements(
        tmp_path / "poly_settlements.jsonl", [{"slug": "btc-test", "settlement": 1.0}])
    store = CatalogStore(tmp_path / "out" / "ingestion.db")
    ingest_local_l2(store, tmp_path / "out", [kalshi], [poly], [settlements])
    assert execution_book(store, "kalshi", "KXBTC15M-TEST", 1_000, "yes", "asks") is not None
    assert execution_book(store, "kalshi", "KXBTC15M-TEST", 1_500, "yes", "asks").ts == 1e-6
    assert execution_book(store, "kalshi", "KXBTC15M-TEST", 999, "yes", "asks") is None
    assert execution_book(store, "kalshi", "KXBTC15M-TEST", 3_000_000_001, "yes", "asks") is None
    assert execution_book(store, "polymarket_us", "btc-test", 100_250_000_000,
                          "yes", "asks") is not None
    assert execution_book(store, "polymarket_us", "btc-test", 100_750_000_000,
                          "yes", "asks").ts == 100.25
    assert execution_book(store, "polymarket_us", "btc-test", 100_240_000_000,
                          "yes", "asks") is None
    assert execution_book(store, "polymarket_us", "btc-test", 102_260_000_000,
                          "yes", "asks") is None
    store.close()


def _poly_rows(ts_values=(100.25, 101.25), result="yes", quantity=2.0,
               updated_values=None, empty=False):
    rows = []
    for index, ts in enumerate(ts_values):
        updated = ts if updated_values is None else updated_values[index]
        book = {"updated_at": updated, "yes_bids": [], "yes_asks": [],
                "no_bids": [], "no_asks": [], "result": result}
        if not empty:
            book["yes_bids"] = [[0.83, 3.0]]
            book["yes_asks"] = [[0.84, quantity]]
        rows.append({"ts": ts, "books": {"btc-test": book}})
    return rows


def _write_poly_rows(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _poly_window_result(tmp_path, rows, settlements=None):
    store = CatalogStore(tmp_path / "out" / "ingestion.db")
    path = _write_poly_rows(tmp_path / "poly.jsonl", rows)
    if settlements is None:
        settlements = []
        for row in rows:
            result = row["books"]["btc-test"].get("result")
            if result in ("yes", "up"):
                settlements.append({"slug": "btc-test", "settlement": 1.0})
            elif result in ("no", "down"):
                settlements.append({"slug": "btc-test", "settlement": 0.0})
    settlement_path = _write_poly_settlements(tmp_path / "settlements.jsonl", settlements)
    result = catalog.ingest_local_l2(
        store, tmp_path / "out", [], [path], [settlement_path])
    window = result["windows"][0]
    store.close()
    return window


def test_poly_capture_gap_is_ineligible(tmp_path):
    window = _poly_window_result(tmp_path, _poly_rows((100.0, 110.0)))
    assert not window["eligible"] and "capture_gap" in window["gap_reason"]


def test_poly_out_of_order_is_ineligible(tmp_path):
    window = _poly_window_result(tmp_path, _poly_rows((101.0, 100.0)))
    assert not window["eligible"] and "out_of_order" in window["gap_reason"]


def test_poly_timestamp_mismatch_is_ineligible(tmp_path):
    window = _poly_window_result(tmp_path, _poly_rows(updated_values=(50.0, 50.0)))
    assert not window["eligible"] and "timestamp_mismatch" in window["gap_reason"]


def test_poly_nonmonotonic_venue_timestamp_is_ineligible(tmp_path):
    window = _poly_window_result(
        tmp_path, _poly_rows(updated_values=(101.25, 100.25)))
    assert not window["eligible"] and "venue_out_of_order" in window["gap_reason"]


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "bad", 0, -1])
def test_poly_invalid_quantity_is_ineligible(tmp_path, quantity):
    window = _poly_window_result(tmp_path, _poly_rows(quantity=quantity))
    assert not window["eligible"] and "invalid_quantity" in window["gap_reason"]


@pytest.mark.parametrize("result", [None, "maybe"])
def test_poly_missing_or_unrecognized_outcome_is_ineligible(tmp_path, result):
    window = _poly_window_result(tmp_path, _poly_rows(result=result))
    assert not window["eligible"] and "missing_outcome" in window["gap_reason"]


def test_poly_conflicting_outcomes_are_ineligible(tmp_path):
    window = _poly_window_result(tmp_path, _poly_rows(result="yes")
                                 + _poly_rows(result="no"))
    assert not window["eligible"] and "outcome_conflict" in window["gap_reason"]


def test_poly_too_few_records_is_ineligible(tmp_path):
    window = _poly_window_result(tmp_path, _poly_rows((100.25,)))
    assert not window["eligible"] and "too_few_records" in window["gap_reason"]


def test_poly_empty_book_is_ineligible(tmp_path):
    window = _poly_window_result(tmp_path, _poly_rows(empty=True))
    assert not window["eligible"] and "missing_depth" in window["gap_reason"]


def test_kalshi_unsealed_manifest_is_ineligible(tmp_path):
    directory = _write_kalshi_window(tmp_path / "kalshi")
    (directory / "manifest.sha256").write_text("0" * 64 + "  orderbooks.jsonl\n")
    store = CatalogStore(tmp_path / "out" / "ingestion.db")
    result = catalog.ingest_local_l2(store, tmp_path / "out", [directory], [])
    assert not result["windows"][0]["eligible"]
    assert "manifest_mismatch" in result["windows"][0]["gap_reason"]
    store.close()


def test_kalshi_missing_venue_timestamp_is_ineligible(tmp_path):
    directory = _write_kalshi_window(tmp_path / "kalshi", venue_timestamps=False)
    store = CatalogStore(tmp_path / "out" / "ingestion.db")
    result = catalog.ingest_local_l2(store, tmp_path / "out", [directory], [])
    assert not result["windows"][0]["eligible"]
    assert "missing_venue_timestamp" in result["windows"][0]["gap_reason"]
    store.close()


def test_poly_book_result_does_not_replace_official_settlement(tmp_path):
    window = _poly_window_result(tmp_path, _poly_rows(result="yes"), settlements=[])
    assert not window["eligible"]
    assert "missing_outcome" in window["gap_reason"]


def test_poly_official_settlement_is_joined_by_market(tmp_path):
    window = _poly_window_result(
        tmp_path, _poly_rows(result="yes"),
        settlements=[{"slug": "btc-test", "settlement": 0.0}])
    assert window["eligible"]
    assert window["outcome"] == "no"


def test_poly_conflicting_official_settlements_are_ineligible(tmp_path):
    window = _poly_window_result(
        tmp_path, _poly_rows(result="yes"),
        settlements=[{"slug": "btc-test", "settlement": 0.0},
                     {"slug": "btc-test", "settlement": 1.0}])
    assert not window["eligible"]
    assert "outcome_conflict" in window["gap_reason"]
