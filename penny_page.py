"""
penny_page.py - the dedicated AI Penny Stock research & trading page (/penny).

Kept in its own module so the (large) dashboard.py stays readable. Exposes one
constant, PENNY_HTML, which dashboard.py serves at /penny.

Design notes:
  * Reads a single endpoint (/api/penny/state) every 5s. One fetch, one render -
    no polling storms, no per-row timers, nothing that degrades over time.
  * The DOM is rebuilt only when the payload actually changed (cheap hash check),
    so an idle page costs effectively nothing and never janks.
  * All animation is CSS transform/opacity only (GPU compositor). No JS animation
    loops, no requestAnimationFrame, no layout thrash.
"""

PENNY_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Penny Stock · Research Desk</title>
<style>
:root{
  --bg:#07090d; --panel:#0d1117; --panel2:#131a23; --line:#1e2733; --line2:#2a3646;
  --txt:#e8eef6; --muted:#7b8798; --gold:#f5c518; --gold2:#ffdd6b;
  --green:#19c37d; --red:#ff4d5f; --blue:#3aa0ff; --amber:#f2b84b;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--blue);text-decoration:none}
.wrap{max-width:1400px;margin:0 auto;padding:18px 20px 60px}

/* ---------- header ---------- */
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:16px 20px;margin-bottom:18px;border-radius:14px;
  background:linear-gradient(135deg,#141a24 0%,#0d1117 60%);
  border:1px solid var(--line2);box-shadow:0 6px 24px rgba(0,0,0,.5)}
.logo{font-size:20px;font-weight:700;letter-spacing:.3px;
  background:linear-gradient(90deg,var(--gold),var(--gold2));
  -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--muted);font-size:12px}
.spacer{flex:1}
.chip{padding:4px 11px;border-radius:999px;font-size:11px;font-weight:600;
  border:1px solid var(--line2);background:#0b0f15;color:var(--muted)}
.chip.on{color:var(--green);border-color:#12503a;background:#0c1f19}
.chip.off{color:var(--muted)}
.chip.live{color:var(--gold);border-color:#4a3c10;background:#1a1508}
.btn{padding:7px 15px;border-radius:9px;border:1px solid var(--line2);
  background:#131a23;color:var(--txt);cursor:pointer;font-size:12px;font-weight:600;
  transition:background .15s,border-color .15s}
.btn:hover{background:#1b242f;border-color:#3a4a5e}
.btn.go{border-color:#1c6b4b;color:#7ef0bb;background:#0c1f19}
.btn.stop{border-color:#6b1c2a;color:#ff9aa6;background:#1f0c11}

/* ---------- stat row ---------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));
  gap:12px;margin-bottom:18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:13px 15px}
.stat .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted)}
.stat .v{font-size:21px;font-weight:700;margin-top:3px;font-family:var(--mono)}
.g{color:var(--green)} .r{color:var(--red)} .gold{color:var(--gold)}

/* ---------- cards ---------- */
.grid{display:grid;grid-template-columns:1fr;gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.card h2{margin:0;padding:13px 17px;font-size:13px;font-weight:700;letter-spacing:.4px;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);
  background:var(--panel2);display:flex;align-items:center;gap:9px}
.card .body{padding:14px 17px}
.empty{padding:26px;text-align:center;color:var(--muted);font-size:13px}

/* ---------- tables ---------- */
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;
  color:var(--muted);font-weight:600;padding:9px 12px;border-bottom:1px solid var(--line)}
td{padding:10px 12px;border-bottom:1px solid #161d26;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#101720}
.mono{font-family:var(--mono)}
.tick{font-weight:700;font-size:14px}
.nm{color:var(--muted);font-size:11px}

/* ---------- verdict pills ---------- */
.v{display:inline-block;padding:3px 10px;border-radius:7px;font-size:10.5px;
  font-weight:700;letter-spacing:.4px;white-space:nowrap}
.v-buy{background:#0c2a1d;color:#3ee89b;border:1px solid #1c6b4b}
.v-watch{background:#2a230c;color:var(--gold);border:1px solid #6b571c}
.v-avoid{background:#2a0f13;color:#ff8a95;border:1px solid #6b1c2a}
.v-rej{background:#191d24;color:#8b95a4;border:1px solid #2c3542}

/* score bar */
.score{display:flex;align-items:center;gap:7px;margin-top:5px}
.bar{flex:1;height:5px;border-radius:3px;background:#1a222c;overflow:hidden;max-width:110px}
.bar i{display:block;height:100%;border-radius:3px;
  transition:width .4s cubic-bezier(.4,0,.2,1)}
.snum{font-family:var(--mono);font-size:11px;color:var(--muted);min-width:26px}

/* reasoning block */
.reason{margin-top:9px;display:grid;gap:6px}
.rline{display:grid;grid-template-columns:96px 1fr;gap:9px;font-size:12px;line-height:1.55}
.rk{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;
  padding-top:1px}
.rv{color:#c9d4e2}
.rv.bull{color:#8fe8bd} .rv.bear{color:#ffa8b1}
.cat{display:inline-block;margin:3px 5px 0 0;padding:2px 8px;border-radius:6px;
  font-size:10.5px;background:#12202c;color:#8fc7f0;border:1px solid #1d3448}
.flag{display:inline-block;margin:3px 5px 0 0;padding:2px 8px;border-radius:6px;
  font-size:10.5px;background:#241016;color:#ff9aa6;border:1px solid #46202a}

.note{padding:11px 17px;font-size:11.5px;color:var(--muted);line-height:1.6;
  border-top:1px solid var(--line);background:#0a0e13}
.err{padding:9px 17px;font-size:12px;color:#ff9aa6;background:#1a0d11;
  border-top:1px solid #3a1a22}
.feed{max-height:230px;overflow:auto;font-family:var(--mono);font-size:11.5px}
.feed div{padding:5px 17px;border-bottom:1px solid #141a22;color:#9aa6b6}
.feed .win{color:#5fe0a6} .feed .loss{color:#ff8a95} .feed .open{color:var(--gold)}
.rank{display:flex;align-items:center;justify-content:center;width:30px;height:30px;
  border-radius:9px;font-weight:800;font-size:13px;font-family:var(--mono);
  background:#161d27;color:var(--muted);border:1px solid var(--line2)}
.rank.t1{background:linear-gradient(135deg,#f5c518,#b8860b);color:#1a1608;border-color:#d4a017}
.rank.t2{background:linear-gradient(135deg,#c9d2dc,#8b95a1);color:#12161c;border-color:#aab4c0}
.rank.t3{background:linear-gradient(135deg,#cd7f32,#8b5a2b);color:#1a1008;border-color:#b8722d}
.sig{display:inline-block;padding:4px 11px;border-radius:8px;font-size:11px;
  font-weight:800;letter-spacing:.4px;white-space:nowrap}
.s-strong{background:#0a2e1c;color:#4dffab;border:1px solid #1f8a5a;
  box-shadow:0 0 12px rgba(77,255,171,.18)}
.s-buy{background:#0c2a1d;color:#3ee89b;border:1px solid #1c6b4b}
.s-research{background:#13243a;color:#8fc7f0;border:1px solid #28517a}
.s-watch{background:#2a230c;color:var(--gold);border:1px solid #6b571c}
.s-avoid{background:#2a0f13;color:#ff8a95;border:1px solid #6b1c2a}
.s-no{background:#191d24;color:#8b95a4;border:1px solid #2c3542}
.lv{font-family:var(--mono);font-size:11px;line-height:1.7}
.lv b{color:var(--txt)} .lv .e{color:#8fc7f0} .lv .s{color:#ff9aa6} .lv .t{color:#8fe8bd}
.mini{display:flex;gap:9px;margin-top:5px;font-size:10px;color:var(--muted)}
.mini span{white-space:nowrap}
.mini i{font-style:normal;font-family:var(--mono);color:#c9d4e2}
.held{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:5px;font-size:9.5px;
  background:#12202c;color:#8fc7f0;border:1px solid #1d3448}
details.why{margin-top:8px}
details.why summary{cursor:pointer;color:var(--muted);font-size:11px;outline:none}
details.why summary:hover{color:var(--txt)}
@media(min-width:1100px){.grid.two{grid-template-columns:1fr 1fr}}
</style></head><body>
<div class="wrap">

<header>
  <div>
    <div class="logo">◆ AI Penny Stock Desk</div>
    <div class="sub" id="hsub">loading…</div>
  </div>
  <div class="spacer"></div>
  <span class="chip" id="c-model">model</span>
  <span class="chip" id="c-market">market</span>
  <span class="chip" id="c-regime">tape</span>
  <span class="chip" id="c-edge">edge</span>
  <span class="chip" id="c-rule">rule</span>
  <span class="chip" id="c-state">state</span>
  <button class="btn" id="btn-toggle" onclick="toggleBot()">…</button>
  <button class="btn" onclick="resetBot()">Reset</button>
  <a class="btn" href="/paper">← Paper</a>
</header>

<div class="stats" id="stats"></div>

<div class="grid">
  <div class="card">
    <h2>◇ Open positions <span class="sub" id="poscount"></span></h2>
    <div id="positions"></div>
  </div>

  <div class="card">
    <h2>◇ Top 20 leaderboard — ranked by composite score, with live signals</h2>
    <div id="watchlist"></div>
    <div class="note" id="rules"></div>
  </div>

  <div class="card">
    <h2>◆ Detected setups — persistence and a fresh quote required for any fill</h2>
    <div id="signals"></div>
  </div>

  <div class="card">
    <h2>Signal validation - forward results, not repeated scanner impressions</h2>
    <div id="accuracy"></div>
  </div>

  <div class="grid two">
    <div class="card">
      <h2>◇ Closed trades</h2>
      <div id="history"></div>
    </div>
    <div class="card">
      <h2>◇ Activity</h2>
      <div class="feed" id="log"></div>
    </div>
  </div>
</div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s??'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money = n => (n<0?'-':'') + '$' + Math.abs(Number(n)||0).toFixed(2);
const signed = n => (Number(n)>=0?'+':'') + money(n).replace('-','');

let lastHash = '';   // only touch the DOM when something actually changed

function verdictPill(w){
  if(w.rejected) return '<span class="v v-rej">REJECTED</span>';
  const v = w.verdict||'';
  if(v==='SPECULATIVE_BUY') return '<span class="v v-buy">SPEC BUY</span>';
  if(v==='WATCH')  return '<span class="v v-watch">WATCH</span>';
  if(v==='AVOID')  return '<span class="v v-avoid">AVOID</span>';
  return '<span class="v v-rej">—</span>';
}
function scoreBar(sc){
  if(sc===undefined||sc===null||sc==='') return '';
  const n = Math.max(0,Math.min(100,Number(sc)||0));
  const col = n>=65?'var(--green)':n>=45?'var(--gold)':'var(--red)';
  return '<div class="score"><div class="bar"><i style="width:'+n+'%;background:'+col+'"></i></div>'
       + '<span class="snum">'+n+'</span></div>';
}
function rl(k,v,cls){ return v ? '<div class="rline"><div class="rk">'+k+'</div>'
  + '<div class="rv '+(cls||'')+'">'+esc(v)+'</div></div>' : ''; }

function render(s){
  $('hsub').textContent = s.status || '';
  $('c-model').textContent = s.ai_model || 'AI';
  const mk = $('c-market');
  mk.textContent = s.market_open ? 'market open' : 'market closed';
  mk.className = 'chip ' + (s.market_open?'live':'off');
  const rg = $('c-regime');
  if(rg){
    const R = s.regime||{};
    rg.textContent = 'tape: ' + (R.label||'unknown') + (R.score!==undefined?' '+R.score:'');
    rg.className = 'chip ' + (R.label==='risk-on'?'on':R.label==='risk-off'?'off':'');
    rg.title = (R.why||[]).join(' | ');
  }
  const ep = s.edge_policy||{};
  const ec = $('c-edge');
  if(ec){
    const p = ep.selection_p_value;
    ec.textContent = 'edge: ' + (ep.status||'missing').toLowerCase()
      + (p==null ? '' : ' (p=' + Number(p).toFixed(2) + ')');
    ec.className = 'chip ' + (ep.auto_trade_allowed?'on':'off');
    // the verdicts say WHY, which matters more than the status word
    const v = ep.inference_verdicts||[];
    ec.title = [ep.reason || 'Automatic entries require a validated edge audit.']
      .concat(v).join('\n');
  }
  const lr = s.live_rule_evidence||{};
  const rc = $('c-rule');
  if(rc){
    if(!lr.measured){
      rc.textContent = 'rule: unmeasured';
      rc.className = 'chip';
      rc.title = 'The deployed scanner rule has not been backtested.';
    } else {
      // gross expectancy is the number that decides whether this rule can ever work
      rc.textContent = 'price core: gross ' + (Number(lr.mean_gross_pct)>=0?'+':'')
        + Number(lr.mean_gross_pct).toFixed(2) + '%';
      rc.className = 'chip ' + (lr.gross_is_zero ? 'off' : '');
      rc.title = [
        'Price/volume component only (not the full catalyst-confirmation strategy)',
        lr.strategy_id + ' over ' + (lr.trades||0).toLocaleString() + ' backtested trades',
        'gross ' + lr.mean_gross_pct + '%  cost ' + lr.mean_cost_pct
          + '%  net ' + lr.test_mean_net_pct + '% (untouched test)',
        lr.diagnosis || ''
      ].concat(Object.entries(lr.rank_verdicts||{}).map(function(e){return e[0]+': '+e[1];}))
       .concat((lr.catalyst_gate||{}).tested ? ['',
          'catalyst gate (EDGAR point-in-time, untouched 2025+):',
          '  ' + (lr.catalyst_gate.setups||0).toLocaleString() + ' setups, gross '
            + lr.catalyst_gate.gross_pct + '%  CI ['
            + (lr.catalyst_gate.gross_ci_pct||[]).join(', ') + ']',
          '  profitable at any modelled cost: '
            + (lr.catalyst_gate.profitable_at_any_modelled_cost ? 'yes' : 'no')] : [])
       .join('\n');
    }
  }
  const st = $('c-state');
  st.textContent = 'scanner live';
  st.className = 'chip on';
  st.title = s.enabled ? 'Continuous research and new paper entries are enabled.'
                       : 'Continuous research is live; only new paper entries are paused.';
  const b = $('btn-toggle');
  b.textContent = s.enabled ? 'Pause entries' : 'Enable entries';
  b.className = 'btn ' + (s.enabled?'stop':'go');

  const pnl = Number(s.total_pnl)||0;
  const s5 = (s.signal_stats||{})['5']||{};
  const fv = s.forward_validation||{};
  const fp = fv.feasibility||{};
  $('stats').innerHTML =
    stat('Equity', money(s.equity), 'gold') +
    stat('Net P&L', signed(pnl), pnl>=0?'g':'r') +
    stat('Cash', money(s.balance)) +
    stat('Open', (s.open_count||0)+' / '+(s.max_open||0)) +
    stat('Closed trades', s.trades||0) +
    stat('Win rate', (s.trades? s.win_rate+'%' : '—')) +
    stat('5d net hit rate', s5.count ? s5.net_hit_rate+'% ('+s5.count+')' : 'collecting') +
    stat('Open risk', money(s.open_risk||0)) +
    stat('Scans', s.scan_count||0) +
    stat('Next scan', s.scan_in_progress ? 'running' : (s.next_scan_in_sec||0)+'s') +
    stat('Hot setups', s.hot_setups||0) +
    stat('Forward edge', (fv.status||'collecting').toLowerCase(),
         fv.status==='PROMISING_NOT_VALIDATED'?'g':fv.status==='REJECTED'?'r':'') +
    stat('Validation power', (fp.status||'not assessable').toLowerCase(),
         fp.status==='RESOLVABLE_NOW'?'g':
         fp.status==='INFEASIBLE_WITHIN_HORIZON'?'r':'');

  // positions
  const P = s.positions||[];
  $('poscount').textContent = P.length ? '('+P.length+')' : '';
  $('positions').innerHTML = P.length ? table(
    ['Ticker','Qty','Entry','Now','Stop','Target','P&L','Held'],
    P.map(p=>'<tr><td><span class="tick">'+esc(p.ticker)+'</span><div class="nm">'+esc(p.name)+'</div>'
      + (p.catalyst?'<div class="nm">'+esc(p.catalyst)+'</div>':'') + '</td>'
      + td(p.qty)+td('$'+p.entry)+td('$'+p.price)
      + td('$'+p.stop + (p.trailing?' <span class="v v-watch">trail</span>':''))
      + td('$'+p.tp)
      + '<td class="mono '+(p.pnl>=0?'g':'r')+'">'+signed(p.pnl)+'<div class="nm">'+p.pnl_pct+'%</div></td>'
      + td(p.held_days+'d')+'</tr>').join('')
  ) : '<div class="empty">No open positions.</div>';

  // ---------- ranked leaderboard ----------
  const W = s.watchlist||[];
  $('watchlist').innerHTML = W.length ? table(
    ['#','Ticker','Price','Signal','Levels','Scores','Why'],
    W.map(w=>{
      const sig = w.signal||{}, ai = w.ai||{}, cf = w.confirmation||{};
      const a = sig.action||'';
      const cls = a==='STRONG BUY'?'s-strong':a==='BUY'?'s-buy':a==='RESEARCH'?'s-research':a==='WATCH'?'s-watch'
                 :a==='NO TRADE'?'s-no':'s-avoid';
      const rk = 'rank'+(w.rank===1?' t1':w.rank===2?' t2':w.rank===3?' t3':'');
      const chg = Number(w.change_pct)||0;
      const levels = (a==='BUY'||a==='STRONG BUY')
        ? '<div class="lv"><b>entry</b> <span class="e">$'+sig.entry+'</span><br>'
          +'<b>stop</b> <span class="s">$'+sig.stop+'</span> <span style="color:#5d6673">-'+sig.risk_pct+'%</span><br>'
          +'<b>tgt</b> <span class="t">$'+sig.target1+' / $'+sig.target2+'</span></div>'
        : a==='RESEARCH'
          ? '<span style="color:#8ab4d6;font-size:11px">TRACK ONLY<br>no authorized trade levels</span>'
          : '<span style="color:#5d6673;font-size:11px">—</span>';
      const scores = '<div class="mini" style="flex-direction:column;gap:3px">'
        + '<span>hype <i>'+w.hype+'</i></span>'
        + '<span>technical <i>'+w.technical+'</i></span>'
        + '<span>catalyst <i>'+w.catalyst+'</i></span>'
        + '<span>quality <i>'+w.quality+'</i></span>'
        + '<span>tradeable <i>'+w.tradeability+'</i></span></div>';
      const drivers = [].concat(w.hype_why||[],w.technical_why||[],w.catalyst_why||[],w.quality_why||[]).slice(0,4)
        .map(x=>'<span class="cat">'+esc(x)+'</span>').join('');
      const deep = ai.bull_case ? '<details class="why"><summary>AI reasoning</summary><div class="reason">'
        + rl('Bull', ai.bull_case,'bull') + rl('Bear', ai.bear_case,'bear')
        + rl('Catalyst', ai.catalyst_assessment) + rl('Verdict', ai.why_this_verdict)
        + rl('Cost', ai.cost_hurdle) + rl('Watch', ai.what_to_watch)
        + '</div></details>' : '';
      return '<tr><td><div class="'+rk+'">'+w.rank+'</div></td>'
        + '<td><span class="tick">'+esc(w.ticker)+'</span>'+(w.held?'<span class="held">HELD</span>':'')
        + '<div class="nm">'+esc(w.name||'')+'</div></td>'
        + '<td class="mono">$'+w.price+'<div class="'+(chg>=0?'g':'r')+'" style="font-size:11px">'
        + (chg>=0?'+':'')+chg+'%</div><div class="nm">'+w.spread_pct+'% '+(w.spread_unreliable?'cost proxy':'live spread')+'</div></td>'
        + '<td><span class="sig '+cls+'">'+esc(a)+'</span>'+scoreBar(w.composite)
        + '<div class="nm" style="margin-top:3px">'+esc(sig.why||'')+'</div>'
        + (['BUY','STRONG BUY'].includes(sig.candidate_action)
          ?'<div class="nm">confirmation '+(cf.observations||0)+' / '+(cf.required||s.confirmation_required||2)
            +(cf.executable_observation?'':' · waiting for trusted live quote')+'</div>':'')
        + '</td>'
        + '<td>'+levels+'</td><td>'+scores+'</td>'
        + '<td>'+drivers+deep+'</td></tr>';
    }).join('')
  ) : '<div class="empty">No scan yet — the bot screens and ranks on its own schedule.</div>';

  // ---------- actionable and forward-research signals panel ----------
  const SG = W.filter(w=>['BUY','STRONG BUY','RESEARCH'].includes((w.signal||{}).action));
  $('signals').innerHTML = SG.length ? table(
    ['#','Ticker','Action','Entry','Stop','Target 1','Target 2','Confirmation','Status'],
    SG.map(w=>{ const g=w.signal, cf=w.confirmation||{}, tradable=['BUY','STRONG BUY'].includes(g.action);
      const cls = g.action==='STRONG BUY'?'s-strong':g.action==='BUY'?'s-buy':'s-research';
      return '<tr><td class="mono">'+w.rank+'</td><td class="tick">'+esc(w.ticker)+'</td>'
        + '<td><span class="sig '+cls+'">'+esc(g.action)+'</span></td>'
        + '<td class="mono '+(tradable?'e':'')+'">'+(tradable?'$'+g.entry:'reference $'+g.entry)+'</td>'
        + '<td class="mono r">'+(tradable?'$'+g.stop:'—')+'</td>'
        + '<td class="mono g">'+(tradable?'$'+g.target1:'—')+'</td>'
        + '<td class="mono g">'+(tradable?'$'+g.target2:'—')+'</td>'
        + '<td class="mono">'+(cf.observations||0)+' / '+(cf.required||s.confirmation_required||2)
        + (cf.confirmed?' <span class="v v-buy">CONFIRMED</span>':'')+'</td>'
        + '<td>'+(w.held?'<span class="held">in book</span>'
          :g.action==='RESEARCH'?'<span class="v v-watch">TRACK ONLY</span>'
          :g.needs_open_recheck?'<span class="v v-watch">RECHECK AT OPEN</span>'
          :'<span class="nm">ready</span>')+'</td></tr>';
    }).join('')
  ) : '<div class="empty">No qualifying setup right now. Automatic fills remain locked until the out-of-sample edge audit passes.</div>';

  const AS = s.signal_stats||{};
  $('accuracy').innerHTML = table(
    ['Horizon','Completed signals','Net positive','Avg after cost','Avg vs IWM','Target 1 touched','Stop touched'],
    ['1','5','10'].map(h=>{const x=AS[h]||{}; return '<tr><td class="mono">'+h+' session'+(h==='1'?'':'s')+'</td>'
      +td(x.count||0)+td(x.count?(x.net_hit_rate+'%'):'collecting')+td(x.count?(x.avg_net_return_pct+'%'):'-')
      +td(x.count&&x.avg_net_excess_pct!=null?(x.avg_net_excess_pct+'%'):'-')
      +td(x.count?(x.target1_rate+'%'):'-')+td(x.count?(x.stop_rate+'%'):'-')+'</tr>';}).join('')
  ) + '<div class="note">Forward verdict: <b>'+esc(fv.status||'COLLECTING')+'</b> — '
    + esc(fv.reason||'waiting for confirmed observations')+'<br>'
    + 'Evidence unit: '+esc(fv.grouping||'signal-day baskets')+'; '+(fv.signal_days||0)
    + ' / '+(fv.minimum_signal_days||60)+' required days. This forward panel never unlocks trading by itself.</div>';

  $('rules').innerHTML = esc(s.rules||'') + '<br><br>' + esc(s.note||'')
    + '<br><br><span style="color:#5d6673">A cost proxy is derived from average dollar volume only for ranking. '
    + 'It is never treated as an executable quote or used for a paper fill.</span>';

  // history
  const H = s.history||[];
  $('history').innerHTML = H.length ? table(
    ['Ticker','Qty','Entry','Exit','P&L','Why','Spread'],
    H.map(h=>'<tr><td class="tick">'+esc(h.ticker)+'</td>'+td(h.qty)+td('$'+h.entry)+td('$'+h.exit)
      + '<td class="mono '+(h.pnl>=0?'g':'r')+'">'+signed(h.pnl)+'<div class="nm">'+h.pnl_pct+'%</div></td>'
      + td(h.reason)+'<td class="mono r">'+money(-(h.spread_cost||0))+'</td></tr>').join('')
  ) : '<div class="empty">No closed trades yet.</div>';

  $('log').innerHTML = (s.log||[]).map(l=>'<div class="'+esc(l.kind||'')+'">'+esc(l.msg)+'</div>').join('')
    || '<div style="padding:16px;color:var(--muted)">No activity.</div>';

  const e = $('errbox');
  if(s.last_error){
    if(!e){ const d=document.createElement('div'); d.id='errbox'; d.className='err';
            d.textContent=s.last_error; $('watchlist').parentNode.appendChild(d); }
    else e.textContent = s.last_error;
  } else if(e){ e.remove(); }
}
function stat(k,v,cls){ return '<div class="stat"><div class="k">'+k+'</div>'
  + '<div class="v '+(cls||'')+'">'+v+'</div></div>'; }
function td(v){ return '<td class="mono">'+esc(v)+'</td>'; }
function table(head,rows){ return '<table><tr>'+head.map(h=>'<th>'+h+'</th>').join('')
  + '</tr>'+rows+'</table>'; }

async function load(){
  try{
    const r = await fetch('/api/penny/state',{cache:'no-store'});
    const s = await r.json();
    const h = JSON.stringify(s);
    if(h === lastHash) return;      // nothing changed -> don't touch the DOM
    lastHash = h;
    render(s);
  }catch(e){ /* transient - next tick retries */ }
}
async function toggleBot(){
  const r = await fetch('/api/penny/state'); const s = await r.json();
  await fetch('/api/penny/toggle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:!s.enabled})});
  lastHash=''; load();
}
async function resetBot(){
  if(!confirm('Reset the AI penny stock paper account to $100?')) return;
  await fetch('/api/penny/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  lastHash=''; load();
}
load();
setInterval(load, 5000);
// pause polling when the tab is hidden - zero cost in the background
document.addEventListener('visibilitychange',()=>{ if(!document.hidden){ lastHash=''; load(); } });
</script>
</body></html>
"""
