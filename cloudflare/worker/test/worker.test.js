import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  constantTimeTokenMatches,
  createWorker,
  findForbiddenField,
  limits,
  resetBestEffortRateLimitsForTests,
  validateIngestPayload,
} from "../src/index.js";

const TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef";
const BASE_NOW = Date.parse("2026-10-11T12:00:00.000Z");

class MockStatement {
  constructor(db, sql) {
    this.db = db;
    this.sql = sql.replace(/\s+/g, " ").trim();
    this.args = [];
  }

  bind(...args) {
    this.args = args;
    return this;
  }

  async run() {
    return this.db.run(this.sql, this.args);
  }

  async first() {
    return this.db.first(this.sql, this.args);
  }

  async all() {
    return this.db.all(this.sql, this.args);
  }
}

class MockD1 {
  constructor() {
    this.snapshots = new Map();
    this.rateWindows = new Map();
  }

  prepare(sql) {
    return new MockStatement(this, sql);
  }

  async run(sql, args) {
    if (sql.startsWith("INSERT INTO ingest_rate_windows")) {
      const [key, expiresAt] = args;
      const prior = this.rateWindows.get(key);
      this.rateWindows.set(key, { hits: (prior?.hits || 0) + 1, expires_at_ms: expiresAt });
      return { meta: { changes: 1 } };
    }
    if (sql.startsWith("INSERT OR IGNORE INTO quote_snapshots")) {
      const [
        snapshot_id, payload_hash, received_at_ms, source_generated_at,
        source_collected_at, expected_collection_interval_seconds,
        incoming_collection_age_seconds, source_stale_after_seconds,
        quote_collected_at, tick_at, bid, ask, last, mid, spread,
        spread_points, digits, point, trade_tick_size,
        mt5_reported_offset_seconds, mt5_reported_offset_material,
      ] = args;
      if (this.snapshots.has(snapshot_id)) return { meta: { changes: 0 } };
      this.snapshots.set(snapshot_id, {
        snapshot_id, payload_hash, received_at_ms, source_generated_at,
        source_collected_at, expected_collection_interval_seconds,
        incoming_collection_age_seconds, source_stale_after_seconds,
        quote_collected_at, tick_at, bid, ask, last, mid, spread,
        spread_points, digits, point, trade_tick_size,
        mt5_reported_offset_seconds, mt5_reported_offset_material,
      });
      return { meta: { changes: 1 } };
    }
    if (sql.startsWith("DELETE FROM quote_snapshots")) {
      let changes = 0;
      for (const [key, row] of this.snapshots) {
        if (row.received_at_ms < args[0]) {
          this.snapshots.delete(key);
          changes += 1;
        }
      }
      return { meta: { changes } };
    }
    if (sql.startsWith("DELETE FROM ingest_rate_windows")) {
      for (const [key, row] of this.rateWindows) {
        if (row.expires_at_ms < args[0]) this.rateWindows.delete(key);
      }
      return { meta: { changes: 0 } };
    }
    throw new Error(`Unhandled run: ${sql}`);
  }

  async first(sql, args) {
    if (sql === "SELECT 1 AS ok") return { ok: 1 };
    if (sql.startsWith("SELECT hits FROM ingest_rate_windows")) {
      return this.rateWindows.get(args[0]) || null;
    }
    if (sql.startsWith("SELECT snapshot_id, payload_hash, received_at_ms FROM quote_snapshots")) {
      return this.snapshots.get(args[0]) || null;
    }
    if (sql.includes("FROM quote_snapshots") && sql.includes("ORDER BY received_at_ms DESC")) {
      return this.sorted().find((row) => row.received_at_ms >= args[0]) || null;
    }
    throw new Error(`Unhandled first: ${sql}`);
  }

  async all(sql, args) {
    if (sql.includes("FROM quote_snapshots") && sql.includes("ORDER BY received_at_ms DESC")) {
      return {
        results: this.sorted()
          .filter((row) => row.received_at_ms >= args[0])
          .slice(0, args[1]),
      };
    }
    throw new Error(`Unhandled all: ${sql}`);
  }

  sorted() {
    return [...this.snapshots.values()].sort((left, right) =>
      right.received_at_ms - left.received_at_ms || right.snapshot_id.localeCompare(left.snapshot_id));
  }
}

function snapshotId(index = 1) {
  return `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
}

function validPayload(index = 1, bid = 5950.1) {
  const ask = bid + 0.5;
  return {
    schema_version: "fixedstep-quotes-v1",
    snapshot_id: snapshotId(index),
    generated_at: "2026-10-11T11:59:59.000Z",
    symbol: "FixedStep0.5",
    collection: {
      status: "live",
      connected: true,
      expected_collection_interval_seconds: 10,
      source_stale_after_seconds: 30,
      collected_at: "2026-10-11T11:59:58.000Z",
      collection_age_seconds: 2,
    },
    quote: {
      collected_at: "2026-10-11T11:59:58.500Z",
      tick_at: "2026-10-11T14:59:58.500Z",
      bid,
      ask,
      last: null,
      mid: (bid + ask) / 2,
      spread: ask - bid,
      spread_points: (ask - bid) / 0.1,
      digits: 1,
      point: 0.1,
      trade_tick_size: 0.1,
      tick_time_basis: "mt5_reported_unverified",
      mt5_reported_offset_seconds: 10_800,
      mt5_reported_offset_material: true,
    },
  };
}

function environment(db = new MockD1()) {
  return { QUOTES_DB: db, INGEST_TOKEN: TOKEN };
}

function ingestRequest(payload, options = {}) {
  const body = typeof payload === "string" ? payload : JSON.stringify(payload);
  return new Request("https://relay.example/v1/ingest", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${options.token ?? TOKEN}`,
      "Content-Type": options.contentType ?? "application/json",
      "CF-Connecting-IP": options.ip ?? "203.0.113.5",
      ...(options.headers || {}),
    },
    body,
  });
}

async function ingest(runtime, env, payload, options) {
  return runtime.fetch(ingestRequest(payload, options), env);
}

test.beforeEach(() => resetBestEffortRateLimitsForTests());

test("token comparison hashes equal and unequal inputs", async () => {
  assert.equal(await constantTimeTokenMatches(TOKEN, TOKEN), true);
  assert.equal(await constantTimeTokenMatches(`${TOKEN}x`, TOKEN), false);
  assert.equal(await constantTimeTokenMatches("", TOKEN), false);
  assert.equal(await constantTimeTokenMatches(TOKEN, "short"), false);
});

test("strict validator accepts only the quote contract", () => {
  const checked = validateIngestPayload(validPayload());
  assert.equal(checked.ok, true);
  assert.equal(findForbiddenField(checked.value), null);

  const extra = validPayload();
  extra.debug = "not allowed";
  assert.deepEqual(validateIngestPayload(extra), { ok: false, code: "invalid_schema" });

  const wrongSymbol = validPayload();
  wrongSymbol.symbol = "EURUSD";
  assert.equal(validateIngestPayload(wrongSymbol).ok, false);
});

test("recursive account, trade, and PnL fields are rejected", async () => {
  const runtime = createWorker({ now: () => BASE_NOW });
  const env = environment();
  for (const forbidden of [
    { metadata: { account: { balance: 10 } } },
    { nested: [{ positions: [] }] },
    { payload: { pnl: 5 } },
  ]) {
    const payload = { ...validPayload(), ...forbidden };
    const response = await ingest(runtime, env, payload);
    assert.equal(response.status, 422);
    assert.equal((await response.json()).error, "forbidden_field");
  }
  assert.equal(env.QUOTES_DB.snapshots.size, 0);
});

test("authentication, content type, and body size fail closed", async () => {
  const runtime = createWorker({ now: () => BASE_NOW });
  const env = environment();
  assert.equal((await ingest(runtime, env, validPayload(), { token: "wrong" })).status, 401);
  assert.equal((await ingest(runtime, env, validPayload(), { contentType: "text/plain" })).status, 415);

  const oversized = ingestRequest("{}", {
    headers: { "Content-Length": String(limits.maxBodyBytes + 1) },
  });
  assert.equal((await runtime.fetch(oversized, env)).status, 413);
  assert.equal(env.QUOTES_DB.snapshots.size, 0);
});

test("ingest is snapshot-id idempotent and detects conflicting reuse", async () => {
  const runtime = createWorker({ now: () => BASE_NOW });
  const env = environment();
  const accepted = await ingest(runtime, env, validPayload());
  assert.equal(accepted.status, 201);
  assert.equal((await accepted.json()).status, "accepted");

  const duplicate = await ingest(runtime, env, validPayload());
  assert.equal(duplicate.status, 200);
  assert.equal((await duplicate.json()).status, "duplicate");
  assert.equal(env.QUOTES_DB.snapshots.size, 1);

  const conflicting = await ingest(runtime, env, validPayload(1, 5960.1));
  assert.equal(conflicting.status, 409);
  assert.equal(env.QUOTES_DB.snapshots.size, 1);
});

test("latest freshness combines incoming same-host age with Worker elapsed time", async () => {
  let now = BASE_NOW;
  const runtime = createWorker({ now: () => now });
  const env = environment();
  assert.equal((await ingest(runtime, env, validPayload())).status, 201);

  now += 5_000;
  const liveResponse = await runtime.fetch(new Request("https://relay.example/v1/latest"), env);
  const live = await liveResponse.json();
  assert.equal(live.collection.status, "live");
  assert.equal(live.collection.collection_age_seconds, 7);
  assert.equal(live.relay.elapsed_seconds, 5);
  assert.equal(live.relay.incoming_collection_age_seconds, 2);

  now += 25_000;
  const stale = await (await runtime.fetch(new Request("https://relay.example/v1/latest"), env)).json();
  assert.equal(stale.collection.status, "stale");
  assert.equal(stale.collection.collection_age_seconds, 32);
});

test("history is ascending, bounded by limit, and quote-only", async () => {
  let now = BASE_NOW;
  const runtime = createWorker({ now: () => now });
  const env = environment();
  for (let index = 1; index <= 3; index += 1) {
    assert.equal((await ingest(runtime, env, validPayload(index, 5950 + index))).status, 201);
    now += 10_000;
  }
  const response = await runtime.fetch(
    new Request("https://relay.example/v1/quotes?limit=2", {
      headers: { Origin: "https://mahvro.github.io" },
    }),
    env,
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), "https://mahvro.github.io");
  const history = await response.json();
  assert.equal(history.samples.length, 2);
  assert.deepEqual(history.samples.map((sample) => sample.bid), [5952, 5953]);
  assert.equal(findForbiddenField(history), null);
  assert.equal(history.coverage.retention_hours, 24);

  const invalid = await runtime.fetch(new Request("https://relay.example/v1/quotes?limit=361"), env);
  assert.equal(invalid.status, 400);
});

test("public route limits are bounded and isolated from ingest authentication", async () => {
  const runtime = createWorker({ now: () => BASE_NOW });
  const env = environment();
  const sharedHeaders = { "CF-Connecting-IP": "203.0.113.5" };

  assert.equal((await ingest(runtime, env, validPayload())).status, 201);
  for (let index = 0; index < limits.historyRequestsPerMinute; index += 1) {
    const response = await runtime.fetch(new Request("https://relay.example/v1/quotes", {
      headers: sharedHeaders,
    }), env);
    assert.equal(response.status, 200);
  }
  assert.equal((await runtime.fetch(new Request("https://relay.example/v1/quotes", {
    headers: sharedHeaders,
  }), env)).status, 429);

  for (let index = 0; index < limits.healthRequestsPerMinute; index += 1) {
    const response = await runtime.fetch(new Request("https://relay.example/healthz", {
      headers: sharedHeaders,
    }), env);
    assert.equal(response.status, 200);
  }
  assert.equal((await runtime.fetch(new Request("https://relay.example/healthz", {
    headers: sharedHeaders,
  }), env)).status, 429);

  for (let index = 0; index < limits.latestRequestsPerMinute; index += 1) {
    const response = await runtime.fetch(new Request("https://relay.example/v1/latest", {
      headers: sharedHeaders,
    }), env);
    assert.equal(response.status, 200);
  }
  assert.equal((await runtime.fetch(new Request("https://relay.example/v1/latest", {
    headers: sharedHeaders,
  }), env)).status, 429);
});

test("CORS is emitted only for the exact GitHub Pages origin", async () => {
  const env = environment();
  const good = await worker.fetch(new Request("https://relay.example/healthz", {
    headers: { Origin: "https://mahvro.github.io" },
  }), env);
  assert.equal(good.status, 200);
  assert.equal(good.headers.get("Access-Control-Allow-Origin"), "https://mahvro.github.io");
  assert.equal(good.headers.get("Cache-Control"), "no-store, max-age=0");

  const bad = await worker.fetch(new Request("https://relay.example/healthz", {
    headers: { Origin: "https://evil.example" },
  }), env);
  assert.equal(bad.status, 200);
  assert.equal(bad.headers.get("Access-Control-Allow-Origin"), null);

  const preflight = await worker.fetch(new Request("https://relay.example/v1/latest", {
    method: "OPTIONS",
    headers: {
      Origin: "https://mahvro.github.io",
      "Access-Control-Request-Method": "GET",
    },
  }), env);
  assert.equal(preflight.status, 204);
});

test("global D1 ingest limit rejects the 31st request in one minute", async () => {
  const runtime = createWorker({ now: () => BASE_NOW });
  const env = environment();
  for (let index = 1; index <= limits.ingestsPerMinute; index += 1) {
    const response = await ingest(runtime, env, validPayload(index));
    assert.equal(response.status, 201);
  }
  const limited = await ingest(runtime, env, validPayload(limits.ingestsPerMinute + 1));
  assert.equal(limited.status, 429);
  assert.equal(env.QUOTES_DB.snapshots.size, limits.ingestsPerMinute);
});

test("a later accepted snapshot prunes observations older than 24 hours", async () => {
  let now = BASE_NOW;
  const runtime = createWorker({ now: () => now });
  const env = environment();
  assert.equal((await ingest(runtime, env, validPayload(1))).status, 201);
  now += limits.retentionMs + 1;
  assert.equal((await ingest(runtime, env, validPayload(2))).status, 201);
  assert.deepEqual([...env.QUOTES_DB.snapshots.keys()], [snapshotId(2)]);
});

test("reads hide expired observations even when no later ingest runs pruning", async () => {
  let now = BASE_NOW;
  const runtime = createWorker({ now: () => now });
  const env = environment();
  assert.equal((await ingest(runtime, env, validPayload(1))).status, 201);
  now += limits.retentionMs + 1;
  assert.equal(
    (await runtime.fetch(new Request("https://relay.example/v1/latest"), env)).status,
    404,
  );
  assert.equal(
    (await runtime.fetch(new Request("https://relay.example/v1/quotes"), env)).status,
    404,
  );
});

test("unknown routes and unsafe methods remain closed", async () => {
  const env = environment();
  assert.equal((await worker.fetch(new Request("https://relay.example/nope"), env)).status, 404);
  const deletion = await worker.fetch(new Request("https://relay.example/v1/latest", { method: "DELETE" }), env);
  assert.equal(deletion.status, 405);
  assert.equal(deletion.headers.get("Allow"), "GET, HEAD, OPTIONS");
});
