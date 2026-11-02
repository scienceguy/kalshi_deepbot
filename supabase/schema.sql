-- Kalshi SuperBot — Supabase schema.
--
-- Run once in the Supabase dashboard: SQL Editor → paste → Run.
-- Safe to re-run (everything is IF NOT EXISTS / idempotent).
--
-- The bot uses the service_role key, which bypasses RLS, so no policies are
-- needed. Columns match the payloads written by supabase_client.py and the
-- n8n "SuperBot Trade" workflow (which PATCHes orders on settlement).

-- ─── signals: every evaluated trade signal ────────────────────────────────
create table if not exists public.signals (
    id             bigserial primary key,
    ts             timestamptz not null default now(),
    type           text,
    ticker         text not null,
    side           text not null,
    price_cents    integer not null,
    contracts      numeric not null,
    edge           numeric,
    model_prob     numeric,
    reason         text,
    executed       boolean default false,
    blocked_reason text,
    outcome_won    boolean,
    outcome_pnl    numeric,
    settled_at     timestamptz
);
create index if not exists signals_ticker_ts_idx on public.signals (ticker, ts desc);

-- ─── orders: placed orders + settlement fill ──────────────────────────────
create table if not exists public.orders (
    id               bigserial primary key,
    ts               timestamptz not null default now(),
    ticker           text not null,
    side             text,
    contracts        numeric,
    price_cents      integer,
    order_id         text unique,
    status           text,
    fees_dollars     numeric default 0,
    cycles           integer default 0,
    fill_price_cents integer,
    outcome_won      boolean,
    pnl_dollars      numeric,
    settled_at       timestamptz
);
create index if not exists orders_ticker_ts_idx on public.orders (ticker, ts desc);

-- ─── bot_state: key/value state mirror (upsert on key) ────────────────────
create table if not exists public.bot_state (
    id         bigserial primary key,
    ts         timestamptz not null default now(),
    key        text not null unique,
    value_json text not null
);

-- ─── backtest_runs: one row per backtest ──────────────────────────────────
create table if not exists public.backtest_runs (
    id             bigserial primary key,
    ts             timestamptz not null default now(),
    params_json    text,
    ev_per_trade   numeric,
    total_pnl      numeric,
    n_trades       integer,
    win_rate       numeric,
    max_drawdown   numeric,
    final_balance  numeric
);
