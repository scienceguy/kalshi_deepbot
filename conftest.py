"""Pytest configuration — isolate config mutation between tests.

Many tests mutate ``config`` module globals (e.g.
``config.EXPOSURE_CEILING_PCT = 0.30``) without restoring them, which makes
test outcomes order-dependent. This autouse fixture snapshots all
module-level uppercase config attributes before each test and restores them
afterwards, so no test can leak config state into another.

Also isolates ``paper_mode`` module state so the paper ledger (which
persists to paper_state.json and local_db) never leaks between tests.

V3 hermetic hardening:
- Pin DRY_RUN=true, POLY_CONFIRM_LIVE=false, KALSHI_CONFIRM_LIVE=false,
  AGGRESSIVE_FILL=false BEFORE config imports. config._load_env() only sets
  vars not already in os.environ, so pre-setting them overrides .env values
  (e.g. DRY_RUN=false, AGGRESSIVE_FILL=true from .env) that would otherwise
  leak into every test.
- Clear credential env vars so no test can accidentally sign live requests.
- Block outbound sockets (non-localhost) so no test can reach the network.
"""
import os
import socket as _socket
import sys
import tempfile

# ── Pin safety-critical env vars BEFORE config imports ─────────────────────
# config._load_env() reads .env and sets os.environ[k] = v only when k is not
# already present, so we set these first to override .env for the whole test
# session. DRY_RUN=true keeps _sizing_cash off the live balance; AGGRESSIVE_FILL
#=false keeps _size() from folding in taker fees; confirm=false blocks --live.
os.environ["DRY_RUN"] = "true"
os.environ["POLY_CONFIRM_LIVE"] = "false"
os.environ["KALSHI_CONFIRM_LIVE"] = "false"
os.environ["AGGRESSIVE_FILL"] = "false"
os.environ["ALLOW_NO_BUYS"] = "false"
# Clear credential env vars so no test can accidentally sign live requests.
for _k in ("KALSHI_KEY_ID", "KALSHI_KEY_FILE", "KALSHI_PRIVATE_KEY",
           "KALSHIEYE_API_KEY_FILE", "N8N_API_KEY_FILE",
           "SUPABASE_KEY_FILE", "SUPABASE_JWKS_URL",
           "SUPERBOT_WEBHOOK_SECRET", "SUPERBOT_WEBHOOK_SECRET_FILE"):
    os.environ.pop(_k, None)

# Redirect every runtime file the bot writes into a throwaway dir BEFORE the
# modules below read these env vars at import time. Previously the suite wrote
# synthetic KXBTC15M-TEST rows into the real markout_log.jsonl (330 rows,
# -$280.50) and could touch paper_state.json and local_data/.
_TMP = tempfile.mkdtemp(prefix="deepbot-tests-")
os.environ["MARKOUT_LOG_FILE"] = os.path.join(_TMP, "markout_log.jsonl")
os.environ["GATES_LOG_FILE"] = os.path.join(_TMP, "gates_log.jsonl")
os.environ["PAPER_STATE_FILE"] = os.path.join(_TMP, "paper_state.json")
os.environ["PAPER_POSITIONS_FILE"] = os.path.join(_TMP, "paper_positions.json")
os.environ["SUPERBOT_LOCAL_DB_DIR"] = os.path.join(_TMP, "local_data")
os.environ["SUPERBOT_WEBHOOK_SECRET"] = ""
os.environ["SUPERBOT_WEBHOOK_SECRET_FILE"] = os.path.join(_TMP, "no_such_secret")
# n8n base URL -> closed port: the outbox sender thread must never touch the
# real n8n on 127.0.0.1:5678 (V3 hermeticity: no live service dependence).
os.environ["N8N_BASE_URL"] = "http://127.0.0.1:9"

import config  # noqa: E402
import local_db  # noqa: E402
import paper_mode  # noqa: E402
import pytest  # noqa: E402

# ── Block outbound sockets (non-localhost) ─────────────────────────────────
_real_create_connection = _socket.create_connection

_LOCALHOSTS = {"127.0.0.1", "::1", "localhost"}


def _blocked_create_connection(address, *args, **kwargs):
    """Allow localhost test servers, block everything else."""
    host = address[0] if isinstance(address, tuple) else str(address)
    if str(host) in _LOCALHOSTS:
        return _real_create_connection(address, *args, **kwargs)
    raise RuntimeError(f"Outbound network access blocked in tests: {address}")


@pytest.fixture(autouse=True)
def _restore_pm_us_client_module():
    """Keep sys.modules["polymarket_us_client"] bound to one stable module object.

    Several tests inject fakes into sys.modules["polymarket_us_client"] or pop
    it entirely (test_crossvenue_pm, test_polymarket_portfolio, test_fund_tracker).
    A pop makes the next `import polymarket_us_client` create a NEW module object,
    while modules imported earlier (poly_us_trader) keep calling the OLD one —
    so `patch("polymarket_us_client.place_maker_order")` patches the new object
    and never intercepts, leaking real network calls into tests. Snapshotting
    the module at test start and restoring it at test end keeps every reference
    in the process consistent.
    """
    before = sys.modules.get("polymarket_us_client")
    yield
    if sys.modules.get("polymarket_us_client") is not before:
        if before is None:
            sys.modules.pop("polymarket_us_client", None)
        else:
            sys.modules["polymarket_us_client"] = before


@pytest.fixture(autouse=True)
def _block_outbound_sockets():
    """Block outbound network access for every test, allow localhost only.

    socket.create_connection is what requests/urllib3/http.client use to open
    outbound TCP connections. Blocking it (except for localhost test servers)
    guarantees no test can reach Kalshi, Polymarket, Supabase, n8n, or any
    external service. Direct socket.socket.connect usage is rare in this
    codebase and is covered by create_connection for the HTTP client path.
    """
    _socket.create_connection = _blocked_create_connection
    yield
    _socket.create_connection = _real_create_connection


@pytest.fixture
def no_sleep(monkeypatch):
    """Monkeypatch time.sleep to a no-op for backoff/rate-limit tests.

    Use in tests that exercise code paths calling time.sleep (e.g. rate_limit,
    n8n_client retries, kalshi_client backoff) so tests run fast and
    deterministically without real delays. Do NOT use in tests that assert on
    real elapsed time (e.g. test_rate_limit.py).
    """
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _isolate_config():
    """Snapshot and restore config module globals around each test.

    Also saves and restores paper_mode._state so paper ledger state never
    leaks between tests, and paper_mode/local_db path attributes so that
    importlib.reload() in V1 data-dir tests doesn't redirect runtime files
    out of the throwaway dir for subsequent tests.
    """
    # Snapshot all public (uppercase) config attributes
    snapshot = {
        name: getattr(config, name)
        for name in dir(config)
        if name.isupper() and not name.startswith("_")
    }
    # Snapshot paper_mode uppercase attributes (e.g. STATE_FILE)
    paper_snapshot = {
        name: getattr(paper_mode, name)
        for name in dir(paper_mode)
        if name.isupper() and not name.startswith("_")
    }
    # Snapshot local_db uppercase attributes (e.g. DB_DIR, DB_PATH)
    local_db_snapshot = {
        name: getattr(local_db, name)
        for name in dir(local_db)
        if name.isupper() and not name.startswith("_")
    }
    # Save paper_mode module state so we can restore it
    paper_state = paper_mode._state
    yield
    # Restore: remove any attributes added during the test, restore changed ones
    for mod, mod_snapshot in (
        (config, snapshot),
        (paper_mode, paper_snapshot),
        (local_db, local_db_snapshot),
    ):
        for name in dir(mod):
            if name.isupper() and not name.startswith("_"):
                if name not in mod_snapshot:
                    # Attribute was added during the test — remove it
                    try:
                        delattr(mod, name)
                    except AttributeError:
                        pass
                elif getattr(mod, name) is not mod_snapshot[name]:
                    # Attribute was mutated — restore it
                    try:
                        setattr(mod, name, mod_snapshot[name])
                    except (TypeError, AttributeError):
                        pass
    # Reset paper_mode module state
    paper_mode._state = paper_state

    # ── Reset module-level singletons/caches that leak state between tests ──
    # These are lowercase attributes NOT covered by the uppercase snapshot
    # above, so they persist across tests and cause order-dependent failures.
    try:
        import poly_us_stream
        poly_us_stream._poly_stream = None
    except Exception:
        pass
    try:
        import report_state
        report_state._n8n_client = None
        report_state._supabase_client = None
    except Exception:
        pass
    try:
        import trader
        trader._ml_model = None
        trader._ladder_strategy = None
        trader._bridge = None
        trader._state_reporter = None
        trader.state = None
    except Exception:
        pass
    try:
        import kalshi_client
        kalshi_client.KalshiClient._markets_cache.clear()
    except Exception:
        pass
    try:
        import polymarket_us_client
        polymarket_us_client._cached_client = None
        polymarket_us_client._cached_creds = ("", "")
        polymarket_us_client._RATE_BUCKET = polymarket_us_client.rate_limit.TokenBucket(
            1.0 / polymarket_us_client.POLYMARKET_MIN_GAP_SECONDS,
            burst=1,
            name="polymarket_us",
        )
    except Exception:
        pass
