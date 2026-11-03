#!/usr/bin/env python3
"""Download historical trade data from Kalshi using the kalshi-trade-downloader client."""
import sys
sys.path.insert(0, 'kalshi-trade-downloader')

from kalshi_client import KalshiClient
from datetime import datetime, timezone
import json, os, glob

client = KalshiClient()

with open('historical_data/markets_2026.json') as f:
    markets = json.load(f)

os.makedirs('historical_data/trades', exist_ok=True)

trade_files = glob.glob('historical_data/trades/*.json')
trade_tickers = set()
for f in trade_files:
    trade_tickers.add(os.path.basename(f).replace('.json', ''))

market_tickers = set(m['ticker'] for m in markets)
missing = market_tickers - trade_tickers
print(f'Total markets: {len(markets)}')
print(f'Markets with trades: {len(trade_tickers)}')
print(f'Markets without trades: {len(missing)}')

total_trades = 0
count = 0
errors = 0

for i, m in enumerate(markets):
    ticker = m['ticker']
    if ticker in trade_tickers:
        continue

    close_time = m.get('close_time', '')
    if not close_time:
        errors += 1
        continue

    ct = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
    start_ts = int(ct.timestamp()) - 3600
    end_ts = int(ct.timestamp()) + 60

    try:
        trades = client.fetch_trades(ticker, start_ts, end_ts)
        if trades:
            with open(f'historical_data/trades/{ticker}.json', 'w') as f:
                json.dump(trades, f)
            total_trades += len(trades)
            count += 1
            if count % 20 == 0:
                print(f'  Processed {count} markets, {total_trades} trades so far...')
    except Exception as e:
        errors += 1
        if errors <= 5:
            print(f'  Error for {ticker}: {e}')

print(f'\nDone! Total: {total_trades} new trades across {count} markets, {errors} errors')
print(f'Total trade files: {len(glob.glob("historical_data/trades/*.json"))}')
