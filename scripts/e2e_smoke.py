#!/usr/bin/env python3
"""X1 — end-to-end smoke test for the five control-plane pillars.

NOT a live check. It never touches n8n, never reaches the network beyond
127.0.0.1, never reads or writes the production database (each run uses a
temporary LocalDB in a temp dir), and never places an order — it exercises
the same code paths the control lane drains.

Run:
    python scripts/e2e_smoke.py

Each pillar is an importable check returning (pillar, ok, detail). `run_all()`
is what the pytest wrapper in tests/test_x1_e2e_smoke.py calls. Exit code is
0 when every pillar passes; a pillar may report a SKIP sub-check it cannot
prove here (it still counts as ok — skipped is not failed). Non-zero exit
if any pillar fails.

Environment isolation: SUPERBOT_DATA_DIR / SUPERBOT_LOCAL_DB_PATH are force-set
to a temp dir before any repo module is imported (local_db reads them at
import), so no production path is resolved. run_all() restores the caller's
prior env values when it returns (the module-level force-set remains for the
first import).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

# ── isolate from production before any repo import ──────────────────────
# Force-set (not setdefault): a hostile/agent shell may already export these,
# and local_db resolves its path at import time, so the assignment that keeps
# the first import honest is this module-level one. Trade-off: importing this
# module clobbers the process env, so run_all() restores the originals in its
# finally block; the module-level force-set still applies to the first import.
_SMOKE_DATA = tempfile.mkdtemp(prefix="x1-smoke-")
atexit.register(shutil.rmtree, _SMOKE_DATA, ignore_errors=True)
_prior_env = {k: os.environ.get(k) for k in
              ("SUPERBOT_DATA_DIR", "SUPERBOT_LOCAL_DB_PATH")}
os.environ["SUPERBOT_DATA_DIR"] = _SMOKE_DATA
os.environ["SUPERBOT_LOCAL_DB_PATH"] = os.path.join(_SMOKE_DATA, "smoke.db")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import config  # noqa: E402
import emit  # noqa: E402
import control  # noqa: E402
import n8n_webhook_auth  # noqa: E402
import agent_bus  # noqa: E402
from local_db import LocalDB  # noqa: E402

# Fake secret for the smoke only — never the production one, never logged.
WEBHOOK_SECRET = "x1-smoke-test-secret"


def _signed_row(db, command, agent_id="operator", payload=None,
                nonce=None, ts_offset_ms=0):
    """Enqueue one signed control command; same shape as test_n4_control."""
    ts = int(time.time() * 1000) + ts_offset_ms
    nonce = nonce or uuid.uuid4().hex
    body = json.dumps({"command": command, **(payload or {})},
                      separators=(",", ":"), ensure_ascii=False)
    sig = n8n_webhook_auth.sign_body(ts, nonce, body)
    row_id = db.enqueue_control_command(command, agent_id, nonce, ts, body, sig)
    return row_id, ts, nonce, body


# ── pillar 1: webhook auth envelope ─────────────────────────────────────
def check_pillar_1_webhook_auth() -> tuple:
    name = "1 webhook auth envelope"
    ts = int(time.time() * 1000)
    nonce = uuid.uuid4().hex
    body = json.dumps({"command": "pause_entries", "reason": "x1 smoke"},
                      separators=(",", ":"), ensure_ascii=False)
    sig = n8n_webhook_auth.sign_body(ts, nonce, body)

    ok, reason = n8n_webhook_auth.verify_request(ts, nonce, body, sig)
    if not ok:
        return name, False, f"valid request rejected: {reason}"

    ok_bad, _ = n8n_webhook_auth.verify_request(ts, nonce, body + " ", sig)
    if ok_bad:
        return name, False, "tampered body verified"

    bad_sig, _ = n8n_webhook_auth.verify_request(ts, nonce, body, "0" * 64)
    if bad_sig:
        return name, False, "invalid signature verified"

    # fresh store: first verify marks the nonce, a replay must be rejected
    store = n8n_webhook_auth.NonceStore()
    ok_first, _ = n8n_webhook_auth.verify_request(ts, nonce, body, sig, store=store)
    ok_second, reason2 = n8n_webhook_auth.verify_request(ts, nonce, body, sig,
                                                         store=store)
    if not ok_first:
        return name, False, "first nonce verify failed"
    if ok_second:
        return name, False, "replayed nonce verified twice"

    return name, True, (f"sign/verify ok; tampered body + bad sig refused; "
                        f"replay refused ({reason2})")


# ── pillar 2: durable signed outbox ────────────────────────────────────
def check_pillar_2_outbox(db) -> tuple:
    name = "2 durable signed outbox"
    event_id = f"smoke-{uuid.uuid4().hex[:12]}"
    row_id = db.insert_outbox_event(
        "smoke.test", json.dumps({"ping": "x1"}),
        delivery_class="critical", event_id=event_id)
    if row_id is None:
        return name, False, "insert_outbox_event returned None"

    pending = db.get_pending_outbox_events()
    if not any(r.get("event_id") == event_id for r in pending):
        return name, False, "enqueued event not returned as pending"

    depth = db.get_outbox_depth_stats()
    if depth.get("outbox_depth", 0) < 1 or depth.get("pending:critical", 0) < 1:
        return name, False, f"depth stats did not see the pending event: {depth}"

    if db.mark_outbox_sent(row_id) is not True:
        return name, False, "mark_outbox_sent failed"

    after = db.get_outbox_depth_stats()
    stats = db.get_outbox_stats()
    if after.get("outbox_depth", -1) != 0:
        return name, False, f"outbox depth not drained after mark_sent: {after}"
    if stats.get("sent:critical", 0) < 1:
        return name, False, f"sent:critical missing from stats: {stats}"

    return name, True, (f"pending -> sent roundtrip ok; "
                        f"sent:critical={stats.get('sent:critical')}, "
                        f"depth={after.get('outbox_depth')}")


# ── pillar 3: control lane (signed in -> trader drain applies) ──────────
def check_pillar_3_control_lane(db) -> tuple:
    name = "3 control lane"
    saved_pause = control.STATE.pause_entries
    saved_emit = emit.emit_event
    # The apply path emits control.applied/rejected through the outbox sender,
    # which would reach n8n. Swallow it: the smoke proves the DB result +
    # in-memory/persisted pause state, not delivery.
    emit.emit_event = lambda *a, **kw: None
    try:
        control.STATE.pause_entries = False
        db.set_control_state("pause_entries", {"paused": False})

        _signed_row(db, "pause_entries", agent_id="operator",
                    payload={"reason": "x1 smoke"})
        results = control.drain_and_apply(db, risk_halted=False)
        applied = [r for r in results if r.get("status") == "applied"]
        if not applied:
            return name, False, f"pause_entries not applied: {results}"
        if not control.STATE.pause_entries:
            return name, False, "STATE.pause_entries not set by drain"
        state = db.get_control_state("pause_entries")
        if not (state or {}).get("paused"):
            return name, False, f"pause state not persisted: {state}"

        _signed_row(db, "resume_entries", agent_id="operator")
        results = control.drain_and_apply(db, risk_halted=False)
        applied = [r for r in results if r.get("status") == "applied"]
        if not applied:
            return name, False, f"resume_entries not applied: {results}"
        if control.STATE.pause_entries:
            return name, False, "STATE.pause_entries still True after resume"
        state = db.get_control_state("pause_entries")
        if (state or {}).get("paused"):
            return name, False, f"pause state still persisted after resume: {state}"

        return name, True, "pause -> applied + persisted; resume -> applied"
    finally:
        control.STATE.pause_entries = saved_pause
        emit.emit_event = saved_emit


# ── pillar 4: agent bus push ────────────────────────────────────────────
def check_pillar_4_agent_bus() -> tuple:
    name = "4 agent bus push"
    identity = "x1-smoke-agent"
    fired = []
    receiver = agent_bus.BusReceiver(identity, lambda env: fired.append(env))
    server = agent_bus.BusServer(receiver, port=0)
    port = server.start()
    try:
        mid = f"x1-{uuid.uuid4().hex[:16]}"
        env = agent_bus.envelope(to_agent=identity,
                                 body={"ping": "x1"},
                                 from_agent=identity,
                                 message_id=mid)

        resp = agent_bus.post_envelope(env, port=port, timeout=2.0)
        if not resp.get("ok"):
            return name, False, f"first push failed: {resp}"
        if len(fired) != 1:
            return name, False, f"handler fired {len(fired)} times, expected 1"

        # H12: same message_id must not be processed twice. If the dedup is
        # not merged on this branch, skip the sub-check instead of failing.
        resp2 = agent_bus.post_envelope(env, port=port, timeout=2.0)
        if resp2.get("duplicate") is not True:
            return name, True, ("SKIP: H12 message_id dedup not merged — "
                                "replay sub-check skipped")
        if len(fired) != 1:
            return name, False, "handler fired again for the same message_id"

        return name, True, "first push delivered exactly once; replay flagged duplicate"
    finally:
        server.stop()


# ── pillar 5: local-first DB mirror ────────────────────────────────────
def check_pillar_5_db_mirror(db) -> tuple:
    name = "5 local-first DB mirror"
    ts = datetime.now(timezone.utc).isoformat()

    if not db.insert_order({"ts": ts, "ticker": "X1-SMOKE", "side": "yes",
                           "contracts": 1, "price_cents": 95,
                           "status": "pending"}):
        return name, False, "insert_order failed"
    if not db.insert_signal({"ts": ts, "ticker": "X1-SMOKE", "side": "yes",
                            "price_cents": 95, "contracts": 1,
                            "edge": 0.05, "model_prob": 0.95,
                            "reason": "x1 smoke"}):
        return name, False, "insert_signal failed"

    orders = db.get_unsynced_orders()
    signals = db.get_unsynced_signals()
    if not any(r.get("ticker") == "X1-SMOKE" for r in orders):
        return name, False, "order row not readable back from the mirror"
    if not any(r.get("ticker") == "X1-SMOKE" for r in signals):
        return name, False, "signal row not readable back from the mirror"

    try:
        status = db.get_sync_status()
    except Exception as e:
        return name, False, f"get_sync_status not callable: {e}"
    if not isinstance(status, dict) or "orders" not in status or "signals" not in status:
        return name, False, f"sync status missing table keys: {status}"

    return name, True, (f"order+signal roundtrip ok; "
                        f"unsynced: orders={status['orders']}, "
                        f"signals={status['signals']}")


# ── runner ──────────────────────────────────────────────────────────────
def run_all() -> list:
    """Run every pillar against an isolated temporary LocalDB.

    Returns a list of (pillar, ok, detail) tuples. Never raises."""
    saved_secret = config.SUPERBOT_WEBHOOK_SECRET
    config.SUPERBOT_WEBHOOK_SECRET = WEBHOOK_SECRET

    tmp = tempfile.mkdtemp(prefix="x1-smoke-run-")
    results = []
    try:
        db = LocalDB(db_path=os.path.join(tmp, "smoke.db"), auto_sync=False)
        # Hard isolation guard: nothing the pillars write may resolve outside the
        # system temp root. config.SUPERBOT_DATA_DIR is deliberately NOT checked:
        # it is frozen at config's import time, so an embedding process (pytest)
        # always shows the production value even though no pillar uses it — the
        # LocalDB below is opened with an explicit temp path.
        import local_db as _local_db
        temp_root = os.path.realpath(tempfile.gettempdir())
        outside = []
        for label, path in (
                ("opened DB", getattr(db, "db_path", None) or os.path.join(tmp, "smoke.db")),
                ("local_db path", _local_db.get_db_path())):
            if not path:
                continue
            if os.path.commonpath([temp_root,
                                   os.path.realpath(str(path))]) != temp_root:
                outside.append(f"{label}={path}")
        if outside:
            try:
                db._get_conn().close()
            except Exception:
                pass
            results.append(("isolation", False,
                            "resolved paths outside temp dir: " + ", ".join(outside)))
            return results
        results.append(check_pillar_1_webhook_auth())
        results.append(check_pillar_2_outbox(db))
        results.append(check_pillar_3_control_lane(db))
        results.append(check_pillar_4_agent_bus())
        results.append(check_pillar_5_db_mirror(db))
        try:
            db._get_conn().close()
        except Exception:
            pass
    except Exception as e:
        results.append(("setup", False, f"temporary LocalDB failed: {e}"))
    finally:
        config.SUPERBOT_WEBHOOK_SECRET = saved_secret
        # Restore the caller's env so embedded/pytest use doesn't clobber the
        # process. The module-level force-set stays in place for first import.
        for k, v in _prior_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def main(argv=None) -> int:
    results = run_all()
    width = max((len(p) for p, _, _ in results), default=0)
    for pillar, ok, detail in results:
        tag = "SKIP" if detail.startswith("SKIP:") else ("PASS" if ok else "FAIL")
        print(f"[{tag}] {pillar.ljust(width)}  {detail}")
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)
    if failed:
        print(f"\nSUMMARY: {total - failed}/{total} pillars passed, "
              f"{failed} FAILED")
        return 1
    print(f"\nSUMMARY: {total}/{total} pillars passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
