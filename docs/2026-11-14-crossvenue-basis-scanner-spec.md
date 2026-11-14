# Cross-Venue Basis Scanner — Kalshi vs Polymarket US (crypto threshold markets)

**Status:** spec. Read-only scanner. **No orders on either venue, ever.**
**Auth:** none required for scanning — Kalshi `/markets` is public; Polymarket US read endpoints work unauthenticated. Scanner must run with zero trading credentials mounted.
**Scope note:** Polymarket **US** (`api.polymarket.us`, SDK `polymarket_us`) — NOT the international venue (`gamma-api.polymarket.com` + CLOB). They are different books; never mix them in one calculation.

---

## 1. What "the same contract" means here (verified live 2026-11-14)

Both venues publish the *same* crypto threshold contracts with the same settlement index and the same window. Live pair, both observed this session:

| | Kalshi | Polymarket US |
|---|---|---|
| Ticker / slug | `KXBTC15M-26NOV141030-30` (event `KXBTC15M-26NOV141030`) | `cpc-btc-updown-15m-2026-11-14-1515z` |
| Contract | `strike_type=greater_or_equal`, `floor_strike=65579.67` | `marketType=ASSET_PRICE_MARKET_TYPE_UP_DOWN`, `priceToBeat=65579.67` |
| Window | `close_time=2026-11-14T15:30:00Z` | `windowStart=…15:15:00Z`, `windowEnd=…15:30:00Z` |
| Index | BRTI (CF Benchmarks) | `indexSymbol="BRTI"` (same index) |
| Top of book | `yes_bid_dollars='0.19'`, `yes_ask_dollars='0.20'`, `no_*` both present | `bestBid/bestAsk` via `get_event_by_slug` payload (92/137 rungs populated) or `get_market_book(slug)` |
|

Hourly ladder (the other matchable pair): Kalshi `KXBTC-26NOV1411-T78299.99` (`greater`, close 16:00Z) ↔ Poly `cpc-btc-above-hr-2026-11-14-1600z-<strike>` (strike every $100, `windowEnd=…16:00:00Z`, `assetPriceTerms.strike.value`, per-market `bestBidQuote`/`bestAskQuote` **embedded in the event response** — one HTTP call returns the whole ladder). Kalshi `B*` "between" rungs ↔ Poly `cpc-btc-range-hr-…-<bucket>`; Kalshi `less` ↔ Poly `…-below-<strike>`.

Key facts that shape the design (measured, not assumed):

- Kalshi 15M series (`KXBTC15M`, `KXETH15M`, …) has **one tradeable market per window**; hourly/daily series quote a **ladder** of rungs.
- Kalshi top-of-book comes inside `/markets?series_ticker=…&status=open` — **no second call needed**. `/markets/{t}/orderbook` returns `orderbook_fp: {yes_dollars:[], no_dollars:[]}` and was **empty** at probe time; treat it as supplemental depth only.
- `yes_bid_dollars='0.0000'` means **no quote**, not free money. 0-price = absent side.
- Poly `outcomePrices == [bestBid, bestAsk]`, i.e. it is **not a mid**, and may be absent (`"0.0100"` placeholder on one rung) or a single element — parse defensively.
- Poly `bestBidQuote/bestAskQuote` exist only on liquid rungs (92 of 137 hourly rungs at probe time). Skip rungs lacking them; do not fall back to `currentPx` (last trade) as a quote.
- Poly US fee fields on the market object: `feeCoefficient: 0.095`, `orderPriceMinTickSize: 0.01`, `minimumTradeQty: 0.01`. **Maker orders (`participateDontInitiate=True`) pay 0**; taker pays ≈ `0.095 · p · (1−p)`.
- Kalshi taker fee = `0.07 · qty · p · (1−p)` dollars per contract (`fees.taker_fee_dollars`, 6dp ceil). At p=0.5 → 1.75c/contract; p=0.9 → 0.63c; p=0.99 → 0.099c. The task's "~7c per $100" is the rough order of magnitude; use the exact quadratic from `fees.py`.
- Kalshi ticker hour is **US Eastern** (`KXBTC15M-26NOV141030` → 10:30 ET → 15:30Z in November); Poly `windowEnd` is **UTC**. Convert before comparing (`crossvenue.to_utc` already does this, with a post-2007 DST rule).
- Boundary semantics differ: Kalshi `greater_or_equal` (≥) / Poly "above" and up/down — both reference BRTI, so ties are measure-zero but nonzero at exactly the strike price; hourly Kalshi strikes are quoted as `X99.99` (e.g. 78299.99) which maps to Poly strike `round(x.99 + 0.01)` on its $100 grid. "Between" rungs and Poly `range-hr` buckets differ by tie handling at bucket edges (99.99 vs 00.00).

## 2. Matching algorithm

Identity key, in priority order:

1. **Time.** `kalshi.close_time(UTC) == poly.windowEnd(UTC)` — to the second. For 15M markets also check `windowStart` matches the ticker window; reject if either side fails.
2. **Asset.** Kalshi series `KX(BTC|ETH|SOL|…)15M|KXBTC|KXBTCD` → lower asset ∈ {btc, eth, sol, …} for the slug.
3. **Strike.** Nearest Poly strike-grid point to `round(kalshi_floor_strike)`:
   - `|floor − poly_strike| ≤ half_grid` (hourly grid = $100; 15M has no grid).
   - Kalshi `greater_or_equal`/`greater` → Poly above-hr or updown priceToBeat (15M only).
   - Kalshi `between` `[lo, hi]` → Poly below/range bucket with matching edges.
   - Kalshi `less` → Poly below bucket.
4. **Strike-grid miss tolerance:** if no rung is within half-grid, do NOT force-match. Fall back to **CDF interpolation** (`crossvenue.to_cdf`): build monotone-interpolated P(above K) for each venue and compare at the Kalshi strikes (±1 grid cell only). Never extrapolate a CDF outside its sampled range.
5. **Tie audit (one-time per pair type, then cache):** record how each venue resolves `price == strike` in `data/basis/tie_rules.json` so `>=` vs `>` drift is visible in logs instead of silently mispricing near the strike.

Output of matching: a `Pair` per (kalshi_ticker, poly_slug) + confidence: `exact` (15M single-market match, same priceToBeat) or `grid` (hourly, nearest-rung) or `cdf` (interpolated). Only `exact` and close `grid` pairs produce actionable flags; `cdf` pairs are logged as informational.

## 3. Scanner design

### 3.1 Data acquisition (per venue, per tick)

| Venue | Call | Cost | Fields used |
|---|---|---|---|
| Kalshi | `GET /trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=100` (public, no auth) | 1 HTTP call | `ticker`, `event_ticker`, `strike_type`, `floor_strike`, `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `close_time`, `volume`, `open_interest` |
| Poly US | `polymarket_us_client.get_event_by_slug("<asset>-updown-15m-<date>-<hhmm>z")` (public read) | 1 HTTP call | `markets[]`: `slug`, `assetPriceTerms{strike,priceToBeat,windowEnd,horizon,indexSymbol}`, `bestBidQuote.value`, `bestAskQuote.value`, `outcomePrices`, `marketSides[0]{long}`, `feeCoefficient`, `updatedAt` |
| Poly US fallback | `get_market_book(slug)` → `marketData.bestBid/bestAsk.value` | 1 call per rung (use only when event payload lacks quotes) | as above |

Hourly ladder: parse quotes from the event response directly (no per-strike calls). 15M: single market, trivial.

**Do not** use `kalshi_client.get_markets()` as-is — it has a **30s cache**, which defeats a 15s polling cadence. Either bypass the cache (module-level `requests.get` like `crossvenue.py`), or call `invalidate_markets(series)` first. Same caution on the Poly side: `get_market_book` enforces 0.2s gap and `time.sleep`s — fine at 2 calls/tick.

### 3.2 Venue fixes baked in

- Kalshi: `0.0000` bid/ask ⇒ absent. `mid_yes = (bid+ask)/2` only when both present; else ask, else bid, else **skip the rung**. Spread > 10c ⇒ skip (not a price).
- Poly: `bestBidQuote/bestAskQuote` authoritative; `outcomePrices` ties to (ask, bid) at probe time but is NOT a mid; `currentPx` = last trade — never used as a quote.
- Stale quotes: Poly rungs carry `updatedAt`; skip if older than 120s. Kalshi: skip rungs whose `yes_bid==0 && yes_ask==0`.
- `minutes_left = (windowEnd − now_utc)/60`; skip if ≤ 0; freeze flags in the final 60s (settlement imminent, quotes meaningless).

### 3.3 Basis and edge formulas

```
# mid basis (screening signal; the formula required by this spec)
basis_yes_c = (kalshi_yes_mid - poly_yes_mid) * 100        # + = Kalshi YES richer
basis_no_c  = (kalshi_no_mid  - poly_no_mid ) * 100        # + = Kalshi NO  richer

# REQUIRED edge formula — long YES on Poly, short YES on Kalshi, Kalshi leg at MID
edge_screen_c        = (kalshi_yes_mid - poly_yes_ask) * 100.0

# executable form — what you actually get when you SELL YES into the Kalshi bid
edge_exec_c          = (kalshi_yes_bid - poly_yes_ask) * 100.0
# equivalent two-leg structure "long Poly YES + long Kalshi NO":
edge_pair_c          = (1 - poly_yes_ask - kalshi_no_ask) * 100.0
# note: edge_pair_c == edge_exec_c exactly when Kalshi's no_ask == 1 - yes_bid
#       (holds on a consistent book; VERIFY — Kalshi also quotes no_ask directly)

# symmetric NO case — long NO on Poly, short NO on Kalshi
edge_no_screen_c     = (kalshi_no_mid - poly_no_ask) * 100.0
edge_no_exec_c       = (kalshi_no_bid - poly_no_ask) * 100.0
```

**Fee-adjusted flag threshold** (per leg, fees on the side you pay):

```
kalshi_taker_c(p) = ceil(0.07 * p * (1-p) * 100, 6dp-ish)   # fees.taker_fee_dollars (exact, repo-verified)
poly_taker_c(p)   = feeCoefficient * p * (1-p) * 100        # feeCoefficient=0.095 observed on the market object;
                                                            # quadratic SHAPE is an assumption until confirmed
                                                            # against Poly US fee docs or /orders/preview
poly_maker_c      = 0                                       # participateDontInitiate (repo-verified)

flag_if  edge_exec_c  >  kalshi_taker_c(k_price) + poly_fee_c + slip_c + basis_buffer_c
flag_if  edge_no_exec_c > kalshi_taker_c(k_price) + poly_fee_c + slip_c + basis_buffer_c

slip_c         = half-spread per rung (ask-bid)/2, ≥ 1c floor
basis_buffer_c = historical |venue settlement disagreement| in cents; start at 2c,
                 replace with the measured BRTI-vs-Poly-reference dispersion from
                 crossvenue_history.py output once available
poly_fee_c     = maker path: 0c | taker path: poly_taker_c(p)
k_price        = Kalshi leg execution price
```

Practical consequence: a **maker-entry on both venues** needs only `edge_exec_c > slip_c + basis_buffer_c` (≈ 3–4c). A **taker-entry on both** at p≈0.9 needs `edge_exec_c > ~1.5c fees + 3–4c` ≈ **5–6c**.

### 3.4 Pseudocode

```python
def tick(now):
    for asset in ("btc", "eth", "sol"):
        kal = kalshi_open_markets(f"KX{asset.upper()}15M")          # public, no cache
        poly_ev_slug = poly_updown_15m_slug(asset, now)             # cpc-{a}-updown-15m-<date>-<hhmm>z
        poly_mkts = poly_get_event(poly_ev_slug).get("markets", []) # 1 call, BBO embedded
        for pmkt in poly_mkts:
            pair = match(asset, kal, pmkt)                          # §2
            if not pair or pair.minutes_left() < 1: continue
            kq, pq = kalshi_book(pair.kalshi), poly_book(pmkt)      # fields, not refetches
            if not fresh(kq) or not fresh(pq): continue
            rows = []
            for side in ("yes", "no"):
                e_screen = (kq.mid[side] - pq.ask[side]) * 100
                e_exec   = (kq.bid[side] - pq.ask[side]) * 100
                fee      = kalshi_taker_c(kq.price[side]) + poly_leg_fee_c(pq.price[side])
                thresh   = fee + slip_c(pq) + BASIS_BUFFER_C
                rows.append((side, e_screen, e_exec, thresh))
                if e_exec > thresh: emit(pair, side, e_screen, e_exec, thresh, pair.confidence)
            log_jsonl(INTER_VENUE_ROWS, rows)                       # append-only
    refresh_hourly_ladders()                                        # KXBTC/KXBTCD vs above-hr/range-hr
```

`emit()` writes one JSONL row per flag and (optionally) pushes to the existing n8n `superbot-monitor` webhook as an **informational** ack — never a trade trigger.

### 3.5 Non-blocking integration

- **Best form: separate process** (`basis_scanner.py`, launched by `continuous_loop.sh` alongside the trader, or under termux-job-scheduler). It shares *nothing* with the order path — no locks, no shared memory, no socket.
- If threaded into `trader.py` instead: run in a daemon thread with `CYCLE_TIMEOUT_SECONDS`-style budget, hard-bounded join of ≤2s, all exceptions swallowed into `log.warning`, and **never** touching `state`, risk limits, or order placement. It may only write `data/basis/YYYYMMDD.jsonl`.
- Per-tick network budget: 2 calls/asset/venue-pair (≤6 total) + ≤2 hourly-ladder calls; hard wall-clock cap 5s per tick; `timeout=3` on every HTTP call; on any 429/5xx → cooldown ×4 (60s), keep last-good quotes flagged `stale`, never raise into the trader.
- Supervisor must not treat scanner death as trader death (it isn't). Scanner exits nonzero → supervisor restarts it, logs, continues.

## 4. Risk factors (ranked)

1. **Settlement-reference disagreement.** For the US updown-15m markets both venues settle on **BRTI** — the gap is small but not zero (BRTI 60s VWAP at expiry vs each venue's oracle read latency, plus the tie question at exactly `priceToBeat`). For hourly `above-hr`/`range-hr`, Poly carries `indexSymbol: BRTI` as well, so the risk is again timing/tie rules, NOT a Binance-vs-BRTI g×p — *but verify `indexSymbol` per event and again in `settlementPrice` after close; do not assume.* The "Binance 1m vs BRTI" disagreement applies to **international** Polymarket and is the reason crossvenue.py calls it a deviation study, not an arb. If anyone ever swaps in Gamma, this factor jumps from tail-risk to structural. Include `basis_buffer_c` in the threshold and keep `crossvenue_history.py`'s settled-ladder basis study scheduled — it is the only legitimate source of the buffer.
2. **Timing mismatch.** Kalshi ticker hour is ET; Poly windows are UTC (`…-1515z`). A one-hour ET/UTC error pairs expiry-16:00 with window-15:00 and produces pure garbage flags. Mitigate with `to_utc()` + a startup self-check that asserts `kalshi.close_time == poly.windowEnd` for the live 15M pair before emitting anything (run once per process, fail loud if mismatched). Also: settle times differ after close — Kalshi settles off the 60s average at T; Poly reads its oracle after `windowEnd` (latencies observed: see `docs/poly_us_settlement_latency.md`). A "basis" computed inside the final minute is partly *latency*, not disagreement — freeze flags <60s to expiry.
3. **Fill uncertainty / execution basis.** The spec'd formula uses mid; the Kalshi side of a short-YES executes at `yes_bid`. Mid-vs-executable gap = half the spread each leg. Kalshi rung at probe time had **empty** `orderbook_fp` — thin books defeat any mid. Poly rungs show `askDepth:1, bidDepth:5` — size beyond 1–5 contracts cannot rest at BBO. Two legs can never be guaranteed simultaneous → one leg fills first → directional exposure you did not intend. A maker-maker strategy avoids legs crossing but converts fill risk into adverse-selection risk (you only get filled when the edge was gone). Size every flag by **displayed depth**, not by what the formula wants.
4. **Quote authenticity on Poly.** `outcomePrices` duplicates quotes, not trades; `currentPx` is last trade; some rungs show placeholder `"0.0100"`. Only `bestBid/bestAskQuote` are usable, and 1/3 of rungs lack them.
5. **Boundary/tie semantics.** Kalshi `greater_or_equal` ≥, Poly "above" >, bucket edges 99.99↔00.00 — a flag raised on a pair whose strikes tie at the same price can be systematically long/short the exact tie event. Keep `tie_rules.json` per pair type.
6. **Fee-model mismatch.** Kalshi fee is quadratic in p (max 1.75c @ p=0.5); Poly US `feeCoefficient: 0.095` taker / 0 maker. A threshold calibrated at p=0.5 and applied at p=0.9 underestimates fees by ~2.8× on the Kalshi leg. Compute fees at the actual leg price, per flag.
7. **Stale venue.** One venue can halt/lag while the other moves (Cloudflare 429s on Poly US >5 req/s, Kalshi maintenance). A one-sided stale quote *manufactures* basis. Require both quotes to be age-checked (Poly `updatedAt` ≤120s; Kalshi `close_time` proximity + non-zero book) and require 2 consecutive agreeing ticks before flagging.
8. **Basis regime / adverse selection.** Historical measurements (task4 memo) show Poly US ladders sat ~20c rich on hourly strikes — any "edge" one direction may be a permanent, unhedgeable bias, not noise. Scanner emits direction-tagged flags and logs rolling basis; it does not assume mean-reversion.
9. **Contract-identity error.** Industry base-rate: ~44% of "equivalent" cross-venue events are not equivalent (off-by-one strikes, tie rules, expiry). The matching algorithm must log the disagreement rate (`pairs_found/windows`, `tie_rule`) so a silent format change (e.g. Poly renames `priceToBeat`) is visible.
10. **Operational.** Scanner bugs must never touch the trader's risk state. JSONL only; no order imports; kill-switch = process exit. `data/basis/*.jsonl` can grow — rotate daily, retain 30d.

## 5. Polling cadence recommendation

| Parameter | Recommended | Why |
|---|---|---|
| Cadence | **15 s**, ticks at :00/:15/:30/:45 ±2s jitter | Matches the 15M window quantum and repo's `REQUEST_MIN_GAP_SECONDS=0.2` (5 req/s/venue cap is 10× our usage) |
| Per-tick HTTP | ≤6 calls (2 endpoints × assets) + ≤2 hourly-ladder calls | ~0.5 req/s total — negligible vs both limits |
| HTTP timeout | 3s per call, 5s hard cap per tick | Scanner can never block anything downstream |
| Flag confirmation | 2 consecutive agreeing ticks (~30s persistence) | Kills single-tick flicker and half-recovered books |
| Freeze zone | no new flags at `<60s` to expiry | Settlement-basis dominates |
| 429/5xx | exponential cooldown ×4 → 60s max, serve stale flagged rows | Poly US Cloudflare; never hammer |
| Output | `data/basis/YYYYMMDD.jsonl`, one row per flag, plus `pairs_found` heartbeat every tick | Reviewable; heartbeat distinguishes "no basis" from "scanner dead" |
| If threaded into trader.py | daemon thread, join ≤2s, exceptions → log+continue, zero shared state | Non-negotiable: scanning is not on any critical path to order placement |

Hourly ladders get the same 15s cadence (CDF cache invalidates per tick; event payload is one call). No benefit from faster polling: Kalshi orderbook depth is thin and Poly US has no L2 websocket for these markets that is authenticated-free.

## 6. Validation checklist before trusting any flag

- [ ] `close_time == windowEnd` asserted for the live 15M pair (one-time, both venues).
- [ ] Tie rule recorded for each of the 4 pair structures (15M GMEq/updown, above-hr, range-hr, below).
- [ ] `basis_buffer_c` derived from settled-market study (`crossvenue_history.py`), not guessed.
- [ ] 2-tick persistence + "Poly side uses `bestBid/AskQuote`, never `currentPx`" enforced in code review.
- [ ] Flag row includes: `pair_confidence`, `minutes_left`, `depth_at_bbo`, `kalshi_fee_c`, `poly_fee_c`, `edge_exec_c`, `edge_no_exec_c`, `basis_yes_c`, `basis_no_c`.
- [ ] Dry-run against live data for ≥24h (count flags by direction; expect strong sign-bias from the measured Poly-rich regime) before anyone acts on a flag.

## 7. Deliberately NOT built (ponytail)

- No order placement path, no unwind logic, no size-from-bankroll — read-only.
- No fair-value/GBM model — pure basis; model risk is crossvenue_edge.py's job.
- No websocket/L2 subscription — unauthenticated read endpoints at 15s suffice; L2 adds complexity, not decisions.
- No Gamma/international fallback in the scanner — different settlement reference, different contract; belongs in a separate, clearly-labelled study if ever wanted.
- No persistence beyond JSONL — Supabase/n8n wiring exists elsewhere; add only when someone asks.

## Open questions for the operator

1. Is the 15M updown pair *intended* as the primary match (it has `indexSymbol: BRTI` — near-zero settlement basis), with hourly ladders as the wide-deviation hunt? Or hourly-first?
2. What is the accepted `basis_buffer_c` for BRTI-vs-BRTI (likely 1–2c) once measured vs Binance-family?
3. Who owns the scanner process — `continuous_loop.sh`, termux-job-scheduler (cf. job 7001), or systemd/proot?
