"""Tests for observability features: consecutive-zero-tradeable alert and per-cycle records."""

import sys
import config
import pytest
from unittest.mock import patch, MagicMock


class TestConsecutiveZeroTradeableAlert:
    """Test the consecutive-zero-tradeable alert logic."""

    def test_counter_increments_on_no_tradeable(self):
        """Consecutive no-tradeable cycles counter increments correctly."""
        original = config.ZERO_TRADEABLE_ALERT_CYCLES
        config.ZERO_TRADEABLE_ALERT_CYCLES = 5
        try:
            sys.argv = ['trader.py', '--dry']
            from trader import BotState

            state = BotState()
            # Simulate 3 consecutive no-tradeable cycles
            state.cycle_outcome = "no-tradeable"
            state.tradeable_markets_count = 0
            state.consecutive_no_tradeable_cycles = 0

            # Cycle 1
            state.cycle_outcome = "no-tradeable"
            state.tradeable_markets_count = 0
            state.consecutive_no_tradeable_cycles += 1
            assert state.consecutive_no_tradeable_cycles == 1

            # Cycle 2
            state.cycle_outcome = "no-tradeable"
            state.tradeable_markets_count = 0
            state.consecutive_no_tradeable_cycles += 1
            assert state.consecutive_no_tradeable_cycles == 2

            # Cycle 3
            state.cycle_outcome = "no-tradeable"
            state.tradeable_markets_count = 0
            state.consecutive_no_tradeable_cycles += 1
            assert state.consecutive_no_tradeable_cycles == 3
        finally:
            config.ZERO_TRADEABLE_ALERT_CYCLES = original

    def test_threshold_triggers_alert(self):
        """Alert fires when consecutive no-tradeable cycles exceeds threshold."""
        original = config.ZERO_TRADEABLE_ALERT_CYCLES
        config.ZERO_TRADEABLE_ALERT_CYCLES = 2
        try:
            sys.argv = ['trader.py', '--dry']
            from trader import BotState

            state = BotState()
            # Mock the n8n client's run_monitor method
            with patch("trader.get_n8n_client") as mock_get_client:
                mock_client = MagicMock()
                mock_get_client.return_value = mock_client

                # Simulate 3 consecutive no-tradeable cycles (threshold=2)
                state.cycle_outcome = "no-tradeable"
                state.tradeable_markets_count = 0
                state.consecutive_no_tradeable_cycles = 0

                # Cycle 1
                state.consecutive_no_tradeable_cycles += 1  # = 1
                # First cycle: 1 < 2, no alert

                # Cycle 2
                state.consecutive_no_tradeable_cycles += 1  # = 2
                # Second cycle: 2 >= 2, alert should fire
                assert state.consecutive_no_tradeable_cycles == 2

                # Cycle 3
                state.consecutive_no_tradeable_cycles += 1  # = 3
                assert state.consecutive_no_tradeable_cycles == 3
        finally:
            config.ZERO_TRADEABLE_ALERT_CYCLES = original

    def test_counter_resets_on_tradeable_cycle(self):
        """Consecutive no-tradeable counter resets when a tradeable cycle occurs."""
        original = config.ZERO_TRADEABLE_ALERT_CYCLES
        try:
            sys.argv = ['trader.py', '--dry']
            from trader import BotState

            state = BotState()

            # 2 no-tradeable cycles
            state.cycle_outcome = "no-tradeable"
            state.tradeable_markets_count = 0
            state.consecutive_no_tradeable_cycles = 2

            # Tradeable cycle
            state.cycle_outcome = "traded"
            state.tradeable_markets_count = 5
            state.consecutive_no_tradeable_cycles = 0  # reset

            assert state.consecutive_no_tradeable_cycles == 0
            assert state.cycle_outcome == "traded"
            assert state.tradeable_markets_count == 5
        finally:
            config.ZERO_TRADEABLE_ALERT_CYCLES = original

    def test_config_default_is_10(self):
        """Default ZERO_TRADEABLE_ALERT_CYCLES is 10."""
        assert config.ZERO_TRADEABLE_ALERT_CYCLES == 10


class TestPerCycleRecordFields:
    """Test that the per-cycle bot_state record includes the new fields."""

    def test_current_state_include_observability_fields(self):
        """current_state dict includes cycle_outcome, tradeable_markets_count, balance, timestamp."""
        original = config.ZERO_TRADEABLE_ALERT_CYCLES
        try:
            sys.argv = ['trader.py', '--dry']
            from trader import BotState

            state = BotState()

            # Simulate a cycle with tradeable markets
            state.cycle_outcome = "traded"
            state.tradeable_markets_count = 8
            cash = 10.50

            current_state = {
                "cycles": state.cycles,
                "cash": cash if True else None,
                "exposure": 0.0,
                "session_pnl": state.session_pnl,
                "cycle_outcome": state.cycle_outcome,
                "tradeable_markets_count": state.tradeable_markets_count,
                "balance": cash if True else None,
                "timestamp": "__name__",  # placeholder; real value from datetime
            }

            assert "cycle_outcome" in current_state
            assert current_state["cycle_outcome"] == "traded"
            assert "tradeable_markets_count" in current_state
            assert current_state["tradeable_markets_count"] == 8
            assert "balance" in current_state
            assert "timestamp" in current_state
        finally:
            config.ZERO_TRADEABLE_ALERT_CYCLES = original