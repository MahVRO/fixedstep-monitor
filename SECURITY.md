# Security model

The active runtime is a one-way FixedStep0.5 market-quote reader. It has no account reader, strategy control, trade execution, webhook, mutation route, or general-purpose file server.

## MT5 boundary

The dedicated `QuoteOnlyMT5` allowlist is exactly `initialize`, `shutdown`, `terminal_info`, `symbol_info`, and `symbol_info_tick`. It does not call account, position, order, deal-history, symbol-selection, or execution APIs. The exact `FixedStep0.5` symbol must exist; no substitute is accepted.

The legacy account-monitor source remains in the repository for historical compatibility, but the installed quote task does not instantiate or schedule it and uses a separate database and stop marker.

## Public-data boundary

Public values are constructed from an explicit allowlist: UTC collection and tick times, bid, ask, optional positive Last, derived mid and spread, and non-sensitive symbol precision metadata. `last=0.0` is treated as unavailable. Missing and non-finite values are omitted or `null`, never converted into observations.

No account balance, equity, margin, P&L, currency, login, broker server, position, trade, order, ticket, magic number, comment, machine name, path, IP address, token, or credential belongs in `docs\ai` or the live response.

## Live API boundary

The Python server binds only to a loopback address and exposes bounded JSON from generated files:

- `GET`/`HEAD /v1/quotes`
- `GET`/`HEAD /v1/latest`
- restricted CORS preflight where supported

It has no query-driven file lookup, directory listing, write method, credentials, cookies, or control action. Responses use `Cache-Control: no-store`; CORS is restricted to the configured dashboard origin and does not allow credentials. Any public HTTPS proxy must forward only to this loopback service and must not expose MT5, SQLite, logs, source files, or other MINIPC services.

## Publishing credential

Use only a write-enabled deploy key dedicated to `MahVRO/fixedstep-monitor`. Never place a personal token, private key, or credential in the repository, command arguments, task definition, generated JSON, or logs.

Publication runs in one background worker and never on the quote-collection thread. Triggers coalesce, Git is non-interactive and time-bounded, and diagnostics redact credentials and local paths. The worker stages the atomic public bundle under a short lock, refuses to commit unrelated staged paths, then commits that exact index snapshot and releases the lock before any network push.

## Public-data warning

Anything exposed by the live endpoint or committed to the public snapshot branch can be copied, cached, indexed, or archived. Removing a later snapshot cannot guarantee deletion of an earlier public response.
