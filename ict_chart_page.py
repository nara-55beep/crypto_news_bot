"""ict_chart_page.py — the HTML for the ICT SM Trades chart page (/ict).
TradingView-style candles (Lightweight Charts) + the ICT SM drawings: Pivot/MSS/✕ markers,
FVG boxes, killzone shading and diamonds (custom canvas overlay). Data from /api/ictsm."""

PAGE_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ICT SM Trades — Chart</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root{--bg:#0a0b0e;--panel:#0e1014;--line:#1d212b;--txt:#d4d8e0;--sub:#8b93a3;}
  *{box-sizing:border-box} html,body{margin:0;height:100%;background:var(--bg);color:var(--txt);
    font-family:'IBM Plex Mono',ui-monospace,Menlo,Consolas,monospace}
  #top{display:flex;align-items:center;gap:14px;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--panel)}
  #top .ttl{font-weight:700;color:#f5d020} #top .pair{color:var(--sub)}
  select,a.nav{background:#14171e;border:1px solid var(--line);border-radius:6px;color:var(--txt);
    padding:5px 9px;font-family:inherit;font-size:12px;text-decoration:none;cursor:pointer}
  #legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--sub);padding:6px 14px;border-bottom:1px solid var(--line)}
  #legend b{color:var(--txt)} .sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:4px}
  #wrap{display:flex;height:calc(100vh - 84px)}
  #chart{flex:1;min-width:0;position:relative}
  #status{margin-left:auto;font-size:12px} #ohlc{color:var(--sub);font-size:12px}
  #bot{flex:0 0 248px;width:248px;background:var(--panel);border-right:1px solid var(--line);
    padding:12px 13px;font-size:12px;display:flex;flex-direction:column;overflow:auto}
  #bot .bhd{display:flex;align-items:center;gap:7px}
  #bot .dot{width:8px;height:8px;border-radius:50%;background:#3a4252;flex:0 0 auto}
  #bot .dot.on{background:#26a69a;box-shadow:0 0 7px #26a69a}
  #bot .bnm{font-weight:700;color:#f5d020;font-size:13px}
  #bot .closebtn{margin-top:9px;width:100%;border:1px solid #ef5350;background:#1c1010;color:#ff8a88;
    border-radius:6px;padding:8px;cursor:pointer;font-family:inherit;font-size:12px;font-weight:600}
  #bot .closebtn:hover{background:#2a1414}
  #bot .tgs{display:flex;gap:7px;margin:10px 0 12px}
  #bot .tg{flex:1;border:1px solid #283041;background:#14171e;color:var(--sub);border-radius:6px;
    padding:6px 0;cursor:pointer;font-family:inherit;font-size:12px;text-align:center}
  #bot #botTg.on{border-color:#26a69a;color:#26a69a;background:#0e1a18}
  #bot #sndTg.on{border-color:#f5d020;color:#f5d020;background:#1a1705}
  #bot #sndTg.muted{border-color:#5a2a2a;color:#ef5350;background:#160e0e}
  #bot .grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:12px}
  #bot .cell{background:#14171e;border:1px solid var(--line);border-radius:7px;padding:7px 9px}
  #bot .cell .k{color:var(--sub);font-size:9.5px;text-transform:uppercase;letter-spacing:.4px}
  #bot .cell .v{font-size:15px;font-weight:600;margin-top:3px}
  #bot .pos.long{color:#26a69a} #bot .pos.short{color:#ef5350}
  #bot .lbl{color:var(--sub);font-size:9.5px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:5px}
  #bot .hist{flex:1;min-height:50px;overflow:auto}
  #bot .ht{display:flex;justify-content:space-between;font-size:11px;padding:3px 1px;border-bottom:1px solid #14171e}
  #bot .empty{color:#4b5263;font-size:11px;padding:6px 0}
  #bot .reset{margin-top:10px;border:1px solid #3a2a2a;background:#170f0f;color:#c98;
    border-radius:6px;padding:6px;cursor:pointer;font-family:inherit;font-size:11px}
  #bot .hint{font-size:10px;color:#5b6273;margin-top:8px;line-height:1.4}
</style></head><body>
<div id="top">
  <span class="ttl">ICT SM Trades</span>
  <span class="pair">BTCUSDT · <span id="tf-lbl">1m</span> · Binance</span>
  <select id="interval">
    <option value="1m" selected>1m</option><option value="5m">5m</option>
    <option value="15m">15m</option><option value="1h">1h</option><option value="4h">4h</option>
  </select>
  <span id="ohlc"></span>
  <span id="status">●</span>
  <a class="nav" href="/">← Dashboard</a>
</div>
<div id="legend">
  <span><span class="sw" style="background:#787b86"></span><b>Pivot</b> swing high/low (liquidity)</span>
  <span><span class="sw" style="background:#26a69a"></span><b>✕ / MSS</b> bullish grab &amp; shift</span>
  <span><span class="sw" style="background:#ef5350"></span><b>✕ / MSS</b> bearish grab &amp; shift</span>
  <span><span class="sw" style="background:rgba(38,166,154,.4)"></span><b>FVG</b> fair-value gap (entry)</span>
  <span><span class="sw" style="background:rgba(120,140,200,.25)"></span><b>Killzone</b> session</span>
  <span>◆ swept liquidity</span>
</div>
<div id="wrap">
  <div id="bot">
    <div class="bhd"><span class="dot" id="b-dot"></span><span class="bnm">🤖 FVG Auto-Bot</span></div>
    <div class="tgs">
      <button class="tg" id="botTg" onclick="botToggle()">off</button>
      <button class="tg" id="sndTg" onclick="soundToggle()">🔊</button>
    </div>
    <div class="grid">
      <div class="cell"><div class="k">Net P&amp;L</div><div class="v" id="b-pnl">$0</div></div>
      <div class="cell"><div class="k">Open P&amp;L</div><div class="v" id="b-open">—</div></div>
      <div class="cell"><div class="k">Position</div><div class="v pos" id="b-pos" style="font-size:13px">flat</div></div>
      <div class="cell"><div class="k">Trades · Win</div><div class="v" id="b-stat" style="font-size:13px">0 · —</div></div>
    </div>
    <button class="closebtn" id="b-close" onclick="closeManual()" style="display:none">✕ Close position now</button>
    <div class="lbl" style="margin-top:12px">Recent trades</div>
    <div class="hist" id="b-hist"></div>
    <button class="reset" onclick="botReset()">reset bot</button>
    <div class="hint">Fires on each NEW square: <b style="color:#26a69a">green→LONG</b>, <b style="color:#ef5350">red→SHORT</b>, flips on the opposite. Click <b>on</b> + <b>🔊</b> once to enable.</div>
  </div>
  <div id="chart"></div></div>
<script>
const $=id=>document.getElementById(id);
const chart=LightweightCharts.createChart($('chart'),{
  autoSize:true,
  layout:{background:{color:'#0a0b0e'},textColor:'#aab0bd',fontFamily:"'IBM Plex Mono',monospace"},
  grid:{vertLines:{color:'#14171e'},horzLines:{color:'#14171e'}},
  timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#1d212b'},
  rightPriceScale:{borderColor:'#1d212b'},
  crosshair:{mode:0},
});
const series=chart.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,
  wickUpColor:'#26a69a',wickDownColor:'#ef5350'});

// ---- custom overlay: FVG boxes, killzone bands, diamonds (canvas primitive) ----
class IctOverlay{
  constructor(){this._d={fvgs:[],killzones:[],diamonds:[]};this._chart=null;this._series=null;this._req=null;}
  attached(p){this._chart=p.chart;this._series=p.series;this._req=p.requestUpdate;}
  detached(){this._chart=null;this._series=null;}
  setData(d){this._d=d;if(this._req)this._req();}
  updateAllViews(){}
  paneViews(){const self=this;return [{zOrder:()=>'bottom',renderer:()=>({draw:t=>self._draw(t)})}];}
  _draw(target){
    const c=this._chart,s=this._series; if(!c||!s)return;
    const ts=c.timeScale(); const tx=t=>ts.timeToCoordinate(t); const py=p=>s.priceToCoordinate(p);
    target.useBitmapCoordinateSpace(scope=>{
      const ctx=scope.context,hr=scope.horizontalPixelRatio,vr=scope.verticalPixelRatio,H=scope.bitmapSize.height;
      for(const k of this._d.killzones){let x1=tx(k.start),x2=tx(k.end);if(x1==null||x2==null)continue;
        ctx.fillStyle='rgba(120,140,200,0.10)';ctx.fillRect(x1*hr,0,(x2-x1)*hr,H);}
      for(const f of this._d.fvgs){let x1=tx(f.start),x2=tx(f.end),yt=py(f.top),yb=py(f.bottom);
        if(x1==null||x2==null||yt==null||yb==null)continue;
        const bull=f.dir==='bull';
        ctx.fillStyle=bull?'rgba(38,166,154,0.16)':'rgba(239,83,80,0.16)';
        ctx.fillRect(x1*hr,yt*vr,(x2-x1)*hr,(yb-yt)*vr);
        ctx.strokeStyle=bull?'rgba(38,166,154,0.55)':'rgba(239,83,80,0.55)';ctx.lineWidth=1;
        ctx.strokeRect(x1*hr,yt*vr,(x2-x1)*hr,(yb-yt)*vr);}
      for(const d of this._d.diamonds){let x=tx(d.time),y=py(d.price);if(x==null||y==null)continue;
        const sz=4.5*hr;x*=hr;y*=vr;ctx.beginPath();
        ctx.moveTo(x,y-sz);ctx.lineTo(x+sz,y);ctx.lineTo(x,y+sz);ctx.lineTo(x-sz,y);ctx.closePath();
        ctx.fillStyle=d.dir==='bull'?'#26a69a':'#ef5350';ctx.fill();}
    });
  }
}
const overlay=new IctOverlay(); series.attachPrimitive(overlay);

function buildMarkers(m){
  const out=[];
  for(const p of m.pivots) out.push({time:p.time,position:p.kind==='high'?'aboveBar':'belowBar',
      color:'#787b86',shape:'square',text:'Pivot'});
  for(const g of m.grabs) out.push({time:g.time,position:g.dir==='bear'?'aboveBar':'belowBar',
      color:g.dir==='bear'?'#ef5350':'#26a69a',shape:g.dir==='bear'?'arrowDown':'arrowUp',text:'✕'});
  for(const s of m.mss) out.push({time:s.time,position:s.dir==='bear'?'aboveBar':'belowBar',
      color:s.dir==='bear'?'#ef5350':'#26a69a',shape:'circle',text:'MSS'});
  out.sort((a,b)=>a.time-b.time);
  // de-dupe identical (time,text) so overlapping labels don't stack
  const seen=new Set(); return out.filter(x=>{const k=x.time+x.text+x.position;if(seen.has(k))return false;seen.add(k);return true;});
}

// ===== FVG auto-bot: trade each NEW square instantly (green=long, red=short) + alert sound =====
const BOT_KEY='ict_fvg_bot_v1';
let bot={enabled:false,sound:false,netPnl:0,wins:0,trades:0,hist:[],pos:null};
try{bot=Object.assign(bot,JSON.parse(localStorage.getItem(BOT_KEY)||'{}'));}catch(e){}
let seenFVG=null;            // Set of FVG keys already drawn (null until first load -> seeds, no trades on history)
let lastPrice=null;          // latest close, for the manual "Close position" button
let audioCtx=null;
function fvgKey(f){return f.dir+'|'+f.start+'|'+Math.round(f.top*100)+'|'+Math.round(f.bottom*100);}
function saveBot(){try{localStorage.setItem(BOT_KEY,JSON.stringify(bot));}catch(e){}}
function ensureAudio(){try{if(!audioCtx)audioCtx=new (window.AudioContext||window.webkitAudioContext)();
  if(audioCtx.state==='suspended')audioCtx.resume();}catch(e){}}
function beep(bull){
  if(!bot.sound)return; ensureAudio(); if(!audioCtx)return;
  try{const o=audioCtx.createOscillator(),g=audioCtx.createGain();
    o.type='sine';o.frequency.value=bull?880:330;          // green=high tone, red=low tone
    g.gain.setValueAtTime(0.0001,audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.3,audioCtx.currentTime+0.01);
    g.gain.exponentialRampToValueAtTime(0.0001,audioCtx.currentTime+0.32);
    o.connect(g);g.connect(audioCtx.destination);o.start();o.stop(audioCtx.currentTime+0.33);
  }catch(e){}
}
function closePos(price){
  if(!bot.pos)return; const pnl=(price-bot.pos.entry)*bot.pos.side;
  bot.netPnl+=pnl; bot.trades++; if(pnl>0)bot.wins++;
  bot.hist.unshift({side:bot.pos.side>0?'long':'short',pnl:pnl}); bot.hist=bot.hist.slice(0,20);
  bot.pos=null;
}
function onNewSquare(f,price){
  beep(f.dir==='bull');                                     // SOUND on every new square
  if(!bot.enabled||price==null)return;
  const want=f.dir==='bull'?1:-1;                           // green/bull -> long, red/bear -> short
  if(bot.pos&&bot.pos.side!==want)closePos(price);          // flip on opposite square
  if(!bot.pos)bot.pos={side:want,entry:price};
  saveBot();
}
function botToggle(){bot.enabled=!bot.enabled;ensureAudio();saveBot();renderBot();}
function soundToggle(){bot.sound=!bot.sound;ensureAudio();if(bot.sound)beep(true);saveBot();renderBot();}
function botReset(){if(!confirm('Reset the FVG bot P&L and trades?'))return;
  bot.netPnl=0;bot.wins=0;bot.trades=0;bot.hist=[];bot.pos=null;saveBot();renderBot();}
function closeManual(){if(!bot.pos)return;                 // user flattens the position themselves
  closePos(lastPrice!=null?lastPrice:bot.pos.entry);saveBot();renderBot(lastPrice);}
function money(x){return (x>=0?'+$':'-$')+Math.abs(x).toFixed(2);}
function renderBot(price){
  $('botTg').textContent=bot.enabled?'on':'off'; $('botTg').className='tg'+(bot.enabled?' on':'');
  $('b-dot').className='dot'+(bot.enabled?' on':'');
  $('b-close').style.display=bot.pos?'block':'none';
  $('sndTg').innerHTML=bot.sound?'🔊':'🔊<span style="color:#ef5350;font-weight:700">✕</span>';
  $('sndTg').className='tg'+(bot.sound?' on':' muted');
  $('b-pnl').textContent=money(bot.netPnl); $('b-pnl').style.color=bot.netPnl>=0?'#26a69a':'#ef5350';
  if(bot.pos){$('b-pos').textContent=(bot.pos.side>0?'LONG':'SHORT')+' '+bot.pos.entry.toFixed(0);
    $('b-pos').className='v pos '+(bot.pos.side>0?'long':'short');
    const op=price!=null?(price-bot.pos.entry)*bot.pos.side:null;
    $('b-open').textContent=op!=null?money(op):'—'; $('b-open').style.color=op==null?'':(op>=0?'#26a69a':'#ef5350');
  }else{$('b-pos').textContent='flat';$('b-pos').className='v pos';$('b-open').textContent='—';$('b-open').style.color='';}
  $('b-stat').textContent=bot.trades+' · '+(bot.trades?Math.round(bot.wins/bot.trades*100)+'%':'—');
  $('b-hist').innerHTML=bot.hist.length?bot.hist.map(h=>'<div class="ht"><span style="color:'+(h.side==='long'?'#26a69a':'#ef5350')+'">'+
    h.side.toUpperCase()+'</span><span style="color:'+(h.pnl>=0?'#26a69a':'#ef5350')+'">'+money(h.pnl)+'</span></div>').join('')
    :'<div class="empty">No trades yet.</div>';
}
renderBot();

let lastData=null;
async function load(){
  try{
    const itv=$('interval').value; $('tf-lbl').textContent=itv;
    const j=await(await fetch('/api/ictsm?interval='+itv)).json();
    if(!j.candles||!j.candles.length){$('status').textContent='no data';$('status').style.color='#ef5350';return;}
    series.setData(j.candles);
    series.setMarkers(buildMarkers(j));
    overlay.setData({fvgs:j.fvgs||[],killzones:j.killzones||[],diamonds:j.diamonds||[]});
    // --- FVG auto-bot: react to NEW squares ---
    const fvgs=j.fvgs||[];
    const price=(j.candles&&j.candles.length)?j.candles[j.candles.length-1].close:null;
    lastPrice=price;
    if(seenFVG===null){ seenFVG=new Set(fvgs.map(fvgKey)); }   // first load: seed existing, don't trade history
    else{ for(const f of fvgs){ const k=fvgKey(f); if(!seenFVG.has(k)){ seenFVG.add(k); onNewSquare(f,price); } } }
    renderBot(price);
    if(!lastData) chart.timeScale().fitContent();
    lastData=j;
    $('status').textContent='● live · '+(j.grabs?j.grabs.length:0)+' grabs · '+(j.mss?j.mss.length:0)+' MSS · '+(j.fvgs?j.fvgs.length:0)+' FVG';
    $('status').style.color='#26a69a';
  }catch(e){$('status').textContent='chart error';$('status').style.color='#ef5350';}
}
chart.subscribeCrosshairMove(p=>{
  if(!p||!p.seriesData){return;} const d=p.seriesData.get(series); if(!d)return;
  $('ohlc').textContent='O '+d.open+'  H '+d.high+'  L '+d.low+'  C '+d.close;
});
$('interval').onchange=()=>{lastData=null;seenFVG=null;if(bot.pos){bot.pos=null;saveBot();renderBot();}load();};
load(); setInterval(load, 5000);
</script></body></html>"""
