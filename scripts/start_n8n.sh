#!/bin/sh
# Start n8n for the SuperBot workflows.
#
# H5: Execute Command is deliberately NOT re-enabled. Anything that needs to
# run a script goes through scripts/job_bridge.py on 127.0.0.1 instead, so an
# n8n editor/API login is no longer a path to shell access.
#
# n8n 2.x disables n8n-nodes-base.executeCommand by default; leaving the
# default in place is the point. Do not add NODES_EXCLUDE='[]' here.
exec n8n start "$@"
