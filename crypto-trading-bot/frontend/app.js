/* Crypto Trading Bot dashboard — vanilla JS, talks to the FastAPI /api. */
"use strict";

const API = "/api";                 // same origin as this page (served by FastAPI)
const $ = (id) => document.getElementById(id);

// Runtime-editable settings and how to render each one.
const SETTINGS_FIELDS = [
  ["entry_mode", "select", ["pullback", "breakout"]],
  ["momentum_mode", "select", ["macd", "roc", "off"]],
  ["htf_timeframe", "text"], ["entry_timeframe", "text"],
  ["ema_fast", "number"], ["ema_slow", "number"],
  ["rsi_period", "number"],
  ["rsi_long_min", "number"], ["rsi_long_max", "number"],
  ["rsi_short_min", "number"], ["rsi_short_max", "number"],
  ["atr_period", "number"], ["atr_stop_mult", "number"], ["rr", "number"],
  ["volume_ma", "number"],
  ["pullback_atr", "number"], ["breakout_lookback", "number"], ["roc_period", "number"],
  ["min_atr_pct", "number"], ["max_atr_pct", "number"],
  ["risk_per_trade", "number"], ["max_open_positions", "number"], ["max_leverage", "number"],
  ["max_daily_loss", "number"], ["cooldown_minutes", "number"],
  ["use_trailing_stop", "bool"], ["trail_atr_mult", "number"],
  ["fee_rate", "number"], ["slippage", "number"],
  ["use_funding_filter", "bool"], ["max_funding", "number"],
  // V4 — Momentum Sweep VWAP Breakout (only used when strategy = v4)
  ["v4_timeframe", "text"], ["v4_atr_stop", "number"], ["v4_rr", "number"],
  ["v4_max_stop_pct", "number"], ["v4_leverage", "number"],
  ["v4_cooldown_bars", "number"], ["v4_start_balance", "number"],
];

const money = (v) => (v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
const pct = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%");
const cls = (v) => (Number(v) >= 0 ? "pos" : "neg");
const esc = (s) => (s || "").toString().replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, method = "GET", body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(API + path, opt);
  return r.json();
}

// ----------------------------- render -----------------------------
let settingsBuilt = false;

function render(s) {
  if (!s) return;
  const running = s.running;
  const dot = $("statusDot");
  dot.className = "dot " + (!running ? "off" : (s.paused_reason ? "blocked" : "on"));
  $("statusText").textContent = s.status || "";

  if ($("modeSelect").value !== s.mode) $("modeSelect").value = s.mode;
  if (s.strategy && $("stratSelect").value !== s.strategy.toLowerCase()) $("stratSelect").value = s.strategy.toLowerCase();
  $("sMode").textContent = (s.mode || "—").toUpperCase() + (s.strategy ? " · " + s.strategy : "");
  $("sEquity").textContent = money(s.equity);
  $("sTotalPnl").innerHTML = `<span class="${cls(s.total_pnl)}">${money(s.total_pnl)} (${pct(s.total_pnl_pct)})</span>`;
  $("sDayPnl").innerHTML = `<span class="${cls(s.day_pnl)}">${money(s.day_pnl)} (${pct(s.day_pnl_pct)})</span>`;
  $("sBalance").textContent = money(s.balance);
  $("sOpen").textContent = (s.open_positions || []).length;

  // live arming bar
  const live = s.mode === "live";
  $("liveBar").classList.toggle("hidden", !live);
  if (live) {
    $("armState").textContent = s.armed ? "ARMED — real orders ON"
      : (s.can_arm ? "disarmed" : "cannot arm: " + (s.arm_blocked_reason || "live disabled in .env"));
  }

  // latest signal
  const sig = s.latest_signal;
  const ls = $("latestSignal");
  if (sig) {
    ls.className = "signal " + sig.side;
    ls.innerHTML = `<b class="${sig.side === "long" ? "pos" : "neg"}">${sig.side.toUpperCase()}</b> ${esc(sig.symbol)} @ ${money(sig.price)}`
      + `<div class="muted small">${esc(sig.reason)}</div>`
      + `<div class="muted small">${esc(JSON.stringify(sig.meta || {}))}</div>`;
  }

  // positions
  const pos = s.open_positions || [];
  $("positions").innerHTML = pos.length ? (
    "<table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Price</th><th>Stop</th><th>Take</th><th>uPnL</th></tr>" +
    pos.map((p) => `<tr><td>${esc(p.symbol)}</td><td class="${p.side === "long" ? "pos" : "neg"}">${p.side}</td>`
      + `<td>${money(p.entry)}</td><td>${money(p.price)}</td><td>${money(p.stop)}</td><td>${money(p.take)}</td>`
      + `<td class="${cls(p.unrealized)}">${money(p.unrealized)}</td></tr>`).join("") + "</table>"
  ) : '<div class="muted">none</div>';

  if (!settingsBuilt && s.settings) buildSettings(s.settings);
}

// ----------------------------- settings form -----------------------------
function buildSettings(values) {
  const box = $("settingsForm");
  box.innerHTML = SETTINGS_FIELDS.map(([key, type, opts]) => {
    const v = values[key];
    if (type === "select") {
      return `<label>${key}<select data-key="${key}">` +
        opts.map((o) => `<option value="${o}" ${o === v ? "selected" : ""}>${o}</option>`).join("") +
        `</select></label>`;
    }
    if (type === "bool") {
      return `<label>${key}<select data-key="${key}"><option value="true" ${v ? "selected" : ""}>true</option>` +
        `<option value="false" ${!v ? "selected" : ""}>false</option></select></label>`;
    }
    const step = type === "number" ? 'step="any"' : "";
    return `<label>${key}<input data-key="${key}" type="${type}" ${step} value="${esc(v)}" /></label>`;
  }).join("");
  settingsBuilt = true;
}

async function saveSettings() {
  const patch = {};
  document.querySelectorAll("#settingsForm [data-key]").forEach((el) => {
    let v = el.value;
    if (v === "true") v = true; else if (v === "false") v = false;
    else if (el.type === "number" && v !== "") v = Number(v);
    patch[el.dataset.key] = v;
  });
  const r = await api("/settings", "POST", patch);
  $("settingsMsg").textContent = r.ok ? "saved ✓ (applies to new trades)" : "error";
  setTimeout(() => ($("settingsMsg").textContent = ""), 3000);
}

// ----------------------------- backtest -----------------------------
async function runBacktest() {
  const symbolsRaw = $("btSymbols").value.trim();
  const symbols = symbolsRaw ? symbolsRaw.split(",").map((x) => x.trim()) : null;
  const days = Number($("btDays").value) || null;
  $("btResults").innerHTML = '<div class="muted">running… this can take a minute</div>';
  const r = await api("/backtest", "POST", { symbols, days });
  if (!r.ok) { $("btResults").innerHTML = '<div class="neg">backtest failed</div>'; return; }
  $("btResults").innerHTML = Object.entries(r.results).map(([sym, res]) => {
    if (res.error) return `<div class="neg small">${esc(sym)}: ${esc(res.error)}</div>`;
    const m = res.metrics || {};
    const cells = [
      ["Trades", m.num_trades], ["Return", pct(m.total_return_pct)], ["Win rate", pct(m.win_rate_pct)],
      ["Profit factor", m.profit_factor], ["Max DD", pct(m.max_drawdown_pct)], ["Sharpe", m.sharpe],
      ["Sortino", m.sortino], ["Avg trade", money(m.avg_trade)], ["Worst", money(m.worst_trade)],
      ["Best", money(m.best_trade)], ["Max consec loss", m.max_consecutive_losses], ["Final eq", money(m.final_equity)],
    ];
    return `<div style="margin-top:8px"><b>${esc(sym)}</b><div class="metrics">` +
      cells.map(([k, v]) => `<div class="metric"><div class="k">${k}</div><div class="v">${v ?? "—"}</div></div>`).join("") +
      `</div></div>`;
  }).join("");
}

// ----------------------------- trades + logs -----------------------------
async function refreshTrades() {
  const r = await api("/trades?limit=40");
  const t = r.trades || [];
  $("trades").innerHTML = t.length ? (
    "<table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th></tr>" +
    t.map((x) => `<tr><td>${esc(x.symbol)}</td><td class="${x.side === "long" ? "pos" : "neg"}">${x.side}</td>`
      + `<td>${money(x.entry_price)}</td><td>${money(x.exit_price)}</td>`
      + `<td class="${cls(x.pnl)}">${money(x.pnl)} (${pct(x.pnl_pct)})</td><td class="muted">${esc(x.reason)}</td></tr>`).join("")
    + "</table>"
  ) : '<div class="muted">no trades yet</div>';
}

async function refreshLogs() {
  const r = await api("/logs?limit=60");
  const logs = r.logs || [];
  $("logs").innerHTML = logs.length ? logs.map((l) =>
    `<div class="logrow ${esc(l.level)}"><span class="lvl">${esc(l.level)}</span> ${esc(l.message)}</div>`).join("")
    : '<div class="muted">—</div>';
}

// ----------------------------- controls -----------------------------
$("startBtn").onclick = async () => { const r = await api("/start", "POST"); if (!r.ok) alert(r.error || "could not start"); };
$("stopBtn").onclick = () => api("/stop", "POST");
$("emergencyBtn").onclick = () => { if (confirm("Flatten ALL positions and stop?")) api("/emergency_stop", "POST"); };
$("stratSelect").onchange = async (e) => {
  const v = e.target.value;
  const NAMES = { v2: "Strategy V2 (15m)", v3: "Strategy V3 (Goodman daily trend-following)", v4: "Momentum Sweep VWAP Breakout (20x intraday)" };
  if (!confirm("Switch to " + (NAMES[v] || v) + "? Best done while flat. Re-run a backtest to see it.")) { poll(); return; }
  await api("/settings", "POST", { strategy_version: v });
  settingsBuilt = false;   // rebuild settings form for the new strategy
  poll(); refreshTrades();
};
$("modeSelect").onchange = async (e) => {
  if (e.target.value === "live" && !confirm("Switch to LIVE mode? Real orders become possible after you Arm.")) {
    e.target.value = "paper"; return;
  }
  await api("/mode", "POST", { mode: e.target.value });
};
$("armBtn").onclick = async () => {
  if (!confirm("ARM live trading? The bot will place REAL orders on your account.")) return;
  const r = await api("/live/arm", "POST"); if (!r.ok) alert(r.error || "cannot arm");
};
$("disarmBtn").onclick = () => api("/live/disarm", "POST");
$("saveSettings").onclick = saveSettings;
$("btRun").onclick = runBacktest;

// ----------------------------- live updates -----------------------------
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws;
  try { ws = new WebSocket(`${proto}://${location.host}${API}/ws`); }
  catch (e) { return poll(); }
  ws.onmessage = (ev) => { try { render(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => setTimeout(connectWS, 3000);   // auto-reconnect
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}
function poll() { api("/status").then(render).catch(() => {}); }

connectWS();
poll();                       // immediate first paint
refreshTrades(); refreshLogs();
setInterval(() => { refreshTrades(); refreshLogs(); }, 5000);
setInterval(poll, 4000);      // safety net if the WS is down
