# VPS runbook — Polymarket US paper engine

Canonical runtime. The Termux/Android checkout is development only; never run a
trader loop there (it would share `local_data/` names with the VPS but not the
state).

| Item | Value |
|---|---|
| Host | `<vps-host>` (SSH user `<vps-user>`, key `<ssh-key>`) |
| Code | `/opt/superbot` (git checkout of `origin/main`) |
| Interpreter | `/opt/superbot/.venv/bin/python` |
| Unit | `poly-us-dry.service` (`deploy/poly-us-dry.service`) |
| State | `/opt/superbot/local_data/poly_us_state.json` |
| Ledger | `/opt/superbot/local_data/poly_us_paper_ledger.jsonl` |
| Logs | `/opt/superbot/local_data/poly_us_dry.log` + journald |
| Lock | `/opt/superbot/.lock_poly` (one instance only) |

## Redeploy

Real host/user/key values are kept in private notes, never in this repository.

```bash
ssh -i <ssh-key> <vps-user>@<vps-host>
cd /opt/superbot
git fetch origin main && git pull --ff-only origin main
.venv/bin/python -m pip install -r requirements.txt      # only if deps changed
.venv/bin/python -m pytest tests/test_poly_us_trader.py tests/test_polymarket_us_client.py -q
sudo systemctl restart poly-us-dry.service
```

## Verify (all four must hold)

```bash
systemctl is-active poly-us-dry.service      # active
systemctl is-enabled poly-us-dry.service     # enabled
systemctl show -p MainPID --value poly-us-dry.service
ls -l /opt/superbot/.lock_poly               # exists, held
journalctl -u poly-us-dry.service -n 30 --no-pager   # a cycle, no Traceback
```

A `Type=notify` unit is only `active` after the engine sends `READY=1`; if it
sits in `activating`, init failed — read journald.

If the worktree has local edits, do **not** `git checkout -- .`
(see AGENTS.md). Back them up first:
`git diff > ~/backups/uncommitted-$(date +%s).patch && git stash push -m "<why>"`.

## Rollback

```bash
cd /opt/superbot
git log --oneline -5                      # pick the previous good SHA
git checkout <sha>
sudo systemctl restart poly-us-dry.service
```

## Unit / watchdog

The unit runs `Type=notify` with `WatchdogSec=120`. The engine pings
`WATCHDOG=1` after every cycle (~15–20 s) **and** from its sleep helper at least
every 30 s while backing off after failures (A10), so a long venue-outage
backoff no longer gets killed and restarted mid-sleep; a genuinely wedged cycle
still trips the watchdog.

The service runs as the dedicated unprivileged user `polybot` with
`NoNewPrivileges=true`, `ProtectSystem=strict`, writes confined to
`local_data/` and `.lock_poly`, `PrivateTmp=true` and `MemoryMax=1G`. Create the
account and restricted `.env` once:

```bash
sudo useradd --system --home /opt/superbot --shell /usr/sbin/nologin polybot
sudo chown -R polybot:polybot /opt/superbot/local_data
sudo install -m 600 -o polybot -g polybot /opt/superbot/.env /opt/superbot/.env
```

Reinstall after editing:

```bash
sudo cp /opt/superbot/deploy/poly-us-dry.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart poly-us-dry.service
```

If SSH exposure is a concern, restrict it to key-only auth, a firewall
allowlist, and rotate the key.

## Log rotation

```bash
sudo cp /opt/superbot/deploy/superbot.logrotate /etc/logrotate.d/superbot
sudo logrotate -d /etc/logrotate.d/superbot     # dry run
```

`copytruncate` is deliberate: the engine keeps its log/ledger files open.

## State backup

```bash
chmod +x /opt/superbot/deploy/state-backup.sh
crontab -e   # add: 15 3 * * * /opt/superbot/deploy/state-backup.sh
```

Snapshots land in `~/backups/state/` (7 kept).

## Modes

Paper by default. Live requires **both** `POLY_CONFIRM_LIVE=true` in `.env` and
`--live` on the command line; without the env var the engine exits 2.
