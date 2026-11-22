# Secret hygiene report (AGENT_TASKSv16.md T16-02)

Date: 2026-11-22  
Agent: opencode:kalshi_deepbot  
Repo HEAD at scan: 98ac689736a83a5c5169c64d3cf1a8abd6daacaf  
Repo visibility: PUBLIC (read-only `gh repo view`)

## Tool and commands (evidence)

1. gitleaks full history:
   `gitleaks detect --source . --report-format json --report-path /tmp/opencode/gitleaks_history.json --exit-code 0 --redact`
   -> "711 commits scanned ... no leaks found" (0 findings).

2. gitleaks working tree:
   `gitleaks detect --source . --no-git --exit-code 0 --redact`
   -> "no leaks found" (0 findings).

3. `.env` is not tracked: `git ls-files | grep -c '^\.env$'` -> 0. Secrets
   live outside the repo in ~/.secrets/ per AGENTS.md.

4. `.env.example` is tracked but contains no secret values (it is a template
   with placeholder lines only). A prior full-history scanner flagged a
   private-key-looking block at `.env.example` line 4 in commit
   `6867ff0919eff6e7e213054820e7cc0b264b44e6`. Operator confirmed on
   2026-11-22 that this is a known safe placeholder, recorded here as an
   intentional false positive. No rotation or git-history rewrite was
   performed.

5. VERTEX_PROJECT_ID (F-10) reads `os.environ.get("VERTEX_PROJECT_ID", "")`
   at config.py:865 - no hardcoded project default remains.

6. Named-path content check: regex scan over logs/, data/, data/cfb/ and
   paper_state_precutover_2026-11-13.json for api-key-like, account-id,
   balance-value and JWT patterns -> no hits in any target.

## Conclusion

No secret, key, token, account id or balance string is present in the tracked
tree or in the full git history. `VERTEX_PROJECT_ID` has no hardcoded default.
`.env` and `*.pem` are excluded from staging by the pre-commit hook
(`scripts/install-pre-commit.sh`). Remaining hygiene items are operational,
not findings: keep `.env`/`.pem` out of the index, keep API keys trade-only,
and re-run this scan after any history rewrite.

## Remaining human decisions

- Repo is PUBLIC: confirm intentionally whether it should stay public or be
  made private.
- The `.env.example` placeholder flag is recorded as safe by operator
  confirmation; no further action needed unless the placeholder is later
  replaced with real key material.
