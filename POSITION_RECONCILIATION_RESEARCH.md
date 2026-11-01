# Position Reconciliation & Resting Order Seeding — Research Report

**Project:** Kalshi SuperBot (`/data/data/com.termux/files/home/kalshi_superbot`)
**Topic:** Trading bot position reconciliation and resting order seeding on restart
**Date:** November 2026
**Status:** Research findings — implementation recommendations

---

## Executive Summary

This report documents research on how trading bots should reconcile local state with exchange state on restart, covering position seeding, resting order reconciliation, orphan/ghost positions, PnL reconciliation, idempotency, startup loops, dust thresholds, and decision persistence.

All source material was fetched directly from Kalshi's official API documentation (primary source) and the CCXT manual. The existing `kalshi_superbot` already implements partial reconciliation (position seeding from `GET /portfolio/positions`, resting-order seeding from `GET /portfolio/orders`, session PnL reconciliation from Supabase, and a local settlement queue replay). This report identifies the gaps and provides concrete implementation advice for closing them.

**Key finding:** The bot currently reconciles *after* it has already connected and started cycling — it fetches positions inside `cycle()` and seeds resting orders in `main()` but neither step is treated as a hard gate. There is no orphan-position detection, no idempotency keying, no dust thresholds, and no persisted reconciliation decisions. These are the areas to strengthen.

---

## 1. Seeding Position State from Exchange on Restart

### Key insight / recommendation

On startup, fetch the **complete current position state from the exchange** before placing any new orders, and use that as the source of truth to rebuild local position state. For Kalshi this means:

- `GET /portfolio/positions` (unsettled by default; add `settlement_status=settled` or `=all` to include recently settled positions still in the live data set)
- `GET /portfolio/balance` for available cash
- `GET /exchange/user_data_timestamp` to know how fresh the returned data is (Kalshi warns there is typically a short delay before exchange events are reflected in API endpoints)

Match local state to exchange state by **market ticker** (the unique market identifier). For each exchange position, reconstruct the local position record (side, entry price, contracts, cost basis, realized PnL, fees paid). For each local position with no exchange counterpart, flag a ghost position (see section 3). For each exchange position with no local counterpart, flag an orphan position (see section 3).

**Source:** https://docs.kalshi.com/api-reference/portfolio/get-positions.md, https://docs.kalshi.com/api-reference/portfolio/get-balance.md, https://docs.kalshi.com/api-reference/exchange/get-user-data-timestamp.md

### Practical implementation advice (Python)

```python
from dataclasses import dataclass, field
from typing import Optional
import logging

log = logging.getLogger("reconcile")

@dataclass
class ExchangePosition:
    ticker: str
    exchange_index: int
    position_fp: float        # net contracts (+YES, -NO)
    position_cost_dollars: float
    realized_pnl_dollars: float
    fees_paid_dollars: float
    last_updated_ts: str

@dataclass
class SeedResult:
    positions: dict[str, ExchangePosition]   # ticker -> position
    cash: float
    portfolio_value: float
    data_as_of: Optional[str]                # user_data_timestamp
    warnings: list[str] = field(default_factory=list)

def seed_position_state(client) -> SeedResult:
    """Fetch full position + balance state from Kalshi on startup.
    Returns the authoritative state the bot should rebuild from.
    """
    warnings = []

    # 1. How fresh is the data? (Kalshi: events lag API by a short delay)
    try:
        ts = client._request("GET", "/exchange/user_data_timestamp")
        data_as_of = ts.get("as_of_time")
    except Exception as e:
        data_as_of = None
        warnings.append(f"could not read user_data_timestamp: {e}")

    # 2. Balance
    cash = client.get_balance()  # existing helper returns dollars

    # 3. All positions (unsettled + settled still live)
    raw = client._request("GET", "/portfolio/positions",
                          params={"settlement_status": "all", "limit": 1000})
    positions = {}
    for p in raw.get("market_positions", []):
        ticker = p["ticker"]
        positions[ticker] = ExchangePosition(
            ticker=ticker,
            exchange_index=p.get("exchange_index", 0),
            position_fp=float(p.get("position_fp", 0)),
            position_cost_dollars=float(p.get("position_cost_dollars", 0)),
            realized_pnl_dollars=float(p.get("realized_pnl_dollars", 0)),
            fees_paid_dollars=float(p.get("fees_paid_dollars", 0)),
            last_updated_ts=p.get("last_updated_ts", ""),
        )

    return SeedResult(
        positions=positions,
        cash=cash,
        portfolio_value=cash + sum(p.position_cost_dollars for p in positions.values()),
        data_as_of=data_as_of,
        warnings=warnings,
    )
```

**Notes for this codebase:** `kalshi_client.get_positions()` currently only requests the default (unsettled) status and returns `market_positions`. To seed fully, either add a `settlement_status` param to `get_positions()` or call the raw endpoint directly as shown. The existing `cycle()` already does a partial version of this (lines 415-529 of trader.py) but only clears settled tickers — it does not rebuild positions from exchange data as the source of truth.

---

## 2. Resting Order Reconciliation — Detecting Filled / Cancelled / Expired Orders While Offline

### Key insight / recommendation

On restart, fetch **all resting orders** from the exchange and compare each against the bot's local resting-order record. Classify each local resting order into one of:

| Local says | Exchange says | Meaning | Action |
|---|---|---|---|
| resting | resting | Still open | Keep in `resting_value`; resume cash reservation |
| resting | executed | Filled while offline | Convert to position; book any partial fill; release reserved cash |
| resting | canceled | Cancelled while offline | Remove from `resting_value`; release reserved cash |
| resting | absent (not returned) | Expired / cancelled / filled and purged | Treat as gone; release reserved cash; log for audit |

For Kalshi specifically, `GET /portfolio/orders?status=resting` returns resting orders only (canceled/executed orders older than the cutoff move to `GET /historical/orders`). Each order has `fill_count_fp`, `remaining_count_fp`, `initial_count_fp`, `status`, `last_update_time`, and `last_update_reason` (e.g. `Trade`, `ExpiryCancel`, `HaltCancel`, `SettlementBoundsCancel`). Use `remaining_count_fp == 0` and `status == "executed"` to detect a fully filled order; `status == "canceled"` for cancellations.

**Source:** https://docs.kalshi.com/api-reference/orders/get-orders.md, https://docs.kalshi.com/websockets/user-orders.md, https://docs.kalshi.com/getting_started/historical_data.md

### Practical implementation advice (Python)

```python
@dataclass
class RestingOrderRecord:
    order_id: str
    client_order_id: str
    ticker: str
    side: str              # yes/no
    price_cents: int
    contracts: int
    reserved_dollars: float

def reconcile_resting_orders(client, local_resting: dict[str, RestingOrderRecord]) -> dict:
    """
    Compare local resting orders to exchange resting orders.
    Returns a dict with 'keep', 'filled', 'canceled', 'missing' lists
    and updated reserved-cash totals.
    """
    exchange_orders = client.get_orders(status="resting")  # existing helper
    exchange_by_id = {o["order_id"] or o.get("id"): o for o in exchange_orders}

    result = {"keep": [], "filled": [], "canceled": [], "missing": []}

    for oid, local in local_resting.items():
        ex = exchange_by_id.get(oid)
        if ex is None:
            # Not in resting list: filled+cleared, canceled, or expired while offline
            result["missing"].append(local)
            continue

        status = ex.get("status")
        remaining = float(ex.get("remaining_count_fp", 0) or 0)
        fill_count = float(ex.get("fill_count_fp", 0) or 0)

        if status == "executed" or remaining <= 0:
            result["filled"].append({**local.__dict__, "filled_count": fill_count,
                                     "exchange": ex})
        elif status == "canceled":
            result["canceled"].append(local)
        else:
            # status == "resting" — still open, keep reserving cash
            result["keep"].append(local)

    return result
```

**Kalshi-specific notes:** The bot already seeds resting orders in `trader.py` lines 749-765, but it only logs a warning on failure and continues trading. It also does not handle the case where a resting order was partially filled while offline — `fill_count_fp` tells you how many contracts filled; the remainder is still resting. The `last_update_reason` field (Trade, ExpiryCancel, HaltCancel, SettlementBoundsCancel, etc.) explains *why* an order changed, which is useful for audit logging.

---

## 3. Orphan Positions and Ghost Positions

### Key insight / recommendation

Two mismatch classes must be handled explicitly and differently:

- **Orphan position:** Exchange has a position the bot doesn't know about (e.g. placed by another client, a previous run, a subaccount, or a manual trade). The bot must **adopt** it: create a local position record from exchange data (position_fp, position_cost_dollars, realized_pnl_dollars, fees_paid_dollars) and stop treating that ticker as available for new entries. Do **not** place new orders that would conflict with an unknown position.

- **Ghost position:** Bot thinks it has a position (in `state.positions`) but the exchange shows none. This usually means the position settled while the bot was offline and the bot missed the settlement event. The bot should **clear** the local position, book realized PnL from exchange settlement data (not synthetic prices), and release reserved capital.

For Kalshi, positions are keyed by `ticker`. `position_fp` is the net contract count (positive = YES, negative = NO). A ticker present in `GET /portfolio/positions` with a non-zero `position_fp` is an active position; absence means no open position (or it settled/archived — check `settlement_status=settled` and historical positions).

**Source:** https://docs.kalshi.com/api-reference/portfolio/get-positions.md, https://docs.kalshi.com/api-reference/portfolio/get-settlements.md, https://docs.kalshi.com/getting_started/market_settlement.md

### Practical implementation advice (Python)

```python
def classify_position_mismatches(local_positions: dict, exchange_positions: dict,
                                 settlements: list[dict]) -> dict:
    """
    local_positions: ticker -> local position record (from state.positions)
    exchange_positions: ticker -> ExchangePosition (from seed_position_state)
    settlements: list of Settlement records from GET /portfolio/settlements
    Returns dict with 'orphans', 'ghosts', 'confirmed', 'settled_locally'
    """
    settled_tickers = {s["ticker"] for s in settlements}

    orphans = []      # exchange has it, local doesn't
    ghosts = []       # local has it, exchange doesn't
    confirmed = []    # both sides agree

    for ticker, ex_pos in exchange_positions.items():
        if ticker not in local_positions:
            orphans.append(ex_pos)   # adopt this position
        else:
            confirmed.append((ticker, local_positions[ticker], ex_pos))

    for ticker, local_pos in local_positions.items():
        if ticker not in exchange_positions:
            if ticker in settled_tickers:
                # Position settled — this is expected, book PnL from settlement
                continue
            ghosts.append(local_pos)  # concerning: position vanished without settlement

    return {"orphans": orphans, "ghosts": ghosts, "confirmed": confirmed}
```

**Orphan handling:** When adopting an orphan, reconstruct entry price from `position_cost_dollars / abs(position_fp)`, set side from sign of `position_fp`, and mark the ticker as entered so the strategy does not open a conflicting position.

**Ghost handling:** A ghost (local position, no exchange position, no settlement record) is a data-integrity anomaly — log it at ERROR level, do not trade that ticker, and require manual review or a full re-sync from fills history before resuming.

---

## 4. PnL Reconciliation Between Local Tracking and Exchange Trade History / Settlement Data

### Key insight / recommendation

Never trust local PnL as the source of truth. Reconcile against exchange data on every restart:

1. **Settled PnL:** Use `GET /portfolio/settlements` which returns `revenue` (cents), `fee_cost`, `market_result` (yes/no/scalar), `yes_count_fp`/`no_count_fp`, `yes_total_cost_dollars`/`no_total_cost_dollars`, and `settled_time`. This is the authoritative realized PnL per market.

2. **Fill-level PnL:** Use `GET /portfolio/fills` (each fill has `fill_id`, `order_id`, `count_fp`, `yes_price_dollars`, `fee_cost`, `is_taker`, `created_time`) to recompute realized PnL trade-by-trade and compare to your local running total. Fills older than the cutoff move to `GET /historical/fills`.

3. **Position-level PnL:** `GET /portfolio/positions` returns `realized_pnl_dollars` and `fees_paid_dollars` per market — compare these to your local `session_pnl` and per-position PnL.

4. **Balance cross-check:** `GET /portfolio/balance` returns `balance` and `portfolio_value`. Your local cash + position cost should approximately equal exchange portfolio value (within dust threshold and pending-order reserves).

Reconcile in this order: settlements → fills → positions → balance. Any discrepancy beyond the dust threshold should be logged, not silently corrected.

**Source:** https://docs.kalshi.com/api-reference/portfolio/get-settlements.md, https://docs.kalshi.com/api-reference/portfolio/get-fills.md, https://docs.kalshi.com/api-reference/portfolio/get-positions.md, https://docs.kalshi.com/api-reference/portfolio/get-balance.md, https://docs.kalshi.com/getting_started/historical_data.md

### Practical implementation advice (Python)

```python
def reconcile_pnl(client, local_session_pnl: float, local_positions: dict,
                  dust_threshold_dollars: float = 0.05) -> dict:
    """
    Compare local PnL tracking to exchange settlement/fill data.
    Returns a reconciliation report with discrepancies flagged.
    """
    report = {"local_pnl": local_session_pnl, "exchange_pnl": 0.0,
              "discrepancy": 0.0, "within_dust": False, "details": []}

    # 1. Sum realized PnL from settlements (authoritative for settled markets)
    settlements = client._request("GET", "/portfolio/settlements",
                                  params={"limit": 1000})
    exchange_pnl = 0.0
    for s in settlements.get("settlements", []):
        revenue_cents = s.get("revenue", 0)
        fee_cost = float(s.get("fee_cost", 0) or 0)
        exchange_pnl += revenue_cents / 100.0 - fee_cost
        report["details"].append({
            "ticker": s["ticker"],
            "market_result": s["market_result"],
            "revenue_dollars": revenue_cents / 100.0,
            "fee_cost": fee_cost,
            "settled_time": s["settled_time"],
        })

    # 2. Cross-check against positions' realized_pnl_dollars
    positions = client._request("GET", "/portfolio/positions",
                                params={"settlement_status": "all", "limit": 1000})
    for p in positions.get("market_positions", []):
        rp = float(p.get("realized_pnl_dollars", 0) or 0)
        # position-level realized PnL should roughly match settlements for that ticker

    report["exchange_pnl"] = exchange_pnl
    report["discrepancy"] = abs(local_session_pnl - exchange_pnl)
    report["within_dust"] = report["discrepancy"] <= dust_threshold_dollars

    if not report["within_dust"]:
        log.warning("PnL reconciliation discrepancy: local=%.2f exchange=%.2f diff=%.2f",
                    local_session_pnl, exchange_pnl, report["discrepancy"])

    return report
```

**For this codebase:** The bot currently computes PnL from `get_market(t).expiration_value` at settlement time (trader.py lines 446-454) and tracks `session_pnl` locally, reconciled only from Supabase (lines 769-775). It never fetches `GET /portfolio/settlements` or `GET /portfolio/fills` to verify. Adding settlement-based PnL reconciliation on startup would catch cases where n8n webhook writes failed (the settlement_queue.json exists for this, but there's no verification against exchange truth).

---

## 5. Idempotency Keys for Trade / Settlement Events to Prevent Double-Counting

### Key insight / recommendation

Every exchange event that affects PnL or position state must be processed **at most once**. Use idempotency keys derived from immutable exchange identifiers:

- **Fills:** `fill_id` (UUID) — each fill has a unique ID. Deduplicate by `fill_id` before applying to PnL/position state.
- **Orders:** `order_id` — unique per order. Use to detect duplicate order-status updates.
- **Settlements:** `(ticker, settled_time)` or a composite of `ticker + market_result + settled_time` — settlements are per-market, so ticker + settled time is effectively unique.
- **Client orders:** `client_order_id` — you generate this on order placement; Kalshi echoes it back. Use it to correlate your local order record with exchange fill/order events.

Persistence pattern: maintain a processed-events store (e.g. SQLite table `processed_events(event_id PRIMARY KEY, event_type, ticker, processed_at)`). Before applying any fill/settlement, `INSERT OR IGNORE` the idempotency key; if the insert fails (already exists), skip the event.

**Source:** https://docs.kalshi.com/api-reference/portfolio/get-fills.md (fill_id), https://docs.kalshi.com/api-reference/orders/get-orders.md (order_id, client_order_id), https://docs.kalshi.com/api-reference/portfolio/get-settlements.md (ticker, settled_time), https://docs.kalshi.com/websockets/user-fills.md (trade_id, order_id), https://docs.kalshi.com/websockets/user-orders.md (order_id, client_order_id)

### Practical implementation advice (Python)

```python
import sqlite3
from contextlib import contextmanager

class IdempotencyStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    def _init(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,   -- fill | settlement | order
                    ticker TEXT,
                    processed_at TEXT NOT NULL,
                    payload_hash TEXT
                )
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def claim(self, event_id: str, event_type: str, ticker: str = "") -> bool:
        """Returns True if this event was newly claimed (process it),
        False if it was already processed (skip it)."""
        from datetime import datetime, timezone
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO processed_events (event_id, event_type, ticker, processed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (event_id, event_type, ticker, datetime.now(timezone.utc).isoformat()),
                )
                return True
        except sqlite3.IntegrityError:
            return False  # already processed


# Usage in settlement processing:
store = IdempotencyStore("reconciliation_state.db")

def apply_settlement(settlement: dict) -> bool:
    key = f"settlement:{settlement['ticker']}:{settlement['settled_time']}"
    if not store.claim(key, "settlement", settlement["ticker"]):
        log.debug("Skipping already-processed settlement %s", key)
        return False
    # ... book PnL ...
    return True

def apply_fill(fill: dict) -> bool:
    if not store.claim(fill["fill_id"], "fill", fill.get("ticker", "")):
        return False
    # ... update position / PnL ...
    return True
```

**Kalshi-specific:** The websocket `fill` message includes `trade_id` (what you use to differentiate fills) and `order_id`. The REST `Fill` schema has `fill_id` and `trade_id` (legacy, same as fill_id). Use `fill_id` as the idempotency key. For settlements, the REST `Settlement` schema has no single unique ID, so composite key `(ticker, settled_time)` is the practical choice.

---

## 6. Startup Reconciliation Loop Best Practices — Reconcile Before Trading, Halt if Reconciliation Fails

### Key insight / recommendation

Reconciliation must be a **hard gate**, not a best-effort step. The startup sequence should be:

1. Verify credentials/auth (fail fast if key invalid).
2. Fetch exchange state: balance, positions, resting orders, settlements, fills, user_data_timestamp.
3. Run reconciliation: classify orphans/ghosts, match resting orders, reconcile PnL.
4. **If any reconciliation step fails (network error, auth error, data mismatch beyond dust threshold, orphan/ghost detected without resolution), halt and alert — do not trade.**
5. Only after reconciliation succeeds, seed local state and begin the trading loop.

Kalshi's `GET /exchange/user_data_timestamp` tells you when user data (balance, orders, fills, positions) was last validated — use it to detect stale data. If the timestamp is older than your tolerance (e.g. >5 minutes), the exchange data may not reflect recent activity; treat as a reconciliation warning.

**Source:** https://docs.kalshi.com/api-reference/exchange/get-user-data-timestamp.md, https://docs.kalshi.com/api-reference/orders/get-orders.md, https://docs.kalshi.com/api-reference/portfolio/get-positions.md

### Practical implementation advice (Python)

```python
class ReconciliationError(Exception):
    """Raised when startup reconciliation cannot be completed safely."""
    pass

def run_startup_reconciliation(client, state, dust_threshold: float = 0.05) -> dict:
    """Full startup reconciliation. Raises ReconciliationError if unsafe to trade."""
    report = {"ok": False, "steps": [], "orphans": [], "ghosts": [],
              "pnl_discrepancy": 0.0, "resting_seeded": 0}

    try:
        # Step 1: Seed position state from exchange
        seed = seed_position_state(client)
        report["steps"].append("position_state_seeded")

        # Step 2: Classify mismatches
        mismatches = classify_position_mismatches(state.positions, seed.positions, [])
        report["orphans"] = mismatches["orphans"]
        report["ghosts"] = mismatches["ghosts"]

        # Orphans: adopt them (create local records)
        for op in mismatches["orphans"]:
            state.positions[op.ticker] = {
                "side": "yes" if op.position_fp > 0 else "no",
                "entry_price": (op.position_cost_dollars / abs(op.position_fp) * 100
                                if op.position_fp else 0),
                "contracts": abs(op.position_fp),
                "order_id": "orphan:" + op.ticker,
            }
            state.window["entered"].add(op.ticker)

        # Ghosts: hard failure — do not trade until resolved
        if mismatches["ghosts"]:
            raise ReconciliationError(
                f"Ghost positions detected (local has, exchange doesn't): "
                f"{[g.get('ticker') or g.ticker for g in mismatches['ghosts']]}. "
                f"Manual review required before trading."
            )

        # Step 3: Reconcile resting orders
        resting = reconcile_resting_orders(client, state.resting_value)
        report["resting_seeded"] = len(resting["keep"])
        # released cash from filled/canceled/missing is automatically available

        # Step 4: Reconcile PnL
        pnl = reconcile_pnl(client, state.session_pnl, state.positions,
                            dust_threshold_dollars=dust_threshold)
        report["pnl_discrepancy"] = pnl["discrepancy"]
        if not pnl["within_dust"]:
            raise ReconciliationError(
                f"PnL discrepancy {pnl['discrepancy']:.2f} exceeds dust threshold "
                f"{dust_threshold:.2f} — do not trade until resolved."
            )
        state.session_pnl = pnl["exchange_pnl"]  # adopt exchange truth

        report["ok"] = True
        return report

    except ReconciliationError:
        raise
    except Exception as e:
        raise ReconciliationError(f"Reconciliation failed: {e}")


# In main(), before the trading loop:
try:
    recon = run_startup_reconciliation(client, state)
    log.info("Startup reconciliation OK: %d resting orders seeded, "
             "%d orphans adopted, PnL diff $%.2f",
             recon["resting_seeded"], len(recon["orphans"]),
             recon["pnl_discrepancy"])
except ReconciliationError as e:
    log.error("🛑 Startup reconciliation FAILED — halting: %s", e)
    sys.exit(1)
```

**For this codebase:** Currently `main()` seeds resting orders (lines 749-765) and reconciles session_pnl from Supabase (lines 769-775), but both are wrapped in try/except that just logs a warning and continues. There is no halt-on-failure, no orphan/ghost detection, and no exchange-truth PnL reconciliation. The loop in `cycle()` also continues after balance/positions failures (just logs and returns). All of these should be upgraded to hard gates at startup.

---

## 7. Dust Thresholds for Position Reconciliation — When to Ignore Small Discrepancies

### Key insight / recommendation

Never treat zero discrepancy as required. Exchange rounding (fixed-point dollars/cents), fee rounding, partial fills, and sub-cent settlement adjustments mean local and exchange PnL will almost never match exactly. Define a **dust threshold** — a dollar amount below which discrepancies are logged but not acted upon.

For Kalshi (prediction markets, $1 per contract, prices in cents):
- A reasonable dust threshold is **$0.01–$0.10 per market** or **$0.05–$0.50 total session PnL**, depending on trade size.
- Positions below a minimum contract count (e.g. < 1 contract, or position_cost < $0.01) can be treated as dust and ignored for reconciliation purposes.
- Rounding: Kalshi uses fixed-point dollar strings; balances settle in whole cents (`revenue` is in cents, rounded to whole cents). `CollateralAmountChange + MiscFeeAmt` equals pre-rounding settlement value. Always compare in cents (integers) where possible to avoid float drift.

Discrepancies above the dust threshold must be investigated — they indicate missed fills, missed settlements, or a ghost/orphan position.

**Source:** https://docs.kalshi.com/getting_started/fee_rounding.md, https://docs.kalshi.com/getting_started/fixed_point_migration.md, https://docs.kalshi.com/getting_started/market_settlement.md

### Practical implementation advice (Python)

```python
# config.py additions
DUST_THRESHOLD_DOLLARS = 0.05      # ignore PnL diffs below $0.05
DUST_POSITION_CONTRACTS = 0.5      # ignore positions smaller than 0.5 contracts
DUST_POSITION_COST_DOLLARS = 0.01  # ignore positions costing less than $0.01

def is_dust_position(pos: ExchangePosition) -> bool:
    """True if a position is small enough to treat as dust."""
    return (abs(pos.position_fp) < DUST_POSITION_CONTRACTS or
            abs(pos.position_cost_dollars) < DUST_POSITION_COST_DOLLARS)

def pnl_within_dust(local: float, exchange: float, threshold: float) -> bool:
    return abs(local - exchange) <= threshold
```

**Application:** In `reconcile_pnl()` (section 4), if discrepancy <= dust threshold, adopt exchange PnL but log at INFO level. If above threshold, adopt exchange PnL anyway for safety but log at WARN/ERROR and require review. Never silently absorb a large discrepancy — it masks a real data-integrity problem.

---

## 8. Persisting Reconciliation Decisions So They Aren't Re-litigated Every Restart

### Key insight / recommendation

Reconciliation decisions should be persisted so that on the next restart the bot does not re-classify the same orphan as "new" or re-book the same settlement. Persist:

1. **Processed event IDs** (idempotency): `fill_id`, `order_id`, `(ticker, settled_time)` — so fills/settlements are applied once (section 5).
2. **Adopted orphans**: Record which exchange positions were adopted as local positions, with the adoption timestamp and exchange position_fp/cost. On next restart, these are expected local positions, not new orphans.
3. **Resolved ghosts**: Record that a ghost was investigated and cleared, so it doesn't re-flag on every restart.
4. **Reconciliation watermark**: The `user_data_timestamp` (or last processed fill/settlement timestamp) at the time of the last successful reconciliation. On next restart, only process events newer than this watermark.
5. **Reconciliation status**: A `reconciliation_state` record with `last_successful_reconciliation_at`, `status` (ok/failed), and `notes`. On startup, if the last reconciliation failed and was never re-run successfully, require manual clearance before trading.

Use an append-only ledger or a SQLite table; never overwrite past decisions — keep history for audit.

**Source:** https://docs.kalshi.com/api-reference/exchange/get-user-data-timestamp.md (as_of_time as watermark), https://docs.kalshi.com/api-reference/portfolio/get-fills.md (fill_id, created_time), https://docs.kalshi.com/api-reference/portfolio/get-settlements.md (settled_time), https://docs.kalshi.com/api-reference/orders/get-orders.md (order_id, last_updated_ts_ms)

### Practical implementation advice (Python)

```python
import sqlite3, json
from datetime import datetime, timezone

class ReconciliationLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    def _init(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reconciliation_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_type TEXT NOT NULL,   -- orphan_adopted | ghost_cleared | pnl_reconciled | resting_synced
                    ticker TEXT,
                    event_id TEXT,                 -- fill_id / order_id / settlement key
                    exchange_position_fp REAL,
                    exchange_cost_dollars REAL,
                    decision_ts TEXT NOT NULL,
                    notes TEXT
                );
                CREATE TABLE IF NOT EXISTS reconciliation_watermark (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_ts TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def record_decision(self, decision_type: str, ticker: str = "",
                        event_id: str = "", exchange_fp: float = 0.0,
                        exchange_cost: float = 0.0, notes: str = ""):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reconciliation_decisions "
                "(decision_type, ticker, event_id, exchange_position_fp, "
                " exchange_cost_dollars, decision_ts, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (decision_type, ticker, event_id, exchange_fp, exchange_cost,
                 datetime.now(timezone.utc).isoformat(), notes))

    def is_decision_recorded(self, decision_type: str, event_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM reconciliation_decisions "
                "WHERE decision_type=? AND event_id=? LIMIT 1",
                (decision_type, event_id)).fetchone()
            return row is not None

    def get_watermark(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM reconciliation_watermark WHERE key=?",
                (key,)).fetchone()
            return row[0] if row else None

    def set_watermark(self, key: str, value: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reconciliation_watermark (key, value, updated_ts) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (key, value, datetime.now(timezone.utc).isoformat()))
```

**Usage pattern:** On startup reconciliation, before adopting an orphan ticker `T`, check `is_decision_recorded("orphan_adopted", f"orphan:{T}")`. If already recorded, skip re-adoption (the local state already reflects it). After a successful reconciliation, `set_watermark("last_reconciliation", user_data_timestamp)`. On subsequent restarts, use `GET /portfolio/fills?min_ts=<watermark>` and `GET /portfolio/settlements?min_ts=<watermark>` to only process new events — this is both an idempotency and an efficiency optimization.

---

## Summary of Gaps in kalshi_superbot and Recommended Priority Order

| # | Gap | Current state | Recommended fix | Priority |
|---|-----|---------------|-----------------|----------|
| 1 | Reconciliation not a hard gate | Warnings only, continues trading | Halt on reconciliation failure (`sys.exit(1)`) | Critical |
| 2 | No orphan position detection | Not implemented | Adopt exchange positions not in local state | Critical |
| 3 | No ghost position handling | Partially clears settled positions, no ghost detection | Flag/local-error on local position with no exchange counterpart | Critical |
| 4 | No idempotency keys | Not implemented | SQLite `processed_events` keyed by fill_id / order_id / (ticker, settled_time) | High |
| 5 | No exchange-truth PnL reconciliation | Reconciles from Supabase only | Reconcile from `GET /portfolio/settlements` + `GET /portfolio/fills` | High |
| 6 | No dust thresholds | Not implemented | Add `DUST_THRESHOLD_DOLLARS`, `is_dust_position()` | Medium |
| 7 | No persisted reconciliation decisions | Not implemented | SQLite `reconciliation_decisions` + watermark table | Medium |
| 8 | No use of `user_data_timestamp` | Not implemented | Check data freshness; use as watermark | Medium |
| 9 | Websocket feeds not used for reconciliation | Not implemented | Subscribe to `user_fills`, `user_orders`, `market_positions` for real-time reconciliation | Low |

---

## Source URLs

1. Kalshi API Reference — Get Orders: https://docs.kalshi.com/api-reference/orders/get-orders.md
2. Kalshi API Reference — Get Positions: https://docs.kalshi.com/api-reference/portfolio/get-positions.md
3. Kalshi API Reference — Get Settlements: https://docs.kalshi.com/api-reference/portfolio/get-settlements.md
4. Kalshi API Reference — Get Fills: https://docs.kalshi.com/api-reference/portfolio/get-fills.md
5. Kalshi API Reference — Get Balance: https://docs.kalshi.com/api-reference/portfolio/get-balance.md
6. Kalshi API Reference — Get User Data Timestamp: https://docs.kalshi.com/api-reference/exchange/get-user-data-timestamp.md
7. Kalshi WebSockets — User Fills: https://docs.kalshi.com/websockets/user-fills.md
8. Kalshi WebSockets — User Orders: https://docs.kalshi.com/websockets/user-orders.md
9. Kalshi WebSockets — Market Positions: https://docs.kalshi.com/websockets/market-positions.md
10. Kalshi — Market Settlement: https://docs.kalshi.com/getting_started/market_settlement.md
11. Kalshi — Historical Data (cutoffs, live vs historical): https://docs.kalshi.com/getting_started/historical_data.md
12. Kalshi — Fee Rounding: https://docs.kalshi.com/getting_started/fee_rounding.md
13. Kalshi — Fixed-Point Representation: https://docs.kalshi.com/getting_started/fixed_point_migration.md
14. Kalshi API Index (llms.txt): https://docs.kalshi.com/llms.txt
15. CCXT Manual (exchange API patterns): https://docs.ccxt.com/en/latest/manual.html

---

## Disclaimer

This report is based on public Kalshi API documentation fetched directly from docs.kalshi.com. Endpoint schemas, rate limits, and field names may change; always verify against the live OpenAPI spec (`https://docs.kalshi.com/openapi.yaml`) before implementing. The Python code snippets are illustrative and adapted to the existing `kalshi_superbot` structure (`kalshi_client.py`, `trader.py`, `config.py`); they should be integrated and tested against the demo environment before live trading.
