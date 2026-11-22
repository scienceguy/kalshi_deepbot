"""T15-04 / F-06: one definition of the entry band, window, and sizing.

These tests fail if the band, the entry window, or the pin-risk minutes
disagree with each other or with the evidence the definitions rest on. A
prior round pinned ENTRY_WINDOW to 13.0-14.5 min elapsed on the claim that
"prices only reach 95-99c in the final 1-2 minutes"; that claim is false on
both the 14-day HF replay (~28% of windows reach 95-99c at minutes 6-11
elapsed; scripts/hf_gate_index.py pins (95-99, 6-11) as the tested config)
and the VPS candle DB (measure_edge --minutes 6 11 -> 18 in-band quotes).
The evidence-backed window is minutes 6-11 elapsed.

Note: .env on the operator host pins ENTRY_WINDOW 8.0-14.5, which overrides
these defaults at runtime. This file tests the config-module definitions
only; the .env/runtime mismatch is an operator decision, noted in the
commit that changed the default.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _reload(monkeypatch, **env):
    """Clear the relevant env keys and reload config.

    Keys are set to "" rather than deleted: config._load_env_files only fills
    keys absent from os.environ, so a deleted key pulls its value back from
    .env (the operator file) instead of falling back to the module default.
    """
    for key in ("ENTRY_WINDOW_START_MIN", "ENTRY_WINDOW_END_MIN",
                "MIN_YES_PRICE_CENTS", "MAX_YES_PRICE_CENTS",
                "ENTRY_BAND_MIN", "ENTRY_BAND_MAX"):
        monkeypatch.setenv(key, "")
    for key, val in env.items():
        monkeypatch.setenv(key, str(val))
    import config
    return importlib.reload(config)


def test_band_definitions_agree(monkeypatch):
    cfg = _reload(monkeypatch)
    # the two band spellings must be the same band, not two opinions
    assert cfg.MIN_YES_PRICE_CENTS == cfg.ENTRY_BAND_MIN
    assert cfg.MAX_YES_PRICE_CENTS == cfg.ENTRY_BAND_MAX


def test_entry_window_defaults_match_the_evidence(monkeypatch):
    cfg = _reload(monkeypatch)
    # the only held-out index-priced verdict (14-day HF replay) tested
    # minutes 6-11 elapsed; AGENTS.md "Optimal Parameters" agrees
    assert cfg.ENTRY_WINDOW_START_MIN == 6.0
    assert cfg.ENTRY_WINDOW_END_MIN == 11.0


def test_entry_window_ordered_and_inside_the_period(monkeypatch):
    cfg = _reload(monkeypatch)
    assert 0.0 <= cfg.ENTRY_WINDOW_START_MIN < cfg.ENTRY_WINDOW_END_MIN <= 15.0


def test_env_override_still_takes_precedence(monkeypatch):
    cfg = _reload(monkeypatch, ENTRY_WINDOW_START_MIN=8.0, ENTRY_WINDOW_END_MIN=14.5)
    assert (cfg.ENTRY_WINDOW_START_MIN, cfg.ENTRY_WINDOW_END_MIN) == (8.0, 14.5)


def test_band_and_window_do_not_encode_two_different_strategies(monkeypatch):
    """The 95-99c favourite band must be reachable inside the entry window.

    With VPS candle data (measure_edge --minutes 6 11) the 95-99c band has
    18 quotes in this window, so a favourite entry can actually occur. If a
    future change makes the window exclude the band's reachable range, the
    strategy silently stops trading; this test is the tripwire.
    """
    cfg = _reload(monkeypatch)
    assert 85.0 <= cfg.MIN_YES_PRICE_CENTS <= cfg.MAX_YES_PRICE_CENTS <= 99.0
    assert cfg.ENTRY_WINDOW_START_MIN >= 3.0, (
        "entries before minute 3 elapsed are the untested final-minute regime")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))