"""Regression tests for the Nov-2026 audit fixes."""
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import config
import gates
import market_finder

ROOT = Path(__file__).resolve().parent.parent


def test_fetch_orderbook_catches_kalshi_api_error():
    """KalshiAPIError was never imported, so the handler raised NameError."""
    class Boom:
        def get_orderbook(self, *a, **k):
            raise market_finder.KalshiAPIError("404")
    assert market_finder._fetch_orderbook(Boom(), "T") == {}


def test_prefetch_does_not_wipe_decision_cache():
    """prefetch() gets asset symbols; the cache is keyed by (ticker, side)."""
    gates._last_decision.clear()
    gates._last_decision[("KXBTC15M-26NOV090415-15", "yes")] = "allow"
    with patch.object(gates, "spot_for", return_value=1.0), \
         patch.object(gates, "atr_1m", return_value=1.0):
        gates.prefetch({"BTC"})
    assert ("KXBTC15M-26NOV090415-15", "yes") in gates._last_decision


def test_decision_cache_is_bounded():
    gates._last_decision.clear()
    for i in range(gates._LAST_DECISION_MAX + 50):
        gates._last_decision[(f"T{i}", "yes")] = "allow"
    gates._prune_last_decision()
    assert len(gates._last_decision) == gates._LAST_DECISION_MAX
    assert ("T0", "yes") not in gates._last_decision


def test_trader_imports_without_torch():
    """trader must not need torch unless --ml-model is passed."""
    code = (
        "import sys; sys.modules['torch'] = None; "
        "sys.argv=['trader.py','--dry']; import trader; print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert "ok" in r.stdout, r.stderr[-800:]


def test_tests_do_not_write_prod_logs():
    assert "deepbot-tests-" in str(config.MARKOUT_LOG_FILE)
    assert "deepbot-tests-" in str(config.GATES_LOG_FILE)
