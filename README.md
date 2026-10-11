# FixedStep0.5 live monitor

A read-only MT5 telemetry collector and static public dashboard for FixedStep0.5.

- Human dashboard: <https://mahvro.github.io/fixedstep-monitor/dashboard/>
- JavaScript-free AI view: <https://mahvro.github.io/fixedstep-monitor/ai/>
- Latest JSON: <https://mahvro.github.io/fixedstep-monitor/ai/data/latest.json>
- Dataset manifest: <https://mahvro.github.io/fixedstep-monitor/ai/manifest.json>

The public site never invents missing trading values. Until a verified collector run is available, it deliberately displays **source unavailable** and omits account and performance numbers.

## Safety boundary

The monitor has no trade-control feature. Its MT5 adapter allows only terminal/account/position/history information calls. Order placement, modification, cancellation, and closing are absent. It writes to its own SQLite database and public-output directory; it does not modify the strategy, MT5 configuration, or any existing research database.

```text
Existing authenticated MT5 terminal
              │ read-only information calls every 30–60s
              ▼
Private SQLite history (full fidelity, WAL)
              │ allowlist + sanitizer
              ▼
Static JSON / CSV / SVG / HTML
              │ rolling snapshot every 10 minutes
              ▼
GitHub Pages
```

Account observations are labelled account-wide. Closed-record statistics are labelled with their actual filter scope and grain. The default closed-record grain is one MT5 closing execution/deal, so partial closes can create multiple rows; it is not silently presented as one complete position lifecycle.

## Public data

Stable endpoints include:

- `/ai/data/latest.json`
- `/ai/data/trades.json` and `/ai/data/trades.csv`
- `/ai/data/equity.json` and `/ai/data/equity.csv`
- `/ai/data/statistics.json`
- `/ai/data/strategy.json`
- `/ai/manifest.json`
- `/ai/charts/*.svg`

Every JSON snapshot includes a schema version, shared snapshot ID, UTC generation time, scope, coverage, omissions, and freshness metadata. The manifest includes absolute URLs, row counts, fields, byte sizes, and SHA-256 hashes.

The private SQLite history remains full fidelity. To bound public file size without concealing gaps, equity observations are published at source cadence for seven days, then as the last observed sample per UTC hour through day 90, and as the last observed sample per UTC day thereafter. No values are interpolated.

## Local validation

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

One read-only collection and export:

```powershell
.\.venv\Scripts\python.exe -m fixedstep_monitor collect `
  --database runtime\fixedstep-monitor.sqlite3 `
  --site-root docs `
  --symbol FixedStep0.5
```

The symbol selector is exact. Add `--magic <reviewed-number>` only after the strategy's authoritative magic number is known. Add `--strategy-state <reviewed-json-file>` only when that file is explicitly produced by the strategy; step and internal state are never inferred from trades.

See [OPERATIONS.md](OPERATIONS.md) for installation, publication, freshness, restart, backup, and recovery procedures. See [SECURITY.md](SECURITY.md) for the public-data and credential model.

## Hosting cadence

The collector target is 60 seconds. The default public push target is 10 minutes, followed by GitHub Pages deployment and cache propagation. The dashboard reports source freshness and public-snapshot freshness separately; it does not claim 30–60 second public freshness on Pages.
