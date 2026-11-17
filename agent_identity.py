"""agent_identity.py — one canonical identity module (M3).

Three distinct agent identities exist on this host, and until this module
they were spelled ad hoc — some sources still said the old pre-VPS host
(Termux) in the shared identity string.

- ``CODING_AGENT``  — the OpenCode coding agent working on this repo.
- ``KALSHI_TRADER`` — the Kalshi trading service process.
- ``POLY_TRADER``   — the Polymarket US trading service process.

Every ``from_agent`` / ``created_by`` / ``agent_id`` in the codebase comes
from here.
"""

CODING_AGENT = "ubuntu:opencode:kalshi_deepbot"
KALSHI_TRADER = "ubuntu:service:kalshi_trader"
POLY_TRADER = "ubuntu:service:poly_us_trader"

ALL = (CODING_AGENT, KALSHI_TRADER, POLY_TRADER)


def presence_topic(identity: str) -> str:
    """Topic name for an identity's presence announcements (presence/heartbeat
    lanes, e.g. C1's bus). Keeps the mapping in exactly one place."""
    return f"presence-{identity}"
