# Kalshi SuperBot

Single-operator live-trading bot for Kalshi 15-minute binary crypto markets and Polymarket US.

## Overview

Places favourite-maker limit orders (two-sided YES/NO at 92–99c), evaluates signals via an LLM with 4-layer fallback, writes signals/orders/bot-state to Supabase, and routes settlement P&L through n8n webhooks.

## Quick Start (OCI VPS)

```bash
cd /opt/superbot
cp .env.example .env  # Fill in your credentials
python trader.py --once --dry  # Test run
python trader.py --once --live --dry  # One live cycle (needs KALSHI_CONFIRM_LIVE=true)
python trader.py --live  # Live loop
```

For production, install systemd services (see **Systemd Services** below).

## Environment Variables

See `.env.example` for all required variables. Never commit real credentials.

Secrets are stored in `~/.secrets/` (outside the repo):
- `~/.secrets/kalshi_private.pem` — Kalshi RSA private key (chmod 600)
- `~/.secrets/n8n_api_key` — n8n API key
- `~/.secrets/supabase_service_key` — Supabase service role key
- `~/.secrets/bw/` — Bitwarden mirror (one JSON per item, chmod 600)

Run `python scripts/n8n_creds_sync.py` to sync credentials from the mirror into n8n.

## Architecture

| File | Purpose |
|------|---------|
| `trader.py` | Main trading loop (Kalshi + Polymarket US) |
| `signals.py` | Signal engine (two-sided favourite maker) |
| `market_finder.py` | Market discovery (parallel series fetch) |
| `gates.py` | Pre-entry gates (ATR hurdle, fee-adjusted EV, time-decay cushion, KL floor) |
| `kalshi_client.py` | Signed Kalshi REST client (RSA-PSS) |
| `fees.py` | Kalshi fee math (configurable `KALSHI_FEE_RATE`) |
| `risk.py` | Risk management (cash floor, exposure ceiling, drawdown halt, per-market loss limit) |
| `resilience.py` | Cycle supervisor with backoff and client rebuild |
| `n8n_client.py` | n8n webhook/API client (health check + webhooks) |
| `supabase_client.py` | Supabase REST client with circuit breaker + batch writes |
| `llm_eval.py` | LLM trade evaluation (4-layer fallback, per-market verdict cache) |
| `polymarket_us_client.py` | Polymarket US SDK wrapper (US API at api.polymarket.us) |
| `poly_us_stream.py` | Private order/position stream for Polymarket US |
| `poly_us_trader.py` | Polymarket US trading loop (dry-run by default) |
| `spot_consensus.py` | Cross-venue spot composite (Binance + Coinbase WS) |
| `crossvenue.py` | Three-way ladder comparison (spot vs Kalshi vs Polymarket) |
| `crossvenue_edge.py` | Spot-with-age getter + BRTI basis measurement |
| `model.py` | GBM fair value baseline (d2 formula) |
| `ml_model_lgb.py` | LightGBM/XGBoost calibrated model (opt-in via `--ml-model`) |
| `forecast14.py` | Multi-model 14-day equity forecaster |
| `box_arbitrage.py` | Cross-venue box arb engine (YES/NO legs) |
| `pnl_reconcile.py` | P&L reconciliation (local DB vs Supabase vs n8n) |
| `backtest.py` / `backtest_engine.py` | Parameter sweep backtester (replay from stored ticks) |
| `data_collector.py` | Market data collector for backtest DB |
| `kalshieye_downloader.py` | Kalshi Eye orderbook downloader |
| `local_db.py` | Local SQLite (WAL) — positions, orders, fills, ledger, control, outbox |
| `mempalace_bridge.py` | Single-owner MemPalace bridge (no-drop spill + sanctioned reads) |
| `agent_bus.py` | Inter-agent coordination (roles, topics, ack protocol) |
| `control.py` | Signed control commands (pause_entries, resume, reduce caps) |
| `events.py` / `emit.py` | Durable event log with idempotency keys |
| `scripts/train_ml.py` | ML model training (XGBoost/LightGBM) |
| `scripts/job_bridge.py` | Loopback command bridge (n8n → scripts) |
| `scripts/scheduled_jobs.py` | Scheduled jobs (digest, retrain, collect) |

## n8n Workflows

Seven workflows injected into n8n via `inject_n8n_workflows.py` (direct DB upsert; REST API returns 401 upstream):
- `superbot-backtest` — Parameter sweep backtest
- `superbot-collect` — Market data collection
- `superbot-trade` — Settlement processor (webhook → Supabase PATCH)
- `superbot-monitor` — Position monitoring (ack-only)
- `superbot-coordinate` — Agent dispatch (task.request/ack)
- `superbot-control` — Signed control commands (pause/resume/reduce caps)
- `superbot-digest` — Daily digest builder (replaces Termux job 7001)

All webhooks are HMAC-signed per-identity (`SUPERBOT_WEBHOOK_SECRET_<SUFFIX>`). No Execute Command node (n8n 2.x default). Delivery via durable `event_outbox` in local SQLite with idempotency keys.

## Systemd Services

All services run from `/opt/superbot-releases/current` (release directory, not the dev tree).

### Install Services

```bash
# Core services (dry + live)
sudo install -m 644 deploy/superbot-dry.service /etc/systemd/system/
sudo install -m 644 deploy/superbot.service /etc/systemd/system/
# Live requires drop-in with KALSHI_CONFIRM_LIVE=true (see deploy/superbot.service)

# Auxiliary services
sudo install -m 644 deploy/superbot-bus-inbox.service /etc/systemd/system/
sudo install -m 644 deploy/superbot-job-bridge.service /etc/systemd/system/
sudo install -m 644 deploy/superbot-jobs.service /etc/systemd/system/
sudo install -m 644 deploy/superbot-jobs.timer /etc/systemd/system/
sudo install -m 644 deploy/superbot-creds-guard.service /etc/systemd/system/
sudo install -m 644 deploy/superbot-creds-guard.timer /etc/systemd/system/
sudo install -m 644 deploy/superbot-retrain.service /etc/systemd/system/
sudo install -m 644 deploy/superbot-retrain.timer /etc/systemd/system/
sudo install -m 644 deploy/poly-us-dry.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now superbot-dry.service superbot-bus-inbox.service superbot-job-bridge.service superbot-jobs.timer superbot-creds-guard.timer superbot-retrain.timer poly-us-dry.service
```

### Service Overview

| Service | Purpose | Schedule |
|---------|---------|----------|
| `superbot-dry` | Paper trader (Kalshi) | Continuous |
| `superbot` | Live trader (Kalshi) | Continuous (needs drop-in) |
| `poly-us-dry` | Paper trader (Polymarket US) | Continuous |
| `superbot-bus-inbox` | MemPalace inbox watcher | Continuous |
| `superbot-job-bridge` | Loopback bridge (port 18711) | Continuous |
| `superbot-jobs` | Scheduled jobs (digest, collect) | Every 15 min via timer |
| `superbot-creds-guard` | Credential sync from BW mirror | Every 15 min via timer |
| `superbot-retrain` | ML model retrain (XGBoost/LightGBM) | Every 6 hours via timer |

### Live Confirmation

The live service (`superbot.service`) requires an operator-created drop-in:

```bash
sudo install -d -m 755 /etc/systemd/system/superbot.service.d
printf '[Service]\nEnvironment=KALSHI_CONFIRM_LIVE=true\n' | \
    sudo tee /etc/systemd/system/superbot.service.d/live-confirm.conf
sudo chmod 644 /etc/systemd/system/superbot.service.d/live-confirm.conf
sudo systemctl daemon-reload && sudo systemctl restart superbot.service
```

This ensures a clone cannot accidentally start live trading.

## Continuous ML Training

`scripts/train_ml.py` retrains the fair-value model from settled Kalshi history and writes the gitignored artifact `data/ml_model.pt`. It is **paper only** and is **not** wired into `trader.py` — the model loads only when `--ml-model` is passed.

- Uses XGBoost or LightGBM (native, no proot)
- Scheduled every 6 hours by `superbot-retrain.timer`
- Logs append to `local_data/retrain_ml.log`

Install native ML libraries:

```bash
pip install xgboost lightgbm --no-binary :all:  # or use system packages
```

## Running Tests

```bash
cd /opt/superbot
python -m pytest tests/ -q
# 1386+ passed, 3 skipped (as of 2026-11-20)
```

Pre-commit hooks enforce:
- No secrets in staged files
- Token corruption guards (4 gates: verify_source, verify_compiles, verify_undefined, verify_operators)
- Large file / log pattern guard (1MB limit)

Run manually:

```bash
python3 scripts/verify_source.py --all
python3 scripts/verify_compiles.py --all
python3 scripts/verify_undefined.py --all
python3 scripts/verify_operators.py
python3 scripts/check_tmp_writes.py
```

## E2E Smoke Test

```bash
python3 scripts/e2e_smoke.py
# 7/7 pillars must pass
```

## Credentials — Durable Store

Agents keep deleting n8n credentials. The secrets are NOT stored in n8n or in this repo as the source of truth. They live in two places, both outside the repo:

1. **Bitwarden** (ultimate source) — every "secure note" in the vault.
2. **`~/.secrets/bw/`** — durable local mirror, one protected JSON per item (`chmod 600`, dir `chmod 700`) plus `index.json`.

n8n holds a **copy** of every credential, recreated from the mirror at any time.

| Task | Command |
|------|---------|
| Recreate every credential in n8n | `python scripts/n8n_creds_sync.py` |
| Verify all are present | `python scripts/n8n_creds_sync.py --check` |
| List mirror items | `python scripts/n8n_creds_sync.py --list` |
| Also delete n8n creds not in mirror | `python scripts/n8n_creds_sync.py --prune` |
| Refresh mirror from Bitwarden | `BW_PASSWORD=... python scripts/bw_pull_secure_notes.py` |

**Kalshi key trap:** The LIVE Kalshi pair is Bitwarden's **"Kalshi GPT API Key"** + **"Kalshi GPT RSA Key"**. The items named plain **"Kalshi API Key"** / **"Kalshi RSA Key"** are **STALE** (different key id, `INCORRECT_API_KEY_SIGNATURE`). After any `bw_pull_secure_notes.py` run, restore those two files from `.env` `KALSHI_KEY_ID` + `~/.secrets/kalshi_private.pem`.

**Polymarket US markets:** The 15-minute crypto up/down series (`cpc-btc-updown-15m`) is on the **US production API** at `api.polymarket.us` (preprod note was stale). Settlement is CF Benchmarks BRTI (same as Kalshi). The `gateway.polymarket.us` public endpoints currently return 404 for 15m markets; authenticated API is required for live book data.

## License

Private