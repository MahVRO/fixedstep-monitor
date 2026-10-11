# FixedStep0.5 quote monitor

A quote-only, read-only MT5 monitor and chart for the exact `FixedStep0.5` symbol.

- Dashboard: <https://mahvro.github.io/fixedstep-monitor/dashboard/>
- JavaScript-free data view: <https://mahvro.github.io/fixedstep-monitor/ai/>
- Static quote JSON: <https://mahvro.github.io/fixedstep-monitor/ai/data/quotes.json>
- Dataset manifest: <https://mahvro.github.io/fixedstep-monitor/ai/manifest.json>

The active runtime samples the latest MT5 bid and ask every 15 seconds. It does not read or publish account state, P&L, positions, orders, deals, strategy state, or broker identity. Missing values remain `null` or unavailable; they are never replaced with zero.

## Safety boundary

`QuoteOnlyMT5` has a dedicated allowlist containing exactly:

- `initialize`
- `shutdown`
- `terminal_info`
- `symbol_info`
- `symbol_info_tick`

There is no account call and no trading action. The exact symbol is checked rather than substituted. The broker currently reports `last=0.0` when Last is unavailable for this positive-price symbol, so the public contract emits `last: null`; bid, ask, mid, and spread remain the authoritative displayed values.

```text
Existing authenticated MT5 terminal
              │ quote-only reads every 15s
              ▼
Local SQLite quote history (WAL, deduplicated by broker tick)
              ├────────► runtime/quote-api/latest.json
              │          └─► loopback /v1/latest (live data)
              │
              └────────► background static worker
                         ├─► docs/ai/data/quotes.json
                         │    └─► loopback /v1/quotes
                         └─► CSV / SVG / no-JS HTML
              │ rolling fallback snapshot every 600s
              ▼
GitHub Pages
```

The loopback API may be exposed by a separately configured, narrowly scoped HTTPS proxy. `/v1/latest` serves the dedicated runtime snapshot and `/v1/quotes` serves the bounded static history file by exact configured paths. It does not perform a generic file lookup and does not expose SQLite, logs, source files, MT5, or an action endpoint.

## Public contract

The `fixedstep-quotes-v1` contract contains:

- exact symbol and UTC-serialized collector generation time;
- collector status, self-relative `collection_age_seconds`, and `collected_at`;
- raw MT5-reported `tick_at`, bid, ask, optional Last, mid, spread, point metadata;
- coverage and sampled quote history.

`collected_at` and `tick_at` are intentionally separate clock domains. `tick_at` is preserved exactly as reported by MT5 with `tick_time_basis: mt5_reported_unverified`; it is never offset-corrected and never controls freshness, retention, chart ranges, or axes. Public chart samples use the tick's immutable `first_collected_at`, exposed as `collected_at`. Collector timestamps are UTC-serialized host-clock readings, but clock synchronization and absolute accuracy are explicitly not asserted.

Freshness uses the backend's same-host `collection_age_seconds`, then adds monotonic time elapsed in the browser since that exact snapshot was received. In addition, `/v1/latest` compares the file's `generated_at` with the API process on that same host clock and fails closed with HTTP 503 once the writer's stale threshold is exceeded. A newly opened browser therefore cannot relabel a frozen snapshot as live. It never subtracts an MT5 or MINIPC timestamp from the browser clock. A material MT5-versus-collector difference is published as diagnostic offset metadata and shown as a clock warning, not as a stale or recent-tick claim.

Static endpoints:

- `/ai/data/latest.json`
- `/ai/data/quotes.json`
- `/ai/data/quotes.csv`
- `/ai/charts/fixedstep-price.svg`
- `/ai/charts/fixedstep-spread.svg`
- `/ai/manifest.json`

The live `runtime/quote-api/latest.json` is deliberately small and carries no history rows, so the 15-second poll does not repeatedly download the chart history. That fast path performs only constant-size database lookups and one atomic file replacement. The static `docs/ai/data/latest.json` is part of the coherent fallback bundle and therefore refreshes on the static cadence. `quotes.json` carries the bounded history and is capped at 18,000 rows and 3.5 MB, below the live API's 4 MiB response limit.

The local SQLite database retains every distinct sampled broker tick. Every 600 seconds, a background worker asks SQLite for only the rows needed publicly: full source cadence for 24 hours, the last observed tick per collector first-observation host-clock minute through day 7, and the last observed tick per collector first-observation host-clock hour through day 90. Older observations remain local-only. No missing interval is interpolated. The raw MT5 clock remains available for audit but does not choose the rows.

History selection, JSON/CSV/SVG rendering, local Git staging and any network push all run outside the 15-second collector thread. One worker permits only one active job and one coalesced trailing job. The worker exclusively owns `docs/ai`; the collector exclusively owns `runtime/quote-api/latest.json`. There is no lock shared between them, so a blocked render, Git stage, commit, or push cannot delay the live snapshot or replace it with an older quote.

## Local validation

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mt5]"
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Run the quote runtime locally:

```powershell
.\.venv\Scripts\python.exe -m fixedstep_monitor quote-live `
  --database runtime\fixedstep-quotes.sqlite3 `
  --site-root docs `
  --symbol FixedStep0.5 `
  --interval 15 `
  --api-data-dir runtime\quote-api `
  --api-host 127.0.0.1 `
  --api-port 8765
```

The live snapshot is then available at `http://127.0.0.1:8765/v1/latest`; bounded chart history is at `http://127.0.0.1:8765/v1/quotes`. Git publication remains opt-in and is bounded to one fallback attempt every 600 seconds or slower. On startup, local files are seeded first; after the first MT5 collection attempt, the background worker rebuilds the first publishable fallback so a real quote is included when available.

See [OPERATIONS.md](OPERATIONS.md) for deployment and recovery, and [SECURITY.md](SECURITY.md) for the public-data and inbound-API model.
