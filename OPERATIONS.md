# Operations runbook

This runbook operates the independent FixedStep0.5 quote monitor beside an already authenticated Windows MT5 terminal. It never starts, stops, configures, or controls a trading strategy.

## Runtime isolation

- Scheduled task: `FixedStep0.5 Quote Monitor`
- Database: `runtime\fixedstep-quotes.sqlite3`
- Live API snapshot: `runtime\quote-api\latest.json`
- Log: `runtime\quote-monitor.log`
- Graceful stop marker: `runtime\stop-quote-monitor`
- Loopback API: `http://127.0.0.1:8765`

These are deliberately distinct from the legacy account monitor. Its task can remain disabled and its `runtime\stop-monitor` marker can remain in place; neither affects the quote runtime.

The task uses the same interactive Windows user as the authenticated MT5
terminal and DPAPI-protected relay credential. It starts at user logon and has
an additional five-minute watchdog trigger. `IgnoreNew` leaves a healthy
long-running instance untouched; after a crash, the next watchdog tick can
start it again. A logged-on Windows session and an authenticated MT5 terminal
remain prerequisites after reboot.

## Install and verify without publication

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

.\scripts\install_scheduled_task.ps1 `
  -ProjectRoot $PWD.Path `
  -IntervalSeconds 10 `
  -PublishIntervalSeconds 600 `
  -Symbol FixedStep0.5

Start-ScheduledTask -TaskName 'FixedStep0.5 Quote Monitor'
```

Verify:

```powershell
Get-ScheduledTask -TaskName 'FixedStep0.5 Quote Monitor'
Get-Content runtime\quote-monitor.log -Tail 30
Invoke-RestMethod http://127.0.0.1:8765/v1/quotes
Invoke-RestMethod http://127.0.0.1:8765/v1/latest
```

Confirm that:

1. `schema_version` is `fixedstep-quotes-v1`.
2. `symbol` is exactly `FixedStep0.5`.
3. `collection.collected_at` and the snapshot ID advance about every 10 seconds, while `collection.collection_age_seconds` remains below the configured stale threshold.
4. `collector_clock_basis` is `utc_serialized_host_clock_synchronization_not_asserted`, and `chart_time_basis` is `collector_first_observation_host_clock`.
5. `quote.tick_at` is preserved separately with `tick_time_basis: mt5_reported_unverified`; a material `mt5_reported_offset_seconds` is reported as a clock diagnostic, never as tick freshness.
6. bid and ask match MT5, ask is not below bid, and broker `last=0.0` appears publicly as `null`.
7. generated files contain no account, P&L, position, order, deal, ticket, magic number, terminal path, broker endpoint, or credential.
8. `/v1/latest` remains a compact quote snapshot, while `/v1/quotes` remains below 3.5 MB and 18,000 public samples.

## Clock audit and safe handling

The collector deliberately treats the MT5-reported timestamp and the Windows host clock as separate, untrusted clock domains. Public retention, bucket selection, range buttons, and SVG/browser chart axes use the tick's collector `first_collected_at`; the raw MT5 value is retained only for audit and offset diagnostics. Freshness uses backend `collection_age_seconds` plus monotonic time since the browser received that exact snapshot. Never calculate freshness with browser wall time minus either source timestamp.

Check Windows clock quality read-only before trusting absolute timestamps:

```powershell
w32tm.exe /query /status
w32tm.exe /stripchart /computer:time.windows.com /samples:3 /dataonly
```

The 2026-10-11 deployment audit found MINIPC on `Local CMOS Clock`, with no successful synchronization and approximately 77.235 seconds slow; raw FixedStep0.5 MT5 tick time tracked a separate clock approximately three hours ahead of an external UTC observation. These measurements explain the explicit warnings but must not be hard-coded as corrections: broker offsets and DST can change. Do not resynchronize Windows while trading under this runbook, because a system-clock step may affect MT5 or the strategy; report it as a manual maintenance action requiring separate authorization and an appropriate trading-safe window.

## Enable the static GitHub Pages fallback

Use a repository-scoped deploy key on the dedicated `live` checkout. Verify a non-interactive dry run first, then reinstall the task with publication enabled:

```powershell
git branch --show-current
git push --dry-run origin HEAD:live

.\scripts\install_scheduled_task.ps1 `
  -ProjectRoot $PWD.Path `
  -PythonExe 'C:\path\to\python.exe' `
  -IntervalSeconds 10 `
  -PublishIntervalSeconds 600 `
  -Symbol FixedStep0.5 `
  -EnableGitPublish
```

`runtime\quote-api\latest.json` and the local `/v1/latest` response update every 10 seconds. Bounded history JSON/CSV, the static `docs\ai\data\latest.json`, static SVGs, the no-JS page, and the manifest rebuild in one background worker after the first collection and then every 600 seconds. That worker owns the entire `docs\ai` bundle and all Git activity; it never writes the runtime live file. Slow rendering, Git staging, commit, or network work therefore cannot pause or regress `/v1/latest`; repeated static triggers coalesce rather than overlap. Pages build and cache propagation add variable delay and are not represented as 10-second public freshness.

## Enable the outbound real-time relay

Provision the Worker and D1 database from `cloudflare\worker` as described in its README. Store the ingest token outside the checkout using Windows DPAPI, then reinstall the same task:

```powershell
$secret = Read-Host 'Relay token' -AsSecureString
.\scripts\save_realtime_secret.ps1 `
  -Secret $secret `
  -Destination "$env:LOCALAPPDATA\FixedStepMonitor\realtime-token.dpapi"

.\scripts\install_scheduled_task.ps1 `
  -ProjectRoot $PWD.Path `
  -PythonExe 'C:\path\to\python.exe' `
  -IntervalSeconds 10 `
  -PublishIntervalSeconds 600 `
  -Symbol FixedStep0.5 `
  -EnableGitPublish `
  -RealtimeIngestUrl 'https://WORKER.workers.dev/v1/ingest' `
  -RealtimeTokenPath "$env:LOCALAPPDATA\FixedStepMonitor\realtime-token.dpapi"
```

The task definition contains the Worker URL and protected-file path, never the token. A slow or failed relay request is isolated on a latest-wins worker: local SQLite, the loopback snapshot and the 600-second GitHub fallback continue. When collection stops or disconnects, no old quote is re-posted; the Worker ages its last receipt to `stale`.

The background history query performs the 24-hour/full, 7-day/minute, and 90-day/hour selection against collector first-observation host time in SQLite before JSON decoding. Raw MT5 time is not used for those boundaries or buckets. The full retained-row count is a separate `COUNT(*)`; old local rows are not loaded merely to generate public files.

At startup, the API is given two exact regular files: `runtime\quote-api\latest.json` for `/v1/latest` and `docs\ai\data\quotes.json` for `/v1/quotes`. The paths cannot alias each other, and a missing, replaced-by-directory, or symlinked target fails closed instead of exposing another file. On every live request, the API also checks `generated_at` against its same-host clock and returns 503 if the writer has frozen past `source_stale_after_seconds`; this prevents a page reload from presenting an old file as newly live.

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

A missing database creates a new empty quote history and does not affect MT5. A disconnected read preserves the last good public quote while marking the collector disconnected; it never substitutes sample data. If an older mixed UTF-16/UTF-8 runtime log is detected, the runner preserves it as `quote-monitor.log.mixed-encoding` and starts a new consistently UTF-8 log; non-JSON native diagnostics are redacted rather than copied verbatim.

## Incident response

If an unexpected public field appears, stop the quote task, disable the HTTPS exposure and Git publication, and rotate the repository deploy key if relevant. Treat already published material as potentially cached. Do not stop or change MT5 or another service to troubleshoot this monitor.
