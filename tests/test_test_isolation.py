"""Each conftest.py redirect must point its module at the throwaway dir.

A bare "the path is a tmpdir" check proves less than it looks, so these also
assert the value is NOT inside the repo. Both modules resolve their path from
the env var at import time, so if the redirect in conftest.py is removed the
imported value becomes the repo default and the test fails here, naming it.
"""
from pathlib import Path

import config
import local_db
import paper_mode

REPO_ROOT = Path(__file__).resolve().parent.parent
TMP_PREFIX = "deepbot-tests-"


def _assert_redirected(value, label):
    p = Path(str(value))
    assert TMP_PREFIX in str(p), f"{label} not in throwaway dir: {p}"
    assert REPO_ROOT not in p.parents, f"{label} still inside repo: {p}"


def test_markout_log_redirected():
    _assert_redirected(config.MARKOUT_LOG_FILE, "config.MARKOUT_LOG_FILE")


def test_gates_log_redirected():
    _assert_redirected(config.GATES_LOG_FILE, "config.GATES_LOG_FILE")


def test_paper_state_redirected():
    _assert_redirected(paper_mode.STATE_FILE, "paper_mode.STATE_FILE")


def test_local_db_dir_redirected():
    _assert_redirected(local_db.DB_DIR, "local_db.DB_DIR")
    _assert_redirected(local_db.DB_PATH, "local_db.DB_PATH")
