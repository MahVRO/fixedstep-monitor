# Operations runbook

This runbook installs the monitor beside an already authenticated Windows MT5 terminal. It does not install, start, stop, or reconfigure the trading strategy.

## Prerequisites

- Windows with the existing MT5 terminal session available to the same interactive user.
- Python 3.11 or newer.
- Git.
- A checkout of the repository's `live` branch when automatic publication is enabled.
- A repository-scoped write deploy key or fine-grained credential for this repository only. Never copy a broad personal token to the trading host.

The public site is a static telemetry surface, not an access-control boundary. Anything published may be cached or archived.

## 1. Install and test without publishing

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"

.\.venv\Scripts\python.exe -m fixedstep_monitor collect `
  --database runtime\fixedstep-monitor.sqlite3 `
  --site-root docs `
  --symbol FixedStep0.5
```

Inspect `docs\ai\data\latest.json`. A successful source reports `collection.status` as `live`; a disconnected terminal reports `no_data` or `disconnected` and publishes no fabricated metrics.

Before enabling automatic collection, confirm all of the following:

1. The exact symbol is `FixedStep0.5`.
2. Account-wide metrics are acceptable for public release.
3. The trade scope in `latest.json` matches the intended strategy selector.
4. No broker identity, raw ticket, comment, path, endpoint, or credential appears under `docs\ai`.
5. A second run deduplicates closing executions and adds only one new account observation.

If the strategy uses a reviewed MT5 magic number, pass it with `-MagicNumber` to the installer. Without that evidence, do not guess one. Optional strategy step/state fields require a separately reviewed JSON status file.

## 2. Configure the rolling public branch

Automatic publication is intentionally limited to `docs\ai` on the dedicated `live` branch. Each update amends that branch's snapshot commit and pushes with `--force-with-lease`, preventing unbounded ten-minute commit history. The `main` branch remains the source history.

Configure the checkout to use its dedicated repository credential, then verify:

```powershell
git branch --show-current
git remote -v
git push --dry-run origin HEAD:live
```

The current branch must print `live`. The dry run must succeed without placing a credential in a file, command argument, scheduled-task argument, or log.

## 3. Install restart-resilient collection

Collection starts when the same Windows user logs in, survives collector failures, and is restarted by Task Scheduler after a process failure. This matches MT5's interactive terminal session model.

```powershell
.\scripts\install_scheduled_task.ps1 `
  -ProjectRoot $PWD.Path `
  -IntervalSeconds 60 `
  -PublishIntervalSeconds 600 `
  -Symbol FixedStep0.5 `
  -EnableGitPublish
```

Optional reviewed settings:

```powershell
.\scripts\install_scheduled_task.ps1 `
  -ProjectRoot $PWD.Path `
  -Symbol FixedStep0.5 `
  -MagicNumber 123456 `
  -InitialCapital 10000 `
  -StrategyStatePath 'D:\reviewed\fixedstep-state.json' `
  -EnableGitPublish
```

The example values above are placeholders for operator-supplied evidence; do not copy them into production unless they are authoritative.

## 4. Verify a running installation

```powershell
Get-ScheduledTask -TaskName 'FixedStep0.5 Read-Only Monitor'
Get-Content runtime\monitor.log -Tail 30
.\.venv\Scripts\python.exe -m fixedstep_monitor export `
  --database runtime\fixedstep-monitor.sqlite3 `
  --site-root docs
```

Check that:

- collection timestamps advance about once per minute;
- the public snapshot advances about once per ten minutes;
- `snapshot_id` matches across all five core JSON files;
- source and public freshness warnings behave independently;
- the GitHub Pages deployment reports `built` for the `live` branch;
- reconnecting after an outage retains history and changes status back to `live`;
- restarting the task does not duplicate closed execution IDs.

## Stopping and removing

Graceful stop:

```powershell
New-Item -ItemType File runtime\stop-monitor -Force
```

Before restarting, remove only that exact stop file, then start the scheduled task:

```powershell
Remove-Item -LiteralPath runtime\stop-monitor
Start-ScheduledTask -TaskName 'FixedStep0.5 Read-Only Monitor'
```

Remove the scheduled task:

```powershell
.\scripts\uninstall_scheduled_task.ps1
```

Removing the task does not delete SQLite history, logs, the repository, or public GitHub history.

## Backup and recovery

The authoritative monitor history is `runtime\fixedstep-monitor.sqlite3`, not the public branch. Stop the scheduled task or use SQLite's online backup facility before copying it. Keep backups private; the database contains raw broker identifiers that are deliberately excluded from public exports.

For recovery, restore the database to the configured runtime path, confirm its companion WAL transaction has been checkpointed, run `export`, and then start the scheduled task. A missing database creates a new empty monitor history; it does not affect MT5 or the trading strategy.

## Freshness and incident response

- Source stale threshold: 180 seconds by default.
- Public stale threshold: 1,200 seconds by default.
- Expected collection: 60 seconds.
- Expected publication: 600 seconds, plus Pages deployment/cache delay.

If an unexpected public field appears, stop the monitor, disable publication, rotate the repository credential if needed, and treat already published content as potentially archived. Do not stop or change the trading system to troubleshoot this independent monitor.
