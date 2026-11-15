"""B19: pyflakes must report zero findings on the core file list.

Pinned by the operator (2026-11-15): no suppression or baseline-exception
machinery — zero findings is the bar, and this test is green in the same
commit as the B19 lint cleanup.
"""

import pathlib
import sys

import pyflakes.api
import pyflakes.reporter

ROOT = pathlib.Path(__file__).resolve().parents[1]

CORE_FILES = [
    "backtest_real.py",
    "binance_client.py",
    "binance_feed.py",
    "crossvenue.py",
    "crossvenue_history.py",
    "historical_downloader.py",
    "ml_model.py",
    "rate_limit.py",
    "trader.py",
    "signals.py",
    "market_finder.py",
    "inject_n8n_workflows.py",
    "scripts/naru_momentum.py",
]


def test_pyflakes_zero_findings_on_core_files():
    class _Collector(pyflakes.reporter.Reporter):
        def __init__(self):
            super().__init__(sys.stdout, sys.stderr)
            self.findings = []

        def flake(self, message):
            self.findings.append(str(message))

    findings = []
    for rel in CORE_FILES:
        path = ROOT / rel
        src = path.read_text(encoding="utf-8")
        collector = _Collector()
        pyflakes.api.check(src, str(path), collector)
        findings.extend(collector.findings)
    assert findings == [], f"pyflakes findings:\n" + "\n".join(findings)
