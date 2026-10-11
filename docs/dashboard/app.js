"use strict";

const DATA_ROOT = "../ai/data/";
const REFRESH_MS = 60_000;
const state = { latest: {}, equityData: {}, tradeData: {}, statsData: {}, strategyData: {}, equity: [], closed: [], open: [], stats: {}, strategy: {}, range: "all", from: null, to: null, zoom: new Map(), transportError: null };
const chartDefinitions = new Map();
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const utcDate = new Intl.DateTimeFormat(undefined, { timeZone: "UTC", year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const shortUtc = new Intl.DateTimeFormat(undefined, { timeZone: "UTC", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });

function el(id) { return document.getElementById(id); }
function has(value) { return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)); }
function first(...values) { return values.find(value => value !== null && value !== undefined && value !== ""); }
function parseTime(value) { const ms = Date.parse(value); return Number.isFinite(ms) ? ms : null; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
function currencyCode() {
  const code = state.latest?.account?.currency;
  return typeof code === "string" && /^[A-Z]{3}$/.test(code) ? code : null;
}
function fmtMoney(value) {
  if (!has(value)) return "Unavailable";
  const code = currencyCode();
  if (!code) return `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })} account units`;
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency: code, maximumFractionDigits: 2 }).format(Number(value)); }
  catch { return `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${code}`; }
}
function fmtNumber(value, digits = 2) { return has(value) ? Number(value).toLocaleString(undefined, { maximumFractionDigits: digits }) : "Unavailable"; }
function fmtPrice(value, row = {}) {
  if (!has(value)) return "Unavailable";
  const supplied = Number(first(row.digits, row.symbol_digits, row.price_digits));
  const digits = Number.isInteger(supplied) && supplied >= 0 && supplied <= 10 ? supplied : 8;
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, useGrouping: false });
}
function fmtPct(value) { return has(value) ? `${Number(value).toFixed(2)}%` : "Unavailable"; }
function fmtDate(value) { const ms = parseTime(value); return ms === null ? "Unavailable" : utcDate.format(ms).replace(",", "") + " UTC"; }
function duration(seconds) {
  if (!has(seconds)) return "Unavailable";
  let value = Math.max(0, Math.round(Number(seconds)));
  const days = Math.floor(value / 86400); value %= 86400;
  const hours = Math.floor(value / 3600); value %= 3600;
  const minutes = Math.floor(value / 60);
  return [days && `${days}d`, hours && `${hours}h`, minutes && `${minutes}m`].filter(Boolean).join(" ") || "<1m";
}
function colorize(node, value) {
  node.classList.remove("positive", "negative");
  if (has(value) && Number(value) > 0) node.classList.add("positive");
  if (has(value) && Number(value) < 0) node.classList.add("negative");
}
function setValue(id, text, numeric) { const node = el(id); node.textContent = text; colorize(node, numeric); }
function sourceStatus() { return String(first(collectionMeta().status, state.latest.status, "unknown")).toLowerCase(); }
function sourceIsLive() {
  if (state.transportError) return false;
  const collected = parseTime(first(collectionMeta().collected_at, state.latest.source_observed_at, state.latest.collected_at));
  const limit = Number(first(collectionMeta().source_stale_after_seconds, collectionMeta().stale_after_seconds, 180)) * 1000;
  return collected !== null && Date.now() - collected <= limit && ["ok", "live", "connected", "collected", "healthy"].includes(sourceStatus());
}
function recordGrain() { return String(first(state.tradeData.grain, state.latest.meta?.closed_trade_grain, "one closing execution/deal")); }
function isExecutionGrain() { return /execution|deal|partial close/i.test(recordGrain()); }
function recordNoun(plural = false) { return isExecutionGrain() ? (plural ? "closing executions" : "closing execution") : (plural ? "closed trades" : "closed trade"); }

async function fetchJson(name) {
  const bucket = Math.floor(Date.now() / REFRESH_MS);
  const response = await fetch(`${DATA_ROOT}${name}?v=${bucket}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
  return response.json();
}

async function refresh() {
  try {
    const [latest, equity, trades, stats, strategy] = await Promise.all([
      fetchJson("latest.json"), fetchJson("equity.json"), fetchJson("trades.json"), fetchJson("statistics.json"), fetchJson("strategy.json")
    ]);
    const payloads = [latest, equity, trades, stats, strategy];
    const snapshotIds = new Set(payloads.map(payload => payload?.snapshot_id).filter(Boolean));
    if (snapshotIds.size !== 1 || payloads.some(payload => !payload?.snapshot_id)) {
      throw new Error("The public data files do not belong to one complete snapshot");
    }
    state.transportError = null;
    state.latest = latest || {};
    state.equityData = equity || {};
    state.tradeData = trades || {};
    state.statsData = stats || {};
    state.strategyData = strategy || {};
    state.equity = Array.isArray(equity) ? equity : equity.samples || equity.equity || equity.data?.samples || [];
    state.closed = trades.closed_trades || trades.trades || trades.data?.closed_trades || trades.data?.trades || [];
    state.open = trades.open_positions || trades.data?.open_positions || [];
    state.stats = stats.statistics || stats.data?.statistics || stats.data || stats || {};
    state.strategy = strategy.strategy || strategy.data?.strategy || strategy.data || strategy || {};
    render();
  } catch (error) {
    state.transportError = error instanceof Error ? error.message : String(error);
    renderKpis();
    renderOpenPositions();
    renderTransportError(error);
  }
}

function collectionMeta() { return state.latest.collection || {}; }
function renderTransportError(error) {
  const alert = el("alert");
  alert.hidden = false;
  alert.className = "alert alert-error";
  alert.innerHTML = `<strong>Public snapshot unavailable.</strong><span>${escapeHtml(error?.message || state.transportError)}. The dashboard has not substituted sample values.</span>`;
  const pill = el("status-pill"); pill.className = "status-pill status-error"; pill.innerHTML = '<span class="status-dot"></span>Snapshot unavailable';
}

function renderFreshness() {
  if (state.transportError) {
    renderTransportError(new Error(state.transportError));
    return;
  }
  const meta = collectionMeta();
  const collectedAt = first(meta.collected_at, state.latest.source_observed_at, state.latest.collected_at);
  const publishedAt = first(meta.published_at, state.latest.published_at, state.latest.generated_at);
  const collectedMs = parseTime(collectedAt);
  const publishedMs = parseTime(publishedAt);
  const now = Date.now();
  const sourceLimit = Number(first(meta.source_stale_after_seconds, meta.stale_after_seconds, 180)) * 1000;
  const publicLimit = Number(first(meta.public_stale_after_seconds, 1200)) * 1000;
  const status = String(first(meta.status, state.latest.status, "unknown")).toLowerCase();
  const sourceStale = collectedMs === null || now - collectedMs > sourceLimit || ["no_data", "disconnected", "error", "stale", "source_unreachable", "unavailable"].includes(status);
  const publicationStale = publishedMs === null || now - publishedMs > publicLimit;
  const live = !sourceStale && !publicationStale && ["ok", "live", "connected", "collected", "healthy"].includes(status);
  const pill = el("status-pill");
  pill.className = `status-pill ${live ? "status-live" : status.includes("error") ? "status-error" : "status-warn"}`;
  const statusLabel = live ? "Live read-only feed" : publicationStale ? "Stale public snapshot" : sourceStale ? "Stale or unavailable source" : (status.replaceAll("_", " ") || "Source unavailable");
  pill.innerHTML = `<span class="status-dot"></span>${escapeHtml(statusLabel)}`;
  el("updated-at").textContent = collectedMs === null ? "No collection received" : `Collected ${fmtDate(collectedAt)}`;
  const lag = collectedMs === null ? null : Math.max(0, (now - collectedMs) / 1000);
  el("source-lag").textContent = lag === null ? "Unavailable" : duration(lag);
  const alert = el("alert");
  if (live) {
    alert.hidden = true;
  } else {
    alert.hidden = false;
    alert.className = sourceStale || publicationStale ? "alert alert-warn" : "alert";
    const reason = first(meta.error?.message, meta.error, state.latest.message,
      sourceStale ? "The collector source is disconnected or older than its freshness threshold." : "The public Pages snapshot is older than its publication threshold.");
    alert.innerHTML = `<strong>${publicationStale ? "Public snapshot is stale." : "Source is not live."}</strong><span>${escapeHtml(reason)}</span>`;
  }
}

function renderKpis() {
  const account = state.latest.account || {};
  const periods = state.latest.periods || {};
  const drawdown = state.latest.drawdown || {};
  setValue("kpi-equity", fmtMoney(account.equity), account.equity);
  setValue("kpi-balance", fmtMoney(account.balance), account.balance);
  const observationLabel = sourceIsLive() ? "Account scope" : "Last observed; source not live";
  el("kpi-equity-note").textContent = has(account.equity) ? `${observationLabel} · ${fmtDate(first(collectionMeta().collected_at, state.latest.source_observed_at))}` : "No verified sample";
  el("kpi-balance-note").textContent = has(account.balance) ? `${observationLabel} · ${fmtDate(first(collectionMeta().collected_at, state.latest.source_observed_at))}` : "No verified sample";
  const daily = first(periods.daily?.realized_pnl, periods.day?.realized_pnl);
  const weekly = periods.weekly?.realized_pnl;
  const monthly = periods.monthly?.realized_pnl;
  const total = first(periods.all_time?.realized_pnl, periods.total?.realized_pnl);
  setValue("kpi-daily", fmtMoney(daily), daily);
  setValue("kpi-weekly", fmtMoney(weekly), weekly);
  setValue("kpi-monthly", fmtMoney(monthly), monthly);
  setValue("kpi-total", fmtMoney(total), total);
  const net = first(account.net_return, periods.all_time?.net_return);
  const netPct = first(account.net_return_pct, periods.all_time?.net_return_pct);
  setValue("kpi-return", has(net) ? fmtMoney(net) : fmtPct(netPct), has(net) ? net : netPct);
  el("kpi-return-note").textContent = has(netPct) ? `${fmtPct(netPct)} from verified initial capital` : "Initial capital unavailable";
  const currentDd = first(drawdown.current_pct, drawdown.percentage);
  setValue("kpi-drawdown", fmtPct(currentDd), has(currentDd) ? -Math.abs(Number(currentDd)) : null);
  el("kpi-drawdown-note").textContent = has(drawdown.current_abs) ? `${fmtMoney(drawdown.current_abs)} below peak` : "Peak-to-current equity";
  const maximumDd = first(drawdown.max_pct, drawdown.maximum_pct);
  setValue("kpi-max-drawdown", fmtPct(maximumDd), has(maximumDd) ? -Math.abs(Number(maximumDd)) : null);
  el("kpi-max-drawdown-note").textContent = has(drawdown.max_abs) ? `${fmtMoney(drawdown.max_abs)} maximum observed` : "Observed equity history";
  setValue("kpi-initial", fmtMoney(account.initial_capital), null);
  const sourceVerified = sourceIsLive();
  const openCount = sourceVerified ? first(state.latest.counts?.open_positions, Array.isArray(state.open) ? state.open.length : null) : null;
  setValue("kpi-open", has(openCount) ? fmtNumber(openCount, 0) : "Unavailable", null);
  el("kpi-open-note").textContent = sourceVerified ? "Current account position count" : "Unavailable while the source is not live";
  const rawStrategyStatus = first(state.strategy.status, state.latest.strategy?.status);
  el("strategy-status").textContent = rawStrategyStatus ? `${sourceVerified ? "" : "Last observed: "}${rawStrategyStatus}` : "Unavailable";
  el("strategy-symbol").textContent = first(state.strategy.symbol, state.latest.strategy?.symbol, "Unavailable");
  const step = first(state.strategy.step, state.strategy.level, state.latest.strategy?.step, state.latest.strategy?.level);
  el("strategy-step").textContent = step === undefined || step === null ? "Unavailable" : String(step);
  setValue("strategy-floating", fmtMoney(account.unrealized_pnl), account.unrealized_pnl);
  el("strategy-floating-label").textContent = sourceVerified ? "Account unrealized P&L" : "Last observed account unrealized P&L";
  const accountScope = first(state.latest.scope?.account, "Account scope unavailable");
  const tradeScope = first(state.latest.scope?.trades, "Execution-record scope unavailable");
  el("scope-heading").textContent = isExecutionGrain() ? "Account metrics + execution records" : "Account metrics + closed trades";
  const coverage = state.latest.coverage || {};
  const accountCoverage = coverage.account_start_at ? ` Account history: ${fmtDate(coverage.account_start_at)} to ${fmtDate(coverage.account_end_at)}.` : " No account-history coverage yet.";
  const tradeCoverage = coverage.trade_start_at ? ` Execution history: ${fmtDate(coverage.trade_start_at)} to ${fmtDate(coverage.trade_end_at)}.` : " No execution-history coverage yet.";
  el("scope-detail").textContent = `${accountScope}; ${tradeScope}. These scopes are not assumed to be identical.${accountCoverage}${tradeCoverage}`;
  el("closed-records-title").textContent = isExecutionGrain() ? "Closing executions" : "Closed trades";
  el("closed-records-grain").textContent = `Published grain: ${recordGrain()}.`;
  el("statistics-scope").textContent = `Statistics use the selected date range and published grain: ${recordGrain()}.`;
}

function filteredByRange(rows, timeFields) {
  const now = Date.now();
  let start = null, end = state.to || now;
  if (state.range === "24h") start = now - 86400e3;
  if (state.range === "7d") start = now - 7 * 86400e3;
  if (state.range === "30d") start = now - 30 * 86400e3;
  if (state.range === "custom") start = state.from;
  return rows.filter(row => {
    const raw = timeFields.map(field => row[field]).find(Boolean);
    const time = parseTime(raw);
    return time !== null && (start === null || time >= start) && (end === null || time <= end);
  }).sort((a, b) => parseTime(timeFields.map(field => a[field]).find(Boolean)) - parseTime(timeFields.map(field => b[field]).find(Boolean)));
}

function rangeStatistics() {
  const trades = filteredByRange(state.closed, ["exit_time","closed_at","time","observed_at"]).filter(trade => has(tradeNet(trade)));
  if (!trades.length) return {};
  const results = trades.map(trade => Number(tradeNet(trade)));
  const wins = results.filter(value => value > 0); const losses = results.filter(value => value < 0);
  let currentWins = 0, currentLosses = 0;
  for (let index = results.length - 1; index >= 0; index--) {
    if (results[index] > 0 && currentLosses === 0) currentWins += 1;
    else if (results[index] < 0 && currentWins === 0) currentLosses += 1;
    else break;
  }
  return {
    win_rate: wins.length + losses.length ? wins.length / (wins.length + losses.length) * 100 : undefined,
    profit_factor: losses.length ? wins.reduce((sum,value) => sum + value,0) / Math.abs(losses.reduce((sum,value) => sum + value,0)) : undefined,
    avg_win: wins.length ? wins.reduce((sum,value) => sum + value,0) / wins.length : undefined,
    avg_loss: losses.length ? losses.reduce((sum,value) => sum + value,0) / losses.length : undefined,
    best_trade: Math.max(...results), worst_trade: Math.min(...results),
    consecutive_wins: currentWins, consecutive_losses: currentLosses
  };
}
function renderStats() {
  const stats = rangeStatistics();
  setValue("stat-win-rate", fmtPct(stats.win_rate), stats.win_rate);
  el("stat-profit-factor").textContent = has(stats.profit_factor) ? fmtNumber(stats.profit_factor) : "Unavailable";
  setValue("stat-avg-win", fmtMoney(stats.avg_win), stats.avg_win);
  setValue("stat-avg-loss", fmtMoney(stats.avg_loss), stats.avg_loss);
  setValue("stat-best", fmtMoney(stats.best_trade), stats.best_trade);
  setValue("stat-worst", fmtMoney(stats.worst_trade), stats.worst_trade);
  el("stat-win-streak").textContent = has(first(stats.consecutive_wins, stats.current_win_streak)) ? fmtNumber(first(stats.consecutive_wins, stats.current_win_streak), 0) : "Unavailable";
  el("stat-loss-streak").textContent = has(first(stats.consecutive_losses, stats.current_loss_streak)) ? fmtNumber(first(stats.consecutive_losses, stats.current_loss_streak), 0) : "Unavailable";
}

function tradeNet(trade) { return first(trade.net_profit, trade.net_pnl, trade.profit); }
function tradeSide(trade) { return String(first(trade.side, trade.direction, trade.type, "")).toLowerCase().replace("position_type_", ""); }
function tradeTime(trade, closed = true) { return first(closed ? trade.exit_time : trade.entry_time, closed ? trade.closed_at : trade.opened_at, trade.time); }
function tradeDuration(trade, open = false) {
  const supplied = first(trade.duration_seconds, trade.duration);
  if (has(supplied)) return duration(supplied);
  const a = parseTime(first(trade.entry_time, trade.opened_at));
  const b = open ? parseTime(first(collectionMeta().collected_at, state.latest.source_observed_at)) : parseTime(first(trade.exit_time, trade.closed_at));
  return a !== null && b !== null ? duration((b - a) / 1000) : "Unavailable";
}

function renderOpenPositions() {
  const rows = [...state.open].sort((a,b) => (parseTime(first(b.entry_time,b.opened_at)) || 0) - (parseTime(first(a.entry_time,a.opened_at)) || 0));
  const live = sourceIsLive();
  el("open-positions-title").textContent = live ? "Open positions" : "Last observed positions (not live)";
  el("open-count").textContent = rows.length ? `${rows.length} ${live ? "current" : "retained"} position record${rows.length === 1 ? "" : "s"}` : (live ? "No open positions observed" : "No live position snapshot");
  el("open-positions").innerHTML = rows.length ? rows.map(row => `<tr>
    <td>${escapeHtml(fmtDate(first(row.entry_time,row.opened_at)))}</td><td>${escapeHtml(first(row.symbol,"Unavailable"))}</td><td>${escapeHtml(tradeSide(row) || "Unavailable")}</td>
    <td>${escapeHtml(fmtNumber(row.volume))}</td><td>${escapeHtml(fmtPrice(first(row.entry_price,row.price_open), row))}</td><td>${escapeHtml(fmtPrice(first(row.current_price,row.price_current), row))}</td>
    <td>${escapeHtml(fmtPrice(first(row.stop_loss,row.sl), row))}</td><td>${escapeHtml(fmtPrice(first(row.take_profit,row.tp), row))}</td>
    <td class="${has(row.unrealized_pnl) && Number(row.unrealized_pnl) < 0 ? "negative" : has(row.unrealized_pnl) && Number(row.unrealized_pnl) > 0 ? "positive" : ""}">${escapeHtml(fmtMoney(row.unrealized_pnl))}</td><td>${escapeHtml(tradeDuration(row, true))}</td>
  </tr>`).join("") : `<tr><td colspan="10" class="empty-cell">${live ? "No open positions were observed in the live account snapshot." : "No current position data is available while the source is not live."}</td></tr>`;
}

function renderTradeFilters() {
  const symbols = [...new Set(state.closed.map(row => row.symbol).filter(Boolean))].sort();
  const current = el("filter-symbol").value;
  el("filter-symbol").innerHTML = '<option value="all">All</option>' + symbols.map(symbol => `<option value="${escapeHtml(symbol)}">${escapeHtml(symbol)}</option>`).join("");
  if (symbols.includes(current)) el("filter-symbol").value = current;
}
function filteredTrades() {
  const side = el("filter-side").value; const symbol = el("filter-symbol").value;
  return filteredByRange(state.closed, ["exit_time","closed_at","time","observed_at"]).filter(trade => {
    const matchesSide = side === "all" || tradeSide(trade).includes(side);
    const matchesSymbol = symbol === "all" || trade.symbol === symbol;
    return matchesSide && matchesSymbol;
  }).sort((a,b) => (parseTime(tradeTime(b)) || 0) - (parseTime(tradeTime(a)) || 0));
}
function renderClosedTrades() {
  const rows = filteredTrades();
  el("trade-count").textContent = `${rows.length.toLocaleString()} ${recordNoun(rows.length !== 1)} shown`;
  el("closed-trades").innerHTML = rows.length ? rows.map(row => { const net = tradeNet(row); return `<tr>
    <td>${escapeHtml(fmtDate(first(row.exit_time,row.closed_at)))}</td><td>${escapeHtml(fmtDate(first(row.entry_time,row.opened_at)))}</td><td>${escapeHtml(first(row.symbol,"Unavailable"))}</td><td>${escapeHtml(tradeSide(row) || "Unavailable")}</td>
    <td>${escapeHtml(fmtNumber(row.volume))}</td><td>${escapeHtml(fmtPrice(first(row.entry_price,row.price_open), row))}</td><td>${escapeHtml(fmtPrice(first(row.exit_price,row.price_close), row))}</td>
    <td>${escapeHtml(fmtPrice(first(row.stop_loss,row.sl), row))}</td><td>${escapeHtml(fmtPrice(first(row.take_profit,row.tp), row))}</td><td>${escapeHtml(fmtMoney(row.commission))}</td><td>${escapeHtml(fmtMoney(row.swap))}</td>
    <td class="${has(net) && Number(net) < 0 ? "negative" : has(net) && Number(net) > 0 ? "positive" : ""}">${escapeHtml(fmtMoney(net))}</td><td>${escapeHtml(tradeDuration(row))}</td>
  </tr>`; }).join("") : `<tr><td colspan="13" class="empty-cell">No verified ${recordNoun(true)} for this selection.</td></tr>`;
}

function chartRows() {
  const equity = filteredByRange(state.equity, ["timestamp","time","collected_at","observed_at"]);
  const trades = filteredByRange(state.closed, ["exit_time","closed_at","time","observed_at"]);
  const cumulative = []; let total = 0;
  for (const trade of trades) { const value = tradeNet(trade); if (has(value)) { total += Number(value); cumulative.push({ timestamp: tradeTime(trade), value: total, trade }); } }
  const steps = state.strategy.progression || state.strategy.step_history || state.strategy.history || [];
  const filteredSteps = filteredByRange(Array.isArray(steps) ? steps : [], ["timestamp","time","changed_at","observed_at"]);
  const dailyMap = new Map();
  for (const trade of trades) {
    const time = parseTime(tradeTime(trade)); const net = tradeNet(trade);
    if (time === null) continue;
    const timestamp = `${new Date(time).toISOString().slice(0, 10)}T00:00:00Z`;
    const bucket = dailyMap.get(timestamp) || { timestamp, realized_pnl: null, count: 0, volume: 0 };
    if (has(net)) bucket.realized_pnl = (has(bucket.realized_pnl) ? Number(bucket.realized_pnl) : 0) + Number(net);
    bucket.count += 1;
    if (has(trade.volume)) bucket.volume += Number(trade.volume);
    dailyMap.set(timestamp, bucket);
  }
  const daily = [...dailyMap.values()].sort((a,b) => parseTime(a.timestamp) - parseTime(b.timestamp));
  return { equity, trades, cumulative, steps: filteredSteps, daily };
}

function setupChart(canvasId, rows, series, options = {}) {
  chartDefinitions.set(canvasId, { rows, series, options });
  drawChart(canvasId);
}
function drawChart(canvasId) {
  const definition = chartDefinitions.get(canvasId); const canvas = el(canvasId); const wrap = canvas.parentElement;
  const rows = definition?.rows || []; wrap.classList.toggle("has-data", rows.length > 0);
  const rect = canvas.getBoundingClientRect(); const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(300, Math.round(rect.width)); const height = Math.max(180, Math.round(rect.height));
  canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d"); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,width,height);
  const summary = el(`${canvasId}-summary`);
  if (!rows.length) {
    delete canvas._chart;
    el("chart-tooltip").hidden = true;
    if (summary) summary.textContent = "No verified values are available for the selected range.";
    return;
  }
  const storedZoom = state.zoom.get(canvasId);
  const zoom = storedZoom ? { ...storedZoom } : { start: 0, end: rows.length - 1 };
  zoom.end = Math.min(rows.length - 1, zoom.end); zoom.start = Math.min(zoom.start, zoom.end); state.zoom.set(canvasId, zoom);
  if (!storedZoom) state.zoom.delete(canvasId);
  const visible = rows.slice(zoom.start, zoom.end + 1); const series = definition.series;
  const values = visible.flatMap(row => series.map(item => Number(item.value(row))).filter(Number.isFinite));
  if (!values.length) {
    wrap.classList.remove("has-data"); delete canvas._chart; el("chart-tooltip").hidden = true;
    if (summary) summary.textContent = "No numeric values are available for the selected range.";
    return;
  }
  const pad = { left: 55, right: 15, top: 14, bottom: 34 }; const plotW = width - pad.left - pad.right; const plotH = height - pad.top - pad.bottom;
  let min = definition.options.zero ? Math.min(0, ...values) : Math.min(...values); let max = definition.options.zero ? Math.max(0, ...values) : Math.max(...values);
  if (min === max) { const delta = Math.abs(min || 1) * .08; min -= delta; max += delta; }
  const margin = (max - min) * .08; min -= margin; max += margin;
  const times = visible.map(row => parseTime(definition.options.time(row)));
  const finiteTimes = times.filter(time => time !== null);
  const firstTime = finiteTimes.length ? Math.min(...finiteTimes) : null;
  const lastTime = finiteTimes.length ? Math.max(...finiteTimes) : null;
  const x = index => firstTime !== null && lastTime !== null && lastTime > firstTime && times[index] !== null
    ? pad.left + (times[index] - firstTime) / (lastTime - firstTime) * plotW
    : pad.left + (visible.length <= 1 ? plotW / 2 : index / (visible.length - 1) * plotW);
  const y = value => pad.top + (max - value) / (max - min) * plotH;
  ctx.font = "10px Inter, system-ui"; ctx.lineWidth = 1; ctx.strokeStyle = "rgba(143,160,183,.18)"; ctx.fillStyle = "#8fa0b7";
  for (let tick=0; tick<=4; tick++) { const value = max - (max-min)*tick/4; const yy = pad.top + plotH*tick/4; ctx.beginPath(); ctx.moveTo(pad.left,yy); ctx.lineTo(width-pad.right,yy); ctx.stroke(); const tickLabel = definition.options.axisFormat ? definition.options.axisFormat(value) : definition.options.percent ? `${value.toFixed(1)}%` : definition.options.integer ? String(Math.round(value)) : compact(value); ctx.fillText(tickLabel, 4, yy+3); }
  const labelIndexes = [0, Math.floor((visible.length-1)/2), visible.length-1];
  for (const index of [...new Set(labelIndexes)]) { const label = shortUtc.format(parseTime(definition.options.time(visible[index])) || Date.now()); ctx.textAlign = index === 0 ? "left" : index === visible.length-1 ? "right" : "center"; ctx.fillText(label, x(index), height-10); }
  ctx.textAlign = "left";
  for (const item of series) {
    ctx.strokeStyle = item.color; ctx.fillStyle = item.color; ctx.lineWidth = 2;
    if (definition.options.bars) {
      const barWidth = Math.max(2, Math.min(16, plotW / Math.max(visible.length,1) * .65));
      visible.forEach((row,index) => { const value = Number(item.value(row)); if (!Number.isFinite(value)) return; const zeroY = y(0); const yy = y(value); ctx.fillStyle = value >= 0 ? "#55d98b" : "#ff6f7d"; ctx.fillRect(x(index)-barWidth/2, Math.min(yy,zeroY), barWidth, Math.max(1,Math.abs(zeroY-yy))); });
    } else {
      ctx.beginPath(); let started = false;
      visible.forEach((row,index) => { const value = Number(item.value(row)); if (!Number.isFinite(value)) { started=false; return; } if (!started) { ctx.moveTo(x(index),y(value)); started=true; } else ctx.lineTo(x(index),y(value)); });
      ctx.stroke();
    }
  }
  canvas._chart = { definition, visible, x, y, pad, plotW, width, height };
  if (summary) {
    const start = fmtDate(definition.options.time(visible[0]));
    const end = fmtDate(definition.options.time(visible[visible.length - 1]));
    const descriptions = series.map(item => {
      const itemValues = visible.map(row => Number(item.value(row))).filter(Number.isFinite);
      if (!itemValues.length) return `${item.label}: unavailable`;
      const formatter = item.format || (definition.options.percent ? fmtPct : fmtMoney);
      return `${item.label}: minimum ${formatter(Math.min(...itemValues))}, maximum ${formatter(Math.max(...itemValues))}`;
    });
    summary.textContent = `${visible.length} observations from ${start} to ${end}. ${descriptions.join(". ")}.`;
  }
}
function compact(value) { const abs = Math.abs(value); if (abs >= 1e6) return `${(value/1e6).toFixed(1)}m`; if (abs >= 1e3) return `${(value/1e3).toFixed(1)}k`; return Number(value).toFixed(abs < 10 ? 2 : 0); }

function renderCharts() {
  const data = chartRows();
  setupChart("equity-chart", data.equity, [
    { label: "Balance", color: "#53a8ff", value: row => first(row.balance, NaN), format: fmtMoney },
    { label: "Equity", color: "#62e4d2", value: row => first(row.equity, NaN), format: fmtMoney }
  ], { time: row => first(row.timestamp,row.time,row.collected_at,row.observed_at) });
  setupChart("drawdown-chart", data.equity, [{ label: "Drawdown", color: "#ff6f7d", value: row => first(row.drawdown_pct,row.drawdown_percentage,NaN) }], { time: row => first(row.timestamp,row.time,row.collected_at,row.observed_at), percent: true, zero: true });
  setupChart("pnl-chart", data.cumulative, [{ label: "Cumulative P&L", color: "#b797ff", value: row => row.value }], { time: row => row.timestamp, zero: true });
  setupChart("trade-results-chart", data.trades.filter(trade => has(tradeNet(trade))), [{ label: "Net P&L", color: "#55d98b", value: tradeNet }], { time: row => tradeTime(row), zero: true, bars: true });
  setupChart("price-chart", data.trades.filter(trade => has(first(trade.entry_price,trade.price_open)) || has(first(trade.exit_price,trade.price_close))), [
    { label: "Entry", color: "#53a8ff", value: row => first(row.entry_price,row.price_open,NaN), format: value => fmtPrice(value) },
    { label: "Exit", color: "#f4bd62", value: row => first(row.exit_price,row.price_close,NaN), format: value => fmtPrice(value) }
  ], { time: row => tradeTime(row), axisFormat: value => fmtPrice(value) });
  setupChart("step-chart", data.steps.filter(row => has(first(row.step,row.level))), [{ label: "Step", color: "#f4bd62", value: row => first(row.step,row.level), format: value => fmtNumber(value, 0) }], { time: row => first(row.timestamp,row.time,row.changed_at,row.observed_at), zero: true });
  setupChart("daily-pnl-chart", data.daily.filter(row => has(row.realized_pnl)), [{ label: "Daily realized P&L", color: "#b797ff", value: row => row.realized_pnl, format: fmtMoney }], { time: row => row.timestamp, zero: true, bars: true });
  setupChart("activity-chart", data.daily, [{ label: recordNoun(true), color: "#53a8ff", value: row => row.count, format: value => fmtNumber(value, 0) }], { time: row => row.timestamp, zero: true, bars: true, integer: true });
}

function attachCanvasInteractions(canvas) {
  canvas.addEventListener("pointermove", event => {
    const chart = canvas._chart; if (!chart) return;
    const rect = canvas.getBoundingClientRect(); const localX = event.clientX - rect.left; const ratio = Math.max(0,Math.min(1,(localX-chart.pad.left)/chart.plotW));
    const index = Math.round(ratio * (chart.visible.length-1)); const row = chart.visible[index]; if (!row) return;
    const lines = chart.definition.series.map(item => { const value = item.value(row); return has(value) ? `<span style="color:${item.color}">${escapeHtml(item.label)}</span>: ${item.format ? item.format(value) : chart.definition.options.percent ? fmtPct(value) : fmtMoney(value)}` : ""; }).filter(Boolean);
    const time = chart.definition.options.time(row); const tooltip = el("chart-tooltip");
    tooltip.innerHTML = `<strong>${escapeHtml(fmtDate(time))}</strong>${lines.join("<br>")}`; tooltip.hidden = false;
    tooltip.style.left = `${Math.min(window.innerWidth-270,event.clientX+14)}px`; tooltip.style.top = `${Math.max(8,event.clientY-20)}px`;
  });
  canvas.addEventListener("pointerleave", () => { el("chart-tooltip").hidden = true; });
  canvas.addEventListener("wheel", event => {
    const definition = chartDefinitions.get(canvas.id); if (!definition || definition.rows.length < 4) return; event.preventDefault();
    const current = state.zoom.get(canvas.id) || { start:0,end:definition.rows.length-1 }; const size = current.end-current.start+1; const factor = event.deltaY > 0 ? 1.25 : .8;
    const newSize = Math.max(3,Math.min(definition.rows.length,Math.round(size*factor))); const rect=canvas.getBoundingClientRect(); const ratio=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width));
    let start=Math.round(current.start+(size-newSize)*ratio); start=Math.max(0,Math.min(definition.rows.length-newSize,start)); state.zoom.set(canvas.id,{start,end:start+newSize-1}); drawChart(canvas.id);
  }, { passive:false });
  canvas.addEventListener("dblclick", () => { state.zoom.delete(canvas.id); drawChart(canvas.id); });
  canvas.addEventListener("keydown", event => {
    const definition = chartDefinitions.get(canvas.id); if (!definition || !definition.rows.length) return;
    const full = definition.rows.length;
    const current = state.zoom.get(canvas.id) || { start: 0, end: full - 1 };
    const size = current.end - current.start + 1;
    if (event.key === "Home" || event.key === "0") {
      event.preventDefault(); state.zoom.delete(canvas.id); drawChart(canvas.id); return;
    }
    if (["+", "=", "-", "_"].includes(event.key)) {
      event.preventDefault();
      const factor = event.key === "+" || event.key === "=" ? 0.8 : 1.25;
      const newSize = Math.max(3, Math.min(full, Math.round(size * factor)));
      let start = Math.round(current.start + (size - newSize) / 2);
      start = Math.max(0, Math.min(full - newSize, start));
      if (newSize === full) state.zoom.delete(canvas.id); else state.zoom.set(canvas.id, { start, end: start + newSize - 1 });
      drawChart(canvas.id); return;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const shift = event.key === "ArrowLeft" ? -Math.max(1, Math.round(size * .15)) : Math.max(1, Math.round(size * .15));
      const start = Math.max(0, Math.min(full - size, current.start + shift));
      state.zoom.set(canvas.id, { start, end: start + size - 1 }); drawChart(canvas.id);
    }
  });
}

function render() {
  renderFreshness(); renderKpis(); renderStats(); renderTradeFilters(); renderOpenPositions(); renderClosedTrades(); renderCharts();
}

document.querySelectorAll("[data-range]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll("[data-range]").forEach(other => { const active = other === button; other.classList.toggle("is-active", active); other.setAttribute("aria-pressed", String(active)); });
  el("range-message").textContent = "";
  state.range = button.dataset.range; state.from = null; state.to = null; state.zoom.clear(); renderStats(); renderClosedTrades(); renderCharts();
}));
el("apply-range").addEventListener("click", () => {
  const from = el("date-from").value; const to = el("date-to").value;
  const fromMs = from ? Date.parse(`${from}Z`) : null;
  const toMs = to ? Date.parse(`${to}:59.999Z`) : null;
  if ((from && !Number.isFinite(fromMs)) || (to && !Number.isFinite(toMs)) || (fromMs !== null && toMs !== null && fromMs > toMs)) {
    el("range-message").textContent = "Enter a valid UTC range with From earlier than To.";
    return;
  }
  el("range-message").textContent = "";
  state.from = fromMs; state.to = toMs; state.range = "custom";
  document.querySelectorAll("[data-range]").forEach(button => { button.classList.remove("is-active"); button.setAttribute("aria-pressed", "false"); }); state.zoom.clear(); renderStats(); renderClosedTrades(); renderCharts();
});
el("reset-zoom").addEventListener("click", () => { state.zoom.clear(); renderCharts(); });
["filter-side","filter-symbol"].forEach(id => el(id).addEventListener("change", renderClosedTrades));
document.querySelectorAll("canvas").forEach(attachCanvasInteractions);
window.addEventListener("resize", () => { clearTimeout(window._resizeTimer); window._resizeTimer = setTimeout(renderCharts,150); });
setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
setInterval(() => { if (Object.keys(state.latest).length) renderFreshness(); }, 10_000);
refresh();
