# T17-07 Real-Data Audit - synthetic fallback removal & production data provenance

## Status
PARTIAL overall.

- backtest_engine.py synthetic 45c fallback: **REMOVED** (PASS) - already absent at HEAD; missing quote => market skipped, never invented.
- Remaining synthetic/invented fallbacks still present in disabled or gated paths: signals.py maker win-probability and price fallbacks; gates.py fixed 0.003 sigma fallback. Documented below as gaps; not removed here because they live in money-path files (signals.py, gates.py) that require independent review per R3, and the task instructs read-only-first unless a minimal removal is clearly needed.

## Method
Read-only audit. No venue network calls, no live orders/balances/secrets, no git mutations, no source/config/test edits.
Searched tree for: `0.45`, `45c`, `fallback`, `synthetic`, `hardcoded price`, `DEFAULT_PRICE`, `mock price`, `interpolat`.
Files inspected: backtest_engine.py, gates.py, signals.py, trader.py, poly_us_trader.py, kalshi_client.py, polymarket_us_client.py, binance_feed.py, coinbase_feed.py, cryptofeed_feed.py, crossvenue.py, brti_tracker.py, fair_value.py.
Focused tests run: test_backtest_engine, test_brti_tracker, test_gates_sigma_v2, test_binance_feed_ws, test_gates, test_signals, test_fair_value, test_quote_freshness.
Source gates: scripts/verify_source.py --all -> all clean (exit 0). All inspected modules py_compile clean.

This document serves as the T17-07 data-provenance record (v17 acceptance names docs/DATA_PROVENANCE.md).

---

## 1. Synthetic 45c fallback - backtest_engine.py - PASS (already removed)

Target: "Remove the synthetic 45c fallback ... a market without a real quote is skipped and counted."

Evidence (HEAD, backtest_engine.py):
- Line 74 docstring: "the engine never invents an entry price, because a synthetic fill price makes every downstream number a fiction."
- Lines 125-128 (early_maker path): no quote for the entry minute => `return None`. Comment: "No quote for this minute: skip the market. A made-up entry price (previously a fixed 45c) would turn a data gap into a fake result."
- Lines 137-139: `ep = entry_price_proxy; if ep is None or ep <= 0: return None`. The `ep = ya if ya else 0` path (lines 114-115) yields `ep = 0` when no market ask exists, which is then skipped at line 138. Missing data => no fill, never a fabricated price.
- `grep` for `45\|0.45\|DEFAULT_PRICE\|mock\|synthetic` in backtest_engine.py returns only lines 74 and 127, both removal comments. No executable synthetic price remains.
- tests/test_backtest_engine.py: 22 passed.

Verdict: PASS. The 45c fallback is gone and missing quotes are skipped.

---

## 2. Per-venue data path: production data vs synthetic/invented fallback

### Kalshi - production data (PASS)
- kalshi_client.py: signed Kalshi v2 REST client. `/markets` is public; portfolio endpoints require RSA-PSS authentication. Real production data.
- kalshi_ws.py: Kalshi WebSocket book feed.
- kalshi_client.py:582 "using whole response as fallback" is a response-parsing fallback, not a price invention.

### Polymarket US - production data (PASS)
- polymarket_us_client.py: authenticated client (Key ID + Ed25519 secret from the protected mirror), production API.
- poly_us_stream.py: WebSocket BBO feed.
- polymarket_us_client.py:439-440: GTD fallback disabled ("fallback disabled"); no order price is invented.

### Binance / Coinbase spot - production data; cross-venue fallback uses REAL venue prices (not synthetic) - PARTIAL (document fallback venue use)
- coinbase_feed.py: Coinbase Advanced Trade WebSocket ticker. Staleness bound `_WS_STALE_SECONDS = 1.0` (line 38); `spot_price()` returns `None` when the tick is older than 1.0 s (lines 168-169). Spot older than 1.0 s => no trade, confirmed on the read path.
- binance_feed.py: Binance REST ticker `get_price()` (lines 407-434).
- binance_feed.py:376-399 `_fallback_sources()`: OKX, Kraken, Gateio, Coinbase, RedStone. These are REAL venue prices (OKX/Gateio/Kraken/Coinbase tickers, RedStone oracle cache), not synthetic/invented numbers, but they are cross-venue fallbacks when Binance is unreachable.
- binance_feed.py:469-486 `composite_price()`: median across reachable venues (a BRTI-style proxy, documented as a proxy).
- binance_feed.py:489-523 `composite_sigma()`: Binance klines first, then OKX klines fallback. Real venue data, fallback venue.

### BRTI / Kalshi settlement index - production-venue proxy, no direct real-time BRTI license - PARTIAL (proxy, not synthetic)
- brti_tracker.py:8-10: "Since we don't have direct real-time BRTI access under our license, this module builds a proxy from constituent exchanges (Binance, Coinbase, OKX)."
- brti_tracker.py:15: proxy is median-of-mids across venues; running 60-second average exposed as `index_ref` (get_index_ref, lines 345-353).
- brti_tracker.py:270-322: records proxy-vs-settlement error (error_bps) by hour for basis-risk measurement. Not synthetic - it compares the proxy against actual settlement values.

---

## 3. Invented data sweep by file

| File | Finding | Line(s) | Status |
|------|---------|---------|--------|
| backtest_engine.py | 45c fallback removed; missing quote => skip | 125-128, 137-139 | PASS |
| gates.py | fixed sigma fallback `sigma = 0.003` when ATR missing but spot present ("fixed_fallback") | 588-590 | GAP - invented vol on decision path |
| gates.py | sigma_per_min falls back to binance_feed.composite_sigma (OKX fallback) - real venue data | 224-243 | OK (venue fallback) |
| gates.py | spot cache GATES_SPOT_CACHE_SECONDS=30s stale-ok; fails closed beyond window (F-08 tension with 1.0s rule) | 103-128, 167-182 | NOTE |
| signals.py | MAKER invents win probability `trade_prob = 0.95 + (entry_c - 94) * 0.01` (95% at 94c, 99c at 99c, linear interpolation) | 915-920 | GAP - invented probability (F-27); maker disabled by default |
| signals.py | maker price fallbacks `or 50`, `or 1`, `or 99` | 173, 178-179, 206, 214 | GAP - invented price fallbacks (maker disabled) |
| trader.py | "No quote means no fill" assertions present | 1073, 3170 | PASS |
| kalshi_client.py | parse fallback only, no price invention | 582 | OK |
| polymarket_us_client.py | GTD fallback disabled | 439-440 | OK |
| binance_feed.py | cross-venue fallback prices (OKX/Kraken/Gateio/Coinbase/RedStone) - real venue prices, not synthetic | 376-399, 407-434, 489-523 | OK (documented) |
| coinbase_feed.py | WS spot, 1.0s stale => None | 38, 168-169 | PASS |
| cryptofeed_feed.py | optional adapter; production smoke failed (Coinbase 401, Binance 451) - not production evidence | - | NOTE |
| crossvenue.py | CDF interpolation only within observed strike range; `compare()` refuses extrapolation ("extrapolating a CDF is inventing a number"); to_cdf requires >=3 rungs | 358-406 | PASS (careful) |

Not price-related `0.45` hits (confirmed not data fallbacks): .env.example:54 `LLM_EVAL_MIN_CONFIDENCE=0.45` (LLM confidence threshold), kl_tools.py:218 example logloss values.

---

## 4. Production data provenance & spot staleness

Spot > 1.0 s means no trade: CONFIRMED on the Coinbase WS path.
- coinbase_feed.py:38 `_WS_STALE_SECONDS = float(os.environ.get("WS_SPOT_STALE_SECONDS", "1.0"))`
- coinbase_feed.py:168-169 `if time.monotonic() - ts > _WS_STALE_SECONDS: return None`

Provenance summary:
- Spot: Binance REST ticker + Coinbase WS ticker; Binance cross-venue fallback (OKX/Kraken/Gateio/Coinbase/RedStone) only when Binance unreachable.
- Books: Kalshi WS book; Polymarket US WS BBO. Trading refuses on stale/one-sided books (resting guard / gate 5).
- Prints: settlement from `expiration_value >= floor_strike` (Kalshi) and venue-stated outcome (Polymarket US); backtest_engine `_default_settlement_yes_won` (lines 212-221).
- Settlement index: Kalshi 15M settles on CF Benchmarks BRTI 60-second average; no direct real-time BRTI license, so BRTI-style 60s median-of-mids proxy from Binance/Coinbase/OKX with error tracking (brti_tracker.py).
- Entry gates cache spot for GATES_SPOT_CACHE_SECONDS = 30 s (F-08: not subsecond on the entry path); WS spot path enforces 1.0 s. Tension noted: the 1.0 s rule applies to the WS feed, but gate 3/entry use a 30 s cached spot.

---

## 5. Focused tests & source gates - PASS

- tests/test_backtest_engine.py: 22 passed (includes minute-quote entry, stop-loss, no-trades-zero, settlement).
- tests/test_brti_tracker.py + tests/test_gates_sigma_v2.py: 24 passed (benign thread teardown CancelledError warning only).
- tests/test_binance_feed_ws.py + tests/test_gates.py + tests/test_signals.py + tests/test_fair_value.py + tests/test_quote_freshness.py: 125 passed.
- scripts/verify_source.py --all: all clean, exit 0.
- py_compile on backtest_engine.py, signals.py, gates.py, binance_feed.py, coinbase_feed.py, brti_tracker.py, crossvenue.py, kalshi_client.py, polymarket_us_client.py, poly_us_trader.py, trader.py: all OK.

ASCII check on this document: clean (run `LC_ALL=C grep -n '[^ -~]'`).

---

## Remaining gaps (recommended follow-up, not applied in this audit)

1. **signals.py:915-920** - MAKER mode invents `trade_prob = 0.95 + (entry_c - 94) * 0.01`. Not measured or model-derived. Maker path is disabled by default (T17-21 maker disabled until real fills). Recommend hard-disable or remove; needs independent review (R3).
2. **signals.py:173, 178-179, 206, 214** - maker price fallbacks `or 50` / `or 1` / `or 99`. Same maker path; same recommendation.
3. **gates.py:588-590** - `sigma = 0.003 if spot else None` fixed_fallback volatility used in gate 3 (time-decay cushion) when ATR is missing but spot exists. Invented constant on the decision path; R8 says missing data means no fill. Recommend failing closed (no cushion_z => block) instead. gates.py is a money-path file; needs a different agent's review before any edit.
4. **Data provenance doc** - v17 acceptance names docs/DATA_PROVENANCE.md. This file (docs/T17_07_REAL_DATA_AUDIT.md) contains the provenance record; consider committing docs/DATA_PROVENANCE.md as the canonical home with a pointer here.

No source/config/test edits were made in this audit; the backtest_engine 45c removal was already present at HEAD and is verified by tests and grep.