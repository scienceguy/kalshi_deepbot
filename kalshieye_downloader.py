"""
kalshieye_downloader.py — Download historical order book data from Kalshi Eye.

Kalshi Eye (https://kalshieye.com) records order books, trades, and settlements
for Kalshi, Polymarket, Limitless, and Chainlink markets.

Their REST API provides historical data for backtesting. Requires an API key
(create at https://kalshieye.com/app/api-keys).

Usage:
    python kalshieye_downloader.py --api-key KEY --series KXBTC15M --start 2026-07-19 --end 2026-07-19
    python kalshieye_downloader.py --api-key KEY --list-series
"""

import argparse, json, logging, os, sys, time
from datetime import date, timedelta
from pathlib import Path

import requests

log = logging.getLogger("kalshieye_downloader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

BASE_URL = "https://kalshieye.com/api/v1"


class KalshieyeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def search(self, q: str, limit: int = 10):
        """Search markets."""
        r = self.session.get(f"{BASE_URL}/search", params={"q": q, "limit": limit}, timeout=30)
        r.raise_for_status()
        return r.json()

    def list_series(self):
        """List all series."""
        r = self.session.get(f"{BASE_URL}/series", timeout=30)
        r.raise_for_status()
        return r.json()

    def get_series(self, series_ticker: str):
        """Get a specific series."""
        r = self.session.get(f"{BASE_URL}/series/{series_ticker}", timeout=30)
        r.raise_for_status()
        return r.json()

    def list_markets(self, series_ticker: str = None, limit: int = 100):
        """List markets, following next_offset until all pages are fetched."""
        params = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        markets = []
        data = {}
        offset = 0
        while True:
            params["offset"] = offset
            r = self.session.get(f"{BASE_URL}/markets", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            markets.extend(data.get("markets", []))
            next_offset = data.get("next_offset")
            if next_offset is None:
                break
            offset = next_offset
        result = dict(data)
        result["markets"] = markets
        result["count"] = len(markets)
        return result

    def get_market(self, ticker: str):
        """Get a specific market."""
        r = self.session.get(f"{BASE_URL}/markets/{ticker}", timeout=30)
        r.raise_for_status()
        return r.json()

    def get_snapshots(self, ticker: str, start_ts: int, end_ts: int,
                  limit: int = 1000, step_ms: int = 1000):
        """Get all snapshots for a market, following next_start_ts until the window is covered."""
        params = {"start_ts": start_ts, "end_ts": end_ts,
                  "limit": limit, "step_ms": step_ms}
        snapshots = []
        data = {}
        while True:
            r = self.session.get(
                f"{BASE_URL}/markets/{ticker}/snapshots",
                params=params, timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            snapshots.extend(data.get("snapshots", []))
            next_start_ts = data.get("next_start_ts")
            if next_start_ts is None:
                break
            params["start_ts"] = next_start_ts
        result = dict(data)
        result["snapshots"] = snapshots
        result["count"] = len(snapshots)
        return result

    def list_files(self, dataset: str = None, start_date: str = None,
                   end_date: str = None, series: str = None):
        """List available download files.

        Kalshieye returns {"files": [...]} — extract the files list.
        """
        params = {}
        if dataset: params["dataset"] = dataset
        if start_date: params["start_date"] = start_date
        if end_date: params["end_date"] = end_date
        if series: params["series"] = series
        r = self.session.get(f"{BASE_URL}/files", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("files", [])

    def estimate_request(self, dataset: str, start_date: str, end_date: str,
                         series: str = None):
        """Estimate cost/size of a download request.

        Kalshieye may not expose an estimate endpoint; if it fails, return
        an empty dict so the caller can proceed without it.
        """
        params = {"dataset": dataset, "start_date": start_date, "end_date": end_date}
        if series: params["series"] = series
        try:
            r = self.session.get(f"{BASE_URL}/estimate", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            # POST as fallback
            try:
                r = self.session.post(f"{BASE_URL}/estimate", json=params, timeout=30)
                r.raise_for_status()
                return r.json()
            except Exception:
                return {}

    def download_file(self, signed_url: str, output_path: Path):
        """Download a file from a signed URL with retry and content validation."""
        last_err = None
        for attempt in range(3):
            try:
                r = self.session.get(signed_url, timeout=120)
                r.raise_for_status()
                # Validate content — must not be an error page
                content_type = r.headers.get("Content-Type", "")
                if "text/html" in content_type and len(r.content) < 5000:
                    raise ValueError(f"Received HTML error page instead of file ({len(r.content)} bytes)")
                output_path.write_bytes(r.content)
                # Verify file was written and is non-empty
                if output_path.stat().st_size == 0:
                    raise ValueError("Downloaded file is empty")
                return output_path
            except Exception as e:
                last_err = e
                if attempt < 2:
                    wait = 2.0 * (attempt + 1)
                    log.warning("Download %s attempt %d/3 failed: %s — retrying in %.1fs",
                               output_path.name, attempt + 1, e, wait)
                    time.sleep(wait)
                else:
                    log.error("Download %s failed after 3 attempts: %s", output_path.name, e)
        raise last_err


def daterange(start_date: date, end_date: date):
    """Generate dates from start to end (inclusive)."""
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description="Download Kalshi Eye historical data")
    parser.add_argument("--api-key", required=True, help="Kalshi Eye API key")
    parser.add_argument("--series", nargs="+", default=["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"],
                        help="Series tickers to download")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--dataset", default="snapshots", help="Dataset type (snapshots, trades, top_of_book)")
    parser.add_argument("--output-dir", default="./kalshieye_data", help="Output directory")
    parser.add_argument("--list-series", action="store_true", help="List available series and exit")
    args = parser.parse_args()

    client = KalshieyeClient(args.api_key)

    if args.list_series:
        series = client.list_series()
        print(json.dumps(series, indent=2)[:5000])
        return

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    for series in args.series:
        log.info("Downloading %s snapshots from %s to %s", series, args.start, args.end)

        for single_date in daterange(start_date, end_date):
            date_str = single_date.isoformat()
            out_file = output_dir / f"{series}_{date_str}.jsonl.gz"

            if out_file.exists():
                log.info("  %s already exists, skipping", out_file.name)
                continue

            try:
                # Estimate first (non-fatal if endpoint unavailable)
                est = client.estimate_request(args.dataset, date_str, date_str, series)
                log.info("  %s %s: estimate=%s", series, date_str, est)
            except Exception as e:
                log.warning("  %s %s: estimate failed: %s — proceeding anyway",
                            series, date_str, e)

            # Get files
            try:
                files = client.list_files(dataset=args.dataset, start_date=date_str,
                                          end_date=date_str, series=series)
            except Exception as e:
                log.warning("  %s %s: list_files failed: %s", series, date_str, e)
                continue
            if not files:
                log.warning("  %s %s: no files available", series, date_str)
                continue

            # Download all files for this date
            try:
                for file_info in files:
                    # Kalshieye file info uses 'key' (e.g. flat/v1/markets/date=.../markets-...parquet)
                    # and 'path' fields instead of 'url'/'download_url'. Construct download URL.
                    key = file_info.get("key") or file_info.get("path", "")
                    signed_url = file_info.get("url") or file_info.get("download_url")
                    if not signed_url and key:
                        # Try multiple download URL patterns for Kalshieye
                        signed_url = (
                            f"{BASE_URL}/files/download?key={key}"
                        )
                    if not signed_url:
                        log.warning("  %s %s: no download URL in file info", series, date_str)
                        continue

                    # Use series/date in filename to avoid overwriting multiple files
                    file_name = file_info.get("name", file_info.get("filename", "data"))
                    safe_name = "".join(c for c in file_name if c.isalnum() or c in "._-")
                    out_file = output_dir / f"{series}_{date_str}_{safe_name}.jsonl.gz"

                    if out_file.exists():
                        log.info("  %s %s/%s already exists, skipping", series, date_str, safe_name)
                        continue

                    client.download_file(signed_url, out_file)
                    log.info("  Downloaded %s (%d bytes)", out_file.name, out_file.stat().st_size)
            except Exception as e:
                log.warning("  %s %s download failed: %s", series, date_str, e)

            time.sleep(1.0)  # Rate limit


if __name__ == "__main__":
    main()