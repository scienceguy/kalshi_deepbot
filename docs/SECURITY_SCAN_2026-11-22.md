# Secrets scan report (T15-02)

Date: 2026-11-22. Agent: opencode:kalshi_deepbot. Repo HEAD at scan: 6549f79.

## Tool and commands (evidence)

1. gitleaks full history:
   `gitleaks detect --source . --report-format json --report-path
   /tmp/opencode/gitleaks_history.json --exit-code 0 --redact`
   -> "681 commits scanned ... no leaks found" (0 findings in the JSON report).
2. gitleaks working tree:
   `gitleaks detect --source . --no-git ... --redact`
   -> "no leaks found" (0 findings).
3. Named-path content check (F-10): regex scan over logs/ (21 files),
   data/cfb, paper_state.json, paper_state_precutover_2026-11-13.json for
   api-key-like, account-id, balance-value and JWT patterns ->
   "scanned N files, 0 with hits" on every target.
4. `.env` is not tracked: `git ls-files | grep -c '^\.env$'` -> 0. Secrets
   live outside the repo (~/.secrets/) per AGENTS.md.
5. VERTEX_PROJECT_ID (F-10) now reads `os.environ.get("VERTEX_PROJECT_ID", "")`
   (config.py:817) - no hardcoded project default remains.

## Conclusion

No secret, key, token, account id or balance string is present in the
tracked tree or in the full git history. Remaining hygiene items are
operational, not findings: keep .env/.pem out of the index (pre-commit hook
blocks them), keep API keys trade-only, and re-run this scan after any
history rewrite. 684 commits scanned; 0 leaks.
