"use strict";

import { isHealthyLive, shouldAdoptLatest } from "./live-policy.js";

const SYMBOL = "FixedStep0.5";
const SCHEMA_VERSION = "fixedstep-quotes-v1";

function validatedLiveOrigin(rawOrigin) {
  if (rawOrigin === null || rawOrigin === undefined || rawOrigin === "") return null;
  if (typeof rawOrigin !== "string" || rawOrigin !== rawOrigin.trim()) return null;
  try {
    const parsed = new URL(rawOrigin);
    if (
      parsed.protocol !== "https:"
      || parsed.username !== ""
      || parsed.password !== ""
      || (parsed.pathname !== "" && parsed.pathname !== "/")
      || parsed.search !== ""
      || parsed.hash !== ""
      || parsed.port !== ""
      || parsed.hostname === ""
      || !parsed.hostname.endsWith(".workers.dev")
    ) return null;
    return parsed.origin;
  } catch {
    return null;
  }
}

const RUNTIME_CONFIG = window.FIXEDSTEP_MONITOR_CONFIG;
const LIVE_ORIGIN = validatedLiveOrigin(RUNTIME_CONFIG?.liveOrigin);
const LIVE_HISTORY_URL = LIVE_ORIGIN ? `${LIVE_ORIGIN}/v1/quotes` : null;
const LIVE_LATEST_URL = LIVE_ORIGIN ? `${LIVE_ORIGIN}/v1/latest` : null;
const STATIC_HISTORY_URL = "../ai/data/quotes.json";
const POLL_MS = 10_000;
const REQUEST_TIMEOUT_MS = 8_000;
const HISTORY_REFRESH_MS = 5 * 60_000;
const STATIC_REFRESH_MS = 10 * 60_000;
const STATIC_RETRY_MS = 60_000;
const DEFAULT_STALE_SECONDS = 45;
const MATERIAL_CLOCK_OFFSET_SECONDS = 300;
const MAX_HISTORY_MS = 90 * 24 * 60 * 60 * 1000;
const MAX_HISTORY_SAMPLES = 18_000;

const state = {
  samples: [],
  latest: null,
  latestSource: null,
  rangeMs: 5 * 60_000,
  liveTransportOk: null,
  transportError: false,
  latestPollRunning: false,
  fullRefreshRunning: false,
  staticRefreshRunning: false,
  lastStaticAttemptAt: 0,
  latestReceiptMonotonicMs: null,
};

const utcDate = new Intl.DateTimeFormat(undefined, {
  timeZone: "UTC",
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});
const shortUtc = new Intl.DateTimeFormat(undefined, {
  timeZone: "UTC",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

function el(id) {
  return document.getElementById(id);
}

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function parseTimestamp(value) {
  if (typeof value !== "string" || !value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?Z$/.exec(value);
  if (match === null) return null;
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return null;
  const instant = new Date(milliseconds);
  return instant.getUTCFullYear() === Number(match[1])
    && instant.getUTCMonth() + 1 === Number(match[2])
    && instant.getUTCDate() === Number(match[3])
    && instant.getUTCHours() === Number(match[4])
    && instant.getUTCMinutes() === Number(match[5])
    && instant.getUTCSeconds() === Number(match[6])
    ? milliseconds
    : null;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);
}

function validatedNumber(value, field, { positive = false, nonNegative = false, integer = false } = {}) {
  if (value === null || value === undefined) return null;
  if (!isFiniteNumber(value)) throw new Error(`${field} must be a finite number or null`);
  if (positive && value <= 0) throw new Error(`${field} must be positive`);
  if (nonNegative && value < 0) throw new Error(`${field} must not be negative`);
  if (integer && !Number.isInteger(value)) throw new Error(`${field} must be an integer`);
  return value;
}

function validatedTimestamp(value, field, { required = false } = {}) {
  if (value === null || value === undefined || value === "") {
    if (required) throw new Error(`${field} is required`);
    return { text: null, milliseconds: null };
  }
  const milliseconds = parseTimestamp(value);
  if (milliseconds === null) throw new Error(`${field} must be a valid timestamp`);
  return { text: value, milliseconds };
}

function nearlyEqual(left, right) {
  if (!isFiniteNumber(left) || !isFiniteNumber(right)) return false;
  const scale = Math.max(1, Math.abs(left), Math.abs(right));
  return Math.abs(left - right) <= scale * 1e-10;
}

function normalizeQuote(raw, context, fieldPrefix) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`${fieldPrefix} must be an object`);
  }

  if (raw.symbol !== undefined && raw.symbol !== SYMBOL) {
    throw new Error(`${fieldPrefix}.symbol does not match ${SYMBOL}`);
  }

  const bid = validatedNumber(raw.bid, `${fieldPrefix}.bid`);
  const ask = validatedNumber(raw.ask, `${fieldPrefix}.ask`);
  const last = validatedNumber(raw.last, `${fieldPrefix}.last`);
  let mid = validatedNumber(raw.mid, `${fieldPrefix}.mid`);
  let spread = validatedNumber(raw.spread, `${fieldPrefix}.spread`);
  let spreadPoints = validatedNumber(raw.spread_points, `${fieldPrefix}.spread_points`);
  const point = validatedNumber(raw.point ?? context.point, `${fieldPrefix}.point`, { positive: true });
  const tradeTickSize = validatedNumber(raw.trade_tick_size ?? context.tradeTickSize, `${fieldPrefix}.trade_tick_size`, { positive: true });
  const digits = validatedNumber(raw.digits ?? context.digits, `${fieldPrefix}.digits`, { integer: true });

  if (digits !== null && (digits < 0 || digits > 10)) {
    throw new Error(`${fieldPrefix}.digits is outside the supported range`);
  }
  if (bid !== null && ask !== null && ask < bid) {
    throw new Error(`${fieldPrefix}.ask must be greater than or equal to bid`);
  }

  if (bid !== null && ask !== null) {
    const derivedMid = bid + (ask - bid) / 2;
    const derivedSpread = ask - bid;
    if (!isFiniteNumber(derivedMid) || !isFiniteNumber(derivedSpread)) {
      throw new Error(`${fieldPrefix} derived price values are not finite`);
    }
    if (mid !== null && !nearlyEqual(mid, derivedMid)) throw new Error(`${fieldPrefix}.mid is inconsistent with bid and ask`);
    if (spread !== null && !nearlyEqual(spread, derivedSpread)) throw new Error(`${fieldPrefix}.spread is inconsistent with bid and ask`);
    mid = derivedMid;
    spread = derivedSpread;
  }
  if (spread !== null && point !== null) {
    const derivedSpreadPoints = spread / point;
    if (!isFiniteNumber(derivedSpreadPoints)) {
      throw new Error(`${fieldPrefix}.spread_points cannot be derived as a finite number`);
    }
    if (spreadPoints !== null && !nearlyEqual(spreadPoints, derivedSpreadPoints)) {
      throw new Error(`${fieldPrefix}.spread_points is inconsistent with spread and point`);
    }
    spreadPoints = derivedSpreadPoints;
  }

  if (spread !== null && spread < 0) throw new Error(`${fieldPrefix}.spread cannot be negative`);
  if (spreadPoints !== null && spreadPoints < 0) throw new Error(`${fieldPrefix}.spread_points cannot be negative`);

  const tick = validatedTimestamp(raw.tick_at, `${fieldPrefix}.tick_at`, { required: true });
  const collected = validatedTimestamp(raw.collected_at, `${fieldPrefix}.collected_at`, { required: true });
  let reportedOffsetSeconds = validatedNumber(
    raw.mt5_reported_offset_seconds,
    `${fieldPrefix}.mt5_reported_offset_seconds`,
  );
  if (reportedOffsetSeconds === null && tick.milliseconds !== null && collected.milliseconds !== null) {
    reportedOffsetSeconds = (tick.milliseconds - collected.milliseconds) / 1000;
  }
  let reportedOffsetMaterial = raw.mt5_reported_offset_material;
  if (reportedOffsetMaterial !== null && reportedOffsetMaterial !== undefined && typeof reportedOffsetMaterial !== "boolean") {
    throw new Error(`${fieldPrefix}.mt5_reported_offset_material must be a boolean`);
  }
  if (reportedOffsetMaterial === null || reportedOffsetMaterial === undefined) {
    reportedOffsetMaterial = reportedOffsetSeconds !== null
      && Math.abs(reportedOffsetSeconds) >= MATERIAL_CLOCK_OFFSET_SECONDS;
  }

  return {
    collected_at: collected.text,
    tick_at: tick.text,
    bid,
    ask,
    last,
    mid,
    spread,
    spread_points: spreadPoints,
    digits,
    point,
    trade_tick_size: tradeTickSize,
    _collected_ms: collected.milliseconds,
    _tick_ms: tick.milliseconds,
    _mt5_reported_offset_seconds: reportedOffsetSeconds,
    _mt5_reported_offset_material: reportedOffsetMaterial,
  };
}

function normalizePayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("The quote response must be a JSON object");
  }
  if (payload.schema_version !== SCHEMA_VERSION) {
    throw new Error("Unsupported quote schema");
  }
  if (payload.symbol !== SYMBOL) {
    throw new Error(`The response is not for ${SYMBOL}`);
  }

  const collectionRaw = payload.collection;
  if (!collectionRaw || typeof collectionRaw !== "object" || Array.isArray(collectionRaw)) {
    throw new Error("collection must be an object");
  }
  const collectionTime = validatedTimestamp(collectionRaw.collected_at, "collection.collected_at");
  const generatedTime = validatedTimestamp(payload.generated_at, "generated_at");
  const publishedTime = validatedTimestamp(payload.published_at, "published_at");
  const staleSeconds = validatedNumber(
    collectionRaw.source_stale_after_seconds,
    "collection.source_stale_after_seconds",
    { positive: true },
  ) ?? DEFAULT_STALE_SECONDS;
  const intervalSeconds = validatedNumber(
    collectionRaw.expected_collection_interval_seconds,
    "collection.expected_collection_interval_seconds",
    { positive: true },
  );
  const collectionAgeSeconds = validatedNumber(
    collectionRaw.collection_age_seconds,
    "collection.collection_age_seconds",
    { nonNegative: true },
  );
  const connected = collectionRaw.connected === null || collectionRaw.connected === undefined
    ? null
    : collectionRaw.connected;
  if (connected !== null && typeof connected !== "boolean") {
    throw new Error("collection.connected must be a boolean or null");
  }
  const status = typeof collectionRaw.status === "string" ? collectionRaw.status.toLowerCase() : "unavailable";
  if (!new Set(["live", "disconnected", "no_data", "stale", "degraded", "unavailable"]).has(status)) {
    throw new Error("collection.status is invalid");
  }
  const tickTimeBasis = payload.tick_time_basis ?? "mt5_reported_unverified";
  if (tickTimeBasis !== "mt5_reported_unverified") {
    throw new Error("tick_time_basis is unsupported");
  }
  const collectorClockBasis = payload.collector_clock_basis
    ?? "utc_serialized_host_clock_synchronization_not_asserted";
  if (collectorClockBasis !== "utc_serialized_host_clock_synchronization_not_asserted") {
    throw new Error("collector_clock_basis is unsupported");
  }
  const chartTimeBasis = payload.chart_time_basis ?? "collector_first_observation_host_clock";
  if (chartTimeBasis !== "collector_first_observation_host_clock") {
    throw new Error("chart_time_basis is unsupported");
  }
  const snapshotId = typeof payload.snapshot_id === "string" ? payload.snapshot_id : null;
  let relay = null;
  if (payload.relay !== null && payload.relay !== undefined) {
    if (typeof payload.relay !== "object" || Array.isArray(payload.relay)) {
      throw new Error("relay must be an object");
    }
    const relayReceived = validatedTimestamp(payload.relay.received_at, "relay.received_at");
    if (
      relayReceived.milliseconds === null
      || payload.relay.freshness_basis !== "incoming_same_host_age_plus_worker_elapsed_time"
    ) {
      throw new Error("relay freshness metadata is invalid");
    }
    relay = {
      received_at: relayReceived.text,
      _received_ms: relayReceived.milliseconds,
      freshness_basis: payload.relay.freshness_basis,
    };
  }

  const quoteContext = {
    digits: payload.quote?.digits ?? null,
    point: payload.quote?.point ?? null,
    tradeTickSize: payload.quote?.trade_tick_size ?? null,
  };
  const quote = payload.quote === null || payload.quote === undefined
    ? null
    : normalizeQuote(payload.quote, quoteContext, "quote");

  const rawSamples = payload.samples === null || payload.samples === undefined ? [] : payload.samples;
  if (!Array.isArray(rawSamples)) throw new Error("samples must be an array");
  if (rawSamples.length > 20_000) throw new Error("samples exceeds the accepted response bound");
  const samples = rawSamples.map((sample, index) => normalizeQuote(sample, quoteContext, `samples[${index}]`));
  if (quote) samples.push(quote);

  return {
    schema_version: payload.schema_version,
    snapshot_id: snapshotId,
    symbol: payload.symbol,
    generated_at: generatedTime.text,
    _generated_ms: generatedTime.milliseconds,
    published_at: publishedTime.text,
    tick_time_basis: tickTimeBasis,
    collector_clock_basis: collectorClockBasis,
    chart_time_basis: chartTimeBasis,
    relay,
    collection: {
      status,
      connected,
      collected_at: collectionTime.text,
      expected_collection_interval_seconds: intervalSeconds,
      source_stale_after_seconds: staleSeconds,
      collection_age_seconds: collectionAgeSeconds,
      _collected_ms: collectionTime.milliseconds,
    },
    quote,
    samples,
  };
}

function sampleKey(sample) {
  if (sample._tick_ms !== null) {
    return `${sample._tick_ms}|${sample.bid ?? ""}|${sample.ask ?? ""}|${sample.mid ?? ""}`;
  }
  return `untimed|${sample._collected_ms ?? ""}|${sample.bid ?? ""}|${sample.ask ?? ""}|${sample.mid ?? ""}`;
}

function mergeSamples(incoming) {
  const merged = new Map();
  for (const sample of [...state.samples, ...incoming]) {
    const key = sampleKey(sample);
    const existing = merged.get(key);
    if (!existing || (sample._collected_ms ?? -Infinity) >= (existing._collected_ms ?? -Infinity)) {
      merged.set(key, sample);
    }
  }

  let rows = [...merged.values()].sort((left, right) => {
    const leftTime = left._collected_ms ?? -Infinity;
    const rightTime = right._collected_ms ?? -Infinity;
    return leftTime - rightTime || (left._tick_ms ?? 0) - (right._tick_ms ?? 0);
  });
  const newestCollection = rows.reduce((latest, row) => Math.max(latest, row._collected_ms ?? -Infinity), -Infinity);
  if (Number.isFinite(newestCollection)) {
    rows = rows.filter(row => row._collected_ms !== null && row._collected_ms >= newestCollection - MAX_HISTORY_MS);
  }
  if (rows.length > MAX_HISTORY_SAMPLES) rows = rows.slice(-MAX_HISTORY_SAMPLES);
  state.samples = rows;
}

function newestQuoteFrom(normalized) {
  if (normalized.quote) return normalized.quote;
  return normalized.samples.reduce((latest, row) => {
    if (!latest) return row;
    const latestTime = latest._collected_ms ?? -Infinity;
    const rowTime = row._collected_ms ?? -Infinity;
    return rowTime >= latestTime ? row : latest;
  }, null);
}

function adoptPayload(normalized, source) {
  mergeSamples(normalized.samples);
  const incomingQuote = newestQuoteFrom(normalized);
  const incomingCollected = normalized.collection._collected_ms ?? incomingQuote?._collected_ms ?? -Infinity;
  const currentCollected = state.latest?.collection?._collected_ms ?? state.latest?.quote?._collected_ms ?? -Infinity;
  const incomingGenerated = normalized._generated_ms ?? -Infinity;
  const currentGenerated = state.latest?._generated_ms ?? -Infinity;
  const incomingRelayReceived = normalized.relay?._received_ms ?? -Infinity;
  const currentRelayReceived = state.latest?.relay?._received_ms ?? -Infinity;
  const shouldAdopt = shouldAdoptLatest({
    source,
    hasLatest: state.latest !== null,
    hasRelay: normalized.relay !== null,
    currentSource: state.latestSource,
    incomingRelayReceived,
    currentRelayReceived,
    incomingGenerated,
    currentGenerated,
    incomingCollected,
    currentCollected,
  });

  if (shouldAdopt) {
    state.latest = { ...normalized, quote: incomingQuote };
    state.latestSource = source;
    state.latestReceiptMonotonicMs = normalized._received_monotonic_ms
      ?? window.performance.now();
  }
}

async function fetchJson(url, { cacheBust = false } = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const requestUrl = cacheBust
      ? `${url}${url.includes("?") ? "&" : "?"}v=${Date.now()}`
      : url;
    const response = await fetch(requestUrl, {
      cache: "no-store",
      credentials: "omit",
      mode: "cors",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    const receivedMonotonicMs = window.performance.now();
    return { ...normalizePayload(body), _received_monotonic_ms: receivedMonotonicMs };
  } finally {
    window.clearTimeout(timeout);
  }
}

async function fetchStaticFallback() {
  state.lastStaticAttemptAt = Date.now();
  try {
    const normalized = await fetchJson(STATIC_HISTORY_URL, { cacheBust: true });
    adoptPayload(normalized, "static");
    return true;
  } catch {
    return false;
  }
}

async function bootstrap() {
  state.fullRefreshRunning = true;
  await fetchStaticFallback();
  if (LIVE_HISTORY_URL) {
    try {
      const normalized = await fetchJson(LIVE_HISTORY_URL);
      adoptPayload(normalized, "history");
    } catch {
      state.transportError = true;
    }
  }
  if (LIVE_LATEST_URL) {
    try {
      const normalized = await fetchJson(LIVE_LATEST_URL);
      if (normalized.relay === null) throw new Error("live relay metadata is required");
      state.liveTransportOk = true;
      state.transportError = false;
      adoptPayload(normalized, "live");
    } catch {
      state.liveTransportOk = false;
      state.transportError = true;
    }
  }
  state.fullRefreshRunning = false;
  render();
}

async function pollLatest() {
  if (state.latestPollRunning) return;
  if (!LIVE_LATEST_URL) {
    if (Date.now() - state.lastStaticAttemptAt >= STATIC_RETRY_MS) await refreshStaticHistory();
    return;
  }
  state.latestPollRunning = true;
  try {
    const normalized = await fetchJson(LIVE_LATEST_URL);
    if (normalized.relay === null) throw new Error("live relay metadata is required");
    state.liveTransportOk = true;
    state.transportError = false;
    adoptPayload(normalized, "live");
  } catch {
    state.liveTransportOk = false;
    state.transportError = true;
    if (Date.now() - state.lastStaticAttemptAt >= STATIC_RETRY_MS) await fetchStaticFallback();
  } finally {
    state.latestPollRunning = false;
    render();
  }
}

async function refreshFullHistory() {
  if (state.fullRefreshRunning || !LIVE_HISTORY_URL) return;
  state.fullRefreshRunning = true;
  try {
    const normalized = await fetchJson(LIVE_HISTORY_URL);
    adoptPayload(normalized, "history");
  } catch {
    await fetchStaticFallback();
  } finally {
    state.fullRefreshRunning = false;
    render();
  }
}

async function refreshStaticHistory() {
  if (state.staticRefreshRunning) return;
  state.staticRefreshRunning = true;
  try {
    await fetchStaticFallback();
  } finally {
    state.staticRefreshRunning = false;
    render();
  }
}

function latestQuote() {
  return state.latest?.quote ?? null;
}

function quoteDigits() {
  const candidates = [latestQuote(), ...state.samples.slice().reverse()];
  const digits = candidates.find(row => row && Number.isInteger(row.digits))?.digits;
  return Number.isInteger(digits) ? digits : 5;
}

function formatPrice(value, digits = quoteDigits()) {
  if (!isFiniteNumber(value)) return "Unavailable";
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: false,
  }).format(value);
}

function formatSpread(value) {
  if (!isFiniteNumber(value)) return "Unavailable";
  const digits = Math.min(10, Math.max(quoteDigits(), 2));
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: false,
  }).format(value);
}

function formatPoints(value) {
  if (!isFiniteNumber(value)) return "Unavailable";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2, useGrouping: false }).format(value);
}

function formatCollectorTime(value) {
  const milliseconds = parseTimestamp(value);
  return milliseconds === null
    ? "Unavailable"
    : `${utcDate.format(milliseconds).replace(",", "")} · collector host clock`;
}

function formatMt5Reported(value) {
  if (typeof value !== "string" || !value) return "Unavailable";
  return `${value} · MT5-reported, time basis unverified`;
}

function formatSignedOffset(seconds) {
  if (!isFiniteNumber(seconds)) return "Offset unavailable";
  const sign = seconds >= 0 ? "+" : "−";
  let remaining = Math.round(Math.abs(seconds));
  const hours = Math.floor(remaining / 3600);
  remaining -= hours * 3600;
  const minutes = Math.floor(remaining / 60);
  const tail = remaining - minutes * 60;
  const parts = [];
  if (hours) parts.push(`${hours}h`);
  if (minutes || hours) parts.push(`${minutes}m`);
  parts.push(`${tail}s`);
  return `${sign}${parts.join(" ")} vs collector`;
}

function formatAge(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "Unavailable";
  let seconds = Math.max(0, Math.floor(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h ago`;
}

function formatDuration(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "Unavailable";
  const seconds = Math.round(milliseconds / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function setTone(node, tone) {
  node.classList.remove("positive", "negative", "warning");
  if (tone) node.classList.add(tone);
}

function visibleSamples() {
  const timed = state.samples.filter(sample => sample._collected_ms !== null);
  if (!timed.length) return [];
  const anchor = timed.reduce((latest, sample) => Math.max(latest, sample._collected_ms), -Infinity);
  const cutoff = anchor - state.rangeMs;
  return timed.filter(sample => sample._collected_ms >= cutoff && sample._collected_ms <= anchor);
}

function renderLatestQuote() {
  const quote = latestQuote();
  el("latest-bid").textContent = formatPrice(quote?.bid);
  el("latest-mid").textContent = formatPrice(quote?.mid);
  el("latest-ask").textContent = formatPrice(quote?.ask);
  el("latest-spread").textContent = formatSpread(quote?.spread);
  el("latest-spread-points").textContent = isFiniteNumber(quote?.spread_points)
    ? `${formatPoints(quote.spread_points)} points`
    : "Points unavailable";
}

function collectionView() {
  const collection = state.latest?.collection ?? null;
  const collectedMs = collection?._collected_ms ?? latestQuote()?._collected_ms ?? null;
  const backendAgeMs = isFiniteNumber(collection?.collection_age_seconds)
    ? collection.collection_age_seconds * 1000
    : null;
  const sinceReceiptMs = state.latestReceiptMonotonicMs === null
    ? null
    : Math.max(0, window.performance.now() - state.latestReceiptMonotonicMs);
  const ageMs = backendAgeMs === null
    ? null
    : backendAgeMs + (sinceReceiptMs ?? 0);
  const staleAfterMs = (collection?.source_stale_after_seconds ?? DEFAULT_STALE_SECONDS) * 1000;
  const status = collection?.status ?? "unavailable";
  const connected = collection?.connected;
  const fresh = ageMs !== null && ageMs <= staleAfterMs;
  const healthy = isHealthyLive({
    fresh,
    status,
    connected,
    latestSource: state.latestSource,
    liveTransportOk: state.liveTransportOk,
    hasRelay: state.latest?.relay !== null && state.latest?.relay !== undefined,
  });
  return { collection, collectedMs, backendAgeMs, sinceReceiptMs, ageMs, staleAfterMs, status, connected, fresh, healthy };
}

function renderFreshness() {
  const view = collectionView();
  const quote = latestQuote();

  const collectorState = el("collector-state");
  if (!view.collection || view.collectedMs === null) {
    collectorState.textContent = "Unavailable";
    setTone(collectorState, "negative");
  } else if (view.healthy) {
    collectorState.textContent = "Live";
    setTone(collectorState, "positive");
  } else if (!view.fresh) {
    collectorState.textContent = "Stale";
    setTone(collectorState, "warning");
  } else if (view.status === "live" && !view.healthy) {
    collectorState.textContent = "Last known live";
    setTone(collectorState, "warning");
  } else {
    const labels = {
      disconnected: "Disconnected",
      no_data: "No data",
      stale: "Stale",
      degraded: "Degraded",
      unavailable: "Unavailable",
    };
    collectorState.textContent = labels[view.status] ?? "Unavailable";
    setTone(collectorState, view.status === "disconnected" || view.status === "unavailable" ? "negative" : "warning");
  }
  const ageBasis = state.latestSource === "static"
    ? "age at static snapshot plus time since load"
    : "backend age plus time since response";
  el("collector-age").textContent = view.ageMs === null
    ? "Backend collection age unavailable"
    : `${formatAge(view.ageMs)} · ${ageBasis}`;

  const tickState = el("tick-state");
  if (!quote?.tick_at) {
    tickState.textContent = "Unavailable";
    setTone(tickState, "negative");
    el("tick-age").textContent = "No MT5-reported timestamp";
  } else if (quote._mt5_reported_offset_material) {
    tickState.textContent = "Clock offset detected";
    setTone(tickState, "warning");
    el("tick-age").textContent = formatSignedOffset(quote._mt5_reported_offset_seconds);
  } else {
    tickState.textContent = "MT5 timestamp retained";
    setTone(tickState, null);
    el("tick-age").textContent = formatSignedOffset(quote._mt5_reported_offset_seconds);
  }

  const transport = el("transport-state");
  if (!LIVE_ORIGIN && state.latest) {
    transport.textContent = "Static publication";
    setTone(transport, "warning");
    el("transport-detail").textContent = "Real-time relay not configured";
  } else if (state.liveTransportOk === true) {
    transport.textContent = "Live endpoint";
    setTone(transport, "positive");
    el("transport-detail").textContent = "10-second polling active";
  } else if (state.latestSource === "static") {
    transport.textContent = "Static fallback";
    setTone(transport, "warning");
    el("transport-detail").textContent = "Live endpoint unavailable";
  } else if (state.latest) {
    transport.textContent = "Last good retained";
    setTone(transport, "warning");
    el("transport-detail").textContent = "Live endpoint unavailable";
  } else {
    transport.textContent = state.liveTransportOk === null ? "Connecting" : "Unavailable";
    setTone(transport, state.liveTransportOk === null ? "warning" : "negative");
    el("transport-detail").textContent = "No verified response";
  }

  const pill = el("status-pill");
  if (state.liveTransportOk === true && view.healthy) {
    pill.className = "status-pill status-live";
    pill.innerHTML = '<span class="status-dot"></span>Live quote feed';
  } else if (!state.latest) {
    pill.className = `status-pill ${state.liveTransportOk === null ? "status-unknown" : "status-error"}`;
    pill.innerHTML = `<span class="status-dot"></span>${state.liveTransportOk === null ? "Connecting" : "Feed unavailable"}`;
  } else {
    pill.className = "status-pill status-warn";
    const label = state.latestSource === "static" ? "Static fallback" : state.liveTransportOk === false ? "Live feed interrupted" : "Collector delayed";
    pill.innerHTML = `<span class="status-dot"></span>${escapeHtml(label)}`;
  }

  el("updated-at").textContent = view.collection?.collected_at
    ? `Collector reported ${formatCollectorTime(view.collection.collected_at)}`
    : "No quote received";

  const alert = el("alert");
  if (!state.latest) {
    alert.hidden = false;
    alert.className = "alert alert-error";
    alert.innerHTML = "<strong>Quote feed unavailable.</strong><span>No verified FixedStep0.5 values are being substituted.</span>";
  } else if (state.liveTransportOk === false) {
    alert.hidden = false;
    alert.className = "alert alert-warn";
    const detail = state.latestSource === "static"
      ? "The dashboard is showing the timestamped static fallback and will retry the live endpoint automatically."
      : "The last verified live quote is retained while the dashboard retries automatically.";
    alert.innerHTML = `<strong>Live endpoint unavailable.</strong><span>${escapeHtml(detail)}</span>`;
  } else if (!view.healthy) {
    alert.hidden = false;
    alert.className = "alert alert-warn";
    alert.innerHTML = "<strong>Collector data is not fresh.</strong><span>Freshness uses the backend collection age plus elapsed time since this response; the MT5-reported timestamp is excluded.</span>";
  } else if (quote?._mt5_reported_offset_material) {
    alert.hidden = false;
    alert.className = "alert alert-warn";
    alert.innerHTML = "<strong>MT5 and collector clocks differ.</strong><span>The raw MT5 timestamp is preserved for audit, but its time basis is unverified and it is excluded from freshness, ranges and chart axes.</span>";
  } else {
    alert.hidden = true;
  }
}

function renderStatistics(rows) {
  const mids = rows.map(row => row.mid).filter(isFiniteNumber);
  const collectionTimes = rows.map(row => row._collected_ms).filter(Number.isFinite);
  el("visible-samples").textContent = String(rows.length);
  if (collectionTimes.length) {
    const start = Math.min(...collectionTimes);
    const end = Math.max(...collectionTimes);
    el("coverage-note").textContent = `${formatCollectorTime(new Date(start).toISOString())} → ${formatCollectorTime(new Date(end).toISOString())}`;
    el("range-span").textContent = formatDuration(end - start);
    el("range-note").textContent = `Range ends at collector host-clock observation ${shortUtc.format(end)}.`;
  } else {
    el("coverage-note").textContent = "No collector-time coverage";
    el("range-span").textContent = "Unavailable";
    el("range-note").textContent = "Range follows collector first-observation time.";
  }

  if (mids.length) {
    const low = Math.min(...mids);
    const high = Math.max(...mids);
    const change = mids[mids.length - 1] - mids[0];
    const changePct = mids[0] === 0 ? null : change / mids[0] * 100;
    el("range-low").textContent = formatPrice(low);
    el("range-high").textContent = formatPrice(high);
    const changeNode = el("range-change");
    changeNode.textContent = `${change >= 0 ? "+" : ""}${formatPrice(change)}`;
    setTone(changeNode, change > 0 ? "positive" : change < 0 ? "negative" : null);
    el("range-change-pct").textContent = changePct === null ? "" : `${changePct >= 0 ? "+" : ""}${changePct.toFixed(3)}%`;
  } else {
    for (const id of ["range-low", "range-high", "range-change"]) el(id).textContent = "Unavailable";
    el("range-change-pct").textContent = "";
    setTone(el("range-change"), null);
  }

  const usePoints = rows.some(row => isFiniteNumber(row.spread_points));
  const spreads = rows
    .map(row => usePoints ? row.spread_points : row.spread)
    .filter(isFiniteNumber);
  el("spread-unit").textContent = usePoints ? "Points" : "Price units";
  if (spreads.length) {
    const formatter = usePoints ? formatPoints : formatSpread;
    el("spread-min").textContent = formatter(Math.min(...spreads));
    el("spread-avg").textContent = formatter(spreads.reduce((sum, value) => sum + value, 0) / spreads.length);
    el("spread-max").textContent = formatter(Math.max(...spreads));
  } else {
    for (const id of ["spread-min", "spread-avg", "spread-max"]) el(id).textContent = "Unavailable";
  }
}

function prepareCanvas(id, hasData) {
  const canvas = el(id);
  const wrapper = canvas.parentElement;
  wrapper.classList.toggle("has-data", hasData);
  const rectangle = canvas.getBoundingClientRect();
  const width = Math.max(280, Math.round(rectangle.width));
  const height = Math.max(170, Math.round(rectangle.height));
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const context = canvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);
  delete canvas._chart;
  return { canvas, context, width, height };
}

function chartGeometry(rows, values, width, height, { includeZero = false } = {}) {
  const pad = { left: width < 480 ? 58 : 72, right: 16, top: 14, bottom: 35 };
  const plotWidth = Math.max(1, width - pad.left - pad.right);
  const plotHeight = Math.max(1, height - pad.top - pad.bottom);
  const times = rows.map(row => row._collected_ms);
  const start = Math.min(...times);
  const end = Math.max(...times);
  let minimum = includeZero ? Math.min(0, ...values) : Math.min(...values);
  let maximum = includeZero ? Math.max(0, ...values) : Math.max(...values);
  if (minimum === maximum) {
    const point = latestQuote()?.point;
    const delta = isFiniteNumber(point) ? Math.max(point * 2, Math.abs(minimum || 1) * .0001) : Math.abs(minimum || 1) * .001;
    minimum -= delta;
    maximum += delta;
  } else {
    const margin = (maximum - minimum) * .08;
    minimum -= margin;
    maximum += margin;
  }
  const x = index => end > start
    ? pad.left + (times[index] - start) / (end - start) * plotWidth
    : pad.left + plotWidth / 2;
  const y = value => pad.top + (maximum - value) / (maximum - minimum) * plotHeight;
  return { pad, plotWidth, plotHeight, times, start, end, minimum, maximum, x, y };
}

function drawAxes(context, geometry, rows, width, height, formatter) {
  context.font = "10px Inter, system-ui, sans-serif";
  context.lineWidth = 1;
  context.strokeStyle = "rgba(142,160,183,.16)";
  context.fillStyle = "#8ea0b7";
  context.textBaseline = "middle";
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = geometry.maximum - (geometry.maximum - geometry.minimum) * tick / 4;
    const y = geometry.pad.top + geometry.plotHeight * tick / 4;
    context.beginPath();
    context.moveTo(geometry.pad.left, y);
    context.lineTo(width - geometry.pad.right, y);
    context.stroke();
    context.textAlign = "right";
    context.fillText(formatter(value), geometry.pad.left - 9, y);
  }

  context.textBaseline = "alphabetic";
  const indexes = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])];
  for (const index of indexes) {
    context.textAlign = index === 0 ? "left" : index === rows.length - 1 ? "right" : "center";
    context.fillText(shortUtc.format(rows[index]._collected_ms), geometry.x(index), height - 10);
  }
  context.textAlign = "left";
}

function strokeSeries(context, rows, geometry, field, color, width) {
  context.beginPath();
  context.strokeStyle = color;
  context.lineWidth = width;
  context.lineJoin = "round";
  context.lineCap = "round";
  let started = false;
  let count = 0;
  rows.forEach((row, index) => {
    const value = row[field];
    if (!isFiniteNumber(value)) {
      started = false;
      return;
    }
    const x = geometry.x(index);
    const y = geometry.y(value);
    if (!started) {
      context.moveTo(x, y);
      started = true;
    } else {
      context.lineTo(x, y);
    }
    count += 1;
  });
  context.stroke();
  if (count === 1) {
    const index = rows.findIndex(row => isFiniteNumber(row[field]));
    context.beginPath();
    context.fillStyle = color;
    context.arc(geometry.x(index), geometry.y(rows[index][field]), 3, 0, Math.PI * 2);
    context.fill();
  }
}

function drawPriceChart(rows) {
  const chartRows = rows.filter(row => [row.bid, row.ask, row.mid].some(isFiniteNumber));
  const prepared = prepareCanvas("quote-chart", chartRows.length > 0);
  const summary = el("quote-chart-summary");
  if (!chartRows.length) {
    summary.textContent = "No verified FixedStep0.5 tick history is available for the selected collector-time range.";
    return;
  }
  const values = chartRows.flatMap(row => [row.bid, row.ask, row.mid].filter(isFiniteNumber));
  const geometry = chartGeometry(chartRows, values, prepared.width, prepared.height);
  drawAxes(prepared.context, geometry, chartRows, prepared.width, prepared.height, value => formatPrice(value));

  const completeBand = chartRows.every(row => isFiniteNumber(row.bid) && isFiniteNumber(row.ask));
  if (completeBand) {
    const context = prepared.context;
    context.beginPath();
    chartRows.forEach((row, index) => {
      const x = geometry.x(index);
      const y = geometry.y(row.ask);
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    for (let index = chartRows.length - 1; index >= 0; index -= 1) {
      context.lineTo(geometry.x(index), geometry.y(chartRows[index].bid));
    }
    context.closePath();
    context.fillStyle = "rgba(184, 156, 255, .11)";
    context.fill();
  }

  strokeSeries(prepared.context, chartRows, geometry, "bid", "#52a7ff", 1.5);
  strokeSeries(prepared.context, chartRows, geometry, "ask", "#ffbd69", 1.5);
  strokeSeries(prepared.context, chartRows, geometry, "mid", "#64e4d2", 2.3);
  prepared.canvas._chart = { kind: "price", rows: chartRows, geometry };

  const mids = chartRows.map(row => row.mid).filter(isFiniteNumber);
  summary.textContent = `${chartRows.length} distinct broker-tick observations first collected from ${formatCollectorTime(chartRows[0].collected_at)} to ${formatCollectorTime(chartRows[chartRows.length - 1].collected_at)}. Mid price minimum ${formatPrice(Math.min(...mids))} and maximum ${formatPrice(Math.max(...mids))}.`;
}

function drawSpreadChart(rows) {
  const usePoints = rows.some(row => isFiniteNumber(row.spread_points));
  const field = usePoints ? "spread_points" : "spread";
  const chartRows = rows.filter(row => isFiniteNumber(row[field]));
  const prepared = prepareCanvas("spread-chart", chartRows.length > 0);
  const summary = el("spread-chart-summary");
  if (!chartRows.length) {
    summary.textContent = "No verified spread history is available for the selected collector-time range.";
    return;
  }
  const values = chartRows.map(row => row[field]);
  const geometry = chartGeometry(chartRows, values, prepared.width, prepared.height, { includeZero: true });
  const formatter = usePoints ? formatPoints : formatSpread;
  drawAxes(prepared.context, geometry, chartRows, prepared.width, prepared.height, formatter);
  strokeSeries(prepared.context, chartRows, geometry, field, "#b89cff", 2);
  prepared.canvas._chart = { kind: "spread", rows: chartRows, geometry, spreadField: field, spreadFormatter: formatter };
  summary.textContent = `${chartRows.length} distinct broker-tick observations. Spread minimum ${formatter(Math.min(...values))}, average ${formatter(values.reduce((sum, value) => sum + value, 0) / values.length)}, and maximum ${formatter(Math.max(...values))} ${usePoints ? "points" : "price units"}.`;
}

function renderCharts() {
  const rows = visibleSamples();
  renderStatistics(rows);
  drawPriceChart(rows);
  drawSpreadChart(rows);
  el("chart-tooltip").hidden = true;
}

function render() {
  renderLatestQuote();
  renderFreshness();
  renderCharts();
}

function closestChartRow(chart, localX) {
  let closest = 0;
  let distance = Infinity;
  chart.rows.forEach((row, index) => {
    const current = Math.abs(chart.geometry.x(index) - localX);
    if (current < distance) {
      closest = index;
      distance = current;
    }
  });
  return chart.rows[closest];
}

function showTooltip(canvas, event) {
  const chart = canvas._chart;
  if (!chart) return;
  const rectangle = canvas.getBoundingClientRect();
  const row = closestChartRow(chart, event.clientX - rectangle.left);
  const tooltip = el("chart-tooltip");
  let details;
  if (chart.kind === "price") {
    details = [
      ["Bid", formatPrice(row.bid)],
      ["Mid", formatPrice(row.mid)],
      ["Ask", formatPrice(row.ask)],
      ["Spread", formatSpread(row.spread)],
      ["Spread points", formatPoints(row.spread_points)],
    ];
  } else {
    details = [["Spread", chart.spreadFormatter(row[chart.spreadField])]];
  }
  tooltip.innerHTML = [
    `<strong>${escapeHtml(formatCollectorTime(row.collected_at))}</strong>`,
    ...details.map(([label, value]) => `<span class="tooltip-label">${escapeHtml(label)}:</span> ${escapeHtml(value)}`),
    `<span class="tooltip-label">First collected:</span> ${escapeHtml(formatCollectorTime(row.collected_at))}`,
    `<span class="tooltip-label">MT5 reported:</span> ${escapeHtml(formatMt5Reported(row.tick_at))}`,
  ].join("<br>");
  tooltip.hidden = false;
  tooltip.style.left = `${Math.min(window.innerWidth - 304, event.clientX + 14)}px`;
  tooltip.style.top = `${Math.max(8, Math.min(window.innerHeight - 190, event.clientY - 22))}px`;
}

for (const canvas of document.querySelectorAll("canvas")) {
  canvas.addEventListener("pointermove", event => showTooltip(canvas, event));
  canvas.addEventListener("pointerleave", () => { el("chart-tooltip").hidden = true; });
}

for (const button of document.querySelectorAll("[data-range-ms]")) {
  button.addEventListener("click", () => {
    for (const other of document.querySelectorAll("[data-range-ms]")) {
      const active = other === button;
      other.classList.toggle("is-active", active);
      other.setAttribute("aria-pressed", String(active));
    }
    state.rangeMs = Number(button.dataset.rangeMs);
    renderCharts();
  });
}

window.addEventListener("resize", () => {
  window.clearTimeout(window._fixedStepResizeTimer);
  window._fixedStepResizeTimer = window.setTimeout(renderCharts, 150);
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) pollLatest();
});

window.setInterval(() => {
  if (!document.hidden) pollLatest();
}, POLL_MS);
window.setInterval(refreshFullHistory, HISTORY_REFRESH_MS);
window.setInterval(refreshStaticHistory, STATIC_REFRESH_MS);
window.setInterval(() => {
  if (state.latest) renderFreshness();
}, 1_000);

bootstrap();
