"""
data_collector.py — Collect market snapshots during dry-run trading for backtesting.

Runs the SuperBot in dry-run mode, recording every market snapshot at entry time
with settlement outcomes. This data can then be used for proper parameter sweep
backtesting without needing an external API key.

Usage:
    python data_collector.py --days 7          # Collect 7 days of dry-run data
    python data_collector.py --once             # One cycle, save snapshots
    python backtest.py --data-dir ./collected_data  # Backtest on collected data
"""

import argparse, gzip, json, logging, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config
from kalshi_client import KalshiClient
from market_finder import find_active_markets, _parse_api_time

log = logging.getLogger("data_collector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")


class MarketSnapshotCollector:
    """Collects market snapshots at entry time with settlement outcomes."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = KalshiClient()
        self.collected = 0

    def collect_market_snapshots(self, series_list, limit=200):
        """
        For each series, fetch closed markets with settlement outcomes.
        We simulate what the orderbook would have looked like based on
        the actual settlement direction and current prices.

        Snapshots are written to disk incrementally after each series so
        that data from completed series survives a crash in a later
        series (previously all snapshots were held in memory and only
        saved at the end, losing everything on a mid-run failure).
        """
        filename = f"collected_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl.gz"
        out_path = self.output_dir / filename
        all_snapshots = []

        with gzip.open(out_path, "wt") as f:
            for series in series_list:
                series_markets = None

                # Historical tier: settled markets with expiration_value,
                # close_time, floor_strike and other settlement fields.
                try:
                    fetched = []
                    cursor = ""
                    while len(fetched) < limit:
                        page = min(100, limit - len(fetched))
                        data = self.client.get_historical_markets(
                            series_ticker=series, limit=page, cursor=cursor
                        )
                        page_markets = data.get("markets", [])
                        if not page_markets:
                            break
                        fetched.extend(page_markets)
                        cursor = data.get("cursor", "")
                        if not cursor:
                            break
                    if fetched:
                        series_markets = fetched
                        log.info("%s: %d historical markets", series, len(fetched))
                except Exception as e:
                    log.warning("%s historical fetch failed: %s", series, e)

                # Fall back to public /markets endpoint
                if series_markets is None:
                    for attempt in range(3):
                        try:
                            series_markets = self.client.get_markets(
                                series_ticker=series,
                                status="closed",
                                limit=limit,
                            )
                            break
                        except Exception as e:
                            if attempt < 2:
                                wait = 1.5 * (2 ** attempt)
                                log.warning(
                                    "%s attempt %d/3 failed: %s — retrying in %.1fs",
                                    series, attempt + 1, e, wait,
                                )
                                time.sleep(wait)
                            else:
                                log.warning("%s failed after 3 attempts: %s", series, e)

                if series_markets is None:
                    continue

                log.info("%s: %d closed markets", series, len(series_markets))

                for m in series_markets:
                    snapshot = self._build_snapshot(m)
                    if snapshot:
                        all_snapshots.append(snapshot)
                        self.collected += 1
                        f.write(json.dumps(snapshot) + "\n")
                        f.flush()  # persist immediately: survive hard crash mid-series

                time.sleep(0.15)

        return all_snapshots, out_path

    def _build_snapshot(self, market: dict) -> dict | None:
        """Build a training snapshot from a closed market."""
        ticker = market.get("ticker", "")
        ct = _parse_api_time(market.get("close_time", ""))
        if not ct:
            return None

        now = datetime.now(timezone.utc)
        minutes_left = (ct - now).total_seconds() / 60
        if minutes_left > 15.5:
            return None

        if minutes_left <= 0:
            # Closed market: market has already settled; record full-window snapshot.
            # Keep the actual (negative) minutes_left so downstream time-decay features
            # see variation across markets that settled at different times.
            market_status = "closed"
            minutes_in = 15.0
        else:
            market_status = "open"
            minutes_in = 15.0 - minutes_left

        # Parse prices
        from market_finder import _parse_market_prices
        yb, ya, nb, na, yq, nq = _parse_market_prices(market)

        # Use last_price as entry price proxy
        last_price = market.get("last_price_dollars")
        if last_price and float(last_price) > 0:
            mid = int(round(float(last_price) * 100))
        elif yb and ya:
            mid = (yb + ya) // 2
        else:
            return None

        # Settlement outcome
        exp_val = market.get("expiration_value")
        settled_yes = int(round(float(exp_val) * 100)) if exp_val is not None else None

        return {
            "ticker": ticker,
            "series_ticker": market.get("series_ticker", ""),
            "close_time": market.get("close_time", ""),
            "minutes_left": minutes_left,
            "minutes_in": minutes_in,
            "market_status": market_status,
            "mid_yes": mid,
            "yes_bid": yb or 0,
            "yes_ask": ya or 0,
            "no_bid": nb or 0,
            "no_ask": na or 0,
            "settled_yes": settled_yes,
            "won_yes": settled_yes >= 100 if settled_yes is not None else None,
            "received_time": datetime.now(timezone.utc).isoformat(),
        }

    def save_snapshots(self, snapshots: list, filename: str = None):
        """Save snapshots to gzipped JSONL."""
        if filename is None:
            filename = f"collected_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl.gz"
        out_path = self.output_dir / filename
        # Level 6 is the sweet spot for JSONL: ~same size as level 9 with much less CPU.
        with gzip.open(out_path, "wt", compresslevel=6) as f:
            for s in snapshots:
                f.write(json.dumps(s) + "\n")
        log.info("Saved %d snapshots to %s", len(snapshots), out_path)
        return out_path


def main():
    parser = argparse.ArgumentParser(description="Collect market data for backtesting")
    parser.add_argument("--series", nargs="+",
                        default=["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"])
    parser.add_argument("--output-dir", default="./collected_data")
    parser.add_argument("--limit", type=int, default=200, help="Max markets per series")
    args = parser.parse_args()

    collector = MarketSnapshotCollector(Path(args.output_dir))

    log.info("Collecting snapshots for %s", args.series)
    snapshots, out_path = collector.collect_market_snapshots(args.series, args.limit)
    log.info("Collected %d snapshots total", len(snapshots))

    if snapshots:
        print(f"\nData saved to: {out_path}")
        print(f"Run backtest with: python backtest.py --data-file {out_path}")
    else:
        print("No snapshots collected. Check API credentials and connectivity.")


if __name__ == "__main__":
    main()