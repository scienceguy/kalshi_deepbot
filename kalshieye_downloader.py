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

    def get_download_links(self, keys: list[str]) -> dict[str, str]:
        """Get signed download URLs for file keys via POST /api/v1/files/links.

        Returns a dict mapping key -> signed URL.
        """
        if not keys:
            return {}
        
        # Scale timeout with key count: base 30s + 0.1s per key, max 120s
        timeout = min(30 + len(keys) * 0.1, 120)
        log.debug("Requesting download links for %d keys (timeout=%.1fs)", len(keys), timeout)
        
        r = self.session.post(f"{BASE_URL}/files/links", json={"keys": keys}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        
        # Validate response structure
        if "files" not in data:
            log.warning("Unexpected response format from /files/links: keys=%s", list(data.keys()))
            return {}
        
        links = {}
        for entry in data.get("files", []):
            # Try multiple key field names that the API might return
            key = entry.get("key") or entry.get("path") or entry.get("file_key") or entry.get("id")
            url = entry.get("url") or entry.get("download_url") or entry.get("signed_url")
            if url and key:
                links[key] = url
            else:
                log.debug("Skipping entry with missing key/url: %s", entry)
        
        log.debug("Got %d signed download links for %d requested keys", len(links), len(keys))
        return links

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
        """Download a file from a signed URL with retry and content validation.

        Signed Cloudflare R2 URLs must be fetched without the session's
        Authorization header (R2 rejects it on pre-signed URLs).
        """
        last_err = None
        for attempt in range(3):
            try:
                # Use a fresh session without the Bearer token for signed URLs
                dl_session = requests.Session()
                r = dl_session.get(signed_url, timeout=120)
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


def _load_kalshieye_api_key() -> str:
    """Resolve the Kalshi Eye API key from env file, env var, or CLI arg.

    Priority: KALSHIEYE_API_KEY_FILE (chmod-600 file) > KALSHIEYE_API_KEY (env)
    > --api-key CLI argument. No secret values are ever printed.
    """
    import os

    key_file = os.environ.get("KALSHIEYE_API_KEY_FILE", "")
    if key_file:
        try:
            return Path(key_file).expanduser().read_text().strip()
        except OSError as e:
            raise SystemExit(f"cannot read KALSHIEYE_API_KEY_FILE {key_file}: {e}")
    env_key = os.environ.get("KALSHIEYE_API_KEY", "")
    if env_key:
        return env_key
    raise SystemExit(
        "Kalshi Eye API key required: set KALSHIEYE_API_KEY_FILE or "
        "KALSHIEYE_API_KEY, or pass --api-key"
    )


def main():
    parser = argparse.ArgumentParser(description="Download Kalshi Eye historical data")
    parser.add_argument("--api-key", default=None, help="Kalshi Eye API key")
    parser.add_argument("--series", nargs="+", default=["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"],
                        help="Series tickers to download")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--dataset", default="snapshots", help="Dataset type (snapshots, trades, top_of_book)")
    parser.add_argument("--output-dir", default="./kalshieye_data", help="Output directory")
    parser.add_argument("--list-series", action="store_true", help="List available series and exit")
    args = parser.parse_args()

    api_key = args.api_key or _load_kalshieye_api_key()
    client = KalshieyeClient(api_key)

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
                # Get files
                files = client.list_files(dataset=args.dataset, start_date=date_str,
                                          end_date=date_str, series=series)
            except Exception as e:
                log.warning("  %s %s: list_files failed: %s", series, date_str, e)
                continue
            if not files:
                log.warning("  %s %s: no files available", series, date_str)
                continue

            # Download all files for this date — first get signed links,
            # then download each file from its signed Cloudflare R2 URL.
            try:
                # Collect all file keys for this date
                keys = [fi.get("key") or fi.get("path") or fi.get("file_key") or fi.get("id") for fi in files]
                keys = [k for k in keys if k]

                # Get signed download links (batch call for all keys at once)
                link_map = {}
                try:
                    link_map = client.get_download_links(keys)
                except Exception as e:
                    log.warning("  %s %s: get_download_links failed: %s", series, date_str, e)

                for file_info in files:
                    # Try multiple key field names to match API response
                    key = file_info.get("key") or file_info.get("path") or file_info.get("file_key") or file_info.get("id")
                    
                    # Prioritize fresh signed URL from link_map over potentially stale URLs in file_info
                    signed_url = link_map.get(key)
                    if not signed_url:
                        # Fallback to any URL in file_info
                        signed_url = file_info.get("url") or file_info.get("download_url") or file_info.get("signed_url")
                    
                    if not signed_url:
                        log.warning("  %s %s: no download URL for key %s", series, date_str, key)
                        continue

                    # Use series/date in filename to avoid overwriting multiple files
                    file_name = file_info.get("name") or file_info.get("filename") or "data"
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