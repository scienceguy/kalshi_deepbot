"""M2 acceptance: Polymarket trader MemPalace parity through the M1 bridge.

Fakes only — no real MemPalace, no network, no live trader state. Covers
AGENT_TASKS_v7.md §M2:
- session start resumes the last checkpoint as a hint (DB wins) and posts an
  `online` status; session end posts the final checkpoint;
- checkpoint payload is operational only (no narrative fields);
- one drawer per breaker trip / risk halt; daily calibration summary drawer;
- measured win rate per price band as a KG fact: kg_supersede on change,
  kg_invalidate when the band stops being measured;
- no payload contains key-like strings.
"""
import re
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("polymarket_us")

import poly_us_trader
from poly_us_trader import PolyUSPaperTrader


class FakeBridge:
    """Records every queued write; nothing is delivered anywhere."""

    def __init__(self):
        self.statuses = []
        self.checkpoints = []
        self.heartbeats = []
        self.drawers = []
        self.kg = []

    def start(self):
        pass

    def stop(self, timeout: float = 5.0):
        pass

    def status(self, status_type, extra=None):
        self.statuses.append((status_type, dict(extra or {})))

    def checkpoint(self, state, correlation_id=None):
        self.checkpoints.append((dict(state), correlation_id))

    def heartbeat(self, cycles_completed=0):
        self.heartbeats.append(cycles_completed)

    def drawer(self, content, metadata=None):
        self.drawers.append((content, dict(metadata or {})))

    def kg_add(self, subject, predicate, obj, source_drawer_id=None):
        self.kg.append(("add", subject, predicate, obj))

    def kg_supersede(self, subject, predicate, old_obj, new_obj,
                     source_drawer_id=None):
        self.kg.append(("supersede", subject, predicate, old_obj, new_obj))

    def kg_invalidate(self, subject, predicate, obj, ended=None):
        self.kg.append(("invalidate", subject, predicate, obj))


@pytest.fixture
def trader(tmp_path):
    state_file = tmp_path / "state.json"
    ledger_file = tmp_path / "ledger.jsonl"
    fake = FakeBridge()
    with patch("poly_us_trader.STATE_FILE", state_file), \
         patch("poly_us_trader.LEDGER_FILE", ledger_file), \
         patch("poly_us_trader._mp_bridge", lambda: fake):
        t = PolyUSPaperTrader(initial_cash=35.0, reset=True)
        t._fake_bridge = fake
        yield t


OPERATIONAL_KEYS = {
    "venue", "mode", "epoch", "equity", "open_positions",
    "positions_summary", "halt", "breaker", "last_cycle_ts",
    "cycles_completed", "git_commit", "config_hash",
}
NARRATIVE_KEYS = {"self_narrative", "interests", "recent_thoughts",
                  "recent_urls", "exploration_depth", "last_topic"}
KEY_PATTERNS = re.compile(
    r"API[_-]?KEY|SECRET|TOKEN|PASSWORD|PRIVATE[_-]?KEY|"
    r"BEGIN [A-Z ]*PRIVATE|sk_live|sk-live|AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{10,}")


def test_session_start_resumes_hint_and_posts_online(trader):
    with patch("poly_us_trader._mp_resume_hint",
               return_value={"venue": "poly", "cycles_completed": 12,
                             "equity": 33.0, "last_cycle_ts": "2026-01-01"}):
        trader.on_mempalace_session_start()
    statuses = trader._fake_bridge.statuses
    assert len(statuses) == 1
    assert statuses[0][0] == "online"
    payload = statuses[0][1]
    assert payload["venue"] == "poly"
    assert payload["mode"] == "dry"
    assert isinstance(payload["equity"], float)
    assert payload["git_commit"] == poly_us_trader._MP_GIT_COMMIT
    assert payload["config_hash"] == poly_us_trader._MP_CONFIG_HASH
    # The hint is advisory only — local state is untouched (DB wins).
    assert trader.state["cash"] == 35.0
    assert trader.state["cycles"] == 0


def test_session_start_without_hint_still_online(trader):
    with patch("poly_us_trader._mp_resume_hint", return_value=None):
        trader.on_mempalace_session_start()
    assert trader._fake_bridge.statuses[0][0] == "online"


def test_session_end_posts_final_checkpoint(trader):
    trader.state["cycles"] = 7
    trader.on_mempalace_session_end()
    cps = trader._fake_bridge.checkpoints
    assert len(cps) == 1
    payload, corr = cps[0]
    assert corr.startswith("poly-final-")
    assert payload["venue"] == "poly"
    assert payload["cycles_completed"] == 7


def test_checkpoint_payload_is_operational_only(trader):
    trader.state["cycles"] = 9
    trader._mp_checkpoint()
    payload, _ = trader._fake_bridge.checkpoints[0]
    assert set(payload) == OPERATIONAL_KEYS
    assert not (set(payload) & NARRATIVE_KEYS)
    assert set(payload["halt"]) <= {"ruin", "venue_recon", "daily_loss",
                                    "a1_breaker"}
    for summary in payload["positions_summary"]:
        assert set(summary) <= {"slug", "side", "qty", "cost"}


def test_breaker_trip_files_exactly_one_drawer(trader, monkeypatch):
    # One valid market, A1 breaker tripping then clearing.
    market = {"slug": "m1", "question": "q", "outcomePrices": ["0.95", "0.05"]}
    monkeypatch.setattr(trader, "scan_markets", lambda: [market])
    monkeypatch.setattr("poly_us_trader.emit_event", lambda *a, **k: None)
    monkeypatch.setattr("poly_us_trader.emit_sync_lag", lambda *a, **k: None)
    monkeypatch.setattr(poly_us_trader, "_poly_state_reporter", MagicMock())
    monkeypatch.setitem(sys.modules, "control",
                        types.SimpleNamespace(drain_and_apply=lambda *a, **k: None))
    trips = {"n": 0}

    def fake_breaker(**kwargs):
        trips["n"] += 1
        return (trips["n"] == 1, "brier above threshold", False)

    monkeypatch.setattr(trader.cal_monitor, "circuit_breaker", fake_breaker)
    trader.state["_last_halted_seen"] = False  # baseline so the trip is a transition

    trader.cycle()  # halt transition -> exactly one drawer
    assert len(trader._fake_bridge.drawers) == 1
    content, meta = trader._fake_bridge.drawers[0]
    assert "POLY RISK HALT" in content
    assert "reason: calibration" in content  # halt category
    assert "reason=brier above threshold" in content  # A1 detail
    assert meta["event"] == "risk_halt"
    assert "equity:" in content and "mode: dry" in content

    trader.cycle()  # still halted -> no second drawer
    assert len(trader._fake_bridge.drawers) == 1

    trader.cycle()  # resume transition -> event, never a drawer
    assert len(trader._fake_bridge.drawers) == 1
    # heartbeat fired each cycle; cycles persisted in the DB-of-record state
    assert len(trader._fake_bridge.heartbeats) == 3
    assert trader.state["cycles"] == 3


def test_daily_calibration_drawer(trader):
    trader._mp_daily_calibration_drawer()
    assert len(trader._fake_bridge.drawers) == 1
    content, meta = trader._fake_bridge.drawers[0]
    assert "POLY DAILY CALIBRATION" in content
    assert meta["event"] == "daily_calibration"


def test_calibration_fact_supersedes_on_change(trader):
    trader.state["_kg_wr_band_92_99c"] = "0.9000@n=25"
    trader._publish_calibration_facts({"price_band_92_99c": {
        "observed_win_rate": 0.95, "n": 30}})
    kg = trader._fake_bridge.kg
    assert kg == [("supersede", "poly_us_trader", "measured_win_rate_92_99c",
                   "0.9000@n=25", "0.9500@n=30")]
    # Same value again -> no new fact write.
    trader._publish_calibration_facts({"price_band_92_99c": {
        "observed_win_rate": 0.95, "n": 30}})
    assert len(trader._fake_bridge.kg) == 1


def test_calibration_fact_adds_when_no_previous(trader):
    trader._publish_calibration_facts({"price_band_92_99c": {
        "observed_win_rate": 0.9734, "n": 12}})
    assert trader._fake_bridge.kg == [
        ("add", "poly_us_trader", "measured_win_rate_92_99c", "0.9734@n=12")]
    assert trader.state["_kg_wr_band_92_99c"] == "0.9734@n=12"


def test_calibration_fact_invalidated_when_band_retired(trader):
    trader.state["_kg_wr_band_92_99c"] = "0.9500@n=30"
    trader._publish_calibration_facts({"price_band_92_99c": None})
    assert trader._fake_bridge.kg == [
        ("invalidate", "poly_us_trader", "measured_win_rate_92_99c",
         "0.9500@n=30")]
    assert "_kg_wr_band_92_99c" not in trader.state
    # Nothing left to invalidate.
    trader._publish_calibration_facts({"price_band_92_99c": None})
    assert len(trader._fake_bridge.kg) == 1


def test_no_payload_contains_key_like_strings(trader):
    with patch("poly_us_trader._mp_resume_hint", return_value=None):
        trader.on_mempalace_session_start()
    trader.state["cycles"] = 4
    trader._mp_checkpoint()
    trader.state["_kg_wr_band_92_99c"] = "0.9000@n=25"
    trader._publish_calibration_facts({"price_band_92_99c": {
        "observed_win_rate": 0.95, "n": 30}})
    trader._mp_halt_drawer("calibration")
    trader._mp_daily_calibration_drawer()

    blobs = []
    for status_type, extra in trader._fake_bridge.statuses:
        blobs.append(status_type + repr(extra))
    for payload, corr in trader._fake_bridge.checkpoints:
        blobs.append(repr(payload) + str(corr))
    for content, meta in trader._fake_bridge.drawers:
        blobs.append(content + repr(meta))
    for rec in trader._fake_bridge.kg:
        blobs.append(repr(rec))
    for blob in blobs:
        assert not KEY_PATTERNS.search(blob), f"key-like string in payload: {blob[:120]}"


def test_bridge_unavailable_degrades_silently(trader):
    with patch("poly_us_trader._mp_bridge", return_value=None):
        trader.on_mempalace_session_start()  # no raise
        trader.on_mempalace_session_end()    # no raise
        trader._publish_calibration_facts({"price_band_92_99c": {
            "observed_win_rate": 0.95, "n": 30}})  # no raise
    assert trader._fake_bridge.statuses == []


def test_m2_uses_bridge_not_mempalace_cli():
    # No synchronous mempalace CLI subprocesses on the trading path (M1); the
    # parity surface goes through the bridge accessor instead.
    text = open(poly_us_trader.__file__).read()
    assert '"mempalace"' not in text
    assert "'mempalace'" not in text
    for call in ("bridge.status", "bridge.checkpoint", "bridge.drawer",
                 "bridge.kg_supersede", "bridge.kg_invalidate",
                 "bridge.kg_add", "bridge.heartbeat"):
        assert call in text
