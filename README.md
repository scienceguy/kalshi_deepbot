# Kalshi SuperBot

Single-operator live-trading bot for Kalshi 15-minute binary crypto markets.

## Overview

Places favourite-maker limit orders (two-sided YES/NO at 82–93c), evaluates signals via an LLM with 7-layer fallback, writes signals/orders/bot-state to Supabase, and routes settlement P&L through n8n webhooks.

## Quick Start

```bash
cd kalshi_superbot
cp .env.example .env  # Fill in your credentials
python trader.py --once --dry  # Test run
python trader.py --once --live --dry  # One live cycle (needs KALSHI_CONFIRM_LIVE=true)
python trader.py --live  # Live loop
```

## Environment Variables

See `.env.example` for all required variables. Never commit real credentials.

## Architecture

- `trader.py` — Main trading loop
- `signals.py` — Signal engine (two-sided favourite maker)
- `market_finder.py` — Market discovery
- `kalshi_client.py` — Signed Kalshi REST client
- `fees.py` — Kalshi fee math
- `risk.py` — Risk management
- `resilience.py` — Cycle supervisor
- `n8n_client.py` — n8n webhook/API client
- `supabase_client.py` — Supabase REST client
- `llm_eval.py` — LLM trade evaluation (7-layer fallback)
- `latency_arb.py` — Latency arbitrage strategy
- `backtest.py` — Parameter sweep backtester
- `data_collector.py` — Market data collector
- `kalshieye_downloader.py` — Kalshi Eye data downloader
- `inject_n8n_workflows.py` — n8n database injector
- `add_n8n_credentials.py` — n8n credential injector
- `tests/` — Pytest test suite

## n8n Workflows

Four workflows injected into n8n via `inject_n8n_workflows.py`:
- `superbot-backtest` — Parameter sweep backtest
- `superbot-collect` — Market data collection
- `superbot-trade` — Settlement processor (webhook → Supabase PATCH)
- `superbot-monitor` — Position monitoring

## License

Private