"""B19 red-first tests: maker_price locked/crossed reject, report_state hook,
inspect.signature validation for the find_active_markets fallback."""
import inspect
from unittest.mock import MagicMock


import config
from signals import _maker_price


def _market(yes_bid, yes_ask, no_bid, no_ask):
    m = MagicMock()
    m.yes_bid = yes_bid
    m.yes_ask = yes_ask
    m.no_bid = no_bid
    m.no_ask = no_ask
    return m


# ── Item 1: crossed books rejected on BOTH sides; zero-spread books now price ─────

def test_maker_price_crossed_yes_book_returns_none():
    m = _market(60, 40, 50, 50)  # yes_ask < yes_bid → crossed
    config.AGGRESSIVE_FILL = False
    assert _maker_price(m, True) is None


def test_maker_price_crossed_no_book_returns_none():
    m = _market(50, 55, 60, 40)  # no_ask < no_bid → crossed NO book
    config.AGGRESSIVE_FILL = False
    assert _maker_price(m, False) is None


def test_maker_price_zero_spread_yes_book_prices():
    # yes_ask == yes_bid → zero spread (not crossed), should price at bid+1/ask-1
    m = _market(50, 50, 45, 55)
    config.AGGRESSIVE_FILL = False
    assert _maker_price(m, True) == 49  # min(bid+1, ask-1) = min(51, 49) = 49
    # NO book is normal
    assert _maker_price(m, False) == 46


def test_maker_price_zero_spread_no_book_prices():
    # no_ask == no_bid → zero spread (not crossed), should price at min(bid+1, ask-1)
    m = _market(50, 55, 45, 45)
    config.AGGRESSIVE_FILL = False
    assert _maker_price(m, False) == 44  # min(bid+1, ask-1) = min(46, 44) = 44


def test_maker_price_normal_book_still_prices():
    m = _market(50, 55, 45, 50)
    config.AGGRESSIVE_FILL = False
    assert _maker_price(m, True) == 51   # yes_bid + 1, under ask-1
    assert _maker_price(m, False) == 46  # no_bid + 1, under no_ask-1


# ── Item 4: report_state classification + on-change-only emission ──────────

def test_report_state_classification_priority():
    from report_state import classify
    # halted outranks everything
    assert classify(halted=True, cooldown=True, degraded=True, traded=True) == "halted"
    assert classify(halted=False, cooldown=True, degraded=True, traded=True) == "cooldown"
    assert classify(halted=False, cooldown=False, degraded=True, traded=True) == "degraded"
    assert classify(halted=False, cooldown=False, degraded=False, traded=True) == "trading"
    assert classify(halted=False, cooldown=False, degraded=False, traded=False) == "idle-no-signal"
    assert classify(halted=False, cooldown=False, degraded=False, traded=False, stopped=True) == "stopped"


def test_report_state_emits_only_on_change_or_heartbeat():
    from report_state import StateReporter
    emitted = []

    class FakeN8n:
        def run_monitor(self, data):
            emitted.append(("webhook", data))
            return {}

    class FakeStateDB:
        def set_bot_state(self, key, value):
            emitted.append(("bot_state", value))
            return True

    import report_state as rs
    orig_n8n, orig_sdb = rs._n8n_client, rs._state_db
    rs._n8n_client, rs._state_db = FakeN8n(), FakeStateDB()
    try:
        r = StateReporter("test-trader", heartbeat_seconds=3600.0)
        now = 1000.0
        r._now = lambda: now
        r.report("idle-no-signal", cycle=1)
        assert len(emitted) == 2, emitted  # webhook + bot_state on first emit
        # same state, heartbeat not due → no emit
        r.report("idle-no-signal", cycle=2)
        assert len(emitted) == 2
        # heartbeat due → re-emit same state
        r._now = lambda: now + 3601.0
        r.report("idle-no-signal", cycle=3)
        assert len(emitted) == 4
        # state change → immediate emit
        r.report("trading", cycle=4)
        assert len(emitted) == 6
        assert emitted[-2][1]["state"] == "trading"
    finally:
        rs._n8n_client, rs._state_db = orig_n8n, orig_sdb


def test_report_state_never_raises_when_sinks_fail():
    from report_state import StateReporter

    class Boom:
        def run_monitor(self, data):
            raise RuntimeError("n8n down")

    class Boom2:
        def set_bot_state(self, key, value):
            raise RuntimeError("local db down")

    import report_state as rs
    orig_n8n, orig_sdb = rs._n8n_client, rs._state_db
    rs._n8n_client, rs._state_db = Boom(), Boom2()
    try:
        r = StateReporter("test-trader", heartbeat_seconds=1.0)
        r._now = lambda: 0.0
        r.report("degraded", cycle=1)  # must not raise
    finally:
        rs._n8n_client, rs._state_db = orig_n8n, orig_sdb


# ── Item 4 wiring: trader.py hook classification ───────────────────────────

def test_kalshi_report_hook_classifies_cycle(monkeypatch):
    import trader
    emitted = []

    class FakeRep:
        def __init__(self, tag, **kw):
            pass

        def report(self, st, **f):
            emitted.append((st, f))

    monkeypatch.setattr(trader.report_state, "StateReporter", FakeRep)
    monkeypatch.setattr(trader, "_state_reporter", None)

    st = MagicMock()
    st.cycles = 5
    st.cycle_outcome = "traded"
    st.risk.state_dict.return_value = {"halted": False, "halt_reason": "", "cooldown_started_at": 0.0}
    st.risk._loss_cooldown_active.return_value = False
    monkeypatch.setattr(trader, "state", st)

    trader._report_cycle_state(100.0, [MagicMock()], st.risk.state_dict())
    assert emitted and emitted[0][0] == "trading"
    assert emitted[0][1]["cycle"] == 5

    # degraded: balance/positions fetch failed
    trader._report_cycle_state(None, None, st.risk.state_dict())
    assert emitted[-1][0] == "degraded"

    # halted: risk halt flag
    halted_state = {"halted": True, "halt_reason": "drawdown", "cooldown_started_at": 0.0}
    trader._report_cycle_state(100.0, [], halted_state)
    assert emitted[-1][0] == "halted"

    # cooldown: loss cooldown active
    st.risk._loss_cooldown_active.return_value = True
    st.cycle_outcome = "evaluated"
    trader._report_cycle_state(100.0, [], st.risk.state_dict())
    assert emitted[-1][0] == "cooldown"

    # idle: no trade, no halt
    st.risk._loss_cooldown_active.return_value = False
    st.cycle_outcome = "no-tradeable"
    trader._report_cycle_state(100.0, [], st.risk.state_dict())
    assert emitted[-1][0] == "idle-no-signal"


# ── Item 5: inspect.signature fallback instead of string-matching TypeError ─

def test_find_active_markets_sig_detection(monkeypatch):
    import trader

    def one_arg(client):
        return []

    def two_arg(client, target_series=None):
        return []

    # a mock/older caller without target_series in the signature → fallback path
    # is taken without raising or string-matching the exception
    with monkeypatch.context() as m:
        m.setattr(trader, "find_active_markets", one_arg)
        result = trader._find_active_markets_with_fallback(MagicMock(), "BTC-15M")
        assert result == []

    with monkeypatch.context() as m:
        m.setattr(trader, "find_active_markets", two_arg)
        result = trader._find_active_markets_with_fallback(MagicMock(), "BTC-15M")
        assert result == []


def test_fallback_signature_check_uses_inspect():
    import trader
    fn = trader._find_active_markets_with_fallback
    src = inspect.getsource(fn)
    assert "inspect.signature" in src
    assert '"target_series" in str' not in src
