# Operations runbook

This runbook operates the independent FixedStep0.5 quote monitor beside an already authenticated Windows MT5 terminal. It never starts, stops, configures, or controls a trading strategy.

## Runtime isolation

- Scheduled task: `FixedStep0.5 Quote Monitor`
- Database: `runtime\fixedstep-quotes.sqlite3`
- Log: `runtime\quote-monitor.log`
- Graceful stop marker: `runtime\stop-quote-monitor`
- Loopback API: `http://127.0.0.1:8765`

These are deliberately distinct from the legacy account monitor. Its task can remain disabled and its `runtime\stop-monitor` marker can remain in place; neither affects the quote runtime.

## Install and verify without publication

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

.\scripts\install_scheduled_task.ps1 `
  -ProjectRoot $PWD.Path `
  -IntervalSeconds 15 `
  -PublishIntervalSeconds 600 `
  -Symbol FixedStep0.5

Start-ScheduledTask -TaskName 'FixedStep0.5 Quote Monitor'
```

Verify:

```powershell
Get-ScheduledTask -TaskName 'FixedStep0.5 Quote Monitor'
Get-Content runtime\quote-monitor.log -Tail 30
Invoke-RestMethod http://127.0.0.1:8765/v1/quotes
```

Confirm that:

1. `schema_version` is `fixedstep-quotes-v1`.
2. `symbol` is exactly `FixedStep0.5`.
3. `collection.collected_at` advances about every 15 seconds.
4. `quote.tick_at` is reported separately and may remain unchanged when the market is closed.
5. bid and ask match MT5, ask is not below bid, and broker `last=0.0` appears publicly as `null`.
6. generated files contain no account, P&L, position, order, deal, ticket, magic number, terminal path, broker endpoint, or credential.
7. `/v1/latest` remains a compact quote snapshot, while `/v1/quotes` remains below 3.5 MB and 18,000 public samples.

## Enable the static GitHub Pages fallback

Use a repository-scoped deploy key on the dedicated `live` checkout. Verify a non-interactive dry run first, then reinstall the task with publication enabled:

```powershell
git branch --show-current
git push --dry-run origin HEAD:live

.\scripts\install_scheduled_task.ps1 `
  -ProjectRoot $PWD.Path `
  -PythonExe 'C:\path\to\python.exe' `
  -IntervalSeconds 15 `
  -PublishIntervalSeconds 600 `
  -Symbol FixedStep0.5 `
  -EnableGitPublish
```

`docs\ai\data\latest.json` and the live `/v1/latest` response update every 15 seconds. Bounded history JSON/CSV, static SVGs, the no-JS page, and the manifest rebuild in one background worker after the first collection and then every 600 seconds. The same worker stages, commits, and pushes when publication is enabled, so slow Git/network work cannot pause quote sampling; repeated triggers coalesce rather than overlap. Pages build and cache propagation add variable delay and are not represented as 15-second public freshness.

The background history query performs the 24-hour/full, 7-day/minute, and 90-day/hour selection in SQLite before JSON decoding. The full retained-row count is a separate `COUNT(*)`; old local rows are not loaded merely to generate public files.

## Stop, restart, and remove

Graceful stop:

```powershell
New-Item -ItemType File runtime\stop-quote-monitor -Force
```

Before restart, remove only the quote stop marker:

```powershell
Remove-Item -LiteralPath runtime\stop-quote-monitor
Start-ScheduledTask -TaskName 'FixedStep0.5 Quote Monitor'
```

Remove the task without deleting data:

```powershell
.\scripts\uninstall_scheduled_task.ps1
```

## Backup and recovery

The local database is authoritative. Stop the quote task or use SQLite's online backup facility before copying `runtime\fixedstep-quotes.sqlite3`. Restoring it preserves deduplication because samples are keyed by exact symbol and broker tick timestamp.

A missing database creates a new empty quote history and does not affect MT5. A disconnected read preserves the last good public quote while marking the collector disconnected; it never substitutes sample data.

## Incident response

If an unexpected public field appears, stop the quote task, disable the HTTPS exposure and Git publication, and rotate the repository deploy key if relevant. Treat already published material as potentially cached. Do not stop or change MT5 or another service to troubleshoot this monitor.
