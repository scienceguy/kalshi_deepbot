# T17-02 repository hygiene verification

Date: 2026-11-23
Pinned revision: e692c946c669ed688270969723a40e260e353591 (e692c94)
Overall result: PARTIAL

This was a read-only audit. No credentials, raw account IDs, or raw balance
values are recorded here. The shared HEAD advanced during the audit through
concurrent work; pinned Git content checks below use e692c94. The named local
artifacts and `verify_source.py --all` are working-tree checks. This agent did
not mutate Git, install tools, call venue endpoints, or edit source/config/tests.

## Acceptance results

| Acceptance point | Result | Evidence and remaining gap |
|---|---|---|
| Full-history scanner and report under `docs/` | PASS | Installed gitleaks 8.18.2 scanned the pinned history and all refs with zero findings; this report is the docs artifact. Trufflehog is not installed and was not run. This PASS is for gitleaks detection, not for every possible project ID or balance field. |
| Remove the runtime `VERTEX_PROJECT_ID` default | PASS | `config.py:865` at e692c94 reads `os.environ.get("VERTEX_PROJECT_ID", "")`. |
| No secret-looking defaults in tracked config/source | PARTIAL | The redacted default scan found a non-placeholder project-ID-shaped literal in tracked `.env.example:38` (30 characters, 12 digits). The value was not printed. A second literal is a generic test fixture in `test_env_var_wins_over_file`, not a runtime default. |
| Named paths contain no unpublishable balances/IDs/keys | PARTIAL | All 28 named local files are ignored and untracked. Gitleaks found zero leaks, and the redacted regex check found zero keys, account/project IDs, or JWTs. It found two balance-value fields in the ignored paper snapshot at lines 3 and 4. Values were not inspected or printed, so publication approval is unresolved. |
| Rotate anything found | PASS | No credential, key, or token finding requires rotation. A project identifier and paper balance fields are not rotatable credentials. |
| Decide intentionally whether the repo stays public | PASS | The operator decision is already recorded: "Operator Q2 (repo visibility) answered: repo is public by design for open-source bot; no action to flip to private." It is also in `docs/T16_HISTORICAL_RECONCILIATION.md:88`. No fresh visibility endpoint was called. |

T17-02 therefore remains PARTIAL, not DONE: the current tracked template value
and the two local paper balance fields still need explicit disposition.

## Commands and results

### Revision and scanner availability

```sh
git rev-parse HEAD
# e692c946c669ed688270969723a40e260e353591 (at audit start)

command -v gitleaks; gitleaks version
# /usr/local/bin/gitleaks
# 8.18.2
command -v trufflehog
# no output; NOT_INSTALLED
```

### Full history, with redacted reports outside the project

```sh
gitleaks detect --source . \
  --log-opts=e692c946c669ed688270969723a40e260e353591 \
  --report-format json \
  --report-path /tmp/opencode/t17_gitleaks_history_e692c94.json \
  --exit-code 0 --redact=100
# rc=0; 720 commits scanned; no leaks found; JSON finding_count=0; errors=0

gitleaks detect --source . --log-opts=--all \
  --report-format json \
  --report-path /tmp/opencode/t17_gitleaks_all_refs_e692c94.json \
  --exit-code 0 --redact=100
# rc=0; 721 commits scanned; no leaks found; JSON finding_count=0; errors=0
```

The second command was supplemental all-ref coverage at scan time. The first
command is the scan pinned exactly to the requested revision.

### Tracked defaults, values redacted

```sh
git show e692c946c669ed688270969723a40e260e353591:config.py | sed -n '865p'
# VERTEX_PROJECT_ID = os.environ.get("VERTEX_PROJECT_ID", "")

.venv/bin/python /tmp/opencode/t17_secret_default_scan.py
# HEAD=e692c946c669ed688270969723a40e260e353591
# findings=69
# shape_empty=22
# shape_placeholder=43
# shape_path_or_reference=2
# shape_literal=2
# REVIEW literal .env.example:38 VERTEX_PROJECT_ID length=30 digits=12
# REVIEW literal tests/test_n8n_client.py:1514 N8N_API_KEY length=9 digits=0
# values_printed=false
```

The temporary scanner reads Git blobs only, recognizes sensitive environment
names and literal defaults, and emits metadata rather than values. The test
literal was separately confirmed to use generic test words in
`test_env_var_wins_over_file`; it is not a config default.

### Named local paths, values redacted

```sh
git ls-files --others --exclude-standard -- \
  logs data paper_state_precutover_2026-11-13.json | wc -l
# 0 (nonignored untracked files)

git ls-files --others --ignored --exclude-standard -- \
  logs data paper_state_precutover_2026-11-13.json | wc -l
# 28 (all named local files are ignored)

.venv/bin/python /tmp/opencode/t17_named_path_scan.py
# files_scanned=28
# files_with_hits=1
# hits_jwt=0
# hits_private_key=0
# hits_known_api_key=0
# hits_secret_assignment=0
# hits_account_or_project_id=0
# hits_balance_value=2
# HIT paper_state_precutover_2026-11-13.json:3 category=balance_value
# HIT paper_state_precutover_2026-11-13.json:4 category=balance_value
# matched_values_printed=false
```

Separate `gitleaks detect --source TARGET --no-git --exit-code 0 --redact=100`
runs for `logs`, `data`, and the paper snapshot each returned `rc=0`,
`no leaks found`, zero JSON findings, and zero errors.

At e692c94, `git ls-tree -r --name-only` returned zero tracked files under the
named paths. `.env` and `*.pem` also have zero tracked entries at that revision.

### Public decision

```sh
mempalace search 'T17-02 public repo hygiene VERTEX_PROJECT_ID gitleaks operator repo public by design'
# Returned the verbatim operator decision quoted in the acceptance table.
```

`docs/SECRET_HYGIENE_2026-11-22.md:45-48` still lists visibility as a remaining
human decision, but the later reconciliation and MemPalace decision record show
that this question has already been answered.

### ASCII/source checks

```sh
git grep -n -I -P '[^\x00-\x7F]' HEAD -- '*.py'
# At execution HEAD was e692c94; rc=0, 1550 matching lines in 189 files.
# This broad grep includes comments and docstrings and is not itself a gate.

.venv/bin/python scripts/verify_source.py --all
# every scanned Python file reported "clean"; final output: "all clean"

grep -nP '[^\x00-\x7F]' docs/T17_REPO_HYGIENE.md
# rc=1; zero matches; this report is ASCII-only
```

## Smallest remaining actions

1. With edit permission, replace `.env.example:38` with an empty value or an
   unambiguous placeholder, then rerun the redacted default scan. If the
   existing project identifier must also be removed from public history, the
   operator must separately approve that history decision; no rewrite is
   justified automatically by this audit.
2. Confirm that the two balance fields in the ignored paper snapshot are
   approved for publication. If not, move or sanitize that local file without
   printing its values and keep it ignored/untracked. Record the decision.
3. Re-run the pinned/current gitleaks and named-path checks after those two
   dispositions. No credential rotation is indicated by the present findings.
