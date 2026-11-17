#!/bin/sh
# V1 — deploy an immutable release directory from a git sha.
# Thin wrapper: the real logic (C3 deploy lease + logstream announce +
# worktree + compileall/tests + smoke + current symlink swap + restart +
# heartbeat wait + rollback) lives in scripts/deploy.py so it is testable
# with fakes. See AGENT_TASKS_v7.md §V1.
exec python3 "$(dirname "$0")/deploy.py" --release "$@"
