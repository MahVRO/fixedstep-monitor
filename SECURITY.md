# Security model

This project is a one-way, read-only telemetry exporter. It intentionally has no HTTP server, inbound control channel, webhook receiver, or trading action.

## Hard boundaries

- The MT5 adapter uses an explicit read-call allowlist. Order placement, order checking, modification, cancellation, and position closing are forbidden.
- The collector writes only to its own SQLite database and generated public-output directory.
- Public export uses an allowlist. Broker login/account number, broker server, credentials, tokens, machine names, IP addresses, local paths, raw comments, and raw configuration are not exported.
- The private SQLite database, logs, local configuration, and credentials are excluded from Git.
- Missing evidence is omitted and recorded as an omission; it is never converted to zero.
- Strategy-specific metrics require an explicit strategy selector, such as a reviewed MT5 magic number, and explicit state fields require a reviewed state file. Account-wide data is labelled as account scope.

## Publishing credential

Do not copy a broad personal GitHub token to the trading host. Use a repository-scoped, fine-grained credential or a write-enabled deploy key dedicated to `MahVRO/fixedstep-monitor`. Store it in Windows Credential Manager or the SSH agent, never in this repository, `config.json`, task arguments, or logs.

## Public-data warning

Everything committed to the public snapshot branch and served by GitHub Pages can be copied, cached, indexed, or archived. Removing a later snapshot cannot guarantee deletion of an earlier public copy.

## Reporting a problem

Stop the independent collector and disable publication if an unexpected field appears. Stopping this monitor must not require stopping or changing any trading system.
