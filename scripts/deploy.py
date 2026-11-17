#!/usr/bin/env python3
"""C3 — deploy helper. Refuses to run without the 'deploy' advisory lease.

Announces start and end on the logstream. Until V1 (release-directory
deploys) lands, the guarded action is an announcement-only no-op that
proves the lease path; V1 extends this script inside the guard.

Exit codes: 0 lease held and action completed, 2 refused (lease held by
someone else), 1 usage error.
"""

import argparse
import json
import subprocess
import sys

sys.path.insert(0, "/opt/superbot")  # when run from elsewhere

import leases  # noqa: E402


def _announce(kind: str, name: str, holder: str) -> None:
    """Append a lease-<start|end> status event. Advisory: never raises."""
    cmd = [
        "python3", "-m", "mempalace", "logstream", "append",
        "--type", "status",
        "--stream", "project/kalshi_deepbot",
        "--room", "status",
        "--topic", f"lease-{kind}",
        "--from-agent", holder,
        "--to-agent", "*",
        "--body", json.dumps({"lease": name, "event": kind,
                              "holder": holder}),
    ]
    try:
        subprocess.run(cmd, timeout=10, check=False, capture_output=True)
    except Exception:
        pass  # announcements are advisory; the lease row is the record


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--by", required=True, help="issuing agent identity")
    ap.add_argument("--ttl", type=float, default=leases.DEFAULT_TTL_S,
                    help="lease TTL seconds (default 300)")
    args = ap.parse_args(argv)
    try:
        with leases.guard("deploy", args.by, ttl_s=args.ttl):
            _announce("start", "deploy", args.by)
            # V1 (P7): the release-directory deploy runs here, inside the guard.
            _announce("end", "deploy", args.by)
            return 0
    except leases.LeaseHeld as e:
        print(json.dumps({"accepted": False, "reason": str(e)}),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
