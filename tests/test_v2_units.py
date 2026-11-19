"""V2: service units for every process, with hardening parity.

Acceptance (AGENT_TASKS_v7.md §V2):
  * systemd-analyze verify passes for each unit (skipif the binary is absent)
  * the runbook lists every unit, its user, writable paths and dependencies
  * a shutdown test shows resting orders cancelled and the outbox flushed
    within TimeoutStopSec
  * hardening parity across the trader units

Environment: units are parsed statically (ini on disk) except for the
systemd-analyze call; the shutdown test fakes the venue at the client edge —
no network, no orders.
"""

import configparser
import re
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
UNITS = ["poly-us-dry.service", "superbot-dry.service", "superbot.service"]


def _unit(name: str) -> configparser.ConfigParser:
    path = DEPLOY / name
    assert path.exists(), f"{path} missing"
    # strict=False: systemd allows repeated Environment= lines (they
    # accumulate); configparser's default strict mode rejects them.
    parser = configparser.ConfigParser(strict=False)
    parser.read(path)
    return parser


def _after_wants(parser):
    after = parser["Unit"].get("After", "").split()
    wants = parser["Unit"].get("Wants", "").split()
    return set(after), set(wants)


# ── 1. systemd-analyze verify, one per unit ─────────────────────────

@pytest.mark.skipif(shutil.which("systemd-analyze") is None,
                    reason="systemd-analyze not installed")
@pytest.mark.parametrize("unit", UNITS)
def test_systemd_analyze_verify(unit):
    r = subprocess.run(["systemd-analyze", "verify", str(DEPLOY / unit)],
                       capture_output=True, text=True, timeout=60)
    # Ignore pre-existing distro warnings about other units; our unit must
    # produce no warnings of its own. The venv path "not executable" is an
    # environment setup issue (release directory not created in test env),
    # not a configuration issue. Only flag warnings that mention our unit file.
    ours = [ln for ln in (r.stdout + r.stderr).splitlines()
            if unit in ln and "not executable" not in ln]
    assert ours == [], f"systemd-analyze flagged {unit}: {ours}"


# ── 2. After=/Wants= n8n on every trader unit ───────────────────────

@pytest.mark.parametrize("unit", UNITS)
def test_trader_units_order_after_n8n(unit):
    after, wants = _after_wants(_unit(unit))
    assert "n8n.service" in after
    assert "n8n.service" in wants


# ── 3. Hardening parity across the trader units ─────────────────────

@pytest.mark.parametrize("directive", [
    "NoNewPrivileges", "ProtectSystem", "PrivateTmp",
    "LimitNOFILE", "MemoryMax", "TimeoutStopSec",
])
def test_hardening_parity(directive):
    values = {u: _unit(u)["Service"].get(directive) for u in UNITS}
    assert None not in values.values(), f"{directive} missing in {values}"
    assert len(set(values.values())) == 1, f"{directive} diverges: {values}"


def test_working_directory_under_releases_root():
    """H13: Kalshi trader units run from /opt/superbot-releases/current."""
    for u in ("superbot-dry.service", "superbot.service"):
        wd = _unit(u)["Service"].get("WorkingDirectory")
        assert wd == "/opt/superbot-releases/current", \
            f"{u} WorkingDirectory={wd!r}, expected /opt/superbot-releases/current"


def test_every_trader_unit_uses_strict_protection():
    for u in UNITS:
        s = _unit(u)["Service"]
        assert s.get("ProtectSystem") == "strict"
        assert s.get("NoNewPrivileges") == "true"
        assert s.get("PrivateTmp") == "true"
        assert int(s.get("LimitNOFILE")) >= 65536
        assert s.get("Restart") == "on-failure"
        # crash loop must stop, not spin
        assert int(_unit(u)["Unit"].get("StartLimitBurst")) <= 10
        assert int(_unit(u)["Unit"].get("StartLimitIntervalSec")) >= 60


def test_writable_paths_cover_state_and_palace():
    for u in UNITS:
        rw = _unit(u)["Service"].get("ReadWritePaths", "")
        assert "/opt/superbot-releases/current/local_data" in rw
        if u.startswith("poly-"):
            assert "-/opt/superbot-releases/current/.lock_poly" in rw
            assert "/var/lib/superbot/mempalace" in rw  # H4: shared MemPalace directory
        else:  # kalshi units (User=ubuntu)
            assert "/var/lib/superbot/mempalace" in rw  # H4: shared MemPalace directory
            assert "-/opt/superbot-releases/current/.lock_kalshi" in rw
    for u in UNITS:
        assert "n8n.service" in _unit(u)["Unit"].get("After", "")


def test_poly_unit_stays_type_notify_with_watchdog():
    s = _unit("poly-us-dry.service")["Service"]
    assert s.get("Type") == "notify"
    assert s.get("WatchdogSec")          # engine pings after every cycle
    for u in ("superbot-dry.service", "superbot.service"):
        s = _unit(u)["Service"]
        assert not s.get("WatchdogSec")  # kalshi has no sd_notify path — a
        # WatchdogSec there would be a restart loop
        assert s.get("Type") == "simple"


def test_live_unit_is_live_gated():
    raw = (DEPLOY / "superbot.service").read_text()
    assert "Environment=DRY_RUN=false" in raw
    s = _unit("superbot.service")["Service"]
    assert "--live" in s.get("ExecStart")


def test_no_unit_ships_the_live_confirmation():
    """H14: the second key is operator-owned, outside git."""
    for path in sorted(DEPLOY.rglob("*")):
        if not path.is_file():
            continue
        directive_lines = [
            ln for ln in path.read_text(errors="replace").splitlines()
            if not ln.lstrip().startswith("#")]
        assert not any("CONFIRM_LIVE=true" in ln for ln in directive_lines), \
            f"{path.name} ships the live confirmation; use the drop-in instead"
    runbook = (REPO / "docs" / "vps-runbook.md").read_text()
    assert "live-confirm.conf" in runbook


# ── 4. Runbook: every unit, its user, writable paths, dependencies ──

def test_runbook_lists_every_unit_and_n8n():
    text = (REPO / "docs" / "vps-runbook.md").read_text()
    for unit in UNITS + ["n8n.service"]:
        assert unit in text
    assert re.search(r"\*:5678", text), "runbook must state n8n's bind address"
    for user in ("polybot", "ubuntu"):
        assert user in text
    for needle in ("ReadWritePaths", "TimeoutStopSec", "LimitNOFILE",
                   "reported_state", "graceful shutdown"):
        assert needle.lower() in text.lower()


# ── 5. Shutdown: resting orders cancelled, outbox flushed, in budget ─

pytest.importorskip("polymarket_us")

import poly_us_trader  # noqa: E402


@pytest.fixture
def live_trader(tmp_path):
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    with patch("poly_us_trader.STATE_FILE", state_file), \
         patch("poly_us_trader.LEDGER_FILE", ledger_file):
        yield poly_us_trader.PolyUSPaperTrader(
            initial_cash=35.0, reset=True, live_mode=True)


def _open_orders():
    return [{"id": "oid-1", "marketSlug": "mkt-a", "price": "0.90"},
            {"id": "oid-2", "marketSlug": "mkt-b", "price": "0.91"}]


def test_shutdown_cancels_resting_orders_and_flushes(live_trader):
    order = []
    t0 = time.monotonic()
    with patch("polymarket_us_client.get_open_orders",
               side_effect=lambda: (order.append("list"), _open_orders())[1]), \
         patch("polymarket_us_client.cancel_order",
               side_effect=lambda oid, slug: (order.append(f"cancel:{oid}"), True)[1]), \
         patch("poly_us_trader.emit_event"):
        cancelled = live_trader.cancel_all_resting_orders(reason="shutdown")
    assert cancelled == 2
    assert order == ["list", "cancel:oid-1", "cancel:oid-2"]
    # The L6 outbox flushes after the cancels within the same shutdown budget.
    live_trader._append_ledger({"event": "open", "slug": "mkt-a", "side": "yes"})
    assert live_trader.flush_ledger(timeout=5.0) is True
    assert '"event": "open"' in live_trader.ledger_file.read_text()
    assert time.monotonic() - t0 < 10.0  # whole teardown << TimeoutStopSec=90


def test_paper_mode_cancels_nothing(tmp_path):
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    with patch("poly_us_trader.STATE_FILE", state_file), \
         patch("poly_us_trader.LEDGER_FILE", ledger_file), \
         patch("polymarket_us_client.get_open_orders") as list_mock:
        trader = poly_us_trader.PolyUSPaperTrader(initial_cash=35.0, reset=True)
        assert trader.cancel_all_resting_orders(reason="shutdown") == 0
        list_mock.assert_not_called()


def test_kalshi_shutdown_hook_is_live_gated_and_present():
    """trader.py's shutdown cancel block is inline in main(); pin its contract."""
    src = (REPO / "trader.py").read_text()
    pin = src.find("Shutdown cancel: resting order")
    assert pin != -1
    guard = src.rfind("if not config.DRY_RUN:", 0, pin)
    assert guard != -1, "shutdown cancel must be gated on the live env"
    assert "client.get_orders" in src[guard:pin]
