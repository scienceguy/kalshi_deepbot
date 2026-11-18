"""H11 end-to-end: a real ``python -m agent_bus`` receiver subprocess persists
a pushed envelope into a real SQLite bus_inbox, and scripts/bus_inbox.py
recovers it. Nothing is monkeypatched in the receiver path — the only shared
state is the tmp_path DB and the loopback socket. The signing secret is a
random per-run value injected via config/env (never hardcoded, never printed).
"""
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import agent_bus
import agent_identity
import config

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "bus.db"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(port: int, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            raise RuntimeError(f"receiver exited early rc={proc.returncode}: {out}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("receiver did not open its port within 10s")


def _rows(db_path: Path) -> list[dict]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM bus_inbox")]


def _cli(env, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/bus_inbox.py", *args],
                          cwd=REPO_ROOT, env=env, capture_output=True,
                          text=True, timeout=10)


class TestBusE2E:
    def test_push_lands_in_bus_inbox_and_cli_recovers(self, tmp_path, monkeypatch):
        secret = secrets.token_hex(32)  # per-run test secret, not the .env one
        monkeypatch.setattr(config, "SUPERBOT_WEBHOOK_SECRET", secret)
        db_path = tmp_path / DB_NAME
        port = _free_port()
        sub_env = {
            **os.environ,
            "SUPERBOT_LOCAL_DB_PATH": str(db_path),
            "SUPERBOT_DATA_DIR": str(tmp_path),
            "SUPERBOT_WEBHOOK_SECRET": secret,
            "AGENT_BUS_PORT_OVERRIDES": json.dumps(
                {agent_identity.CODING_AGENT: port}),
        }
        proc = subprocess.Popen([sys.executable, "-m", "agent_bus"],
                                cwd=REPO_ROOT, env=sub_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            _wait_port(port, proc)
            mid = f"e2e-{uuid.uuid4()}"
            corr = f"corr-{uuid.uuid4()}"
            env = agent_bus.envelope(to_agent=agent_identity.CODING_AGENT,
                                     from_agent=agent_identity.KALSHI_TRADER,
                                     body="durable hello",
                                     type="task.request",
                                     topic="h11-e2e",
                                     correlation_id=corr,
                                     message_id=mid)
            resp = agent_bus.post_envelope(env, port=port, timeout=2.0)
            assert resp.get("ok") is True and not resp.get("duplicate")

            rows = _rows(db_path)  # raw sqlite3, independent of LocalDB
            assert len(rows) == 1
            row = rows[0]
            assert row["message_id"] == mid
            assert row["type"] == "task.request"
            assert row["from_agent"] == agent_identity.KALSHI_TRADER
            assert row["correlation_id"] == corr
            assert row["status"] == "pending"

            replay = agent_bus.post_envelope(env, port=port, timeout=2.0)
            assert replay.get("ok") is True and replay.get("duplicate") is True
            assert len(_rows(db_path)) == 1  # H12 replay guard + H11 store

            listed = _cli(sub_env, "list")
            assert listed.returncode == 0, listed.stderr
            msgs = json.loads(listed.stdout)
            assert [m["message_id"] for m in msgs] == [mid]
            row_id = msgs[0]["id"]

            acked = _cli(sub_env, "ack", str(row_id))
            assert acked.returncode == 0, acked.stderr
            assert json.loads(acked.stdout)["ok"] is True

            listed_again = _cli(sub_env, "list")
            assert json.loads(listed_again.stdout) == []
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
