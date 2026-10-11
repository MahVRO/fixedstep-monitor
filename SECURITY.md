# Security model

The active runtime is a one-way FixedStep0.5 market-quote reader. It has no account reader, strategy control, trade execution, webhook, mutation route, or general-purpose file server.

## MT5 boundary

The dedicated `QuoteOnlyMT5` allowlist is exactly `initialize`, `shutdown`, `terminal_info`, `symbol_info`, and `symbol_info_tick`. It does not call account, position, order, deal-history, symbol-selection, or execution APIs. The exact `FixedStep0.5` symbol must exist; no substitute is accepted.

Legacy account-monitor modules remain in the repository for historical test compatibility, but no CLI command exposes them. The executable requires an explicit supported subcommand and the installed task invokes only `quote-live`, using a separate database and stop marker.

## Public-data boundary

Public values are constructed from an explicit allowlist: UTC-serialized collector host-clock readings, raw MT5-reported tick timestamps, same-host collection age, the material clock-offset diagnostic, bid, ask, optional positive Last, derived mid and spread, and non-sensitive symbol precision metadata. `last=0.0` is treated as unavailable. Missing and non-finite values are omitted or `null`, never converted into observations.

The raw MT5 timestamp has an explicitly unverified time basis. It is preserved rather than corrected and is never used for freshness, retention, buckets, or chart axes. Collector host timestamps likewise do not assert that Windows is synchronized; live freshness is derived from the backend's same-clock age plus browser-monotonic time since receipt. The monitor never changes the Windows, MT5, or broker clock.

No account balance, equity, margin, P&L, currency, login, broker server, position, trade, order, ticket, magic number, comment, machine name, path, IP address, token, or credential belongs in `docs\ai` or the live response.

## Local API boundary

The Python server binds only to a loopback address and exposes bounded JSON from two exact, separately owned generated files:

- `GET`/`HEAD /v1/quotes` from `docs\ai\data\quotes.json`
- `GET`/`HEAD /v1/latest` from `runtime\quote-api\latest.json`
- restricted CORS preflight where supported

The paths are resolved and validated as distinct regular files, and requests never influence path selection. Symlinks, missing files, directories, and oversized payloads fail closed. The live route also validates the snapshot's generation time against the API process on the same host clock and returns 503 if the writer has stopped beyond its advertised stale threshold, moved into the future, or produced a malformed contract. It has no query-driven file lookup, directory listing, write method, credentials, cookies, or control action. It remains bound to loopback and is not required for public access.

## Outbound relay boundary

The MINIPC initiates an HTTPS `POST` to one exact `/v1/ingest` URL. The sender rebuilds a compact object from a quote-only allowlist and follows no redirects. It sends only a fresh `live` observation; a source failure cannot refresh an old quote. Network I/O runs on a one-active/one-replaceable-pending worker and therefore cannot block MT5 sampling or create a catch-up burst.

The Cloudflare Worker independently enforces the exact symbol, schema, 10-second collection declaration, finite internally consistent quote values, a 16 KiB body limit, rate limits and recursive sensitive-field rejection. Only `GET`/`HEAD` quote routes are public. Browser CORS is restricted to `https://mahvro.github.io`, credentials are not allowed, and responses use `no-store`. Freshness is the received same-host collection age plus elapsed Worker time; it never compares the MINIPC, MT5, Cloudflare or browser clocks directly.

## Publishing credentials

Use only a write-enabled deploy key dedicated to `MahVRO/fixedstep-monitor`. The relay bearer token is a Wrangler secret at Cloudflare and a current-user DPAPI-protected file outside the checkout on Windows. Never place either credential in the repository, command arguments, task definition, generated JSON, URLs, or logs.

Publication runs in background workers and never on the quote-collection thread. Triggers coalesce, Git is non-interactive and time-bounded, relay requests are timeout-bounded, and diagnostics redact credentials and local paths. The static worker exclusively owns the `docs\ai` bundle and refuses to commit unrelated staged paths. The collector's runtime live file is outside that tree and shares no lock with rendering, relay I/O or Git, so publication cannot block or overwrite the 10-second snapshot.

## Public-data warning

Anything exposed by the live endpoint or committed to the public snapshot branch can be copied, cached, indexed, or archived. Removing a later snapshot cannot guarantee deletion of an earlier public response.
