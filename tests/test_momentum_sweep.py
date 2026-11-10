"""Guards for scripts/momentum_sweep.py: a typo'd override would silently
sweep nothing, so assert every variant knob is a real config attribute."""
import importlib.util
from pathlib import Path

import config

_SPEC = importlib.util.spec_from_file_location(
    "momentum_sweep",
    Path(__file__).resolve().parent.parent / "scripts" / "momentum_sweep.py",
)
ms = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ms)


def test_variant_overrides_are_real_config_attrs():
    for label, overrides in ms.VARIANTS:
        for key in overrides:
            assert hasattr(config, key), f"{label}: unknown config attr {key}"


def test_summarise_ci_brackets_mean():
    s = ms.summarise([-0.2, 0.1, -0.3, 0.05])
    assert s["n"] == 4
    assert s["lo"] <= s["mean"] <= s["hi"]
    assert 0.0 <= s["win"] <= 1.0
