"""Pytest configuration — isolate config mutation between tests.

Many tests mutate ``config`` module globals (e.g.
``config.EXPOSURE_CEILING_PCT = 0.30``) without restoring them, which makes
test outcomes order-dependent. This autouse fixture snapshots all
module-level uppercase config attributes before each test and restores them
afterwards, so no test can leak config state into another.

Also isolates ``paper_mode`` module state so the paper ledger (which
persists to paper_state.json and local_db) never leaks between tests.
"""
import os
import tempfile

# Redirect every runtime file the bot writes into a throwaway dir BEFORE the
# modules below read these env vars at import time. Previously the suite wrote
# synthetic KXBTC15M-TEST rows into the real markout_log.jsonl (330 rows,
# -$280.50) and could touch paper_state.json and local_data/.
_TMP = tempfile.mkdtemp(prefix="deepbot-tests-")
os.environ["MARKOUT_LOG_FILE"] = os.path.join(_TMP, "markout_log.jsonl")
os.environ["GATES_LOG_FILE"] = os.path.join(_TMP, "gates_log.jsonl")
os.environ["PAPER_STATE_FILE"] = os.path.join(_TMP, "paper_state.json")
os.environ["SUPERBOT_LOCAL_DB_DIR"] = os.path.join(_TMP, "local_data")
os.environ["TREND_MAKER_LOG_FILE"] = os.path.join(_TMP, "trend_maker_log.jsonl")

import config  # noqa: E402
import paper_mode  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_config():
    """Snapshot and restore config module globals around each test.

    Also saves and restores paper_mode._state so paper ledger state never
    leaks between tests.
    """
    # Snapshot all public (uppercase) config attributes
    snapshot = {
        name: getattr(config, name)
        for name in dir(config)
        if name.isupper() and not name.startswith("_")
    }
    # Save paper_mode module state so we can restore it
    paper_state = paper_mode._state
    yield
    # Restore: remove any attributes added during the test, restore changed ones
    for name in dir(config):
        if name.isupper() and not name.startswith("_"):
            if name not in snapshot:
                # Attribute was added during the test — remove it
                try:
                    delattr(config, name)
                except AttributeError:
                    pass
            elif getattr(config, name) is not snapshot[name]:
                # Attribute was mutated — restore it
                try:
                    setattr(config, name, snapshot[name])
                except (TypeError, AttributeError):
                    pass
    # Reset paper_mode module state
    paper_mode._state = paper_state
