"""agent_identity.py — one canonical identity module (M3).

Five distinct agent identities exist on this host:

- ``CODING_AGENT``      — the OpenCode coding agent working on this repo.
- ``KALSHI_TRADER``     — the Kalshi trading service process.
- ``POLY_TRADER``       — the Polymarket US live trading service process.
- ``POLY_US_PAPER``     — the Polymarket US paper trading service process.
- ``POLY_US_COLLECTOR`` — the Polymarket US data collector service process.

Every ``from_agent`` / ``created_by`` / ``agent_id`` in the codebase comes
from here.
"""

CODING_AGENT = "ubuntu:opencode:kalshi_deepbot"
KALSHI_TRADER = "ubuntu:service:kalshi_trader"
POLY_TRADER = "ubuntu:service:poly_us_trader"
POLY_US_PAPER = "ubuntu:service:poly_us_paper"
POLY_US_COLLECTOR = "ubuntu:service:poly_us_collector"

ALL = (CODING_AGENT, KALSHI_TRADER, POLY_TRADER, POLY_US_PAPER, POLY_US_COLLECTOR)


def presence_topic(identity: str) -> str:
    """Topic name for an identity's presence announcements (presence/heartbeat
    lanes, e.g. C1's bus). Keeps the mapping in exactly one place."""
    return f"presence-{identity}"
