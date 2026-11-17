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

-- ─── pnl_ledger: idempotent settlement P&L ledger ──────────────────────────
-- Every settlement P&L is recorded exactly once via idempotency_key.
-- Used for audit and reconciliation against Kalshi /portfolio/settlements.
create table if not exists public.pnl_ledger (
    idempotency_key   text primary key,
    market_ticker     text not null,
    realized_pnl_dollars numeric not null,
    fee_dollars       numeric default 0,
    applied_at        timestamptz not null default now(),
    source            text not null default 'local',
    synced            boolean default false,
    created_at        timestamptz not null default now()
);
create index if not exists idx_pnl_ledger_market on public.pnl_ledger(market_ticker, applied_at);
create index if not exists idx_pnl_ledger_synced on public.pnl_ledger(synced);

-- ─── D1: deterministic signal_id for idempotent remote upserts ────────────
-- The local->remote sync posts signals with on_conflict=signal_id, so a
-- retried row updates instead of duplicating. UNAPPLIED migration: apply in
-- the Supabase dashboard SQL editor (operator action required).
alter table public.signals add column if not exists signal_id text;
create unique index if not exists signals_signal_id_key
    on public.signals (signal_id) where signal_id is not null;

-- ─── D2: Polymarket US local-first mirror tables (UNAPPLIED migration) ───
-- The poly trader's system of record is local_data/superbot_local_poly_
-- {dry,live}.db; the sync thread mirrors these same-named tables. Apply in
-- the Supabase dashboard SQL editor (operator action required). Idempotent.
create table if not exists public.poly_orders (
    id     text primary key,
    ts     timestamptz not null default now(),
    slug   text not null,
    side   text,
    action text not null default 'buy',
    qty    numeric,
    price  numeric,
    cost   numeric,
    fee    numeric,
    status text not null default 'resting',
    mode   text not null default ''
);
create index if not exists poly_orders_slug_ts_idx on public.poly_orders (slug, ts desc);

create table if not exists public.poly_positions (
    id   text primary key,
    slug text not null,
    side text,
    qty  numeric,
    cost numeric,
    price numeric,
    mode text not null default ''
);

create table if not exists public.poly_fills (
    id       text primary key,
    ts       timestamptz not null default now(),
    slug     text not null,
    side     text,
    action   text not null default 'buy',
    qty      numeric,
    price    numeric,
    fee      numeric,
    order_id text,
    mode     text not null default ''
);
create index if not exists poly_fills_slug_ts_idx on public.poly_fills (slug, ts desc);

create table if not exists public.poly_settlements (
    id      text primary key,
    ts      timestamptz not null default now(),
    slug    text not null,
    side    text,
    qty     numeric,
    payout  numeric,
    pnl     numeric,
    outcome integer,
    mode    text not null default ''
);
create index if not exists poly_settlements_slug_ts_idx on public.poly_settlements (slug, ts desc);

create table if not exists public.poly_ledger (
    id          text primary key,
    ts          timestamptz not null default now(),
    event       text not null,
    slug        text not null default '',
    payload_json text,
    mode        text not null default ''
);

create table if not exists public.poly_epochs (
    id           text primary key,
    started_at   timestamptz not null default now(),
    start_balance numeric,
    cash         numeric,
    mode         text not null default ''
);

create table if not exists public.poly_calibration (
    id           text primary key,
    ts           timestamptz not null default now(),
    payload_json text not null,
    mode         text not null default ''
);

-- RLS on for all poly tables; service_role bypasses it (house rule), the
-- policies exist for any future key-based access.
alter table public.poly_orders      enable row level security;
alter table public.poly_positions   enable row level security;
alter table public.poly_fills       enable row level security;
alter table public.poly_settlements enable row level security;
alter table public.poly_ledger      enable row level security;
alter table public.poly_epochs      enable row level security;
alter table public.poly_calibration enable row level security;
do $$
declare t text;
begin
    foreach t in array array['poly_orders','poly_positions','poly_fills',
                             'poly_settlements','poly_ledger','poly_epochs',
                             'poly_calibration']
    loop
        execute format('create policy if not exists service_role_all on public.%I '
                       'for all to service_role using (true) with check (true)', t);
    end loop;
end $$;
