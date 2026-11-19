"""B16: import-time side effects moved into main()/parse_args().

Acceptance: `python -c "import trader, poly_us_trader"` prints nothing, writes
no files, and does not read sys.argv — a bogus flag on the command line must
not make the import exit non-zero.
"""
import os
import subprocess
import sys
from pathlib import Path


sys.argv = ["trader.py"]

import config
import trader

REPO = Path(__file__).resolve().parent.parent


def _repo_files():
    return {p.name for p in REPO.iterdir() if p.is_file()}


def test_import_prints_nothing_creates_nothing_ignores_argv(tmp_path):
    """Acceptance: import trader, poly_us_trader via -c with a bogus flag.

    A parser still wired into import treats the flag as a real argument and
    exits non-zero (argparse) — that is the red half of this test."""
    before = _repo_files()
    proc = subprocess.run(
        [sys.executable, "-c",
         "import trader, poly_us_trader; print('IMPORTED')",
         "--definitely-not-a-real-flag"],
        capture_output=True, text=True, timeout=300,
        cwd=tmp_path,
        # A clean clone has no .env, so config logs an ERROR at import for the
        # missing credentials. Give the child the minimum valid values so this
        # test measures import side effects, not the host's secrets.
        env={**os.environ, "PYTHONPATH": str(REPO),
             "KALSHI_KEY_ID": "dummy",
             "KALSHI_PRIVATE_KEY": "dummy",
             "KALSHI_KEY_FILE": "",
             "SUPABASE_URL": "https://dummy.supabase.co",
             "SUPABASE_SERVICE_KEY": "dummy-key"},
    )
    assert proc.returncode == 0, (
        f"import exited {proc.returncode}; stderr:\n{proc.stderr[:800]}")
    assert proc.stdout == "IMPORTED\n"
    assert proc.stderr == ""
    assert _repo_files() == before, "import created new files in the repo root"
    assert list(tmp_path.iterdir()) == [], "import wrote files to the scratch cwd"


def test_parse_args_defaults_and_flag_overrides(monkeypatch):
    """parse_args(argv=None) is the only parser; --once/--live/--dry/--debug work
    and --dry sets config.DRY_RUN=True."""
    monkeypatch.setattr("config.DRY_RUN", False)
    monkeypatch.setattr("config.LOG_LEVEL", "INFO")
    args = trader.parse_args(["--dry"])
    assert args.dry is True and args.once is False and args.live is False
    assert config.DRY_RUN is True
    monkeypatch.setattr("config.DRY_RUN", False)
    args = trader.parse_args(["--debug"])
    assert args.debug is True
    assert config.LOG_LEVEL == "DEBUG"


def test_get_state_creates_lazy_singleton():
    """get_state() returns the same BotState every call; state exists only after
    the first get_state()."""
    s1 = trader.get_state()
    s2 = trader.get_state()
    assert s1 is s2
    assert isinstance(s1, trader.BotState)
