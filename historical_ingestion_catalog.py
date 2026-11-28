#!/usr/bin/env python3
"""Read-only historical market catalog ingestion; no L2 or order paths.

Live forward capture may use an authenticated official venue WebSocket. The
historical archive has no WS backfill: these endpoints intentionally remain
REST with cursor state, cutoff-compatible archived paths and Retry-After jitter.
"""
import argparse
import hashlib
import json
import random
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import requests

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
POLYMARKET_BASE = "https://gateway.polymarket.us"

MARKET_CATALOG_SCHEMA = {
    "venue": "string", "series": "string", "market_id": "string",
    "event_id": "string", "open_ts": "int64", "close_ts": "int64",
    "asset": "string", "window_start_ts": "int64", "window_end_ts": "int64",
    "price_to_beat": "double", "settlement_value": "double",
    "settlement_ts": "int64", "status": "string", "raw_json": "string",
}
CONTRACT_BAR_SCHEMA = {
    "venue": "string", "series": "string", "market_id": "string", "ts": "int64",
    "open": "double", "high": "double", "low": "double", "close": "double",
    "volume": "double", "raw_json": "string",
}
TRADE_PRINT_SCHEMA = {
    "venue": "string", "series": "string", "market_id": "string",
    "trade_id": "string", "ts": "int64", "price": "double",
    "quantity": "double", "taker_side": "string", "raw_json": "string",
}
INDEX_BAR_SCHEMA = {
    "index_id": "string", "ts": "int64", "open": "double", "high": "double",
    "low": "double", "close": "double", "source": "string", "raw_json": "string",
}
L2_LEVEL_SCHEMA = {
    "venue": "string", "series": "string", "market_id": "string",
    "capture_ts_ns": "int64", "venue_ts_ns": "int64", "side": "string",
    "action": "string", "level_index": "int64", "price": "text",
    "quantity": "text", "transport": "string", "raw_sha256": "string",
    "raw_json": "string",
}
L2_WINDOW_SCHEMA = {
    "venue": "string", "series": "string", "market_id": "string",
    "first_capture_ts_ns": "int64", "last_capture_ts_ns": "int64",
    "eligible": "int64", "gap_reason": "string", "outcome": "string",
    "timestamp_provenance": "string", "raw_sha256": "string", "parquet_sha256": "string",
}
SCHEMAS = {
    "markets_catalog": MARKET_CATALOG_SCHEMA,
    "contract_bars": CONTRACT_BAR_SCHEMA,
    "trade_prints": TRADE_PRINT_SCHEMA,
    "underlying_index_bars": INDEX_BAR_SCHEMA,
    "l2_book_levels": L2_LEVEL_SCHEMA,
    "l2_windows": L2_WINDOW_SCHEMA,
}
UNIQUE_KEYS = {
    "markets_catalog": ("venue", "market_id"),
    "contract_bars": ("venue", "market_id", "ts"),
    "trade_prints": ("venue", "trade_id"),
    "underlying_index_bars": ("index_id", "ts"),
    "l2_book_levels": ("venue", "market_id", "capture_ts_ns", "side", "action", "level_index"),
    "l2_windows": ("venue", "market_id"),
}


def unix_ts(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value if abs(value) < 100_000_000_000 else value / 1000)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _number(value):
    if isinstance(value, dict):
        value = value.get("value")
    return None if value is None or value == "" else float(value)


def _asset_from_series(series):
    if series.startswith("KX") and series.endswith("15M"):
        return series[2:-3]
    return series.split("-")[0].upper()


def normalize_kalshi_market(market):
    series = market.get("series_ticker") or market.get("event_ticker", "").split("-")[0]
    return {
        "venue": "kalshi", "series": series, "market_id": market["ticker"],
        "event_id": market.get("event_ticker"), "open_ts": unix_ts(market.get("open_time")),
        "close_ts": unix_ts(market.get("close_time")), "asset": _asset_from_series(series),
        "window_start_ts": unix_ts(market.get("open_time")),
        "window_end_ts": unix_ts(market.get("close_time")),
        "price_to_beat": _number(market.get("floor_strike")),
        "settlement_value": _number(market.get("settlement_value_dollars")),
        "settlement_ts": unix_ts(market.get("settlement_ts")),
        "status": market.get("status"), "raw_json": json.dumps(market, sort_keys=True),
    }


def normalize_polymarket_market(market):
    terms = market.get("assetPriceTerms") or {}
    asset = ((terms.get("asset") or {}).get("symbol")
             or market.get("asset") or market["slug"].split("-")[0]).upper()
    return {
        "venue": "polymarket_us", "series": asset.lower(), "market_id": market["slug"],
        "event_id": market.get("eventSlug"), "open_ts": unix_ts(
            terms.get("windowStart") or market.get("startDate") or market.get("start_time")),
        "close_ts": unix_ts(terms.get("windowEnd") or market.get("endDate")),
        "asset": asset, "window_start_ts": unix_ts(
            terms.get("windowStart") or market.get("startDate") or market.get("start_time")),
        "window_end_ts": unix_ts(terms.get("windowEnd") or market.get("endDate")),
        "price_to_beat": _number(terms.get("strike") or terms.get("priceToBeat")),
        "settlement_value": _number(terms.get("settlementPrice", market.get("settlement"))),
        "settlement_ts": unix_ts(terms.get("settlementTime") or market.get("resolvedAt")),
        "status": market.get("status"), "raw_json": json.dumps(market, sort_keys=True),
    }


class CatalogStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS ingestion_state ("
            "venue TEXT NOT NULL, market_id TEXT NOT NULL, endpoint_type TEXT NOT NULL, "
            "last_cursor TEXT, last_fetched_ts INTEGER NOT NULL, "
            "PRIMARY KEY (venue, market_id, endpoint_type))")
        for table, schema in SCHEMAS.items():
            columns = ", ".join(f"{name} {kind}" for name, kind in schema.items())
            keys = ", ".join(UNIQUE_KEYS[table])
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ({columns}, UNIQUE({keys}))")
        self.connection.commit()

    def _upsert(self, table, rows):
        rows = list(rows)
        if not rows:
            return 0
        names = list(SCHEMAS[table])
        placeholders = ",".join(
            "? || ''" if table == "l2_book_levels" and name in ("price", "quantity")
            else "?" for name in names)
        sql = (f"INSERT OR REPLACE INTO {table} ({','.join(names)}) "
               f"VALUES ({placeholders})")
        chunk_size = 1000 if table == "l2_book_levels" else len(rows)
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start:start + chunk_size]
            with self.connection:
                self.connection.executemany(
                    sql, [[row.get(name) for name in names] for row in chunk])
        return len(rows)

    def upsert_markets(self, rows):
        return self._upsert("markets_catalog", rows)

    def upsert_trades(self, rows):
        return self._upsert("trade_prints", rows)

    def save_cursor(self, venue, market_id, endpoint_type, cursor, fetched_ts):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO ingestion_state "
                "(venue, market_id, endpoint_type, last_cursor, last_fetched_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (venue, market_id, endpoint_type, cursor, int(fetched_ts)))

    def load_cursor(self, venue, market_id, endpoint_type):
        row = self.connection.execute(
            "SELECT last_cursor, last_fetched_ts FROM ingestion_state "
            "WHERE venue=? AND market_id=? AND endpoint_type=?",
            (venue, market_id, endpoint_type)).fetchone()
        return row if row else (None, None)

    def close(self):
        self.connection.close()


def write_parquet_partition(root, rows):
    rows = list(rows)
    if not rows:
        raise ValueError("empty partition")
    groups = {}
    for row in rows:
        ts = row["close_ts"] or row["open_ts"] or row.get("ts")
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        groups.setdefault((row["venue"], row["series"], day), []).append(row)
    paths = []
    for (venue, series, day), group in sorted(groups.items()):
        path = Path(root) / "data" / venue / series / f"{day}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = UNIQUE_KEYS["markets_catalog"]
        unique = {}
        if path.exists():
            unique.update({tuple(row[name] for name in keys): row
                           for row in pq.read_table(path).to_pylist()})
        for row in group:
            unique[tuple(row[name] for name in keys)] = row
        table = pd.DataFrame(list(unique.values()), columns=list(MARKET_CATALOG_SCHEMA))
        tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
        table.to_parquet(tmp, index=False)
        tmp.replace(path)
        paths.append(path)
    return paths


class OfficialCatalogClient:
    def __init__(self, timeout=20.0):
        self.session = requests.Session()
        self.timeout = timeout

    def _get(self, base, path, params):
        for _ in range(3):
            response = self.session.get(base + path, params=params, timeout=self.timeout)
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            retry_after = float(response.headers.get("Retry-After", "1"))
            time.sleep(retry_after + random.uniform(0.0, 0.25))
        raise RuntimeError("rate limit persisted after Retry-After retries")

    def kalshi_pages(self, path, params=None):
        params = dict(params or {})
        seen = set()
        while True:
            data = self._get(KALSHI_BASE, path, params)
            yield data
            cursor = data.get("cursor") or ""
            if not cursor:
                return
            if cursor in seen:
                raise ValueError("repeated Kalshi pagination cursor")
            seen.add(cursor)
            params["cursor"] = cursor

    def polymarket_pages(self, params=None):
        params = dict(params or {})
        seen = set()
        while True:
            data = self._get(POLYMARKET_BASE, "/v1/markets", params)
            yield data
            if data.get("eof") is True:
                return
            cursor = data.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("malformed Polymarket pagination")
            seen.add(cursor)
            params["cursor"] = cursor


def ingest_official_catalog(store, root, kalshi_limit=2, poly_limit=2):
    counts = {}
    for label, path in (("recent", "/markets"), ("archived", "/historical/markets")):
        rows = []
        cursor = None
        for page in OfficialCatalogClient().kalshi_pages(path, {
                "series_ticker": "KXBTC15M", "limit": kalshi_limit}):
            rows.extend(page.get("markets", []))
            cursor = page.get("cursor")
            if len(rows) >= kalshi_limit:
                break
        normalized = [normalize_kalshi_market(row) for row in rows[:kalshi_limit]]
        store.upsert_markets(normalized)
        if normalized:
            write_parquet_partition(root, normalized)
            store.save_cursor("kalshi", normalized[-1]["market_id"], label, cursor, int(time.time()))
        counts[f"kalshi_{label}"] = len(normalized)
    rows = []
    for page in OfficialCatalogClient().polymarket_pages({
            "limit": poly_limit, "active": "true", "closed": "false", "categories": "crypto"}):
        rows.extend(page.get("markets", []))
        if len(rows) >= poly_limit:
            break
    normalized = [normalize_polymarket_market(row) for row in rows[:poly_limit]]
    store.upsert_markets(normalized)
    if normalized:
        write_parquet_partition(root, normalized)
        store.save_cursor("polymarket_us", normalized[-1]["market_id"], "markets", None, int(time.time()))
    counts["polymarket_us"] = len(normalized)
    return counts


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _kalshi_executable(raw):
    book = raw.get("orderbook_fp", raw)
    yes = book.get("yes_dollars") or []
    no = book.get("no_dollars") or []
    return {
        "yes": {"bids": yes, "asks": [[str(Decimal(1) - Decimal(p)), q] for p, q in no]},
        "no": {"bids": no, "asks": [[str(Decimal(1) - Decimal(p)), q] for p, q in yes]},
    }


def _level_rows(venue, series, market_id, capture_ns, venue_ns, executable,
                transport, raw_sha256, raw_json):
    rows = []
    for side in ("yes", "no"):
        for action in ("bids", "asks"):
            for index, (price, quantity) in enumerate(executable[side][action]):
                numeric = Decimal(str(quantity))
                if not numeric.is_finite() or numeric <= 0:
                    raise ValueError("invalid depth")
                rows.append({
                    "venue": venue, "series": series, "market_id": market_id,
                    "capture_ts_ns": capture_ns, "venue_ts_ns": venue_ns,
                    "side": side, "action": action, "level_index": index,
                    "price": str(price), "quantity": str(quantity),
                    "transport": transport, "raw_sha256": raw_sha256,
                    "raw_json": raw_json,
                })
    return rows


def _kalshi_l2_window(directory, max_gap_seconds):
    directory = Path(directory)
    ticker = directory.name
    levels, reasons = [], []
    expected = {}
    manifest = directory / "manifest.sha256"
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            digest, name = line.split("  ", 1)
            expected[name] = digest
    raw_hashes = {}
    for path in sorted(directory.glob("*.jsonl")):
        raw_hashes[path.name] = _digest(path)
        if expected.get(path.name) != raw_hashes[path.name]:
            reasons.append("manifest_mismatch")
    raw_sha256 = hashlib.sha256(json.dumps(raw_hashes, sort_keys=True).encode()).hexdigest()
    book_path = directory / "orderbooks.jsonl"
    records = []
    if book_path.exists():
        records = [line for line in book_path.read_text().splitlines() if line.strip()]
    previous = None
    previous_venue = None
    timestamp_provenance = "capture_only"
    for line in records:
        record = json.loads(line)
        capture_ns = int(record["received_ts_ns"])
        if previous is not None and capture_ns < previous:
            reasons.append("out_of_order")
        if previous is not None and capture_ns - previous > max_gap_seconds * 1_000_000_000:
            reasons.append("capture_gap")
        previous = capture_ns
        venue_ns = None
        if record["raw"].get("sending_ts_ms") is None:
            reasons.append("missing_venue_timestamp")
        else:
            venue_ns = int(record["raw"]["sending_ts_ms"]) * 1_000_000
            timestamp_provenance = "venue+capture"
            if previous_venue is not None and venue_ns < previous_venue:
                reasons.append("venue_out_of_order")
            previous_venue = venue_ns
            if abs(capture_ns - venue_ns) > 2_000_000_000:
                reasons.append("timestamp_mismatch")
        try:
            executable = _kalshi_executable(record["raw"])
            if record.get("executable") != executable:
                reasons.append("complement_mismatch")
                continue
            line_hash = hashlib.sha256(line.encode()).hexdigest()
            levels.extend(_level_rows(
                "kalshi", ticker.split("-")[0], ticker, capture_ns,
                venue_ns, executable, record.get("transport", "rest"), line_hash, line))
        except (ValueError, TypeError, InvalidOperation):
            reasons.append("invalid_quantity")
    outcomes = set()
    settlement_path = directory / "settlements.jsonl"
    if settlement_path.exists():
        for line in settlement_path.read_text().splitlines():
            if line.strip():
                result = json.loads(line).get("raw", {}).get("result")
                if result in ("yes", "no"):
                    outcomes.add(result)
    if len(outcomes) > 1:
        reasons.append("outcome_conflict")
    outcome = outcomes.pop() if len(outcomes) == 1 else None
    if outcome is None:
        reasons.append("missing_outcome")
    if len(records) < 2:
        reasons.append("too_few_records")
    if not levels:
        reasons.append("missing_depth")
    window = {
        "venue": "kalshi", "series": ticker.split("-")[0], "market_id": ticker,
        "first_capture_ts_ns": json.loads(records[0])["received_ts_ns"] if records else None,
        "last_capture_ts_ns": json.loads(records[-1])["received_ts_ns"] if records else None,
        "eligible": int(not reasons), "gap_reason": ",".join(sorted(set(reasons))),
        "outcome": outcome, "timestamp_provenance": timestamp_provenance,
        "raw_sha256": raw_sha256, "parquet_sha256": None,
    }
    return levels, window


def _poly_l2_window(path, settlement_files, max_gap_seconds):
    path = Path(path)
    settlement_hashes = {str(Path(item)): _digest(item) for item in settlement_files}
    raw_sha256 = hashlib.sha256(json.dumps({
        "books": _digest(path), "settlements": settlement_hashes,
    }, sort_keys=True).encode()).hexdigest()
    outcomes_by_market = {}
    for settlement_file in settlement_files:
        for line in Path(settlement_file).read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            outcome = {"1": "yes", "1.0": "yes", "0": "no", "0.0": "no"}.get(
                str(row.get("settlement")))
            if outcome is not None:
                outcomes_by_market.setdefault(row["slug"], set()).add(outcome)
    by_market = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        for market_id, book in (record.get("books") or {}).items():
            by_market.setdefault(market_id, []).append((line, record, book))
    results = []
    for market_id, records in sorted(by_market.items()):
        levels, reasons = [], []
        previous = None
        previous_venue = None
        for line, record, book in records:
            capture_ns = int(round(float(record["ts"]) * 1_000_000_000))
            if previous is not None and capture_ns < previous:
                reasons.append("out_of_order")
            if previous is not None and capture_ns - previous > max_gap_seconds * 1_000_000_000:
                reasons.append("capture_gap")
            previous = capture_ns
            if book.get("updated_at") is None:
                reasons.append("missing_venue_timestamp")
                update_ns = None
            else:
                update_ns = int(round(float(book["updated_at"]) * 1_000_000_000))
                if previous_venue is not None and update_ns < previous_venue:
                    reasons.append("venue_out_of_order")
                previous_venue = update_ns
                if abs(capture_ns - update_ns) > 2_000_000_000:
                    reasons.append("timestamp_mismatch")
            executable = {side: {action: book.get(f"{side}_{action}") or []
                                 for action in ("bids", "asks")}
                          for side in ("yes", "no")}
            try:
                levels.extend(_level_rows(
                    "polymarket_us", market_id.split("-")[1] if "-" in market_id else market_id,
                    market_id, capture_ns, update_ns, executable, "websocket",
                    raw_sha256, line))
            except (ValueError, TypeError, InvalidOperation):
                reasons.append("invalid_quantity")
        market_outcomes = outcomes_by_market.get(market_id, set())
        if len(market_outcomes) > 1:
            reasons.append("outcome_conflict")
        outcome = market_outcomes.pop() if len(market_outcomes) == 1 else None
        if outcome is None:
            reasons.append("missing_outcome")
        if len(records) < 2:
            reasons.append("too_few_records")
        if not levels:
            reasons.append("missing_depth")
        first = records[0] if records else None
        last = records[-1] if records else None
        window = {
            "venue": "polymarket_us",
            "series": market_id.split("-")[1] if "-" in market_id else market_id,
            "market_id": market_id,
            "first_capture_ts_ns": int(round(float(first[1]["ts"]) * 1_000_000_000)) if first else None,
            "last_capture_ts_ns": int(round(float(last[1]["ts"]) * 1_000_000_000)) if last else None,
            "eligible": int(bool(records) and not reasons),
            "gap_reason": ",".join(sorted(set(reasons))),
            "outcome": outcome, "timestamp_provenance": "venue+capture",
            "raw_sha256": raw_sha256, "parquet_sha256": None,
        }
        results.append((levels, window))
    return results


def _atomic_parquet(root, relative, rows, schema):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    if schema is L2_LEVEL_SCHEMA:
        import pyarrow as pa
        writer = pq.ParquetWriter(tmp, pa.schema([
            (name, pa.int64() if kind == "int64" else pa.string())
            for name, kind in schema.items()]))
        try:
            for start in range(0, len(rows), 1000):
                frame = pd.DataFrame(rows[start:start + 1000], columns=list(schema))
                writer.write_table(pa.Table.from_pandas(
                    frame, schema=writer.schema, preserve_index=False))
        finally:
            writer.close()
    else:
        frame = pd.DataFrame(rows, columns=list(schema))
        frame.to_parquet(tmp, index=False)
    tmp.replace(path)
    return path


def ingest_local_l2(store, root, kalshi_dirs=(), poly_files=(), poly_settlement_files=(), max_gap_seconds=7.0):
    """Seal bounded recorded WS captures as locally recorded historical full depth."""
    root = Path(root)
    datasets = []
    for directory in kalshi_dirs:
        datasets.append(_kalshi_l2_window(directory, max_gap_seconds))
    for path in poly_files:
        datasets.extend(_poly_l2_window(path, poly_settlement_files, max_gap_seconds))
    windows = []
    for levels, window in datasets:
        relative = Path("data") / "l2" / window["venue"] / window["series"] / (
            window["market_id"] + ".parquet")
        path = _atomic_parquet(root, relative, levels, L2_LEVEL_SCHEMA)
        window["parquet_sha256"] = _digest(path)
        windows.append(window)
        checkpoint, _ = store.load_cursor(window["venue"], window["market_id"], "local_l2")
        if checkpoint != window["raw_sha256"] and levels:
            store._upsert("l2_book_levels", levels)
        store._upsert("l2_windows", [window])
        if checkpoint != window["raw_sha256"]:
            store.save_cursor(window["venue"], window["market_id"], "local_l2",
                              window["raw_sha256"], int(time.time()))
    windows.sort(key=lambda row: (row["first_capture_ts_ns"] is None,
                                  row["first_capture_ts_ns"] or 0,
                                  row["venue"], row["market_id"]))
    _atomic_parquet(root, Path("data") / "l2_windows.parquet", windows, L2_WINDOW_SCHEMA)
    manifest = {
        "evidence_label": "locally recorded historical full depth",
        "venue_historical_backfill": "unavailable",
        "eligible_windows": sum(row["eligible"] for row in windows),
        "ineligible_windows": len(windows) - sum(row["eligible"] for row in windows),
        "windows": windows,
    }
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    tmp = root / f".manifest.{time.time_ns()}.tmp"
    tmp.write_text(payload)
    tmp.replace(root / "manifest.json")
    return manifest


def execution_book(store, venue, market_id, capture_ts_ns, side, action):
    window = store.connection.execute(
        "SELECT eligible FROM l2_windows WHERE venue=? AND market_id=?",
        (venue, market_id)).fetchone()
    if not window or not window[0]:
        return None
    latest = store.connection.execute(
        "SELECT MAX(capture_ts_ns) FROM l2_book_levels WHERE venue=? AND market_id=? "
        "AND capture_ts_ns<=? AND side=? AND action=?",
        (venue, market_id, int(capture_ts_ns), side, action)).fetchone()[0]
    if latest is None or int(capture_ts_ns) - latest > 1_000_000_000:
        return None
    rows = store.connection.execute(
        "SELECT price, quantity FROM l2_book_levels WHERE venue=? AND market_id=? "
        "AND capture_ts_ns=? AND side=? AND action=? ORDER BY level_index",
        (venue, market_id, latest, side, action)).fetchall()
    if not rows:
        return None
    from execution_realism import Book, Level
    return Book(venue=venue, side=side, ts=float(latest) / 1e9,
                levels=[Level(float(price), float(quantity)) for price, quantity in rows])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--kalshi-limit", type=int, default=2)
    parser.add_argument("--poly-limit", type=int, default=2)
    args = parser.parse_args(argv)
    store = CatalogStore(args.root / "ingestion.db")
    try:
        print(json.dumps(ingest_official_catalog(
            store, args.root, args.kalshi_limit, args.poly_limit), sort_keys=True))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
