# FixedStep0.5 quote relay

This Cloudflare Worker accepts one narrowly defined, read-only `FixedStep0.5`
quote snapshot from the MINIPC and exposes recent quote data to the GitHub Pages
dashboard. It stores no balance, equity, account, position, order, deal, volume,
commission, swap, profit, loss, or P&L field.

The relay is an outbound-only replacement for the public Tailscale Funnel path:

```text
MINIPC collector -- HTTPS POST every 10 s --> Worker + D1
GitHub Pages     -- HTTPS GET every 10 s  --> Worker + D1
```

The static GitHub Pages export remains an independent fallback.

## Routes

- `POST /v1/ingest` requires `Authorization: Bearer <INGEST_TOKEN>`.
- `GET /v1/latest` returns the newest quote and request-time freshness.
- `GET /v1/quotes?limit=360` returns 1–360 observations in ascending order.
- `GET /healthz` checks that the D1 binding is reachable.

Only the exact browser origin `https://mahvro.github.io` receives a CORS allow
header. Responses use `Cache-Control: no-store`.

## Ingest contract

The JSON body must be no larger than 16 KiB and must have exactly this shape.
Extra fields are rejected. The sender must derive this object from an allowlist;
it must not forward the complete local export.

```json
{
  "schema_version": "fixedstep-quotes-v1",
  "snapshot_id": "00000000-0000-4000-8000-000000000001",
  "generated_at": "2026-10-11T11:59:59.000Z",
  "symbol": "FixedStep0.5",
  "collection": {
    "status": "live",
    "connected": true,
    "expected_collection_interval_seconds": 10,
    "source_stale_after_seconds": 30,
    "collected_at": "2026-10-11T11:59:58.000Z",
    "collection_age_seconds": 2
  },
  "quote": {
    "collected_at": "2026-10-11T11:59:58.500Z",
    "tick_at": "2026-10-11T14:59:58.500Z",
    "bid": 5950.1,
    "ask": 5950.6,
    "last": null,
    "mid": 5950.35,
    "spread": 0.5,
    "spread_points": 5,
    "digits": 1,
    "point": 0.1,
    "trade_tick_size": 0.1,
    "tick_time_basis": "mt5_reported_unverified",
    "mt5_reported_offset_seconds": 10800,
    "mt5_reported_offset_material": true
  }
}
```

The token is compared through SHA-256 digests. Snapshot IDs are unique and
idempotent; reusing an ID with different content is rejected. The Worker rebuilds
the stored row from explicitly allowed fields and recursively rejects sensitive
field names before schema validation.

Freshness does not compare the MINIPC clock with the Cloudflare clock. D1 stores
the Worker's receipt time. Every read reports:

```text
effective age = incoming same-host collection age + elapsed Worker time
```

This keeps stale detection valid even though synchronization of the MINIPC and
raw MT5 clocks is not asserted.

## Provision and deploy

Prerequisites are a Cloudflare account with Workers/D1 enabled and a local
Wrangler login. No Cloudflare credential or ingest token belongs in Git.

1. From this directory, run `npx wrangler login`.
2. If reprovisioning, create two distinct databases with
   `npx wrangler d1 create fixedstep-quotes` and
   `npx wrangler d1 create fixedstep-quotes-preview`.
3. Put the production ID in `database_id` and the different preview ID in
   `preview_database_id`. The checked-in values identify the already-provisioned
   production and preview databases; replace both only when intentionally
   reprovisioning.
4. Apply the schema to production with
   `npx wrangler d1 migrations apply QUOTES_DB --remote`, then to preview with
   `npx wrangler d1 migrations apply QUOTES_DB --remote --preview`.
5. Generate a random token of 32-256 visible ASCII characters and store it only through
   `npx wrangler secret put INGEST_TOKEN`.
6. Deploy with `npx wrangler deploy`.
7. Put the same token in the MINIPC's protected local secret store; never place
   it in the repository, a command-line argument, a scheduled-task definition,
   a URL, or logs.

Never reuse the production ID as the preview ID: remote development must not
write to or prune production quote history.

## Local checks

Run the dependency-free unit suite with:

```text
npm test
```

The suite covers strict validation, recursive sensitive-field rejection,
authentication, body limits, rate limits, idempotency, conflict handling,
request-time freshness, exact CORS, bounded history, and 24-hour pruning.

## Storage and traffic bounds

- Authenticated ingestion is limited to 30 requests per minute in D1, with an
  additional best-effort per-client edge bound.
- Public reads have separate best-effort per-client bounds: 30 latest reads,
  four history reads, and 12 health checks per minute. These counters are
  isolated from authenticated ingestion so a normal collector cannot throttle
  a dashboard sharing the same public IP.
- History responses contain at most 360 points and 3.5 MB. At the 10-second
  source cadence this is one hour; the compact 90-day archive remains on
  GitHub Pages.
- Rows older than 24 hours are pruned after every newly accepted snapshot. The
  compact 90-day public archive remains in the independent GitHub Pages bundle.
- A new quote every 10 seconds is six ingests per minute, comfortably below the
  abuse bound.
