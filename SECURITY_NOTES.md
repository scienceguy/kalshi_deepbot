# Security Notes — Kalshi SuperBot

## 1. Secret Exposure & Key Rotation Audit
- **Supabase Service Key**: Historical commit logs referenced a Supabase service key. Confirm that the current production database uses a rotated secret key and that `SUPABASE_SERVICE_KEY` is loaded exclusively from `~/.secrets/supabase_service_key` (`chmod 600`) or environment variables, outside git tracking.
- **Kalshi API Key & RSA Private Key**: The production Kalshi RSA key lives at `~/.secrets/kalshi_private.pem` (`chmod 600`) outside the repo. Pre-commit hooks (`scripts/install-pre-commit.sh`) block `.env`, `*.pem`, and staged key patterns.

## 2. Sensitive Committed Files Review
The following committed files from early development should be scrubbed or ignored in future clones:
- `logs/agent-autopilot/opencode/*`: Agent execution metadata logs.
- `data/cfb/20261110.jsonl`: Market snapshot data.
- `paper_state_precutover_2026-11-13.json`: Pre-cutover paper trading state snapshot.

## 3. Remediation Recommendations
1. Ensure the GitHub repository visibility is set to **Private**.
2. Run periodic scans using `trufflehog` or `gitleaks` prior to public releases.
3. If public release is ever required, use `git-filter-repo` to scrub historical `*.pem`, `*.env`, and legacy log snapshots.

## 4. In-Process Code Execution Hardening
- **MemPalace Task Sweep (`_handle_task`)**: Verified strictly log-only. The daemon logs received inbox events but never evaluates, executes, or shells out task payload bodies.
