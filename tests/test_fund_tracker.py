"""Tests for fund_tracker.py — READ-ONLY balance + manual transfer ledger.
Mocks kalshi_client and polymarket_us_client so no network/auth is required.
"""
import json
import sys
from unittest.mock import patch, MagicMock

import fund_tracker as ft


def _ledger_file(tmp_path, monkeypatch):
    p = tmp_path / "fund_ledger.jsonl"
    monkeypatch.setattr(ft, "LEDGER_FILE", p)
    return p


# --- balance subcommand -----------------------------------------------------

def test_balance_reads_both_venues(monkeypatch, capsys):
    monkeypatch.setattr(ft, "_read_kalshi_balance",
                        lambda: {"venue": "kalshi", "cash_dollars": 12.5, "ok": True})
    monkeypatch.setattr(ft, "_read_pm_balance",
                        lambda: {"venue": "polymarket_us", "cash_dollars": 30.0, "ok": True})
    assert ft.cmd_balance(None) == 0
    out = capsys.readouterr().out
    assert "kalshi" in out and "polymarket_us" in out
    assert "$12.50" in out and "$30.00" in out
    assert "$42.50" in out  # total


def test_balance_handles_error_gracefully(monkeypatch, capsys):
    monkeypatch.setattr(ft, "_read_kalshi_balance",
                        lambda: {"venue": "kalshi", "cash_dollars": None,
                                 "ok": False, "error": "boom"})
    monkeypatch.setattr(ft, "_read_pm_balance",
                        lambda: {"venue": "polymarket_us", "cash_dollars": 5.0, "ok": True})
    assert ft.cmd_balance(None) == 0
    out = capsys.readouterr().out
    assert "ERROR: boom" in out
    assert "$5.00" in out


# --- move subcommand --------------------------------------------------------

def test_move_records_to_ledger(monkeypatch, tmp_path):
    p = _ledger_file(tmp_path, monkeypatch)
    args = MagicMock(direction="kalshi_to_pm", amount=25.5, note="rebalance")
    assert ft.cmd_move(args) == 0
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 1
    e = json.loads(lines[0])
    assert e["direction"] == "kalshi_to_pm"
    assert e["amount_dollars"] == 25.5
    assert e["note"] == "rebalance"
    assert "ts" in e


def test_move_rejects_bad_amount(monkeypatch, tmp_path, capsys):
    p = _ledger_file(tmp_path, monkeypatch)
    assert ft.cmd_move(MagicMock(direction="kalshi_to_pm", amount=0, note="")) == 1
    assert not p.exists()
    assert "ERROR" in capsys.readouterr().err


def test_move_rejects_bad_direction(monkeypatch, tmp_path, capsys):
    p = _ledger_file(tmp_path, monkeypatch)
    assert ft.cmd_move(MagicMock(direction="sideways", amount=10, note="")) == 1
    assert not p.exists()
    assert "ERROR" in capsys.readouterr().err


# --- ledger subcommand ------------------------------------------------------

def test_ledger_lists_entries(monkeypatch, tmp_path, capsys):
    p = _ledger_file(tmp_path, monkeypatch)
    ft._append_ledger({"direction": "pm_to_kalshi", "amount_dollars": 40.0, "note": "x"})
    assert ft.cmd_ledger(None) == 0
    out = capsys.readouterr().out
    assert "pm_to_kalshi" in out and "40.00" in out


def test_ledger_empty_message(monkeypatch, tmp_path, capsys):
    _ledger_file(tmp_path, monkeypatch)
    assert ft.cmd_ledger(None) == 0
    assert "empty" in capsys.readouterr().out.lower()


# --- main() arg parsing -----------------------------------------------------

def test_main_balance_subcommand(monkeypatch):
    monkeypatch.setattr(ft, "cmd_balance", lambda a: 42)
    assert ft.main(["balance"]) == 42


def test_main_requires_subcommand():
    import pytest
    with pytest.raises(SystemExit):
        ft.main([])  # argparse required=True

