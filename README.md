# Kalshi SuperBot

Single-operator live-trading bot for Kalshi 15-minute binary crypto markets.

## Overview

Places favourite-maker limit orders (two-sided YES/NO at 82–93c), evaluates signals via an LLM with 7-layer fallback, writes signals/orders/bot-state to Supabase, and routes settlement P&L through n8n webhooks.

## Quick Start

```bash
cd kalshi_deepbot
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
- `gates.py` — Pre-entry gates (ATR hurdle, fee-adjusted EV, time-decay cushion)
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

## Trend maker (ARCHIVED — paper only, unreachable)

`trend_maker.py` is kept on disk for reference but is **no longer wired into
`trader.py`**: setting `TREND_MAKER_ENABLED=true` no longer runs it. It was
model-gated and **paper only** — never placed or cancelled a real order — and
was archived on 2026-11-10 because every measured cut lost money net of taker
fees (negative bootstrap CI; the replay CI lower bound never cleared zero), so
the objective's fallback applied. Reverse: `git revert <this-sha>`.

The archived engine can still be run directly against a corpus by the analysis
scripts:

```bash
python scripts/trend_report.py                      # CI on net P&L per round trip
```

Each cycle it fits a slope to the YES mid, averages the enabled models
(`TREND_MAKER_MODELS=gbm,momentum[,ml]`), and joins the bid on the side where fair
value beats price by `TREND_MAKER_MIN_EDGE_CENTS` and the trend agrees. After a fill it
rests a sell `TREND_MAKER_TP_CENTS` higher; a taker exit (with fee) happens only on the
stop or the flatten clock. Every decision is appended to `trend_maker_log.jsonl`.
Size is a capped fraction of equity, so it compounds only if paper equity grows.
On a synthetic efficient market it loses money, as it should: it needs a real
information edge (e.g. Kalshi lagging spot) to pay for stop-outs.

## License

Private