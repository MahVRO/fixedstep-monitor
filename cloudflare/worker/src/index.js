const SCHEMA_VERSION = "fixedstep-quotes-v1";
const SYMBOL = "FixedStep0.5";
const ALLOWED_ORIGIN = "https://mahvro.github.io";

const MAX_BODY_BYTES = 16 * 1024;
const MAX_RESPONSE_BYTES = 3_500_000;
const MAX_HISTORY_LIMIT = 8_640;
const DEFAULT_HISTORY_LIMIT = 360;
const RETENTION_MS = 24 * 60 * 60 * 1000;
const INGESTS_PER_MINUTE = 30;
const PUBLIC_REQUESTS_PER_MINUTE = 240;
const AUTH_ATTEMPTS_PER_MINUTE = 60;

const FORBIDDEN_FIELD_NAMES = new Set([
  "account",
  "accountinfo",
  "accountnumber",
  "balance",
  "commission",
  "deal",
  "deals",
  "equity",
  "leverage",
  "loss",
  "losses",
  "lot",
  "lots",
  "margin",
  "order",
  "orders",
  "pnl",
  "position",
  "positions",
  "profit",
  "profits",
  "swap",
  "trade",
  "trades",
  "transaction",
  "transactions",
  "volume",
]);

const publicRateWindows = new Map();

function normalizeFieldName(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9]/g, "");
}

export function findForbiddenField(root) {
  const pending = [{ value: root, depth: 0 }];
  let visited = 0;
  while (pending.length > 0) {
    const { value, depth } = pending.pop();
    visited += 1;
    if (visited > 256 || depth > 12) {
      return "payload_too_complex";
    }
    if (Array.isArray(value)) {
      for (const item of value) pending.push({ value: item, depth: depth + 1 });
      continue;
    }
    if (value === null || typeof value !== "object") continue;
    for (const [key, child] of Object.entries(value)) {
      if (FORBIDDEN_FIELD_NAMES.has(normalizeFieldName(key))) return key;
      pending.push({ value: child, depth: depth + 1 });
    }
  }
  return null;
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value, expected) {
  if (!isPlainObject(value)) return false;
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function isNullableFiniteNumber(value) {
  return value === null || isFiniteNumber(value);
}

function isUtcTimestamp(value) {
  return (
    typeof value === "string" &&
    value.length >= 20 &&
    value.length <= 32 &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value) &&
    Number.isFinite(Date.parse(value))
  );
}

function nearlyEqual(left, right) {
  return Math.abs(left - right) <= Math.max(1e-9, Math.abs(right) * 1e-10);
}

export function validateIngestPayload(value) {
  const forbidden = findForbiddenField(value);
  if (forbidden !== null) return { ok: false, code: "forbidden_field" };

  if (!hasExactKeys(value, [
    "schema_version",
    "snapshot_id",
    "generated_at",
    "symbol",
    "collection",
    "quote",
  ])) {
    return { ok: false, code: "invalid_schema" };
  }
  if (
    value.schema_version !== SCHEMA_VERSION ||
    value.symbol !== SYMBOL ||
    typeof value.snapshot_id !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.snapshot_id) ||
    !isUtcTimestamp(value.generated_at)
  ) {
    return { ok: false, code: "invalid_schema" };
  }

  const collection = value.collection;
  if (!hasExactKeys(collection, [
    "status",
    "connected",
    "expected_collection_interval_seconds",
    "source_stale_after_seconds",
    "collected_at",
    "collection_age_seconds",
  ])) {
    return { ok: false, code: "invalid_schema" };
  }
  if (
    collection.status !== "live" ||
    collection.connected !== true ||
    collection.expected_collection_interval_seconds !== 10 ||
    !Number.isInteger(collection.source_stale_after_seconds) ||
    collection.source_stale_after_seconds < 20 ||
    collection.source_stale_after_seconds > 300 ||
    !isUtcTimestamp(collection.collected_at) ||
    !isFiniteNumber(collection.collection_age_seconds) ||
    collection.collection_age_seconds < 0 ||
    collection.collection_age_seconds > collection.source_stale_after_seconds
  ) {
    return { ok: false, code: "invalid_collection" };
  }

  const quote = value.quote;
  if (!hasExactKeys(quote, [
    "collected_at",
    "tick_at",
    "bid",
    "ask",
    "last",
    "mid",
    "spread",
    "spread_points",
    "digits",
    "point",
    "trade_tick_size",
    "tick_time_basis",
    "mt5_reported_offset_seconds",
    "mt5_reported_offset_material",
  ])) {
    return { ok: false, code: "invalid_schema" };
  }

  const spread = quote.ask - quote.bid;
  if (
    !isUtcTimestamp(quote.collected_at) ||
    !isUtcTimestamp(quote.tick_at) ||
    !isFiniteNumber(quote.bid) || quote.bid <= 0 ||
    !isFiniteNumber(quote.ask) || quote.ask < quote.bid ||
    !isNullableFiniteNumber(quote.last) || (quote.last !== null && quote.last <= 0) ||
    !isFiniteNumber(quote.mid) || !nearlyEqual(quote.mid, (quote.bid + quote.ask) / 2) ||
    !isFiniteNumber(quote.spread) || quote.spread < 0 || !nearlyEqual(quote.spread, spread) ||
    !isNullableFiniteNumber(quote.spread_points) ||
    (quote.spread_points !== null && quote.spread_points < 0) ||
    !Number.isInteger(quote.digits) || quote.digits < 0 || quote.digits > 10 ||
    !isFiniteNumber(quote.point) || quote.point <= 0 ||
    !isFiniteNumber(quote.trade_tick_size) || quote.trade_tick_size <= 0 ||
    quote.tick_time_basis !== "mt5_reported_unverified" ||
    !isNullableFiniteNumber(quote.mt5_reported_offset_seconds) ||
    typeof quote.mt5_reported_offset_material !== "boolean"
  ) {
    return { ok: false, code: "invalid_quote" };
  }
  if (quote.spread_points !== null && !nearlyEqual(quote.spread_points, spread / quote.point)) {
    return { ok: false, code: "invalid_quote" };
  }

  // Rebuild the object from an allowlist. No input object is persisted directly.
  return {
    ok: true,
    value: {
      schema_version: SCHEMA_VERSION,
      snapshot_id: value.snapshot_id,
      generated_at: value.generated_at,
      symbol: SYMBOL,
      collection: {
        status: "live",
        connected: true,
        expected_collection_interval_seconds: 10,
        source_stale_after_seconds: collection.source_stale_after_seconds,
        collected_at: collection.collected_at,
        collection_age_seconds: collection.collection_age_seconds,
      },
      quote: {
        collected_at: quote.collected_at,
        tick_at: quote.tick_at,
        bid: quote.bid,
        ask: quote.ask,
        last: quote.last,
        mid: quote.mid,
        spread: quote.spread,
        spread_points: quote.spread_points,
        digits: quote.digits,
        point: quote.point,
        trade_tick_size: quote.trade_tick_size,
        tick_time_basis: "mt5_reported_unverified",
        mt5_reported_offset_seconds: quote.mt5_reported_offset_seconds,
        mt5_reported_offset_material: quote.mt5_reported_offset_material,
      },
    },
  };
}

function responseHeaders(request, contentType = "application/json; charset=utf-8") {
  const headers = new Headers({
    "Cache-Control": "no-store, max-age=0",
    Pragma: "no-cache",
    "Content-Type": contentType,
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    Vary: "Origin",
  });
  if (request.headers.get("Origin") === ALLOWED_ORIGIN) {
    headers.set("Access-Control-Allow-Origin", ALLOWED_ORIGIN);
  }
  return headers;
}

function jsonResponse(request, status, value, method = request.method) {
  const body = JSON.stringify(value) + "\n";
  return new Response(method === "HEAD" ? null : body, {
    status,
    headers: responseHeaders(request),
  });
}

function errorResponse(request, status, code) {
  return jsonResponse(request, status, { error: code });
}

async function readBoundedBody(request) {
  const lengthHeader = request.headers.get("Content-Length");
  if (lengthHeader !== null) {
    if (!/^\d+$/.test(lengthHeader)) return { ok: false, status: 400, code: "invalid_content_length" };
    if (Number(lengthHeader) > MAX_BODY_BYTES) return { ok: false, status: 413, code: "payload_too_large" };
  }
  if (request.body === null) return { ok: false, status: 400, code: "empty_body" };

  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      return { ok: false, status: 413, code: "payload_too_large" };
    }
    chunks.push(value);
  }
  if (size === 0) return { ok: false, status: 400, code: "empty_body" };
  const combined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return { ok: true, text: new TextDecoder("utf-8", { fatal: true }).decode(combined) };
  } catch {
    return { ok: false, status: 400, code: "invalid_utf8" };
  }
}

async function sha256Bytes(value) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

async function sha256Hex(value) {
  const bytes = await sha256Bytes(value);
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function constantTimeTokenMatches(provided, expected) {
  const safeProvided = typeof provided === "string" && provided.length <= 256 ? provided : "";
  const safeExpected = typeof expected === "string" && expected.length >= 32 && expected.length <= 256 ? expected : "";
  const [left, right] = await Promise.all([sha256Bytes(safeProvided), sha256Bytes(safeExpected)]);
  let difference = safeProvided.length ^ safeExpected.length;
  for (let index = 0; index < left.length; index += 1) difference |= left[index] ^ right[index];
  return safeExpected.length > 0 && difference === 0;
}

function bearerToken(request) {
  const header = request.headers.get("Authorization");
  if (header === null || header.length > 300) return "";
  const match = /^Bearer ([\x21-\x7e]{1,256})$/i.exec(header);
  return match ? match[1] : "";
}

function clientRateAllowed(request, nowMs, limit) {
  const address = request.headers.get("CF-Connecting-IP") || "unknown";
  const bucket = Math.floor(nowMs / 60_000);
  const key = `${address}:${bucket}`;
  const current = publicRateWindows.get(key) || 0;
  publicRateWindows.set(key, current + 1);
  if (publicRateWindows.size > 2_000) {
    for (const candidate of publicRateWindows.keys()) {
      if (!candidate.endsWith(`:${bucket}`)) publicRateWindows.delete(candidate);
    }
  }
  return current < limit;
}

async function globalIngestRateAllowed(db, nowMs) {
  const bucket = `ingest:${Math.floor(nowMs / 60_000)}`;
  const expiresAt = nowMs + 2 * 60_000;
  await db.prepare(
    `INSERT INTO ingest_rate_windows (window_key, hits, expires_at_ms)
     VALUES (?1, 1, ?2)
     ON CONFLICT(window_key) DO UPDATE SET
       hits = hits + 1,
       expires_at_ms = excluded.expires_at_ms`,
  ).bind(bucket, expiresAt).run();
  const row = await db.prepare(
    "SELECT hits FROM ingest_rate_windows WHERE window_key = ?1",
  ).bind(bucket).first();
  return row !== null && Number(row.hits) <= INGESTS_PER_MINUTE;
}

function rowToQuote(row) {
  return {
    collected_at: row.quote_collected_at,
    tick_at: row.tick_at,
    bid: Number(row.bid),
    ask: Number(row.ask),
    last: row.last === null ? null : Number(row.last),
    mid: Number(row.mid),
    spread: Number(row.spread),
    spread_points: row.spread_points === null ? null : Number(row.spread_points),
    digits: Number(row.digits),
    point: Number(row.point),
    trade_tick_size: Number(row.trade_tick_size),
    tick_time_basis: "mt5_reported_unverified",
    mt5_reported_offset_seconds:
      row.mt5_reported_offset_seconds === null ? null : Number(row.mt5_reported_offset_seconds),
    mt5_reported_offset_material: Boolean(row.mt5_reported_offset_material),
  };
}

function buildLatest(row, nowMs) {
  const elapsedSeconds = Math.max(0, (nowMs - Number(row.received_at_ms)) / 1000);
  const incomingAge = Number(row.incoming_collection_age_seconds);
  const effectiveAge = incomingAge + elapsedSeconds;
  const staleAfter = Number(row.source_stale_after_seconds);
  return {
    schema_version: SCHEMA_VERSION,
    snapshot_id: row.snapshot_id,
    generated_at: new Date(nowMs).toISOString(),
    timezone: "UTC",
    collector_clock_basis: "utc_serialized_host_clock_synchronization_not_asserted",
    tick_time_basis: "mt5_reported_unverified",
    chart_time_basis: "collector_first_observation_host_clock",
    symbol: SYMBOL,
    collection: {
      status: effectiveAge > staleAfter ? "stale" : "live",
      connected: true,
      expected_collection_interval_seconds: Number(row.expected_collection_interval_seconds),
      source_stale_after_seconds: staleAfter,
      collected_at: row.source_collected_at,
      collection_age_seconds: effectiveAge,
    },
    quote: rowToQuote(row),
    relay: {
      received_at: new Date(Number(row.received_at_ms)).toISOString(),
      elapsed_seconds: elapsedSeconds,
      incoming_collection_age_seconds: incomingAge,
      source_generated_at: row.source_generated_at,
      freshness_basis: "incoming_same_host_age_plus_worker_elapsed_time",
    },
    samples: [],
  };
}

const SELECT_COLUMNS = `
  snapshot_id, received_at_ms, source_generated_at, source_collected_at,
  expected_collection_interval_seconds, incoming_collection_age_seconds,
  source_stale_after_seconds, quote_collected_at, tick_at, bid, ask, last,
  mid, spread, spread_points, digits, point, trade_tick_size,
  mt5_reported_offset_seconds, mt5_reported_offset_material`;

async function latestRow(db) {
  return db.prepare(
    `SELECT ${SELECT_COLUMNS}
     FROM quote_snapshots
     ORDER BY received_at_ms DESC, snapshot_id DESC
     LIMIT 1`,
  ).first();
}

async function handleIngest(request, env, nowMs) {
  if (!clientRateAllowed(request, nowMs, AUTH_ATTEMPTS_PER_MINUTE)) {
    return errorResponse(request, 429, "rate_limited");
  }
  if (!env.QUOTES_DB || typeof env.INGEST_TOKEN !== "string") {
    return errorResponse(request, 503, "service_unavailable");
  }
  if (!(await constantTimeTokenMatches(bearerToken(request), env.INGEST_TOKEN))) {
    return errorResponse(request, 401, "unauthorized");
  }
  const contentType = (request.headers.get("Content-Type") || "").toLowerCase();
  const contentEncoding = (request.headers.get("Content-Encoding") || "identity").toLowerCase();
  if (!/^application\/json(?:\s*;\s*charset=utf-8)?$/.test(contentType) || contentEncoding !== "identity") {
    return errorResponse(request, 415, "unsupported_media_type");
  }
  const body = await readBoundedBody(request);
  if (!body.ok) return errorResponse(request, body.status, body.code);
  let parsed;
  try {
    parsed = JSON.parse(body.text);
  } catch {
    return errorResponse(request, 400, "invalid_json");
  }
  const checked = validateIngestPayload(parsed);
  if (!checked.ok) return errorResponse(request, 422, checked.code);

  const canonical = JSON.stringify(checked.value);
  const payloadHash = await sha256Hex(canonical);
  const db = env.QUOTES_DB;
  try {
    if (!(await globalIngestRateAllowed(db, nowMs))) {
      return errorResponse(request, 429, "rate_limited");
    }
    const prior = await db.prepare(
      "SELECT snapshot_id, payload_hash, received_at_ms FROM quote_snapshots WHERE snapshot_id = ?1",
    ).bind(checked.value.snapshot_id).first();
    if (prior !== null) {
      if (prior.payload_hash !== payloadHash) return errorResponse(request, 409, "snapshot_conflict");
      return jsonResponse(request, 200, {
        status: "duplicate",
        snapshot_id: prior.snapshot_id,
        received_at: new Date(Number(prior.received_at_ms)).toISOString(),
      });
    }

    const item = checked.value;
    const quote = item.quote;
    const result = await db.prepare(
      `INSERT OR IGNORE INTO quote_snapshots (
        snapshot_id, payload_hash, received_at_ms, source_generated_at,
        source_collected_at, expected_collection_interval_seconds,
        incoming_collection_age_seconds, source_stale_after_seconds,
        quote_collected_at, tick_at, bid, ask, last, mid, spread,
        spread_points, digits, point, trade_tick_size,
        mt5_reported_offset_seconds, mt5_reported_offset_material
      ) VALUES (
        ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11,
        ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21
      )`,
    ).bind(
      item.snapshot_id,
      payloadHash,
      nowMs,
      item.generated_at,
      item.collection.collected_at,
      item.collection.expected_collection_interval_seconds,
      item.collection.collection_age_seconds,
      item.collection.source_stale_after_seconds,
      quote.collected_at,
      quote.tick_at,
      quote.bid,
      quote.ask,
      quote.last,
      quote.mid,
      quote.spread,
      quote.spread_points,
      quote.digits,
      quote.point,
      quote.trade_tick_size,
      quote.mt5_reported_offset_seconds,
      quote.mt5_reported_offset_material ? 1 : 0,
    ).run();

    const changes = Number(result?.meta?.changes ?? result?.changes ?? 0);
    if (changes !== 1) {
      const raced = await db.prepare(
        "SELECT snapshot_id, payload_hash, received_at_ms FROM quote_snapshots WHERE snapshot_id = ?1",
      ).bind(item.snapshot_id).first();
      if (raced === null || raced.payload_hash !== payloadHash) {
        return errorResponse(request, 409, "snapshot_conflict");
      }
      return jsonResponse(request, 200, {
        status: "duplicate",
        snapshot_id: raced.snapshot_id,
        received_at: new Date(Number(raced.received_at_ms)).toISOString(),
      });
    }

    await db.prepare("DELETE FROM quote_snapshots WHERE received_at_ms < ?1")
      .bind(nowMs - RETENTION_MS).run();
    await db.prepare("DELETE FROM ingest_rate_windows WHERE expires_at_ms < ?1")
      .bind(nowMs).run();
    return jsonResponse(request, 201, {
      status: "accepted",
      snapshot_id: item.snapshot_id,
      received_at: new Date(nowMs).toISOString(),
    });
  } catch {
    return errorResponse(request, 503, "storage_unavailable");
  }
}

async function handleLatest(request, env, nowMs) {
  if (!env.QUOTES_DB) return errorResponse(request, 503, "service_unavailable");
  try {
    const row = await latestRow(env.QUOTES_DB);
    if (row === null) return errorResponse(request, 404, "no_quote_data");
    return jsonResponse(request, 200, buildLatest(row, nowMs));
  } catch {
    return errorResponse(request, 503, "storage_unavailable");
  }
}

function historyLimit(url) {
  const keys = [...url.searchParams.keys()];
  if (keys.some((key) => key !== "limit") || url.searchParams.getAll("limit").length > 1) return null;
  const raw = url.searchParams.get("limit");
  if (raw === null) return DEFAULT_HISTORY_LIMIT;
  if (!/^[1-9]\d{0,3}$/.test(raw)) return null;
  const value = Number(raw);
  return value <= MAX_HISTORY_LIMIT ? value : null;
}

async function handleHistory(request, env, nowMs, url) {
  const limit = historyLimit(url);
  if (limit === null) return errorResponse(request, 400, "invalid_limit");
  if (!env.QUOTES_DB) return errorResponse(request, 503, "service_unavailable");
  try {
    const result = await env.QUOTES_DB.prepare(
      `SELECT ${SELECT_COLUMNS}
       FROM quote_snapshots
       ORDER BY received_at_ms DESC, snapshot_id DESC
       LIMIT ?1`,
    ).bind(limit).all();
    const rows = Array.isArray(result?.results) ? result.results : [];
    if (rows.length === 0) return errorResponse(request, 404, "no_quote_data");
    const latest = buildLatest(rows[0], nowMs);
    const samples = rows.slice().reverse().map(rowToQuote);
    const payload = {
      ...latest,
      coverage: {
        quote_samples: samples.length,
        maximum_response_samples: MAX_HISTORY_LIMIT,
        retention_hours: 24,
        history_generated_at: new Date(nowMs).toISOString(),
        collection_start_at: samples[0].collected_at,
        collection_end_at: samples[samples.length - 1].collected_at,
      },
      samples,
    };
    let encoded = JSON.stringify(payload) + "\n";
    while (new TextEncoder().encode(encoded).byteLength > MAX_RESPONSE_BYTES && payload.samples.length > 1) {
      payload.samples.shift();
      payload.coverage.quote_samples = payload.samples.length;
      payload.coverage.collection_start_at = payload.samples[0].collected_at;
      encoded = JSON.stringify(payload) + "\n";
    }
    return new Response(request.method === "HEAD" ? null : encoded, {
      status: 200,
      headers: responseHeaders(request),
    });
  } catch {
    return errorResponse(request, 503, "storage_unavailable");
  }
}

async function handleHealth(request, env) {
  if (!env.QUOTES_DB) return errorResponse(request, 503, "service_unavailable");
  try {
    const result = await env.QUOTES_DB.prepare("SELECT 1 AS ok").first();
    if (Number(result?.ok) !== 1) return errorResponse(request, 503, "storage_unavailable");
    return jsonResponse(request, 200, { status: "ok" });
  } catch {
    return errorResponse(request, 503, "storage_unavailable");
  }
}

function handleOptions(request, url) {
  if (!["/healthz", "/v1/latest", "/v1/quotes"].includes(url.pathname)) {
    return errorResponse(request, 403, "cors_forbidden");
  }
  if (request.headers.get("Origin") !== ALLOWED_ORIGIN) {
    return errorResponse(request, 403, "cors_forbidden");
  }
  const requestedMethod = (request.headers.get("Access-Control-Request-Method") || "GET").toUpperCase();
  if (!["GET", "HEAD"].includes(requestedMethod)) return errorResponse(request, 403, "cors_forbidden");
  const requestedHeaders = (request.headers.get("Access-Control-Request-Headers") || "")
    .split(",").map((item) => item.trim().toLowerCase()).filter(Boolean);
  if (requestedHeaders.some((item) => !["accept", "cache-control", "pragma"].includes(item))) {
    return errorResponse(request, 403, "cors_forbidden");
  }
  const headers = responseHeaders(request);
  headers.delete("Content-Type");
  headers.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
  if (requestedHeaders.length > 0) headers.set("Access-Control-Allow-Headers", requestedHeaders.join(", "));
  headers.set("Access-Control-Max-Age", "600");
  return new Response(null, { status: 204, headers });
}

export function createWorker({ now = () => Date.now() } = {}) {
  return {
    async fetch(request, env) {
      const nowMs = now();
      let url;
      try {
        url = new URL(request.url);
      } catch {
        return errorResponse(request, 400, "invalid_url");
      }

      if (request.method === "OPTIONS") return handleOptions(request, url);
      if (request.method === "POST" && url.pathname === "/v1/ingest" && url.search === "") {
        return handleIngest(request, env, nowMs);
      }
      if (!["GET", "HEAD"].includes(request.method)) {
        const response = errorResponse(request, 405, "method_not_allowed");
        response.headers.set("Allow", url.pathname === "/v1/ingest" ? "POST" : "GET, HEAD, OPTIONS");
        return response;
      }
      if (!clientRateAllowed(request, nowMs, PUBLIC_REQUESTS_PER_MINUTE)) {
        return errorResponse(request, 429, "rate_limited");
      }
      if (url.pathname === "/healthz" && url.search === "") return handleHealth(request, env);
      if (url.pathname === "/v1/latest" && url.search === "") return handleLatest(request, env, nowMs);
      if (url.pathname === "/v1/quotes") return handleHistory(request, env, nowMs, url);
      return errorResponse(request, 404, "not_found");
    },
  };
}

export function resetBestEffortRateLimitsForTests() {
  publicRateWindows.clear();
}

export const limits = Object.freeze({
  maxBodyBytes: MAX_BODY_BYTES,
  maxHistoryLimit: MAX_HISTORY_LIMIT,
  retentionMs: RETENTION_MS,
  ingestsPerMinute: INGESTS_PER_MINUTE,
});

export default createWorker();
