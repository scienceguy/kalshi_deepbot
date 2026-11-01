"""Pytest configuration — isolate config mutation between tests.

Many tests mutate ``config`` module globals (e.g.
``config.EXPOSURE_CEILING_PCT = 0.30``) without restoring them, which makes
test outcomes order-dependent. This autouse fixture snapshots all
module-level uppercase config attributes before each test and restores them
afterwards, so no test can leak config state into another.
"""
import config
import pytest


@pytest.fixture(autouse=True)
def _isolate_config():
    """Snapshot and restore config module globals around each test."""
    # Snapshot all public (uppercase) config attributes
    snapshot = {
        name: getattr(config, name)
        for name in dir(config)
        if name.isupper() and not name.startswith("_")
    }
    yield
    # Restore: remove any attributes added during the test, restore changed ones
    for name in dir(config):
        if name.isupper() and not name.startswith("_"):
            if name not in snapshot:
                # Attribute was added during the test — remove it
                try:
                    delattr(config, name)
                except AttributeError:
                    pass
            elif getattr(config, name) is not snapshot[name]:
                # Attribute was mutated — restore it
                try:
                    setattr(config, name, snapshot[name])
                except (TypeError, AttributeError):
                    pass
