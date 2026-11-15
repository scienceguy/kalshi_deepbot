"""B7: rate_limit.py is wired into production clients.

- kalshi_client uses rate_limit.Backoff for 429 waits: Retry-After is honored
  and capped (an uncapped venue header used to make us sleep for the header's
  raw value — hours), and the backoff sleeps through a fake clock.
- both production clients import rate_limit.
- the per-cycle Kalshi request counter warns over the budget
  (KALSHI_REQUEST_BUDGET_PER_CYCLE, 0 disables).
"""
import sys

# trader.py parses argv at import time; give it a clean one first.
sys.argv = ["trader.py"]

from unittest.mock import MagicMock, patch

import pytest

import config
import kalshi_client
import polymarket_us_client
import rate_limit
import trader as trader_mod
from kalshi_client import KalshiClient


@pytest.fixture(autouse=True)
def _config_defaults(monkeypatch):
    monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
    monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(config, "KALSHI_REQUEST_BUDGET_PER_CYCLE", 40)
    yield


def _make_client(monkeypatch):
    with patch.object(KalshiClient, "_load_private_key") as m, \
         patch.object(KalshiClient, "_verify_key"):
        m.return_value = MagicMock()
        m.return_value.sign.return_value = b"fake_sig"
        client = KalshiClient()
    client._verified = True
    return client


def _resp(status, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = {"markets": []}
    r.text = ""
    return r


# ── production import exists ────────────────────────────────────────────

def test_production_clients_import_rate_limit():
    """A production import exists (B7 acceptance, first half)."""
    assert kalshi_client.rate_limit is rate_limit
    assert polymarket_us_client.rate_limit is rate_limit


# ── 429 backoff honors Retry-After, capped ──────────────────────────────

def test_429_retry_after_is_honored(monkeypatch):
    client = _make_client(monkeypatch)
    sleeps = []
    monkeypatch.setattr(kalshi_client.time, "sleep", lambda s: sleeps.append(s))
    req = MagicMock(side_effect=[
        _resp(429, {"Retry-After": "3"}),
        _resp(200),
    ])
    monkeypatch.setattr(client.session, "request", req)
    out = client._request("GET", "/markets")
    assert out == {"markets": []}
    assert sleeps and abs(sleeps[0] - 3.0) < 1e-6   # exact Retry-After honored


def test_429_retry_after_is_capped(monkeypatch):
    """A hostile/crazy Retry-After must not sleep the bot for hours."""
    client = _make_client(monkeypatch)
    sleeps = []
    monkeypatch.setattr(kalshi_client.time, "sleep", lambda s: sleeps.append(s))
    req = MagicMock(side_effect=[
        _resp(429, {"Retry-After": "9999"}),
        _resp(200),
    ])
    monkeypatch.setattr(client.session, "request", req)
    client._request("GET", "/markets")
    assert sleeps and sleeps[0] <= 30.0
    assert sleeps[0] > 0


def test_429_without_header_uses_backoff(monkeypatch):
    client = _make_client(monkeypatch)
    sleeps = []
    monkeypatch.setattr(kalshi_client.time, "sleep", lambda s: sleeps.append(s))
    req = MagicMock(side_effect=[_resp(429), _resp(200)])
    monkeypatch.setattr(client.session, "request", req)
    client._request("GET", "/markets")
    assert sleeps and 0 < sleeps[0] <= 30.0   # jittered backoff, not raw 0.7*2**n


# ── per-cycle request counter + budget WARNING ──────────────────────────

def test_request_counter_counts_attempts(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(client.session, "request",
                        MagicMock(return_value=_resp(200)))
    client.reset_request_counter()
    client._request("GET", "/markets")
    client._request("GET", "/markets")
    assert client.request_count == 2


def test_cycle_request_budget_warning(caplog):
    client = MagicMock()
    client.request_count = 41
    with caplog.at_level("WARNING"):
        trader_mod._report_kalshi_request_budget(client)
    assert any("request budget" in r.message for r in caplog.records)


def test_cycle_request_budget_disabled(caplog):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "KALSHI_REQUEST_BUDGET_PER_CYCLE", 0)
    try:
        client = MagicMock()
        client.request_count = 999
        with caplog.at_level("WARNING"):
            trader_mod._report_kalshi_request_budget(client)
        assert not any("request budget" in r.message for r in caplog.records)
    finally:
        monkeypatch.undo()


def test_budget_helper_uses_client_counter(caplog):
    client = MagicMock()
    client.request_count = 10
    with caplog.at_level("DEBUG"):
        trader_mod._report_kalshi_request_budget(client)
    assert not any("request budget" in r.message for r in caplog.records)
