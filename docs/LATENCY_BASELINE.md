# Latency baseline — measured p50/p95/p99

Source: `local_data/superbot_local_dry.db` → `latency_measurements` table
(written by `trader.py` through `scripts/latency_baseline.py`). Dry-run
(paper) mode, no live order leaves the host.

Window: **2026-11-18 06:32 UTC → 2026-11-19 19:21 UTC**. Recomputed with SQL
percentiles over `duration_ms` per `stage` in that table.

| Stage | n | p50 | p95 | p99 | max | Budget |
|---|---|---|---|---|---|---|
| `market_find` (discovery, 9 series + orderbook) | 6365 | 883 ms | 1611 ms | 1693 ms | 251 693 ms | < 2 000 ms |
| `order_place` (local order-build + intent write) | 17 | 0.4 ms | 0.5 ms | 0.6 ms | 0.6 ms | < 50 ms |

Notes:

- Six `market_find` samples exceed 10 s (max 251 s) — cycles where the process
  was paused or the venue throttled. Dropping them leaves p99 at 1693 ms, so
  the percentile is not driven by the outliers.
- `order_place` is the local write path, not the venue acknowledgement. The
  REST round-trip is recorded as `order_ack` once live mode is enabled, which
  does not happen in dry-run.
- The Polymarket process does not yet write `latency_measurements` (its DB has
  no such table); H6/H8 wiring records `spot_age_ms` and `guard_tick_ms` in the
  logs instead. Add the recorder there before enabling event-driven wake-ups on
  the Polymarket side.
