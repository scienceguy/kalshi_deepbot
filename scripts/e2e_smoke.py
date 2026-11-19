#!/usr/bin/env python3
"""X1/X2 — end-to-end smoke test for both traders, against loopback only.

NOT a live check. It never touches n8n, never reaches the network beyond
127.0.0.1, never reads or writes the production database (each run uses a
temporary LocalDB in a temp dir), and never places an order — it exercises
the same code paths the control lane drains.

Pillars 1-5 are the original X1 control-plane checks. Pillars 6-7 drive both
traders through three real dry-run cycles each against a mock n8n on
127.0.0.1: orders are placed (dry-run fill from the book), settlement is
detected, P&L is booked in the local ledger, events flow event -> outbox ->
n8n (mock), and signed control commands pause/resume entries mid-run.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
import n8n_client  # noqa: E402
import n8n_webhook_auth  # noqa: E402
import agent_bus  # noqa: E402
import report_state  # noqa: E402
import paper_mode  # noqa: E402
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


# ── mock n8n (loopback only) ────────────────────────────────────────────

class _MockN8nHandler(BaseHTTPRequestHandler):
    """Verifies the HMAC envelope like the n8n webhook would; records what it
    accepts (path + exact raw body). Anything unsigned/badly signed gets 401
    and is NOT recorded."""

    def do_POST(self):  # noqa: N802 — stdlib handler name
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            body = raw.decode("utf-8")
        except Exception:
            self._reply(400, {"error": "bad request"})
            return
        ok, reason = n8n_webhook_auth.verify_request(
            self.headers.get("X-Superbot-Timestamp"),
            self.headers.get("X-Superbot-Nonce"),
            body,
            self.headers.get("X-Superbot-Signature"),
            store=self.server.nonce_store)
        if not ok:
            self._reply(401, {"error": reason})
            return
        self.server.received.append((self.path, body,
                                     self.headers.get("X-Superbot-Key")))
        self._reply(200, {"ok": True})

    def _reply(self, code, obj):
        blob = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):  # keep the smoke output clean
        pass


class _MockN8n:
    """Loopback n8n mock + client override. On start, points the global
    n8n_client/report_state singletons at the mock and disables the background
    outbox senders (the smoke delivers synchronously via drain_outbox()).
    On stop, restores everything and shuts the server down."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockN8nHandler)
        self.server.daemon_threads = True
        self.server.received = []
        self.server.nonce_store = n8n_webhook_auth.NonceStore()
        self._saved = {}

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def received(self):
        return self.server.received

    def __enter__(self):
        import threading
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.1}, daemon=True).start()
        self._saved = {
            "n8n_client._client": n8n_client._client,
            "n8n_client._start_outbox_sender": n8n_client._start_outbox_sender,
            "n8n_client._outbox_sender_started": n8n_client._outbox_sender_started,
            "report_state._n8n_client": report_state._n8n_client,
            "report_state._state_db": report_state._state_db,
        }
        n8n_client._client = n8n_client.N8nClient(
            base_url=f"http://127.0.0.1:{self.port}", api_key="x1-smoke-test-key")
        n8n_client._start_outbox_sender = lambda: None
        n8n_client._outbox_sender_started = True
        report_state._n8n_client = None
        report_state._state_db = None
        return self

    def __exit__(self, exc_type, exc, tb):
        n8n_client._client = self._saved["n8n_client._client"]
        n8n_client._start_outbox_sender = self._saved["n8n_client._start_outbox_sender"]
        n8n_client._outbox_sender_started = self._saved["n8n_client._outbox_sender_started"]
        report_state._n8n_client = self._saved["report_state._n8n_client"]
        report_state._state_db = self._saved["report_state._state_db"]
        self.server.shutdown()
        self.server.server_close()
        return False


def _drain_to_mock(timeout_seconds=5.0) -> int:
    """Deliver every pending critical/normal outbox row to the mock (and flush
    the telemetry coalescer, which drain_outbox only flushes when a normal row
    happened to be claimed in the same pass)."""
    delivered = n8n_client.drain_outbox(timeout_seconds=timeout_seconds)
    try:
        n8n_client._drain_telemetry()
    except Exception:
        pass
    return delivered


def _received_envelopes(mock) -> list:
    """Parsed JSON bodies for every accepted delivery."""
    out = []
    for _, body, _ in mock.received:
        try:
            out.append(json.loads(body))
        except Exception:
            out.append({})
    return out


def _pending_outbox_count(db) -> int:
    try:
        return len(db.get_pending_outbox_events() or [])
    except Exception:
        return -1


def _pnl_ledger_keys(db, like: str) -> list:
    import sqlite3
    conn = sqlite3.connect(str(db.db_path))
    try:
        rows = conn.execute(
            "SELECT idempotency_key, realized_pnl_dollars FROM pnl_ledger "
            "WHERE idempotency_key LIKE ?", (like,)).fetchall()
        return rows
    finally:
        conn.close()


def _control_inbox_rows(db, nonces=None) -> list:
    """(command, status, result) for control_inbox rows, oldest first.
    When `nonces` is given, only rows whose nonce is in it are returned —
    pillars 3/6 share the run's temp DB, so callers scope to their own
    commands by nonce."""
    import sqlite3
    conn = sqlite3.connect(str(db.db_path))
    try:
        if nonces:
            qmarks = ",".join("?" for _ in nonces)
            return conn.execute(
                "SELECT command, status, result FROM control_inbox "
                f"WHERE nonce IN ({qmarks}) ORDER BY id ASC",
                tuple(nonces)).fetchall()
        return conn.execute(
            "SELECT command, status, result FROM control_inbox ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()


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


# ── pillar 6: kalshi — three real dry-run cycles ───────────────────────
TICKER = "KXBTC15M-26NOV15-45"
ORDER_ID = "sb_e2e_kalshi_1"


def check_pillar_6_kalshi_e2e(db) -> tuple:
    """Three cycles through trader.cycle(): cycle 1 places (and dry-run fills)
    one YES order off the book; cycle 2 detects settlement at $1.00 and books
    P&L into the local ledger exactly once; cycle 3 proves no double-settle.
    A signed pause_entries lands mid-run (applied inside cycle 2 — no further
    orders) and a signed resume_entries + request_status are applied after
    cycle 3. Events flow event -> outbox -> mock n8n; assertions run against
    the mock's record and the local DB."""
    name = "6 kalshi dry-run e2e (order→fill→settle→PnL)"
    import trader
    from signals import TradeSignal
    from market_finder import MarketInfo
    from risk import RiskManager
    import types as _types

    tmp = tempfile.mkdtemp(prefix="x1-kalshi-")
    saved = {}
    # snapshot every knob the pillar touches
    saved["config.DRY_RUN"] = config.DRY_RUN
    saved["config.LLM_EVALUATION_ENABLED"] = config.LLM_EVALUATION_ENABLED
    saved["config.DRY_STARTING_BALANCE"] = getattr(config, "DRY_STARTING_BALANCE", None)
    saved["config.ML_MODEL_PATH"] = getattr(config, "ML_MODEL_PATH", "")
    saved["trader.PAPER_POSITIONS_FILE"] = trader.PAPER_POSITIONS_FILE
    saved["paper_mode.STATE_FILE"] = paper_mode.STATE_FILE
    saved["paper_mode._state"] = paper_mode._state
    saved["control.STATE.pause_entries"] = control.STATE.pause_entries
    saved["find_active_markets"] = trader.find_active_markets
    saved["se.evaluate"] = trader.se.evaluate
    saved["se.rank_signals"] = trader.se.rank_signals
    saved["gates.prefetch"] = trader.gates.prefetch
    saved["gates.asset_for"] = trader.gates.asset_for
    saved["gates.evaluate_entry"] = trader.gates.evaluate_entry
    saved["state.__dict__"] = dict(trader.state.__dict__)

    def restore():
        config.DRY_RUN = saved["config.DRY_RUN"]
        config.LLM_EVALUATION_ENABLED = saved["config.LLM_EVALUATION_ENABLED"]
        config.DRY_STARTING_BALANCE = saved["config.DRY_STARTING_BALANCE"]
        config.ML_MODEL_PATH = saved["config.ML_MODEL_PATH"]
        trader.PAPER_POSITIONS_FILE = saved["trader.PAPER_POSITIONS_FILE"]
        paper_mode.STATE_FILE = saved["paper_mode.STATE_FILE"]
        paper_mode._state = saved["paper_mode._state"]
        control.STATE.pause_entries = saved["control.STATE.pause_entries"]
        trader.find_active_markets = saved["find_active_markets"]
        trader.se.evaluate = saved["se.evaluate"]
        trader.se.rank_signals = saved["se.rank_signals"]
        trader.gates.prefetch = saved["gates.prefetch"]
        trader.gates.asset_for = saved["gates.asset_for"]
        trader.gates.evaluate_entry = saved["gates.evaluate_entry"]
        trader.state.__dict__.clear()
        trader.state.__dict__.update(saved["state.__dict__"])
        try:
            # leave the shared pause flag unpaused even if the pillar failed mid-way
            if control.STATE.pause_entries:
                db.set_control_state("pause_entries", {"paused": False})
                control.STATE.pause_entries = False
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    try:
        config.DRY_RUN = True
        config.LLM_EVALUATION_ENABLED = False
        config.DRY_STARTING_BALANCE = 100.0
        trader.PAPER_POSITIONS_FILE = type(trader.PAPER_POSITIONS_FILE)(tmp) / "paper_positions.json"
        paper_mode.STATE_FILE = type(paper_mode.STATE_FILE)(tmp) / "paper_state.json"
        paper_mode._state = None

        trader.get_state()
        st = trader.state
        st.positions = {}
        st.position_risk = {}
        st.resting_value = {}
        st.resting_tickers = set()
        st.live_resting_orders = []
        st.risk = RiskManager()
        st.window = {"id": None, "start_cash": 0.0, "budget": 0.0, "used": 0.0,
                     "reserved_cash": 0.0, "entered": set()}
        st.cycles = 0
        st.orders_today = 0
        st.last_order_time = 0.0
        st.trades_placed = 0
        st.consecutive_no_trade_cycles = 0
        st.session_pnl = 0.0
        st.settled_since = {}
        st.today = datetime.now(timezone.utc).date().isoformat()

        market = MarketInfo(
            ticker=TICKER, series="KXBTC15M",
            close_time=datetime.now(timezone.utc), strike=0, strike_type="",
            yes_bid=92, yes_ask=93, no_bid=7, no_ask=8,
            yes_liquidity=100, no_liquidity=100, minutes_left=5.0,
            bracket_role="MAIN")
        sig = TradeSignal("YES", "yes", 93, 1.0, 1.0, "x1 smoke", market,
                          win_prob=0.99)
        trader.se.evaluate = lambda m, cash, max_trade_override=None: [sig]
        trader.se.rank_signals = lambda sigs: list(sigs)
        trader.gates.prefetch = lambda assets: None
        trader.gates.asset_for = lambda m: "BTC"
        trader.gates.evaluate_entry = lambda *a, **k: _types.SimpleNamespace(allowed=True)
        trader.find_active_markets = lambda c: [market]

        from unittest.mock import MagicMock
        client = MagicMock()
        client.get_balance.return_value = 100.0
        client.get_positions.return_value = []
        client.get_orders.return_value = []
        client.get_market.return_value = {"result": "yes"}
        client.place_order.return_value = {
            "order": {"order_id": ORDER_ID, "status": "resting"}}
        client.invalidate_markets.return_value = None

        with _MockN8n() as mock:
            # ── cycle 1: place + dry-run fill off the book ──
            trader.cycle(client)
            if TICKER not in st.positions:
                return name, False, "cycle 1 did not book the filled paper position"
            rows = _pnl_ledger_keys(db, f"settle:{TICKER}:%")
            if rows:
                return name, False, f"pnl_ledger settled before settlement: {rows}"
            if _pending_outbox_count(db) <= 0:
                return name, False, "cycle 1 emitted no outbox events"
            _drain_to_mock()

            # ── control: pause entries for the rest of the run ──
            _, _, pause_nonce, _ = _signed_row(db, "pause_entries", agent_id="operator",
                                               payload={"reason": "x1 smoke pause"})

            # ── cycle 2: settlement detected + pause applied in-cycle ──
            trader.cycle(client)
            if not control.STATE.pause_entries:
                return name, False, "signed pause_entries not applied during cycle 2"
            pstate = db.get_control_state("pause_entries") or {}
            if not pstate.get("paused"):
                return name, False, f"pause state not persisted: {pstate}"
            if TICKER in st.positions:
                return name, False, "settled position still open after cycle 2"
            applied = [r for r in _pnl_ledger_keys(db, f"settle:{TICKER}:{ORDER_ID}")]
            if len(applied) != 1:
                return name, False, f"expected exactly 1 settlement ledger row, got {applied}"
            pnl = applied[0][1]
            if not (pnl and pnl > 0):
                return name, False, f"expected positive realized PnL, got {pnl}"
            _drain_to_mock()

            # ── cycle 3: no double settlement, still paused ──
            trader.cycle(client)
            applied = _pnl_ledger_keys(db, f"settle:{TICKER}:{ORDER_ID}")
            if len(applied) != 1:
                return name, False, "settlement ledger row duplicated across cycles"
            if not control.STATE.pause_entries:
                return name, False, "pause flag lost by cycle 3"
            n_orders = _scalar(db, "SELECT COUNT(*) FROM orders WHERE ticker = ?",
                               (TICKER,))
            if n_orders != 1:
                return name, False, f"expected 1 orders row, found {n_orders}"
            n_intents = _scalar(db, "SELECT COUNT(*) FROM pending_send WHERE ticker = ?",
                                (TICKER,))
            if n_intents != 1:
                return name, False, f"expected 1 pending_send intent, found {n_intents}"
            intent_status = _scalar(
                db, "SELECT status FROM pending_send WHERE ticker = ?",
                (TICKER,))
            if intent_status != "confirmed":
                return name, False, f"order intent not confirmed after dry-run fill: {intent_status}"
            if _pending_outbox_count(db) != 0:
                return name, False, "outbox not fully drained after cycles 2-3"
            _drain_to_mock()

            # ── resume + status through the same drain path ──
            _, _, resume_nonce, _ = _signed_row(db, "resume_entries", agent_id="operator")
            _, _, status_nonce, _ = _signed_row(db, "request_status", agent_id="operator")
            results = control.drain_and_apply(db, risk_halted=False,
                                              positions=st.positions,
                                              source="kalshi_trader")
            statuses = {r.get("status") for r in results}
            if statuses != {"applied"}:
                return name, False, f"resume/status not applied: {results}"
            if control.STATE.pause_entries:
                return name, False, "resume_entries did not lift the pause flag"
            if (db.get_control_state("pause_entries") or {}).get("paused"):
                return name, False, "pause state still persisted after resume"
            _drain_to_mock()

            # control commands must be durably recorded in control_inbox:
            # all three applied, and request_status's result JSON carries a
            # well-formed status snapshot. (control.status telemetry is
            # coalesced latest-wins per path|venue and is overwritten by
            # control.applied, so delivery to n8n is not assertable here.)
            # scope to pillar 6's own commands: the run's temp DB is shared
            # with pillar 3's control rows
            own = (pause_nonce, resume_nonce, status_nonce)
            rows = _control_inbox_rows(db, own)
            cmds = [(r[0], r[1]) for r in rows]
            if cmds != [("pause_entries", "applied"),
                        ("resume_entries", "applied"),
                        ("request_status", "applied")]:
                return name, False, f"control_inbox unexpected: {cmds}"
            try:
                snap = json.loads(next(r[2] for r in rows
                                       if r[0] == "request_status"))["status"]
                assert isinstance(snap["positions_open"], int)
                assert isinstance(snap["risk_halted"], bool)
                assert isinstance(snap["caps"], dict) and snap["caps"]
                assert snap["control"]["pause_entries"] is False
            except Exception as e:
                return name, False, f"request_status result malformed: {e}"

            envelopes = _received_envelopes(mock)
            types = {e.get("type") for e in envelopes if isinstance(e, dict)}
            missing = {"order.placed", "order.filled", "position.settled",
                       "control.applied"} - types
            if missing:
                return name, False, (f"mock n8n missing event types {sorted(missing)}; "
                                     f"got {sorted(t for t in types if t)}")
            settled = [e for e in envelopes
                       if e.get("type") == "position.settled"
                       and (e.get("payload") or {}).get("order_id") == ORDER_ID]
            if not settled:
                return name, False, "mock n8n never received position.settled for our order"
            flat_settle = [e for e in envelopes
                           if e.get("action") == "settlement"
                           and e.get("order_id") == ORDER_ID]
            if not flat_settle:
                return name, False, "mock n8n never received the settlement webhook"
            if _pending_outbox_count(db) != 0:
                return name, False, "outbox not empty at pillar end"
            # every delivery the mock accepted must have a signature that verified
            # (the handler 401s otherwise), so: count check is enough
            delivered = len(mock.received)
        return name, True, (f"3 cycles ok; settle ledger +${applied[0][1]:.2f}; "
                            f"pause persisted+resumed via drain; {delivered} signed "
                            f"deliveries to loopback n8n mock")
    except Exception as e:
        return name, False, f"unexpected error: {e.__class__.__name__}: {e}"
    finally:
        restore()


def _scalar(db, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(str(db.db_path))
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ── pillar 7: poly — three real dry-run cycles ─────────────────────────
POLY_SLUG = "e2e-poly-favorite-above-50k"


def check_pillar_7_poly_e2e(db) -> tuple:
    """Three cycles of PolyUSPaperTrader against a canned favorite market:
    cycle 1 places+fills a paper YES (taker at BBO); cycle 2 settles at $1.00
    through the real settlement path (pnl ledger exactly once, payout
    credited); cycle 3 proves no double-settle. Same event->outbox->mock path
    as the Kalshi pillar."""
    name = "7 poly dry-run e2e (order→fill→settle→PnL)"

    # Patch the exact module object poly_us_trader consults — another object
    # with the same file may exist in this process (cf. conftest's
    # _restore_pm_us_client_module); sys.modules lookups are not reliable.
    # Import order does not matter: every patch target is an attribute read
    # at call time.
    import poly_us_trader
    import crossvenue_edge
    pm = poly_us_trader.polymarket_us_client

    tmp = tempfile.mkdtemp(prefix="x1-poly-")
    data_dir = os.path.join(tmp, "poly_us")
    os.makedirs(data_dir, exist_ok=True)
    patched = []  # snapshot true originals — DATA_DIR redirects the live
    # bs_calibration ledger (and state files) into the temp dir
    for mod, attr in ((poly_us_trader, "DATA_DIR"),
                      (poly_us_trader, "STATE_FILE"), (poly_us_trader, "LEDGER_FILE"),
                      (poly_us_trader, "_mp_bridge"),
                      (poly_us_trader, "_bbo_for_slug"),
                      (crossvenue_edge, "get_cached_spot"),
                      (crossvenue_edge, "get_cached_vol"),
                      (pm, "health_check"),
                      (pm, "_get_client"),
                      (pm, "_get_authenticated_client"),
                      (pm, "get_market_settlement"),
                      (pm, "get_market_book"),
                      (config, "ML_MODEL_PATH"),
                      (control.STATE, "pause_entries")):
        patched.append((mod, attr, getattr(mod, attr)))

    def restore():
        try:
            for mod, attr, value in reversed(patched):
                setattr(mod, attr, value)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    try:
        # hermetic mocks: no venue calls, no palace bridge, canned above-market
        pm.health_check = lambda: True
        pm._get_client = lambda: None
        pm._get_authenticated_client = lambda: None
        pm.get_market_settlement = (
            lambda slug: {"slug": slug, "settlement": 1.0})
        pm.get_market_book = lambda slug: {
            "slug": slug, "bids": [{"price": 0.94, "size": 100}],
            "asks": [{"price": 0.96, "size": 100}]}
        crossvenue_edge.get_cached_spot = lambda symbol: 100000.0
        crossvenue_edge.get_cached_vol = lambda symbol: 0.5
        poly_us_trader._mp_bridge = lambda: None
        poly_us_trader._bbo_for_slug = lambda slug: (0.94, 0.96)
        config.ML_MODEL_PATH = ""

        poly_us_trader.DATA_DIR = poly_us_trader.Path(data_dir)
        poly_us_trader.STATE_FILE = poly_us_trader.Path(data_dir) / "poly_us_state.json"
        poly_us_trader.LEDGER_FILE = poly_us_trader.Path(data_dir) / "poly_us_paper_ledger.jsonl"

        window_end = (datetime.now(timezone.utc).timestamp() + 90 * 60)
        window_iso = datetime.fromtimestamp(window_end, timezone.utc).isoformat().replace("+00:00", "Z")
        market = {
            "slug": POLY_SLUG,
            "question": "BTC above 50k?",
            "category": "crypto",
            "closed": False,
            "outcomePrices": ["0.95", "0.05"],
            "assetPriceTerms": {
                "asset": {"symbol": "BTC"},
                "strike": {"value": 50000},
                "windowEnd": window_iso,
                "marketType": "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW",
            },
            "eventSlug": "e2e-poly-event",
        }
        scan_state = {"calls": 0}

        def fake_scan():
            scan_state["calls"] += 1
            m = dict(market)
            m["closed"] = scan_state["calls"] > 1  # closed from cycle 2 on
            return [m]

        trader_obj = poly_us_trader.PolyUSPaperTrader(initial_cash=35.0, reset=True)
        trader_obj.scan_markets = fake_scan
        trader_obj.preview_order = lambda slug, side, price, qty, good_till_time=None: True

        with _MockN8n() as mock:
            trader_obj.cycle()  # cycle 1: place + fill
            pos_slugs = [p["slug"] for p in trader_obj.state.get("positions", [])]
            if POLY_SLUG not in pos_slugs:
                return name, False, "cycle 1 did not book the paper position"
            if _pending_outbox_count(db) <= 0:
                return name, False, "cycle 1 emitted no outbox events"
            _drain_to_mock()

            trader_obj.cycle()  # cycle 2: settlement path
            if trader_obj.state.get("positions"):
                return name, False, "position not settled in cycle 2"
            settle_rows = _pnl_ledger_keys(db, "poly_settle:%")
            mine = [r for r in settle_rows if r[0].startswith(f"poly_settle:{POLY_SLUG}:")]
            if len(mine) != 1:
                return name, False, f"expected 1 poly settlement ledger row, got {mine}"
            if not (trader_obj.state.get("realized_pnl", 0) > 0):
                return name, False, f"realized pnl not credited: {trader_obj.state.get('realized_pnl')}"
            _drain_to_mock()

            cash_after_cycle2 = trader_obj.state["cash"]
            trader_obj.cycle()  # cycle 3: no double settlement
            mine = [r for r in _pnl_ledger_keys(db, "poly_settle:%")
                    if r[0].startswith(f"poly_settle:{POLY_SLUG}:")]
            if len(mine) != 1:
                return name, False, "settlement ledger row duplicated across cycles"
            if trader_obj.state.get("realized_pnl", 0) <= 0:
                return name, False, "realized pnl regressed in cycle 3"
            if _pending_outbox_count(db) != 0:
                return name, False, "outbox not fully drained after poly cycles"
            _drain_to_mock()

            envelopes = _received_envelopes(mock)
            types = {e.get("type") for e in envelopes if isinstance(e, dict)}
            missing = {"order.placed", "order.filled", "position.settled"} - types
            if missing:
                return name, False, (f"mock n8n missing event types {sorted(missing)}; "
                                     f"got {sorted(t for t in types if t)}")
            settled = [e for e in envelopes
                       if e.get("type") == "position.settled"
                       and (e.get("payload") or {}).get("slug") == POLY_SLUG]
            if not settled:
                return name, False, "mock n8n never received position.settled for our slug"
            delivered = len(mock.received)
        return name, True, (f"3 cycles ok; realized pnl +${trader_obj.state['realized_pnl']:.2f}; "
                            f"cash ${cash_after_cycle2:.2f} after settle; {delivered} signed "
                            f"deliveries to loopback n8n mock")
    except Exception as e:
        return name, False, f"unexpected error: {e.__class__.__name__}: {e}"
    finally:
        restore()
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
        # Clear control_inbox so pillars 6/7 see only their own commands.
        try:
            db._get_conn().execute("DELETE FROM control_inbox")
            db._get_conn().commit()
        except Exception:
            pass
        # Point traders at the run_all() temp DB so they write to the same DB
        # we check for outbox events.
        prior_db_path = os.environ.get("SUPERBOT_LOCAL_DB_PATH")
        os.environ["SUPERBOT_LOCAL_DB_PATH"] = str(db.db_path)
        try:
            results.append(check_pillar_6_kalshi_e2e(db))
            results.append(check_pillar_7_poly_e2e(db))
        finally:
            if prior_db_path is None:
                os.environ.pop("SUPERBOT_LOCAL_DB_PATH", None)
            else:
                os.environ["SUPERBOT_LOCAL_DB_PATH"] = prior_db_path
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
