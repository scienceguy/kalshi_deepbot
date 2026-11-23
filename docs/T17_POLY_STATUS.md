# T17-01 Polymarket US Verification

## Result

- Verification date: 2026-11-22 (UTC)
- HEAD checked: `e692c946c669ed688270969723a40e260e353591` (`e692c94`)
- Document reviewed: `docs/POLY_US_MARKETS.md` at that HEAD
- Overall result: **PARTIAL**
- Scope: current BTC/ETH/SOL/XRP 15-minute Up/Down production status, CF
  Benchmarks reference, tie rule, and fee tier only.

The document contains specific, dated historical production-API observations,
but they are not sufficient to establish the current state. Its market slugs are
truncated, it preserves neither the original command nor raw JSON, and its only
quoted tie-rule description is for BTC. A fresh unauthenticated read-only
production metadata attempt returned HTTP 401 for all four assets, so this
review obtained no current market payload. The 401 responses must not be
interpreted as evidence that the markets do not exist.

No credentials or secrets were read or supplied. No orders, order previews,
balances, authenticated endpoints, live trading, source/config/test edits,
process changes, or git mutations were used.

## Evidence assessment

| Item | Result | Assessment |
|---|---|---|
| Current BTC/ETH/SOL/XRP 15m production status | **FAIL** | Lines 85-90 record `MARKET_STATUS_OPEN` only for a 05:45:00Z-06:00:00Z window on 2026-11-22. The slugs are elided with `...`, and no command or raw response is preserved. The fresh 23:15Z-window requests returned 401, so current status is unverified. |
| CF Benchmarks reference | **PARTIAL** | Lines 85-90 record `indexSymbol` values BRTI, ETHUSD_RTI, SOLUSD_RTI, and XRPUSD_RTI. Lines 96-102 quote BTC settlement text naming CF Benchmarks BRTI and ETH averaging text. Those are specific historical observations, but there is no raw payload or current revalidation, and no verbatim resolution text for SOL/XRP. |
| Tie rule | **PARTIAL** | The BTC quotation at lines 96-99 explicitly says `greater than or equal`, supporting BTC's inclusive tie rule for that observed market. The document does not quote equivalent rule text separately for ETH/SOL/XRP, and no current descriptions were obtained. |
| Market `feeCoefficient` | **PARTIAL** | Lines 85-90 record 0.095 for all four historical observations. No raw field payload was retained and the fresh requests could not retrieve it. |
| Real applicable fee tier | **FAIL** | A market-level 0.095 coefficient alone does not document the requesting account's trailing-volume or new-account tier. Lines 116-118 state that the earlier order-preview response contained no fee field. No preview was run here, and no independent account-tier evidence is present. |

The older claims above are reported only as claims contained in
`docs/POLY_US_MARKETS.md`, not as newly confirmed venue facts.

## Fresh production read-only check

Command run from `/opt/superbot`; query timestamp printed by the command was
`2026-11-22T23:29:24.190930+00:00`. It attempted one unauthenticated GET per
asset for the 23:15:00Z window, with no retries:

```bash
.venv/bin/python - <<'PY'
import datetime as dt
import json
import time
import urllib.error
import urllib.request

now = dt.datetime.now(dt.timezone.utc)
start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
stamp = start.strftime('%Y-%m-%d-%H%Mz')
prefixes = {'BTC': 'cpc-btc', 'ETH': 'cpceth-eth', 'SOL': 'cpcsol-sol', 'XRP': 'cpcxrp-xrp'}
print('QUERY_UTC=' + now.isoformat())
print('WINDOW_START_UTC=' + start.isoformat())
for asset, prefix in prefixes.items():
    slug = f'{prefix}-updown-15m-{stamp}'
    url = 'https://api.polymarket.us/v1/market/slug/' + slug
    print('\nASSET=' + asset)
    print('URL=' + url)
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'superbot-readonly-verification/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode('utf-8')
            print('HTTP_STATUS=' + str(response.status))
            payload = json.loads(body)
            market = payload.get('market', payload) if isinstance(payload, dict) else payload
            if isinstance(market, dict):
                keys = ('slug', 'status', 'active', 'closed', 'assetPriceTerms', 'orderPriceMinTickSize', 'minimumTradeQty', 'feeCoefficient', 'description', 'startDate', 'endDate', 'expirationDate', 'eventSlug')
                selected = {key: market.get(key) for key in keys if key in market}
                print(json.dumps(selected, indent=2, sort_keys=True, ensure_ascii=True))
            else:
                print(body)
    except urllib.error.HTTPError as exc:
        print('HTTP_ERROR=' + str(exc))
        print(exc.read().decode('utf-8', errors='replace'))
        if exc.code == 429:
            break
    except Exception as exc:
        print('ERROR=' + repr(exc))
        break
    time.sleep(2)
PY
```

Provider/API output, verbatim:

```text
QUERY_UTC=2026-11-22T23:29:24.190930+00:00
WINDOW_START_UTC=2026-11-22T23:15:00+00:00

ASSET=BTC
URL=https://api.polymarket.us/v1/market/slug/cpc-btc-updown-15m-2026-11-22-2315z
HTTP_ERROR=HTTP Error 401: Unauthorized
Missing required API key headers


ASSET=ETH
URL=https://api.polymarket.us/v1/market/slug/cpceth-eth-updown-15m-2026-11-22-2315z
HTTP_ERROR=HTTP Error 401: Unauthorized
Missing required API key headers


ASSET=SOL
URL=https://api.polymarket.us/v1/market/slug/cpcsol-sol-updown-15m-2026-11-22-2315z
HTTP_ERROR=HTTP Error 401: Unauthorized
Missing required API key headers


ASSET=XRP
URL=https://api.polymarket.us/v1/market/slug/cpcxrp-xrp-updown-15m-2026-11-22-2315z
HTTP_ERROR=HTTP Error 401: Unauthorized
Missing required API key headers

```

This run returned 401, not 429. Because every request failed before returning a
market object, it provides no fresh evidence for or against market availability,
index references, tie rules, or fee values.

## Required source-integrity check

Run from `/opt/superbot` at `2026-11-22T23:31:42Z`:

```bash
.venv/bin/python scripts/verify_source.py --all
```

Result: **PASS**. Exit status 0; final output line: `all clean`.

## Required ASCII check

Run from `/opt/superbot` at `2026-11-22T23:38:15Z`:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
path = Path('docs/T17_POLY_STATUS.md')
data = path.read_bytes()
bad = [(index, byte) for index, byte in enumerate(data) if byte > 0x7f]
if bad:
    print('ASCII FAIL:', bad[:20])
    raise SystemExit(1)
print(f'ASCII PASS: {path} ({len(data)} bytes)')
PY
```

Result: **PASS**. Output was
`ASCII PASS: docs/T17_POLY_STATUS.md (7091 bytes)` before this section was
added; the same check was rerun after the final edit.

## Gaps preventing PASS

1. Obtain a successful current production metadata response for each of BTC,
   ETH, SOL, and XRP, preserving the query timestamp, exact read-only command,
   HTTP status, complete slug, and relevant unedited JSON fields.
2. Preserve each market's `assetPriceTerms` and full resolution description so
   the CF Benchmarks index and inclusive/exclusive tie rule are explicit per
   asset rather than inferred from BTC or a summary table.
3. Record the current `feeCoefficient`, and separately evidence the actual
   account-applicable fee tier. Do not equate a market coefficient with an
   account tier without venue documentation or account-specific read-only
   evidence.
4. Keep failed provider responses distinct from negative market evidence. A
   401/429 cannot confirm or refute market existence or rules.

Full T17-01 is not closed by this report. The 5m/1h/4h catalog, order types,
and configuration changes were outside this verification-only assignment.
