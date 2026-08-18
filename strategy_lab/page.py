"""Strategy Library page.

The tokens, panel shapes and control styles are lifted from PAPER_HTML in
dashboard.py so this reads as the same product: same IBM Plex pair, same
--bg/--panel/--line palette, same .bot panel, .bhead, .stats/.stat, .btn,
.badge, .ph, .empty and .feed components, same 3/2/1 column breakpoints.

The list is virtualised: with a thousand-plus strategies only the rows inside
the viewport exist in the DOM, and search runs against a prebuilt index on the
server, so filtering stays responsive.
"""

STRATEGY_LAB_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strategy Library · paper trading</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* ---- tokens copied from the Paper Trading page so the two pages match ---- */
  :root{--bg:#05070a;--panel:#0a0d12;--panel2:#10151d;--line:#1d2633;--line2:#2b3748;
    --txt:#edf3fb;--muted:#7c8798;--amber:#f2b84b;--green:#19c37d;--red:#ff4d5f;--blue:#3aa0ff;
    --bin:'IBM Plex Mono',ui-monospace,monospace;--sans:'IBM Plex Sans',system-ui,sans-serif;}
  *{box-sizing:border-box}
  html,body{margin:0;background:#05070a;color:var(--txt);font-family:var(--sans);font-size:13px}
  body{background:linear-gradient(180deg,#05070a 0%,#070a0f 52%,#05070a 100%)}
  #topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;padding:10px 18px;
    background:#070a0f;border-bottom:1px solid #263244;box-shadow:0 12px 28px rgba(0,0,0,.28);flex-wrap:wrap}
  .ticker{font-family:var(--bin);font-weight:600;font-size:16px;text-transform:uppercase;letter-spacing:.12em;color:#f5f8fc}
  .ticker .dot{color:var(--amber)}
  .nav{font-family:var(--bin);font-size:13px;color:var(--amber);text-decoration:none;border:1px solid #263244;
    padding:6px 11px;border-radius:4px;background:#0e131b}
  .nav:hover{background:#121925;border-color:#3a4658;color:#fff}
  .spacer{flex:1}
  .sub{font-family:var(--bin);font-size:11.5px;color:var(--muted)}
  #wrap{max-width:none;margin:0 auto;padding:18px;display:grid;grid-template-columns:repeat(3,minmax(300px,1fr));gap:12px}
  @media(max-width:1320px){#wrap{grid-template-columns:repeat(2,minmax(300px,1fr))}}
  @media(max-width:900px){#wrap{grid-template-columns:1fr;padding:12px}}
  .bot{background:#090d13;border:1px solid #202a39;border-radius:6px;overflow:hidden;display:flex;flex-direction:column;
    box-shadow:0 10px 24px rgba(0,0,0,.18);min-width:0}
  .wide{grid-column:1/-1}
  .bhead{display:flex;align-items:center;gap:10px;padding:12px 15px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .bname{font-family:var(--bin);font-weight:600;font-size:14px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block}
  .dot.on{background:var(--green);box-shadow:0 0 7px var(--green)}
  .dot.off{background:var(--red)} .dot.watch{background:var(--amber);box-shadow:0 0 7px var(--amber)}
  .badge{font-family:var(--bin);font-size:10.5px;padding:3px 8px;border-radius:6px;border:1px solid var(--line2);color:var(--muted)}
  .badge.live{color:var(--amber);border-color:var(--amber)}
  .badge.ok{color:var(--green);border-color:#1c5} .badge.bad{color:var(--red);border-color:#722}
  .badge.info{color:var(--blue);border-color:#2b5b86}
  .btn{font-family:var(--bin);font-size:12px;border-radius:4px;cursor:pointer;padding:6px 12px;
    border:1px solid #263244;background:#0e131b;color:#cdd6e3}
  .btn:hover{background:#121925;border-color:#3a4658;color:#fff}
  .btn.on{background:#103024;border-color:#1c5;color:var(--green)}
  .btn.off{background:#2e1216;border-color:#722;color:var(--red)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  select,input{font-family:var(--bin);font-size:12px;background:#0e131b;color:var(--txt);
    border:1px solid #263244;border-radius:4px;padding:5px 8px}
  select:hover,input:hover{border-color:#3a4658}
  input:focus-visible,select:focus-visible,button:focus-visible,a:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border-bottom:1px solid var(--line)}
  .stat{background:var(--panel);padding:10px 12px;min-width:0}
  .stat .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
  .stat .v{font-family:var(--bin);font-size:16px;font-weight:600;margin-top:3px;overflow-wrap:anywhere}
  .pos{color:var(--green)} .neg{color:var(--red)}
  .ph{font-family:var(--bin);font-size:10.5px;font-weight:600;letter-spacing:.4px;color:var(--muted);
    text-transform:uppercase;padding:9px 14px 7px;border-bottom:1px solid var(--line);border-top:1px solid var(--line)}
  .empty{color:var(--muted);font-size:12px;font-family:var(--bin);padding:14px}
  .feed{max-height:230px;overflow:auto}
  .ln{display:grid;grid-template-columns:110px 1fr;gap:8px;font-family:var(--bin);font-size:11px;
    padding:4px 14px;border-bottom:1px solid #11141a}
  .ln .lt{color:var(--muted)}
  .controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;padding:12px 15px}
  .field{display:flex;flex-direction:column;gap:4px;min-width:0}
  .field label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
  .field input,.field select{width:100%}
  .actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:0 15px 12px}
  /* virtualised list: only rows in view are in the DOM */
  #listview{height:560px;overflow:auto;position:relative;border-top:1px solid var(--line)}
  #spacer{position:relative;width:100%}
  #rows{position:absolute;top:0;left:0;right:0}
  .srow{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1.05fr) 88px 96px 118px;gap:10px;
    padding:9px 14px;border-bottom:1px solid #11141a;align-items:center;cursor:pointer}
  .srow:hover{background:#0e131b}
  .srow.sel{background:#101a26;box-shadow:inset 2px 0 0 var(--amber)}
  .srow .nm{font-family:var(--bin);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .srow .al{font-size:10px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .srow .ct{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .srow .st{font-family:var(--bin);font-size:10px}
  .st.executable{color:var(--green)} .st.requires-data{color:var(--amber)}
  .st.research-only{color:var(--blue)} .st.unsupported{color:var(--red)}
  @media(max-width:900px){.srow{grid-template-columns:minmax(0,1fr) 92px}.srow .ct,.srow .tf,.srow .dr{display:none}}
  .table-wrap{overflow:auto;max-height:460px}
  table{width:100%;border-collapse:collapse;font-size:12px;min-width:900px}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #11141a;white-space:nowrap}
  th{position:sticky;top:0;background:var(--panel2);color:var(--muted);font-family:var(--bin);
    font-size:10px;text-transform:uppercase;letter-spacing:.4px;z-index:1}
  td.num{text-align:right;font-family:var(--bin);font-variant-numeric:tabular-nums}
  .progress{height:6px;flex:1;min-width:120px;background:#182230;border-radius:99px;overflow:hidden}
  .progress i{display:block;height:100%;background:linear-gradient(90deg,var(--amber),var(--green));width:0;transition:width .2s}
  .warn{margin:0;padding:10px 14px;background:#1b1408;border-top:1px solid #4a3a12;color:#ffd478;font-size:11.5px}
  .warn.err{background:#1a0d12;border-top-color:#722;color:#ffb3bd}
  .rules{padding:12px 15px;font-size:12px;color:#b9c6d6}
  .rules h4{margin:12px 0 5px;font-family:var(--bin);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
  .rules ul{margin:4px 0;padding-left:18px} .rules li{margin:3px 0}
  .rules a{color:#8ec5ff;overflow-wrap:anywhere}
  .chips{display:flex;gap:6px;flex-wrap:wrap;padding:0 15px 10px}
  .skel{height:14px;border-radius:4px;background:linear-gradient(90deg,#0c121b,#172231,#0c121b);
    background-size:220% 100%;animation:pulse 1.3s linear infinite;margin:8px 14px}
  @keyframes pulse{to{background-position:-220% 0}}
  @media (prefers-reduced-motion:reduce){.skel{animation:none}.progress i{transition:none}}
  svg.spark{width:100%;height:120px;display:block}
  /* paper desk: one mini bot card per strategy, same look as a Paper Trading panel */
  #desk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px;padding:12px 15px}
  .acct{border:1px solid #202a39;border-radius:6px;background:#090d13;overflow:hidden}
  .acct .ah{display:flex;align-items:center;gap:8px;padding:9px 11px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .acct .an{font-family:var(--bin);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
  .acct .ag{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line)}
  .acct .ac{background:var(--panel);padding:7px 9px;min-width:0}
  .acct .ac .k{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.3px}
  .acct .ac .v{font-family:var(--bin);font-size:13px;font-weight:600;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .acct .af{padding:6px 11px;font-family:var(--bin);font-size:10px;color:var(--muted);border-top:1px solid var(--line);
    display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .acct.busted{border-color:#722}
  @media(max-width:520px){.acct .ag{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<header id="topbar">
  <span class="ticker">STRATEGY<span class="dot">.</span>LIBRARY</span>
  <a class="nav" href="/paper">← Paper Trading</a>
  <a class="nav" href="/">Chart</a>
  <span class="spacer"></span>
  <span class="sub" id="headline">loading catalog…</span>
</header>

<main id="wrap">
  <section class="bot wide" aria-labelledby="hdr-overview">
    <div class="bhead"><span class="dot watch" id="status-dot"></span>
      <span class="bname" id="hdr-overview">Catalog</span>
      <span class="badge" id="badge-research">research —</span>
      <span class="badge info">paper only</span>
      <span class="badge" id="badge-data">market data —</span>
      <span class="spacer"></span>
      <span class="badge live" id="badge-sim">idle</span>
    </div>
    <div class="stats" id="overview-stats">
      <div class="stat"><div class="k">Strategies</div><div class="v">—</div></div>
      <div class="stat"><div class="k">Executable</div><div class="v">—</div></div>
      <div class="stat"><div class="k">Needs data</div><div class="v">—</div></div>
      <div class="stat"><div class="k">Rule engines</div><div class="v">—</div></div>
    </div>
    <p class="warn" id="disclaimer">Paper trading and historical replay only.</p>
  </section>

  <section class="bot wide" aria-labelledby="hdr-desk">
    <div class="bhead"><span class="dot" id="desk-dot"></span>
      <span class="bname" id="hdr-desk">Paper desk — every strategy trading paper money</span>
      <span class="badge" id="desk-symbol">—</span>
      <span class="badge info">no live order routing</span>
      <span class="spacer"></span>
      <button class="btn" id="desk-refresh">Refresh now</button>
      <button class="btn on" id="desk-toggle">Pause</button>
      <button class="btn off" id="desk-reset">Reset all</button></div>
    <div class="stats" id="desk-stats">
      <div class="stat"><div class="k">Accounts</div><div class="v">—</div></div>
      <div class="stat"><div class="k">Total equity</div><div class="v">—</div></div>
      <div class="stat"><div class="k">Total net P&amp;L</div><div class="v">—</div></div>
      <div class="stat"><div class="k">In position</div><div class="v">—</div></div>
    </div>
    <p class="warn" id="desk-note">Each strategy holds its own <b>$50,000</b> paper account that started
      <b>flat on the day it was opened</b> — no backfilled history, so every number here is money made or lost
      going forward. Accounts advance one daily bar at a time and persist across restarts.
      Simulated fills only; no broker order is ever sent.</p>
    <div class="controls">
      <div class="field"><label for="d-q">Find account</label><input id="d-q" placeholder="strategy name…"></div>
      <div class="field"><label for="d-view">Show</label><select id="d-view">
        <option value="all">All accounts</option><option value="traded">Has traded</option>
        <option value="profitable">Profitable</option><option value="unprofitable">Unprofitable</option>
        <option value="in-position">In position now</option><option value="idle">No trades yet</option>
      </select></div>
      <div class="field"><label for="d-sort">Sort</label><select id="d-sort">
        <option value="net_pnl">Net P&amp;L</option><option value="equity">Equity</option>
        <option value="win_rate">Win rate</option><option value="trades">Trades</option>
        <option value="drawdown">Max drawdown</option><option value="name">Name</option>
      </select></div>
      <div class="field"><label for="d-cat">Category</label><select id="d-cat"><option value="">All</option></select></div>
    </div>
    <div class="ph"><span id="desk-count">—</span></div>
    <div id="desk-grid"><div class="empty">Loading paper accounts…</div></div>
  </section>

  <section class="bot wide" aria-labelledby="hdr-browse">
    <div class="bhead"><span class="dot on"></span><span class="bname" id="hdr-browse">Browse</span>
      <span class="badge" id="badge-count">0 shown</span><span class="spacer"></span>
      <label class="sub" for="f-exec"><input type="checkbox" id="f-exec"> executable only</label>
    </div>
    <div class="controls">
      <div class="field"><label for="f-q">Search</label><input id="f-q" placeholder="name, alias, indicator, author…"></div>
      <div class="field"><label for="f-category">Category</label><select id="f-category"><option value="">All</option></select></div>
      <div class="field"><label for="f-status">Status</label><select id="f-status"><option value="">All</option></select></div>
      <div class="field"><label for="f-evidence">Evidence</label><select id="f-evidence"><option value="">All</option></select></div>
      <div class="field"><label for="f-direction">Direction</label><select id="f-direction"><option value="">All</option></select></div>
      <div class="field"><label for="f-timeframe">Timeframe</label><select id="f-timeframe"><option value="">All</option></select></div>
      <div class="field"><label for="f-complexity">Complexity</label><select id="f-complexity"><option value="">All</option></select></div>
      <div class="field"><label for="f-sort">Sort</label><select id="f-sort">
        <option value="relevance">Runnable first</option><option value="category">Category</option><option value="name">Name</option>
        <option value="age">Oldest first</option><option value="newest">Newest first</option>
        <option value="status">Status</option><option value="evidence">Evidence</option>
        <option value="complexity">Complexity</option></select></div>
    </div>
    <div class="ph"><span>Strategy</span></div>
    <div id="listview" tabindex="0" role="listbox" aria-label="Strategy catalog">
      <div id="spacer"><div id="rows"></div></div>
    </div>
    <div class="empty" id="list-empty" hidden>No strategy matches these filters.</div>
  </section>

  <section class="bot" aria-labelledby="hdr-detail">
    <div class="bhead"><span class="dot" id="detail-dot"></span>
      <span class="bname" id="hdr-detail">Strategy detail</span>
      <span class="badge" id="detail-status">—</span></div>
    <div id="detail-body"><div class="empty">Select a strategy from the list.</div></div>
  </section>

  <section class="bot" aria-labelledby="hdr-run">
    <div class="bhead"><span class="dot watch"></span><span class="bname" id="hdr-run">Run one strategy</span>
      <span class="spacer"></span><span class="badge" id="run-badge">idle</span></div>
    <div class="controls">
      <div class="field"><label for="r-symbol">Symbol</label><input id="r-symbol" value="SPY"></div>
      <div class="field"><label for="r-start">Start</label><input id="r-start" type="date" value="2015-01-01"></div>
      <div class="field"><label for="r-end">End</label><input id="r-end" type="date" value="2025-01-01"></div>
      <div class="field"><label for="r-capital">Capital</label><input id="r-capital" type="number" value="100000" min="1000" step="1000"></div>
      <div class="field"><label for="r-commission">Commission / share</label><input id="r-commission" type="number" value="0.005" min="0" step="0.001"></div>
      <div class="field"><label for="r-spread">Spread (bps)</label><input id="r-spread" type="number" value="5" min="0" step="0.5"></div>
      <div class="field"><label for="r-slippage">Slippage (bps)</label><input id="r-slippage" type="number" value="2" min="0" step="0.5"></div>
      <div class="field"><label for="r-sizing">Sizing</label><select id="r-sizing">
        <option value="fixed-fraction">Fixed fraction</option>
        <option value="volatility-target">Volatility target</option>
        <option value="fixed-shares">Fixed shares</option></select></div>
      <div class="field"><label for="r-short">Short side</label><select id="r-short">
        <option value="1">Allowed</option><option value="0">Blocked</option></select></div>
    </div>
    <div class="actions">
      <button class="btn on" id="run-btn" disabled>Run backtest</button>
      <span class="sub" id="run-note">select a strategy first</span>
    </div>
    <div id="run-result"><div class="empty">No run yet.</div></div>
  </section>

  <section class="bot wide" aria-labelledby="hdr-batch">
    <div class="bhead"><span class="dot" id="batch-dot"></span>
      <span class="bname" id="hdr-batch">Run all executable strategies</span>
      <span class="badge" id="batch-counts">—</span><span class="spacer"></span>
      <button class="btn on" id="batch-btn">Run all</button>
      <button class="btn off" id="batch-cancel" disabled>Cancel</button>
      <div class="progress" id="batch-track" role="progressbar" aria-label="Batch progress"
           aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i id="batch-bar"></i></div>
      <span class="sub" id="batch-state">idle</span>
    </div>
    <div class="chips">
      <button class="btn" data-lb="all">All results</button>
      <button class="btn" data-lb="sufficient">Sufficient sample</button>
      <button class="btn" data-lb="insufficient">Insufficient sample</button>
      <button class="btn" data-lb="profitable">Profitable</button>
      <button class="btn" data-lb="unprofitable">Unprofitable</button>
      <button class="btn" data-lb="failed">Failed</button>
    </div>
    <p class="warn" id="batch-warn">Rows without a sufficient sample are listed last and must not be
      read as evidence. A positive backtest on one symbol is not a forecast.</p>
    <div class="table-wrap"><table id="lb-table"><thead><tr>
      <th>Strategy</th><th>Category</th><th class="num">Net %</th><th class="num">CAGR %</th>
      <th class="num">Max DD %</th><th class="num">Sharpe</th><th class="num">Sortino</th>
      <th class="num">Calmar</th><th class="num">Win %</th><th class="num">PF</th>
      <th class="num">Expectancy</th><th class="num">Trades</th><th class="num">Hold</th>
      <th class="num">Costs</th><th class="num">Exposure %</th><th class="num">Turnover</th>
      <th class="num">Bench %</th><th>Sample</th></tr></thead>
      <tbody id="lb-body"><tr><td colspan="18"><div class="empty">No batch has been run yet.</div></td></tr></tbody>
    </table></div>
  </section>

  <section class="bot wide" aria-labelledby="hdr-compare">
    <div class="bhead"><span class="dot"></span><span class="bname" id="hdr-compare">Compare</span>
      <span class="badge" id="cmp-count">0 selected</span><span class="spacer"></span>
      <button class="btn" id="cmp-add" disabled>Add selected</button>
      <button class="btn on" id="cmp-run" disabled>Compare</button>
      <button class="btn reset" id="cmp-clear">Clear</button></div>
    <div id="cmp-body"><div class="empty">Pick strategies, press “Add selected”, then Compare.</div></div>
  </section>
</main>

<script>
'use strict';
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(v,d=2)=>v==null||v===''?'—':Number(v).toFixed(d);
const money=v=>v==null?'—':'$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:0});
const compact=v=>{if(v==null)return'—';const n=Number(v),a=Math.abs(n),sg=n<0?'-':'';
  if(a>=1e6)return sg+'$'+(a/1e6).toFixed(2)+'M';
  if(a>=1e4)return sg+'$'+(a/1e3).toFixed(1)+'k';
  return sg+'$'+a.toLocaleString(undefined,{maximumFractionDigits:0})};
const ROW_H=44;
let OVERVIEW=null,ITEMS=[],TOTAL=0,SELECTED=null,COMPARE=[],BATCH=null,POLL=null,LB=[],LBFILTER='all';
let PAGE=1,PAGE_SIZE=200,LOADING=false,EXHAUSTED=false;

async function api(url,opts){const r=await fetch(url,opts);let j;
  try{j=await r.json()}catch(e){throw new Error('server returned a non-JSON response')}
  if(!r.ok||j.ok===false)throw new Error(j.error||('HTTP '+r.status));return j}

function query(){const p=new URLSearchParams();
  const map={q:'f-q',category:'f-category',implementation_status:'f-status',evidence_level:'f-evidence',
             direction:'f-direction',timeframe:'f-timeframe',complexity:'f-complexity',sort:'f-sort'};
  for(const [k,id] of Object.entries(map)){const v=$(id).value.trim();if(v)p.set(k,v)}
  if($('f-exec').checked)p.set('executable_only','1');
  p.set('page',String(PAGE));p.set('page_size',String(PAGE_SIZE));return p.toString()}

async function loadOverview(){try{
  OVERVIEW=await api('/api/strategies/overview');const s=OVERVIEW.stats;
  $('headline').textContent=`${s.total.toLocaleString()} strategies · ${s.executable.toLocaleString()} executable · researched ${OVERVIEW.research_date}`;
  $('badge-research').textContent='research '+OVERVIEW.research_date;
  $('badge-data').textContent='yfinance daily';
  $('status-dot').className='dot on';
  $('disclaimer').textContent=OVERVIEW.disclaimer;
  $('overview-stats').innerHTML=[
    ['Strategies',s.total.toLocaleString()],['Executable',s.executable.toLocaleString()],
    ['Needs data',(s.requires_data+s.research_only+s.unsupported).toLocaleString()],
    ['Rule engines',String(s.rule_engines)],
    ['Canonical families',s.canonical_families.toLocaleString()],['Variations',s.variations.toLocaleString()],
    ['Oldest',String(s.oldest_year??'—')],['Newest',String(s.newest_year??'—')],
  ].map(x=>`<div class="stat"><div class="k">${esc(x[0])}</div><div class="v">${esc(x[1])}</div></div>`).join('');
  const f=OVERVIEW.facets;
  const fill=(id,vals)=>{const el=$(id);const keep=el.value;
    el.innerHTML='<option value="">All</option>'+vals.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');el.value=keep};
  fill('f-category',f.category);fill('d-cat',f.category);fill('f-status',f.implementation_status);fill('f-evidence',f.evidence_level);
  fill('f-direction',f.direction);fill('f-timeframe',f.timeframe);fill('f-complexity',f.complexity);
}catch(e){$('headline').textContent='catalog failed to load';$('status-dot').className='dot off';
  $('disclaimer').className='warn err';$('disclaimer').textContent='Could not load the catalog: '+e.message}}

async function loadPage(reset){
  if(LOADING)return; LOADING=true;
  if(reset){PAGE=1;ITEMS=[];EXHAUSTED=false;$('listview').scrollTop=0}
  try{const j=await api('/api/strategies/browse?'+query());
    TOTAL=j.total; ITEMS=reset?j.items:ITEMS.concat(j.items);
    EXHAUSTED=ITEMS.length>=TOTAL;
    $('badge-count').textContent=`${ITEMS.length.toLocaleString()} of ${TOTAL.toLocaleString()}`;
    $('list-empty').hidden=TOTAL>0;
    renderRows();
  }catch(e){$('list-empty').hidden=false;$('list-empty').textContent='Search failed: '+e.message}
  finally{LOADING=false}}

// Only the rows inside the viewport are ever created; 1,000+ entries stay smooth.
function renderRows(){
  const view=$('listview'),rows=$('rows');
  $('spacer').style.height=(ITEMS.length*ROW_H)+'px';
  const first=Math.max(0,Math.floor(view.scrollTop/ROW_H)-6);
  const last=Math.min(ITEMS.length,first+Math.ceil(view.clientHeight/ROW_H)+12);
  rows.style.transform=`translateY(${first*ROW_H}px)`;
  rows.innerHTML=ITEMS.slice(first,last).map(s=>`
    <div class="srow${SELECTED===s.id?' sel':''}" data-id="${esc(s.id)}" role="option"
         aria-selected="${SELECTED===s.id}" tabindex="-1" style="height:${ROW_H}px">
      <div><div class="nm">${esc(s.name)}</div><div class="al">${esc(s.aliases.slice(0,2).join(' · ')||s.subcategory)}</div></div>
      <div class="ct">${esc(s.category)}</div>
      <div class="ct tf">${esc(s.timeframes.join(', '))}</div>
      <div class="ct dr">${esc(s.direction)}</div>
      <div class="st ${esc(s.implementation_status)}">${esc(s.implementation_status)}</div>
    </div>`).join('');
  if(!EXHAUSTED && view.scrollTop+view.clientHeight > (ITEMS.length-30)*ROW_H){PAGE+=1;loadPage(false)}}

async function select(id){SELECTED=id;renderRows();
  $('detail-body').innerHTML='<div class="skel"></div><div class="skel"></div><div class="skel"></div>';
  try{const j=await api('/api/strategies/detail/'+encodeURIComponent(id));const s=j.strategy;
    $('detail-status').textContent=s.implementation_status;
    $('detail-status').className='badge '+(s.is_executable?'ok':'bad');
    $('detail-dot').className='dot '+(s.is_executable?'on':'watch');
    $('hdr-detail').textContent=s.display_name;
    $('run-btn').disabled=!s.is_executable;
    $('cmp-add').disabled=!s.is_executable;
    $('run-note').textContent=s.is_executable?'ready':'not runnable here — see “why not runnable”';
    const list=(t,a)=>a&&a.length?`<h4>${t}</h4><ul>${a.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'';
    $('detail-body').innerHTML=`<div class="rules">
      <p>${esc(s.description)}</p>
      <h4>Why it might work</h4><p>${esc(s.thesis)}</p>
      ${s.origin_creator||s.origin_year?`<h4>Origin</h4><p>${esc(s.origin_creator||'—')}${s.origin_year?` · ${s.origin_year}`:''}${s.origin_notes?` — ${esc(s.origin_notes)}`:''}</p>`:''}
      ${s.systematic_interpretation?`<p class="warn" style="margin:8px 0">Systematic interpretation: the original is discretionary, so these fixed rules approximate it and will not match any one author's reading.</p>`:''}
      ${list('Entry',s.entry_rules)}${list('Exit',s.exit_rules)}${list('Stops',s.stop_rules)}
      ${list('Position sizing',s.position_sizing_rules)}${list('Risk',s.risk_rules)}${list('No-trade',s.no_trade_rules)}
      ${list('Data required',s.data_requirements)}${list('Indicators',s.indicator_requirements)}
      ${s.external_data_requirements.length?list('Missing data on this platform',s.external_data_requirements):''}
      ${s.unsupported_reason?`<h4>Why not runnable</h4><p>${esc(s.unsupported_reason)}</p>`:''}
      ${list('Limitations',s.limitations)}
      ${s.parameters.length?`<h4>Parameters</h4><ul>${s.parameters.map(p=>`<li>${esc(p.label)} — default <b>${esc(String(p.default))}</b></li>`).join('')}</ul>`:''}
      ${s.sources.length?`<h4>Sources</h4><ul>${s.sources.map(x=>`<li>${x.url?`<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a>`:esc(x.title)}${x.author?` — ${esc(x.author)}`:''}${x.year?` (${x.year})`:''}${x.kind==='index'?' [discovery index]':''}</li>`).join('')}</ul>`:''}
      <h4>Identity</h4><p class="sub">${esc(s.id)} · v${esc(s.version)} · evidence: ${esc(s.evidence_level)}</p>
    </div>`;
  }catch(e){$('detail-body').innerHTML=`<p class="warn err">Could not load detail: ${esc(e.message)}</p>`}}

function runBody(){return{strategy_id:SELECTED,symbol:$('r-symbol').value.trim().toUpperCase(),
  start:$('r-start').value,end:$('r-end').value,starting_capital:Number($('r-capital').value),
  commission_per_share:Number($('r-commission').value),spread_bps:Number($('r-spread').value),
  slippage_bps:Number($('r-slippage').value),sizing:$('r-sizing').value,
  allow_short:$('r-short').value==='1'}}

function metricRow(m){return[['Net',num(m.net_return_pct)+'%',m.net_return_pct>=0?'pos':'neg'],
  ['CAGR',m.annualised_return_pct==null?'—':num(m.annualised_return_pct)+'%',''],
  ['Max DD',num(m.max_drawdown_pct)+'%','neg'],['Sharpe',num(m.sharpe),''],
  ['Win rate',num(m.win_rate_pct,1)+'%',''],['Profit factor',num(m.profit_factor),''],
  ['Trades',String(m.trades),''],['Costs',money(m.total_cost),''],
  ['Benchmark',num(m.benchmark_return_pct)+'%',''],
  ['Excess',num(m.excess_return_pct)+'%',m.excess_return_pct>=0?'pos':'neg'],
  ['Exposure',num(m.exposure_pct,1)+'%',''],['Turnover',num(m.turnover,1)+'x','']]
  .map(x=>`<div class="stat"><div class="k">${esc(x[0])}</div><div class="v ${x[2]}">${esc(x[1])}</div></div>`).join('')}

function sparkline(points){if(!points||points.length<2)return'';
  const v=points.map(p=>Number(p.equity)),lo=Math.min(...v),hi=Math.max(...v),span=(hi-lo)||1;
  const step=Math.max(1,Math.floor(v.length/400));const use=v.filter((_,i)=>i%step===0);
  const pts=use.map((y,i)=>`${(i/(use.length-1)*600).toFixed(1)},${(120-(y-lo)/span*112-4).toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 600 120" role="img"
    aria-label="Equity curve from ${money(v[0])} to ${money(v[v.length-1])}, low ${money(lo)}, high ${money(hi)}">
    <polyline fill="none" stroke="#19c37d" stroke-width="2" points="${pts}"/></svg>`}

async function runOne(){if(!SELECTED)return;
  $('run-badge').textContent='running';$('run-result').innerHTML='<div class="skel"></div><div class="skel"></div>';
  try{const j=await api('/api/strategies/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(runBody())});
    const r=j.result,m=r.metrics,p=r.provenance;
    $('run-badge').textContent='done';
    const tr=(r.trades||[]).slice(-25).reverse();
    $('run-result').innerHTML=`
      <div class="stats">${metricRow(m)}</div>
      ${(r.warnings||[]).map(w=>`<p class="warn">${esc(w)}</p>`).join('')}
      <div class="ph">Equity curve</div>${sparkline(r.equity_curve)}
      <div class="ph">Provenance</div>
      <div class="feed">${Object.entries({Symbol:p.symbol,Bars:p.bars,From:p.first_bar,To:p.last_bar,
        'Dropped bars':p.dropped_invalid_bars,Capital:money(p.starting_capital),Sizing:p.sizing,
        Benchmark:p.benchmark,Kind:p.result_kind,'Run id':r.run_id,Adjustment:p.adjustment_note}).map(
        ([k,v])=>`<div class="ln"><span class="lt">${esc(k)}</span><span>${esc(String(v))}</span></div>`).join('')}</div>
      <div class="ph">Last ${tr.length} trades</div>
      <div class="feed">${tr.length?tr.map(t=>`<div class="ln"><span class="lt">${esc(t.exit_date.slice(0,10))}</span>
        <span>${esc(t.direction)} ${t.shares} @ ${num(t.entry_price)} → ${num(t.exit_price)}
        <b class="${t.net_pnl>=0?'pos':'neg'}">${t.net_pnl>=0?'+':''}${num(t.net_pnl)}</b> ${esc(t.exit_reason)}</span></div>`).join('')
        :'<div class="empty">No trades were generated.</div>'}</div>`;
  }catch(e){$('run-badge').textContent='error';
    $('run-result').innerHTML=`<p class="warn err">${esc(e.message)}</p>`}}

function lbFiltered(){const r=LB;
  if(LBFILTER==='sufficient')return r.filter(x=>x.ok&&x.sample_sufficient);
  if(LBFILTER==='insufficient')return r.filter(x=>x.ok&&!x.sample_sufficient);
  if(LBFILTER==='profitable')return r.filter(x=>x.ok&&(x.net_return_pct||0)>0);
  if(LBFILTER==='unprofitable')return r.filter(x=>x.ok&&(x.net_return_pct||0)<=0);
  if(LBFILTER==='failed')return r.filter(x=>!x.ok);
  return r}

function renderLeaderboard(){const rows=lbFiltered().slice(0,400);
  $('lb-body').innerHTML=rows.length?rows.map(r=>!r.ok
    ? `<tr><td>${esc(r.name)}</td><td>${esc(r.category)}</td><td colspan="15" class="neg">${esc(r.error)}</td><td>failed</td></tr>`
    : `<tr><td title="${esc(r.strategy_id)}">${esc(r.name)}</td><td>${esc(r.category)}</td>
      <td class="num ${r.net_return_pct>=0?'pos':'neg'}">${num(r.net_return_pct)}</td>
      <td class="num">${r.annualised_return_pct==null?'—':num(r.annualised_return_pct)}</td>
      <td class="num neg">${num(r.max_drawdown_pct)}</td><td class="num">${num(r.sharpe)}</td>
      <td class="num">${num(r.sortino)}</td><td class="num">${num(r.calmar)}</td>
      <td class="num">${num(r.win_rate_pct,1)}</td><td class="num">${num(r.profit_factor)}</td>
      <td class="num">${num(r.expectancy)}</td><td class="num">${r.trades}</td>
      <td class="num">${num(r.avg_bars_held,1)}</td><td class="num">${money(r.total_cost)}</td>
      <td class="num">${num(r.exposure_pct,1)}</td><td class="num">${num(r.turnover,1)}</td>
      <td class="num">${num(r.benchmark_return_pct)}</td>
      <td>${r.sample_sufficient?'<span class="badge ok">sufficient</span>':'<span class="badge bad">too small</span>'}</td></tr>`
    ).join('') : '<tr><td colspan="18"><div class="empty">Nothing matches this filter.</div></td></tr>';
  $('batch-counts').textContent=`${lbFiltered().length} rows`}

async function startBatch(){try{
  $('batch-btn').disabled=true;$('batch-cancel').disabled=false;$('batch-dot').className='dot watch';
  const j=await api('/api/strategies/batch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(runBody())});
  BATCH=j.id;$('badge-sim').textContent='batch running';pollBatch();
}catch(e){$('batch-state').textContent='error: '+e.message;$('batch-btn').disabled=false;
  $('batch-cancel').disabled=true;$('batch-dot').className='dot off'}}

async function pollBatch(){if(!BATCH)return;
  try{const j=await api('/api/strategies/batch/'+encodeURIComponent(BATCH));
    $('batch-bar').style.width=j.progress+'%';
    $('batch-track').setAttribute('aria-valuenow',String(j.progress));
    $('batch-state').textContent=`${j.status} · ${j.completed} ok · ${j.failed} failed · ${j.skipped} skipped · ${j.cached} cached`;
    LB=j.rows||[];renderLeaderboard();
    if(['completed','error','cancelled'].includes(j.status)){
      $('batch-btn').disabled=false;$('batch-cancel').disabled=true;BATCH=null;
      $('badge-sim').textContent='idle';
      $('batch-dot').className='dot '+(j.status==='completed'?'on':'off');return}
    POLL=setTimeout(pollBatch,400);
  }catch(e){$('batch-state').textContent='error: '+e.message;$('batch-btn').disabled=false;
    $('batch-cancel').disabled=true;BATCH=null;$('batch-dot').className='dot off'}}

async function cancelBatch(){if(!BATCH)return;clearTimeout(POLL);
  try{await api('/api/strategies/batch/'+encodeURIComponent(BATCH)+'/cancel',{method:'POST'})}catch(e){}
  $('batch-state').textContent='cancelled';$('batch-btn').disabled=false;$('batch-cancel').disabled=true;
  $('batch-dot').className='dot off';BATCH=null;$('badge-sim').textContent='idle'}

function cmpSync(){$('cmp-count').textContent=`${COMPARE.length} selected`;
  $('cmp-run').disabled=COMPARE.length<2}
async function runCompare(){if(COMPARE.length<2)return;
  $('cmp-body').innerHTML='<div class="skel"></div><div class="skel"></div>';
  try{const j=await api('/api/strategies/compare',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({...runBody(),strategy_ids:COMPARE})});
    $('cmp-body').innerHTML=`<div class="table-wrap"><table><thead><tr><th>Strategy</th>
      <th class="num">Net %</th><th class="num">Max DD %</th><th class="num">Sharpe</th>
      <th class="num">Trades</th><th class="num">Costs</th><th>Sample</th></tr></thead><tbody>
      ${j.results.map(r=>r.ok?`<tr><td>${esc(r.name)}</td>
        <td class="num ${r.metrics.net_return_pct>=0?'pos':'neg'}">${num(r.metrics.net_return_pct)}</td>
        <td class="num neg">${num(r.metrics.max_drawdown_pct)}</td>
        <td class="num">${num(r.metrics.sharpe)}</td><td class="num">${r.metrics.trades}</td>
        <td class="num">${money(r.metrics.total_cost)}</td>
        <td>${r.metrics.sample_sufficient?'<span class="badge ok">sufficient</span>':'<span class="badge bad">too small</span>'}</td></tr>`
        :`<tr><td>${esc(r.name)}</td><td colspan="6" class="neg">${esc(r.error)}</td></tr>`).join('')}
      </tbody></table></div>
      ${j.results.filter(r=>r.ok).map(r=>`<div class="ph">${esc(r.name)}</div>${sparkline(r.equity_curve)}`).join('')}`;
  }catch(e){$('cmp-body').innerHTML=`<p class="warn err">${esc(e.message)}</p>`}}

// ---- wiring ----
let debounce=null;
['f-q','f-category','f-status','f-evidence','f-direction','f-timeframe','f-complexity','f-sort','f-exec']
  .forEach(id=>$(id).addEventListener(id==='f-q'?'input':'change',()=>{
    clearTimeout(debounce);debounce=setTimeout(()=>loadPage(true),id==='f-q'?180:0)}));
$('listview').addEventListener('scroll',()=>requestAnimationFrame(renderRows));
$('listview').addEventListener('click',e=>{const row=e.target.closest('.srow');if(row)select(row.dataset.id)});
$('listview').addEventListener('keydown',e=>{
  if(e.key!=='ArrowDown'&&e.key!=='ArrowUp'&&e.key!=='Enter')return;
  e.preventDefault();
  const i=ITEMS.findIndex(x=>x.id===SELECTED);
  if(e.key==='Enter'){if(SELECTED)select(SELECTED);return}
  const next=Math.max(0,Math.min(ITEMS.length-1,i+(e.key==='ArrowDown'?1:-1)));
  if(ITEMS[next]){select(ITEMS[next].id);$('listview').scrollTop=Math.max(0,next*ROW_H-200)}});
$('run-btn').addEventListener('click',runOne);
$('batch-btn').addEventListener('click',startBatch);
$('batch-cancel').addEventListener('click',cancelBatch);
$('cmp-add').addEventListener('click',()=>{if(SELECTED&&!COMPARE.includes(SELECTED)&&COMPARE.length<8){COMPARE.push(SELECTED);cmpSync()}});
$('cmp-clear').addEventListener('click',()=>{COMPARE=[];cmpSync();$('cmp-body').innerHTML='<div class="empty">Pick strategies, press “Add selected”, then Compare.</div>'});
$('cmp-run').addEventListener('click',runCompare);
document.querySelectorAll('[data-lb]').forEach(b=>b.addEventListener('click',()=>{
  LBFILTER=b.dataset.lb;document.querySelectorAll('[data-lb]').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');renderLeaderboard()}));


// ---- paper desk: one live account card per strategy ----
let DESK_TIMER=null;
function deskQuery(){const p=new URLSearchParams();
  const q=$('d-q').value.trim(); if(q)p.set('q',q);
  const c=$('d-cat').value; if(c)p.set('category',c);
  p.set('view',$('d-view').value); p.set('sort',$('d-sort').value); return p.toString()}

function acctCard(r){
  const pnlCls=r.net_pnl>0?'pos':(r.net_pnl<0?'neg':'');
  const pos=r.position;
  return `<article class="acct${r.busted?' busted':''}">
    <div class="ah"><span class="dot ${pos?'on':(r.busted?'off':'')}"></span>
      <span class="an" title="${esc(r.strategy_id)}">${esc(r.name)}</span>
      ${r.busted?'<span class="badge bad">busted</span>':''}
      ${pos?`<span class="badge ${pos.side==='long'?'ok':'bad'}">${esc(pos.side)}</span>`:'<span class="badge">flat</span>'}
    </div>
    <div class="ag">
      <div class="ac"><div class="k">Balance</div><div class="v" title="${money(r.balance)}">${compact(r.balance)}</div></div>
      <div class="ac"><div class="k">Equity</div><div class="v" title="${money(r.equity)}">${compact(r.equity)}</div></div>
      <div class="ac"><div class="k">Net P&amp;L</div><div class="v ${pnlCls}" title="${money(r.net_pnl)}">${r.net_pnl>=0?'+':''}${compact(r.net_pnl)}</div></div>
      <div class="ac"><div class="k">Return</div><div class="v ${pnlCls}">${num(r.net_pnl_pct,1)}%</div></div>
      <div class="ac"><div class="k">Win rate</div><div class="v">${num(r.win_rate,1)}%</div></div>
      <div class="ac"><div class="k">Trades</div><div class="v">${r.trades}</div></div>
      <div class="ac"><div class="k">Max DD</div><div class="v neg">${num(r.max_drawdown_pct,1)}%</div></div>
      <div class="ac"><div class="k">Costs</div><div class="v" title="${money(r.costs)}">${compact(r.costs)}</div></div>
    </div>
    <div class="af">
      ${pos?`<span>${esc(pos.side)} ${pos.shares} @ ${num(pos.entry)} since ${esc(pos.since)}</span>
        <span class="${pos.unrealized>=0?'pos':'neg'}">${pos.unrealized>=0?'+':''}${money(pos.unrealized)} open</span>`
        :(r.trades?'<span>flat, waiting for the next signal</span>'
                  :'<span>no trade yet — waiting for its first signal</span>')}
      <span class="spacer"></span><span>${r.bars_live??0}d live</span>
    </div>
    ${r.error?`<div class="af neg">${esc(r.error)}</div>`:''}
  </article>`}

async function loadDesk(){try{
  const j=await api('/api/strategies/paper?'+deskQuery());
  $('desk-dot').className='dot '+(j.enabled?'on':'off');
  $('desk-symbol').textContent=j.symbol+' · '+j.bars+' bars · '+(j.last_bar||'—');
  $('desk-toggle').textContent=j.enabled?'Pause':'Resume';
  $('desk-toggle').className='btn '+(j.enabled?'on':'off');
  const pnlCls=j.total_net_pnl>0?'pos':(j.total_net_pnl<0?'neg':'');
  $('desk-stats').innerHTML=[
    ['Accounts',String(j.accounts),''],
    ['Each started with',money(j.start_balance_each),''],
    ['Total net P&L',(j.total_net_pnl>=0?'+':'')+money(j.total_net_pnl),pnlCls],
    ['Profitable',`${j.profitable} / ${j.with_trades}`,j.profitable>j.unprofitable?'pos':'neg'],
    ['In position now',String(j.in_position),''],
    ['Traded so far',String(j.with_trades),''],
    ['Not traded yet',String(j.awaiting_first_trade??0),''],
    ['Last bar',esc(j.last_bar||'—'),''],
  ].map(x=>`<div class="stat"><div class="k">${esc(x[0])}</div><div class="v ${x[2]}">${esc(x[1])}</div></div>`).join('');
  $('desk-count').textContent=`${j.total.toLocaleString()} accounts shown · scroll for all`
    +(j.opened_on?` · opened ${j.opened_on}`:'');
  $('desk-grid').innerHTML=j.rows.length?j.rows.map(acctCard).join('')
    :'<div class="empty">No paper account matches this filter.</div>';
  if(j.data_error){$('desk-note').className='warn err';$('desk-note').textContent=j.data_error}
}catch(e){$('desk-dot').className='dot off';
  $('desk-grid').innerHTML=`<div class="empty">Paper desk failed to load: ${esc(e.message)}</div>`}}

async function deskAction(url,body){try{
  await api(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:body?JSON.stringify(body):undefined});
  await loadDesk()}catch(e){alert('Paper desk: '+e.message)}}

['d-view','d-sort','d-cat'].forEach(id=>$(id).addEventListener('change',loadDesk));
$('d-q').addEventListener('input',()=>{clearTimeout(DESK_TIMER);DESK_TIMER=setTimeout(loadDesk,200)});
$('desk-refresh').addEventListener('click',()=>deskAction('/api/strategies/paper/refresh'));
$('desk-toggle').addEventListener('click',()=>deskAction('/api/strategies/paper/toggle',
  {enabled:$('desk-toggle').textContent==='Resume'}));
$('desk-reset').addEventListener('click',()=>{
  if(confirm('Reset every paper account back to $100,000 and erase their trade history?'))
    deskAction('/api/strategies/paper/reset')});
setInterval(loadDesk,60000);

loadOverview().then(()=>loadPage(true)).then(loadDesk);
</script>
</body>
</html>"""
