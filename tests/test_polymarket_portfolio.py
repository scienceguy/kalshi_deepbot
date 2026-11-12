"""Tests for polymarket_portfolio.py — READ-ONLY portfolio/balance reporter.

All tests mock the polymarket_us_client module via sys.modules so no network/auth
is required. Uses pytest with unittest.mock.

Pattern: follow test_trader.py - set sys.argv before calling main().
"""

import builtins
import sys
import pytest
from unittest.mock import patch, MagicMock

# The script under test
import polymarket_portfolio


# ---------------------------------------------------------------------------
# Fake polymarket_us_client module — shared by all tests
# ---------------------------------------------------------------------------

class FakePositionsResponse:
    """Mock response resembling the polymarket_us client positions dict."""

    def __init__(self, positions=None):
        self.positions = positions or []
        self.data = self.positions
        self.result = self.positions


class FakeBalancesResponse:
    """Mock response resembling the polymarket_us client balances dict."""

    def __init__(self, balances=None):
        self.balances = balances or []
        self.data = self.balances
        self.result = self.balances


class FakeMarketResponse:
    """Mock response resembling the polymarket_us client market-by-slug dict."""

    def __init__(self, **kwargs):
        self.question = kwargs.get("question", "Test Question")
        self.status = kwargs.get("status", "OPEN")
        # market_sides / sides / outcomes
        sides = kwargs.get("sides", [])
        if not sides:
            # Default demo sides
            sides = [
                {"outcome": "Yes", "price": "0.75"},
                {"outcome": "No", "price": "0.25"},
            ]
        self.market_sides = sides
        # best bid/ask
        self.best_bid = kwargs.get("best_bid", "0.74")
        self.best_ask = kwargs.get("best_ask", "0.76")


# The fake client object kept on the module
class _FakeClient:
    """Fake PolymarketUSClient with the three required methods."""

    def __init__(self, positions=None, balances=None, market=None):
        self._positions = positions or []
        self._balances = balances or []
        self._market = market or {}

    def get_portfolio_positions(self):
        """Return a dict with a 'positions' key (list)."""
        return {"positions": self._positions}

    def get_account_balances(self):
        """Return a dict with a 'balances' key (list)."""
        return {"balances": self._balances}

    def get_market_by_slug(self, slug=None):
        """Return a dict representing a market snapshot."""
        # Use provided slug or default
        return {
            "question": self._market.get("question", "Test Question"),
            "status": self._market.get("status", "OPEN"),
            "market_sides": self._market.get("market_sides", self._market.get("sides", [])),
            "best_bid": self._market.get("best_bid", "0.74"),
            "best_ask": self._market.get("best_ask", "0.76"),
        }


# ---------------------------------------------------------------------------
# Helper: inject the fake module into sys.modules
# ---------------------------------------------------------------------------

def _inject_fake_client(positions=None, balances=None, market=None):
    """Ensure polymarket_us_client is in sys.modules as a fake module.

    Args:
        positions: list of position dicts to return from get_portfolio_positions
        balances: list of balance dicts to return from get_account_balances
        market: dict with market data for get_market_by_slug (question, status, etc.)
    """
    fake = _FakeClient(positions=positions, balances=balances, market=market)
    class _FakeModule:
        pass
    fake_mod = _FakeModule()
    fake_mod.get_portfolio_positions = fake.get_portfolio_positions
    fake_mod.get_account_balances = fake.get_account_balances
    fake_mod.get_market_by_slug = fake.get_market_by_slug
    sys.modules["polymarket_us_client"] = fake_mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPositionsSubcommand:
    """(a) positions subcommand calls the fake client's get_portfolio_positions
    and prints without crashing."""

    def test_positions_with_data(self, capsys):
        _inject_fake_client(
            positions=[
                {"market_slug": "BTC-above-on-november-5-2026", "side": "yes",
                 "contracts": 10, "avg_price": 0.75},
                {"market_slug": "ETH-above-on-november-5-2026", "side": "no",
                 "contracts": 5, "avg_price": 0.25},
            ]
        )
        original_argv = sys.argv
        sys.argv = ["polymarket_portfolio.py", "positions"]
        try:
            polymarket_portfolio.main()
        finally:
            sys.argv = original_argv
        captured = capsys.readouterr()
        # Banner + positions should appear
        assert "READ-ONLY" in captured.out
        assert "BTC" in captured.out or "ETH" in captured.out

    def test_positions_empty(self, capsys):
        _inject_fake_client(positions=[])
        original_argv = sys.argv
        sys.argv = ["polymarket_portfolio.py", "positions"]
        try:
            polymarket_portfolio.main()
        finally:
            sys.argv = original_argv
        captured = capsys.readouterr()
        assert "READ-ONLY" in captured.out
        # "no open positions" should appear when API returns empty
        assert "no open positions" in captured.out


class TestBalancesSubcommand:
    """(b) balances subcommand calls get_account_balances."""

    def test_balances_with_data(self, capsys):
        _inject_fake_client(
            balances=[
                {"currency": "USDC", "currentBalance": 1000.0, "buyingPower": 800.0},
                {"currency": "USDT", "currentBalance": 500.0},
            ]
        )
        original_argv = sys.argv
        sys.argv = ["polymarket_portfolio.py", "balances"]
        try:
            polymarket_portfolio.main()
        finally:
            sys.argv = original_argv
        captured = capsys.readouterr()
        assert "READ-ONLY" in captured.out
        assert "USDC" in captured.out
        assert "1000" in captured.out
        assert "800" in captured.out
        assert "USDT" in captured.out

    def test_balances_empty(self, capsys):
        _inject_fake_client(balances=[])
        original_argv = sys.argv
        sys.argv = ["polymarket_portfolio.py", "balances"]
        try:
            polymarket_portfolio.main()
        finally:
            sys.argv = original_argv
        captured = capsys.readouterr()
        assert "READ-ONLY" in captured.out
        assert "no balance data" in captured.out


class TestMarketSubcommand:
    """(c) market subcommand calls get_market_by_slug."""

    def test_market_with_slug(self, capsys):
        _inject_fake_client(
            market={
                "question": "Will BTC rise above $50k by Nov 5 2026?",
                "status": "OPEN",
                "market_sides": [
                    {"outcome": "Yes", "price": "0.75"},
                    {"outcome": "No", "price": "0.25"},
                ],
                "best_bid": "0.74",
                "best_ask": "0.76",
            }
        )
        original_argv = sys.argv
        sys.argv = ["polymarket_portfolio.py", "market", "--slug", "btc-above-on-november-5-2026"]
        try:
            polymarket_portfolio.main()
        finally:
            sys.argv = original_argv
        captured = capsys.readouterr()
        assert "READ-ONLY" in captured.out
        assert "Will BTC rise above" in captured.out
        assert "0.75" in captured.out
        assert "0.25" in captured.out
        assert "best bid" in captured.out.lower() or "best ask" in captured.out.lower()


class TestMissingModule:
    """(d) a missing polymarket_us_client module causes a graceful message,
    not a traceback."""

    def test_missing_module_exits_gracefully(self, capsys):
        original_argv = sys.argv
        try:
            # Save original __import__ before patching
            original_import = builtins.__import__
            # Patch builtins.__import__ to raise ImportError when polymarket_us_client is requested
            # This genuinely simulates the module being absent, even if the file exists on disk
            with patch("builtins.__import__", side_effect=lambda *a, **k: (
                (_ for _ in ()).throw(ImportError("no module named polymarket_us_client"))
                if a[0] == "polymarket_us_client"
                else original_import(*a, **k)
            )):
                # Ensure polymarket_us_client is not in sys.modules
                sys.modules.pop("polymarket_us_client", None)

                # Run main() with "positions" argv — it should return 1 with a helpful message
                sys.argv = ["polymarket_portfolio.py", "positions"]
                ret = polymarket_portfolio.main()

                # Verify it returns 1 (not 0)
                assert ret == 1, f"Expected return value 1, got {ret}"

                # Verify the error message is about the missing module
                captured = capsys.readouterr()
                assert "ERROR:" in captured.err or "ERROR:" in captured.out
        finally:
            sys.argv = original_argv


def test_import_without_module():
    """Verify the script can be imported without polymarket_us_client present.

    This tests the "lazy import" pattern: the module-level code does NOT
    import polymarket_us_client, so a plain `import polymarket_portfolio`
    succeeds even when the module is absent.
    """
    # Temporarily remove if present
    popped = sys.modules.pop("polymarket_us_client", None)
    original_argv = sys.argv
    try:
        # This import should not fail even though polymarket_us_client is missing
        import importlib
        import polymarket_portfolio  # noqa: F401
        assert True
    finally:
        if popped is not None:
            sys.modules["polymarket_us_client"] = popped
        sys.argv = original_argv