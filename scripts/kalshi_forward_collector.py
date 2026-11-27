#!/usr/bin/env python3
"""Read-only forward capture from unsigned official Kalshi public REST APIs."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "local_data/research/us-datasets/kalshi-alpha-forward"
SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M"}
ONE = Decimal("1")
MAX_WINDOW_BYTES = 64 * 1024 * 1024
MIN_FREE_SPACE_BYTES = 1024 * 1024 * 1024


class CapacityError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def official_outcome(market):
    result = market.get("result")
    return result if result in ("yes", "no") else None


def _levels(raw, complement=False):
    result = []
    for price, quantity in raw:
        result.append([str(ONE - Decimal(price)) if complement else price, quantity])
    return result


def executable_book(raw):
    """Derive executable bids/asks without changing source strings or rounding."""
    book = raw.get("orderbook_fp", raw)
    yes = book.get("yes_dollars") or []
    no = book.get("no_dollars") or []
    return {
        "yes": {"bids": _levels(yes), "asks": _levels(no, complement=True)},
        "no": {"bids": _levels(no), "asks": _levels(yes, complement=True)},
    }


def _atomic_write(path, content):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


@contextmanager
def _dataset_writer_lock(path):
    with path.open("w") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CapacityError("dataset writer active") from error
        except OSError as error:
            raise CapacityError(f"dataset writer lock unavailable: {error}") from error
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


class WindowWriter:
    def __init__(self, root, ticker, metadata=None):
        if not re.fullmatch(r"[A-Z0-9-]+", ticker):
            raise ValueError("unsafe ticker")
        self.ticker = ticker
        self.directory = Path(root) / "windows" / ticker
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = Path(root) / ".writer.lock"
        self._hash_states = {}
        self._trade_ids = set()
        for path in sorted(self.directory.glob("*.jsonl")):
            content = path.read_bytes()
            self._hash_states[path.name] = hashlib.sha256(content)
            if path.name == "trades.jsonl":
                for line in content.splitlines():
                    if line.strip():
                        self._trade_ids.add(json.loads(line)["raw"].get("trade_id"))
        if metadata is not None and not (self.directory / "metadata.jsonl").exists():
            self.append("metadata", metadata)

    def append(self, kind, raw, executable=None, transport=None):
        self.append_many(kind, [raw], executable=executable, transport=transport)

    def append_many(self, kind, raws, executable=None, transport=None):
        with _dataset_writer_lock(self.lock_path):
            self._append_many_locked(kind, raws, executable=executable, transport=transport)

    def _append_many_locked(self, kind, raws, executable=None, transport=None):
        if kind not in {"metadata", "orderbooks", "trades", "settlements", "incidents"}:
            raise ValueError("unknown record kind")
        raws = list(raws)
        if not raws:
            return
        path = self.directory / f"{kind}.jsonl"
        encoded = []
        for raw in raws:
            record = {"schema_version": 1, "kind": kind, "ticker": self.ticker,
                      "received_ts": utc_now(), "received_ts_ns": time.time_ns(),
                      "interpolated": False, "raw": raw}
            if executable is not None:
                record["request"] = {"depth": 0}
                record["executable"] = executable
            if transport is not None:
                record["transport"] = transport
            encoded.append((json.dumps(record, separators=(",", ":")) + "\n").encode())
        payload = b"".join(encoded)
        names = sorted(set(self._hash_states) | {path.name})
        manifest_size = sum(64 + 2 + len(name) + 1 for name in names)
        manifest_path = self.directory / "manifest.sha256"
        old_manifest_size = manifest_path.stat().st_size if manifest_path.exists() else 0
        growth = len(payload) + manifest_size - old_manifest_size
        current_size = sum(item.stat().st_size for item in self.directory.iterdir()
                           if item.is_file())
        if current_size + growth > MAX_WINDOW_BYTES:
            raise CapacityError(f"window byte cap exceeded for {self.ticker}")
        free = shutil.disk_usage(self.directory).free
        if free - growth < MIN_FREE_SPACE_BYTES:
            raise CapacityError("root filesystem free-space reserve exceeded")
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.name not in self._hash_states:
            self._hash_states[path.name] = hashlib.sha256()
        self._hash_states[path.name].update(payload)
        self.refresh_manifest()

    def refresh_manifest(self):
        lines = [f"{self._hash_states[name].hexdigest()}  {name}"
                 for name in sorted(self._hash_states)]
        _atomic_write(self.directory / "manifest.sha256", "\n".join(lines) + ("\n" if lines else ""))


def append_trades(writer, trades):
    new = []
    batch_ids = set()
    for trade in trades:
        trade_id = trade.get("trade_id")
        if trade_id and trade_id not in writer._trade_ids and trade_id not in batch_ids:
            new.append(trade)
            batch_ids.add(trade_id)
    writer.append_many("trades", new)
    writer._trade_ids.update(batch_ids)
    return len(new)


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _manifest_errors(directory):
    errors = []
    manifest = directory / "manifest.sha256"
    expected = {}
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            digest, name = line.split("  ", 1)
            expected[name] = digest
    for path in directory.glob("*.jsonl"):
        if expected.get(path.name) != hashlib.sha256(path.read_bytes()).hexdigest():
            errors.append(path.name)
    return sorted(errors)


def validate_dataset(root, expected_interval=2.0):
    root = Path(root)
    results = []
    for directory in sorted((root / "windows").glob("*")) if (root / "windows").exists() else []:
        books = _read_jsonl(directory / "orderbooks.jsonl")
        trades = _read_jsonl(directory / "trades.jsonl")
        timestamps = [row["received_ts_ns"] for row in books]
        threshold = max(expected_interval * 3, expected_interval + 5.0)
        gaps = [round((later - earlier) / 1e9, 6)
                for earlier, later in zip(timestamps, timestamps[1:])
                if (later - earlier) / 1e9 > threshold]
        trade_ids = [row["raw"].get("trade_id") for row in trades]
        complement_errors = 0
        for row in books:
            try:
                if row["executable"] != executable_book(row["raw"]):
                    complement_errors += 1
            except (KeyError, TypeError, ValueError):
                complement_errors += 1
        settlements = _read_jsonl(directory / "settlements.jsonl")
        item = {
            "ticker": directory.name,
            "orderbooks": len(books),
            "trades": len(trades),
            "first_received_ts": books[0]["received_ts"] if books else None,
            "last_received_ts": books[-1]["received_ts"] if books else None,
            "max_snapshot_gap_seconds": round(max(
                ((b - a) / 1e9 for a, b in zip(timestamps, timestamps[1:])), default=0.0), 6),
            "gaps": gaps,
            "duplicate_trade_ids": len(trade_ids) - len(set(trade_ids)),
            "complement_errors": complement_errors,
            "interpolated_records": sum(row.get("interpolated") is not False for row in books + trades),
            "hash_mismatches": _manifest_errors(directory),
            "settled": bool(settlements),
            "settlement_raw": settlements[-1]["raw"] if settlements else None,
        }
        item["ok"] = not any((gaps, item["duplicate_trade_ids"], complement_errors,
                              item["interpolated_records"], item["hash_mismatches"]))
        results.append(item)
    return {"ok": bool(results) and all(item["ok"] for item in results),
            "expected_interval_seconds": expected_interval,
            "gap_threshold_seconds": max(expected_interval * 3, expected_interval + 5.0),
            "windows": results}


class PublicKalshi:
    def __init__(self, timeout=15.0):
        self.session = requests.Session()
        self.timeout = timeout

    def get(self, path, **params):
        response = self.session.get(BASE_URL + path, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


class ForwardCollector:
    def __init__(self, root, series, interval=2.0):
        self.root = Path(root)
        self.series = series
        self.interval = interval
        self.client = PublicKalshi()
        self.writers = {}
        self.closes = {}
        self.settled = set()
        self.last_discovery = 0.0
        self.last_settlement_check = 0.0

    def incident(self, ticker, source, error):
        raw = {"source": source, "error": f"{type(error).__name__}: {error}",
               "retry_seconds": self.interval}
        if ticker in self.writers:
            self.writers[ticker].append("incidents", raw)
        else:
            path = self.root / "incidents.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps({"received_ts": utc_now(), "ticker": ticker,
                                         "interpolated": False, "raw": raw}) + "\n")

    def discover(self):
        for asset in self.series:
            series = SERIES[asset]
            try:
                data = self.client.get("/markets", series_ticker=series, status="open",
                                       limit=100, exchange_index=2)
            except Exception as exc:
                self.incident(series, "discovery", exc)
                continue
            for market in data.get("markets", []):
                ticker = market.get("ticker")
                close = market.get("close_time")
                if not ticker or not close:
                    continue
                if ticker not in self.writers:
                    self.writers[ticker] = WindowWriter(self.root, ticker, market)
                    self.closes[ticker] = parse_time(close)
                    print(json.dumps({"event": "window_started", "ticker": ticker,
                                      "close_time": close}), flush=True)

    def capture_book(self, ticker):
        try:
            raw = self.client.get(f"/markets/{ticker}/orderbook", depth=0)
            self.writers[ticker].append("orderbooks", raw, executable=executable_book(raw))
        except Exception as exc:
            self.incident(ticker, "orderbook", exc)

    def capture_trades(self, ticker):
        try:
            cursor = ""
            seen_cursors = set()
            while cursor not in seen_cursors:
                seen_cursors.add(cursor)
                data = self.client.get("/markets/trades", ticker=ticker,
                                       min_ts=int(self.closes[ticker] - 901), limit=1000,
                                       **({"cursor": cursor} if cursor else {}))
                append_trades(self.writers[ticker], data.get("trades", []))
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
        except Exception as exc:
            self.incident(ticker, "trades", exc)

    def capture_settlements(self):
        for ticker, close in list(self.closes.items()):
            if ticker in self.settled or time.time() < close:
                continue
            try:
                raw = self.client.get(f"/markets/{ticker}", exchange_index=2)
                market = raw.get("market", raw)
                if official_outcome(market) is not None:
                    self.writers[ticker].append("settlements", market)
                    self.settled.add(ticker)
                    print(json.dumps({"event": "settled", "ticker": ticker,
                                      "result": market.get("result"),
                                      "expiration_value": market.get("expiration_value")}), flush=True)
            except Exception as exc:
                self.incident(ticker, "settlement", exc)

    def run(self, duration_minutes=0.0, once=False):
        started = time.time()
        while once or not duration_minutes or time.time() - started < duration_minutes * 60:
            now = time.time()
            if now - self.last_discovery >= 30.0:
                self.discover()
                self.last_discovery = now
            for ticker, close in list(self.closes.items()):
                if now <= close + 2.0:
                    self.capture_book(ticker)
                if ticker not in self.settled or now <= close + 300:
                    self.capture_trades(ticker)
            if now - self.last_settlement_check >= 15.0:
                self.capture_settlements()
                self.last_settlement_check = now
            if once:
                break
            time.sleep(self.interval)


def snapshot_to_raw(snapshot):
    return {"orderbook_fp": {
        "yes_dollars": snapshot.yes_levels,
        "no_dollars": snapshot.no_levels,
    }}


class WebSocketCollector(ForwardCollector):
    """Authenticated official WS capture; REST is used only for metadata/results."""
    def __init__(self, root, series, interval=2.0, ws=None):
        super().__init__(root, series, interval)
        if ws is None:
            from kalshi_ws import KalshiWebSocket
            ws = KalshiWebSocket()
        self.ws = ws
        self.ws.set_orderbook_snapshot_callback(self._on_snapshot)
        self.ws.set_orderbook_delta_callback(self._on_delta)
        self.ws.set_trade_callback(self._on_trade)
        self.ws.set_market_lifecycle_callback(self._on_lifecycle)

    def _on_snapshot(self, snapshot):
        if snapshot.ticker not in self.writers:
            return
        raw = snapshot_to_raw(snapshot)
        self.writers[snapshot.ticker].append(
            "orderbooks", raw, executable=executable_book(raw), transport="websocket")

    def _on_delta(self, delta):
        return None

    def _on_trade(self, trade):
        ticker = trade.get("ticker") or trade.get("market_ticker")
        if ticker in self.writers:
            append_trades(self.writers[ticker], [trade])

    def _on_lifecycle(self, message):
        ticker = message.get("ticker") or message.get("market_ticker")
        if ticker in self.writers and official_outcome(message) is not None:
            self.capture_settlements()

    def run(self, duration_minutes=0.0, once=False):
        started = time.time()
        self.discover()
        self.ws.start()
        self.ws.subscribe_orderbook_delta(sorted(self.closes))
        self.ws.subscribe_trade(sorted(self.closes))
        self.ws.subscribe_market_lifecycle(sorted(self.closes))
        try:
            while once or not duration_minutes or time.time() - started < duration_minutes * 60:
                time.sleep(0.25 if once else self.interval)
                now = time.time()
                if now - self.last_discovery >= 30.0:
                    self.discover()
                    tickers = sorted(self.closes)
                    self.ws.subscribe_orderbook_delta(tickers)
                    self.ws.subscribe_trade(tickers)
                    self.ws.subscribe_market_lifecycle(tickers)
                    self.last_discovery = now
                if now - self.last_settlement_check >= 15.0:
                    self.capture_settlements()
                    self.last_settlement_check = now
                if once:
                    break
        finally:
            self.ws.stop()


def ws_credentials_available():
    import config
    if config.KALSHI_PRIVATE_KEY_CONTENT:
        return True
    return bool(config.KALSHI_KEY_FILE and Path(config.KALSHI_KEY_FILE).expanduser().exists())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--series", default="BTC", help="Comma-separated BTC,ETH,SOL,XRP")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--duration-minutes", type=float, default=0.0,
                        help="0 means run continuously")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--transport", choices=("auto", "ws", "rest"), default="auto")
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args(argv)
    if args.validate:
        result = validate_dataset(args.validate, args.interval)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    assets = [item.strip().upper() for item in args.series.split(",") if item.strip()]
    unknown = set(assets) - set(SERIES)
    if unknown:
        parser.error(f"unsupported assets: {sorted(unknown)}")
    if args.transport == "ws" and not ws_credentials_available():
        parser.error("official Kalshi WS requires existing RSA credentials; use --transport rest")
    collector_class = (WebSocketCollector if args.transport == "ws"
                       else ForwardCollector if args.transport == "rest"
                       else WebSocketCollector if ws_credentials_available() else ForwardCollector)
    collector_class(args.root, assets, args.interval).run(args.duration_minutes, args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
