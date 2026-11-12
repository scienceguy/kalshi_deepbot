#!/bin/sh
# Start n8n with the SuperBot workflows' required node set.
#
# n8n 2.x disables n8n-nodes-base.executeCommand by default for security;
# the SuperBot Backtest/Data Collect workflows use it to run Python scripts.
# NODES_EXCLUDE=[] re-enables it. Any other launcher (systemd, TJS, plain
# `n8n start`) must set the same variable or those two webhooks return
# "Unrecognized node type".
exec env NODES_EXCLUDE='[]' n8n start "$@"
