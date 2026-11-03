#!/usr/bin/env python3
"""
historical_downloader.py — Download granular Kalshi historical data for backtests.

Uses raw requests (with optional kalshi-sdk auth) to fetch historical markets,
trades, and candlesticks from Kalshi's /historical/* endpoints. Data is stored
locally as Parquet files (zstd compressed, hive-partitioned by series/date) for
fast backtest ingestion.

Public endpoints (/historical/markets, /historical/trades,
/historical/markets/{ticker}/candlesticks) do NOT require authentication.
Authenticated endpoints (/historical/fills, /historical/orders, etc.) require
KALSHI_KEY_ID + KALSHI_KEY_FILE.

Usage:
    python historical_downloader.py --series KXBTC15M KXETH15M --days 30
    python historical_downloader.py --series KXBTC15M --limit 5000
    python historical_downloader.py --cutoff-only   # check historical cutoff
    python historical_downloader.py --help

Output layout (under ./historical_data/):
    historical_data/markets/series=KXBTC15M/date=2026-11-02/data.parquet
    historical_data/trades/series=KXBTC15M/date=2026-11-02/data.parquet
    historical_data/candlesticks/series=KXBTC15M/date=2026-11-02/data.parquet
    historical_data/cutoff.json   # last known cutoff timestamps
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

log = logging.getLogger("historical_downloader")

# ── Local storage paths ──────────────────────────────────────────────────
HIST_DIR = Path(__file__).resolve().parent / "historical_data"
MARKETS_DIR = HIST_DIR / "markets"
TRADES_DIR = HIST_DIR / "trades"
CANDLES_DIR = HIST_DIR / "candlesticks"
CUTOFF_FILE = HIST_DIR / "cutoff.json"

# ── Parquet compression ──────────────────────────────────────────────────
PARQUET_COMPRESSION = "zstd"

# ── Kalshi series targets (15-min crypto markets) ────────────────────────
DEFAULT_SERIES = [
    "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M",
    "KXBNB15M", "KXHYPE15M", "KXZEC15M", "KXNEAR15M",
]

# Kalshi API base URL
BASE_URL = config.KALSHI_BASE_URL.rstrip("/")

# Rate limiting: Basic tier = 200 read tokens/sec, ~10 tokens per request
MIN_REQUEST_GAP = float(os.environ.get("HIST_DOWNLOAD_MIN_GAP", "0.1"))
_last_request_time = 0.0

# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _rate_limit():
    """Enforce minimum gap between API requests to avoid 429s."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < MIN_REQUEST_GAP:
        time.sleep(MIN_REQUEST_GAP - elapsed)
    _last_request_time = time.time()


def _series_dir(series: str) -> str:
    """Sanitize series ticker for directory naming."""
    return series.replace("-", "_")


def _date_partition(ts_ms: Optional[int]) -> str:
    """Convert epoch-ms to YYYY-MM-DD date partition string."""
    if ts_ms is None:
        return "unknown"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "unknown"


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write a DataFrame to Parquet with zstd compression (idempotent: overwrite)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df)
    pq.write_table(table, path, compression=PARQUET_COMPRESSION)
    log.debug("Wrote %s (%d rows)", path, len(df))


def _read_parquet(path: Path) -> Optional[pd.DataFrame]:
    """Read a Parquet file, returning None if missing or unreadable."""
    try:
        if not path.exists():
            return None
        return pd.read_parquet(path)
    except Exception as e:
        log.warning("Could not read %s: %s", path, e)
        return None


def _load_json(path: Path) -> dict:
    """Load a JSON file, returning {} if missing or corrupt."""
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        log.warning("Could not read %s: %s", path, e)
    return {}


def _save_json(path: Path, data: dict) -> None:
    """Atomically write a JSON file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)


def _api_get(path: str, params: dict, retries: int = 5) -> Optional[dict]:
    """GET a Kalshi API endpoint with rate limiting and 429 backoff."""
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        _rate_limit()
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 0.7 * (2 ** attempt)
                log.warning("429 on %s — backing off %.1fs (attempt %d/%d)",
                            path, wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600:
                wait = 0.7 * (2 ** attempt)
                log.warning("HTTP %s on %s — backing off %.1fs (attempt %d/%d)",
                            resp.status_code, path, wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            # 4xx (except 429) — don't retry, return None immediately
            if resp.status_code >= 400:
                log.error("HTTP %s on %s — not retrying (client error)",
                          resp.status_code, path)
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = 0.7 * (2 ** attempt)
                log.warning("Request error on %s: %s — retrying in %.1fs",
                            path, e, wait)
                time.sleep(wait)
                continue
            log.error("Failed to GET %s after %d retries: %s", path, retries, e)
            return None
    return None


# ════════════════════════════════════════════════════════════════════════════
# Downloaders
# ════════════════════════════════════════════════════════════════════════════

def get_cutoff() -> Optional[dict]:
    """Get historical cutoff timestamps from Kalshi API."""
    return _api_get("/historical/cutoff", {})


def download_markets(
    series: str, limit: int = 1000,
    min_settled_ts: Optional[int] = None,
    max_settled_ts: Optional[int] = None,
) -> int:
    """Download settled historical markets for a series. Returns row count."""
    all_markets = []
    cursor = None
    pages = 0

    while True:
        params = {"limit": limit, "series_ticker": series, "status": "settled"}
        if min_settled_ts:
            params["min_settled_ts"] = min_settled_ts
        if max_settled_ts:
            params["max_settled_ts"] = max_settled_ts
        if cursor:
            params["cursor"] = cursor

        data = _api_get("/historical/markets", params)
        if data is None:
            break

        markets = data.get("markets", [])
        cursor = data.get("cursor")

        if not markets:
            break

        all_markets.extend(markets)
        pages += 1
        log.info("  Page %d: %d markets (cursor=%s)", pages, len(markets), cursor)

        if not cursor:
            break

    if not all_markets:
        log.info("No markets found for %s", series)
        return 0

    df = pd.DataFrame(all_markets)
    # Normalize timestamp fields
    for col in ("open_time", "close_time", "settlement_ts", "created_time", "updated_time"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], unit="ms", errors="coerce")

    # Hive partition by date
    date_col = "settlement_ts" if "settlement_ts" in df.columns else "close_time"
    if date_col in df.columns:
        df["_date"] = df[date_col].dt.strftime("%Y-%m-%d")
    else:
        df["_date"] = "unknown"

    total_written = 0
    for date_val, group in df.groupby("_date"):
        group = group.drop(columns=["_date"])
        path = MARKETS_DIR / f"series={_series_dir(series)}" / f"date={date_val}" / "data.parquet"
        existing = _read_parquet(path)
        if existing is not None and len(existing) > 0:
            combined = pd.concat([existing, group], ignore_index=True)
            if "ticker" in combined.columns:
                combined = combined.drop_duplicates(subset=["ticker"], keep="last")
            group = combined
        _write_parquet(group, path)
        total_written += len(group)

    log.info("Downloaded %d markets for %s across %d date partitions",
             total_written, series, df["_date"].nunique())
    return total_written


def download_trades(
    series: str, limit: int = 1000,
    min_ts: Optional[int] = None,
    max_ts: Optional[int] = None,
) -> int:
    """Download historical trades for a series. Returns row count.

    Note: The /historical/trades endpoint does not accept min_ts/max_ts
    as milliseconds. Use cursor pagination to iterate through all trades.
    Trade records include created_time (ISO 8601) for date partitioning.
    """
    all_trades = []
    cursor = None
    pages = 0

    while True:
        params = {"limit": limit, "series_ticker": series}
        if cursor:
            params["cursor"] = cursor

        data = _api_get("/historical/trades", params)
        if data is None:
            break

        trades = data.get("trades", [])
        cursor = data.get("cursor")

        if not trades:
            break

        all_trades.extend(trades)
        pages += 1
        log.info("  Page %d: %d trades (cursor=%s)", pages, len(trades), cursor)

        if not cursor:
            break

    if not all_trades:
        log.info("No trades found for %s", series)
        return 0

    df = pd.DataFrame(all_trades)
    # Normalize timestamp — Kalshi returns created_time as ISO 8601
    # (e.g., "2023-11-07T05:31:56Z"), NOT epoch milliseconds.
    if "created_time" in df.columns:
        df["created_time"] = pd.to_datetime(df["created_time"], errors="coerce")
    elif "ts" in df.columns:
        df["created_time"] = pd.to_datetime(df["ts"], errors="coerce")

    if "created_time" in df.columns:
        df["_date"] = df["created_time"].dt.strftime("%Y-%m-%d")
    else:
        df["_date"] = "unknown"

    total_written = 0
    for date_val, group in df.groupby("_date"):
        group = group.drop(columns=["_date"])
        path = TRADES_DIR / f"series={_series_dir(series)}" / f"date={date_val}" / "data.parquet"
        existing = _read_parquet(path)
        if existing is not None and len(existing) > 0:
            combined = pd.concat([existing, group], ignore_index=True)
            if "trade_id" in combined.columns:
                combined = combined.drop_duplicates(subset=["trade_id"], keep="last")
            elif "id" in combined.columns:
                combined = combined.drop_duplicates(subset=["id"], keep="last")
            group = combined
        _write_parquet(group, path)
        total_written += len(group)

    log.info("Downloaded %d trades for %s across %d date partitions",
             total_written, series, df["_date"].nunique())
    return total_written


def download_candlesticks(
    series: str, period_interval: int = 1,
    start_ts: Optional[int] = None, end_ts: Optional[int] = None,
    limit: int = 1000,
) -> int:
    """Download historical candlesticks for all markets in a series."""
    # Get markets list from local Parquet
    series_dir = MARKETS_DIR / f"series={_series_dir(series)}"
    if not series_dir.exists():
        log.warning("No local markets for %s — download markets first", series)
        return 0

    frames = []
    for date_dir in series_dir.iterdir():
        if not date_dir.is_dir():
            continue
        for pq_file in date_dir.glob("*.parquet"):
            df = _read_parquet(pq_file)
            if df is not None:
                frames.append(df)
    if not frames:
        log.warning("No market data found for %s", series)
        return 0
    markets_df = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["ticker"], keep="last"
    )

    total_written = 0
    for _, market in markets_df.iterrows():
        ticker = market["ticker"]
        try:
            params = {"period_interval": period_interval, "limit": limit}
            if start_ts:
                params["start_ts"] = start_ts
            if end_ts:
                params["end_ts"] = end_ts

            data = _api_get(
                f"/historical/markets/{ticker}/candlesticks", params
            )
            if data is None:
                continue

            candle_list = data.get("candles", [])
            if not candle_list:
                continue

            df = pd.DataFrame(candle_list)
            df["ticker"] = ticker
            df["series"] = series

            if "start_ts" in df.columns:
                df["start_time"] = pd.to_datetime(df["start_ts"], unit="s", errors="coerce")
                df["_date"] = df["start_time"].dt.strftime("%Y-%m-%d")
            else:
                df["_date"] = "unknown"

            for date_val, group in df.groupby("_date"):
                group = group.drop(columns=["_date"])
                path = (CANDLES_DIR / f"series={_series_dir(series)}"
                        / f"date={date_val}" / "data.parquet")
                existing = _read_parquet(path)
                if existing is not None and len(existing) > 0:
                    combined = pd.concat([existing, group], ignore_index=True)
                    if "start_ts" in combined.columns and "ticker" in combined.columns:
                        combined = combined.drop_duplicates(
                            subset=["ticker", "start_ts"], keep="last"
                        )
                    group = combined
                _write_parquet(group, path)
                total_written += len(group)

        except Exception as e:
            log.warning("Error downloading candlesticks for %s: %s", ticker, e)
            continue

    log.info("Downloaded %d candlesticks for %s", total_written, series)
    return total_written


def sync_all(
    series_list: list, days: int = 30, limit: int = 1000,
    download_markets_: bool = True, download_trades_: bool = True,
    download_candles_: bool = False,
) -> dict:
    """Download all historical data for the given series."""
    results = {"series": series_list, "markets": {}, "trades": {}, "candlesticks": {}}

    # Get cutoff first
    cutoff = get_cutoff()
    if cutoff:
        _save_json(CUTOFF_FILE, cutoff)
        log.info("Historical cutoff: %s", json.dumps(cutoff, indent=2, default=str))

    # Determine time window
    end_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    for series in series_list:
        log.info("=== Processing series: %s ===", series)

        if download_markets_:
            n = download_markets(
                series, limit=limit,
                min_settled_ts=start_ts, max_settled_ts=end_ts,
            )
            results["markets"][series] = n

        if download_trades_:
            n = download_trades(
                series, limit=limit, min_ts=start_ts, max_ts=end_ts,
            )
            results["trades"][series] = n

        if download_candles_:
            n = download_candlesticks(
                series, start_ts=start_ts // 1000, end_ts=end_ts // 1000,
                limit=limit,
            )
            results["candlesticks"][series] = n

    return results


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Download Kalshi historical data for backtests",
    )
    parser.add_argument(
        "--series", nargs="+", default=DEFAULT_SERIES,
        help="Series tickers to download (default: all 15M crypto series)",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Number of days of history to download (default: 30)",
    )
    parser.add_argument(
        "--limit", type=int, default=1000,
        help="Records per API page (max 1000, default: 1000)",
    )
    parser.add_argument(
        "--cutoff-only", action="store_true",
        help="Only check and print historical cutoff timestamps",
    )
    parser.add_argument(
        "--no-markets", action="store_true",
        help="Skip downloading markets",
    )
    parser.add_argument(
        "--no-trades", action="store_true",
        help="Skip downloading trades",
    )
    parser.add_argument(
        "--candles", action="store_true",
        help="Also download candlesticks (1-minute intervals)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    # Create output directories
    for d in (MARKETS_DIR, TRADES_DIR, CANDLES_DIR):
        d.mkdir(parents=True, exist_ok=True)

    if args.cutoff_only:
        cutoff = get_cutoff()
        if cutoff:
            print(json.dumps(cutoff, indent=2, default=str))
        else:
            print("Failed to get cutoff", file=sys.stderr)
            sys.exit(1)
        return

    results = sync_all(
        series_list=args.series,
        days=args.days,
        limit=args.limit,
        download_markets_=not args.no_markets,
        download_trades_=not args.no_trades,
        download_candles_=args.candles,
    )

    # Print summary
    print("\n" + "=" * 60)
    print("HISTORICAL DATA DOWNLOAD COMPLETE")
    print("=" * 60)
    for series in args.series:
        m = results["markets"].get(series, 0)
        t = results["trades"].get(series, 0)
        c = results["candlesticks"].get(series, 0)
        print(f"  {series}: {m} markets, {t} trades, {c} candlesticks")
    print(f"\nOutput directory: {HIST_DIR}")
    print(f"Cutoff file: {CUTOFF_FILE}")


if __name__ == "__main__":
    main()