"""V1: SUPERBOT_DATA_DIR isolation acceptance tests.

Acceptance criteria (per AGENT_TASKS_v6.md):
  * With SUPERBOT_DATA_DIR set to a temp dir, a full cycle() writes nothing
    outside it.
  * Two instances with different data dirs hold locks simultaneously.
  * Same dir refuses a second instance.
"""

import importlib
from pathlib import Path

import pytest

import config


def _reload_with_data_dir(monkeypatch, data_dir: str):
    """Set SUPERBOT_DATA_DIR env var and reload config (+ dependents).

    conftest.py pre-sets MARKOUT_LOG_FILE / GATES_LOG_FILE / PAPER_STATE_FILE /
    PAPER_POSITIONS_FILE / SUPERBOT_LOCAL_DB_DIR which take precedence over
    SUPERBOT_DATA_DIR, so clear them too so the SUPERBOT_DATA_DIR fallback wins.
    """
    monkeypatch.setenv("SUPERBOT_DATA_DIR", data_dir)
    for key in ("MARKOUT_LOG_FILE", "GATES_LOG_FILE", "PAPER_STATE_FILE",
                "PAPER_POSITIONS_FILE", "SUPERBOT_LOCAL_DB_DIR",
                "SUPERBOT_LOCAL_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(config)
    return config


# ── V1-1: all runtime path anchors live under SUPERBOT_DATA_DIR ──────────


def test_default_data_dir_is_repo_local_data(monkeypatch):
    """Without SUPERBOT_DATA_DIR, paths fall back to repo-local local_data/."""
    monkeypatch.delenv("SUPERBOT_DATA_DIR", raising=False)
    cfg = _reload_with_data_dir(monkeypatch, "")
    assert cfg.SUPERBOT_DATA_DIR == Path(__file__).resolve().parent.parent / "local_data"
    assert cfg.MARKOUT_LOG_FILE == cfg.SUPERBOT_DATA_DIR / "markout_log.jsonl"
    assert cfg.GATES_LOG_FILE == cfg.SUPERBOT_DATA_DIR / "gates_log.jsonl"
    assert cfg.LOG_FILE == cfg.SUPERBOT_DATA_DIR / "bot.log"


def test_data_dir_from_env(monkeypatch, tmp_path):
    """With SUPERBOT_DATA_DIR set, all config paths live under it."""
    data_dir = tmp_path / "my_data"
    cfg = _reload_with_data_dir(monkeypatch, str(data_dir))
    assert cfg.SUPERBOT_DATA_DIR == data_dir
    assert cfg.MARKOUT_LOG_FILE == data_dir / "markout_log.jsonl"
    assert cfg.GATES_LOG_FILE == data_dir / "gates_log.jsonl"
    assert cfg.LOG_FILE == data_dir / "bot.log"


def test_trader_paths_under_data_dir(monkeypatch, tmp_path):
    """trader.py runtime files resolve under SUPERBOT_DATA_DIR."""
    data_dir = tmp_path / "trader_data"
    _ = _reload_with_data_dir(monkeypatch, str(data_dir))
    importlib.reload(__import__("trader"))
    import trader

    assert trader.SETTLEMENT_QUEUE_FILE == data_dir / "settlement_queue.json"
    assert trader.SETTLED_SINCE_FILE == data_dir / "settled_since.json"
    assert trader.PAPER_POSITIONS_FILE == data_dir / "paper_positions.json"
    assert trader.INSTANCE_LOCK_NAME == ".lock_kalshi"


def test_local_db_dir_falls_back_to_superbot_data_dir(monkeypatch, tmp_path):
    """local_db.py DB_DIR falls back to SUPERBOT_DATA_DIR (not local_data/)."""
    data_dir = tmp_path / "db_data"
    _ = _reload_with_data_dir(monkeypatch, str(data_dir))
    importlib.reload(__import__("local_db"))
    import local_db

    assert local_db.DB_DIR == data_dir


def test_poly_us_paths_under_data_dir(monkeypatch, tmp_path):
    """poly_us_trader.py runtime files resolve under SUPERBOT_DATA_DIR/poly_us."""
    data_dir = tmp_path / "poly_data"
    _ = _reload_with_data_dir(monkeypatch, str(data_dir))
    importlib.reload(__import__("paper_mode"))
    import paper_mode
    assert paper_mode.STATE_FILE == data_dir / "paper_state.json"


# ── V1-2: lock behavior ─────────────────────────────────────────────────


def test_two_instances_different_dirs_hold_locks_simultaneously(monkeypatch, tmp_path):
    """Acceptance: two instances with different data dirs hold locks at once."""
    import trader

    dir_a = tmp_path / "data_a"
    dir_b = tmp_path / "data_b"
    dir_a.mkdir()
    dir_b.mkdir()

    lock_a = dir_a / ".lock_kalshi"
    lock_b = dir_b / ".lock_kalshi"

    # Two different lock files can both be acquired.
    fd1 = trader.acquire_instance_lock(lock_a)
    fd2 = trader.acquire_instance_lock(lock_b)
    try:
        # Neither raises — both locks held simultaneously.
        assert fd1 is not None
        assert fd2 is not None
    finally:
        import os
        os.close(fd1)
        os.close(fd2)


def test_same_dir_refuses_second_instance(tmp_path, monkeypatch):
    """Acceptance: same dir refuses a second instance lock."""
    import trader

    lock_path = tmp_path / ".lock_kalshi"
    fd1 = trader.acquire_instance_lock(lock_path)
    try:
        with pytest.raises(SystemExit) as exc:
            trader.acquire_instance_lock(lock_path)
        assert exc.value.code == 1
    finally:
        import os
        os.close(fd1)
