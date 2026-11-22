# T16-61 local DB authority audit

## Scope and verdict

- Audited source HEAD: `cf32aa5d817408f5f95213b9992f31147baf49dc` (`Must fix: record independent trader wiring review`). Source was read with `git show HEAD:...`; unrelated worktree changes were not used. At completion, HEAD was `b20f6651729dedff79178dc8e79feae57727496c`; `git diff --name-status cf32aa5d..b20f6651` showed only `docs/T16_PALACE_AUDIT.md` and `tests/test_b15_instance_lock.py`, with no audited local-authority source changes.
- Task text: ignored, uncommitted `AGENT_TASKSv16.md`, SHA256 `2ce2fce597318b3f2af2543538a993bbe3ed4c2284e968734c11c6dc4133f55b`. R9 replaces remote-replica language with local SQLite plus backups and retains the separate n8n outbox (`AGENT_TASKSv16.md:45-50`). T16-61 is at `:588-598`; F-12 is at `:285-288`.
- Read-only audit: no live API/account/runtime DB/secret/process inspection or mutation, no orders, no source/config/test edits, and no git mutations. Tests used isolated temporary paths.
- Overall: **FAIL**. F-12 remains open. Poly JSON is still the decision authority; the migration check does not compare against SQLite; startup is not consistently fail-closed; offsite backup and the required drills are not evidenced.

## Authority at HEAD

| Venue/mode | Cash and positions | Orders | Settlements and PnL |
|---|---|---|---|
| Kalshi live | Venue REST is read for sizing and position seeding (`trader.py:1894-1914,3700-3747`). Local memory and SQLite order IDs link the journal. | `pending_send` and `orders` in SQLite, but intent failure can be ignored and filled-order insertion is best-effort (`trader.py:508-575,3091-3114`). | JSON queue drives replay; SQLite `pnl_ledger` deduplicates PnL, then memory, paper state, bot state, orders, n8n and queue are updated separately (`trader.py:1173-1393`). Live startup prefers ledger PnL (`trader.py:3749-3767`). |
| Kalshi paper | `paper_state.json` and `paper_positions.json` are authoritative (`paper_mode.py:69-125`; `trader.py:187-221`). SQLite stores only a redundant scalar paper-PnL backup. | Same mixed SQLite intent/order path. | JSON paper PnL and positions plus JSON settlement queue and SQLite ledger are separate authorities. |
| Polymarket US, both modes | `self.state` from `poly_us_state.json` drives cash sizing and positions (`poly_us_trader.py:938-998,2242-2264`). Live venue comparison is periodic and fail-open on venue errors (`:1354-1422,2872-2887`). | JSON resting orders plus shared `pending_send`; normalized Poly DB writes are explicitly best-effort (`:2516-2603`). | Shared SQLite ledger write gates a settlement, but JSON state/JSONL and Poly DB mirrors are updated independently (`:1645-1857`). Realized PnL used by the trader comes from JSON state. |

## Acceptance results

| Point | Result | Evidence |
|---|---|---|
| Local schema covers configured/legacy tables | **PARTIAL** | Core `SYNC_TABLES` has five tables and Poly has seven (`local_db.py:165-168,2475-2477`), matching the 12 tables in legacy `supabase/schema.sql`. Local-only control/outbox/market tables also exist (`local_db.py:249-596,2488-2659`). There is no table registry or DB-authority flag in `config.py`; only sync/retry settings exist (`config.py:467-503`). No runtime schema was inspected. |
| Poly cash, positions and ledger move behind a DB flag; JSON becomes export-only | **FAIL** | No authority flag exists. Code explicitly says JSON is authoritative (`poly_us_trader.py:904-910,988-998,1047-1058,1110-1131`). `_load_state()` never reads the SQLite state snapshot (`:938-986`). |
| Poly orders/settlements/PnL have one atomic authority | **FAIL** | `apply_poly_cycle()` is transactional (`local_db.py:2691-2727`), but production never calls it directly; individual best-effort mirrors do (`local_db.py:2771-2899`; `poly_us_trader.py:1321-1336,1792-1857,2591-2602`). The trader drops ledger operations when its bounded queue fills (`poly_us_trader.py:1005-1018`). |
| Poly restart cannot lose or double-credit settlement cash/PnL | **FAIL** | The DB commits post-state at `poly_us_trader.py:1792-1802`, but JSON is saved later at `:1856-1857`. After a crash in that window, startup reads stale JSON; a duplicate settlement only detaches the position and does not restore DB cash/PnL (`:1839-1845`). The correct DB snapshot at `local_db.py:921-935` is ignored. |
| `ledger_realized_gap` is a valid migration test and old/new paper agree for several days | **FAIL** | The check reads JSON state plus JSONL only (`poly_us_trader.py:881-928`), not SQLite. On first load it sets `pre_epoch_realized_pnl = ledger - state`, forcing the gap to zero (`:918-928`); the test explicitly expects that (`tests/test_poly_us_trader.py:1223-1256`). No side-by-side SQLite comparison or multi-day evidence was found. |
| Legacy migration is idempotent and complete | **FAIL** | Production keys are `poly_settle:slug:(order_id or opened_at or slug):qty` (`poly_us_trader.py:1775-1776`), and JSONL stores that exact `settle_key` (`:1818-1828`). The migration ignores it and appends an extra entry-price component (`scripts/migrate_poly_ledger_to_db.py:56-64`), so already-applied settlements can be inserted under new keys. `PolyLocalDB.import_legacy()` imports ledger JSON but never inserts `poly_settlements` (`local_db.py:2941-2961`). |
| Kalshi cash/positions/orders/settlements/PnL have consistent local authority | **PARTIAL** | Live venue reads and idempotent SQLite PnL are present, but paper uses JSON. `record_order_intent()` returns `False` on DB errors (`local_db.py:2101-2123`), while the Kalshi caller checks only exceptions and can still place the order (`trader.py:3091-3114`). `insert_order()` also returns `False` on error (`local_db.py:695-719`), and its caller ignores that result (`trader.py:547-567`). `orders.order_id` has no local uniqueness constraint and insertion is plain INSERT (`local_db.py:278-296,695-719`). |
| Legacy migration is complete and idempotent | **FAIL** | Production stores `settle_key` in JSONL (`poly_us_trader.py:1775-1776,1818-1828`), but the migrator ignores it and creates a different key with an extra entry-price component (`scripts/migrate_poly_ledger_to_db.py:56-64`), allowing duplicate PnL on import. `PolyLocalDB.import_legacy()` maps open/exit rows but never creates `poly_settlements` (`local_db.py:2941-2961`). |
| Durable outbox, idempotency, breaker and dead-row handling | **PASS (implementation only)** | n8n uses a unique `event_id`, atomic claims, retries/dead status and a delivery breaker (`local_db.py:349-371,1081-1270`; `n8n_client.py:75-147,196-251`). This is separate from the disabled Supabase sync breaker/quarantine (`local_db.py:1354-1426,1794-1845`). This PASS does not prove the unrun outage drill. |
| Startup reconciliation refuses trading until reconciled | **FAIL** | Kalshi position-seed failures can halt (`trader.py:3692-3697,3741-3746`), but other paths fail open. Poly ledger gaps only warn (`poly_us_trader.py:929-936`); pending-intent failures are swallowed (`:3575-3593`); venue errors return success (`:1366-1374`). Unrecoverable SQLite corruption is renamed and recreated empty, after which construction succeeds (`local_db.py:197-233`; `tests/test_d4_backup_restore.py:170-181`). |
| R9: no Supabase code/config on trading or reporting paths | **PARTIAL** | Remote sync is hard-disabled (`supabase_client.py:197-205`; `local_db.py:181,1794-1806`), so it is not current cash/PnL authority. However startup/PnL fallback code remains (`trader.py:109,3665-3668,3768-3774`; `pnl_reconcile.py:89-120`), as do sync settings (`config.py:467-503`). Normal n8n events still route to `superbot-trade` (`emit.py:8-11,23-27,47-54`), whose tracked workflow PATCHes Supabase (`n8n/workflows/superbot-trade.json:32-69`); the injector also accepts Supabase credentials (`inject_n8n_workflows.py:161-200,285-292,402-408`). Trading can continue without it, but R9 removal is incomplete. |
| Litestream or periodic online backups, including offsite retention | **PARTIAL** | No tracked Litestream reference or backup service/timer was found. WAL-safe local backup and restore code exists (`local_db.py:1429-1650`) and both traders call nightly maintenance (`trader.py:1977-1982`; `poly_us_trader.py:3233-3237`). Off-box OCI copy is explicitly unwired (`docs/vps-runbook.md:250-273`). The cron script reads `local_data/poly_us_state.json` (`deploy/state-backup.sh:5-11`), but the actual default is `local_data/poly_us/poly_us_state.json` (`poly_us_trader.py:65-72`); with `set -e`, that failure prevents its SQLite section (`deploy/state-backup.sh:40-67`). |
| Restore drill proves recovery without loss | **PARTIAL** | Temp-dir tests cover concurrent online backup and corruption restoration (`tests/test_d4_backup_restore.py:42-167`), and a runbook exists (`docs/vps-runbook.md:275-298`). There is no recorded clean-machine/offsite restore result. A normal restart does not restore a healthy DB; startup restores only after failed integrity (`local_db.py:1597-1615`). |
| 30-minute paper network-down drill: zero lost rows and no duplicates | **FAIL** | No tracked executable 30-minute drill or result was found. Unit tests exercise retry/idempotency, not a 30-minute outage plus recovery. Under R9 the check must cover canonical local rows and n8n outbox events, not Supabase. It was not run in this read-only investigation. |

## Smallest safe patch plan (not applied)

1. **Fail closed before changing authority.** Make Kalshi honor a false `record_order_intent()` and false filled-order insert. Add tests proving no venue call occurs without a durable intent. Make unrecoverable DB corruption and failed canonical reads/writes stop new entries; do not recreate an empty authoritative DB. Restore pending/venue reconciliation before Poly entries.
2. **Put one Poly canonical state behind a default-off flag.** Add a versioned canonical Poly state row in `PolyLocalDB` containing the existing state document (cash, positions, resting orders, epoch and PnL fields). Extend the existing transaction helper so each money transition commits that state plus order/fill/settlement/ledger rows together. Load that row at startup when enabled; write JSON/JSONL only after commit as exports. Keep the flag off during migration and never run two writers against one data directory.
3. **Fix migration and the comparison.** Prefer the stored JSONL `settle_key`; quarantine ambiguous legacy rows rather than minting a new key. Import `poly_settlements`. Add a real SQLite-vs-export comparison for cash, positions, resting orders, settlement counts and realized PnL; disable epoch re-baselining during that check. Run legacy and DB-authority paper instances in separate data directories against the same recorded production inputs, and require zero gap for several days before cutover.
4. **Remove the remaining Kalshi JSON authority next.** Persist full paper state, positions and settlement work in SQLite transactions, then export JSON snapshots. Deduplicate existing order rows before adding a partial unique index on nonempty `orders.order_id`; replay tests must prove one row and one PnL credit.
5. **Finish R9 without removing n8n durability.** Make `superbot-trade` ack-only or a local consumer, remove Supabase startup/PnL branches, sync implementation/settings, credential plumbing and packaging references. Keep `event_outbox`, its breaker, quarantine/dead handling and local reporting.
6. **Use the existing online-backup alternative rather than adding Litestream.** Fix Poly paths from the configured data root, add a backup service/timer and authenticated OCI upload outside the repo. For restore, stop writers, restore into an empty clean-machine paper directory, run `PRAGMA integrity_check`, compare table counts/content hashes and canonical revisions, then run one paper cycle. Record date, HEAD and results.
7. **Automate the 30-minute paper drill.** Keep producing from recorded real production inputs while n8n/offsite delivery is unavailable. After reconnect, require zero missing canonical revisions and no duplicate `pnl_ledger.idempotency_key`, `poly_settlements.id`, `orders.order_id` or `event_outbox.event_id`; also require `ledger_realized_gap == 0`. Store machine-readable and human evidence before marking T16-61 done.

## Commands and observed evidence

```bash
git rev-parse HEAD
# b20f6651729dedff79178dc8e79feae57727496c

git diff --name-status cf32aa5d817408f5f95213b9992f31147baf49dc..b20f6651729dedff79178dc8e79feae57727496c
# Only docs/T16_PALACE_AUDIT.md and tests/test_b15_instance_lock.py changed;
# no file used for authority findings changed.

git show HEAD:poly_us_trader.py | nl -ba | sed -n '881,1131p;1354,1422p;1645,1857p;2196,2264p;2516,2739p;2872,2887p'
git show HEAD:local_db.py | nl -ba | sed -n '162,247p;249,596p;695,960p;1354,1650p;2442,3068p'
git show HEAD:trader.py | nl -ba | sed -n '187,221p;508,575p;1173,1393p;1894,1914p;3091,3114p;3460,3810p'
git show HEAD:paper_mode.py | nl -ba
git show HEAD:scripts/migrate_poly_ledger_to_db.py | nl -ba
# Output supplied the file:line evidence above.

git grep -n 'apply_poly_cycle' HEAD -- '*.py'
# Production callers are individual mirror helpers; full-cycle atomic use is test-only.
git grep -n -i -E 'POLY.*(DB|SQLITE).*AUTHOR|DB.*AUTHORIT|SQLITE.*AUTHORIT' HEAD -- '*.py' '.env.example'
# No authority flag; only comments/log text.
git grep -i -l 'litestream' HEAD -- ':!AGENT_TASKSv15.md' ':!AGENT_TASKSv16.md' | wc -l
# 0
git ls-tree -r --name-only HEAD deploy scripts | grep -E '(backup|litestream).*\.(service|timer)$' | wc -l
# 0

git grep -n -i -E '30[ -]?(minute|min)|network outage|offline drill|partition drill|offsite restore|clean.machine|zero lost' HEAD -- ':!legacy/*' ':!docs/AUDIT*' ':!docs/ponytail*'
# No completed required drill; historical reconciliation also marks it unverified.

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q \
  tests/test_d2_poly_local_db.py tests/test_d1_poly_settlement.py \
  tests/test_d4_backup_restore.py tests/test_h9_order_intent.py tests/test_paper_mode.py
# 40 passed in 1.72s. These prove the existing helpers, not single authority or outage acceptance.

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/verify_source.py --all
# all clean

LC_ALL=C grep -nP '[^\x00-\x7F]' docs/T16_LOCAL_DB_AUDIT.md
# non_ascii_grep_exit=1 (no matches; report is ASCII)
```

Not checked: live deployment contents, active cron/systemd state, runtime schemas/data, real balances/positions/orders, secrets, remote backups, and actual outage/restore drills. Their absence here is not evidence of runtime absence; it is why those acceptance items cannot pass from this HEAD audit alone.
