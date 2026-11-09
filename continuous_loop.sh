#!/bin/bash
# Continuous dry-run loop: run trader cycles until paper balance reaches $10,000
TARGET=10000
CYCLE=0
while true; do
    CYCLE=$((CYCLE + 1))
    echo "=== Cycle $CYCLE ==="
    
    # Run one dry-run cycle
    timeout 45 python trader.py --dry --once 2>&1 | grep -E "cash=|paper:|tradeable|signal|New 15m|No signals|No tradeable"
    
    # Check paper balance
    PAPER=$(python3 -c "import paper_mode; print(f'{paper_mode.cash(0.0):.2f}')" 2>/dev/null)
    echo "Paper balance: $PAPER"
    
    # Check if target reached
    if python3 -c "import sys; sys.exit(0 if float('$PAPER') >= $TARGET else 1)" 2>/dev/null; then
        echo "TARGET REACHED: $PAPER >= $TARGET"
        break
    fi
    
    # MemPalace checkpoint
    python3 -c "
import local
state = {
    'resume_key': 'continuous_dev_cycle_$CYCLE',
    'cycles_completed': $CYCLE,
    'paper_balance': float('$PAPER'),
    'target': $TARGET,
    'last_topic': 'continuous_dry_run',
}
local.checkpoint(state)
" 2>/dev/null
    
    # Wait before next cycle (15s interval)
    sleep 15
done
echo "Continuous loop complete."
