import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from log_sanitizer import SanitizingFormatter


SYNTHETIC = "amount=$12.34 equity=56.78 cash: 90.12 session_pnl: -4.56"


def _run_logging(tmp_path, module, setup, log_name, relative):
    env = dict(os.environ, SUPERBOT_DATA_DIR=str(tmp_path), DRY_RUN="true",
               DRY_LOG_FILE=str(tmp_path / "kalshi.log"))
    code = (
        "import logging, logging.handlers;"
        f"import {module};"
        f"{module}.{setup}();"
        "hs=[h for h in logging.getLogger().handlers if isinstance(h,logging.FileHandler)];"
        "assert len(hs)==1 and type(hs[0]).__name__=='RotatingFileHandler';"
        "assert hs[0].maxBytes>0 and hs[0].backupCount>0;"
        f"logging.getLogger({log_name!r}).info({SYNTHETIC!r});"
        "logging.shutdown()"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[1],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    text = (tmp_path / relative).read_text()
    assert "12.34" not in text and "56.78" not in text
    assert "90.12" not in text and "-4.56" not in text
    assert "$[REDACTED]" in text
    assert "equity=[REDACTED]" in text
    assert "cash: [REDACTED]" in text
    assert "session_pnl: [REDACTED]" in text


@pytest.mark.parametrize("field", [
    "token", "api_key", "n8n_api_key", "n8n_access_token", "secret",
    "password", "private_key", "account_id", "accountId", "KALSHI_KEY_ID",
])
def test_credential_and_account_fields_are_redacted(field):
    record = logging.LogRecord("hostile", logging.INFO, __file__, 1,
                               f'{field}: "FAKE_VALUE_123"', (), None)
    text = SanitizingFormatter("%(message)s").format(record)
    assert "FAKE_VALUE_123" not in text
    assert f"{field}:" in text and "[REDACTED]" in text


@pytest.mark.parametrize("header", [
    "Authorization: Bearer FAKE_AUTH_123",
    "Proxy-Authorization: Basic FAKE_AUTH_123",
    "X-API-Key: FAKE_AUTH_123",
    "X-N8N-API-KEY: FAKE_AUTH_123",
])
def test_authorization_headers_are_redacted(header):
    record = logging.LogRecord("hostile", logging.INFO, __file__, 1, header, (), None)
    text = SanitizingFormatter("%(message)s").format(record)
    assert "FAKE_AUTH_123" not in text
    assert "[REDACTED]" in text


def test_redaction_preserves_ordinary_words_event_ids_and_tracebacks():
    formatter = SanitizingFormatter("%(message)s")
    ordinary = logging.LogRecord(
        "hostile", logging.INFO, __file__, 1,
        "token secret accountability event_id=ACCT-FAKE-12345 unicode=\u2603", (), None)
    text = formatter.format(ordinary)
    assert "token secret accountability" in text
    assert "event_id=ACCT-FAKE-12345" in text and "\u2603" in text
    try:
        raise ValueError("token=FAKE_TRACEBACK_123")
    except ValueError:
        record = logging.LogRecord("hostile", logging.ERROR, __file__, 1,
                                   "failed", (), sys.exc_info())
    trace = formatter.format(record)
    assert "Traceback (most recent call last)" in trace and "ValueError" in trace
    assert "FAKE_TRACEBACK_123" not in trace


@pytest.mark.parametrize("message", [
    '{"password":"FAKE_FIRST FAKE_SECOND"}',
    "{'password':'FAKE_FIRST FAKE_SECOND'}",
    "API KEY: FAKE_FIRST FAKE_SECOND",
    "API KEY=FAKE_FIRST FAKE_SECOND",
])
def test_multiword_credentials_are_fully_redacted(message):
    record = logging.LogRecord("hostile", logging.INFO, __file__, 1, message, (), None)
    text = SanitizingFormatter("%(message)s").format(record)
    assert "FAKE_FIRST" not in text and "FAKE_SECOND" not in text
    assert "[REDACTED]" in text


def test_multiword_traceback_and_unquoted_boundaries():
    formatter = SanitizingFormatter("%(message)s")
    try:
        raise ValueError("token='FAKE_FIRST FAKE_SECOND'")
    except ValueError:
        record = logging.LogRecord("hostile", logging.ERROR, __file__, 1,
                                   "failed", (), sys.exc_info())
    trace = formatter.format(record)
    assert "Traceback (most recent call last)" in trace and "ValueError" in trace
    assert "FAKE_FIRST" not in trace and "FAKE_SECOND" not in trace
    boundary = logging.LogRecord(
        "hostile", logging.INFO, __file__, 1,
        "token=FAKE_FIRST event_id=evt-synthetic-123", (), None)
    text = formatter.format(boundary)
    assert "FAKE_FIRST" not in text
    assert "event_id=evt-synthetic-123" in text


def test_json_escaped_quote_and_ampersand_boundaries():
    formatter = SanitizingFormatter("%(message)s")
    escaped = logging.LogRecord(
        "hostile", logging.INFO, __file__, 1,
        '{"password":"FAKE_FIRST\\" FAKE_SECOND"}', (), None)
    text = formatter.format(escaped)
    assert "FAKE_FIRST" not in text and "FAKE_SECOND" not in text

    try:
        raise ValueError('token="FAKE_FIRST\\" FAKE_SECOND"')
    except ValueError:
        record = logging.LogRecord("hostile", logging.ERROR, __file__, 1,
                                   "failed", (), sys.exc_info())
    trace = formatter.format(record)
    assert "Traceback (most recent call last)" in trace and "ValueError" in trace
    assert "FAKE_FIRST" not in trace and "FAKE_SECOND" not in trace

    separated = logging.LogRecord(
        "hostile", logging.INFO, __file__, 1,
        "token=FAKE_FIRST&event_id=evt-synthetic-123", (), None)
    text = formatter.format(separated)
    assert "FAKE_FIRST" not in text
    assert "event_id=evt-synthetic-123" in text


def test_kalshi_dry_logging_is_bounded_and_sanitized(tmp_path):
    _run_logging(tmp_path, "trader", "_configure_logging", "trader", "kalshi.log")


def test_poly_dry_logging_is_bounded_and_sanitized(tmp_path):
    _run_logging(tmp_path, "poly_us_trader", "_configure_logging", "poly_us_trader",
                 "poly_us/poly_us_dry.log")


@pytest.mark.parametrize("unit", ["superbot-dry.service", "poly-us-dry.service"])
def test_source_units_use_direct_release_commands(unit):
    text = (Path(__file__).parents[1] / "deploy" / unit).read_text()
    command = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert "/opt/superbot-releases/current/.venv/bin/python" in command
    assert "sed" not in command and "|" not in command and "/tmp/opencode" not in command
