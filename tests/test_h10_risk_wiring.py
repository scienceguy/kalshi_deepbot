"""H10 — no RiskManager method may exist without a production caller.

The audit found `kelly_size`, `check_liquidity` and the per-ticker exposure
pair had no call sites in the traders. `kelly_size` and `check_liquidity` are
in fact called from `RiskManager.check()`, which `trader._cycle` calls with
`win_prob`, `entry_price_cents` and `market` — so they are wired, and the two
tests below prove they actually shrink or block an entry. The per-ticker pair
was deleted: its map was never populated, so its "cap" never bound.
"""

import inspect
import re
from pathlib import Path

import config
import control
import risk

REPO = Path(__file__).resolve().parents[1]

# Methods that are intentionally not called outside risk.py, with the reason.
ALLOWLIST = {
    # reachable only through MarketOrder-style internal helpers; kept for
    # state persistence and the operator summary
}


def _public_methods():
    return sorted(
        name for name, _fn in inspect.getmembers(risk.RiskManager, inspect.isfunction)
        if not name.startswith("_"))


def test_every_public_risk_method_has_a_caller():
    sources = [p.read_text() for p in sorted(REPO.glob("*.py"))]
    missing = []
    for name in _public_methods():
        if name in ALLOWLIST:
            continue
        # attribute access only: `self.kelly_size(` / `r.kelly_size(` — the
        # definition line `def kelly_size(` does not count as a caller
        pat = re.compile(rf"\.{re.escape(name)}\(")
        if any(pat.search(src) for src in sources):
            continue
        callback_registered = any(
            "RISK_REVIEW_APPROVER = self.approve_operator_review" in src
            for src in sources)
        callback_invoked = any("RISK_REVIEW_APPROVER(reviewer)" in src
                               for src in sources)
        if (name == "approve_operator_review" and callback_registered
                and callback_invoked):
            continue
        missing.append(name)
    assert missing == [], f"no caller for RiskManager methods: {missing}"


def test_operator_review_callback_releases_recovered_red_halt(monkeypatch):
    r = _manager(monkeypatch)
    r.state.operator_review_required = True
    r.state.halted = True
    r.state.last_drawdown = -0.05
    control.STATE.pause_entries = True

    assert control.approve_risk_review("Zeta") is True
    assert r.state.operator_review_required is False
    assert r.state.halted is False
    assert control.STATE.pause_entries is False


def test_sizing_guards_are_reachable_from_the_traders_check():
    """check_liquidity is called by check(); kelly_size by max_trade_dollars,
    which check() calls; and trader calls check()."""
    risk_src = (REPO / "risk.py").read_text()
    trader_src = (REPO / "trader.py").read_text()
    check_body = risk_src.split("def check(", 1)[1].split("\n    def ", 1)[0]
    size_body = risk_src.split("def max_trade_dollars(", 1)[1].split("\n    def ", 1)[0]
    assert ".check_liquidity(" in check_body
    assert ".max_trade_dollars(" in check_body
    assert ".kelly_size(" in size_body
    assert ".check(" in trader_src


def _market(bid, ask, depth):
    return type("M", (), {"yes_bid": bid, "yes_ask": ask, "yes_liquidity": depth,
                          "no_bid": bid, "no_ask": ask, "no_liquidity": depth})()


def _manager(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "CASH_FLOOR_PCT", 0.70)
    monkeypatch.setattr(config, "EXPOSURE_CEILING_PCT", 0.30)
    monkeypatch.setattr(config, "MIN_CASH_ABSOLUTE", 0.0)
    monkeypatch.setattr(config, "PER_TRADE_PCT", 0.50)
    monkeypatch.setattr(config, "MAX_TRADE_DOLLARS", 1000.0)
    r = risk.RiskManager()
    r.initialize(100.0)
    return r


def test_liquidity_guard_blocks_a_wide_entry(monkeypatch):
    monkeypatch.setattr(config, "MAX_SPREAD_CENTS", 3)
    monkeypatch.setattr(config, "MIN_DEPTH_CONTRACTS", 5)
    r = _manager(monkeypatch)
    ok, reason = r.check(100.0, 0.0, 1.0, "KXBTC15M-YES", 100.0,
                         market=_market(90, 99, 100), side="yes")
    assert ok is False
    assert "liquidity" in reason


def test_kelly_guard_shrinks_a_thin_edge(monkeypatch):
    """kelly_size really sizes: a thinner edge yields a smaller cap, and an
    edge below the EV floor yields zero, which blocks the entry in check()."""
    monkeypatch.setattr(config, "MAX_SPREAD_CENTS", 10)
    monkeypatch.setattr(config, "MIN_DEPTH_CONTRACTS", 1)
    monkeypatch.setattr(config, "KELLY_FRACTION", 0.5)
    monkeypatch.setattr(config, "MIN_EV_EDGE_PCT", 0.01)
    r = _manager(monkeypatch)
    market = _market(94, 95, 100)
    fat = r.kelly_size(0.95, 80, 100.0)
    thin = r.kelly_size(0.87, 80, 100.0)
    assert fat > thin > 0.0
    # Below the EV floor Kelly returns 0 -> check() refuses any trade cost.
    ok, reason = r.check(100.0, 0.0, 1.0, "KXBTC15M-YES", 100.0,
                         win_prob=0.50, entry_price_cents=80, market=market, side="yes")
    assert ok is False
    assert "risk cap" in reason
