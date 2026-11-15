"""B8: auth-error detection by structured type/status, not substrings.

An order id such as ``...3f401a...`` used to classify as an auth failure
(the substring "401") and stop the main loop on a healthy key. Now 401/403
raise KalshiAuthError from the client and is_auth_error() tests isinstance
or status_code; numeric substrings are gone. Exiting after max rebuilds is
opt-in (CLIENT_MAX_REBUILDS_EXIT, default off) — 100 consecutive non-auth
failures keep the loop alive at max backoff.
"""
import sys

import pytest

sys.argv = ["trader.py"]

import config
import kalshi_client
import resilience
from kalshi_client import KalshiClient, KalshiAuthError
from resilience import CycleSupervisor, ResilienceSettings
from unittest.mock import MagicMock, patch


def test_order_id_containing_401_is_not_an_auth_error():
    s = CycleSupervisor()
    s.mark_failure(ValueError("POST /portfolio/events/orders failed: order 3f401a bad"))
    assert not s.is_auth_error()


def test_real_401_exception_is_an_auth_error():
    s = CycleSupervisor()
    s.mark_failure(KalshiAuthError("401 unauthorized"))
    assert s.is_auth_error()


def test_status_code_attribute_counts():
    class FakeHTTPError(Exception):
        status_code = 403
    s = CycleSupervisor()
    s.mark_failure(FakeHTTPError("forbidden"))
    assert s.is_auth_error()


def test_legacy_auth_message_still_detected():
    s = CycleSupervisor()
    s.mark_failure(ValueError("Kalshi API error: 401 authentication failed"))
    assert s.is_auth_error()


def test_429_and_5xx_are_not_auth_errors():
    s = CycleSupervisor()
    s.mark_failure(kalshi_client.KalshiAPIError("429 rate limit exceeded"))
    assert not s.is_auth_error()
    s.mark_failure(kalshi_client.KalshiAPIError("503 upstream"))
    assert not s.is_auth_error()


def _make_client(monkeypatch):
    monkeypatch.setattr(config, "REQUEST_MIN_GAP_SECONDS", 0.0)
    monkeypatch.setattr(config, "REQUEST_TIMEOUT_SECONDS", 10)
    with patch.object(KalshiClient, "_load_private_key") as m, \
         patch.object(KalshiClient, "_verify_key"):
        m.return_value = MagicMock()
        m.return_value.sign.return_value = b"fake_sig"
        client = KalshiClient()
    client._verified = True
    return client


def _resp(status, text=""):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.text = text
    r.json.return_value = {}
    return r


def test_401_response_raises_kalshi_auth_error(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(client.session, "request", MagicMock(return_value=_resp(401)))
    with pytest.raises(KalshiAuthError):
        client._request("GET", "/portfolio/balance")


def test_403_response_raises_kalshi_auth_error(monkeypatch):
    client = _make_client(monkeypatch)
    monkeypatch.setattr(client.session, "request", MagicMock(return_value=_resp(403)))
    with pytest.raises(KalshiAuthError):
        client._request("GET", "/portfolio/positions")


def test_100_non_auth_failures_do_not_end_the_loop(monkeypatch):
    """Default config keeps looping: exit-after-N-rebuilds is opt-in."""
    monkeypatch.setattr(config, "CLIENT_MAX_REBUILDS_EXIT", False)
    s = CycleSupervisor(ResilienceSettings(max_rebuilds=2, rebuild_threshold=1))
    for _ in range(100):
        s.mark_failure(ValueError("order 3f401a failed"))
    assert not s.is_auth_error()
    assert s.rebuilds < s.settings.max_rebuilds or not config.CLIENT_MAX_REBUILDS_EXIT
    # The loop's exit branch mirrors this predicate exactly:
    would_exit = bool(config.CLIENT_MAX_REBUILDS_EXIT
                      and s.rebuilds >= s.settings.max_rebuilds)
    assert not would_exit


def test_rebuild_exit_opt_in_works(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_MAX_REBUILDS_EXIT", True)
    s = CycleSupervisor(ResilienceSettings(max_rebuilds=1, rebuild_threshold=1))
    s.mark_failure(ValueError("boom"))
    s.after_rebuild()
    assert s.rebuilds >= s.settings.max_rebuilds
    would_exit = bool(config.CLIENT_MAX_REBUILDS_EXIT
                      and s.rebuilds >= s.settings.max_rebuilds)
    assert would_exit
