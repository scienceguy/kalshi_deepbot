# T17-01 Polymarket US Production Evidence

## Result

- Verification date: 2026-11-23 (UTC).
- HEAD: `f46d6ccac9e03705c6befa7d2f6bbb658a636b08` (`f46d6cc`).
- Endpoint: public `https://gateway.polymarket.us`; unauthenticated read-only GETs only.
- Direct-market batch started at `2026-11-23T01:00:32.882317+00:00`.
- Catalog batch started at `2026-11-23T01:02:45.625821+00:00`.
- Overall: **PARTIAL**.

| Check | Result | Evidence |
|---|---|---|
| BTC/ETH/SOL/XRP current 5m markets | PASS | 4/4 returned HTTP 200, `MARKET_STATUS_OPEN`, `active=true`, `closed=false`. |
| BTC/ETH/SOL/XRP current 15m markets | PASS | 4/4 returned HTTP 200, `MARKET_STATUS_OPEN`, `active=true`, `closed=false`. |
| BTC/ETH/SOL/XRP current 1h markets | PASS | 4/4 returned HTTP 200, `MARKET_STATUS_OPEN`, `active=true`, `closed=false`. |
| BTC/ETH/SOL/XRP current 4h markets | FAIL | The four calculated current slugs returned HTTP 404. The active, unclosed crypto catalog contained no 4h Up/Down row at the catalog query time. This is not proof that no other historical, inactive, closed, or differently categorized 4h market exists. |
| Metadata and exact resolution text for returned current markets | PASS | All 12 HTTP 200 payloads are recorded below. No 4h payload was returned. |
| Active crypto catalog | PASS | Sixteen offset pages returned HTTP 200: 1,504 rows total, ending in a short page. Sixteen active Up/Down rows were present: the 12 current 5m/15m/1h rows and four 1d rows; zero 4h rows. |
| Account-applicable fee tier | NOT VERIFIED | `feeCoefficient=0.095` is market metadata, not account-specific tier proof. No authenticated endpoint or order preview was used. |

The 4h calculation floored the batch UTC time to a four-hour boundary
(`00:00`, `04:00`, and so on). A 404 applies only to the exact requested slug.
The separate catalog result below is the evidence about other active 4h listings.

No credentials, secrets, balances, orders, order previews, or authenticated
endpoints were used. No process, config, source, or test files were changed, and
no git mutation was performed.

## Direct current-market evidence

`tick` is `orderPriceMinTickSize`. Window timestamps are from `assetPriceTerms`.
For HTTP 404 rows, the response had no market object; fields are marked
`not returned` rather than inferred. Every 404 body was exactly:

```json
{"code":5, "message":"The server was unable to process your request.", "details":[]}
```

| Asset | Horizon | Full slug | HTTP | status | active | closed | indexSymbol | tick | minimumTradeQty | feeCoefficient | windowStart | windowEnd |
|---|---|---|---:|---|---|---|---|---:|---:|---:|---|---|
| BTC | 5m | `cpc-btc-updown-5m-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `BRTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T01:05:00Z` |
| BTC | 15m | `cpc-btc-updown-15m-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `BRTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T01:15:00Z` |
| BTC | 1h | `cpc-btc-updown-1h-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `BRTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T02:00:00Z` |
| BTC | 4h | `cpc-btc-updown-4h-2026-11-23-0000z` | 404 | not returned | not returned | not returned | not returned | not returned | not returned | not returned | not returned | not returned |
| ETH | 5m | `cpceth-eth-updown-5m-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `ETHUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T01:05:00Z` |
| ETH | 15m | `cpceth-eth-updown-15m-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `ETHUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T01:15:00Z` |
| ETH | 1h | `cpceth-eth-updown-1h-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `ETHUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T02:00:00Z` |
| ETH | 4h | `cpceth-eth-updown-4h-2026-11-23-0000z` | 404 | not returned | not returned | not returned | not returned | not returned | not returned | not returned | not returned | not returned |
| SOL | 5m | `cpcsol-sol-updown-5m-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `SOLUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T01:05:00Z` |
| SOL | 15m | `cpcsol-sol-updown-15m-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `SOLUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T01:15:00Z` |
| SOL | 1h | `cpcsol-sol-updown-1h-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `SOLUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T02:00:00Z` |
| SOL | 4h | `cpcsol-sol-updown-4h-2026-11-23-0000z` | 404 | not returned | not returned | not returned | not returned | not returned | not returned | not returned | not returned | not returned |
| XRP | 5m | `cpcxrp-xrp-updown-5m-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `XRPUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T01:05:00Z` |
| XRP | 15m | `cpcxrp-xrp-updown-15m-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `XRPUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T01:15:00Z` |
| XRP | 1h | `cpcxrp-xrp-updown-1h-2026-11-23-0100z` | 200 | `MARKET_STATUS_OPEN` | true | false | `XRPUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-23T01:00:00Z` | `2026-11-23T02:00:00Z` |
| XRP | 4h | `cpcxrp-xrp-updown-4h-2026-11-23-0000z` | 404 | not returned | not returned | not returned | not returned | not returned | not returned | not returned | not returned | not returned |

All 12 successful responses also had `assetPriceTerms.horizon` equal to the
requested horizon. Their `nextWindowStart` values equaled `windowEnd`.

## Exact resolution and tie-rule text

Each fenced block below is the complete, unedited `description` for the named
market. The inclusive tie rule is explicit in every block: `greater than or
equal`.

### BTC 5m: `cpc-btc-updown-5m-2026-11-23-0100z`

```text
This market will settle to Up if the Bitcoin price at 8:05PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI), available at https://www.cfbenchmarks.com/data/indices/BRTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 BRTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 2 decimal places. Please note that this market is about the price according to CF Benchmarks' BRTI, not according to other sources or spot markets.
```

### BTC 15m: `cpc-btc-updown-15m-2026-11-23-0100z`

```text
This market will settle to Up if the Bitcoin price at 8:15PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI), available at https://www.cfbenchmarks.com/data/indices/BRTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 BRTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 2 decimal places. Please note that this market is about the price according to CF Benchmarks' BRTI, not according to other sources or spot markets.
```

### BTC 1h: `cpc-btc-updown-1h-2026-11-23-0100z`

```text
This market will settle to Up if the Bitcoin price at 9:00PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI), available at https://www.cfbenchmarks.com/data/indices/BRTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 BRTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 2 decimal places. Please note that this market is about the price according to CF Benchmarks' BRTI, not according to other sources or spot markets.
```

### ETH 5m: `cpceth-eth-updown-5m-2026-11-23-0100z`

```text
This market will settle to Up if the Ethereum price at 8:05PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Ether-Dollar Real-Time Index (ETHUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/ETHUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 ETHUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 2 decimal places. Please note that this market is about the price according to CF Benchmarks' ETHUSD_RTI, not according to other sources or spot markets.
```

### ETH 15m: `cpceth-eth-updown-15m-2026-11-23-0100z`

```text
This market will settle to Up if the Ethereum price at 8:15PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Ether-Dollar Real-Time Index (ETHUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/ETHUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 ETHUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 2 decimal places. Please note that this market is about the price according to CF Benchmarks' ETHUSD_RTI, not according to other sources or spot markets.
```

### ETH 1h: `cpceth-eth-updown-1h-2026-11-23-0100z`

```text
This market will settle to Up if the Ethereum price at 9:00PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Ether-Dollar Real-Time Index (ETHUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/ETHUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 ETHUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 2 decimal places. Please note that this market is about the price according to CF Benchmarks' ETHUSD_RTI, not according to other sources or spot markets.
```

### SOL 5m: `cpcsol-sol-updown-5m-2026-11-23-0100z`

```text
This market will settle to Up if the Solana price at 8:05PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Solana-Dollar Real-Time Index (SOLUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/SOLUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 SOLUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 4 decimal places. Please note that this market is about the price according to CF Benchmarks' SOLUSD_RTI, not according to other sources or spot markets.
```

### SOL 15m: `cpcsol-sol-updown-15m-2026-11-23-0100z`

```text
This market will settle to Up if the Solana price at 8:15PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Solana-Dollar Real-Time Index (SOLUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/SOLUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 SOLUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 4 decimal places. Please note that this market is about the price according to CF Benchmarks' SOLUSD_RTI, not according to other sources or spot markets.
```

### SOL 1h: `cpcsol-sol-updown-1h-2026-11-23-0100z`

```text
This market will settle to Up if the Solana price at 9:00PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' Solana-Dollar Real-Time Index (SOLUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/SOLUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 SOLUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 4 decimal places. Please note that this market is about the price according to CF Benchmarks' SOLUSD_RTI, not according to other sources or spot markets.
```

### XRP 5m: `cpcxrp-xrp-updown-5m-2026-11-23-0100z`

```text
This market will settle to Up if the XRP price at 8:05PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' XRP-Dollar Real-Time Index (XRPUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/XRPUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 XRPUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 5 decimal places. Please note that this market is about the price according to CF Benchmarks' XRPUSD_RTI, not according to other sources or spot markets.
```

### XRP 15m: `cpcxrp-xrp-updown-15m-2026-11-23-0100z`

```text
This market will settle to Up if the XRP price at 8:15PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' XRP-Dollar Real-Time Index (XRPUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/XRPUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 XRPUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 5 decimal places. Please note that this market is about the price according to CF Benchmarks' XRPUSD_RTI, not according to other sources or spot markets.
```

### XRP 1h: `cpcxrp-xrp-updown-1h-2026-11-23-0100z`

```text
This market will settle to Up if the XRP price at 9:00PM ET on Nov 22, 2026 is greater than or equal to the price at 8:00PM ET on Nov 22, 2026. Otherwise, this market will settle to Down. Outcome sourced from CF Benchmarks' XRP-Dollar Real-Time Index (XRPUSD_RTI), available at https://www.cfbenchmarks.com/data/indices/XRPUSD_RTI. The price at the beginning and at the end of the time range is determined the same way: in the last minute before each of those two times, 60 XRPUSD_RTI prices are collected. The official value for that time is the simple average of these prices, rounded to the nearest 5 decimal places. Please note that this market is about the price according to CF Benchmarks' XRPUSD_RTI, not according to other sources or spot markets.
```

## Catalog evidence and the 4h distinction

The public catalog query was
`GET /v1/markets?active=true&closed=false&categories=crypto&limit=100&offset=N`.
Offsets 0 through 1400 in steps of 100 each returned HTTP 200 and 100 rows.
Offset 1500 returned HTTP 200 and four rows, terminating pagination. The script
reported:

```text
TOTAL_ACTIVE_OPEN_CRYPTO_ROWS=1504
ACTIVE_OPEN_UPDOWN_ROWS=16
```

The 16 Up/Down rows were the 12 current 5m/15m/1h markets in the direct table
plus these four 1d rows. All catalog pages returned HTTP 200; these four were
not separately requested by slug.

| Asset | Full 1d slug | status | active | closed | indexSymbol | tick | minimumTradeQty | feeCoefficient | windowStart | windowEnd |
|---|---|---|---|---|---|---:|---:|---:|---|---|
| BTC | `cpc-btc-updown-1d-2026-11-22-2200z` | `MARKET_STATUS_OPEN` | true | false | `BRTI` | 0.01 | 0.01 | 0.095 | `2026-11-22T22:00:00Z` | `2026-11-23T22:00:00Z` |
| ETH | `cpceth-eth-updown-1d-2026-11-22-2200z` | `MARKET_STATUS_OPEN` | true | false | `ETHUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-22T22:00:00Z` | `2026-11-23T22:00:00Z` |
| SOL | `cpcsol-sol-updown-1d-2026-11-22-2200z` | `MARKET_STATUS_OPEN` | true | false | `SOLUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-22T22:00:00Z` | `2026-11-23T22:00:00Z` |
| XRP | `cpcxrp-xrp-updown-1d-2026-11-22-2200z` | `MARKET_STATUS_OPEN` | true | false | `XRPUSD_RTI` | 0.01 | 0.01 | 0.095 | `2026-11-22T22:00:00Z` | `2026-11-23T22:00:00Z` |

No row had `assetPriceTerms.horizon=4h` or a slug containing `-updown-4h-`.
Thus the four direct 404s are not the sole basis for the active-market finding:
the filtered catalog also listed no active, unclosed crypto 4h Up/Down market at
`2026-11-23T01:02:45.625821+00:00`. This does **not** prove that no 4h market
exists outside that filter or at another time. The live catalog used offset
pagination, not an immutable historical snapshot.

## Exact primary evidence commands

Both commands were run from `/opt/superbot`. They used only HTTP GET and did not
import project configuration or credential-loading code.

### Direct markets

Started `2026-11-23T01:00:32.882317+00:00`:

```bash
.venv/bin/python - <<'PY'
import datetime as dt
import json
import time
import urllib.error
import urllib.request

now = dt.datetime.now(dt.timezone.utc)
print('BATCH_QUERY_UTC=' + now.isoformat())
prefixes = {'BTC': 'cpc-btc', 'ETH': 'cpceth-eth', 'SOL': 'cpcsol-sol', 'XRP': 'cpcxrp-xrp'}
horizons = ('5m', '15m', '1h', '4h')

def window_start(horizon):
    if horizon == '5m':
        return now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    if horizon == '15m':
        return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    if horizon == '1h':
        return now.replace(minute=0, second=0, microsecond=0)
    return now.replace(hour=now.hour - now.hour % 4, minute=0, second=0, microsecond=0)

for asset, prefix in prefixes.items():
    for horizon in horizons:
        start = window_start(horizon)
        stamp = start.strftime('%Y-%m-%d-%H%Mz')
        slug = f'{prefix}-updown-{horizon}-{stamp}'
        url = 'https://gateway.polymarket.us/v1/market/slug/' + slug
        request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'superbot-readonly-verification/1.0'}, method='GET')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode('utf-8')
                payload = json.loads(body)
                market = payload.get('market', payload)
                terms = market.get('assetPriceTerms') or {}
                selected = {
                    'asset': asset,
                    'queryUtc': dt.datetime.now(dt.timezone.utc).isoformat(),
                    'url': url,
                    'httpStatus': response.status,
                    'slug': market.get('slug'),
                    'status': market.get('status'),
                    'active': market.get('active'),
                    'closed': market.get('closed'),
                    'indexSymbol': terms.get('indexSymbol'),
                    'horizon': terms.get('horizon'),
                    'tick': market.get('orderPriceMinTickSize'),
                    'minimumTradeQty': market.get('minimumTradeQty'),
                    'feeCoefficient': market.get('feeCoefficient'),
                    'windowStart': terms.get('windowStart'),
                    'windowEnd': terms.get('windowEnd'),
                    'nextWindowStart': terms.get('nextWindowStart'),
                    'description': market.get('description'),
                }
                print(json.dumps(selected, ensure_ascii=True, sort_keys=True))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            print(json.dumps({'asset': asset, 'queryUtc': dt.datetime.now(dt.timezone.utc).isoformat(), 'url': url, 'httpStatus': exc.code, 'slug': slug, 'body': body}, ensure_ascii=True, sort_keys=True))
        except Exception as exc:
            print(json.dumps({'asset': asset, 'queryUtc': dt.datetime.now(dt.timezone.utc).isoformat(), 'url': url, 'httpStatus': None, 'slug': slug, 'error': repr(exc)}, ensure_ascii=True, sort_keys=True))
        time.sleep(0.25)
PY
```

### Active crypto catalog

Started `2026-11-23T01:02:45.625821+00:00`:

```bash
.venv/bin/python - <<'PY'
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

base = 'https://gateway.polymarket.us/v1/markets'
page_size = 100
print('CATALOG_QUERY_UTC=' + dt.datetime.now(dt.timezone.utc).isoformat())
all_rows = []
for offset in range(0, 2000, page_size):
    query = urllib.parse.urlencode({'active': 'true', 'closed': 'false', 'categories': 'crypto', 'limit': page_size, 'offset': offset})
    url = base + '?' + query
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'superbot-readonly-verification/1.0'}, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode('utf-8'))
            rows = payload.get('markets', [])
            print(json.dumps({'url': url, 'httpStatus': response.status, 'topLevelKeys': sorted(payload), 'rowCount': len(rows)}, sort_keys=True))
            all_rows.extend(rows)
            if len(rows) < page_size:
                break
    except urllib.error.HTTPError as exc:
        print(json.dumps({'url': url, 'httpStatus': exc.code, 'body': exc.read().decode('utf-8', errors='replace')}, sort_keys=True))
        break
    time.sleep(0.3)
print('TOTAL_ACTIVE_OPEN_CRYPTO_ROWS=' + str(len(all_rows)))
updown = [m for m in all_rows if '-updown-' in (m.get('slug') or '')]
print('ACTIVE_OPEN_UPDOWN_ROWS=' + str(len(updown)))
for m in sorted(updown, key=lambda row: row.get('slug') or ''):
    terms = m.get('assetPriceTerms') or {}
    print(json.dumps({'slug': m.get('slug'), 'status': m.get('status'), 'active': m.get('active'), 'closed': m.get('closed'), 'indexSymbol': terms.get('indexSymbol'), 'horizon': terms.get('horizon'), 'windowStart': terms.get('windowStart'), 'windowEnd': terms.get('windowEnd'), 'tick': m.get('orderPriceMinTickSize'), 'minimumTradeQty': m.get('minimumTradeQty'), 'feeCoefficient': m.get('feeCoefficient')}, ensure_ascii=True, sort_keys=True))
PY
```

## Gaps

1. No current 4h market object was returned, so 4h status flags, index, tick,
   minimum quantity, fee coefficient, window timestamps, and resolution text
   remain unavailable. The negative finding is limited to the exact 404 slugs
   and the dated active, unclosed crypto-catalog snapshot.
2. This is not evidence about historical, inactive, closed, delisted, or
   differently categorized 4h markets, and not evidence that 4h markets can
   never be listed.
3. The market-level `feeCoefficient=0.095` does not establish the fee tier
   applicable to any account. Account-tier proof remains open because this
   assignment prohibited authentication and order previews.
4. Order-type verification, configuration changes, fee probes, and other
   T17-01 acceptance items were outside this read-only assignment. This report
   therefore does not close all of T17-01.

## Required checks

### Non-ASCII grep

Run from `/opt/superbot` at `2026-11-23T01:22:33Z`:

```bash
if grep -nP '[^\x00-\x7F]' docs/T17_POLY_PRODUCTION_EVIDENCE.md; then
    echo 'NON_ASCII_CHECK=FAIL'; exit 1
else
    rc=$?
    if [ "$rc" -eq 1 ]; then
        echo 'NON_ASCII_CHECK=PASS (grep exit 1: no matches)'
    else
        echo "NON_ASCII_CHECK=ERROR (grep exit $rc)"; exit "$rc"
    fi
fi
```

Result: **PASS**. Output: `NON_ASCII_CHECK=PASS (grep exit 1: no matches)`.
The same grep was rerun after this final section was added.

### Source-integrity gate

Run from `/opt/superbot` at `2026-11-23T01:22:33Z`:

```bash
.venv/bin/python scripts/verify_source.py --all
```

Result: **PASS**. Exit status 0; every listed file was `clean`, and the final
output line was `all clean`.
