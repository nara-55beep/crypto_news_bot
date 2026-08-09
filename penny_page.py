"""
penny_page.py - responsive AI Penny Stock research desk served at /penny.

The page intentionally has no build step or framework dependency. It reads one
state endpoint every five seconds and only repaints when the payload changes.
"""

PENNY_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Penny Stock Evidence Desk</title>
<style>
:root{
  --bg:#070a0f;--surface:#0d121a;--surface2:#111925;--surface3:#172231;
  --line:#202d3d;--line2:#2d3d51;--text:#edf3fb;--soft:#b8c4d3;--muted:#748397;
  --gold:#f7ca45;--green:#34d399;--red:#fb7185;--blue:#60a5fa;--amber:#fbbf24;
  --shadow:0 18px 46px rgba(0,0,0,.28);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html{width:100%;max-width:100%;scrollbar-gutter:stable}
body{width:100%;max-width:100%;margin:0;min-width:0;overflow-x:hidden;background:
  radial-gradient(circle at 8% -10%,rgba(52,211,153,.08),transparent 28rem),
  radial-gradient(circle at 92% 0,rgba(96,165,250,.08),transparent 30rem),var(--bg);
  color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
button,a{font:inherit}a{color:inherit;text-decoration:none}
.page{width:100%;max-width:1720px;min-width:0;margin:0 auto;padding:18px clamp(12px,2.1vw,34px) 64px}
.topbar{position:relative;display:flex;align-items:flex-start;gap:22px;padding:21px 22px;
  border:1px solid var(--line2);border-radius:18px;background:linear-gradient(135deg,rgba(18,27,39,.96),rgba(10,15,22,.96));
  box-shadow:var(--shadow);overflow:hidden}
.topbar:after{content:"";position:absolute;inset:0 0 auto;height:2px;background:linear-gradient(90deg,var(--green),var(--blue),transparent 72%)}
.brand{min-width:270px}.eyebrow{color:var(--green);font:700 10px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase}
.brand h1{margin:8px 0 4px;font-size:clamp(21px,2.1vw,30px);line-height:1.12;letter-spacing:-.035em}
.statusline{max-width:850px;color:var(--muted);font-size:12px}
.actions{margin-left:auto;display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap}
.btn{min-height:36px;padding:8px 13px;border:1px solid var(--line2);border-radius:9px;background:#121a25;color:var(--soft);cursor:pointer;font-size:12px;font-weight:700;transition:.15s ease}
.btn:hover{border-color:#47617f;background:#192536;color:var(--text)}
.btn.go{border-color:#1d694e;background:#0b211a;color:#79e8b9}.btn.stop{border-color:#71303b;background:#241015;color:#ffabb7}
.systembar{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:12px 0 16px;padding:0 2px}
.chip{display:inline-flex;align-items:center;gap:6px;max-width:100%;padding:5px 9px;border:1px solid var(--line);border-radius:999px;background:#0b1017;color:var(--muted);font:650 10.5px/1.2 var(--mono)}
.chip:before{content:"";width:6px;height:6px;border-radius:99px;background:#526071;flex:none}
.chip.on{color:#7ce4b9;border-color:#1e5c47}.chip.on:before{background:var(--green);box-shadow:0 0 9px rgba(52,211,153,.7)}
.chip.off{color:#9ba8b8}.chip.off:before{background:#68778a}.chip.live{color:#ffe08a;border-color:#5f4b18}.chip.live:before{background:var(--gold)}
.alerts{display:grid;gap:8px;margin-bottom:14px}.alert{padding:11px 14px;border:1px solid #6e2b39;border-radius:11px;background:#251017;color:#ffb1be;font:12px/1.5 var(--mono)}

.metrics{display:grid;grid-template-columns:repeat(8,minmax(118px,1fr));gap:10px;margin-bottom:12px}
.metric{min-width:0;padding:13px 14px;border:1px solid var(--line);border-radius:13px;background:rgba(13,18,26,.95)}
.metric .label{overflow:hidden;text-overflow:ellipsis;color:var(--muted);font:700 9.5px/1.2 var(--mono);letter-spacing:.09em;text-transform:uppercase;white-space:nowrap}
.metric .value{overflow-wrap:anywhere;margin-top:7px;font:750 clamp(17px,1.55vw,23px)/1.1 var(--mono);letter-spacing:-.04em}
.green{color:var(--green)}.red{color:var(--red)}.gold{color:var(--gold)}

.evidence-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:16px}
.evidence{min-width:0;padding:15px;border:1px solid var(--line);border-radius:14px;background:linear-gradient(145deg,#101722,#0c1118)}
.evidence-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.evidence-title{color:var(--muted);font:700 10px/1.3 var(--mono);letter-spacing:.08em;text-transform:uppercase}
.badge{flex:none;padding:3px 7px;border-radius:6px;border:1px solid var(--line2);color:var(--soft);font:700 9.5px/1.2 var(--mono)}
.badge.good{border-color:#216a50;color:#77e2b4}.badge.bad{border-color:#76313c;color:#ff9fac}.badge.wait{border-color:#66511a;color:#f8d57b}
.evidence-value{margin:9px 0 4px;font-size:18px;font-weight:750;letter-spacing:-.02em}.evidence-copy{min-height:38px;overflow-wrap:anywhere;color:var(--muted);font-size:11.5px;line-height:1.55}
.progress{height:6px;margin-top:12px;overflow:hidden;border-radius:99px;background:#1b2634}.progress i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--blue),var(--green));transition:width .35s ease}
.progress-meta{display:flex;justify-content:space-between;gap:12px;margin-top:7px;color:#8795a7;font:10px/1.3 var(--mono)}
.universe{margin-bottom:16px;padding:15px;border:1px solid var(--line2);border-radius:15px;background:linear-gradient(110deg,#101a25,#0c121a)}
.universe-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:12px}.universe-head h2{margin:0;color:var(--soft);font-size:12px;letter-spacing:.06em;text-transform:uppercase}.universe-head p{margin:4px 0 0;color:var(--muted);font-size:11px}
.coverage-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px}.coverage-stat{min-width:0;padding:10px 11px;border:1px solid var(--line);border-radius:10px;background:#0b1119}.coverage-stat span{display:block;color:var(--muted);font:700 9px/1.2 var(--mono);letter-spacing:.06em;text-transform:uppercase}.coverage-stat b{display:block;margin-top:5px;overflow-wrap:anywhere;color:var(--text);font:750 16px/1.15 var(--mono)}
.coverage-note{margin-top:10px;color:#8f9eb0;font-size:10.5px;line-height:1.55}.coverage-note.bad{color:#ff9fac}

.workspace{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px}.span-12{grid-column:span 12}.span-7{grid-column:span 7}.span-5{grid-column:span 5}.span-6{grid-column:span 6}
.card{min-width:0;border:1px solid var(--line);border-radius:15px;background:rgba(13,18,26,.96);box-shadow:0 12px 32px rgba(0,0,0,.12);overflow:hidden}
.card-head{display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:49px;padding:12px 15px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#131c28,#101720)}
.card-title{min-width:0}.card-title h2{margin:0;color:var(--soft);font-size:12px;line-height:1.3;letter-spacing:.055em;text-transform:uppercase}.card-title p{margin:3px 0 0;color:var(--muted);font-size:10.5px}
.count{flex:none;color:var(--muted);font:11px/1 var(--mono)}
.empty{padding:34px 18px;text-align:center;color:var(--muted);font-size:12.5px}.empty strong{display:block;margin-bottom:4px;color:var(--soft);font-size:14px}

.table-wrap{width:100%;max-width:100%;overflow-x:auto;overscroll-behavior-inline:contain;scrollbar-color:#34465d #0b1017}
.table-wrap:focus{outline:1px solid var(--blue);outline-offset:-1px}
table{width:100%;min-width:720px;border-collapse:separate;border-spacing:0;font-size:12px}
table.wide{min-width:1220px}table.medium{min-width:900px}
th{position:sticky;top:0;z-index:1;padding:10px 11px;border-bottom:1px solid var(--line);background:#0e151f;color:var(--muted);font:700 9.5px/1.25 var(--mono);letter-spacing:.06em;text-align:left;text-transform:uppercase;white-space:nowrap}
td{padding:11px;border-bottom:1px solid #17212d;color:#c9d4e1;vertical-align:top}tr:last-child td{border-bottom:0}tbody tr:hover td{background:#101925}
.mono{font-family:var(--mono)}.ticker{color:var(--text);font-size:13px;font-weight:800}.muted{color:var(--muted);font-size:10.5px}.nowrap{white-space:nowrap}
.note{padding:12px 15px;border-top:1px solid var(--line);background:#0a0f16;color:var(--muted);font-size:11px;line-height:1.65}.note b{color:#aebccb}
.method{border-top:1px solid var(--line);background:#0a0f16}.method summary{padding:11px 15px;color:#91a1b4;font:700 10.5px/1.3 var(--mono);cursor:pointer}.method .method-copy{padding:0 15px 13px;color:var(--muted);font-size:11px;line-height:1.65}
.feed{max-height:330px;overflow:auto;font:11px/1.5 var(--mono)}.feed div{padding:7px 14px;border-bottom:1px solid #17212d;color:#9aa8ba}.feed .win{color:#69dfac}.feed .loss{color:#ff94a4}.feed .open{color:#f8d06b}

.pill,.signal{display:inline-flex;align-items:center;padding:3px 8px;border:1px solid var(--line2);border-radius:6px;font:750 9.5px/1.25 var(--mono);letter-spacing:.025em;white-space:nowrap}
.p-buy,.s-buy,.s-strong{border-color:#226b50;background:#0b281d;color:#69e8af}.p-watch,.s-watch{border-color:#68531d;background:#282108;color:#f6d56f}.p-avoid,.s-avoid{border-color:#71313b;background:#2a1016;color:#ff99a8}.p-rej,.s-no{background:#151b24;color:#8b98a9}.s-research{border-color:#285681;background:#10253a;color:#8dc9ff}
.s-strong{box-shadow:0 0 13px rgba(52,211,153,.14)}
.rank{display:grid;place-items:center;width:28px;height:28px;border:1px solid var(--line2);border-radius:8px;background:#161f2b;color:#9caabc;font:800 11px/1 var(--mono)}
.rank.t1{border-color:#d1a72d;background:#d6ad35;color:#151006}.rank.t2{border-color:#94a3b6;background:#aab5c2;color:#11151b}.rank.t3{border-color:#a96b30;background:#a86c32;color:#190e05}
.score{display:flex;align-items:center;gap:6px;margin-top:5px}.bar{width:82px;height:4px;overflow:hidden;border-radius:99px;background:#1b2634}.bar i{display:block;height:100%}.snum{color:#8291a4;font:10px/1 var(--mono)}
.levels{font:10.5px/1.7 var(--mono)}.levels b{color:var(--text)}.levels .entry{color:#8cc9ff}.levels .stop{color:#ff97a6}.levels .target{color:#7ee0b3}
.mini{display:grid;gap:2px;color:var(--muted);font:9.5px/1.45 var(--mono)}.mini i{color:#c4cfdb;font-style:normal}
.tag{display:inline-block;margin:2px 3px 1px 0;padding:2px 6px;border:1px solid #25445e;border-radius:5px;background:#102131;color:#9bcdf3;font-size:9.5px}
.held{margin-left:5px;padding:1px 5px;border:1px solid #245173;border-radius:5px;color:#8dc9ff;font:8.5px/1.2 var(--mono)}
details.why{margin-top:7px}details.why summary{color:#8493a6;font-size:10px;cursor:pointer}.reason{display:grid;gap:5px;margin-top:6px}.rline{display:grid;grid-template-columns:70px 1fr;gap:6px;font-size:10.5px}.rkey{color:var(--muted);text-transform:uppercase}.rvalue{color:#bfccd9}.rvalue.bull{color:#7ee0b3}.rvalue.bear{color:#ff9daa}

@media(max-width:1240px){.metrics{grid-template-columns:repeat(4,minmax(0,1fr))}.coverage-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.span-7,.span-5{grid-column:span 12}}
@media(max-width:860px){.page{padding:10px 9px 42px}.topbar{display:block;padding:17px}.actions{justify-content:flex-start;margin-top:14px}.systembar{margin-top:10px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.evidence-grid{grid-template-columns:1fr}.coverage-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.span-6{grid-column:span 12}.metric .value{font-size:19px}.card-head,.universe-head{align-items:flex-start}.statusline{font-size:11px}}
@media(max-width:520px){.topbar,.systembar,.metrics,.evidence-grid,.workspace,.card{width:100%;max-width:100%;min-width:0}.actions{display:grid;grid-template-columns:1fr;width:100%}.actions .btn{display:block;width:100%;min-width:0;text-align:center}.metrics{grid-template-columns:minmax(0,1fr);gap:7px}.metric{padding:11px}.workspace{gap:10px}.brand{min-width:0}.evidence{max-width:100%;overflow:hidden}.chip{max-width:calc(50vw - 14px);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
</style>
</head>
<body>
<main class="page">
  <header class="topbar">
    <div class="brand">
      <div class="eyebrow">Forward research workspace</div>
      <h1>Penny Stock Evidence Desk</h1>
      <div class="statusline" id="hsub">Loading scanner state...</div>
    </div>
    <div class="actions">
      <button class="btn" id="btn-toggle" onclick="toggleBot()">Loading...</button>
      <button class="btn" onclick="resetBot()">Reset paper account</button>
      <a class="btn" href="/paper">Back to paper desk</a>
    </div>
  </header>

  <div class="systembar">
    <span class="chip" id="c-state">scanner</span><span class="chip" id="c-market">market</span>
    <span class="chip" id="c-model">model</span><span class="chip" id="c-regime">tape</span>
    <span class="chip" id="c-edge">edge</span><span class="chip" id="c-rule">rule</span>
  </div>
  <div class="alerts" id="alerts"></div>
  <section class="metrics" id="stats" aria-label="Account and scanner summary"></section>
  <section class="evidence-grid" id="evidence" aria-label="Validation progress"></section>
  <section class="universe" id="universe" aria-label="Market universe coverage"></section>

  <section class="workspace">
    <article class="card span-5">
      <div class="card-head"><div class="card-title"><h2>Open paper positions</h2><p>Executable entries only; no real orders</p></div><span class="count" id="poscount"></span></div>
      <div id="positions"></div>
    </article>
    <article class="card span-7">
      <div class="card-head"><div class="card-title"><h2>Detected setups</h2><p>Persistence and a fresh trusted quote are required</p></div></div>
      <div id="signals"></div>
    </article>
    <article class="card span-12">
      <div class="card-head"><div class="card-title"><h2>Live opportunity leaderboard</h2><p>Top 20 names ranked by catalyst, technical quality and tradeability</p></div><span class="count">Scroll table sideways if needed</span></div>
      <div id="watchlist"></div><div class="note" id="rules"></div>
    </article>
    <article class="card span-12">
      <div class="card-head"><div class="card-title"><h2>Forward signal validation</h2><p>Completed signal-day baskets, never repeated scanner impressions</p></div></div>
      <div id="accuracy"></div>
    </article>
    <article class="card span-6">
      <div class="card-head"><div class="card-title"><h2>Closed paper trades</h2><p>After-cost outcomes</p></div></div>
      <div id="history"></div>
    </article>
    <article class="card span-6">
      <div class="card-head"><div class="card-title"><h2>Scanner activity</h2><p>Most recent events</p></div></div>
      <div class="feed" id="log"></div>
    </article>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>\"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[ch]));
const number = value => Number(value) || 0;
const money = value => (number(value) < 0 ? '-' : '') + '$' + Math.abs(number(value)).toFixed(2);
const signedMoney = value => (number(value) >= 0 ? '+' : '-') + '$' + Math.abs(number(value)).toFixed(2);
let lastHash = '';

function metric(label,value,cls=''){
  return '<div class="metric"><div class="label">'+esc(label)+'</div><div class="value '+cls+'">'+value+'</div></div>';
}
function td(value,cls='mono'){ return '<td class="'+cls+'">'+esc(value)+'</td>'; }
function table(head,rows,size=''){
  return '<div class="table-wrap" tabindex="0"><table class="'+size+'"><thead><tr>'+
    head.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table></div>';
}
function statusClass(status){
  if(['PROMISING_NOT_VALIDATED','AI_LIFT_PROMISING_NOT_VALIDATED','RESOLVABLE_NOW','COMPLETE'].includes(status)) return 'good';
  if(['REJECTED','NO_MEASURED_AI_EDGE','INFEASIBLE_WITHIN_HORIZON','DATA_INCOMPLETE','PARTIAL','FAILED'].includes(status)) return 'bad';
  return 'wait';
}
function progressCard(title,value,copy,current,required,status){
  const need = Math.max(0,number(required)); const have = Math.max(0,number(current));
  const pct = need ? Math.min(100,have/need*100) : 0;
  return '<article class="evidence"><div class="evidence-head"><div class="evidence-title">'+esc(title)+'</div>'+
    '<span class="badge '+statusClass(status)+'">'+esc(status||'COLLECTING')+'</span></div>'+
    '<div class="evidence-value">'+esc(value)+'</div><div class="evidence-copy">'+esc(copy)+'</div>'+
    '<div class="progress" aria-label="'+pct.toFixed(0)+' percent complete"><i style="width:'+pct+'%"></i></div>'+
    '<div class="progress-meta"><span>'+have+' completed</span><span>'+(need ? need+' minimum' : 'awaiting data')+'</span></div></article>';
}
function validationPowerView(fv){
  const fp = fv.feasibility || {}; const days = number(fv.signal_days);
  if(!days) return {value:'Waiting for outcomes',copy:'Power needs completed returns. No signal day has reached the '+number(fv.horizon_sessions||5)+'-session outcome horizon yet.',status:'COLLECTING'};
  if(fp.applicable === false) return {value:'Still collecting',copy:fp.summary||fp.reason||'More completed outcomes are required before power can be estimated.',status:'COLLECTING'};
  return {value:String(fp.status||'Power estimate ready').replaceAll('_',' '),copy:fp.summary||fp.reason||'Power is estimated from the observed event rate and return dispersion.',status:fp.status||'RESOLVABLE_NOW'};
}
function evidencePanel(s){
  const fv=s.forward_validation||{}, av=s.ai_value_audit||{}, clock=s.evidence_clock||{};
  const power=validationPowerView(fv);
  return progressCard('Forward strategy evidence',(number(fv.signal_days))+' / '+number(fv.minimum_signal_days||60)+' days',fv.reason||'Waiting for complete signal-day outcomes.',fv.signal_days,fv.minimum_signal_days||60,fv.status||'COLLECTING')+
    progressCard('Incremental AI value',(number(av.comparison_days))+' / '+number(av.minimum_comparison_days||60)+' days',av.reason||'Waiting for complete AI-versus-mechanical days.',av.comparison_days,av.minimum_comparison_days||60,av.status||'COLLECTING')+
    progressCard('Validation power',power.value,power.copy,clock.completed_signal_days,clock.completed_signal_days_required||60,power.status);
}
function renderUniverse(s){
  const c=s.universe_coverage||{};
  if(!c.last_completed_at){
    $('universe').innerHTML='<div class="universe-head"><div><h2>Market-wide universe coverage</h2><p>The first exhaustive listed-market snapshot is pending.</p></div><span class="badge wait">PENDING</span></div>';
    return;
  }
  const status=c.status||'UNKNOWN';
  const item=(label,value)=>'<div class="coverage-stat"><span>'+esc(label)+'</span><b>'+esc(value)+'</b></div>';
  $('universe').innerHTML='<div class="universe-head"><div><h2>Market-wide universe coverage</h2><p>Every active tradable non-OTC U.S. equity is requested in the cheap first stage; only candidates receive expensive deep analysis.</p></div><span class="badge '+statusClass(status)+'">'+esc(status)+'</span></div>'+
    '<div class="coverage-grid">'+item('Active listed',number(c.active_listed_tradable).toLocaleString())+item('Snapshots returned',number(c.snapshots_returned).toLocaleString()+' / '+number(c.symbols_requested).toLocaleString())+item('Snapshot coverage',number(c.snapshot_coverage_pct).toFixed(2)+'%')+item('Penny-price matches',number(c.penny_price_matches).toLocaleString())+item('Deep dossiers',number(c.deep_scored).toLocaleString()+' / '+number(c.penny_price_matches).toLocaleString())+item('OTC excluded',number(c.otc_excluded).toLocaleString())+'</div>'+
    '<div class="coverage-note '+(c.error?'bad':'')+'">Discovery feed: '+esc(c.feed_description||c.feed||'unknown')+'. Delayed snapshots find the broad universe; confirmation and fills still require fresh regular-session execution quotes. '+esc(c.error||c.otc_reason||'')+'</div>';
}
function scoreBar(value){
  if(value===undefined||value===null||value==='') return '';
  const n=Math.max(0,Math.min(100,number(value))); const color=n>=65?'var(--green)':n>=45?'var(--gold)':'var(--red)';
  return '<div class="score"><div class="bar"><i style="width:'+n+'%;background:'+color+'"></i></div><span class="snum">'+n+'</span></div>';
}
function reasonLine(key,value,cls=''){
  return value ? '<div class="rline"><div class="rkey">'+esc(key)+'</div><div class="rvalue '+cls+'">'+esc(value)+'</div></div>' : '';
}

function renderHeader(s){
  $('hsub').textContent=s.status||'Scanner state unavailable';
  const setChip=(id,text,cls,title='')=>{const el=$(id);el.textContent=text;el.className='chip '+cls;el.title=title;};
  setChip('c-state','continuous scanner','on',s.enabled?'Research and paper-entry toggle enabled.':'Research remains live; new paper entries are paused.');
  setChip('c-market',s.market_open?'market open':'market closed',s.market_open?'live':'off');
  setChip('c-model',s.ai_model||'AI','');
  const regime=s.regime||{}; setChip('c-regime','tape: '+(regime.label||'unknown'),regime.label==='risk-on'?'on':regime.label==='risk-off'?'off':'',(regime.why||[]).join(' | '));
  const edge=s.edge_policy||{}; setChip('c-edge','edge: '+String(edge.status||'missing').toLowerCase(),edge.auto_trade_allowed?'on':'off',[edge.reason||'Trading remains locked.'].concat(edge.inference_verdicts||[]).join('\n'));
  const rule=s.live_rule_evidence||{};
  setChip('c-rule',rule.measured?'price core gross '+(number(rule.mean_gross_pct)>=0?'+':'')+number(rule.mean_gross_pct).toFixed(2)+'%':'rule: unmeasured',rule.gross_is_zero?'off':'',rule.diagnosis||'Price/volume component only; not the full deployed strategy.');
  const button=$('btn-toggle');button.textContent=s.enabled?'Pause new entries':'Enable new entries';button.className='btn '+(s.enabled?'stop':'go');
  const problems=[];if(s.state_save_error)problems.push('EVIDENCE AT RISK - '+s.state_save_error);if(s.archive_error)problems.push('EVIDENCE AT RISK - '+s.archive_error);if(s.last_error)problems.push(s.last_error);
  $('alerts').innerHTML=problems.map(x=>'<div class="alert">'+esc(x)+'</div>').join('');
}
function renderStats(s){
  const pnl=number(s.total_pnl);
  $('stats').innerHTML=metric('Equity',money(s.equity),'gold')+metric('Net P&L',signedMoney(pnl),pnl>=0?'green':'red')+
    metric('Cash',money(s.balance))+metric('Open positions',number(s.open_count)+' / '+number(s.max_open))+
    metric('Closed trades',number(s.trades))+metric('Win rate',s.trades?esc(s.win_rate)+'%':'No outcomes')+
    metric('Scans',number(s.scan_count))+metric('Next scan',s.scan_in_progress?'Running now':number(s.next_scan_in_sec)+'s');
  $('evidence').innerHTML=evidencePanel(s);
}
function renderPositions(s){
  const rows=s.positions||[];$('poscount').textContent=rows.length?rows.length+' open':'0 open';
  $('positions').innerHTML=rows.length?table(['Ticker','Qty','Entry','Now','Stop','Target','P&L','Held'],rows.map(p=>
    '<tr><td><span class="ticker">'+esc(p.ticker)+'</span><div class="muted">'+esc(p.name||'')+'</div></td>'+td(p.qty)+td('$'+p.entry)+td('$'+p.price)+
    td('$'+p.stop+(p.trailing?' trailing':''))+td('$'+p.tp)+'<td class="mono '+(number(p.pnl)>=0?'green':'red')+'">'+signedMoney(p.pnl)+'<div class="muted">'+esc(p.pnl_pct)+'%</div></td>'+td(p.held_days+'d')+'</tr>').join(''),'medium'):
    '<div class="empty"><strong>No open positions</strong>The evidence gate is still collecting, so automatic execution remains locked.</div>';
}
function renderLeaderboard(s){
  const rows=s.watchlist||[];
  $('watchlist').innerHTML=rows.length?table(['#','Ticker','Price','Signal','Levels','Scores','Why'],rows.map(w=>{
    const signal=w.signal||{},ai=w.ai||{},confirmation=w.confirmation||{},action=signal.action||'';
    const signalClass=action==='STRONG BUY'?'s-strong':action==='BUY'?'s-buy':action==='RESEARCH'?'s-research':action==='WATCH'?'s-watch':action==='NO TRADE'?'s-no':'s-avoid';
    const rankClass='rank'+(w.rank===1?' t1':w.rank===2?' t2':w.rank===3?' t3':'');const change=number(w.change_pct);
    const tradable=['BUY','STRONG BUY'].includes(action);
    const levels=tradable?'<div class="levels"><b>entry</b> <span class="entry">$'+esc(signal.entry)+'</span><br><b>stop</b> <span class="stop">$'+esc(signal.stop)+'</span><br><b>targets</b> <span class="target">$'+esc(signal.target1)+' / $'+esc(signal.target2)+'</span></div>':
      action==='RESEARCH'?'<span class="muted">TRACK ONLY<br>no trade levels</span>':'<span class="muted">-</span>';
    const scores='<div class="mini"><span>hype <i>'+esc(w.hype)+'</i></span><span>technical <i>'+esc(w.technical)+'</i></span><span>catalyst <i>'+esc(w.catalyst)+'</i></span><span>quality <i>'+esc(w.quality)+'</i></span><span>tradeable <i>'+esc(w.tradeability)+'</i></span></div>';
    const drivers=[].concat(w.hype_why||[],w.technical_why||[],w.catalyst_why||[],w.quality_why||[]).slice(0,4).map(x=>'<span class="tag">'+esc(x)+'</span>').join('');
    const deep=ai.bull_case?'<details class="why"><summary>AI reasoning</summary><div class="reason">'+reasonLine('Bull',ai.bull_case,'bull')+reasonLine('Bear',ai.bear_case,'bear')+reasonLine('Catalyst',ai.catalyst_assessment)+reasonLine('Verdict',ai.why_this_verdict)+reasonLine('Cost',ai.cost_hurdle)+reasonLine('Watch',ai.what_to_watch)+'</div></details>':
      w.ai_error?'<div class="muted">AI error: '+esc(w.ai_error)+'</div>':w.ai_skip_reason?'<div class="muted">AI '+esc(w.ai_skip_reason)+'</div>':'';
    return '<tr><td><div class="'+rankClass+'">'+esc(w.rank)+'</div></td><td><span class="ticker">'+esc(w.ticker)+'</span>'+(w.held?'<span class="held">HELD</span>':'')+'<div class="muted">'+esc(w.name||'')+'</div></td>'+
      '<td class="mono nowrap">$'+esc(w.price)+'<div class="'+(change>=0?'green':'red')+'">'+(change>=0?'+':'')+esc(change)+'%</div><div class="muted">'+esc(w.spread_pct)+'% '+(w.spread_unreliable?'cost proxy':'live spread')+'</div></td>'+
      '<td><span class="signal '+signalClass+'">'+esc(action||'NO SIGNAL')+'</span>'+scoreBar(w.composite)+'<div class="muted">'+esc(signal.why||'')+'</div>'+(tradable?'<div class="muted">confirmation '+number(confirmation.observations)+' / '+number(confirmation.required||s.confirmation_required||2)+(confirmation.executable_observation?'':' - awaiting trusted quote')+'</div>':'')+'</td>'+
      '<td>'+levels+'</td><td>'+scores+'</td><td>'+drivers+deep+'</td></tr>';
  }).join(''),'wide'):'<div class="empty"><strong>No completed scan yet</strong>The scanner ranks candidates automatically on its normal schedule.</div>';
  $('rules').innerHTML=esc(s.rules||'')+'<br><br>'+esc(s.note||'')+'<br><br>A dollar-volume cost proxy is used only for ranking. It is never treated as an executable quote or used for a paper fill.';
}
function renderSignals(s){
  const rows=(s.watchlist||[]).filter(w=>['BUY','STRONG BUY','RESEARCH'].includes((w.signal||{}).action));
  $('signals').innerHTML=rows.length?table(['Ticker','Action','Entry','Stop','Target 1','Confirmation','Status'],rows.map(w=>{
    const signal=w.signal||{},confirmation=w.confirmation||{},tradable=['BUY','STRONG BUY'].includes(signal.action);const cls=signal.action==='STRONG BUY'?'s-strong':signal.action==='BUY'?'s-buy':'s-research';
    return '<tr><td class="ticker">'+esc(w.ticker)+'</td><td><span class="signal '+cls+'">'+esc(signal.action)+'</span></td>'+td(tradable?'$'+signal.entry:'reference $'+signal.entry)+td(tradable?'$'+signal.stop:'-')+td(tradable?'$'+signal.target1:'-')+
      '<td class="mono">'+number(confirmation.observations)+' / '+number(confirmation.required||s.confirmation_required||2)+(confirmation.confirmed?' <span class="pill p-buy">CONFIRMED</span>':'')+'</td><td>'+(w.held?'<span class="held">IN BOOK</span>':signal.action==='RESEARCH'?'<span class="pill p-watch">TRACK ONLY</span>':signal.needs_open_recheck?'<span class="pill p-watch">RECHECK AT OPEN</span>':'<span class="muted">ready</span>')+'</td></tr>';
  }).join(''),'medium'):'<div class="empty"><strong>No qualifying setup right now</strong>The scanner is still running. AI review starts only after a name passes the mechanical setup gates.</div>';
}
function renderValidation(s){
  const stats=s.signal_stats||{},fv=s.forward_validation||{},av=s.ai_value_audit||{},clock=s.evidence_clock||{};
  const rows=['1','5','10'].map(h=>{const x=stats[h]||{};return '<tr>'+td(h+' session'+(h==='1'?'':'s'))+td(x.count||0)+td(x.count?x.net_hit_rate+'%':'collecting')+td(x.count?x.avg_net_return_pct+'%':'-')+td(x.count&&x.avg_net_excess_pct!=null?x.avg_net_excess_pct+'%':'-')+td(x.count?x.target1_rate+'%':'-')+td(x.count?x.stop_rate+'%':'-')+'</tr>';}).join('');
  const summary=number(fv.signal_days)===0?'<div class="note"><b>Why validation is still collecting:</b> no signal day has completed the outcome horizon. The scanner can run hundreds of times, but repeated scans are not independent evidence. Progress begins only when a qualifying setup is recorded and its full '+number(fv.horizon_sessions||5)+'-session result is available.</div>':'';
  const methodology='<details class="method"><summary>Methodology and limitations</summary><div class="method-copy"><b>Forward verdict:</b> '+esc(fv.status||'COLLECTING')+' - '+esc(fv.reason||'waiting for confirmed observations')+'<br><b>Evidence unit:</b> '+esc(fv.grouping||'signal-day baskets')+'; '+number(fv.signal_days)+' / '+number(fv.minimum_signal_days||60)+' required days. This panel never unlocks trading by itself.<br><b>AI filter audit:</b> '+esc(av.status||'COLLECTING')+' - '+esc(av.reason||'waiting for complete comparison days')+'; '+number(av.comparison_days)+' / '+number(av.minimum_comparison_days||60)+' days.<br><b>Evidence clock:</b> '+number(clock.completed_signal_days)+' completed, '+number(clock.benchmarked_signal_days)+' benchmarked, '+number(clock.unresolved_rows)+' unresolved rows. Resolved-row averages above are diagnostic only and may contain survivor bias. The IWM-relative AI leg is a consistency check, not an independent confirmation.</div></details>';
  $('accuracy').innerHTML=summary+table(['Horizon','Completed signals','Net positive','Avg after cost','Avg vs IWM','Target 1 touched','Stop touched'],rows,'medium')+methodology;
}
function renderHistoryAndLog(s){
  const rows=s.history||[];$('history').innerHTML=rows.length?table(['Ticker','Qty','Entry','Exit','P&L','Why','Spread'],rows.map(h=>'<tr><td class="ticker">'+esc(h.ticker)+'</td>'+td(h.qty)+td('$'+h.entry)+td('$'+h.exit)+'<td class="mono '+(number(h.pnl)>=0?'green':'red')+'">'+signedMoney(h.pnl)+'<div class="muted">'+esc(h.pnl_pct)+'%</div></td>'+td(h.reason)+td(money(-number(h.spread_cost)))+'</tr>'),'medium'):'<div class="empty"><strong>No closed trades yet</strong>Completed paper outcomes will appear here.</div>';
  $('log').innerHTML=(s.log||[]).map(item=>'<div class="'+esc(item.kind||'')+'">'+esc(item.msg)+'</div>').join('')||'<div class="empty">No scanner activity yet.</div>';
}
function render(s){renderHeader(s);renderStats(s);renderUniverse(s);renderPositions(s);renderSignals(s);renderLeaderboard(s);renderValidation(s);renderHistoryAndLog(s);}
async function load(){try{const response=await fetch('/api/penny/state',{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);const state=await response.json();const hash=JSON.stringify(state);if(hash===lastHash)return;lastHash=hash;render(state);}catch(error){$('alerts').innerHTML='<div class="alert">Dashboard connection lost. Retrying automatically.</div>';}}
async function toggleBot(){const response=await fetch('/api/penny/state');const state=await response.json();await fetch('/api/penny/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!state.enabled})});lastHash='';load();}
async function resetBot(){if(!confirm('Reset the AI penny-stock paper account to $100?'))return;await fetch('/api/penny/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});lastHash='';load();}
load();setInterval(load,5000);document.addEventListener('visibilitychange',()=>{if(!document.hidden){lastHash='';load();}});
</script>
</body>
</html>
"""
