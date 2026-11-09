"""Integration tests for paper_mode.py paper ledger."""

import pytest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import config
import paper_mode


class TestPaperModeLedger:
    """Test paper_mode ledger persistence and recovery."""

    def setup_method(self):
        """Reset paper_mode state before each test."""
        paper_mode._state = None
        # Remove any existing state file
        state_file = Path(paper_mode.STATE_FILE)
        if state_file.exists():
            state_file.unlink()

    def teardown_method(self):
        """Clean up after each test."""
        paper_mode._state = None
        state_file = Path(paper_mode.STATE_FILE)
        if state_file.exists():
            state_file.unlink()

    def test_start_balance_returns_configured_value(self):
        """start_balance should return DRY_STARTING_BALANCE from config."""
        with patch.object(config, 'DRY_STARTING_BALANCE', 100.0):
            assert paper_mode.start_balance() == 100.0

    def test_pnl_returns_zero_initially(self):
        """pnl should return 0.0 before any trades."""
        assert paper_mode.pnl() == 0.0

    def test_add_pnl_increments_realized_pnl(self):
        """add_pnl should increase the realized PnL."""
        paper_mode.add_pnl(10.50)
        assert paper_mode.pnl() == 10.50
        
        paper_mode.add_pnl(-2.25)
        assert paper_mode.pnl() == 8.25

    def test_cash_calculates_correctly(self):
        """cash should be start + pnl - open_risk."""
        paper_mode.add_pnl(5.0)
        # start=100, pnl=5, open_risk=2 -> cash=103
        assert paper_mode.cash(2.0) == 103.0

    def test_persists_to_file(self):
        """State should persist to paper_state.json."""
        paper_mode.add_pnl(25.75)
        
        # Reload by resetting module state
        paper_mode._state = None
        assert paper_mode.pnl() == 25.75
        assert paper_mode.start_balance() == 100.0

    def test_reset_clears_state_and_file(self):
        """reset should clear state and delete state file."""
        paper_mode.add_pnl(50.0)
        new_balance = paper_mode.reset()
        
        assert new_balance == 100.0
        assert paper_mode.pnl() == 0.0
        # File is deleted, next call recreates it
        assert paper_mode.start_balance() == 100.0

    def test_recovery_from_corrupted_file(self):
        """Should handle corrupted JSON gracefully and reseed."""
        # Write corrupted JSON
        Path(paper_mode.STATE_FILE).write_text("{invalid json")
        
        # Should not crash, should reseed
        paper_mode._state = None
        assert paper_mode.start_balance() == 100.0
        assert paper_mode.pnl() == 0.0

    def test_recovery_from_missing_file(self):
        """Should reseed when file doesn't exist."""
        paper_mode._state = None
        assert paper_mode.start_balance() == 100.0
        assert paper_mode.pnl() == 0.0

    def test_rounding_precision(self):
        """PnL should be rounded to 6 decimal places."""
        paper_mode.add_pnl(0.123456789)
        assert paper_mode.pnl() == 0.123457  # Rounded to 6 places

    def test_negative_pnl_allowed(self):
        """Should allow negative PnL (losses)."""
        paper_mode.add_pnl(-10.0)
        assert paper_mode.pnl() == -10.0
        paper_mode.add_pnl(5.0)
        assert paper_mode.pnl() == -5.0


class TestPaperModeConfig:
    """Test paper_mode configuration."""

    def setup_method(self):
        paper_mode._state = None
        state_file = Path(paper_mode.STATE_FILE)
        if state_file.exists():
            state_file.unlink()

    def teardown_method(self):
        paper_mode._state = None
        state_file = Path(paper_mode.STATE_FILE)
        if state_file.exists():
            state_file.unlink()

    def test_dry_starting_balance_configurable(self):
        """DRY_STARTING_BALANCE should be configurable."""
        with patch.object(config, 'DRY_STARTING_BALANCE', 500.0):
            paper_mode._state = None
            state_file = Path(paper_mode.STATE_FILE)
            if state_file.exists():
                state_file.unlink()
            
            assert paper_mode.start_balance() == 500.0


class TestPaperModeStateFile:
    """Test state file operations."""

    def setup_method(self):
        paper_mode._state = None
        state_file = Path(paper_mode.STATE_FILE)
        if state_file.exists():
            state_file.unlink()

    def teardown_method(self):
        paper_mode._state = None
        state_file = Path(paper_mode.STATE_FILE)
        if state_file.exists():
            state_file.unlink()

    def test_file_contains_correct_structure(self):
        """State file should have start_balance and pnl keys."""
        paper_mode.add_pnl(42.5)
        
        content = json.loads(Path(paper_mode.STATE_FILE).read_text())
        assert "start_balance" in content
        assert "pnl" in content
        assert content["start_balance"] == 100.0
        assert content["pnl"] == 42.5

    def test_multiple_add_pnl_calls_accumulate(self):
        """Multiple add_pnl calls should accumulate."""
        paper_mode.add_pnl(10.0)
        paper_mode.add_pnl(20.0)
        paper_mode.add_pnl(-5.0)
        
        paper_mode._state = None
        assert paper_mode.pnl() == 25.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])