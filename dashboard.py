"""
================================================================================
 dashboard.py  —  LOCAL WEB CHART  (TradingView-style, with news dots)
================================================================================
A tiny web server (built into the bot) that serves a live BTC candlestick chart
in your browser at  http://127.0.0.1:8000 . It uses TradingView's own free
charting library (Lightweight Charts, loaded from a CDN).

What you get:
  - Live BTC/USDT candles (from Binance public klines — same source the bot uses)
  - A YELLOW DOT for EVERY news message from your Telegram channels, placed at the
    exact minute it arrived (traded OR skipped — every single one)
  - Click a dot -> a small popup shows that news (and what the AI decided)
  - A full news feed list under the chart (newest first), also showing every message

It runs as part of the bot: start `python main.py`, then open the URL above.
No extra packages — it uses aiohttp, which the bot already installs.

The news comes from data/trades.db (the bot logs every analyzed message there).
The chart auto-refreshes every few seconds.
================================================================================
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import sqlite3
import time

import aiohttp
from aiohttp import web

import config
import orderbook
import manual_trader
import journal
import lighter_live
import copy_trader
import funding_bot
import lighter_stats
import lighter_markets
import news_reactor_bot
import news_sniper_bot
import cross_arb_paper
import cryptal_maker_paper
import cryptal_georgian_scanner
import georgian_venue_scanner
import airdrop_scanner
import confirmed_airdrops
import fv_track_paper
import trend_breakout_paper
import ainews_paper
import tv_strategies_paper
import meanrev_paper
import news_momo_paper
import news_paper_bot
import pennystock_paper
from penny_page import PENNY_HTML
import claude_haiku_paper
import rsi2_scalper_paper
import all_pattern_paper
import apex_vwap_paper
import ict_lab
import lucid_lab.web as lucid_lab_web

BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"
CHART_SYMBOL = "BTCUSDT"
HOST = "127.0.0.1"
# A dirty reboot/power-cut can make Windows (Hyper-V winnat) reserve port 8000 so
# it cannot be bound ("WinError 10013"). Try 8000 first, then fall back to known-free
# ports so the dashboard ALWAYS comes up. The chosen port is written to
# data/dashboard_port.txt so the launcher can open the right URL.
PORT_CANDIDATES = [8000, 5000, 5050, 3000, 8080, 8888]
PORT = 8000   # updated at startup by _pick_free_port()

def _pick_free_port() -> int:
    """Return the first bindable port from PORT_CANDIDATES and record it so the
    launcher can open the right browser URL. Never raises - falls back to 8000."""
    import socket as _socket
    chosen = PORT_CANDIDATES[0]
    for cand in PORT_CANDIDATES:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.bind((HOST, cand))
            chosen = cand
            break
        except OSError:
            continue
        finally:
            s.close()
    try:
        import config as _cfg
        with open(os.path.join(_cfg.DATA_DIR, "dashboard_port.txt"), "w", encoding="utf-8") as f:
            f.write(str(chosen))
    except Exception:
        pass
    return chosen


WHALES = orderbook.WhaleTracker()    # background tape of trades
MANUAL = manual_trader.ManualTrader() # your hand-clicked paper account
COPY = copy_trader.CopyTrader()      # paper copy-trading of 4 Hyperliquid wallets
FUNDING = funding_bot.FundingBot()   # paper funding-settlement timing bot (Lighter)
NEWSAI = news_reactor_bot.NewsReactorBot()   # News Reactor: AI reads the feed, paper-trades the call
SNIPER = news_sniper_bot.NewsSniperBot()     # News Sniper: NO-AI rules engine, reacts instantly
CROSSARB = cross_arb_paper.CrossArbBot()     # Cross-exchange arbitrage: Binance vs Hyperliquid, hedged
CRYPTAL_DATA = cryptal_maker_paper.CryptalPublicDataHub(
    min_interval_sec=1.0, cache_ttl_sec=4.0
)
CRYPTALMAKER = cryptal_maker_paper.CryptalMakerPaperBot(
    data_hub=CRYPTAL_DATA
)  # passive Cryptal spot + Binance hedge (paper)
CRYPTALGELMAKER = cryptal_maker_paper.CryptalGelMakerPaperBot(
    data_hub=CRYPTAL_DATA
)  # BTC-TOGEL + the same Binance hedge (paper)
GEORGIANVENUES = georgian_venue_scanner.GeorgianVenueOpportunityScanner(
    CRYPTAL_DATA
)
CRYPTALGEOSCANNER = cryptal_georgian_scanner.CryptalGeorgianMarketScanner(
    CRYPTAL_DATA, venue_scanner=GEORGIANVENUES
)
CRYPTALGEOBOT = cryptal_georgian_scanner.CryptalBestGeorgianMarketPaperBot(
    CRYPTALGEOSCANNER, CRYPTAL_DATA
)
AIRDROPS = airdrop_scanner.AirdropRadar(100.0)  # Airdrop Radar: tokenless protocols ranked for a small bankroll
FVTRACK = fv_track_paper.FVTrackBot()        # Fair-value tracking: Lighter follower vs leader consensus (zero-fee edge)
TREND = trend_breakout_paper.TrendBreakoutBot()  # Crypto Trend Breakout + ATR risk (daily, BTC/ETH/SOL, Goodman)
AINEWS = ainews_paper.AINewsBot()            # AI News Trading Bot (Google News RSS -> LLM sentiment -> BTC position)
CLAUDEHAIKU = claude_haiku_paper.ClaudeHaikuPaperBot()  # Claude Haiku live-feed AI news trader (paper BTC)
TVSTRATS = tv_strategies_paper.TVStrategiesBot()  # 12 TradingView-style strategies, each $100 / 20x paper on BTC
MEANREV = meanrev_paper.MeanReversionBot()   # Bollinger + RSI + 200-SMA mean reversion ($100 / 20x paper, BTC 1h)
NEWSMOMO = news_momo_paper.NewsMomentumBot() # news-momentum: 1.5s pre-news >=0.08% move -> bet direction ($100/20x, 5% trail)
PENNY = pennystock_paper.PennyStockPaperBot()   # AI penny-stock scanner -> paper trades ($10k, amber panel)
NEWSPAPER = news_paper_bot.NewsPaperBot()    # News Paper Bot: rule-based news -> paper trades, $10k, BTC/ETH/SOL (blue panel)
RSI2NOATR = rsi2_scalper_paper.RSI2ScalperPaperBot(
    "rsi2_noatr", "RSI2 EMA50 Scalper (paper - no ATR)", use_atr_filter=False
)
RSI2ATR = rsi2_scalper_paper.RSI2ScalperPaperBot(
    "rsi2_atr", "RSI2 EMA50 Scalper (paper - ATR filter)", use_atr_filter=True
)
PATTERN_BOTS = {
    key: all_pattern_paper.AllPatternPaperBot(cfg)
    for key, cfg in all_pattern_paper.BOT_CONFIGS.items()
}
ICTLAB = ict_lab.ICTLab()                 # live visual ICT chart scanner, no trading

# ---- per-bot "uptime since last reset" timer (shown on every panel header) -----
_UPTIME_PATH = os.path.join(config.DATA_DIR, "bot_uptimes.json")
BOT_RESET_TS: dict = {}
KNOWN_BOT_KEYS = ["newsbot", "sniperbot", "arb", "cryptalmaker", "cryptalgelmaker", "cryptalgeo", "fv", "trend", "ainews", "claudehaiku", "tvstrats", "meanrev",
                  "newsmomo", "newspaper", "rsi2noatr", "rsi2atr", "ictbot", "ictsm", "ictfreqbot", "freqbot", "freqtpbot",
                  "freqtrendbot", "freq5bot", "freqtfbot", "tsbot", "cotbot", "nwbot", "onchainbot", "ictlab",
                  "patternbots", "apexvwap", "lucidcont", "lucidpass", "nqmr15", "nr7", "nr7aggr", "penny"]


def _save_uptimes():
    try:
        with open(_UPTIME_PATH, "w") as f:
            json.dump(BOT_RESET_TS, f)
    except Exception:
        pass


def _load_uptimes():
    global BOT_RESET_TS
    try:
        if os.path.exists(_UPTIME_PATH):
            with open(_UPTIME_PATH) as f:
                BOT_RESET_TS = json.load(f)
    except Exception:
        BOT_RESET_TS = {}
    now = time.time()
    changed = False
    for k in KNOWN_BOT_KEYS:                # first-ever sight = start the clock now (persisted)
        if k not in BOT_RESET_TS:
            BOT_RESET_TS[k] = now
            changed = True
    if changed:
        _save_uptimes()


_load_uptimes()


@web.middleware
async def _uptime_mw(request, handler):
    """Record a reset time whenever ANY /api/<key>/reset succeeds — so every bot's
    'up since reset' timer is captured centrally, without editing each bot."""
    resp = await handler(request)
    try:
        p = request.path
        if (request.method == "POST" and p.startswith("/api/") and p.endswith("/reset")
                and getattr(resp, "status", 200) < 400):
            key = p[len("/api/"):-len("/reset")]
            if key:
                BOT_RESET_TS[key] = time.time()
                _save_uptimes()
    except Exception:
        pass
    return resp


# Injected into EVERY html page (see _scroll_keep_mw). The dashboards re-render their
# panels via `el.innerHTML = ...` every 1-2s; that destroys and recreates the inner scroll
# boxes (the Activity .feed / Closed-trades .hist / table containers), so the scrollbar
# snaps back to the top and you can't read older rows while scrolling.
#
# Fix: continuously REMEMBER where the user has scrolled each inner container, then after any
# DOM change re-apply it on the next animation frame (rAF, after layout, so it actually sticks).
# A container is identified by the CHILD-INDEX PATH from its nearest id'd ancestor, and on
# restore we resolve that path DIRECTLY (getElementById + walk children) — O(path depth),
# completely independent of how many rows the panel holds. That is the crucial difference from
# the previous version, which scanned every node in the panel and bailed out on big panels, so
# long trade lists (e.g. On-chain Radar with 100s of trades) silently lost the fix. Only
# restores a box that was just reset to the top (never fights the live trade tape, which
# manages its own scroll), and forgets a box once it is scrolled back to the top.
_SCROLL_KEEP_JS = """<script>
(function(){
  try{
    if(window.__scrollKeep) return;
    window.__scrollKeep = true;
    var mem = Object.create(null);

    // Identify a scroll container by the child-index path from its nearest id'd ancestor.
    // Rows changing INSIDE the box never change the box's own path, and resolving the path is
    // O(depth) no matter how many rows the panel holds.
    function keyOf(el){
      var path = [], node = el, guard = 0;
      while(node && node.nodeType === 1 && guard++ < 80){
        if(node.id) return { anchor: node.id, path: path.reverse() };
        var parent = node.parentElement;
        if(!parent) return { anchor: null, path: path.reverse() };
        var idx = 0, sib = node;
        while((sib = sib.previousElementSibling)) idx++;
        path.push(idx);
        node = parent;
      }
      return { anchor: null, path: path.reverse() };
    }
    function keyStr(k){ return (k.anchor || '~') + ':' + k.path.join(','); }
    function resolve(k){
      var node = k.anchor ? document.getElementById(k.anchor) : document.documentElement;
      if(!node) return null;
      for(var i = 0; i < k.path.length; i++){
        node = node.children[k.path[i]];
        if(!node) return null;
      }
      return node;
    }

    // Remember the latest scroll position of whatever the user scrolls; forget it once they
    // return it to the top (so we never drag them back down).
    document.addEventListener('scroll', function(e){
      var el = e.target;
      if(!el || el.nodeType !== 1) return;
      try{
        var k = keyOf(el), s = keyStr(k);
        if(el.scrollTop > 0 || el.scrollLeft > 0) mem[s] = { k: k, top: el.scrollTop, left: el.scrollLeft };
        else delete mem[s];
      }catch(_){}
    }, true);

    // After any DOM change, re-apply the few remembered positions on the next frame. Each is a
    // direct path resolve (cheap), and only acts on a box that was just reset to the top.
    var pending = false;
    function restore(){
      pending = false;
      for(var s in mem){
        var m = mem[s], el = resolve(m.k);
        if(!el) continue;
        if(m.top > 0 && el.scrollTop === 0 && (el.scrollHeight - el.clientHeight) > 1) el.scrollTop = m.top;
        if(m.left > 0 && el.scrollLeft === 0 && (el.scrollWidth - el.clientWidth) > 1) el.scrollLeft = m.left;
      }
    }
    var mo = new MutationObserver(function(){
      if(pending) return;
      pending = true;
      requestAnimationFrame(restore);
    });
    mo.observe(document.documentElement || document.body, {childList: true, subtree: true});
  }catch(e){}
})();
</script>"""


@web.middleware
async def _scroll_keep_mw(request, handler):
    """Inject the scroll-position keeper (above) into every HTML page, so panels that
    re-render every 1-2s no longer snap inner lists (e.g. 'Closed trades') back to the
    top. One place -> covers all current and future pages, no per-page edits."""
    resp = await handler(request)
    try:
        body = getattr(resp, "body", None)
        if (isinstance(resp, web.Response) and body
                and (getattr(resp, "content_type", "") or "") == "text/html"
                and b"__scrollKeep" not in body):
            html = body.decode("utf-8", "ignore")
            if "</body>" in html:
                html = html.replace("</body>", _SCROLL_KEEP_JS + "</body>", 1)
            else:
                html = html + _SCROLL_KEEP_JS
            resp.text = html
    except Exception:
        pass
    return resp


async def _uptimes(request):
    now = time.time()
    return web.json_response({k: int(now - v) for k, v in BOT_RESET_TS.items()})
_MARKET = None                        # set in start_dashboard (bot's price feed)
_BROKER = None                        # set in start_dashboard (bot's paper account)
_NWBOT = None                         # set in start_dashboard (news+whale bot)
_ICTBOT = None                        # set in start_dashboard (ICT 2022-model paper bot)
_ICTSMBOT = None                      # set in start_dashboard (ICT SM Trades paper bot, BTC)
_ICTFREQBOT = None                    # set in start_dashboard (frequent ICT paper bot)
_FREQBOT = None                       # set in start_dashboard (Freqtrade-style paper bot)
_FREQTPBOT = None                     # set in start_dashboard (Freqtrade-style improved TP/SL paper bot)
_FREQTRENDBOT = None                  # set in start_dashboard (Freqtrade-style trend TP/SL paper bot)
_FREQ5BOT = None                      # set in start_dashboard (Freqtrade-style improved TP/SL 5% paper bot)
_FREQTFBOT = None                     # set in start_dashboard (Freqtrade-style trend+flow paper bot)
_TSBOT = None                         # set in start_dashboard (Trend-Sweep VWAP paper bot)
_APEXVWAPBOT = None                   # set in start_dashboard (Apex ES VWAP ORB paper bot)
_LUCIDCONTBOT = None                  # set in start_dashboard (Lucid continuous basket paper bot)
_LUCIDPASSBOT = None                  # set in start_dashboard (Lucid 50K monthly pass basket paper bot)
_NQMR15BOT = None                     # set in start_dashboard (NQ 15m MR flat600 paper bot)
_NR7BOT = None                        # set in start_dashboard (NR7 breakout ES+NQ+CL paper bot)
_NR7AGGRBOT = None                    # set in start_dashboard (NR7 + NQ reversion aggressive paper bot)
_OB_BOTS = {}                         # set in start_dashboard: {"1m":bot,"5m":bot,"15m":bot}
_COTBOT = None                        # set in start_dashboard (COT crowded-positioning fade paper bot)
_ONCHAINBOT = None                    # set in start_dashboard (on-chain radar paper bot)
_POLYBOT = None                       # set in start_dashboard (Polymarket copy-trade paper bot)
_STOCK_MARKET = None                  # Alpaca stock price feed
_STOCK_BOT = None                     # stock AI news bot
_LIGHTER = None                       # real-money Lighter access (set in start_dashboard)
_LIGHTERBOT = None                    # real-money News+Whale bot (set in start_dashboard)
_LIGHTER_LOCK = None                  # serialize Lighter calls (nonce safety)
_LMSTATS = lighter_stats.ManualTracker()  # YOUR manual real-money P&L / win-rate tracker
_LAST_LT_EQUITY = None                # last perps equity seen (baseline for manual P&L)
_LT_PNL_CACHE = {"ts": 0.0, "data": None}   # cache the fast P&L read so polls don't hammer Lighter
_LT_BACKOFF_UNTIL = 0.0               # if Lighter rate-limits us, pause its reads until this time

# ---- standalone "all Lighter markets" table page (search + sortable columns) ----
MARKETS_PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Lighter Markets</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0e11;--panel:#12161c;--line:#222a33;--txt:#e6edf3;--muted:#7d8893;--green:#13a06a;--red:#e0455a;--amber:#e8a23d;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--mono);font-size:13px}
.wrap{max-width:1100px;margin:0 auto;padding:18px}
.hdr{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:14px}
.hdr h1{font-size:18px;margin:0;font-weight:600}.hdr h1 span{color:var(--muted);font-weight:400;font-size:14px}
#q{flex:1;min-width:160px;background:var(--panel);border:1px solid var(--line);color:var(--txt);
   font-family:var(--mono);font-size:13px;border-radius:8px;padding:9px 12px}
a.back{color:var(--amber);text-decoration:none;font-size:12px;border:1px solid var(--line);padding:8px 12px;border-radius:8px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:right;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--panel)}
th:hover{color:var(--txt)}th.act{color:var(--amber)}
tr:hover td{background:#171c23}td.sym{font-weight:600;color:var(--txt)}
.pos{color:var(--green)}.neg{color:var(--red)}
#st{color:var(--muted);font-size:11px;margin-top:8px}
</style></head><body><div class="wrap">
<div class="hdr"><h1>Lighter Markets <span id="cnt"></span></h1>
<input id="q" placeholder="Search market by name…" oninput="render()" autocomplete="off">
<a class="back" href="/">&larr; dashboard</a></div>
<table><thead><tr>
<th data-k="symbol" onclick="sortBy('symbol')">Market</th>
<th data-k="price" onclick="sortBy('price')">Price</th>
<th data-k="change" onclick="sortBy('change')">24h %</th>
<th data-k="volume" onclick="sortBy('volume')">24h Volume</th>
<th data-k="oi" onclick="sortBy('oi')">Open Interest</th>
<th data-k="funding" onclick="sortBy('funding')">Funding</th>
<th data-k="max_leverage" onclick="sortBy('max_leverage')">Max Lev</th>
</tr></thead><tbody id="rows"></tbody></table>
<div id="st">loading every Lighter market…</div></div>
<script>
let DATA=[], sortKey='volume', sortDir=-1;
function fmtN(n,d){ return (n==null)?'\\u2014':Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d}); }
function fmtUsd(n){ if(n==null)return '\\u2014'; const a=Math.abs(n);
  if(a>=1e9)return '$'+(n/1e9).toFixed(2)+'B'; if(a>=1e6)return '$'+(n/1e6).toFixed(2)+'M';
  if(a>=1e3)return '$'+(n/1e3).toFixed(1)+'K'; return '$'+Number(n).toFixed(2); }
function fmtPct(n){ return (n==null)?'\\u2014':((n>=0?'+':'')+Number(n).toFixed(2)+'%'); }
function fmtFund(n){ return (n==null)?'\\u2014':((n>=0?'+':'')+(Number(n)*100).toFixed(4)+'%'); }
function priceDec(p){ return (p==null)?2:(p>=100?2:(p>=1?4:6)); }
async function load(){
  try{ const r=await (await fetch('/api/lighter/markets')).json();
    if(r.ok && r.markets){ DATA=r.markets; render();
      document.getElementById('st').textContent='live from Lighter \\u00b7 '+DATA.length+' markets \\u00b7 updates every 15s'; }
    else { document.getElementById('st').textContent='could not load markets: '+(r.error||'?'); }
  }catch(e){ document.getElementById('st').textContent='load error: '+e; }
}
function sortBy(k){ if(sortKey===k){ sortDir=-sortDir; } else { sortKey=k; sortDir=(k==='symbol')?1:-1; } render(); }
function render(){
  const q=(document.getElementById('q').value||'').toLowerCase();
  let rows=DATA.filter(m=>(m.symbol||'').toLowerCase().includes(q));
  rows.sort((a,b)=>{ let x=a[sortKey], y=b[sortKey];
    if(x==null)x=(typeof y==='string')?'':-Infinity; if(y==null)y=(typeof x==='string')?'':-Infinity;
    if(typeof x==='string'||typeof y==='string') return sortDir*String(x).localeCompare(String(y));
    return sortDir*(x-y); });
  document.getElementById('cnt').textContent='('+rows.length+')';
  document.querySelectorAll('th').forEach(th=>th.classList.toggle('act', th.dataset.k===sortKey));
  document.getElementById('rows').innerHTML = rows.map(m=>{
    const ch=(m.change==null)?'':(m.change>=0?'pos':'neg');
    const fc=(m.funding==null)?'':((m.funding>=0)?'pos':'neg');
    return '<tr><td class="sym">'+m.symbol+'</td>'+
      '<td>'+fmtN(m.price,priceDec(m.price))+'</td>'+
      '<td class="'+ch+'">'+fmtPct(m.change)+'</td>'+
      '<td>'+fmtUsd(m.volume)+'</td>'+
      '<td>'+fmtUsd(m.oi)+'</td>'+
      '<td class="'+fc+'">'+fmtFund(m.funding)+'</td>'+
      '<td>'+(m.max_leverage?m.max_leverage+'x':'\\u2014')+'</td></tr>';
  }).join('');
}
load(); setInterval(load, 15000);
</script></body></html>"""


def _btc_price():
    """Live BTC price from the bot's market feed (used to mark manual trades)."""
    try:
        return _MARKET.price("BTCUSDT") if _MARKET else None
    except Exception:
        return None


async def _fetch_binance_candles(interval="1m", limit=500):
    if interval not in ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1M"):
        interval = "1m"
    try:
        limit = max(50, min(int(limit), 1500))
    except Exception:
        limit = 500
    params = {"symbol": CHART_SYMBOL, "interval": interval, "limit": limit}
    async with aiohttp.ClientSession() as s:
        async with s.get(BINANCE_KLINES, params=params,
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                body = (await r.text())[:150]
                raise RuntimeError(f"Binance HTTP {r.status}: {body}")
            data = await r.json()
    return [{"time": int(k[0]) // 1000, "open": float(k[1]),
             "high": float(k[2]), "low": float(k[3]), "close": float(k[4])}
            for k in data]


# ---------------------------------------------------------------------------
# API: candles (proxied from Binance so the browser has no CORS / key issues)
# ---------------------------------------------------------------------------
async def _candles(request: web.Request):
    interval = request.query.get("interval", "1m")
    if interval not in ("1m", "5m", "15m", "1h", "2h", "4h", "1d", "1w", "1M"):
        interval = "1m"
    try:
        candles = await _fetch_binance_candles(interval, 500)
        return web.json_response({"candles": candles})
    except Exception as e:
        return web.json_response({"error": f"{type(e).__name__}: {e}", "candles": []})


# ---- ICT SM Trades chart page (/ict): candles + liquidity-grab/MSS/FVG/killzone drawings ----
async def _ictsm_data(request: web.Request):
    interval = request.query.get("interval", "1m")
    if interval not in ("1m", "5m", "15m", "1h", "4h"):
        interval = "1m"
    try:
        candles = await _fetch_binance_candles(interval, 1000)
        import ict_sm_markers
        markers = ict_sm_markers.compute(candles)
        return web.json_response({"candles": candles, **markers})
    except Exception as e:
        return web.json_response({"error": f"{type(e).__name__}: {e}", "candles": []})


async def _ictsm_page(request: web.Request):
    import ict_chart_page
    return web.Response(text=ict_chart_page.PAGE_HTML, content_type="text/html")


# ---------------------------------------------------------------------------
# API: market stats — realized volatility (from 1-min candles) + consolidated
# volume across the tracked exchanges (from the whale tape), plus how volume
# reacted to the latest news. Polled by the always-on strip under the toolbar.
# ---------------------------------------------------------------------------
async def _marketstats(request: web.Request):
    import time
    now = time.time()
    out = {"volatility_pct": None, "volatility_avg_pct": None, "volume_5m_usd": 0,
           "volume_avg_usd": 0, "volume_pct_of_avg": None,
           "news_change_pct": None, "vola_change_pct": None}

    # --- realized volatility from actual TRADE prices (the tape) — the SAME
    #     prices the candles show. RANGE-based: high-low over the window as % of
    #     mid. We were using the smoothed mark price before, which barely moves,
    #     so the number looked frozen while the candles swung. Newest-first,
    #     stops early (15s margin for out-of-order trades). No network.
    def _vol_hist(t_from, t_to):
        lo_ms, hi_ms, stop_ms = t_from * 1000, t_to * 1000, t_from * 1000 - 15000
        lo = hi = None
        for t in reversed(WHALES.tape):
            ts = t["ts"]
            if ts < stop_ms:
                break
            if lo_ms <= ts <= hi_ms:
                p = t["price"]
                if lo is None or p < lo:
                    lo = p
                if hi is None or p > hi:
                    hi = p
        if lo is None or lo <= 0:
            return None
        mid = (lo + hi) / 2.0
        return round((hi - lo) / mid * 100, 3) if mid > 0 else None
    out["volatility_pct"] = _vol_hist(now - 60, now)        # price swing in the last minute
    # baseline = average of the PRIOR few 1-minute swings (excludes the current
    # minute, so 'now vs avg' is a clean is-it-more-volatile-than-usual read)
    _mv = [v for v in (_vol_hist(now - 60 * (k + 1), now - 60 * k) for k in range(1, 9))
           if v is not None]
    out["volatility_avg_pct"] = round(sum(_mv) / len(_mv), 3) if _mv else None

    # --- volume across the 9 tracked exchanges (tape = trades >= WHALE_MIN_USD)
    #     AND the volume/volatility reaction to the latest headline. Computed in
    #     ONE pass over the tape (newest-first, stops early past the window) and
    #     the latest-news time is read from memory — so this NEVER scans the whole
    #     tape repeatedly or makes a blocking SQLite call on the server thread.
    news_ts = _LAST_NEWS_TS                       # tracked in memory by push_news()
    have_nw = bool(news_ts) and (now - news_ts) >= 30
    w = min(now - news_ts, 300) if have_nw else 0
    a_lo, a_hi = (news_ts, news_ts + w) if have_nw else (0.0, 0.0)
    b_lo, b_hi = (news_ts - w, news_ts) if have_nw else (0.0, 0.0)
    c5, c3600 = now - 300, now - 3600
    floor = min(c3600, b_lo) if have_nw else c3600
    stop_ms = (floor - 60) * 1000                 # 60s margin for out-of-order arrivals
    vol5 = total = nafter = nbefore = 0.0
    oldest = now
    have = False
    _disp = 10000   # volume stats stay whale-based even though the panel now shows every trade
    for t in reversed(WHALES.tape):               # newest first; break once safely past
        ts_ms = t["ts"]
        if ts_ms < stop_ms:
            break
        u = t["usd"]
        if u < _disp:                             # skip the tiny trades for the volume display
            continue
        ts = ts_ms / 1000.0
        if ts >= c3600:
            have = True
            total += u
            if ts < oldest:
                oldest = ts
            if ts >= c5:
                vol5 += u
        if have_nw:
            if a_lo <= ts < a_hi:
                nafter += u
            if b_lo <= ts < b_hi:
                nbefore += u
    if have:
        out["volume_5m_usd"] = int(vol5)
        blocks = max((now - oldest) / 300.0, 1.0)  # 5-min blocks of data we actually have
        avg5 = total / blocks
        if avg5 > 0:
            out["volume_avg_usd"] = int(avg5)
            out["volume_pct_of_avg"] = round(vol5 / avg5 * 100)
    if have_nw:
        if nbefore > 0:
            out["news_change_pct"] = round((nafter - nbefore) / nbefore * 100)
        elif nafter > 0:
            out["news_change_pct"] = 100
        va = _vol_hist(news_ts, news_ts + w)
        vb = _vol_hist(news_ts - w, news_ts)
        if va is not None and vb and vb > 0:
            out["vola_change_pct"] = round((va - vb) / vb * 100)
    return web.json_response(out)


# ---------------------------------------------------------------------------
# API: news (read every logged message from the bot's SQLite db)
# ---------------------------------------------------------------------------
def _read_news():
    out = []
    try:
        con = sqlite3.connect(config.DB_PATH)
        rows = con.execute(
            "SELECT ts, text, decision, direction, conviction, coins, traded, source "
            "FROM news ORDER BY ts DESC LIMIT 100").fetchall()
        con.close()
        for ts, text, decision, direction, conviction, coins, traded, source in rows:
            out.append({"ts": ts, "text": text or "", "decision": decision or "",
                        "direction": direction or "", "conviction": conviction or "",
                        "coins": coins or "", "traded": bool(traded),
                        "source": source or ""})
    except Exception:
        pass
    # MERGE the live Tree of Alpha feed (Latest News + Twitter) into the main feed
    # so a page reload shows every message, not just the bot's analysed headlines.
    try:
        for it in list(_toa_items):
            out.append(_toa_to_news(it))
    except Exception:
        pass
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out[:200]


async def _news(request: web.Request):
    return web.json_response({"news": _read_news()})


# ---- INSTANT news push (Server-Sent Events) -------------------------------
# When a headline arrives, main.on_news calls push_news(...) and it goes out to
# every open browser over a live connection in ~milliseconds — no polling wait.
_news_subscribers = set()           # one asyncio.Queue per connected browser
_LAST_NEWS_TS = None                 # epoch secs of the latest headline (set by push_news)


def push_news(item):
    global _LAST_NEWS_TS
    try:
        ts = item.get("ts") if isinstance(item, dict) else None
    except Exception:
        ts = None
    import time
    _LAST_NEWS_TS = float(ts) if ts else time.time()
    for q in list(_news_subscribers):
        try:
            q.put_nowait(item)
        except Exception:
            pass


async def _news_stream(request: web.Request):
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    q = asyncio.Queue()
    _news_subscribers.add(q)
    try:
        await resp.write(b": connected\n\n")
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(("data: " + json.dumps(item) + "\n\n").encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")     # keepalive so proxies don't drop it
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError, Exception):
        pass
    finally:
        _news_subscribers.discard(q)
    return resp


# ===========================================================================
# Tree of Alpha live news  (https://news.treeofalpha.com)
# ---------------------------------------------------------------------------
# Streams BOTH the "Latest News" (Blogs / breaking headlines) AND the Twitter
# feed to your browser. We POLL the public REST endpoint every few seconds and
# push any new headline to every open browser over Server-Sent Events (and to the
# AI news bot). NOTE: their public WebSocket (wss://…/ws) connects but does NOT
# stream the feed without a premium login — it stays silent — so polling the REST
# API is what actually delivers the live feed reliably.
# ===========================================================================
TOA_WS_URL   = "wss://news.treeofalpha.com/ws"   # public WS is silent w/o premium — unused
TOA_REST_URL = "https://news.treeofalpha.com/api/news"
TOA_POLL_SEC = 1                                 # how often to poll for new headlines (no premium key)
_TOA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Origin": "https://news.treeofalpha.com",
}

_toa_items       = collections.deque(maxlen=400)   # newest-LAST ring buffer
_toa_seen        = set()                            # _id de-dup
_toa_subscribers = set()                            # one asyncio.Queue per browser


def _toa_norm(raw):
    """Normalise a Tree of Alpha item into the compact shape the page renders."""
    if not isinstance(raw, dict):
        return None
    _id = str(raw.get("_id") or raw.get("id") or "")
    title = raw.get("title") or raw.get("body") or ""
    if not _id and not title:
        return None
    coins = []
    for sug in (raw.get("suggestions") or []):
        c = sug.get("coin") if isinstance(sug, dict) else None
        if c and c not in coins:
            coins.append(c)
    return {
        "id":     _id or title,
        "source": raw.get("source") or "",     # "Twitter" | "Blogs" | ...
        "title":  title,
        "url":    raw.get("url") or "",
        "icon":   raw.get("icon") or "",
        "image":  raw.get("image") or "",
        "time":   raw.get("time") or 0,         # ms epoch
        "coins":  coins,
    }


def _toa_add(raw):
    """Add a raw item to the ring buffer; return the normalised item if NEW."""
    item = _toa_norm(raw)
    if not item or item["id"] in _toa_seen:
        return None
    _toa_seen.add(item["id"])
    _toa_items.append(item)
    if len(_toa_seen) > 4000:                    # keep the seen-set bounded
        _toa_seen.intersection_update(i["id"] for i in _toa_items)
    return item


def push_toa(item):
    for q in list(_toa_subscribers):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                q.get_nowait(); q.put_nowait(item)   # drop oldest, keep newest
            except Exception:
                pass
        except Exception:
            pass


async def _toa(request: web.Request):
    """Backfill endpoint — newest-first, for the page's initial load."""
    return web.json_response({"news": list(reversed(_toa_items))})


async def _toa_stream(request: web.Request):
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    q = asyncio.Queue(maxsize=500)
    _toa_subscribers.add(q)
    try:
        await resp.write(b": connected\n\n")
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(("data: " + json.dumps(item) + "\n\n").encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")     # keepalive
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError, Exception):
        pass
    finally:
        _toa_subscribers.discard(q)
    return resp


def _toa_to_news(it):
    """Shape a Tree of Alpha item like a MAIN news-feed row so it shows up in the
    site's main news feed alongside everything else. decision='NEWS' marks it as a
    raw headline (not an AI verdict) so the UI badges it neutrally."""
    import time as _t
    ts = (it.get("time") or 0) / 1000.0 or _t.time()
    return {"ts": ts, "text": it.get("title", ""), "decision": "NEWS",
            "direction": "", "conviction": "", "coins": ",".join(it.get("coins") or []),
            "traded": False, "source": it.get("source") or "Tree"}


def _toa_emit(it):
    """Fan one fresh item out to: the main news feed (every browser, instantly),
    the dedicated /treeofalpha page, and the News Reactor bot."""
    push_news(_toa_to_news(it))          # → the MAIN news feed (what the user watches)
    push_toa(it)                          # → the /treeofalpha page
    try:                                  # → the News Reactor bot (AI)
        asyncio.create_task(NEWSAI.on_news(it["source"], it["title"]))
    except Exception:
        pass
    try:                                  # → the News Sniper bot (no-AI, instant)
        SNIPER.on_news(it["source"], it["title"])
    except Exception:
        pass


async def _toa_poll(session, push=False):
    """Pull the latest items via REST. With push=True, broadcast every newly-seen
    item. Returns # of new items.

    Tree of Alpha's public WebSocket (wss://…/ws) connects but never streams the
    feed without a premium login, so we drive the feed by polling this REST endpoint
    every second — confirmed to carry the live feed (Latest News + Twitter)."""
    try:
        async with session.get(TOA_REST_URL,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                print(f"[treeofalpha] poll HTTP {r.status}")
                return 0
            data = await r.json()
    except Exception as e:
        print("[treeofalpha] poll failed:", e)
        return 0
    # REST returns newest-first → add oldest-first so the deque ends newest-last
    fresh = 0
    for raw in reversed(data if isinstance(data, list) else []):
        it = _toa_add(raw)
        if not it:
            continue
        fresh += 1
        if push:
            _toa_emit(it)
    return fresh


def _toa_handle_raw(raw):
    """Add one raw item; if new, fan it out instantly."""
    it = _toa_add(raw)
    if not it:
        return False
    _toa_emit(it)
    return True


async def _toa_ws_stream(session, api_key):
    """TRUE instant push: connect the WebSocket, log in with the premium API key,
    and stream every headline the moment it lands. Raises on disconnect so the
    caller can reconnect. A slow REST safety-poll runs alongside (see _toa_listener)."""
    async with session.ws_connect(TOA_WS_URL, heartbeat=20,
                                  timeout=aiohttp.ClientTimeout(total=30)) as ws:
        await ws.send_str(f"login {api_key}")
        print("[treeofalpha] WebSocket logged in — INSTANT live news streaming")
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue                                  # e.g. a 'logged in' ack string
                items = data if isinstance(data, list) else [data]
                n = sum(1 for raw in items if _toa_handle_raw(raw))
                if n:
                    print(f"[treeofalpha] +{n} new headline(s) (ws)")
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise ConnectionError("ws closed")


async def _toa_listener():
    """Deliver the Tree of Alpha feed (Latest News + Twitter) with as little delay
    as possible:
      - with a premium API key (config.TOA_API_KEY) -> WebSocket = INSTANT push.
      - without a key -> the public WS is silent, so we poll the REST API every
        TOA_POLL_SEC second(s), which is the only keyless way to get the feed.
    Either way every new headline goes to all browsers and the News Reactor bot."""
    api_key = (getattr(config, "TOA_API_KEY", "") or "").strip()
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), family=0)
    async with aiohttp.ClientSession(headers=_TOA_HEADERS, connector=connector) as session:
        await _toa_poll(session, push=False)          # initial history (silent — don't trade old news)
        print(f"[treeofalpha] backfilled {len(_toa_items)} items")

        if api_key:
            # WebSocket = instant. A 10s REST poll runs alongside as a safety net so
            # a silent stall can never lose a headline for more than 10s.
            async def _safety_poll():
                while True:
                    await asyncio.sleep(10)
                    try:
                        await _toa_poll(session, push=True)
                    except Exception:
                        pass
            asyncio.create_task(_safety_poll())
            backoff = 2
            while True:
                try:
                    await _toa_ws_stream(session, api_key)
                except Exception as e:
                    print(f"[treeofalpha] WS dropped ({type(e).__name__}: {str(e)[:60]}) — "
                          f"reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        else:
            print(f"[treeofalpha] no premium key — polling every {TOA_POLL_SEC}s "
                  f"(set config.TOA_API_KEY for instant WebSocket push)")
            misses = 0
            while True:
                await asyncio.sleep(TOA_POLL_SEC)
                try:
                    n = await _toa_poll(session, push=True)
                    if n:
                        print(f"[treeofalpha] +{n} new headline(s)")
                    misses = 0
                except Exception as e:
                    misses += 1
                    print(f"[treeofalpha] poll error #{misses}: {type(e).__name__}: {str(e)[:80]}")
                    await asyncio.sleep(min(misses * 2, 20))


TOA_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tree of Alpha · Live News</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0b0e; --panel:#101217; --panel2:#15181f; --line:#1d212b;
    --txt:#d7dbe4; --muted:#737a89; --amber:#f5c518; --green:#26a69a;
    --red:#ef5350; --blue:#58c1ff;
    --mono:'IBM Plex Mono', ui-monospace, monospace;
    --sans:'IBM Plex Sans', system-ui, sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans)}
  #topbar{display:flex;align-items:center;gap:14px;padding:12px 18px;
    border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:10}
  .ttl{font-family:var(--mono);font-weight:600;font-size:17px;letter-spacing:.4px}
  .ttl .dot{color:var(--blue)}

  /* AI Penny Stock Desk - golden nav button (GPU-only animation, no JS) */
  .navgold{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;margin-left:4px;
    border-radius:9px;text-decoration:none;font-weight:700;font-size:12.5px;
    color:#1c1606;background:linear-gradient(100deg,#f5c518,#ffe083 45%,#f5c518);
    background-size:220% 100%;border:1px solid #b8912a;position:relative;overflow:hidden;
    box-shadow:0 2px 10px rgba(245,197,24,.22);
    animation:ngShift 7s ease-in-out infinite;will-change:background-position;
    transition:transform .16s ease,box-shadow .16s ease}
  .navgold:hover{transform:translateY(-1px);box-shadow:0 4px 16px rgba(245,197,24,.42)}
  .navgold .ng-i{font-size:12px;opacity:.85}
  .navgold .ng-a{opacity:.7;font-weight:900}
  @keyframes ngShift{0%,100%{background-position:0% 0}50%{background-position:100% 0}}
  @media (prefers-reduced-motion:reduce){.navgold{animation:none}}
  .nav{font-family:var(--mono);font-size:13px;color:var(--amber);text-decoration:none;
    border:1px solid var(--line);padding:5px 10px;border-radius:6px}
  .nav:hover{background:var(--panel2)}
  .spacer{flex:1}
  .tabs{display:flex;gap:6px}
  .tab{font-family:var(--mono);font-size:13px;color:var(--muted);background:var(--panel2);
    border:1px solid var(--line);padding:6px 12px;border-radius:6px;cursor:pointer}
  .tab:hover{color:var(--txt)}
  .tab.on{background:var(--blue);color:#04121f;border-color:var(--blue);font-weight:600}
  #status{font-family:var(--mono);font-size:12px;color:var(--muted)}
  #status .live{color:var(--green)}
  #status .down{color:var(--red)}
  #count{font-family:var(--mono);font-size:12px;color:var(--blue)}
  .soundbtn{background:transparent;border:1px solid var(--line);color:var(--txt);
    border-radius:6px;cursor:pointer;font-size:14px;padding:3px 9px}
  .soundbtn.off{opacity:.45}

  #feed{max-width:860px;margin:0 auto;padding:6px 0 60px}
  .row{display:grid;grid-template-columns:40px 1fr;gap:11px;padding:12px 18px;
    border-bottom:1px solid #14171e;animation:none}
  .row:hover{background:var(--panel)}
  .row.flash{animation:flash 1.6s ease-out}
  @keyframes flash{0%{background:#13314a}100%{background:transparent}}
  .av{width:40px;height:40px;border-radius:50%;background:var(--panel2);object-fit:cover;
    border:1px solid var(--line)}
  .body{min-width:0}
  .head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:3px}
  .src{font-family:var(--mono);font-size:11px;padding:2px 7px;border-radius:5px;font-weight:600}
  .src.tw{background:#10202e;color:var(--blue);border:1px solid #1c3447}
  .src.news{background:#2a2410;color:var(--amber);border:1px solid #443c12}
  .src.other{background:#1b1f29;color:#8b93a4;border:1px solid var(--line)}
  .when{font-family:var(--mono);font-size:11px;color:var(--muted)}
  .ttx{font-size:15px;line-height:1.45;color:var(--txt);white-space:pre-wrap;word-break:break-word}
  .ttx a{color:inherit;text-decoration:none}
  .ttx a:hover{text-decoration:underline}
  .coins{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}
  .coin{font-family:var(--mono);font-size:11px;color:var(--green);background:#0e1c18;
    border:1px solid #1c3a31;border-radius:5px;padding:1px 6px}
  .thumb{margin-top:8px;max-width:320px;max-height:200px;border-radius:8px;border:1px solid var(--line);display:block}
  .ext{font-family:var(--mono);font-size:11px;color:var(--blue);text-decoration:none;margin-left:auto}
  .ext:hover{text-decoration:underline}
  .empty{color:var(--muted);text-align:center;padding:40px;font-family:var(--mono);font-size:13px}
</style>
</head>
<body>
  <div id="topbar">
    <a class="nav" href="/">&larr; Chart</a>
    <span class="ttl">Tree of Alpha<span class="dot"> · </span>Live News</span>
    <div class="tabs">
      <div class="tab on" data-f="all"  onclick="setFilter('all',this)">All</div>
      <div class="tab"     data-f="news" onclick="setFilter('news',this)">Latest News</div>
      <div class="tab"     data-f="tw"   onclick="setFilter('tw',this)">Twitter</div>
    </div>
    <span class="spacer"></span>
    <button class="soundbtn off" id="soundbtn" onclick="toggleSound()" title="sound on new headline">🔔</button>
    <span id="count">0</span>
    <span id="status">connecting…</span>
  </div>
  <div id="feed"><div class="empty" id="empty">waiting for news…</div></div>

<script>
var FILTER = "all";
var SOUND = false;
var seen = {};
var actx = null;

function esc(s){ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

function relTime(ms){
  if(!ms) return "";
  var d = Math.max(0, Date.now() - ms);
  var s = Math.floor(d/1000);
  if(s < 60) return s + "s";
  var m = Math.floor(s/60); if(m < 60) return m + "m";
  var h = Math.floor(m/60); if(h < 24) return h + "h";
  return Math.floor(h/24) + "d";
}

function kind(item){ return (item.source||"").toLowerCase() === "twitter" ? "tw" : "news"; }

function matches(item){
  if(FILTER === "all") return true;
  return FILTER === kind(item);
}

function setFilter(f, el){
  FILTER = f;
  document.querySelectorAll(".tab").forEach(function(t){ t.classList.remove("on"); });
  el.classList.add("on");
  document.querySelectorAll(".row").forEach(function(r){
    r.style.display = (r.dataset.kind === f || f === "all") ? "" : "none";
  });
  refreshEmpty();
}

function refreshEmpty(){
  var feed = document.getElementById("feed");
  var anyVisible = Array.prototype.some.call(feed.querySelectorAll(".row"),
    function(r){ return r.style.display !== "none"; });
  var empty = document.getElementById("empty");
  if(anyVisible){ if(empty) empty.remove(); }
  else if(!empty){
    var e = document.createElement("div");
    e.className = "empty"; e.id = "empty"; e.textContent = "no items in this filter";
    feed.appendChild(e);
  }
}

function rowHTML(item){
  var k = kind(item);
  var srcCls = k === "tw" ? "tw" : "news";
  var srcLbl = item.source || "News";
  var icon = item.icon ? '<img class="av" src="'+esc(item.icon)+'" onerror="this.style.visibility=\'hidden\'">'
                       : '<div class="av"></div>';
  var coins = "";
  if(item.coins && item.coins.length){
    coins = '<div class="coins">' + item.coins.map(function(c){
      return '<span class="coin">$'+esc(c)+'</span>'; }).join("") + '</div>';
  }
  var thumb = item.image ? '<img class="thumb" src="'+esc(item.image)+'" onerror="this.remove()">' : "";
  var titleHtml = esc(item.title);
  if(item.url){
    titleHtml = '<a href="'+esc(item.url)+'" target="_blank" rel="noopener">'+titleHtml+'</a>';
  }
  var ext = item.url ? '<a class="ext" href="'+esc(item.url)+'" target="_blank" rel="noopener">open ↗</a>' : "";
  return icon +
    '<div class="body">' +
      '<div class="head">' +
        '<span class="src '+srcCls+'">'+esc(srcLbl)+'</span>' +
        '<span class="when" data-t="'+(item.time||0)+'">'+relTime(item.time)+'</span>' +
        ext +
      '</div>' +
      '<div class="ttx">'+titleHtml+'</div>' +
      coins + thumb +
    '</div>';
}

function addItem(item, isNew){
  if(!item || seen[item.id]) return;
  seen[item.id] = true;
  var feed = document.getElementById("feed");
  var emp = document.getElementById("empty"); if(emp) emp.remove();
  var row = document.createElement("div");
  row.className = "row" + (isNew ? " flash" : "");
  row.dataset.kind = kind(item);
  row.dataset.time = item.time || 0;
  row.innerHTML = rowHTML(item);
  if(!matches(item)) row.style.display = "none";
  feed.insertBefore(row, feed.firstChild);
  document.getElementById("count").textContent = Object.keys(seen).length;
  if(isNew && SOUND && matches(item)) beep();
}

function beep(){
  try{
    if(!actx) actx = new (window.AudioContext||window.webkitAudioContext)();
    var o = actx.createOscillator(), g = actx.createGain();
    o.connect(g); g.connect(actx.destination);
    o.frequency.value = 880; g.gain.value = 0.05;
    o.start(); o.stop(actx.currentTime + 0.12);
  }catch(e){}
}

function toggleSound(){
  SOUND = !SOUND;
  document.getElementById("soundbtn").classList.toggle("off", !SOUND);
  if(SOUND && !actx){ try{ actx = new (window.AudioContext||window.webkitAudioContext)(); }catch(e){} }
}

// initial backfill (newest-first)
fetch("/api/toa").then(function(r){ return r.json(); }).then(function(d){
  var arr = (d.news || []);
  for(var i = arr.length - 1; i >= 0; i--) addItem(arr[i], false);  // oldest first → newest ends on top
  refreshEmpty();
}).catch(function(){});

// instant live push
(function open(){
  var es = new EventSource("/api/toa/stream");
  es.onopen = function(){ document.getElementById("status").innerHTML = '<span class="live">● live</span>'; };
  es.onmessage = function(ev){
    if(!ev.data) return;
    try{ addItem(JSON.parse(ev.data), true); }catch(e){}
  };
  es.onerror = function(){
    document.getElementById("status").innerHTML = '<span class="down">● reconnecting…</span>';
    // EventSource auto-reconnects on its own
  };
})();

// keep relative timestamps fresh
setInterval(function(){
  document.querySelectorAll(".when").forEach(function(el){
    el.textContent = relTime(parseInt(el.dataset.t || "0", 10));
  });
}, 15000);
</script>
</body>
</html>
"""


async def _toa_page(request: web.Request):
    return web.Response(text=TOA_PAGE_HTML, content_type="text/html")


# ---- INSTANT trade push (Server-Sent Events) ------------------------------
# WHALES.on_add (wired in start_dashboard) calls push_trade(...) the MOMENT a trade
# lands on the tape, so each trade streams to the browser over a live connection with
# zero polling delay. Queues are bounded: if a browser falls behind we drop the oldest
# trade rather than grow memory.
_trade_subscribers = set()           # one bounded asyncio.Queue per connected browser


def push_trade(trade):
    for q in list(_trade_subscribers):
        try:
            q.put_nowait(trade)
        except asyncio.QueueFull:
            try:
                q.get_nowait()                 # client fell behind -> drop oldest, keep newest
                q.put_nowait(trade)
            except Exception:
                pass
        except Exception:
            pass


async def _trades_stream(request: web.Request):
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    q = asyncio.Queue(maxsize=4000)
    _trade_subscribers.add(q)
    try:
        await resp.write(b": connected\n\n")
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(("data: " + json.dumps(item) + "\n\n").encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")     # keepalive so proxies don't drop it
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError, Exception):
        pass
    finally:
        _trade_subscribers.discard(q)
    return resp


async def _orderbook(request: web.Request):
    return web.json_response(await orderbook.fetch_all_orderbooks())


async def _whales(request: web.Request):
    try:
        window = int(request.query.get("window", "60"))
    except ValueError:
        window = 60
    window = max(10, min(window, 14400))     # 10s .. 4h
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    inc = WHALES.since(since_seq=since, window_sec=window)   # only what's new
    buy, sell = WHALES.flow(window)
    span = 0
    if WHALES.tape:
        import time as _t
        span = int(_t.time() - WHALES.tape[0]["ts"] / 1000)   # oldest item (no full scan)
    return web.json_response({"new": inc["new"], "max_seq": inc["max_seq"],
                              "cutoff_ts": inc["cutoff_ts"], "count": inc["count"],
                              "min_usd": WHALES.min_usd, "sources": WHALES.sources,
                              "feed_status": orderbook.trade_feed_status(),
                              "window": window, "span": span,
                              "buy_usd": round(buy), "sell_usd": round(sell)})


async def _index(request: web.Request):
    # no-cache so a NORMAL refresh (F5) always pulls the latest page+JS after a restart —
    # you no longer need a hard refresh to see UI changes. (The HTML/JS is all inline here.)
    return web.Response(text=PAGE_HTML, content_type="text/html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


async def _book_page(request: web.Request):
    return web.Response(text=ORDERBOOK_HTML, content_type="text/html")


# ---- manual paper trading (you click these) -------------------------------
async def _manual_state(request: web.Request):
    try:
        fw = int(request.query.get("fw", "60"))
    except ValueError:
        fw = 60
    fw = max(10, min(fw, 14400))
    s = MANUAL.state(_btc_price())
    buy, sell = WHALES.flow(fw)
    tot = buy + sell
    s["flow_buy"] = round(buy / tot * 100, 1) if tot > 0 else None
    s["flow_window"] = fw
    return web.json_response(s)


async def _manual_start(request: web.Request):
    body = await request.json()
    return web.json_response(MANUAL.start(body.get("balance", 0)))


async def _manual_order(request: web.Request):
    b = await request.json()
    return web.json_response(MANUAL.open(
        b.get("side"), b.get("margin"), b.get("leverage", 1),
        _btc_price(), b.get("sl", 0), b.get("tp", 0)))


async def _manual_close(request: web.Request):
    b = await request.json()
    return web.json_response(MANUAL.close(b.get("id"), _btc_price(), "manual"))


async def _manual_modify(request: web.Request):
    b = await request.json()
    return web.json_response(MANUAL.set_sl_tp(b.get("id"), b.get("sl"), b.get("tp")))


async def _manual_reset(request: web.Request):
    return web.json_response(MANUAL.reset())


# ---- REAL-MONEY Lighter (runs sync ccxt in a thread, serialized) ----------
async def _lighter_run(method, *args):
    global _LIGHTER_LOCK
    if _LIGHTER is None:
        return {"ok": False, "error": "Lighter is not set up"}
    if _LIGHTER_LOCK is None:
        _LIGHTER_LOCK = asyncio.Lock()
    loop = asyncio.get_event_loop()
    fn = getattr(_LIGHTER, method)
    # READS (state) must NOT take the signed-order lock — otherwise a manual order waits behind
    # a background state refresh (that was the ~10s open delay). Only SIGNED calls serialize.
    if method == "state":
        try:
            return await loop.run_in_executor(None, lambda: fn(*args))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    async with _LIGHTER_LOCK:
        try:
            return await loop.run_in_executor(None, lambda: fn(*args))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


async def _lighter_page(request: web.Request):
    return web.Response(text=LIGHTER_PAGE_HTML, content_type="text/html")


async def _lighter_state(request: web.Request):
    data = await _lighter_run("state")
    try:
        fw = int(request.query.get("fw", "60"))
        fw = max(10, min(fw, 14400))
        buy, sell = WHALES.flow(fw)
        tot = buy + sell
        if isinstance(data, dict):
            data["flow_buy"] = round(buy / tot * 100, 1) if tot > 0 else None
            # cache perps equity (P&L baseline) and settle a manual round-trip the instant the
            # account reads FLAT — realized P&L = equity delta, exactly what Lighter shows
            global _LAST_LT_EQUITY
            equity = None
            try:
                equity = float((data.get("balance") or {}).get("total"))
            except Exception:
                equity = None
            if equity is not None:
                _LAST_LT_EQUITY = equity
            if data.get("ok") and isinstance(data.get("positions"), list):
                _LMSTATS.on_state(bool(data["positions"]), equity, data.get("price"))
            data["manual_stats"] = _LMSTATS.stats()
    except Exception:
        pass
    return web.json_response(data)


async def _lighter_order(request: web.Request):
    b = await request.json()
    otype = b.get("type", "market")
    reduce_only = bool(b.get("reduceOnly", False))
    market = (b.get("market") or "BTC")
    res = await _lighter_run("order", b.get("side"), b.get("usd"), b.get("margin"),
                             b.get("leverage"), otype, b.get("price"), reduce_only, False, market)
    # track a manual OPEN for the real-money P&L / win-rate panel (a market order fills now;
    # a reduce-only order is a close; a resting limit isn't filled yet). Baseline = equity now.
    try:
        if isinstance(res, dict) and res.get("ok") and not reduce_only and otype != "limit":
            _LMSTATS.on_open(b.get("market") or "BTC", b.get("side"),
                             res.get("average"), res.get("amount"), _LAST_LT_EQUITY)
    except Exception:
        pass
    return web.json_response(res)


async def _lighter_close(request: web.Request):
    try:
        b = await request.json()
    except Exception:
        b = {}
    # Pass the position size/side the panel already shows so close skips the slow CCXT read.
    # the round-trip P&L is booked from the EQUITY change the moment the account reads flat
    # (see _lighter_state -> _LMSTATS.on_state), so it matches Lighter exactly. Nothing to do here.
    res = await _lighter_run("close", None, b.get("size"), b.get("side"), (b.get("market") or "BTC"))
    return web.json_response(res)


async def _lighter_setlev(request: web.Request):
    """Pre-set the account leverage when the slider changes, so the ORDER itself doesn't
    have to send the slow leverage transaction inline (that was the ~10s lag)."""
    b = await request.json()
    return web.json_response(await _lighter_run("set_leverage", b.get("leverage"),
                                                (b.get("market") or "BTC")))


async def _lighter_candles_api(request: web.Request):
    """Lighter's OWN candles for the selected market, so the chart tracks Lighter directly."""
    market = request.query.get("market", "BTC")
    tf = request.query.get("tf", "1m")
    try:
        limit = max(20, min(int(request.query.get("limit", "300")), 1000))
    except Exception:
        limit = 300
    loop = asyncio.get_event_loop()
    try:
        rows = await loop.run_in_executor(None, lambda: lighter_markets.candles(market, tf, limit))
        return web.json_response({"ok": True, "candles": rows})
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}",
                                  "candles": []})


async def _lighter_pnl(request: web.Request):
    """FAST, lightweight: the open position's EXACT unrealized P&L + mark price straight from
    Lighter (one positions read). The panel polls this ~1x/s so the live P&L matches Lighter's
    own number instead of lagging the full ~3s account poll."""
    global _LT_BACKOFF_UNTIL
    import time as _t
    now = _t.time()
    # serve the cache when it's fresh OR while we're backing off from a rate-limit — this keeps
    # the actual hit-rate on Lighter low no matter how often the browser polls.
    if _LT_PNL_CACHE["data"] is not None and ((now - _LT_PNL_CACHE["ts"]) < 2.5 or now < _LT_BACKOFF_UNTIL):
        return web.json_response(_LT_PNL_CACHE["data"])
    if _LIGHTER is None:
        return web.json_response({"ok": False})
    loop = asyncio.get_event_loop()

    def go():
        try:
            ex = _LIGHTER._ensure()

            def f(*v):
                for x in v:
                    if x in (None, ""):
                        continue
                    try:
                        return float(x)
                    except Exception:
                        pass
                return None
            for p in ex.fetch_positions():
                info = p.get("info") or {}
                size = f(p.get("contracts"), info.get("position"))
                if size:
                    return {"ok": True, "has": True,
                            "pnl": f(info.get("unrealized_pnl"), p.get("unrealizedPnl")),
                            "price": f(info.get("mark_price"), info.get("last_trade_price"),
                                       p.get("markPrice")),
                            "entry": f(info.get("avg_entry_price"), p.get("entryPrice")),
                            "size": abs(size), "side": p.get("side")}
            return {"ok": True, "has": False}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}
    data = await loop.run_in_executor(None, go)
    if isinstance(data, dict) and not data.get("ok"):
        err = str(data.get("error", "")).lower()
        if any(k in err for k in ("rate", "429", "418", "ddos", "too many", "limit")):
            _LT_BACKOFF_UNTIL = now + 60.0          # Lighter limited us → pause its reads 60s
            return web.json_response(_LT_PNL_CACHE["data"] or {"ok": True, "has": False})
    if isinstance(data, dict) and data.get("ok"):
        _LT_PNL_CACHE["ts"] = now; _LT_PNL_CACHE["data"] = data
    return web.json_response(data)


async def _lighter_markets_api(request: web.Request):
    """Every Lighter market with live price / funding / 24h volume / OI (cached)."""
    loop = asyncio.get_event_loop()
    try:
        rows = await loop.run_in_executor(None, lighter_markets.markets)
        return web.json_response({"ok": True, "markets": rows, "n": len(rows)})
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}",
                                  "markets": []})


async def _markets_page(request: web.Request):
    return web.Response(text=MARKETS_PAGE_HTML, content_type="text/html")


# ---- the REAL-MONEY News+Whale bot (autonomous) ----
async def _lighterbot_state(request: web.Request):
    if _LIGHTERBOT is None:
        return web.json_response({"ok": False, "error": "bot not running"})
    try:
        fw = int(request.query.get("fw", "60"))
        fw = max(10, min(fw, 14400))
    except Exception:
        fw = 60
    try:
        return web.json_response(_LIGHTERBOT.state(flow_window_sec=fw))
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})


async def _lighterbot_toggle(request: web.Request):
    if _LIGHTERBOT is None:
        return web.json_response({"ok": False, "error": "bot not running"})
    b = await request.json()
    return web.json_response(_LIGHTERBOT.set_enabled(b.get("enabled", not _LIGHTERBOT.enabled)))


async def _lighterbot_leverage(request: web.Request):
    if _LIGHTERBOT is None:
        return web.json_response({"ok": False, "error": "bot not running"})
    b = await request.json()
    return web.json_response(_LIGHTERBOT.set_leverage(b.get("leverage")))


async def _lighterbot_strategy(request: web.Request):
    if _LIGHTERBOT is None:
        return web.json_response({"ok": False, "error": "bot not running"})
    b = await request.json()
    return web.json_response(_LIGHTERBOT.set_strategy(b.get("strategy")))


async def _lighterbot_limitmode(request: web.Request):
    if _LIGHTERBOT is None:
        return web.json_response({"ok": False, "error": "bot not running"})
    b = await request.json()
    on = b.get("on", not getattr(_LIGHTERBOT, "_limit_mode", False))
    return web.json_response(_LIGHTERBOT.set_limit_mode(on))


async def _lighterbot_flow(request: web.Request):
    """Tiny, cheap endpoint: just the live buy/sell flow % over the window. The panel
    hits this several times a second so the flow bar looks live, without re-fetching
    the whole bot state."""
    try:
        fw = int(request.query.get("fw", "60"))
        fw = max(2, min(fw, 14400))
    except Exception:
        fw = 60
    # When the SCALPER strategy is active, force the EXACT window it decides on so the bar
    # shows the same number the bot acts on (no more "60% on the bar but bot acted on 80%").
    try:
        if _LIGHTERBOT is not None and getattr(_LIGHTERBOT, "strategy", "") == "scalper":
            import lighter_news_bot as _lnb
            fw = _lnb.SCALP_FLOW_WINDOW
    except Exception:
        pass
    try:
        # SAME cross-exchange tape as the 'Trades · live · all exchanges' panel, so the
        # bot bar and that panel agree.
        buy, sell = WHALES.flow(fw)
        tot = buy + sell
        pct = round(buy / tot * 100, 1) if tot > 0 else None
    except Exception:
        pct = None
    # Live BTC price too, so the panel can recompute the open position's P&L FAST
    # (every poll) instead of waiting for the slow account refresh.
    px = None
    try:
        if _MARKET is not None:
            px = _MARKET.price("BTCUSDT")
    except Exception:
        px = None
    return web.json_response({"flow_pct": pct, "flow_window": fw, "price": px})


async def _lighterbot_close(request: web.Request):
    if _LIGHTERBOT is None:
        return web.json_response({"ok": False, "error": "bot not running"})
    return web.json_response(await _LIGHTERBOT.close_now())


async def _lighterbot_reset(request: web.Request):
    if _LIGHTERBOT is None:
        return web.json_response({"ok": False, "error": "bot not running"})
    return web.json_response(_LIGHTERBOT.reset())


async def _lighterbot_page(request: web.Request):
    return web.Response(text=LIGHTERBOT_PAGE_HTML, content_type="text/html")


# ---- the bot's own account (read-only view) -------------------------------
async def _bot_state(request: web.Request):
    positions, equity = [], None
    if _BROKER is not None:
        try:
            equity = round(_BROKER.equity(), 2)
            for p in _BROKER.positions.values():
                mark = _MARKET.price(p.symbol) if _MARKET else None
                up = p.unrealized(mark) if mark else 0.0
                positions.append({
                    "symbol": p.symbol, "side": p.side, "entry": round(p.entry_price, 2),
                    "margin": round(p.margin, 2), "leverage": p.leverage,
                    "pnl": round(up, 2),
                    "pnl_pct": round(up / p.margin * 100, 2) if p.margin else 0})
        except Exception:
            pass
    # recent closed bot trades from the db
    history = []
    try:
        con = sqlite3.connect(config.DB_PATH)
        rows = con.execute(
            "SELECT symbol, side, entry_price, exit_price, pnl, reason, closed_at "
            "FROM trades ORDER BY closed_at DESC LIMIT 15").fetchall()
        con.close()
        for sym, side, ep, xp, pnl, reason, ts in rows:
            history.append({"symbol": sym, "side": side, "entry": round(ep or 0, 2),
                            "exit": round(xp or 0, 2), "pnl": round(pnl or 0, 2),
                            "reason": reason or "", "ts": ts})
    except Exception:
        pass
    return web.json_response({"equity": equity, "positions": positions, "history": history})


async def _manual_mark_loop():
    """Mark manual positions against the live price every second (SL/TP/liq)."""
    while True:
        MANUAL.mark(_btc_price())
        await asyncio.sleep(1)


async def _lighter_tape_feed():
    """Feed LIGHTER's own recent trades into the cross-exchange 'all exchanges' tape (WHALES).
    Lighter isn't on ccxt's trade API, so we pull it from the Lighter SDK and push new prints
    into the same tape the other venues use — it then shows in the live trades panel and counts
    in the flow, with 'lighter' appearing as a source. The fetch runs off the loop; the feed
    itself runs ON the loop (so WHALES._add / the SSE push stay single-threaded and safe)."""
    last_id = 0
    primed = False
    while True:
        await asyncio.sleep(1.5)
        try:
            if _LIGHTER is None or not _LIGHTER.configured():
                WHALES.sources["lighter"] = False
                continue
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, _LIGHTER.recent_trades_rows, 100)
            if rows is None:
                WHALES.sources["lighter"] = False
                continue
            WHALES.sources["lighter"] = True
            mx = max([last_id] + [r[0] for r in rows]) if rows else last_id
            if not primed:                       # first pass: prime ids, don't dump the backlog
                last_id = mx
                primed = True
                continue
            for tid, ts, usd, is_buy, price, size in sorted(rows):
                if tid > last_id and price > 0 and size > 0:
                    WHALES._add("lighter", "lt" + str(tid), price, size,
                                "buy" if is_buy else "sell", ts)
            last_id = mx
        except Exception:
            WHALES.sources["lighter"] = False


# ---- news + whale bot (read-only view + on/off) ---------------------------
async def _nw_state(request: web.Request):
    if _NWBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    try:
        fw = int(request.query.get("fw", str(config.NW_FLOW_WINDOW)))
    except ValueError:
        fw = config.NW_FLOW_WINDOW
    fw = max(10, min(fw, 14400))
    s = _NWBOT.state(flow_window_sec=fw); s["running"] = True
    return web.json_response(s)


async def _nw_toggle(request: web.Request):
    if _NWBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_NWBOT.set_enabled(b.get("enabled", not _NWBOT.enabled)))


async def _nw_reset(request: web.Request):
    if _NWBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_NWBOT.reset())


async def _nw_strategy(request: web.Request):
    if _NWBOT is None:
        return web.json_response({"ok": False, "error": "bot not running"})
    b = await request.json()
    return web.json_response(_NWBOT.set_strategy(b.get("strategy")))


# ---- AI NEWS BOT (reads the site's news feed, paper-trades the AI's call) ----
async def _newsbot_state(request: web.Request):
    s = NEWSAI.state()
    s["running"] = True
    return web.json_response(s)


async def _newsbot_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(NEWSAI.set_enabled(b.get("enabled", not NEWSAI.enabled)))


async def _newsbot_reset(request: web.Request):
    return web.json_response(NEWSAI.reset())


async def _newsbot_mode(request: web.Request):
    b = await request.json()
    return web.json_response(NEWSAI.set_mode(b.get("mode")))


# ---- NEWS SNIPER (no-AI rules engine, reacts to headline text instantly) ----
async def _sniperbot_state(request: web.Request):
    s = SNIPER.state()
    s["running"] = True
    return web.json_response(s)


async def _sniperbot_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(SNIPER.set_enabled(b.get("enabled", not SNIPER.enabled)))


async def _sniperbot_reset(request: web.Request):
    return web.json_response(SNIPER.reset())


async def _sniperbot_mode(request: web.Request):
    b = await request.json()
    return web.json_response(SNIPER.set_mode(b.get("mode")))


# ---- CROSS-EXCHANGE ARBITRAGE (Binance vs Hyperliquid, hedged paper) --------
async def _arb_state(request: web.Request):
    s = CROSSARB.state()
    s["running"] = True
    return web.json_response(s)


async def _arb_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(CROSSARB.set_enabled(b.get("enabled", not CROSSARB.enabled)))


async def _arb_reset(request: web.Request):
    return web.json_response(CROSSARB.reset())


async def _arb_lev(request: web.Request):
    b = await request.json()
    return web.json_response(CROSSARB.set_lev(b.get("lev")))


# ---- CRYPTAL PASSIVE MAKER + BINANCE HEDGE (public-data paper only) --------
async def _cryptal_maker_state(request: web.Request):
    return web.json_response(CRYPTALMAKER.state())


async def _cryptal_maker_toggle(request: web.Request):
    body = await request.json()
    return web.json_response(CRYPTALMAKER.set_enabled(
        body.get("enabled", not CRYPTALMAKER.enabled)))


async def _cryptal_maker_reset(request: web.Request):
    return web.json_response(CRYPTALMAKER.reset())


async def _cryptal_gel_maker_state(request: web.Request):
    return web.json_response(CRYPTALGELMAKER.state())


async def _cryptal_gel_maker_toggle(request: web.Request):
    body = await request.json()
    return web.json_response(CRYPTALGELMAKER.set_enabled(
        body.get("enabled", not CRYPTALGELMAKER.enabled)))


async def _cryptal_gel_maker_reset(request: web.Request):
    return web.json_response(CRYPTALGELMAKER.reset())


async def _cryptal_geo_state(request: web.Request):
    return web.json_response(CRYPTALGEOBOT.state())


async def _cryptal_geo_toggle(request: web.Request):
    body = await request.json()
    return web.json_response(CRYPTALGEOBOT.set_enabled(
        body.get("enabled", not CRYPTALGEOBOT.enabled)))


async def _cryptal_geo_reset(request: web.Request):
    return web.json_response(CRYPTALGEOBOT.reset())


async def _cryptal_geo_scan(request: web.Request):
    if CRYPTALGEOSCANNER.scan_in_progress:
        return web.json_response({"ok": True, "already_running": True})

    async def run_now():
        timeout = aiohttp.ClientTimeout(total=cryptal_maker_paper.REQUEST_TIMEOUT_SEC)
        connector = aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300
        )
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            try:
                await asyncio.gather(
                    CRYPTALGEOSCANNER.scan_once(session),
                    GEORGIANVENUES.scan_once(session),
                    return_exceptions=True,
                )
            except Exception:
                pass

    asyncio.create_task(run_now())
    return web.json_response({"ok": True})


AIRDROPS_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Airdrop Radar</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root{--bg:#070a0f;--card:#0b0906;--line:#1a1408;--gold:#fcd34d;--dim:#8b7a5a;
        --good:#4ade80;--bad:#f87171}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:#d8cdb4;
       font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
  a{color:var(--gold)}
  #top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 18px;
       background:linear-gradient(90deg,#120d05,#0a0805);border-bottom:1px solid var(--line);
       position:sticky;top:0;z-index:5}
  .nav{color:#8fa3bd;text-decoration:none;border:1px solid #223;border-radius:8px;
       padding:4px 12px;font-size:12px}
  .nav:hover{background:#111a26}
  h1{margin:0;font-size:17px;letter-spacing:.16em;text-transform:uppercase;font-weight:800;
     background:linear-gradient(100deg,#b45309,#fde68a 50%,#b45309);
     -webkit-background-clip:text;background-clip:text;color:transparent}
  .spacer{flex:1}
  .wrap{max-width:1180px;margin:0 auto;padding:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        margin-bottom:16px;overflow:hidden}
  .card h2{margin:0;padding:11px 16px;font-size:12px;letter-spacing:.1em;
           text-transform:uppercase;color:#b3873f;background:#100c06;
           border-bottom:1px solid var(--line)}
  .ask{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;padding:18px 16px}
  .ask label{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;
             letter-spacing:.07em;margin-bottom:6px}
  #amount{background:#0a0805;border:1px solid #78350f;color:var(--gold);padding:11px 14px;
          border-radius:9px;width:190px;font:700 20px ui-monospace,monospace}
  #go{background:linear-gradient(100deg,#b45309,#f59e0b 45%,#fde68a);color:#3b2503;
      border:1px solid #fcd34d;border-radius:9px;padding:12px 26px;cursor:pointer;
      font:800 13px/1 inherit;letter-spacing:.14em;text-transform:uppercase}
  #go:hover{filter:brightness(1.1)}
  #go:disabled{opacity:.5;cursor:wait}
  .kpis{display:flex;flex-wrap:wrap;gap:26px;padding:16px}
  .kpi .k{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;
          letter-spacing:.07em}
  .kpi .v{font-size:21px;font-weight:700;color:var(--gold);
          font-variant-numeric:tabular-nums}
  .v.good{color:var(--good)}.v.bad{color:var(--bad)}
  .warn{background:#2d0a0a;border:1px solid #7f1d1d;color:#fca5a5;padding:12px 16px;
        font-size:12.5px;line-height:1.6}
  .warn b{color:#fecaca}
  .note{padding:12px 16px;color:var(--dim);font-size:11.5px;line-height:1.7}
  .farm{border-bottom:1px solid var(--line)}
  .fhead{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:13px 16px;
         cursor:pointer}
  .fhead:hover{background:#120d06}
  .fn{color:var(--gold);font-weight:700;font-size:14px}
  .pill{font-size:9.5px;font-weight:800;padding:3px 9px;border-radius:999px;
        letter-spacing:.06em;white-space:nowrap}
  .p-STRONG{background:#052e16;color:var(--good);border:1px solid #166534}
  .p-MODERATE{background:#1c1917;color:#fbbf24;border:1px solid #78350f}
  .p-SPECULATIVE{background:#2a1207;color:#fb923c;border:1px solid #7c2d12}
  .p-HIGHRISK{background:#2d0a0a;color:var(--bad);border:1px solid #7f1d1d}
  .cost{font-family:ui-monospace,monospace;font-size:12px;color:#e5d5b0}
  .chance{font-family:ui-monospace,monospace;font-size:12px;color:var(--good);font-weight:700}
  .detail{padding:0 16px 16px 16px;background:#080604}
  .grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;
         margin:12px 0}
  .box{background:#0d0a06;border:1px solid var(--line);border-radius:9px;padding:11px 13px}
  .box h4{margin:0 0 7px 0;font-size:10px;letter-spacing:.09em;text-transform:uppercase;
          color:#b3873f}
  .box .line{font-size:12px;color:#c9b58a;margin:4px 0;display:flex;justify-content:space-between;gap:10px}
  .box .line b{color:#e5d5b0;font-variant-numeric:tabular-nums}
  .math{font-family:ui-monospace,monospace;font-size:11px;color:#9fb8a0;margin:5px 0;
        word-break:break-word}
  ol.steps{margin:6px 0 0 20px;padding:0}
  ol.steps li{margin:8px 0;color:#c9b58a;font-size:12.5px;line-height:1.65}
  .st{font-size:9.5px;font-weight:800;padding:3px 9px;border-radius:999px;letter-spacing:.06em}
  .st-OPEN{background:#052e16;color:#4ade80;border:1px solid #166534}
  .st-PENDING{background:#1c1917;color:#fbbf24;border:1px solid #78350f}
  .st-DISTRIBUTED{background:#2d0a0a;color:#f87171;border:1px solid #7f1d1d}
  .conf{border-bottom:1px solid var(--line);padding:13px 16px}
  .conf.gone{opacity:.5}
  .conf .top{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  .conf .nm{color:var(--gold);font-weight:700;font-size:14px}
  .conf .win{font-family:ui-monospace,monospace;font-size:11.5px;color:#e5d5b0}
  .conf .src{font-size:11px;color:var(--dim);margin-top:5px}
  .conf ol{margin:8px 0 0 20px;padding:0}
  .conf li{margin:5px 0;color:#c9b58a;font-size:12px;line-height:1.6}
  .lesson{background:#101418;border:1px solid #1e2a38;color:#9fb8c8;padding:12px 16px;
          font-size:12.5px;line-height:1.65}
  .empty{padding:40px;text-align:center;color:var(--dim)}
  table{width:100%;border-collapse:collapse}
  th{background:#120d05;color:#b3873f;font-size:10px;text-transform:uppercase;
     letter-spacing:.08em;text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}
  td{padding:9px 12px;border-bottom:1px solid var(--line);font-size:12px}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
</style></head><body>
<div id="top">
  <a class="nav" href="/">&larr; Chart</a>
  <a class="nav" href="/paper">Paper Trading</a>
  <h1>&#10022; Airdrop Radar</h1>
  <span class="spacer"></span>
  <span id="meta" style="color:#8b7a5a;font-size:11.5px"></span>
</div>
<div class="wrap">

  <div class="card">
    <h2>How much do you want to invest?</h2>
    <div class="ask">
      <div>
        <label for="amount">Amount you want to invest (USD)</label>
        <input id="amount" type="number" min="10" step="10" value="100">
      </div>
      <button id="go" onclick="analyze()">Analyze airdrops</button>
      <div style="flex:1;min-width:220px;color:#8b7a5a;font-size:11.5px;line-height:1.6">
        Most of this is a deposit you can withdraw again. Only the gas is spent.
      </div>
    </div>
  </div>

  <div id="out"><div class="card"><div class="empty">Enter an amount and press Analyze.</div></div></div>
</div>
<script>
const $=i=>document.getElementById(i);
const esc=t=>String(t==null?'':t).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const usd=v=>'$'+Number(v||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const usd0=v=>'$'+Number(v||0).toLocaleString(undefined,{maximumFractionDigits:0});
const pct=v=>Math.round((v||0)*100)+'%';
let OPEN={};

async function analyze(){
  const amt=Number(($('amount')||{}).value||0);
  $('go').disabled=true; $('go').textContent='Analyzing\u2026';
  try{
    await fetch('/api/airdrops/bankroll',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({bankroll_usd:amt})});
  }catch(e){}
  await load();
  $('go').disabled=false; $('go').textContent='Analyze airdrops';
}

async function load(){
  let s; try{ s=await(await fetch('/api/airdrops/state')).json(); }catch(e){ return; }
  const plan=s.plan||{}, rows=s.rows||[], a=s.assumptions||{};
  $('meta').textContent = s.error ? ('scan error: '+s.error)
    : (Number(s.universe||0).toLocaleString()+' protocols scanned in '
       +Number(s.scan_seconds||0).toFixed(2)+'s \u2192 '+(s.candidates||0)+' candidates');

  if(!rows.length){
    $('out').innerHTML='<div class="card"><div class="empty">'+
      (s.error?esc(s.error):'first scan still running\u2026')+'</div></div>'; return;
  }
  if(!plan.affordable){
    $('out').innerHTML='<div class="card"><h2>Not enough to start</h2><div class="warn">'+
      'The cheapest farm needs <b>'+usd(plan.cheapest_entry_usd)+'</b> '+
      '('+usd(25)+' deposit that clears a threshold, plus gas). You are '+
      usd(plan.shortfall_usd)+' short. A deposit below that buys the gas without '+
      'buying the eligibility, so it is worse than not farming at all.</div></div>';
    return;
  }

  const cf=s.confirmed||{};
  let html='';
  if(cf.open||cf.distributed){
    const card=function(e,gone){
      return '<div class="conf'+(gone?' gone':'')+'"><div class="top">'+
        '<span class="nm">'+esc(e.name)+'</span>'+
        '<span class="st st-'+esc(e.status)+'">'+esc(e.status)+'</span>'+
        '<span class="win">'+esc(e.token)+' · '+esc(e.tge_window)+'</span></div>'+
        '<div class="src">Confirmed by: '+esc(e.confirmed_by)+
        ' — <a href="'+esc(e.source_url)+'" target="_blank" rel="noopener noreferrer">source</a></div>'+
        '<div class="src">'+esc(e.why_it_matters||'')+'</div>'+
        ((e.qualify||[]).length?'<ol>'+e.qualify.map(function(q){return '<li>'+esc(q)+'</li>';}).join('')+'</ol>':'')+
        '<div class="src" style="color:#f87171">Risk: '+esc(e.risk||'')+'</div></div>';
    };
    html+='<div class="card"><h2>Confirmed by the team — '+(cf.farmable_count||0)+
      ' still open</h2><div class="lesson">'+esc(cf.lesson||'')+'</div>'+
      (cf.open||[]).map(function(e){return card(e,false);}).join('')+
      (cf.pending||[]).map(function(e){return card(e,false);}).join('')+
      (cf.distributed||[]).map(function(e){return card(e,true);}).join('')+
      '<div class="warn">'+esc(cf.caveat||'')+'</div>'+
      '<div class="note">'+esc((cf.verified||{}).note||'')+' Verified '+
      esc((cf.verified||{}).verified_on||'')+', '+Number((cf.verified||{}).days_old||0).toFixed(0)+
      ' days ago.'+((cf.verified||{}).stale?' <b style="color:#f87171">This check is stale — re-verify each source.</b>':'')+
      '</div></div>';
  }
  html+='<div class="card"><h2>Speculative candidates — your plan</h2><div class="kpis">'+
    '<div class="kpi"><span class="k">airdrops you can farm</span><span class="v">'+plan.farms+'</span></div>'+
    '<div class="kpi"><span class="k">deposit in each</span><span class="v">'+usd(plan.deposit_each_usd)+'</span></div>'+
    '<div class="kpi"><span class="k">gas (actually spent)</span><span class="v bad">'+usd(plan.gas_total_usd)+'</span></div>'+
    '<div class="kpi"><span class="k">you can withdraw</span><span class="v">'+usd(plan.recoverable_usd)+'</span></div>'+
    '<div class="kpi"><span class="k">expected total</span><span class="v good">'+usd(plan.expected_total_usd)+'</span></div>'+
    '<div class="kpi"><span class="k">if every one lands</span><span class="v good">'+usd(plan.upside_total_usd)+'</span></div>'+
    '<div class="kpi"><span class="k">chance at least one pays</span><span class="v good">'+pct(plan.probability_any_pays)+'</span></div>'+
    '<div class="kpi"><span class="k">chance nothing pays</span><span class="v bad">'+pct(plan.probability_of_nothing)+'</span></div>'+
    '</div><div class="note">Of your '+usd(plan.amount_usd)+', only <b>'+usd(plan.gas_total_usd)+
    '</b> is truly spent (gas). The rest sits in the protocols and can be withdrawn. '+
    'Horizon: '+esc(plan.horizon||'')+'.'+
    (plan.capped_by_time?' <b>'+esc(plan.cap_note||'')+'</b>':'')+'</div></div>';

  html+='<div class="card"><div class="warn"><b>Before you deposit anything.</b> '+
    'These are ranked <b>candidates, not confirmed airdrops</b> \u2014 a high score means the '+
    'protocol looks real, not that a token is coming. <b>No airdrop here has an announced end '+
    'date</b>, because unannounced drops do not publish one; any site showing you a countdown '+
    'is inventing it. The dollar figures are a model, not a measurement: '+esc(a.caveat||'')+
    '</div></div>';

  html+='<div class="card"><h2>Farm these '+plan.farms+' \u2014 click one for exact steps</h2>';
  (plan.rows||[]).forEach(function(r,i){
    const c=r.cost||{}, e=r.expected||{}, t=r.timing||{}, m=r.probability_math||{};
    const band=(r.risk_band||'').replace(' ','');
    const open=OPEN[i]?'block':'none';
    html+='<div class="farm"><div class="fhead" onclick="tog('+i+')">'+
      '<span style="color:#6b5c40;font-variant-numeric:tabular-nums">'+(i+1)+'</span>'+
      '<span class="fn">'+esc(r.name)+'</span>'+
      '<span class="pill p-'+esc(band)+'">'+esc(r.risk_band||'')+'</span>'+
      '<span class="cost">costs '+usd(c.total_usd)+' \u00b7 on '+esc(c.farm_on_chain||'')+'</span>'+
      '<span class="spacer"></span>'+
      '<span class="chance">'+pct(e.p_paid)+' chance you get paid</span>'+
      '<span class="cost">\u2192 '+usd(e.value_if_paid_usd)+'</span>'+
      '</div><div class="detail" id="d'+i+'" style="display:'+open+'">'+

      '<div class="grid2">'+
        '<div class="box"><h4>What it costs you</h4>'+
          '<div class="line"><span>deposit (you get this back)</span><b>'+usd(c.deposit_usd)+'</b></div>'+
          '<div class="line"><span>gas on '+esc(c.farm_on_chain||'')+' (spent)</span><b>'+usd(c.gas_usd)+'</b></div>'+
          '<div class="line"><span>total to enter</span><b>'+usd(c.total_usd)+'</b></div>'+
          '<div class="line"><span>minimum that still works</span><b>'+usd(c.minimum_total_usd)+'</b></div>'+
        '</div>'+
        '<div class="box"><h4>Chance, calculated</h4>'+
          '<div class="math">'+esc(m.formula_drops||'')+'</div>'+
          '<div class="math">'+esc(m.formula_paid||'')+'</div>'+
          '<div class="math">'+esc(m.formula_value||'')+'</div>'+
          '<div class="line"><span>expected value</span><b>'+usd(e.expected_usd)+'</b></div>'+
          '<div class="note" style="padding:6px 0 0 0">'+esc(m.caveat||'')+'</div>'+
        '</div>'+
        '<div class="box"><h4>Timing</h4>'+
          '<div class="line"><span>running without a token</span><b>'+Number(t.months_without_token||0).toFixed(0)+' months</b></div>'+
          '<div class="line"><span>stage</span><b>'+esc(t.stage||'')+'</b></div>'+
          '<div class="line"><span>announced end date</span><b style="color:#f87171">none</b></div>'+
          '<div class="note" style="padding:6px 0 0 0">'+esc(t.note||'')+' '+esc(t.deadline_note||'')+'</div>'+
        '</div>'+
        '<div class="box"><h4>Evidence</h4>'+
          '<div class="line"><span>TVL</span><b>'+usd0(r.tvl_usd)+'</b></div>'+
          '<div class="line"><span>audits</span><b>'+(r.audits||0)+'</b></div>'+
          '<div class="line"><span>category</span><b>'+esc(r.category||'')+'</b></div>'+
          '<div class="note" style="padding:6px 0 0 0">'+esc((r.signals||[]).join(' \u00b7 '))+'</div>'+
        '</div>'+
      '</div>'+

      '<h4 style="margin:14px 0 0 0;font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:#b3873f">'+
      'Exactly what to do'+(r.url?' \u2014 <a href="'+esc(r.url)+'" target="_blank" rel="noopener noreferrer">'+esc(r.url)+'</a>':'')+'</h4>'+
      '<ol class="steps">'+(r.instructions||[]).map(function(x){return '<li>'+esc(x)+'</li>';}).join('')+'</ol>'+
      '</div></div>';
  });
  html+='</div>';

  const rest=rows.filter(function(r){return !r.funded;});
  if(rest.length){
    html+='<div class="card"><h2>'+rest.length+' more candidates your budget does not reach</h2>'+
      '<table><thead><tr><th>protocol</th><th>risk</th><th class="num">score</th>'+
      '<th class="num">TVL</th><th class="num">costs</th><th class="num">chance paid</th>'+
      '<th>farm on</th></tr></thead><tbody>'+
      rest.map(function(r){
        const c=r.cost||{}, e=r.expected||{};
        return '<tr><td>'+(r.url?'<a href="'+esc(r.url)+'" target="_blank" rel="noopener noreferrer">'+esc(r.name)+'</a>':esc(r.name))+'</td>'+
          '<td>'+esc(r.risk_band||'')+'</td><td class="num">'+Number(r.score||0).toFixed(0)+'</td>'+
          '<td class="num">'+usd0(r.tvl_usd)+'</td><td class="num">'+usd(c.minimum_total_usd)+'</td>'+
          '<td class="num">'+pct(e.p_paid)+'</td><td>'+esc(c.farm_on_chain||'')+'</td></tr>';
      }).join('')+'</tbody></table></div>';
  }

  html+='<div class="card"><h2>How these numbers are built</h2><div class="note">'+
    'Candidates are DeFiLlama protocols holding real TVL in a user-facing category with '+
    '<b>no token yet</b>, ranked by a legitimacy score from published audits, TVL at risk, '+
    'age, disclosure and chain spread, minus a penalty for mercenary TVL spikes.<br><br>'+
    'Multi-chain protocols are costed on their <b>cheapest</b> chain, since activity usually '+
    'counts on any deployment and paying Ethereum gas for the same allocation is money burned.'+
    '<br><br>Payoff assumes at best a '+pct(a.p_airdrop_range?a.p_airdrop_range[1]:0)+
    ' chance a protocol drops, that only '+pct(a.value_retention)+' of an allocation keeps its '+
    'value ('+esc(a.value_retention_basis||'')+'), and that '+pct(1-(a.qualification_rate||0))+
    ' of small wallets are filtered out ('+esc(a.qualification_basis||'')+'). Allocation is '+
    'discounted by deposit size \u2014 but because it is logarithmic, a small amount still earns '+
    'a better <i>multiple</i> than a large one.</div></div>';

  $('out').innerHTML=html;
}
function tog(i){
  OPEN[i]=!OPEN[i];
  const d=$('d'+i); if(d) d.style.display=OPEN[i]?'block':'none';
}
load();
</script></body></html>
"""


async def _airdrops_page(request: web.Request):
    return web.Response(text=AIRDROPS_HTML, content_type="text/html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ---- AIRDROP RADAR (read-only DeFiLlama scan, no wallet or key involved) ----
async def _airdrops_state(request: web.Request):
    return web.json_response({**AIRDROPS.state(),
                              "confirmed": confirmed_airdrops.overview()})


async def _airdrops_bankroll(request: web.Request):
    body = await request.json()
    return web.json_response(AIRDROPS.set_bankroll(body.get("bankroll_usd", 100.0)))


async def _airdrops_refresh(request: web.Request):
    await AIRDROPS.refresh()
    return web.json_response({"ok": True, "error": AIRDROPS.error})


# ---- FAIR-VALUE TRACKING (Lighter follower vs leader consensus, zero-fee) ---
async def _fv_state(request: web.Request):
    s = FVTRACK.state()
    s["running"] = True
    return web.json_response(s)


async def _fv_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(FVTRACK.set_enabled(b.get("enabled", not FVTRACK.enabled)))


async def _fv_reset(request: web.Request):
    return web.json_response(FVTRACK.reset())


async def _fv_lev(request: web.Request):
    b = await request.json()
    return web.json_response(FVTRACK.set_lev(b.get("lev")))


# ---- CRYPTO TREND BREAKOUT + ATR (daily, BTC/ETH/SOL, Goodman method) -------
async def _trend_state(request: web.Request):
    s = TREND.state()
    s["running"] = True
    return web.json_response(s)


async def _trend_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(TREND.set_enabled(b.get("enabled", not TREND.enabled)))


async def _trend_reset(request: web.Request):
    return web.json_response(TREND.reset())


async def _trend_risk(request: web.Request):
    b = await request.json()
    return web.json_response(TREND.set_risk(b.get("risk")))


# ---- AI NEWS TRADING BOT (Google News RSS -> LLM sentiment -> BTC paper) ----
async def _ainews_state(request: web.Request):
    s = AINEWS.state()
    s["running"] = True
    return web.json_response(s)


async def _ainews_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(AINEWS.set_enabled(b.get("enabled", not AINEWS.enabled)))


async def _ainews_reset(request: web.Request):
    return web.json_response(AINEWS.reset())


# ---- CLAUDE HAIKU LIVE-FEED NEWS BOT (paper, BTC, $100/20x) ----------------
async def _claudehaiku_state(request: web.Request):
    s = CLAUDEHAIKU.state()
    s["running"] = True
    return web.json_response(s)


async def _claudehaiku_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(CLAUDEHAIKU.set_enabled(b.get("enabled", not CLAUDEHAIKU.enabled)))


async def _claudehaiku_reset(request: web.Request):
    return web.json_response(CLAUDEHAIKU.reset())


# ---- TRADINGVIEW STRATEGY PACK (12 strategies, each $100/20x paper on BTC) ---
async def _tvstrats_state(request: web.Request):
    s = TVSTRATS.state()
    s["running"] = True
    return web.json_response(s)


async def _tvstrats_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(TVSTRATS.set_enabled(b.get("key"), b.get("enabled", True)))


async def _tvstrats_reset(request: web.Request):
    b = await request.json()
    return web.json_response(TVSTRATS.reset(b.get("key")))


# ---- BOLLINGER + RSI + 200-SMA mean reversion ($100/20x paper, BTC 1h) ------
async def _meanrev_state(request: web.Request):
    s = MEANREV.state()
    s["running"] = True
    return web.json_response(s)


async def _meanrev_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(MEANREV.set_enabled(b.get("enabled", not MEANREV.enabled)))


async def _meanrev_reset(request: web.Request):
    return web.json_response(MEANREV.reset())


# ---- NEWS-MOMENTUM (1.5s pre-news >=0.08% move -> bet direction; 5% trail exit) ----
async def _newsmomo_state(request: web.Request):
    s = NEWSMOMO.state()
    s["running"] = True
    return web.json_response(s)


async def _newsmomo_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(NEWSMOMO.set_enabled(b.get("enabled", not NEWSMOMO.enabled)))


async def _newsmomo_reset(request: web.Request):
    return web.json_response(NEWSMOMO.reset())


# ---- RSI2 EMA50 SCALPER (BTC 15m, $100/10x, daily stop + cooldown) ----------
async def _rsi2noatr_state(request: web.Request):
    s = RSI2NOATR.state()
    s["running"] = True
    return web.json_response(s)


async def _rsi2noatr_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(RSI2NOATR.set_enabled(b.get("enabled", not RSI2NOATR.enabled)))


async def _rsi2noatr_reset(request: web.Request):
    return web.json_response(RSI2NOATR.reset())


async def _rsi2atr_state(request: web.Request):
    s = RSI2ATR.state()
    s["running"] = True
    return web.json_response(s)


async def _rsi2atr_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(RSI2ATR.set_enabled(b.get("enabled", not RSI2ATR.enabled)))


async def _rsi2atr_reset(request: web.Request):
    return web.json_response(RSI2ATR.reset())


# ---- ALL-PATTERN CONSENSUS OPTIMIZED PAPER BOTS ---------------------------
async def _patternbots_state(request: web.Request):
    return web.json_response({"running": True, "bots": [b.state() for b in PATTERN_BOTS.values()]})


def _patternbot_pick(key: str):
    return PATTERN_BOTS.get(str(key or ""))


async def _patternbots_toggle(request: web.Request):
    b = await request.json()
    bot = _patternbot_pick((b or {}).get("key"))
    if bot is None:
        return web.json_response({"error": "unknown pattern bot"}, status=404)
    return web.json_response(bot.set_enabled((b or {}).get("enabled", not bot.enabled)))


async def _patternbots_reset(request: web.Request):
    b = await request.json()
    bot = _patternbot_pick((b or {}).get("key"))
    if bot is None:
        return web.json_response({"error": "unknown pattern bot"}, status=404)
    return web.json_response(bot.reset())


# ---- NEWS PAPER BOT (rule-based news -> paper trades; $10k; BTC/ETH/SOL) --------
async def _penny_page(request: web.Request):
    return web.Response(text=PENNY_HTML, content_type="text/html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                                 "Pragma": "no-cache", "Expires": "0"})


async def _penny_state(request: web.Request):
    return web.json_response(PENNY.state())


async def _penny_toggle(request: web.Request):
    b = await request.json()
    return web.json_response(PENNY.set_enabled(b.get("enabled", not PENNY.enabled)))


async def _penny_reset(request: web.Request):
    return web.json_response(PENNY.reset())


async def _penny_scan(request: web.Request):
    return web.json_response(await PENNY.scan_now())


async def _newspaper_state(request: web.Request):
    s = NEWSPAPER.state()
    s["running"] = True
    return web.json_response(s)


async def _newspaper_start(request: web.Request):
    return web.json_response(NEWSPAPER.start())


async def _newspaper_stop(request: web.Request):
    return web.json_response(NEWSPAPER.stop())


async def _newspaper_reset(request: web.Request):
    return web.json_response(NEWSPAPER.reset())


async def _newspaper_settings(request: web.Request):
    b = await request.json()
    return web.json_response(NEWSPAPER.update_settings(b or {}))


async def _newspaper_test(request: web.Request):
    b = await request.json()
    d = NEWSPAPER.test_news((b or {}).get("headline", ""), (b or {}).get("source", "Test (Reuters)"))
    return web.json_response({"ok": True, "decision": d})


# ---- ICT 2022-model paper bot (sweep -> MSS -> FVG) -----------------------
async def _ict_state(request: web.Request):
    if _ICTBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_ICTBOT.state())


async def _ict_toggle(request: web.Request):
    if _ICTBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_ICTBOT.set_enabled(b.get("enabled", not _ICTBOT.enabled)))


async def _ict_reset(request: web.Request):
    if _ICTBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_ICTBOT.reset())


# ---- ICT SM Trades paper bot (liquidity grab -> MSS -> FVG, killzones; BTC) ----
async def _ictsm_state(request: web.Request):
    if _ICTSMBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_ICTSMBOT.state())


async def _ictsm_toggle(request: web.Request):
    if _ICTSMBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_ICTSMBOT.set_enabled(b.get("enabled", not _ICTSMBOT.enabled)))


async def _ictsm_reset(request: web.Request):
    if _ICTSMBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_ICTSMBOT.reset())


# ---- Frequent ICT paper copy ------------------------------------------------
async def _ictfreq_state(request: web.Request):
    if _ICTFREQBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_ICTFREQBOT.state())


async def _ictfreq_toggle(request: web.Request):
    if _ICTFREQBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_ICTFREQBOT.set_enabled(b.get("enabled", not _ICTFREQBOT.enabled)))


async def _ictfreq_reset(request: web.Request):
    if _ICTFREQBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_ICTFREQBOT.reset())


# ---- Freqtrade-style paper bot (RSI + Bollinger + ROI/stop/trail) ---------
async def _freq_state(request: web.Request):
    if _FREQBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_FREQBOT.state())


async def _freq_toggle(request: web.Request):
    if _FREQBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_FREQBOT.set_enabled(b.get("enabled", not _FREQBOT.enabled)))


async def _freq_reset(request: web.Request):
    if _FREQBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_FREQBOT.reset())


# ---- Freqtrade-style paper bot, improved margin-based TP/SL ----------------
async def _freqtp_state(request: web.Request):
    if _FREQTPBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_FREQTPBOT.state())


async def _freqtp_toggle(request: web.Request):
    if _FREQTPBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_FREQTPBOT.set_enabled(b.get("enabled", not _FREQTPBOT.enabled)))


async def _freqtp_reset(request: web.Request):
    if _FREQTPBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_FREQTPBOT.reset())


# ---- Freqtrade-style paper bot, trend-follow TP/SL -------------------------
async def _freqtrend_state(request: web.Request):
    if _FREQTRENDBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_FREQTRENDBOT.state())


async def _freqtrend_toggle(request: web.Request):
    if _FREQTRENDBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_FREQTRENDBOT.set_enabled(b.get("enabled", not _FREQTRENDBOT.enabled)))


async def _freqtrend_reset(request: web.Request):
    if _FREQTRENDBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_FREQTRENDBOT.reset())


# ---- Freqtrade-style paper bot, improved TP/SL 5% trailing gap --------------
async def _freq5_state(request: web.Request):
    if _FREQ5BOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_FREQ5BOT.state())


async def _freq5_toggle(request: web.Request):
    if _FREQ5BOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_FREQ5BOT.set_enabled(b.get("enabled", not _FREQ5BOT.enabled)))


async def _freq5_reset(request: web.Request):
    if _FREQ5BOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_FREQ5BOT.reset())


# ---- Freqtrade-style paper bot, trend + order-flow confirmed ----------------
async def _freqtf_state(request: web.Request):
    if _FREQTFBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_FREQTFBOT.state())


async def _freqtf_toggle(request: web.Request):
    if _FREQTFBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_FREQTFBOT.set_enabled(b.get("enabled", not _FREQTFBOT.enabled)))


async def _freqtf_reset(request: web.Request):
    if _FREQTFBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_FREQTFBOT.reset())


# ---- Trend-Sweep VWAP paper bot (4H trend · PDL/PDH sweep · 5m breakout) ---
async def _ts_state(request: web.Request):
    if _TSBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_TSBOT.state())


async def _ts_toggle(request: web.Request):
    if _TSBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_TSBOT.set_enabled(b.get("enabled", not _TSBOT.enabled)))


async def _ts_reset(request: web.Request):
    if _TSBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_TSBOT.reset())


# ---- Apex ES VWAP + Opening Range paper bot --------------------------------
async def _apexvwap_state(request: web.Request):
    if _APEXVWAPBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_APEXVWAPBOT.state())


async def _apexvwap_toggle(request: web.Request):
    if _APEXVWAPBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_APEXVWAPBOT.set_enabled(b.get("enabled", not _APEXVWAPBOT.enabled)))


async def _apexvwap_reset(request: web.Request):
    if _APEXVWAPBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_APEXVWAPBOT.reset())


# ---- Lucid 50K continuous basket paper bot ----------------------------------
async def _lucidcont_state(request: web.Request):
    if _LUCIDCONTBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": [], "setups": []})
    return web.json_response(_LUCIDCONTBOT.state())


async def _lucidcont_toggle(request: web.Request):
    if _LUCIDCONTBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_LUCIDCONTBOT.set_enabled(b.get("enabled", not _LUCIDCONTBOT.enabled)))


async def _lucidcont_notify(request: web.Request):
    if _LUCIDCONTBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_LUCIDCONTBOT.set_notifications(
        b.get("enabled", not _LUCIDCONTBOT.telegram_enabled)
    ))


async def _lucidcont_reset(request: web.Request):
    if _LUCIDCONTBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_LUCIDCONTBOT.reset())


# ---- Lucid 50K monthly pass basket paper bot --------------------------------
async def _lucidpass_state(request: web.Request):
    if _LUCIDPASSBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": [], "setups": []})
    return web.json_response(_LUCIDPASSBOT.state())


async def _lucidpass_toggle(request: web.Request):
    if _LUCIDPASSBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_LUCIDPASSBOT.set_enabled(b.get("enabled", not _LUCIDPASSBOT.enabled)))


async def _lucidpass_notify(request: web.Request):
    if _LUCIDPASSBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_LUCIDPASSBOT.set_notifications(
        b.get("enabled", not _LUCIDPASSBOT.telegram_enabled)
    ))


async def _lucidpass_reset(request: web.Request):
    if _LUCIDPASSBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_LUCIDPASSBOT.reset())


# ---- NQ 15m mean-reversion flat600 paper bot --------------------------------
async def _nqmr15_state(request: web.Request):
    if _NQMR15BOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": [], "setups": []})
    return web.json_response(_NQMR15BOT.state())


async def _nqmr15_toggle(request: web.Request):
    if _NQMR15BOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_NQMR15BOT.set_enabled(b.get("enabled", not _NQMR15BOT.enabled)))


async def _nqmr15_reset(request: web.Request):
    if _NQMR15BOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_NQMR15BOT.reset())


# ---- NR7 breakout Apex paper bot (ES+NQ+CL) --------------------------------
async def _nr7_state(request: web.Request):
    if _NR7BOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": [], "setups": []})
    return web.json_response(_NR7BOT.state())


async def _nr7_toggle(request: web.Request):
    if _NR7BOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_NR7BOT.set_enabled(b.get("enabled", not _NR7BOT.enabled)))


async def _nr7_reset(request: web.Request):
    if _NR7BOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_NR7BOT.reset())


# ---- NR7 Aggressive (NR7 + NQ reversion) paper bot --------------------------
async def _nr7aggr_state(request: web.Request):
    if _NR7AGGRBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": [], "setups": []})
    return web.json_response(_NR7AGGRBOT.state())


async def _nr7aggr_toggle(request: web.Request):
    if _NR7AGGRBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_NR7AGGRBOT.set_enabled(b.get("enabled", not _NR7AGGRBOT.enabled)))


async def _nr7aggr_reset(request: web.Request):
    if _NR7AGGRBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_NR7AGGRBOT.reset())


# ---- OB / Smart-Money paper bots (Reddit Freqtrade port) on 1m/5m/15m ------
def _ob_pick(request):
    return _OB_BOTS.get(request.query.get("tf", "1m"))


async def _ob_state(request: web.Request):
    bot = _ob_pick(request)
    if bot is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(bot.state())


async def _ob_toggle(request: web.Request):
    bot = _ob_pick(request)
    if bot is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(bot.set_enabled(b.get("enabled", not bot.enabled)))


async def _ob_reset(request: web.Request):
    bot = _ob_pick(request)
    if bot is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(bot.reset())


# ---- COT crowded-positioning fade paper bot -------------------------------
async def _cot_state(request: web.Request):
    if _COTBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": []})
    return web.json_response(_COTBOT.state())


async def _cot_toggle(request: web.Request):
    if _COTBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_COTBOT.set_enabled(b.get("enabled", not _COTBOT.enabled)))


async def _cot_reset(request: web.Request):
    if _COTBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_COTBOT.reset())


# ---- On-chain Radar paper bot ---------------------------------------------
async def _onchain_state(request: web.Request):
    if _ONCHAINBOT is None:
        return web.json_response({"enabled": False, "running": False,
                                  "positions": [], "history": [], "log": [],
                                  "signals": [], "learned_wallets": []})
    return web.json_response(_ONCHAINBOT.state())


async def _onchain_toggle(request: web.Request):
    if _ONCHAINBOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_ONCHAINBOT.set_enabled(b.get("enabled", not _ONCHAINBOT.enabled)))


async def _onchain_reset(request: web.Request):
    if _ONCHAINBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_ONCHAINBOT.reset())


# ---- Polymarket copy-trade paper bot (mirrors @huskyvs) --------------------
async def _poly_state(request: web.Request):
    if _POLYBOT is None:
        return web.json_response({"running": False, "status": "bot not running",
                                  "positions": [], "history": [], "opens": []})
    return web.json_response(_POLYBOT.state())


async def _poly_reset(request: web.Request):
    if _POLYBOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_POLYBOT.reset())


# ---- ICT Lab page (/ict): live visual ICT scanner -------------------------
ICTLAB_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICT Lab - Live Visual Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root{--bg:#05070a;--panel:#0a0d12;--panel2:#10151d;--line:#1d2633;--line2:#2b3748;
    --txt:#edf3fb;--muted:#7c8798;--amber:#f2b84b;--green:#19c37d;--red:#ff4d5f;--blue:#3aa0ff;
    --mono:'IBM Plex Mono',ui-monospace,monospace;--sans:'IBM Plex Sans',system-ui,sans-serif}
  *{box-sizing:border-box}
  html,body{margin:0;background:#05070a;color:var(--txt);font-family:var(--sans);font-size:13px}
  body{background:linear-gradient(180deg,#05070a 0%,#070a0f 56%,#05070a 100%)}
  #topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    padding:10px 14px;background:#070a0f;border-bottom:1px solid #263244;box-shadow:0 12px 28px rgba(0,0,0,.28)}
  .ttl{font-family:var(--mono);font-size:16px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#f5f8fc}
  .ttl .dot{color:var(--amber)}
  .nav,.btn,select{font-family:var(--mono);font-size:12px;border-radius:4px;background:#0e131b;border:1px solid #263244;
    color:#cdd6e3;padding:7px 10px;text-decoration:none;box-shadow:inset 0 1px 0 rgba(255,255,255,.025)}
  .nav:hover,.btn:hover,select:hover{background:#121925;border-color:#3a4658;color:#fff}
  .btn{cursor:pointer}.btn.run{background:#0b281d;border-color:#1d8c62;color:#42e49b}
  .btn.pause{background:#2a1117;border-color:#813040;color:#ff6b78}.btn.reset{color:#8793a5}
  .spacer{flex:1}.status{font-family:var(--mono);font-size:12px;color:#8793a5}
  .shell{display:grid;grid-template-columns:minmax(0,1fr) 380px;min-height:calc(100vh - 52px)}
  @media(max-width:1050px){.shell{grid-template-columns:1fr}.side{border-left:0;border-top:1px solid var(--line)}}
  .chartarea{min-width:0;padding:12px}
  #chartbox{position:relative;height:calc(100vh - 84px);min-height:520px;border:1px solid #202a39;border-radius:6px;
    overflow:hidden;background:#05070a;box-shadow:0 18px 34px rgba(0,0,0,.22)}
  #chart{position:absolute;inset:0;z-index:1}
  #overlay{position:absolute;inset:0;z-index:2;pointer-events:none}
  .side{border-left:1px solid #202a39;background:#070b11;min-width:0}
  .card{border-bottom:1px solid #182130;padding:13px 14px}
  .h{font-family:var(--mono);font-size:10px;color:#8793a5;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px}
  .phasegrid{display:grid;grid-template-columns:repeat(5,1fr);gap:6px}
  .phase{font-family:var(--mono);font-size:10px;text-align:center;padding:7px 4px;border-radius:4px;border:1px solid #263244;color:#60718a;background:#0b1018}
  .phase.on{border-color:#d7a63c;color:#ffd66e;background:#241a06}
  .phase.off.on{border-color:#813040;color:#ff6b78;background:#2a1117}
  .kv{display:grid;grid-template-columns:116px minmax(0,1fr);gap:6px 10px;font-family:var(--mono);font-size:12px}
  .kv .k{color:#6f7b8d}.kv .v{color:#edf3fb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .good{color:var(--green)!important}.bad{color:var(--red)!important}.warn{color:var(--amber)!important}
  .setup{font-family:var(--mono);font-size:12px;line-height:1.65;color:#cdd6e3}
  .setup b{color:#fff}.setup .long{color:var(--green)}.setup .short{color:var(--red)}
  .concept{display:grid;grid-template-columns:1fr auto;gap:8px;font-family:var(--mono);font-size:11px;padding:5px 0;border-bottom:1px solid #101722}
  .concept:last-child{border-bottom:0}.concept span:first-child{color:#cdd6e3}.concept span:last-child{color:#8793a5}
  .log{max-height:210px;overflow:auto;font-family:var(--mono);font-size:11px}
  .ln{display:grid;grid-template-columns:72px minmax(0,1fr);gap:8px;padding:5px 0;border-bottom:1px solid #101722}
  .ln .t{color:#60718a}.ln.info .m{color:#cdd6e3}.ln.open .m{color:var(--amber)}
  .ln.win .m{color:var(--green)}.ln.loss .m,.ln.skip .m{color:var(--red)}
  .levels{display:grid;gap:6px;font-family:var(--mono);font-size:11px}
  .level{display:grid;grid-template-columns:1fr auto;gap:8px;padding:5px 7px;border:1px solid #182130;border-radius:4px;background:#0a0f16}
  .level .name{color:#cdd6e3}.level .px{color:#f2b84b;font-variant-numeric:tabular-nums}
  .note{font-family:var(--mono);font-size:11px;line-height:1.55;color:#7c8798}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ttl">ICT<span class="dot">.</span>Lab</span>
    <a class="nav" href="/">&larr; Chart</a>
    <a class="nav" href="/paper">Paper Trading</a>
    <a class="nav" href="/journal">Journal</a>
    <select id="interval" onchange="reload(true)">
      <option value="1m" selected>1m</option>
      <option value="3m">3m</option>
      <option value="5m">5m</option>
      <option value="15m">15m</option>
      <option value="30m">30m</option>
      <option value="1h">1h</option>
    </select>
    <button class="btn" id="toggle" onclick="toggleBot()">...</button>
    <button class="btn reset" onclick="resetBot()">Reset log</button>
    <span class="spacer"></span>
    <span class="status" id="status">connecting...</span>
  </div>

  <div class="shell">
    <main class="chartarea">
      <div id="chartbox">
        <div id="chart"></div>
        <canvas id="overlay"></canvas>
      </div>
    </main>
    <aside class="side">
      <div class="card">
        <div class="h">Bot phase</div>
        <div class="phasegrid" id="phasegrid"></div>
      </div>
      <div class="card">
        <div class="h">Current read</div>
        <div class="kv">
          <div class="k">Symbol</div><div class="v" id="sym">BTCUSDT</div>
          <div class="k">Price</div><div class="v" id="price">-</div>
          <div class="k">Killzone</div><div class="v" id="kz">-</div>
          <div class="k">Bias</div><div class="v" id="bias">-</div>
          <div class="k">Event</div><div class="v" id="event">-</div>
        </div>
      </div>
      <div class="card">
        <div class="h">Active setup</div>
        <div class="setup" id="setup">No setup yet.</div>
      </div>
      <div class="card">
        <div class="h">Watched concepts</div>
        <div id="concepts"></div>
      </div>
      <div class="card">
        <div class="h">Levels on chart</div>
        <div class="levels" id="levels"></div>
      </div>
      <div class="card">
        <div class="h">Discovery log</div>
        <div class="log" id="log"></div>
      </div>
      <div class="card">
        <div class="note">Visual only. It does not place orders. It redraws from fresh candles every ~2 seconds and shows the same ICT chain: liquidity pool -> sweep -> MSS/displacement -> FVG/OB -> entry/stop/target.</div>
      </div>
    </aside>
  </div>

<script>
const $ = id => document.getElementById(id);
const phases = ['SCAN','SWEPT','ARMED','PENDING','FILLED'];
let chart, series, chartBox, canvas, ctx, state=null, priceLines=[], firstLoad=true, busy=false;

function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function money(x){ if(x==null || isNaN(x)) return '-'; return '$'+Number(x).toLocaleString(undefined,{maximumFractionDigits:2}); }
function timeText(t){ try{return new Date(t*1000).toLocaleTimeString();}catch(e){return '';} }

function init(){
  chartBox = $('chartbox'); canvas = $('overlay'); ctx = canvas.getContext('2d');
  chart = LightweightCharts.createChart($('chart'), {
    layout:{background:{type:'solid',color:'#05070a'},textColor:'#9aa6b8'},
    grid:{vertLines:{color:'#101722'},horzLines:{color:'#101722'}},
    rightPriceScale:{borderColor:'#263244'}, timeScale:{borderColor:'#263244',timeVisible:true,secondsVisible:false},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    handleScale:true, handleScroll:true
  });
  series = chart.addCandlestickSeries({
    upColor:'#19c37d',downColor:'#ff4d5f',borderVisible:false,wickUpColor:'#19c37d',wickDownColor:'#ff4d5f'
  });
  new ResizeObserver(resize).observe(chartBox);
  chart.timeScale().subscribeVisibleTimeRangeChange(drawOverlay);
  phaseGrid('OFF');
  resize();
  reload(true);
  setInterval(()=>reload(false), 2200);
}

function resize(){
  const r = chartBox.getBoundingClientRect();
  chart.applyOptions({width:Math.max(200, r.width), height:Math.max(300, r.height)});
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(r.width*dpr));
  canvas.height = Math.max(1, Math.floor(r.height*dpr));
  canvas.style.width = r.width+'px'; canvas.style.height = r.height+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  drawOverlay();
}

async function reload(forceFit){
  if(busy) return; busy=true;
  const intv = $('interval').value;
  const lim = intv==='1m' ? 1200 : 900;
  try{
    const r = await fetch('/api/ictlab/state?interval='+encodeURIComponent(intv)+'&limit='+lim, {cache:'no-store'});
    const s = await r.json(); state = s;
    if(!s.ok) throw new Error(s.error || 'ICT API failed');
    series.setData(s.candles || []);
    clearLines();
    for(const l of (s.levels||[])) addLine(l);
    series.setMarkers((s.markers||[]).map(m=>({
      time:m.time, position:m.position||'aboveBar', color:m.color||'#f2b84b',
      shape:m.shape||'circle', text:m.text||''
    })));
    renderState(s);
    if(firstLoad || forceFit){ chart.timeScale().fitContent(); firstLoad=false; }
    requestAnimationFrame(drawOverlay);
    $('status').textContent = 'live - '+new Date().toLocaleTimeString();
  }catch(e){
    $('status').textContent = 'error: '+e.message;
  }finally{ busy=false; }
}

function clearLines(){
  for(const l of priceLines){ try{ series.removePriceLine(l); }catch(e){} }
  priceLines = [];
}

function lineStyle(kind){
  const L = LightweightCharts.LineStyle;
  if(kind==='entry' || kind==='target' || kind==='stop') return L.Solid;
  if(kind==='eq' || kind==='mss') return L.Dotted;
  return L.Dashed;
}

function lineColor(l){
  if(l.kind==='entry') return '#f2b84b';
  if(l.kind==='stop') return '#ff4d5f';
  if(l.kind==='target') return '#19c37d';
  if(l.kind==='mss') return '#a78bfa';
  if(l.kind==='eq') return '#8793a5';
  return l.color || (l.side>0 ? '#d8a84a' : '#4fb7ff');
}

function addLine(l){
  if(l.price==null) return;
  try{
    priceLines.push(series.createPriceLine({
      price:Number(l.price), color:lineColor(l), lineWidth:(l.kind==='entry'||l.kind==='stop'||l.kind==='target')?2:1,
      lineStyle:lineStyle(l.kind), axisLabelVisible:true, title:(l.name||l.kind||'level')+' '+Number(l.price).toFixed(1)
    }));
  }catch(e){}
}

function drawOverlay(){
  if(!ctx || !state) return;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.clearRect(0,0,w,h);
  for(const z of (state.zones||[])){
    const x1 = chart.timeScale().timeToCoordinate(z.time1);
    const x2 = chart.timeScale().timeToCoordinate(z.time2);
    if(x1==null || x2==null) continue;
    if(z.kind === 'killzone'){
      ctx.fillStyle = z.name==='NY AM' ? 'rgba(242,184,75,.055)' : (z.name==='London' ? 'rgba(58,160,255,.045)' : 'rgba(167,139,250,.045)');
      ctx.fillRect(Math.min(x1,x2),0,Math.abs(x2-x1)+2,h);
      ctx.fillStyle = 'rgba(180,190,210,.42)';
      ctx.font = '10px IBM Plex Mono, monospace';
      ctx.fillText(z.name, Math.min(x1,x2)+5, 16);
      continue;
    }
    const yTop = series.priceToCoordinate(Number(z.top));
    const yBot = series.priceToCoordinate(Number(z.bottom));
    if(yTop==null || yBot==null) continue;
    const x = Math.min(x1,x2), y = Math.min(yTop,yBot), rw = Math.max(8, Math.abs(x2-x1)), rh = Math.max(4, Math.abs(yBot-yTop));
    const isLong = z.side === 'long';
    ctx.fillStyle = z.kind==='fvg' ? (isLong?'rgba(25,195,125,.13)':'rgba(255,77,95,.13)') : 'rgba(242,184,75,.11)';
    ctx.strokeStyle = z.kind==='fvg' ? (isLong?'rgba(25,195,125,.7)':'rgba(255,77,95,.7)') : 'rgba(242,184,75,.68)';
    ctx.lineWidth = 1;
    ctx.fillRect(x,y,rw,rh); ctx.strokeRect(x+.5,y+.5,rw,rh);
    ctx.fillStyle = z.kind==='fvg' ? (isLong?'#76f1b1':'#ff8793') : '#ffd66e';
    ctx.font = '10px IBM Plex Mono, monospace';
    ctx.fillText((z.name||z.kind).toUpperCase(), x+5, Math.max(12,y-4));
  }
}

function phaseGrid(active){
  $('phasegrid').innerHTML = phases.map(p => '<div class="phase '+(active===p?'on':'')+'">'+p+'</div>').join('');
  if(active==='OFF') $('phasegrid').innerHTML = '<div class="phase off on" style="grid-column:1/-1">OFF - bot paused</div>';
}

function renderState(s){
  $('toggle').textContent = s.enabled ? 'Pause visual bot' : 'Enable visual bot';
  $('toggle').className = 'btn '+(s.enabled?'pause':'run');
  phaseGrid(s.phase || 'SCAN');
  $('sym').textContent = s.symbol || 'BTCUSDT';
  $('price').textContent = money(s.price);
  $('kz').textContent = s.killzone || 'outside';
  $('bias').textContent = s.bias || '-';
  $('bias').className = 'v '+(s.bias==='LONG'?'good':(s.bias==='SHORT'?'bad':''));
  $('event').textContent = s.event || '-';
  renderSetup(s.setup, s.phase);
  $('concepts').innerHTML = (s.concepts||[]).map(c =>
    '<div class="concept"><span>'+esc(c.name)+'</span><span>'+esc(c.state)+'</span></div>'
  ).join('') || '<div class="note">No concept data yet.</div>';
  const lv = (s.levels||[]).filter(l=>l.price!=null).slice(0,18);
  $('levels').innerHTML = lv.map(l =>
    '<div class="level"><span class="name">'+esc(l.name||l.kind)+'</span><span class="px">'+money(l.price)+'</span></div>'
  ).join('') || '<div class="note">No levels yet.</div>';
  $('log').innerHTML = (s.log||[]).map(e =>
    '<div class="ln '+esc(e.kind||'info')+'"><span class="t">'+timeText(e.t)+'</span><span class="m">'+esc(e.msg)+'</span></div>'
  ).join('') || '<div class="note">Nothing discovered yet.</div>';
}

function renderSetup(p, phase){
  if(!p){ $('setup').innerHTML = phase==='OFF' ? 'Paused. Enable it to scan live candles.' : 'No setup yet.'; return; }
  const cls = p.side === 'long' ? 'long' : 'short';
  let h = '<b class="'+cls+'">'+String(p.side||'').toUpperCase()+'</b> from <b>'+esc(p.pool)+'</b><br>';
  h += 'sweep pool '+money(p.pool_price)+' / raid '+money(p.raid)+'<br>';
  if(p.mss) h += 'MSS '+money(p.mss)+'<br>';
  if(p.entry) h += 'entry '+money(p.entry)+' via '+esc(p.entry_model)+'<br>';
  if(p.stop) h += 'stop '+money(p.stop)+'<br>';
  if(p.target) h += 'target '+money(p.target)+'<br>';
  h += 'score <b>'+esc(p.score)+'</b>';
  if(p.rr) h += ' / R:R <b>'+esc(p.rr)+'</b>';
  if(p.premium_discount_ok===false) h += '<br><span class="bad">premium/discount reject</span>';
  if(p.rejected) h += '<br><span class="bad">not tradable</span>';
  else if(p.tradable) h += '<br><span class="good">tradable visual setup</span>';
  $('setup').innerHTML = h;
}

async function toggleBot(){
  const btn = $('toggle');
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = 'Switching...';
  try{
    let enabled = state && typeof state.enabled === 'boolean' ? state.enabled : null;
    if(enabled === null){
      const cur = await (await fetch('/api/ictlab/state?interval='+encodeURIComponent($('interval').value)+'&limit=120', {cache:'no-store'})).json();
      enabled = !!cur.enabled;
    }
    const want = !enabled;
    const r = await fetch('/api/ictlab/toggle',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:want})
    });
    const d = await r.json();
    if(!d.ok) throw new Error(d.error || 'toggle failed');
    state = Object.assign({}, state || {}, {enabled:!!d.enabled, phase:d.enabled?'SCAN':'OFF'});
    renderState(state);
    await reload(true);
  }catch(e){
    $('status').textContent = 'toggle error: '+e.message;
    btn.textContent = oldText;
  }finally{
    btn.disabled = false;
  }
}

async function resetBot(){
  await fetch('/api/ictlab/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  reload(false);
}

window.addEventListener('load', init);
</script>
</body>
</html>
"""


# ---- Paper Trading page (runs multiple paper strategies on one page) -------
async def _paper_page(request: web.Request):
    # no-cache so a normal F5 always fetches the latest page (otherwise the browser
    # serves a stale cached /paper and you never see code changes).
    return web.Response(text=PAPER_HTML, content_type="text/html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                                 "Pragma": "no-cache", "Expires": "0"})


# ---- ICT Lab page: live visual ICT scanner, no orders ---------------------
async def _ictlab_page(request: web.Request):
    return web.Response(text=ICTLAB_HTML, content_type="text/html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                                 "Pragma": "no-cache", "Expires": "0"})


async def _ictlab_state(request: web.Request):
    interval = request.query.get("interval", "1m")
    if interval not in ("1m", "3m", "5m", "15m", "30m", "1h"):
        interval = "1m"
    try:
        limit = int(request.query.get("limit", "1200" if interval == "1m" else "900"))
    except ValueError:
        limit = 1200 if interval == "1m" else 900
    try:
        candles = await _fetch_binance_candles(interval, limit)
        state = ICTLAB.state(candles, _btc_price())
        state["interval"] = interval
        state["symbol"] = CHART_SYMBOL
        return web.json_response(state)
    except Exception as e:
        return web.json_response({"ok": False, "enabled": ICTLAB.enabled,
                                  "phase": "SCAN", "error": f"{type(e).__name__}: {e}",
                                  "candles": [], "levels": [], "zones": [], "markers": []})


async def _ictlab_toggle(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return web.json_response(ICTLAB.set_enabled(body.get("enabled", not ICTLAB.enabled)))


async def _ictlab_reset(request: web.Request):
    return web.json_response(ICTLAB.reset())


# ---- stocks page (Alpaca-backed AI news bot) ------------------------------
async def _stock_page(request: web.Request):
    return web.Response(text=STOCK_PAGE_HTML, content_type="text/html")


async def _stock_symbols(request: web.Request):
    return web.json_response({"symbols": config.STOCK_SYMBOLS})


async def _stock_candles(request: web.Request):
    sym = (request.query.get("symbol") or "AAPL").upper()
    interval = request.query.get("interval", "5m")
    if sym not in config.STOCK_SYMBOLS:
        return web.json_response({"candles": [], "error": "unknown symbol"})
    if _STOCK_MARKET is None:
        return web.json_response({"candles": [], "error": "stock feed off"})
    candles = await _STOCK_MARKET.candles(sym, interval, 300)
    err = "" if candles else (getattr(_STOCK_MARKET, "candle_error", "")
                              or ("no Alpaca key" if not getattr(_STOCK_MARKET, "ok", False)
                                  else "no data (market closed?)"))
    return web.json_response({"candles": candles, "symbol": sym, "error": err})


async def _stock_news(request: web.Request):
    if _STOCK_BOT is None:
        return web.json_response({"news": []})
    return web.json_response({"news": _STOCK_BOT.news_events[:300]})


async def _stock_bot_state(request: web.Request):
    if _STOCK_BOT is None:
        return web.json_response({"running": False, "positions": [], "history": [], "log": []})
    s = _STOCK_BOT.state(); s["running"] = True
    return web.json_response(s)


async def _stock_bot_toggle(request: web.Request):
    if _STOCK_BOT is None:
        return web.json_response({"error": "bot not running"})
    b = await request.json()
    return web.json_response(_STOCK_BOT.set_enabled(b.get("enabled", not _STOCK_BOT.enabled)))


async def _stock_bot_reset(request: web.Request):
    if _STOCK_BOT is None:
        return web.json_response({"error": "bot not running"})
    return web.json_response(_STOCK_BOT.reset())


async def _bot_reset(request: web.Request):
    """Reset the crypto NEWS bot's paper account (balance + positions + trade
    history). Leaves the news log so the chart's news dots are unaffected."""
    if _BROKER is None:
        return web.json_response({"error": "broker not running"})
    return web.json_response({"ok": bool(_BROKER.reset())})


# ---- journal / performance + order-flow overlays -------------------------
_INT_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "2h": 7200,
            "4h": 14400, "1d": 86400, "1w": 604800, "1M": 2592000}


async def _journal_page(request: web.Request):
    return web.Response(text=JOURNAL_PAGE_HTML, content_type="text/html")


async def _journal(request: web.Request):
    acct = request.query.get("account", "all")
    return web.json_response(journal.stats(acct))


_CVD_CACHE = {"interval": None, "at": 0.0, "data": None}


async def _cvd(request: web.Request):
    """Cumulative volume delta: running sum of (buy USD - sell USD), bucketed by
    the chart interval. The shape (and divergence vs price) is what matters.
    Cached ~2s — it's a slow cumulative line, so this avoids scanning the whole
    tape every second when CVD is on."""
    import time as _t
    interval = request.query.get("interval", "1m")
    now = _t.time()
    c = _CVD_CACHE
    if c["interval"] == interval and c["data"] is not None and (now - c["at"]) < 2.0:
        return web.json_response(c["data"])
    sec = _INT_SEC.get(interval, 60)
    buckets = {}
    for t in list(WHALES.tape):
        b = int((t["ts"] // 1000) // sec * sec)
        buckets[b] = buckets.get(b, 0) + (t["usd"] if t["side"] == "buy" else -t["usd"])
    out, run = [], 0
    for tt in sorted(buckets):
        run += buckets[tt]
        out.append({"time": tt, "value": round(run)})
    data = {"cvd": out, "min_usd": WHALES.min_usd}
    c["interval"], c["at"], c["data"] = interval, now, data
    return web.json_response(data)


async def _bigtrades(request: web.Request):
    """Individual large trades (>= BIG_TRADE_USD) to mark on the price chart."""
    interval = request.query.get("interval", "1m")
    sec = _INT_SEC.get(interval, 60)
    try:
        min_usd = int(request.query.get("min", str(config.BIG_TRADE_USD)))
    except ValueError:
        min_usd = config.BIG_TRADE_USD
    rows = []
    for t in reversed(WHALES.tape):            # newest first; stop once we have enough
        if t["usd"] >= min_usd:
            rows.append(t)
            if len(rows) >= 400:
                break
    out = [{"time": int((t["ts"] // 1000) // sec * sec), "side": t["side"],
            "usd": t["usd"], "price": t["price"]} for t in rows]
    return web.json_response({"trades": out, "min_usd": min_usd})


async def _copy_page(request: web.Request):
    return web.Response(text=COPYTRADE_HTML, content_type="text/html")


async def _copy_state(request: web.Request):
    return web.json_response(COPY.snapshot())


async def _copy_reset(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    COPY.reset(body.get("addr"))      # addr -> reset one; none -> reset all
    return web.json_response({"ok": True})


async def _funding_page(request: web.Request):
    return web.Response(text=FUNDING_HTML, content_type="text/html")


async def _funding_state(request: web.Request):
    return web.json_response(FUNDING.state())


async def _funding_toggle(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return web.json_response(FUNDING.set_enabled(bool(body.get("enabled"))))


async def _funding_reset(request: web.Request):
    return web.json_response(FUNDING.reset())


PAPER_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trading · multi-strategy</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{--bg:#08090c;--panel:#0f1116;--panel2:#15181f;--line:#1c202a;--line2:#252a36;
    --txt:#d7dbe4;--muted:#6f7787;--amber:#f5c518;--green:#22c55e;--red:#f04452;--blue:#3b82f6;
    --bin:'IBM Plex Mono',ui-monospace,monospace;--sans:'IBM Plex Sans',system-ui,sans-serif;}
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px}
  #topbar{display:flex;align-items:center;gap:14px;padding:13px 20px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#13161d,#0f1116);flex-wrap:wrap}
  .ticker{font-family:var(--bin);font-weight:600;font-size:18px;letter-spacing:.5px}
  .ticker .dot{color:var(--amber)}
  .nav{font-family:var(--bin);font-size:13px;color:var(--amber);text-decoration:none;border:1px solid var(--line2);padding:6px 11px;border-radius:7px}
  .nav:hover{background:var(--panel2)}
  .spacer{flex:1}
  .sub{font-family:var(--bin);font-size:11.5px;color:var(--muted)}
  #wrap{max-width:1500px;margin:0 auto;padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:1100px){#wrap{grid-template-columns:1fr}}

  /* AI Penny Stock Desk link card - animation is transform/opacity only so the
     compositor handles it; no JS timers, no repaint of surrounding panels. */
  .goldcard{grid-column:1/-1;display:flex;align-items:center;gap:16px;
    padding:16px 20px;border-radius:14px;text-decoration:none;color:#f7e6b0;
    background:linear-gradient(110deg,#1a1608 0%,#241d09 45%,#1a1608 100%);
    border:1px solid #5c4a12;position:relative;overflow:hidden;
    box-shadow:0 4px 18px rgba(0,0,0,.45);
    transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
  .goldcard:hover{transform:translateY(-2px);border-color:#a4831f;
    box-shadow:0 8px 26px rgba(245,197,24,.16)}
  .goldcard::after{content:"";position:absolute;top:0;left:-60%;width:45%;height:100%;
    background:linear-gradient(90deg,transparent,rgba(255,221,107,.14),transparent);
    transform:skewX(-18deg);animation:gcSheen 6s ease-in-out infinite;
    will-change:transform;pointer-events:none}
  @keyframes gcSheen{0%,72%{transform:translateX(0) skewX(-18deg)}
    100%{transform:translateX(360%) skewX(-18deg)}}
  @media (prefers-reduced-motion:reduce){.goldcard::after{animation:none}}
  .gc-icon{font-size:26px;color:#f5c518;text-shadow:0 0 14px rgba(245,197,24,.5)}
  .gc-txt{display:flex;flex-direction:column;line-height:1.35}
  .gc-txt b{font-size:15px;color:#ffdf87;letter-spacing:.2px}
  .gc-txt em{font-style:normal;font-size:11.5px;color:#9c8c5e}
  .gc-go{margin-left:auto;padding:7px 15px;border-radius:9px;font-size:12px;
    font-weight:700;color:#1a1608;background:linear-gradient(90deg,#f5c518,#ffdd6b)}
  .bot{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
  /* per-bot "up since reset" timer: a footer driven by --uptime on the panel's own style,
     so panel innerHTML re-renders can NEVER wipe it (no flicker possible). */
  .bot::after{content:var(--uptime);display:block;text-align:right;padding:3px 12px;font-size:10px;color:#7fb5ff;background:#0e1626;border-top:1px solid var(--line);letter-spacing:.3px}
  .bhead{display:flex;align-items:center;gap:10px;padding:12px 15px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .bname{font-family:var(--bin);font-weight:600;font-size:14px}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block}
  .dot.on{background:var(--green);box-shadow:0 0 7px var(--green)} .dot.off{background:var(--red)}
  .dot.watch{background:var(--amber);box-shadow:0 0 7px var(--amber)}
  .badge{font-family:var(--bin);font-size:10.5px;padding:3px 8px;border-radius:6px;border:1px solid var(--line2);color:var(--muted)}
  .badge.live{color:var(--amber);border-color:var(--amber)}
  .btn{font-family:var(--bin);font-size:12px;border-radius:7px;cursor:pointer;padding:6px 12px;border:1px solid var(--line2);background:transparent;color:var(--txt)}
  .btn.on{background:#103024;border-color:#1c5;color:var(--green)} .btn.off{background:#2e1216;border-color:#722;color:var(--red)}
  .btn.reset{color:var(--muted)} .btn.reset:hover{border-color:var(--red);color:var(--red)}
  select{font-family:var(--bin);font-size:12px;background:var(--panel2);color:var(--txt);border:1px solid var(--line2);border-radius:7px;padding:5px 8px}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border-bottom:1px solid var(--line)}
  .stat{background:var(--panel);padding:10px 12px}
  .stat .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
  .stat .v{font-family:var(--bin);font-size:16px;font-weight:600;margin-top:3px}
  .pos{color:var(--green)} .neg{color:var(--red)}
  .ph{font-family:var(--bin);font-size:10.5px;font-weight:600;letter-spacing:.4px;color:var(--muted);text-transform:uppercase;padding:9px 14px 7px;border-bottom:1px solid var(--line);border-top:1px solid var(--line)}
  .posrow{padding:9px 14px;border-bottom:1px solid #12151c;font-family:var(--bin);font-size:12px}
  .posrow .top{display:flex;justify-content:space-between;align-items:center}
  .side{font-size:10px;padding:2px 7px;border-radius:5px;font-weight:600}
  .side.long{background:#103024;color:var(--green)} .side.short{background:#2e1216;color:var(--red)}
  .det{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin-top:5px}
  .empty{color:var(--muted);font-size:12px;font-family:var(--bin);padding:14px}
  .feed{max-height:230px;overflow:auto}
  .ln{display:grid;grid-template-columns:62px 1fr;gap:8px;font-family:var(--bin);font-size:11px;padding:4px 14px;border-bottom:1px solid #11141a}
  .ln .lt{color:var(--muted)}
  .open b,.win b{color:var(--green)} .loss b{color:var(--red)} .skip b{color:var(--muted)} .error b{color:var(--red)}
  .open span.v,.win span.v{color:var(--green)} .loss span.v{color:var(--red)}
  .hist{max-height:210px;overflow:auto}

  /* Professional dark trading-terminal skin (design only) */
  :root{
    --bg:#05070a;--panel:#0a0d12;--panel2:#10151d;--line:#1d2633;--line2:#2b3748;
    --txt:#edf3fb;--muted:#7c8798;--amber:#f2b84b;--green:#19c37d;--red:#ff4d5f;--blue:#3aa0ff;
  }
  html,body{background:#05070a;color:var(--txt);font-size:13px}
  body{background:linear-gradient(180deg,#05070a 0%,#070a0f 52%,#05070a 100%)}
  #topbar{
    position:sticky;top:0;z-index:20;padding:10px 18px;background:#070a0f;
    border-bottom:1px solid #263244;box-shadow:0 12px 28px rgba(0,0,0,.28)
  }
  .ticker{font-size:16px;text-transform:uppercase;letter-spacing:.12em;color:#f5f8fc}
  .nav,.btn,select{
    border-radius:4px;background:#0e131b;border-color:#263244;color:#cdd6e3;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.025)
  }
  .nav:hover,.btn:hover,select:hover{background:#121925;border-color:#3a4658;color:#fff}
  #wrap{max-width:none;padding:18px;grid-template-columns:repeat(3,minmax(300px,1fr));gap:12px}
  @media(max-width:1320px){#wrap{grid-template-columns:repeat(2,minmax(300px,1fr))}}
  @media(max-width:900px){#wrap{grid-template-columns:1fr;padding:12px}}
  .bot{
    border-radius:6px;border:1px solid #202a39;background:#090d13;
    box-shadow:0 10px 24px rgba(0,0,0,.18);min-width:0
  }
  /* Keep the Cryptal maker collector unmistakable at the top of Paper Trading.
     Two background layers leave the panel opaque while only its border moves. */
  .cryptal-featured{
    grid-column:1/-1!important;border:3px solid transparent!important;
    background:
      linear-gradient(#090d13,#090d13) padding-box,
      linear-gradient(110deg,#14b8a6,#22d3ee,#3b82f6,#a855f7,#f59e0b,#14b8a6) border-box!important;
    background-size:100% 100%,300% 300%!important;
    animation:cryptalBorderFlow 4s linear infinite,cryptalHalo 2.2s ease-in-out infinite alternate;
    box-shadow:0 0 22px rgba(20,184,166,.38),0 14px 34px rgba(0,0,0,.32)!important
  }
  .cryptal-featured .bhead{
    background:linear-gradient(90deg,rgba(8,63,68,.72),#0b111a 45%,rgba(45,20,75,.58));
    border-bottom-color:#1d8f92
  }
  .cryptal-new-badge{
    color:#071315!important;background:linear-gradient(90deg,#5eead4,#67e8f9)!important;
    border-color:#99f6e4!important;font-weight:700;letter-spacing:.08em;
    animation:cryptalBadgePulse 1.6s ease-in-out infinite alternate
  }
  @keyframes cryptalBorderFlow{
    0%{background-position:0 0,0% 50%}
    50%{background-position:0 0,100% 50%}
    100%{background-position:0 0,0% 50%}
  }
  @keyframes cryptalHalo{
    from{box-shadow:0 0 13px rgba(20,184,166,.3),0 14px 34px rgba(0,0,0,.32)}
    to{box-shadow:0 0 30px rgba(34,211,238,.62),0 14px 34px rgba(0,0,0,.32)}
  }
  @keyframes cryptalBadgePulse{
    from{opacity:.76;transform:scale(.98)}
    to{opacity:1;transform:scale(1.04)}
  }
  @media (prefers-reduced-motion:reduce){
    .cryptal-featured,.cryptal-new-badge{animation:none}
  }
  .bot::after{background:#070b11;color:#60718a;border-top:1px solid #182130;padding:4px 12px}
  .bhead{
    min-height:44px;padding:10px 12px;background:linear-gradient(180deg,#0f141d,#0a0e15);
    border-bottom:1px solid #202a39
  }
  .bname{text-transform:uppercase;letter-spacing:.05em;font-size:12px;color:#f5f8fc}
  .badge{border-radius:4px;background:#0d121a}
  .btn{padding:5px 10px;font-size:11px}
  .btn.on{background:#0b281d;border-color:#1d8c62;color:#42e49b}
  .btn.off{background:#2a1117;border-color:#813040;color:#ff6b78}
  .btn.reset:hover{background:#241017}
  .stats{background:#202a39;border-color:#202a39}
  .stat{background:#080c12;padding:10px 11px}
  .stat .k{font-size:9px;letter-spacing:.08em;color:#6f7b8d}
  .stat .v{font-size:15px;color:#edf3fb;font-variant-numeric:tabular-nums}
  .stat .v.pos,.tp-stat .v.pos,.posrow .pos,.det .pos{color:var(--green)}
  .stat .v.neg,.tp-stat .v.neg,.posrow .neg,.det .neg{color:var(--red)}
  .ph{
    background:#070b11;color:#8793a5;border-color:#1a2331;padding:8px 12px;
    letter-spacing:.09em
  }
  .posrow{border-bottom:1px solid #121a26;background:#090d13;padding:8px 12px}
  .posrow:hover{background:#0d131d}
  .det{color:#8793a5}
  .side{border-radius:4px;letter-spacing:.06em}
  .side.long{background:#092419;color:#3ee797}.side.short{background:#2a1017;color:#ff6b78}
  .empty{color:#6f7b8d}
  .feed,.hist{scrollbar-color:#344155 #080c12}
  .ln{border-bottom:1px solid #121a26;grid-template-columns:70px 1fr}
  iframe{filter:saturate(.92) contrast(1.02)}
  .pinbtn{
    width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;
    border-radius:4px;border:1px solid #263244;background:#0b1018;color:#7c8798;
    font-size:13px;line-height:1;cursor:pointer;padding:0
  }
  .pinbtn:hover{border-color:#b88932;color:#f2b84b;background:#151104}
  .pinbtn.on{border-color:#d7a63c;color:#ffd66e;background:#241a06}
  .bot.pinned{
    background:linear-gradient(180deg,#1a1304 0%,#0f0d08 48%,#090d13 100%);
    border-color:#d7a63c!important;box-shadow:0 0 0 1px rgba(215,166,60,.35),0 18px 34px rgba(0,0,0,.26)
  }
  .bot.pinned .bhead{background:linear-gradient(180deg,#2a1c04,#121006);border-bottom-color:#8f6b2b}
  .bot.pinned .stat,.bot.pinned .posrow{background:rgba(16,13,7,.82)}
  .bot.pinned .ph{background:#120f07;border-color:#4e3a17;color:#d7b76c}
  .poly-closed{max-height:260px;overflow:auto;background:#070b11}
  .poly-row,.poly-head{
    display:grid;grid-template-columns:72px 78px 92px 86px 118px minmax(180px,1fr);
    gap:8px;align-items:center;padding:7px 12px;border-bottom:1px solid #121a26;
    font-family:var(--bin);font-size:11px
  }
  .poly-head{
    position:sticky;top:0;z-index:1;background:#0b1018;color:#8793a5;
    text-transform:uppercase;letter-spacing:.08em;font-size:9px
  }
  .poly-row:hover{background:#0d131d}
  .poly-title{color:#8793a5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .poly-num{font-variant-numeric:tabular-nums}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ticker">Paper<span class="dot">·</span>Trading</span>
    <a class="nav" href="/">&larr; Chart</a>
    <a class="nav" href="/ict">ICT Lab →</a>
    <a class="nav" href="/funding">Funding Bot →</a>
    <a class="nav" href="/lucid-lab" style="color:#f4bd4a;border-color:#6b4d19">Lucid Strategy Lab →</a>
    <a class="nav" href="http://127.0.0.1:8100" target="_blank" rel="noopener">Research Bot ↗</a>
    <span class="sub">multiple paper strategies, one page · simulated fills on real Lighter data · no real money</span>
    <span class="spacer"></span>
    <span class="sub" id="px">BTC —</span>
  </div>
  <div id="wrap">
    <div class="bot cryptal-featured" id="cryptalmaker-panel"></div>
    <div class="bot cryptal-featured" id="cryptalgelmaker-panel"></div>
    <div class="bot cryptal-featured" id="cryptalgeo-panel"></div>
    <div class="bot pinned" id="lucidcont-panel" style="grid-column:1/-1;border:3px solid #22c55e;box-shadow:0 0 18px rgba(34,197,94,.55)"></div>
    <div class="bot pinned" id="lucidpass-panel" style="grid-column:1/-1;border:3px solid #facc15;box-shadow:0 0 18px rgba(250,204,21,.6)"></div>
    <div class="bot pinned" id="nqmr15-panel" style="grid-column:1/-1;border:3px solid #fbbf24;box-shadow:0 0 16px rgba(251,191,36,.55)"></div>
    <div class="bot pinned" id="nr7-panel" style="grid-column:1/-1;border:3px solid #10b981;box-shadow:0 0 14px rgba(16,185,129,.55)"></div>
    <div class="bot pinned" id="nr7aggr-panel" style="grid-column:1/-1;border:3px solid #f59e0b;box-shadow:0 0 14px rgba(245,158,11,.5)"></div>
    <div class="bot pinned" id="apexvwap-panel" style="grid-column:1/-1;border:3px solid #38bdf8;box-shadow:0 0 10px rgba(56,189,248,.35)"></div>
    <div class="bot" id="research-panel" style="grid-column:1/-1;border-color:#2a5a5a">
      <div class="bhead"><span class="dot on"></span><span class="bname">🧪 Strategy Research Bot — BTC/ETH/SOL · backtest · paper · live</span>
        <span class="spacer"></span>
        <a class="btn" href="http://127.0.0.1:8100" target="_blank" rel="noopener" style="text-decoration:none">Open full dashboard ↗</a></div>
      <div class="sub" style="padding:7px 14px">Separate rule-based FastAPI research tool (HTF trend filter + RSI/MACD momentum + ATR risk, fully backtestable). Start it once with
        <code style="background:#11151c;padding:2px 6px;border-radius:4px">cd crypto-trading-bot &amp;&amp; uvicorn backend.main:app --port 8100</code>
        then it loads below. Live trading is OFF by default.</div>
      <iframe src="http://127.0.0.1:8100" title="Strategy Research Bot" loading="lazy"
        style="width:100%;height:680px;border:0;border-top:1px solid #232838;background:#0b0d12"></iframe>
    </div>
    <div class="bot" id="news-panel" style="grid-column:1/-1;border-color:#2a4a6a"></div>
    <div class="bot" id="sniper-panel" style="grid-column:1/-1;border-color:#2a6a4a"></div>
    <div class="bot" id="arb-panel" style="grid-column:1/-1;border-color:#5a4a2a"></div>
    <div class="bot" id="fv-panel" style="grid-column:1/-1;border-color:#2a5a5a"></div>
    <div class="bot" id="trend-panel" style="grid-column:1/-1;border-color:#3a5a2a"></div>
    <div class="bot" id="ainews-panel" style="grid-column:1/-1;border-color:#5a2a5a"></div>
    <div class="bot" id="claudehaiku-panel" style="grid-column:1/-1;border-color:#8b5cf6"></div>
    <div class="bot" id="tvstrats-panel" style="grid-column:1/-1;border:3px solid #f5d020;box-shadow:0 0 10px rgba(245,208,32,.35)"></div>
    <div class="bot" id="meanrev-panel" style="grid-column:1/-1;border:3px solid #ef4444;box-shadow:0 0 10px rgba(239,68,68,.4)"></div>
    <div class="bot" id="newsmomo-panel" style="grid-column:1/-1;border:3px solid #ff8c1a;box-shadow:0 0 10px rgba(255,140,26,.4)"></div>
    <div class="bot" id="rsi2noatr-panel" style="grid-column:1/-1;border:3px solid #22c55e;box-shadow:0 0 10px rgba(34,197,94,.35)"></div>
    <div class="bot" id="rsi2atr-panel" style="grid-column:1/-1;border:3px solid #14b8a6;box-shadow:0 0 10px rgba(20,184,166,.35)"></div>
    <div class="bot" id="patternbots-panel" style="grid-column:1/-1;border:3px solid #a855f7;box-shadow:0 0 10px rgba(168,85,247,.35)"></div>
    <div class="bot" id="newspaper-panel" style="grid-column:1/-1;border:3px solid #3b82f6;box-shadow:0 0 12px rgba(59,130,246,.45)"></div>
    <div class="bot pinned" id="ictsm-panel" style="grid-column:1/-1;border:3px solid #a855f7;box-shadow:0 0 12px rgba(168,85,247,.5)"></div>
    <div class="bot" id="ict-panel"></div>
    <div class="bot" id="ictfreq-panel"></div>
    <div class="bot" id="freq-panel"></div>
    <div class="bot" id="freqtp-panel"></div>
    <div class="bot" id="freqtrend-panel"></div>
    <div class="bot" id="freq5-panel"></div>
    <div class="bot" id="freqtf-panel"></div>
    <div class="bot" id="ts-panel"></div>
    <div class="bot" id="ob-panel-1m"></div>
    <div class="bot" id="ob-panel-5m"></div>
    <div class="bot" id="ob-panel-15m"></div>
    <div class="bot" id="cot-panel"></div>
    <div class="bot" id="nw-panel"></div>
    <div class="bot" id="onchain-panel"></div>
    <div class="bot" id="poly-panel" style="grid-column:1/-1;border-color:#6b3fa0"></div>
  </div>
<script>
  const $=id=>document.getElementById(id);
  const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const money=v=>(v==null?'—':'$'+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}));
  const fmt=v=>(v==null?'—':(v>=0?'+':'')+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}));
  const px1=v=>(v==null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:1}));
  const tstr=ts=>new Date(ts*1000).toLocaleTimeString();

  function posBlock(s){
    if(!s.positions || !s.positions.length) return '<div class="empty">No open position.</div>';
    return s.positions.map(p=>{
      const up=p.pnl>=0;
      const pnlExtra = (p.pnl_R!=null)? (' ('+fmt(p.pnl_R)+'R)') : (p.pnl_pct!=null? (' ('+fmt(p.pnl_pct)+'%)') : '');
      let det1='<span>entry '+px1(p.entry)+'</span>';
      if(p.stop!=null) det1+='<span>stop '+px1(p.stop)+'</span>';
      if(p.tp1!=null) det1+='<span>TP1 '+px1(p.tp1)+'</span>';
      if(p.tp2!=null) det1+='<span>TP2 '+px1(p.tp2)+'</span>';
      if(p.leverage!=null) det1+='<span>'+p.leverage+'x</span>';
      if(p.liq!=null) det1+='<span>liq '+px1(p.liq)+'</span>';
      let flags='';
      if(p.half) flags+=' · 50% out';
      if(p.be) flags+=' · BE';
      return '<div class="posrow"><div class="top">'+
        '<span class="side '+p.side+'">'+p.side.toUpperCase()+(p.qty!=null?' · '+p.qty+' BTC':'')+flags+'</span>'+
        '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl)+pnlExtra+'</span></div>'+
        '<div class="det">'+det1+'</div>'+
        (p.news?'<div class="det" style="white-space:normal"><span style="color:var(--muted)">'+esc(p.news)+'</span></div>':'')+
        '</div>';
    }).join('');
  }
  function logBlock(s){
    if(!s.log || !s.log.length) return '<div class="empty">No activity yet.</div>';
    return s.log.map(l=>'<div class="ln '+(l.kind||'info')+'"><span class="lt">'+tstr(l.t)+'</span><span><b>'+esc(l.msg)+'</b></span></div>').join('');
  }
  function histBlock(s){
    if(!s.history || !s.history.length) return '<div class="empty">No closed trades yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side '+t.side+'">'+t.side.toUpperCase()+'</span>'+
      '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+(t.rr!=null?' ('+fmt(t.rr)+'R)':'')+'</span></div>'+
      '<div class="det"><span>'+esc(t.reason||'')+'</span><span>'+px1(t.entry)+' → '+px1(t.exit)+'</span></div></div>').join('');
  }
  function statHead(s){
    const winrate = s.trades? Math.round(s.wins/s.trades*100)+'%':'—';
    return '<div class="stats">'+
      '<div class="stat"><div class="k">Equity</div><div class="v">'+money(s.equity)+'</div></div>'+
      '<div class="stat"><div class="k">Net P&L</div><div class="v '+(s.total_pnl>=0?'pos':'neg')+'">'+fmt(s.total_pnl)+'</div></div>'+
      '<div class="stat"><div class="k">Win rate</div><div class="v">'+winrate+' <span style="color:var(--muted);font-size:11px">· '+s.trades+'</span></div></div>'+
      '<div class="stat"><div class="k">Balance</div><div class="v">'+money(s.balance)+'</div></div>'+
      '</div>';
  }

  function renderICTPanel(panel, s, title, toggleFn, resetFn){
    if(!s.running){ $(panel).innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">'+esc(title)+'</span></div><div class="empty">bot not running</div>'; return; }
    const dot = s.enabled?'on':'off';
    const stateTxt = !s.running?'not running':(s.enabled?(s.killzone?('killzone: '+s.killzone):'live · waiting for killzone'):'paused');
    const tgl = s.enabled?'Pause':'Enable';
    const tglCls = s.enabled?'on':'off';
    const biasTxt = s.bias?('bias '+s.bias.toUpperCase()):'bias —';
    const phaseTxt = s.phase||'—';
    let rangeTxt='';
    if(s.range_low!=null) rangeTxt = 'range '+px1(s.range_low)+'–'+px1(s.range_high)+' · EQ '+px1(s.eq);
    const maxTrades = s.max_trades_day || 2;
    const dayTxt = 'today '+(s.trades_today||0)+'/'+maxTrades+' trades · '+fmt(s.day_R)+'R';
    const modeTxt = s.active_window?(' · '+s.active_window+' · min R:R '+s.min_rr+' · risk '+(s.risk_pct*100).toFixed(2)+'%'):'';
    const errTxt = s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    $(panel).innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">'+esc(title)+'</span>'+
        '<span class="badge '+(s.killzone?'live':'')+'">'+phaseTxt+'</span>'+
        '<span class="badge">'+biasTxt+'</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="'+toggleFn+'()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="'+resetFn+'()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(stateTxt)+' · '+esc(rangeTxt)+' · '+dayTxt+esc(modeTxt)+'</div>'+
      errTxt+
      '<div class="ph">Open position</div>'+posBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
    if(s.price) $('px').textContent='BTC '+money(s.price);
  }
  async function loadICT(){
    let s; try{ s=await(await fetch('/api/ictbot/state')).json(); }catch(e){ return; }
    renderICTPanel('ict-panel', s, 'ICT 2022 model', 'toggleICT', 'resetICT');
  }
  async function toggleICT(){ let s; try{s=await(await fetch('/api/ictbot/state')).json();}catch(e){return;} await fetch('/api/ictbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadICT(); }
  async function resetICT(){ if(!confirm('Reset the ICT paper account?'))return; await fetch('/api/ictbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadICT(); }
  async function loadICTSM(){
    let s; try{ s=await(await fetch('/api/ictsm/state')).json(); }catch(e){ return; }
    renderICTPanel('ictsm-panel', s, 'ICT SM Trades (BTC · paper)', 'toggleICTSM', 'resetICTSM');
  }
  async function toggleICTSM(){ let s; try{s=await(await fetch('/api/ictsm/state')).json();}catch(e){return;} await fetch('/api/ictsm/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadICTSM(); }
  async function resetICTSM(){ if(!confirm('Reset the ICT SM Trades paper account?'))return; await fetch('/api/ictsm/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadICTSM(); }

  async function loadICTFreq(){
    let s; try{ s=await(await fetch('/api/ictfreqbot/state')).json(); }catch(e){ return; }
    renderICTPanel('ictfreq-panel', s, 'ICT 2022 model (frequent paper)', 'toggleICTFreq', 'resetICTFreq');
  }
  async function toggleICTFreq(){ let s; try{s=await(await fetch('/api/ictfreqbot/state')).json();}catch(e){return;} await fetch('/api/ictfreqbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadICTFreq(); }
  async function resetICTFreq(){ if(!confirm('Reset the frequent ICT paper account?'))return; await fetch('/api/ictfreqbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadICTFreq(); }

  async function loadNW(){
    let s; try{ s=await(await fetch('/api/nwbot/state?fw=60')).json(); }catch(e){ return; }
    if(!s.running){ $('nw-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">Whale / News (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?(s.watching?'on':'on'):'off';
    const stateTxt=!s.enabled?'paused':(s.watching?'watching news…':'live · waiting');
    const tgl=s.enabled?'Pause':'Resume';
    const tglCls=s.enabled?'on':'off';
    let stratOpts='';
    if(s.strategies){ stratOpts=Object.entries(s.strategies).map(kv=>'<option value="'+kv[0]+'"'+(kv[0]===s.strategy?' selected':'')+'>'+esc(kv[1])+'</option>').join(''); }
    $('nw-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">Whale / News (paper)</span>'+
        '<select id="nw-strat" onchange="setNWStrat(this.value)">'+stratOpts+'</select>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleNW()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetNW()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(stateTxt)+(s.flow_pct!=null?(' · flow '+s.flow_pct+'% buy'):'')+'</div>'+
      '<div class="ph">Open positions</div>'+posBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleNW(){ let s; try{s=await(await fetch('/api/nwbot/state')).json();}catch(e){return;} await fetch('/api/nwbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadNW(); }
  async function resetNW(){ if(!confirm('Reset the whale/news paper account?'))return; await fetch('/api/nwbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNW(); }
  async function setNWStrat(v){ await fetch('/api/nwbot/strategy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategy:v})}); loadNW(); }

  // ---- AI NEWS BOT: reads the site's news feed, paper-trades the AI's call ----
  function newsPosBlock(s){
    if(!s.positions || !s.positions.length) return '<div class="empty">No open position.</div>';
    return s.positions.map(p=>{
      const up=p.pnl>=0;
      let det='<span>'+esc(p.coin)+'</span><span>entry '+px1(p.entry)+'</span>';
      if(p.mark!=null) det+='<span>mark '+px1(p.mark)+'</span>';
      if(p.stop!=null) det+='<span>SL '+px1(p.stop)+'</span>';
      if(p.tp1!=null) det+='<span>TP '+px1(p.tp1)+'</span>';
      if(p.leverage!=null) det+='<span>'+p.leverage+'x</span>';
      if(p.conviction) det+='<span>'+esc(p.conviction)+'</span>';
      return '<div class="posrow"><div class="top">'+
        '<span class="side '+p.side+'">'+p.side.toUpperCase()+' · '+esc(p.coin)+'</span>'+
        '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl)+' ('+fmt(p.pnl_pct)+'%)</span></div>'+
        '<div class="det">'+det+'</div>'+
        (p.news?'<div class="det" style="white-space:normal"><span style="color:var(--muted)">'+esc(p.news)+'</span></div>':'')+
        '</div>';
    }).join('');
  }
  function newsHistBlock(s){
    if(!s.history || !s.history.length) return '<div class="empty">No closed trades yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side '+t.side+'">'+t.side.toUpperCase()+' · '+esc(t.coin||'')+'</span>'+
      '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>'+
      '<div class="det"><span>'+esc(t.reason||'')+'</span><span>'+px1(t.entry)+' → '+px1(t.exit)+'</span></div></div>').join('');
  }
  async function loadNewsAI(){
    let s; try{ s=await(await fetch('/api/newsbot/state')).json(); }catch(e){ return; }
    if(!s.running){ $('news-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">📰 News Reactor (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Resume';
    const tglCls=s.enabled?'on':'off';
    const stateTxt=!s.enabled?'paused':(s.analyzing>0?('reading '+s.analyzing+' headline'+(s.analyzing>1?'s':'')+'…'):'live · watching the news feed');
    let modeOpts='';
    if(s.modes){ modeOpts=Object.entries(s.modes).map(kv=>'<option value="'+kv[0]+'"'+(kv[0]===s.mode?' selected':'')+'>'+esc(kv[1])+'</option>').join(''); }
    $('news-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">📰 News Reactor (paper)</span>'+
        '<span class="badge'+(s.analyzing>0?' live':'')+'">'+(s.analyzing>0?('AI ×'+s.analyzing):'idle')+'</span>'+
        '<select id="news-mode" title="how aggressively to trade the news" onchange="setNewsMode(this.value)">'+modeOpts+'</select>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleNewsAI()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetNewsAI()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(stateTxt)+' · scores every headline (impact×confidence), bets bigger on stronger news</div>'+
      '<div class="ph">Open positions</div>'+newsPosBlock(s)+
      '<div class="ph">Decisions &amp; activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+newsHistBlock(s)+'</div>';
  }
  async function toggleNewsAI(){ let s; try{s=await(await fetch('/api/newsbot/state')).json();}catch(e){return;} await fetch('/api/newsbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadNewsAI(); }
  async function resetNewsAI(){ if(!confirm('Reset the News Reactor paper account? Clears balance, positions and trade history.'))return; await fetch('/api/newsbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNewsAI(); }
  async function setNewsMode(v){ await fetch('/api/newsbot/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:v})}); loadNewsAI(); }

  // ---- NEWS SNIPER: no-AI rules engine, reacts to the headline text instantly ----
  async function loadSniper(){
    let s; try{ s=await(await fetch('/api/sniperbot/state')).json(); }catch(e){ return; }
    if(!s.running){ $('sniper-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">🎯 News Sniper (paper · no AI)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Resume';
    const tglCls=s.enabled?'on':'off';
    let modeOpts='';
    if(s.modes){ modeOpts=Object.entries(s.modes).map(kv=>'<option value="'+kv[0]+'"'+(kv[0]===s.mode?' selected':'')+'>'+esc(kv[1])+'</option>').join(''); }
    $('sniper-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">🎯 News Sniper (paper · no AI)</span>'+
        '<span class="badge">instant · rules</span>'+
        '<select id="sniper-mode" title="how aggressively to trade the news" onchange="setSniperMode(this.value)">'+modeOpts+'</select>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleSniper()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetSniper()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+(s.enabled?'live':'paused')+' · keyword + event lexicon scores each headline (no API call), coin pulled from the text</div>'+
      '<div class="ph">Open positions</div>'+newsPosBlock(s)+
      '<div class="ph">Decisions &amp; activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+newsHistBlock(s)+'</div>';
  }
  async function toggleSniper(){ let s; try{s=await(await fetch('/api/sniperbot/state')).json();}catch(e){return;} await fetch('/api/sniperbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadSniper(); }
  async function resetSniper(){ if(!confirm('Reset the News Sniper paper account? Clears balance, positions and trade history.'))return; await fetch('/api/sniperbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadSniper(); }
  async function setSniperMode(v){ await fetch('/api/sniperbot/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:v})}); loadSniper(); }

  // ---- CROSS-EXCHANGE ARBITRAGE: all venues, multiple hedged pairs ----
  function arbPricesBlock(s){
    if(!s.prices || !s.prices.length) return '<div class="empty">No live exchange prices yet.</div>';
    const lo=s.prices[0].px, hi=s.prices[s.prices.length-1].px;
    return '<div style="padding:8px 14px;display:flex;flex-wrap:wrap;gap:6px">'+
      s.prices.map(r=>{
        const col = r.px===lo?'var(--green)':(r.px===hi?'var(--red)':'var(--muted)');
        return '<span class="flowwin" style="color:'+col+'">'+esc(r.ex)+' '+px1(r.px)+'</span>';
      }).join('')+'</div>';
  }
  function arbPairsBlock(s){
    if(!s.pairs || !s.pairs.length) return '<div class="empty">No open pairs. Opens when any two venues differ by ≥ '+s.entry_bps+' bps.</div>';
    return s.pairs.map(p=>{
      const cup=p.combined_pnl>=0;
      const legs = p.legs.map(l=>{
        const up=l.pnl>=0;
        return '<div class="det" style="justify-content:space-between"><span class="side '+l.side+'">'+l.side.toUpperCase()+' '+esc(l.ex)+'</span>'+
          '<span>entry '+px1(l.entry)+(l.mark!=null?' · mark '+px1(l.mark):'')+'</span>'+
          '<span style="color:'+(up?'var(--green)':'var(--red)')+'">'+fmt(l.pnl)+'</span></div>';
      }).join('');
      return '<div class="posrow"><div class="top">'+
        '<span style="font-weight:600">'+esc(p.a)+' ↔ '+esc(p.b)+'</span>'+
        '<span style="color:'+(cup?'var(--green)':'var(--red)')+';font-weight:700">'+fmt(p.combined_pnl)+'</span></div>'+
        '<div class="det"><span>gap '+p.entry_gap_bps+'→'+(p.gap_bps_now!=null?p.gap_bps_now:'?')+' bps</span></div>'+
        legs+'</div>';
    }).join('');
  }
  function arbHistBlock(s){
    if(!s.history || !s.history.length) return '<div class="empty">No closed pairs yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="src">'+esc(t.a)+'/'+esc(t.b)+' · '+esc(t.reason||'')+'</span>'+
      '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>'+
      '<div class="det"><span>gap '+t.entry_gap_bps+'->'+t.exit_gap_bps+' bps</span><span>'+t.lev+'x</span></div></div>').join('');
  }
  async function loadArb(){
    let s; try{ s=await(await fetch('/api/arb/state')).json(); }catch(e){ return; }
    if(!s.running){ $('arb-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">🔀 Cross-Exchange Arbitrage (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Resume';
    const tglCls=s.enabled?'on':'off';
    let levOpts='';
    if(s.levs){ levOpts=Object.entries(s.levs).map(kv=>'<option value="'+kv[0]+'"'+(String(kv[0])===String(s.lev)?' selected':'')+'>'+esc(kv[1])+'</option>').join(''); }
    const gapCol = s.best_gap_bps>=s.entry_bps?'var(--amber)':'var(--muted)';
    $('arb-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">🔀 Cross-Exchange Arbitrage (paper)</span>'+
        '<span class="badge">'+(s.venues||0)+' venues</span>'+
        '<select id="arb-lev" title="leverage per leg ($100 margin each side)" onchange="setArbLev(this.value)">'+levOpts+'</select>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleArb()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetArb()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">widest gap <b style="color:'+gapCol+'">'+s.best_gap_bps+' bps</b> · '+
        'enter ≥'+s.entry_bps+' / close ≤'+s.converge_bps+' bps · '+esc(s.status||'')+'</div>'+
      '<div class="ph">Live BTC by exchange (green = cheapest, red = dearest)</div>'+arbPricesBlock(s)+
      '<div class="ph">Open pairs (long cheap / short dear)</div>'+arbPairsBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed pairs</div><div class="hist">'+arbHistBlock(s)+'</div>';
  }
  async function toggleArb(){ let s; try{s=await(await fetch('/api/arb/state')).json();}catch(e){return;} await fetch('/api/arb/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadArb(); }
  async function resetArb(){ if(!confirm('Reset the Cross-Exchange Arbitrage paper account?'))return; await fetch('/api/arb/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadArb(); }
  async function setArbLev(v){ const r=await(await fetch('/api/arb/lev',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lev:v})})).json(); if(!r.ok&&r.error) alert(r.error); loadArb(); }

  // ---- CRYPTAL MAKER: BTC-TOUSD + BTC-TOGEL, immediate Binance hedge ----
  function cryptalPx(v){ v=Number(v||0); if(v>=1000)return v.toLocaleString(undefined,{maximumFractionDigits:2}); if(v>=1)return v.toFixed(4); if(v>=.01)return v.toFixed(6); return v.toFixed(8); }
  function cryptalMakerHist(s){
    if(!s.history || !s.history.length) return '<div class="empty">No completed maker/hedge cycles yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="src">'+esc(s.display_pair||'Cryptal')+' maker cycle</span><span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">$'+Number(t.pnl||0).toFixed(4)+'</span></div>'+
      '<div class="det"><span>spot '+cryptalPx(t.buy)+' -> '+cryptalPx(t.sell)+'</span><span>hedge close '+cryptalPx(t.hedge_close)+'</span><span>'+fmt(t.return_bps)+' bps</span></div></div>').join('');
  }
  function cryptalUniverse(s){
    const u=s.market_universe||{}; if(!Object.keys(u).length)return '';
    const rows=(u.opportunities||[]).slice(0,18);
    const table=rows.length?rows.map((r,i)=>'<div class="posrow" style="padding:6px 14px"><div class="top"><span><b>'+(i+1)+'. '+esc(r.display_pair||r.pair)+'</b> <span class="src">hedge '+esc(r.hedge_symbol||'')+'</span></span><span style="color:var(--green)">'+Number(r.conservative_net_bps||0).toFixed(1)+' bps screened</span></div><div class="det"><span>book '+cryptalPx(r.cryptal_bid)+' / '+cryptalPx(r.cryptal_ask)+'</span><span>spread '+Number(r.spread_bps||0).toFixed(1)+' bps</span><span>last print '+Math.round(Number(r.last_trade_age_sec||0)/60)+'m ago</span></div></div>').join(''):'<div class="empty">No market currently clears the conservative paper screen.</div>';
    const every=Number(u.scan_interval_sec||60), cadence=every<120?Math.round(every)+' seconds':Math.round(every/60)+' minutes';
    const v=u.georgian_venues||{}, vr=(v.opportunities&&v.opportunities.length?v.opportunities:v.rows||[]).slice(0,16);
    const venueRows=vr.length?vr.map(r=>'<div class="posrow" style="padding:6px 14px"><div class="top"><span><b>'+esc(r.venue||'venue')+' · '+esc(r.pair||'')+'</b> <span class="src">'+esc(r.quote_kind||'fixed quote')+'</span></span><span style="color:'+(Number(r.net_edge_bps||0)>=100?'var(--green)':'var(--muted)')+'">'+Number(r.net_edge_bps||0).toFixed(1)+' bps after reserves</span></div><div class="det"><span>'+esc(r.direction||'')+'</span><span>gross '+Number(r.gross_edge_bps||0).toFixed(1)+' bps</span><span>'+esc(r.reason||r.detail||'screen only')+'</span></div></div>').join(''):'<div class="empty">Waiting for public fixed quotes from Coinet, Mycoins and PlatformaEX.</div>';
    const venueBlock='<div class="ph">Other registered Georgian venues — fixed-quote screen, no assumed fills</div><div class="sub" style="padding:7px 14px">'+esc(v.status||'waiting for multi-venue scan')+'; '+Number(v.registered_vasp_count||42)+' active VASPs audited, '+Number(v.machine_readable_local_count||4)+' verified machine-readable local feeds, and only '+Number(v.same_as_cryptal_count||1)+' Cryptal-style public order book. Screen cadence '+Number(v.scan_interval_sec||15)+'s at $'+Number(v.paper_notional_usd||100).toFixed(0)+'. '+esc(v.limitations||'')+'</div>'+venueRows;
    return '<div class="ph">Cryptal order-book markets</div><div class="sub" style="padding:7px 14px">'+esc(u.status||'')+'; catalog '+Number(u.catalog_count||0)+', hedgeable '+Number(u.eligible_count||0)+', scanned '+Number(u.scanned_count||0)+'. Full Cryptal universe refresh every '+cadence+'; the selected market remains on a 2-second fill loop. <button class="btn" onclick="scanCryptalGeo()">scan now</button></div>'+table+venueBlock;
  }
  async function renderCryptalMaker(endpoint,panelId,toggleFn,resetFn,fallbackName){
    let s; try{ s=await(await fetch(endpoint+'/state')).json(); }catch(e){ return; }
    if(!s.running){ $(panelId).innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">'+esc(fallbackName)+'</span></div><div class="empty">bot not running</div>'; return; }
    const m=s.market||{}, q=s.quote||{}, inv=s.inventory||{}, val=s.validation||{};
    const alloc=s.starting_allocation||{};
    const dot=s.enabled&&!s.data_error?'on':'off';
    const tgl=s.enabled?'Pause':'Resume';
    const localCode=(s.display_pair||'BTC-TOUSD').split('-')[1]||s.quote_currency||'quote';
    const quoteText=s.quote
      ? esc(q.side)+' '+Number(q.qty||0).toFixed(8)+' '+esc(s.base_asset||'BTC')+' @ '+cryptalPx(q.price)+'; queue '+Number(q.queue_ahead_base!=null?q.queue_ahead_base:q.queue_ahead_btc||0).toFixed(8)+' '+esc(s.base_asset||'BTC')+'; '+esc(String(q.edge_metric||'edge').replaceAll('_',' '))+' '+Number(q.edge_metric_bps||0).toFixed(1)+' bps'
      : 'No virtual maker order working.';
    const marketText=m.fair_quote
      ? 'Cryptal '+cryptalPx(m.cryptal_bid)+' / '+cryptalPx(m.cryptal_ask)+' '+localCode+'; fair '+cryptalPx(m.fair_quote)+'; spread '+Number(m.cryptal_spread_bps||0).toFixed(1)+' bps; Binance '+cryptalPx(m.binance_bid)+' / '+cryptalPx(m.binance_ask)+'; USDT/'+localCode+' '+Number(m.stable_mid||0).toFixed(4)+(s.quote_currency&&s.quote_currency!=='USD'?'; USDT/TOUSD settlement '+Number(m.settlement_mid||0).toFixed(4):'')
      : 'Waiting for Cryptal and Binance public order books.';
    const asset=esc(s.base_asset||inv.base_asset||'BTC');
    const inventoryText='Cryptal spot '+Number(inv.spot_qty||0).toFixed(8)+' '+asset+'; Binance short '+Number(inv.short_qty||0).toFixed(8)+' '+asset+'; net delta '+Number(inv.delta_base!=null?inv.delta_base:inv.delta_btc||0).toFixed(8)+' '+asset;
    const problems=[s.data_error,s.persistence_error].filter(Boolean).map(x=>'<div class="sub" style="padding:6px 14px;color:var(--red)">'+esc(x)+'</div>').join('');
    const venueAudit=s.quote_currency==='GEL'&&s.georgian_market_audit
      ? '<div class="sub" style="padding:6px 14px;color:var(--muted)">Georgian venue audit: '+Number(s.georgian_market_audit.registered_vasps_in_scope||0)+' VASPs were in the official NBG register used as scope; only Cryptal exposed a qualifying public local order book and timestamped trade tape. Global exchange liquidity, dealer, wallet, P2P and OTC quotes fail closed.</div>'
      : '';
    const nativeAlloc=s.quote_currency==='GEL'&&Number(alloc.cryptal_quote_amount||0)>0
      ? ' (~'+Number(alloc.cryptal_quote_amount).toFixed(2)+' '+localCode+')':'';
    const marketBadge=s.quote_currency==='GEL'?'<span class="badge">GEORGIAN GEL BOOK</span>':'';
    $(panelId).innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">'+esc(s.name||fallbackName)+'</span>'+
        '<span class="badge cryptal-new-badge">NEW STRATEGY</span>'+marketBadge+'<span class="badge">PAPER ONLY</span><span class="badge">'+esc(s.display_pair||'BTC-TOUSD')+'</span><span class="spacer"></span>'+
        '<button class="btn '+(s.enabled?'on':'off')+'" onclick="'+toggleFn+'()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="'+resetFn+'()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'; isolated $'+Number(s.paper_bankroll_usd||100).toFixed(0)+' paper trial ($'+Number(alloc.cryptal_usd_equivalent||50).toFixed(0)+'-equivalent Cryptal '+localCode+nativeAlloc+' + $'+Number(alloc.binance_usdt||50).toFixed(0)+' Binance hedge collateral); maximum quote $'+Number(alloc.maximum_quote_notional_usd||alloc.maximum_quote_notional||40).toFixed(0)+' equivalent. Polls every '+Number(s.poll_sec||2)+'s; unresolved inventory is force-closed after '+Math.round(Number(s.max_hedged_hold_sec||86400)/3600)+'h. No private API or real-order code exists.</div>'+
      problems+
      venueAudit+
      '<div class="ph">Executable market and conversion</div><div class="sub" style="padding:8px 14px">'+marketText+'</div>'+
      '<div class="ph">Working paper quote</div><div class="sub" style="padding:8px 14px">'+quoteText+'</div>'+
      '<div class="ph">Hedged inventory</div><div class="sub" style="padding:8px 14px">'+inventoryText+'</div>'+
      '<div class="sub" style="padding:0 14px 8px">Validation: '+esc(val.status||'COLLECTING')+'; '+Number(val.completed_cycles||0)+' / '+Number(val.minimum_cycles||30)+' completed cycles. Public prints cannot prove exact queue position, so results never unlock live trading.</div>'+
      (s.capital_model?'<div class="sub" style="padding:0 14px 8px;color:var(--amber)">'+esc(s.capital_model)+'</div>':'')+
      cryptalUniverse(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed paper cycles</div><div class="hist">'+cryptalMakerHist(s)+'</div>';
  }
  async function loadCryptalMaker(){ return renderCryptalMaker('/api/cryptalmaker','cryptalmaker-panel','toggleCryptalMaker','resetCryptalMaker','Cryptal BTC-TOUSD Maker + Binance Hedge'); }
  async function loadCryptalGelMaker(){ return renderCryptalMaker('/api/cryptalgelmaker','cryptalgelmaker-panel','toggleCryptalGelMaker','resetCryptalGelMaker','Cryptal BTC-TOGEL Maker + Binance Hedge'); }
  async function loadCryptalGeo(){ return renderCryptalMaker('/api/cryptalgeo','cryptalgeo-panel','toggleCryptalGeo','resetCryptalGeo','Georgian Multi-Venue Arbitrage Monitor + Cryptal Maker'); }
  async function toggleCryptalMaker(){ let s; try{s=await(await fetch('/api/cryptalmaker/state')).json();}catch(e){return;} await fetch('/api/cryptalmaker/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadCryptalMaker(); }
  async function resetCryptalMaker(){ if(!confirm('Reset the Cryptal maker paper collector?'))return; await fetch('/api/cryptalmaker/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadCryptalMaker(); }
  async function toggleCryptalGelMaker(){ let s; try{s=await(await fetch('/api/cryptalgelmaker/state')).json();}catch(e){return;} await fetch('/api/cryptalgelmaker/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadCryptalGelMaker(); }
  async function resetCryptalGelMaker(){ if(!confirm('Reset the Cryptal BTC-TOGEL maker paper collector?'))return; await fetch('/api/cryptalgelmaker/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadCryptalGelMaker(); }
  async function toggleCryptalGeo(){ let s; try{s=await(await fetch('/api/cryptalgeo/state')).json();}catch(e){return;} await fetch('/api/cryptalgeo/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadCryptalGeo(); }
  async function resetCryptalGeo(){ if(!confirm('Reset the currently selected Georgian-market paper ledger?'))return; await fetch('/api/cryptalgeo/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadCryptalGeo(); }
  async function scanCryptalGeo(){ await fetch('/api/cryptalgeo/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadCryptalGeo(); }

  // ---- FAIR-VALUE TRACKING: Lighter follower vs leader consensus (zero-fee edge) ----
  function fvPriceBlock(s){
    if(s.fv==null || s.lighter==null) return '<div class="empty">Waiting for fresh leader + Lighter prices on the tape.</div>';
    const devCol = (s.dev_bps!=null && Math.abs(s.dev_bps)>=s.enter_bps)?'var(--amber)':'var(--muted)';
    return '<div style="padding:8px 14px;display:flex;flex-wrap:wrap;gap:6px;align-items:center">'+
      '<span class="flowwin">FV (leaders) '+px1(s.fv)+'</span>'+
      '<span class="flowwin">Lighter '+px1(s.lighter)+'</span>'+
      '<span class="flowwin" style="color:'+devCol+'">deviation <b>'+(s.dev_bps!=null?(s.dev_bps>=0?'+':'')+s.dev_bps:'—')+' bps</b></span>'+
      '<span class="src">'+(s.n_leaders||0)+' leader venues · enter at ±'+s.enter_bps+' / converge ≤'+s.exit_bps+'</span></div>';
  }
  function fvPosBlock(s){
    const p=s.pos;
    if(!p) return '<div class="empty">Flat. Opens when Lighter deviates from fair value by ≥ '+s.enter_bps+' bps (Lighter cheap → LONG, rich → SHORT).</div>';
    const up=p.pnl>=0;
    return '<div class="posrow"><div class="top">'+
      '<span class="side '+p.side+'">'+p.side.toUpperCase()+' Lighter</span>'+
      '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:700">'+fmt(p.pnl)+' ('+fmt(p.pnl_pct)+'%)</span></div>'+
      '<div class="det"><span>entry '+px1(p.entry)+(p.mark!=null?' · mark '+px1(p.mark):'')+'</span>'+
      '<span>dev '+(p.entry_dev>=0?'+':'')+p.entry_dev+'→'+(p.dev_now!=null?(p.dev_now>=0?'+':'')+p.dev_now:'?')+' bps</span></div>'+
      '<div class="det"><span>'+p.qty+' BTC · '+s.lev+'x</span><span>FV@entry '+px1(p.entry_fv)+'</span></div></div>';
  }
  function fvHistBlock(s){
    if(!s.history || !s.history.length) return '<div class="empty">No closed trades yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side '+t.side+'">'+t.side.toUpperCase()+' · '+esc(t.reason||'')+'</span>'+
      '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>'+
      '<div class="det"><span>'+px1(t.entry)+' → '+px1(t.exit)+'</span><span>dev '+t.entry_dev+'→'+t.exit_dev+' bps</span></div></div>').join('');
  }
  async function loadFV(){
    let s; try{ s=await(await fetch('/api/fv/state')).json(); }catch(e){ return; }
    if(!s.running){ $('fv-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">🎯 Fair-Value Tracking (paper · Lighter, zero-fee)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Resume';
    const tglCls=s.enabled?'on':'off';
    let levOpts='';
    if(s.levs){ levOpts=Object.entries(s.levs).map(kv=>'<option value="'+kv[0]+'"'+(String(kv[0])===String(s.lev)?' selected':'')+'>'+esc(kv[1])+'</option>').join(''); }
    const feeCol = s.net_with_fees>=0?'var(--green)':'var(--red)';
    $('fv-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">🎯 Fair-Value Tracking (paper · Lighter, zero-fee)</span>'+
        '<span class="badge">'+(s.n_leaders||0)+' leaders</span>'+
        '<select id="fv-lev" title="leverage on the Lighter leg ($100 margin)" onchange="setFVLev(this.value)">'+levOpts+'</select>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleFV()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetFV()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">Lighter tracks the leaders\' consensus; bet on convergence when it drifts. '+esc(s.status||'')+'</div>'+
      '<div class="sub" style="padding:0 14px 7px">On Lighter fees are <b>$0</b> — the same trades after a '+'4.5'+' bps taker fee would net <b style="color:'+feeCol+'">'+fmt(s.net_with_fees)+'</b> (fees avoided so far: '+money(s.fee_would_pay)+'). That gap is the edge.</div>'+
      '<div class="ph">Fair value vs Lighter</div>'+fvPriceBlock(s)+
      '<div class="ph">Open position</div>'+fvPosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+fvHistBlock(s)+'</div>';
  }
  async function toggleFV(){ let s; try{s=await(await fetch('/api/fv/state')).json();}catch(e){return;} await fetch('/api/fv/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadFV(); }
  async function resetFV(){ if(!confirm('Reset the Fair-Value Tracking paper account?'))return; await fetch('/api/fv/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadFV(); }
  async function setFVLev(v){ const r=await(await fetch('/api/fv/lev',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lev:v})})).json(); if(!r.ok&&r.error) alert(r.error); loadFV(); }

  // ---- CRYPTO TREND BREAKOUT + ATR (daily, BTC/ETH/SOL — the book's system) ----
  function trendRegimeBlock(s){
    const bull = (s.regime||'').indexOf('LONG-MODE')>=0;
    const col = bull?'var(--green)':'var(--amber)';
    let cells='';
    (s.symbols||[]).forEach(sym=>{
      const k=(s.snap||{})[sym]; if(!k) return;
      const up=k.above_200;
      cells+='<span class="flowwin" style="color:'+(up?'var(--green)':'var(--muted)')+'">'+esc(sym.split('/')[0])+' '+px1(k.price)+
        ' <span style="color:var(--muted)">'+(up?'>':'<')+'200MA '+px1(k.ma200)+'</span></span>';
    });
    return '<div class="sub" style="padding:7px 14px">Market filter: <b style="color:'+col+'">'+esc(s.regime||'')+'</b></div>'+
      '<div style="padding:0 14px 8px;display:flex;flex-wrap:wrap;gap:6px">'+cells+'</div>';
  }
  function trendPosBlock(s){
    if(!s.positions || !s.positions.length) return '<div class="empty">No open trades. Waits for a base breakout or pullback in LONG-MODE (cash is a position in danger mode).</div>';
    return s.positions.map(p=>{
      const up=p.upnl>=0;
      return '<div class="posrow"><div class="top">'+
        '<span style="font-weight:600">'+esc(p.symbol.split('/')[0])+' <span class="side long">'+esc(p.kind)+'</span></span>'+
        '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:700">'+fmt(p.upnl)+' ('+fmt(p.r_mult)+'R)</span></div>'+
        '<div class="det"><span>entry '+px1(p.entry)+' · mark '+px1(p.mark)+'</span><span>stop '+px1(p.stop)+'</span></div>'+
        '<div class="det"><span>'+p.qty+' · '+(p.added?'full':'half (awaiting retest)')+'</span><span>'+(p.took1?'+2R✓ ':'')+(p.took2?'+4R✓':'')+'</span></div></div>';
    }).join('');
  }
  function trendHistBlock(s){
    if(!s.history || !s.history.length) return '<div class="empty">No closed trades yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="src">'+esc((t.symbol||'').split('/')[0])+' · '+esc(t.reason||'')+'</span>'+
      '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>'+
      '<div class="det"><span>'+px1(t.entry)+' → '+px1(t.exit)+'</span><span>'+esc(t.kind||'')+'</span></div></div>').join('');
  }
  async function loadTrend(){
    let s; try{ s=await(await fetch('/api/trend/state')).json(); }catch(e){ return; }
    if(!s.running){ $('trend-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">📈 Crypto Trend Breakout + ATR (paper · daily)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Resume';
    const tglCls=s.enabled?'on':'off';
    let riskOpts='';
    if(s.risks){ riskOpts=Object.entries(s.risks).map(kv=>'<option value="'+kv[0]+'"'+(String(kv[0])===String(s.risk)?' selected':'')+'>'+esc(kv[1])+'</option>').join(''); }
    $('trend-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">📈 Crypto Trend Breakout + ATR (paper · daily · BTC/ETH/SOL)</span>'+
        '<span class="badge">'+(s.trades||0)+' trades</span>'+
        '<select id="trend-risk" title="risk per trade" onchange="setTrendRisk(this.value)">'+riskOpts+'</select>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleTrend()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetTrend()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      trendRegimeBlock(s)+
      '<div class="ph">Open trades</div>'+trendPosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+trendHistBlock(s)+'</div>';
  }
  async function toggleTrend(){ let s; try{s=await(await fetch('/api/trend/state')).json();}catch(e){return;} await fetch('/api/trend/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadTrend(); }
  async function resetTrend(){ if(!confirm('Reset the Crypto Trend Breakout paper account?'))return; await fetch('/api/trend/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadTrend(); }
  async function setTrendRisk(v){ const r=await(await fetch('/api/trend/risk',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({risk:v})})).json(); if(!r.ok&&r.error) alert(r.error); loadTrend(); }

  // ---- AI NEWS TRADING BOT (Google News RSS -> LLM sentiment -> BTC) ----
  function aiScoreBar(score){
    const pct=Math.round((Math.max(-1,Math.min(1,score))+1)/2*100);   // -1..1 -> 0..100
    const col=score>0.05?'var(--green)':(score<-0.05?'var(--red)':'var(--muted)');
    return '<div style="padding:6px 14px"><div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)">'+
      '<span>SELL -1</span><span style="color:'+col+';font-weight:700">sentiment '+(score>=0?'+':'')+score.toFixed(2)+'</span><span>+1 BUY</span></div>'+
      '<div style="height:8px;background:#1a1f2b;border-radius:4px;position:relative;margin-top:3px">'+
      '<div style="position:absolute;left:50%;top:-2px;width:1px;height:12px;background:#3a4250"></div>'+
      '<div style="position:absolute;left:'+pct+'%;top:-3px;width:10px;height:14px;background:'+col+';border-radius:3px;transform:translateX(-50%)"></div></div></div>';
  }
  function aiHeadlines(s){
    if(!s.headlines || !s.headlines.length) return '<div class="empty">No recent headlines pulled yet.</div>';
    return '<div style="max-height:150px;overflow:auto">'+s.headlines.map(h=>
      '<div class="ln"><span class="lt">'+Math.round(h.age_min)+'m</span><span>'+esc(h.title)+'</span></div>').join('')+'</div>';
  }
  function aiPosBlock(s){
    const p=s.position;
    if(!p) return '<div class="empty">Flat. Opens when |sentiment| ≥ '+s.enter+' (long if positive, short if negative).</div>';
    const up=p.upnl>=0;
    return '<div class="posrow"><div class="top">'+
      '<span class="side '+p.side+'">'+p.side.toUpperCase()+' BTC</span>'+
      '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:700">'+fmt(p.upnl)+'</span></div>'+
      '<div class="det"><span>entry '+px1(p.entry)+' · mark '+px1(p.mark)+'</span><span>$'+p.notional+' notional ('+(p.lev||1)+'x)</span></div>'+
      '<div class="det"><span>stop '+px1(p.stop)+' · tp '+px1(p.tp)+' · liq '+px1(p.liq)+'</span><span>sentiment '+(p.entry_score>=0?'+':'')+p.entry_score+'</span></div></div>';
  }
  function aiHistBlock(s){
    if(!s.history || !s.history.length) return '<div class="empty">No closed trades yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side '+t.side+'">'+t.side.toUpperCase()+' · '+esc(t.reason||'')+'</span>'+
      '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>'+
      '<div class="det"><span>'+px1(t.entry)+' → '+px1(t.exit)+'</span><span>sentiment '+(t.entry_score>=0?'+':'')+t.entry_score+'</span></div></div>').join('');
  }
  async function loadAINews(){
    let s; try{ s=await(await fetch('/api/ainews/state')).json(); }catch(e){ return; }
    if(!s.running){ $('ainews-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">🤖 AI News Trading Bot (paper · BTC)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Resume';
    const tglCls=s.enabled?'on':'off';
    $('ainews-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">🤖 AI News Trading Bot (paper · BTC · Google News → LLM sentiment)</span>'+
        '<span class="badge">'+esc(s.engine||'')+'</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleAINews()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetAINews()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      aiScoreBar(s.score||0)+
      '<div class="ph">Latest headlines (Google News, last hour)</div>'+aiHeadlines(s)+
      '<div class="ph">Open position</div>'+aiPosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+aiHistBlock(s)+'</div>';
  }
  async function toggleAINews(){ let s; try{s=await(await fetch('/api/ainews/state')).json();}catch(e){return;} await fetch('/api/ainews/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadAINews(); }
  async function resetAINews(){ if(!confirm('Reset the AI News Trading paper account?'))return; await fetch('/api/ainews/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadAINews(); }

  // ---- CLAUDE HAIKU LIVE-FEED NEWS BOT ($100 / 20x, BTC) ----
  function claudeDecisionBlock(s){
    if(!s.decisions || !s.decisions.length) return '<div class="empty">No Claude decisions yet. It reacts to the live website news feed.</div>';
    return '<div style="max-height:220px;overflow:auto">'+s.decisions.map(d=>{
      const trade=d.decision==='TRADE';
      const col=trade?(d.side==='long'?'var(--green)':'var(--red)'):'var(--muted)';
      const plan=trade?(' '+String(d.side||'').toUpperCase()+' - SL '+Number(d.sl||0).toFixed(2)+'% - TP '+Number(d.tp||0).toFixed(2)+'% - hold '+Number(d.hold||0).toFixed(0)+'m'):' SKIP';
      return '<div class="posrow" style="padding:6px 14px"><div class="top">'+
        '<span style="color:'+col+';font-weight:700">'+esc(d.decision||'')+plan+'</span>'+
        '<span style="color:var(--muted)">'+(d.latency_ms?Math.round(d.latency_ms)+'ms':'')+'</span></div>'+
        '<div class="det"><span>'+esc(d.confidence||'')+'</span><span>'+esc(d.reason||'')+'</span><span>'+esc(d.source||'')+'</span></div>'+
        '<div class="det" style="white-space:normal"><span>'+esc(d.headline||'')+'</span></div></div>';
    }).join('')+'</div>';
  }
  async function loadClaudeHaiku(){
    let s; try{ s=await(await fetch('/api/claudehaiku/state')).json(); }catch(e){ return; }
    if(!s.running){ $('claudehaiku-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">Claude Haiku Live News (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Resume';
    const tglCls=s.enabled?'on':'off';
    $('claudehaiku-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">Claude Haiku Live News (paper - BTC - $100 - 20x)</span>'+
        '<span class="badge">'+esc(s.model||'Claude')+'</span>'+
        '<span class="badge">'+(s.price?('BTC '+px1(s.price)):'BTC -')+'</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleClaudeHaiku()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetClaudeHaiku()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+' - watches every live feed headline and lets Claude choose trade / skip / SL / TP / hold time.</div>'+
      '<div class="ph">Open position</div>'+posBlock(s)+
      '<div class="ph">Claude decisions</div>'+claudeDecisionBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleClaudeHaiku(){ let s; try{s=await(await fetch('/api/claudehaiku/state')).json();}catch(e){return;} await fetch('/api/claudehaiku/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadClaudeHaiku(); }
  async function resetClaudeHaiku(){ if(!confirm('Reset the Claude Haiku paper account?'))return; await fetch('/api/claudehaiku/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadClaudeHaiku(); }

  // ---- TRADINGVIEW STRATEGY PACK (12 strategies, each $100 / 20x on BTC) ----
  function tvCard(s){
    const dot=s.enabled?'on':'off';
    const sig=s.signal?s.signal.toUpperCase():'flat';
    const sigCol=s.signal==='long'?'var(--green)':(s.signal==='short'?'var(--red)':'var(--muted)');
    const pnlCol=s.pnl>=0?'var(--green)':'var(--red)';
    const lbl='display:block;margin:8px 0 2px;color:#f5d020;font-size:10.5px;font-weight:700;letter-spacing:.4px;text-transform:uppercase';
    // --- Open position ---
    let posHtml;
    if(s.position){ const p=s.position; const up=p.upnl>=0;
      posHtml='<div class="posrow" style="padding:4px 0"><div class="top">'+
          '<span class="side '+p.side+'">'+p.side.toUpperCase()+' · $'+p.notional+' (20x)</span>'+
          '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.upnl)+'</span></div>'+
          '<div class="det"><span>entry '+px1(p.entry)+(p.mark!=null?' · mark '+px1(p.mark):'')+'</span><span>liq '+px1(p.liq)+'</span></div></div>';
    } else { posHtml='<div class="empty" style="padding:4px 0">flat · signal '+sig+'</div>'; }
    // --- Activity (this strategy's own log) ---
    const actHtml=(s.log&&s.log.length)
      ? s.log.map(l=>'<div class="ln '+(l.kind||'info')+'"><span class="lt">'+tstr(l.t)+'</span><span>'+esc(l.msg)+'</span></div>').join('')
      : '<div class="empty" style="padding:4px 0">No activity yet.</div>';
    // --- Closed trades (this strategy's own history) ---
    const histHtml=(s.history&&s.history.length)
      ? s.history.map(t=>'<div class="det" style="padding:2px 0"><span><span class="side '+t.side+'">'+t.side.toUpperCase()+'</span> '+px1(t.entry)+'→'+px1(t.exit)+' <span class="muted">'+esc(t.reason||'')+'</span></span>'+
          '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>').join('')
      : '<div class="empty" style="padding:4px 0">No closed trades yet.</div>';
    return '<div style="border:1px solid #f5d020;border-radius:9px;padding:8px 11px;margin:6px 0;background:#15140c">'+
      '<div class="top"><span><span class="dot '+dot+'"></span> <b>'+esc(s.name)+'</b> <span class="badge">'+esc(s.tf)+'</span></span>'+
        '<span style="color:'+pnlCol+';font-weight:700">'+money(s.equity)+' ('+fmt(s.pnl_pct)+'%)</span></div>'+
      '<div class="det"><span style="color:'+sigCol+'">signal '+sig+'</span><span>'+s.wins+'/'+s.trades+' wins</span></div>'+
      '<span style="'+lbl+'">Open position</span>'+posHtml+
      '<span style="'+lbl+'">Activity</span><div class="feed" style="max-height:118px;overflow:auto">'+actHtml+'</div>'+
      '<span style="'+lbl+'">Closed trades</span><div class="hist" style="max-height:150px;overflow:auto">'+histHtml+'</div>'+
      '<div class="row" style="margin-top:6px"><button class="btn '+(s.enabled?'on':'off')+'" onclick="tvToggle(\''+s.key+'\','+(!s.enabled)+')">'+(s.enabled?'Pause':'Resume')+'</button>'+
        '<button class="btn reset" onclick="tvReset(\''+s.key+'\')">reset</button></div></div>';
  }
  async function loadTVStrats(){
    let s; try{ s=await(await fetch('/api/tvstrats/state')).json(); }catch(e){ return; }
    if(!s.running){ $('tvstrats-panel').innerHTML='<div class="bhead"><span class="bname" style="color:#f5d020">⭐ TradingView Strategy Pack</span></div><div class="empty">bot not running</div>'; return; }
    const cards=(s.strategies||[]).map(tvCard).join('');
    $('tvstrats-panel').innerHTML =
      '<div class="bhead"><span class="bname" style="color:#f5d020">⭐ TradingView Strategy Pack — 12 strategies · BTC · $100 each · 20x</span>'+
        '<span class="badge">BTC '+(s.btc?px1(s.btc):'—')+'</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn reset" onclick="tvResetAll()">reset all</button></div>'+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+' · each is a separate $100 paper account, all-in 20x. Implemented from each strategy’s standard public indicator logic.</div>'+
      '<div style="padding:4px 12px 10px">'+cards+'</div>';
  }
  async function tvToggle(k,on){ await fetch('/api/tvstrats/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k,enabled:on})}); loadTVStrats(); }
  async function tvReset(k){ if(!confirm('Reset '+k+' to $100?'))return; await fetch('/api/tvstrats/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})}); loadTVStrats(); }
  async function tvResetAll(){ if(!confirm('Reset ALL 12 strategies to $100?'))return; await fetch('/api/tvstrats/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadTVStrats(); }

  // ---- BOLLINGER + RSI + 200-SMA MEAN REVERSION ($100 / 20x, BTC 1h) ----
  function meanrevSnap(s){
    const k=s.snap||{}; if(k.price==null) return '<div class="empty">Loading 1h indicators…</div>';
    const above=k.above_200;
    return '<div style="padding:6px 14px;display:flex;flex-wrap:wrap;gap:6px;align-items:center">'+
      '<span class="flowwin">BTC '+px1(k.price)+'</span>'+
      '<span class="flowwin" style="color:'+(above?'var(--green)':'var(--red)')+'">'+(above?'>':'<')+' 200SMA '+px1(k.sma200)+'</span>'+
      '<span class="flowwin">RSI '+k.rsi+(k.rsi<35?' ✓<35':'')+'</span>'+
      '<span class="flowwin">BB '+px1(k.bb_lo)+' / '+px1(k.bb_mid)+' / '+px1(k.bb_up)+'</span></div>';
  }
  function meanrevPos(s){
    const p=s.position;
    if(!p) return '<div class="empty">Flat. Buys when price touches the lower Bollinger band AND RSI &lt; 35 AND price &gt; 200SMA. Exit = 20SMA, stop = 2×ATR.</div>';
    const up=p.upnl>=0;
    return '<div class="posrow"><div class="top"><span class="side long">LONG · $'+p.notional+' (20x)</span>'+
      '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:700">'+fmt(p.upnl)+'</span></div>'+
      '<div class="det"><span>entry '+px1(p.entry)+' · mark '+px1(p.mark)+'</span><span>stop '+px1(p.stop)+' · liq '+px1(p.liq)+'</span></div></div>';
  }
  function meanrevHist(s){
    if(!s.history||!s.history.length) return '<div class="empty">No closed trades yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side long">LONG · '+esc(t.reason||'')+'</span>'+
      '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>'+
      '<div class="det"><span>'+px1(t.entry)+' → '+px1(t.exit)+'</span></div></div>').join('');
  }
  async function loadMeanRev(){
    let s; try{ s=await(await fetch('/api/meanrev/state')).json(); }catch(e){ return; }
    if(!s.running){ $('meanrev-panel').innerHTML='<div class="bhead"><span class="bname" style="color:#ef6b6b">🟥 Bollinger + RSI + 200SMA Mean Reversion (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off'; const tgl=s.enabled?'Pause':'Resume'; const tglCls=s.enabled?'on':'off';
    $('meanrev-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname" style="color:#ef6b6b">🟥 Bollinger + RSI + 200SMA Mean Reversion (paper · BTC '+esc(s.timeframe||'5m')+' · $100 · 20x)</span>'+
        '<span class="badge">'+(s.trades||0)+' trades</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleMeanRev()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetMeanRev()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      meanrevSnap(s)+
      '<div class="ph">Open trade</div>'+meanrevPos(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+meanrevHist(s)+'</div>';
  }
  async function toggleMeanRev(){ let s; try{s=await(await fetch('/api/meanrev/state')).json();}catch(e){return;} await fetch('/api/meanrev/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadMeanRev(); }
  async function resetMeanRev(){ if(!confirm('Reset the Mean Reversion paper account to $100?'))return; await fetch('/api/meanrev/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadMeanRev(); }

  // ---- NEWS-MOMENTUM (1.5s pre-news ≥0.08% move -> bet direction; 5% trail) ----
  function newsmomoPos(s){
    const p=s.position;
    if(!p) return '<div class="empty">Flat. On ANY news it looks at the last 5 seconds (our news delay): if the crowd already moved BTC ≥0.07% it follows them that direction immediately. Exit = improved 5% margin trail.</div>';
    const up=p.upnl>=0; const sideCls=p.side==='long'?'long':'short';
    return '<div class="posrow"><div class="top"><span class="side '+sideCls+'">'+p.side.toUpperCase()+' · $'+p.notional+' (20x)</span>'+
      '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:700">'+fmt(p.upnl)+'</span></div>'+
      '<div class="det"><span>entry '+px1(p.entry)+' · mark '+px1(p.mark)+'</span><span>stop '+px1(p.stop)+' ('+p.stop_pct_margin+'% mgn)'+(p.trail_on?' · trailing':'')+'</span></div>'+
      '<div class="det"><span>trigger move '+(p.entry_move_pct!=null?p.entry_move_pct+'%':'—')+'</span><span>liq '+px1(p.liq)+'</span></div></div>';
  }
  function newsmomoHist(s){
    if(!s.history||!s.history.length) return '<div class="empty">No closed trades yet.</div>';
    return s.history.map(t=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side '+(t.side==='long'?'long':'short')+'">'+(t.side||'').toUpperCase()+' · '+esc(t.reason||'')+'</span>'+
      '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>'+
      '<div class="det"><span>'+px1(t.entry)+' → '+px1(t.exit)+'</span><span>'+(t.entry_move_pct!=null?'move '+t.entry_move_pct+'%':'')+'</span></div></div>').join('');
  }
  async function loadNewsMomo(){
    let s; try{ s=await(await fetch('/api/newsmomo/state')).json(); }catch(e){ return; }
    if(!s.running){ $('newsmomo-panel').innerHTML='<div class="bhead"><span class="bname" style="color:#ff8c1a">📰⚡ News-Momentum (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off'; const tgl=s.enabled?'Pause':'Resume'; const tglCls=s.enabled?'on':'off';
    $('newsmomo-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname" style="color:#ff8c1a">📰⚡ News-Momentum (paper · BTC · follow last-5s ≥0.07% move on news · $100 · 20x · 5% trail)</span>'+
        '<span class="badge">'+(s.trades||0)+' trades</span>'+
        '<span class="badge">'+(s.news_seen||0)+' news</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleNewsMomo()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetNewsMomo()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      '<div style="padding:2px 14px 6px;color:var(--muted);font-size:12px">last signal: '+esc(s.last_signal||'(none yet — waiting for news)')+'</div>'+
      '<div class="ph">Open position</div>'+newsmomoPos(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+newsmomoHist(s)+'</div>';
  }
  async function toggleNewsMomo(){ let s; try{s=await(await fetch('/api/newsmomo/state')).json();}catch(e){return;} await fetch('/api/newsmomo/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadNewsMomo(); }
  async function resetNewsMomo(){ if(!confirm('Reset the News-Momentum paper account to $100?'))return; await fetch('/api/newsmomo/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNewsMomo(); }

  function renderRSI2Panel(panel, s, title, toggleFn, resetFn){
    if(!s.running){ $(panel).innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">'+esc(title)+'</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable';
    const tglCls=s.enabled?'on':'off';
    const snap=s.snap||{};
    let ind='RSI2 '+(snap.rsi2!=null?snap.rsi2:'-')+' - EMA50 '+px1(snap.ema50);
    if(s.atr_filter){
      ind+=' - ATR '+(snap.atr_pct!=null?snap.atr_pct+'%':'-')+' in '+(snap.atr_q15!=null?snap.atr_q15+'%':'-')+'-'+(snap.atr_q85!=null?snap.atr_q85+'%':'-');
    } else {
      ind+=' - ATR filter OFF';
    }
    if(s.day_paused) ind+=' - daily stop active';
    if(s.cooldown_left) ind+=' - cooldown '+Math.ceil(Number(s.cooldown_left)/60)+'m';
    $(panel).innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">'+esc(title)+'</span>'+
        '<span class="badge">'+(s.timeframe||'15m')+' - BTC - '+(s.leverage||10)+'x</span>'+
        '<span class="badge">SL 1% - TP 1.5%</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="'+toggleFn+'()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="'+resetFn+'()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+' - '+esc(ind)+' - day '+fmt(s.day_pnl_pct)+'% - cost '+s.cost_bps+'bps/side</div>'+
      '<div class="ph">Open position</div>'+posBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
    if(s.price) $('px').textContent='BTC '+money(s.price);
  }
  async function loadRSI2NoATR(){
    let s; try{ s=await(await fetch('/api/rsi2noatr/state')).json(); }catch(e){ return; }
    renderRSI2Panel('rsi2noatr-panel', s, 'RSI2 EMA50 Scalper (paper - no ATR)', 'toggleRSI2NoATR', 'resetRSI2NoATR');
  }
  async function toggleRSI2NoATR(){ let s; try{s=await(await fetch('/api/rsi2noatr/state')).json();}catch(e){return;} await fetch('/api/rsi2noatr/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadRSI2NoATR(); }
  async function resetRSI2NoATR(){ if(!confirm('Reset the RSI2 no-ATR paper account to $100?'))return; await fetch('/api/rsi2noatr/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadRSI2NoATR(); }

  async function loadRSI2ATR(){
    let s; try{ s=await(await fetch('/api/rsi2atr/state')).json(); }catch(e){ return; }
    renderRSI2Panel('rsi2atr-panel', s, 'RSI2 EMA50 Scalper (paper - ATR filter)', 'toggleRSI2ATR', 'resetRSI2ATR');
  }
  async function toggleRSI2ATR(){ let s; try{s=await(await fetch('/api/rsi2atr/state')).json();}catch(e){return;} await fetch('/api/rsi2atr/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadRSI2ATR(); }
  async function resetRSI2ATR(){ if(!confirm('Reset the RSI2 ATR-filter paper account to $100?'))return; await fetch('/api/rsi2atr/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadRSI2ATR(); }

  function renderPatternBotCard(s){
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable';
    const tglCls=s.enabled?'on':'off';
    const cfg=s.config||{};
    const snap=s.snap||{};
    const fam=(snap.families&&snap.families.length)?snap.families.join(', '):'none';
    const err=s.data_error?'<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    const cfgTxt=s.symbol+' - '+s.timeframe+' - '+s.leverage+'x - '+cfg.min_families+'+ families - RR '+cfg.rr+' - stop '+cfg.stop_mode+' - hold '+cfg.max_bars_mode;
    return '<div class="bot" style="margin:10px;border-color:#34214f">'+
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">'+esc(s.label)+'</span>'+
        '<span class="badge '+(s.enabled?'live':'')+'">'+esc(s.status||'')+'</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="togglePatternBot(&quot;'+s.key+'&quot;)">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetPatternBot(&quot;'+s.key+'&quot;)">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(cfgTxt)+' - price '+px1(s.price)+' - signal '+esc(snap.signal||'-')+' - families '+esc(fam)+'</div>'+
      err+
      '<div class="ph">Open position</div>'+posBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>'+
      '</div>';
  }
  async function loadPatternBots(){
    let s; try{s=await(await fetch('/api/patternbots/state')).json();}catch(e){return;}
    const panel=$('patternbots-panel'); if(!panel) return;
    const bots=s.bots||[];
    panel.innerHTML='<div class="bhead"><span class="dot on"></span><span class="bname">All-Pattern Consensus (paper optimized) - BTC / ESM2026 / NMQ2026 / CL1</span>'+
      '<span class="badge live">best optimized configs</span><span class="spacer"></span></div>'+
      '<div class="sub" style="padding:7px 14px">Scans 13 chart-pattern families at once. Trades only when the optimized family-consensus rule for that market fires.</div>'+
      bots.map(renderPatternBotCard).join('');
  }
  async function togglePatternBot(k){
    let s; try{s=await(await fetch('/api/patternbots/state')).json();}catch(e){return;}
    const bot=(s.bots||[]).find(x=>x.key===k); if(!bot) return;
    await fetch('/api/patternbots/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k,enabled:!bot.enabled})});
    loadPatternBots();
  }
  async function resetPatternBot(k){
    if(!confirm('Reset this all-pattern paper account to $100?'))return;
    await fetch('/api/patternbots/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})});
    loadPatternBots();
  }

  // ================= NEWS PAPER BOT (rule-based news -> paper trades; $10k; BTC/ETH/SOL) =================
  const NP_TS='border-collapse:collapse;width:100%;font-size:11px';
  const NP_TH='text-align:left;padding:3px 7px;color:var(--muted);border-bottom:1px solid #243049;white-space:nowrap';
  const NP_TD='padding:3px 7px;border-bottom:1px solid #18223a;white-space:nowrap';
  const NP_FIELDS=[['starting_balance','Starting balance',10],['leverage','Leverage (x)',1],
    ['max_open_trades','Max open trades',1],['max_daily_loss_pct','Max daily loss %',0.5],
    ['min_impact_score','Min impact score',0.5],['min_confidence','Min confidence',0.05],
    ['stop_loss_pct','Stop loss %',0.05],['take_profit_pct','Take profit %',0.05],
    ['dup_cooldown_min','Dup cooldown (min)',1],['min_confirmation_move_pct','Min confirm move %',0.01],
    ['max_already_moved_pct','Max already-moved %',0.05],['max_hold_min','Max hold (min)',1]];
  const NP_SAMPLES=['BREAKING: Israel launches missile strike on Iran','US CPI comes in hotter than expected',
    'SEC approves spot Ethereum ETF','Random analyst says Bitcoin may go up','Solana network suffers major outage'];
  function npF(v){ return (v>=0?'+':'')+(Math.round(v*100)/100); }
  function npHeadHTML(s){
    const on=s.enabled; const dot=on?'on':'off';
    return '<div class="bhead"><span class="dot '+dot+'"></span>'+
      '<span class="bname" style="color:#60a5fa">🟦 News Paper Bot (paper · BTC/ETH/SOL · $100 · 20x · rule-based · NO real trading)</span>'+
      '<span class="badge" style="background:'+(on?'#16351f':'#3a1f1f')+';color:'+(on?'#6ee7a0':'#f6a0a0')+'">'+(on?'ON':'OFF')+'</span>'+
      '<span class="spacer"></span>'+
      '<button class="btn '+(on?'off':'on')+'" onclick="'+(on?'npStop()':'npStart()')+'">'+(on?'Stop Bot':'Start Bot')+'</button>'+
      '<button class="btn reset" onclick="npReset()">Reset Account</button></div>';
  }
  function npStatsHTML(s){
    const t=(k,v,c)=>'<div class="stat"><div class="k">'+k+'</div><div class="v '+(c||'')+'">'+v+'</div></div>';
    const dp=s.day_pnl>=0?'pos':'neg', tp=s.total_pnl>=0?'pos':'neg';
    return '<div class="stats">'+
      t('Balance',money(s.balance))+ t('Equity',money(s.equity))+
      t('Day P&L',npF(s.day_pnl),dp)+ t('Total P&L',npF(s.total_pnl)+' ('+npF(s.total_pnl_pct)+'%)',tp)+
      t('Win rate',(s.win_rate||0)+'% ('+s.wins+'/'+s.num_trades+')')+ t('Trades',s.num_trades)+
      t('Max DD',(s.max_drawdown||0)+'%')+ t('Avg win',npF(s.avg_win),'pos')+ t('Avg loss',npF(s.avg_loss),'neg')+
      t('Open',s.open_count+' / '+s.max_open)+'</div>';
  }
  function npPositions(s){
    if(!s.positions||!s.positions.length) return '<div class="empty">No open positions.</div>';
    const r=s.positions.map(p=>{const up=p.upnl>=0;return '<tr><td style="'+NP_TD+'">'+esc(p.symbol)+'</td>'+
      '<td style="'+NP_TD+'" class="'+(p.direction==='LONG'?'pos':'neg')+'">'+p.direction+'</td>'+
      '<td style="'+NP_TD+'">'+px1(p.entry)+'</td><td style="'+NP_TD+'">'+(p.current!=null?px1(p.current):'—')+'</td>'+
      '<td style="'+NP_TD+'">$'+Math.round(p.notional).toLocaleString()+'</td><td style="'+NP_TD+'">'+px1(p.sl)+'</td>'+
      '<td style="'+NP_TD+'">'+px1(p.tp)+'</td><td style="'+NP_TD+';color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+npF(p.upnl)+'</td>'+
      '<td style="'+NP_TD+'">'+tstr(p.open_ts)+'</td></tr>';}).join('');
    return '<div style="overflow:auto;max-height:160px"><table style="'+NP_TS+'"><tr><th style="'+NP_TH+'">Symbol</th><th style="'+NP_TH+'">Dir</th><th style="'+NP_TH+'">Entry</th><th style="'+NP_TH+'">Now</th><th style="'+NP_TH+'">Size</th><th style="'+NP_TH+'">SL</th><th style="'+NP_TH+'">TP</th><th style="'+NP_TH+'">uPnL</th><th style="'+NP_TH+'">Opened</th></tr>'+r+'</table></div>';
  }
  function npTrades(s){
    if(!s.trades||!s.trades.length) return '<div class="empty">No closed trades yet.</div>';
    const r=s.trades.map(t=>'<tr><td style="'+NP_TD+'">'+esc(t.symbol)+'</td>'+
      '<td style="'+NP_TD+'" class="'+(t.direction==='LONG'?'pos':'neg')+'">'+t.direction+'</td>'+
      '<td style="'+NP_TD+'">'+px1(t.entry)+'</td><td style="'+NP_TD+'">'+px1(t.exit)+'</td>'+
      '<td style="'+NP_TD+';color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+npF(t.pnl)+'</td>'+
      '<td style="'+NP_TD+';color:'+(t.pnl>=0?'var(--green)':'var(--red)')+'">'+npF(t.pnl_pct)+'%</td>'+
      '<td style="'+NP_TD+'">'+esc(t.reason)+'</td><td style="'+NP_TD+';max-width:240px;overflow:hidden;text-overflow:ellipsis">'+esc(t.headline||'')+'</td>'+
      '<td style="'+NP_TD+'">'+tstr(t.close_ts)+'</td></tr>').join('');
    return '<div style="overflow:auto;max-height:200px"><table style="'+NP_TS+'"><tr><th style="'+NP_TH+'">Symbol</th><th style="'+NP_TH+'">Dir</th><th style="'+NP_TH+'">Entry</th><th style="'+NP_TH+'">Exit</th><th style="'+NP_TH+'">PnL</th><th style="'+NP_TH+'">PnL%</th><th style="'+NP_TH+'">Reason</th><th style="'+NP_TH+'">Headline</th><th style="'+NP_TH+'">Closed</th></tr>'+r+'</table></div>';
  }
  function npDecisions(s){
    if(!s.decisions||!s.decisions.length) return '<div class="empty">No decisions yet. Press Start, or use Test News below.</div>';
    const r=s.decisions.map(d=>{const tr=d.decision==='TRADE';return '<tr>'+
      '<td style="'+NP_TD+'">'+tstr(d.ts)+'</td><td style="'+NP_TD+'">'+esc((d.source||'').slice(0,16))+'</td>'+
      '<td style="'+NP_TD+';max-width:260px;overflow:hidden;text-overflow:ellipsis">'+esc(d.headline)+'</td>'+
      '<td style="'+NP_TD+'">'+esc(d.category)+'</td><td style="'+NP_TD+'">'+esc(d.symbol||'-')+'</td>'+
      '<td style="'+NP_TD+'" class="'+(d.direction==='LONG'?'pos':(d.direction==='SHORT'?'neg':''))+'">'+d.direction+'</td>'+
      '<td style="'+NP_TD+'">'+d.impact+'</td><td style="'+NP_TD+'">'+d.confidence+'</td>'+
      '<td style="'+NP_TD+';font-weight:700;color:'+(tr?'#6ee7a0':'var(--muted)')+'">'+d.decision+'</td>'+
      '<td style="'+NP_TD+';max-width:300px;overflow:hidden;text-overflow:ellipsis">'+esc(d.reason)+(d.result?' · <b>'+esc(d.result)+'</b>':'')+'</td></tr>';}).join('');
    return '<div style="overflow:auto;max-height:260px"><table style="'+NP_TS+'"><tr><th style="'+NP_TH+'">Time</th><th style="'+NP_TH+'">Source</th><th style="'+NP_TH+'">Headline</th><th style="'+NP_TH+'">Category</th><th style="'+NP_TH+'">Symbol</th><th style="'+NP_TH+'">Dir</th><th style="'+NP_TH+'">Score</th><th style="'+NP_TH+'">Conf</th><th style="'+NP_TH+'">Decision</th><th style="'+NP_TH+'">Reason</th></tr>'+r+'</table></div>';
  }
  function npNews(s){
    if(!s.recent_news||!s.recent_news.length) return '<div class="empty">No news processed yet.</div>';
    return '<div style="overflow:auto;max-height:120px;font-size:11px;padding:2px 4px">'+
      s.recent_news.map(n=>'<div class="det" style="padding:2px 0"><span>'+tstr(n.ts)+' · <span class="muted">'+esc((n.source||'').slice(0,16))+'</span> '+esc(n.headline)+'</span></div>').join('')+'</div>';
  }
  function npSettingsHTML(s){
    const inp=NP_FIELDS.map(f=>'<label style="display:flex;flex-direction:column;font-size:10.5px;color:var(--muted);gap:2px">'+f[1]+
      '<input id="np-set-'+f[0]+'" type="number" step="'+f[2]+'" value="'+s.settings[f[0]]+'" style="width:120px;background:#0e1626;border:1px solid #243049;color:#dfe7f5;border-radius:5px;padding:4px 6px;font-size:12px"></label>').join('');
    return '<div class="ph">Settings</div><div style="display:flex;flex-wrap:wrap;gap:9px;padding:6px 14px">'+inp+
      '</div><div style="padding:0 14px 8px"><button class="btn on" onclick="npSaveSettings()">Save settings</button>'+
      '<span id="np-set-msg" class="muted small" style="margin-left:8px"></span></div>';
  }
  function npTestHTML(s){
    const btns=NP_SAMPLES.map((h,i)=>'<button class="btn" style="font-size:10.5px" onclick="npFill('+i+')">'+esc(h.slice(0,22))+'…</button>').join(' ');
    return '<div class="ph">Test News (paste a headline → see TRADE / SKIP)</div>'+
      '<div style="padding:6px 14px;display:flex;flex-direction:column;gap:6px">'+
      '<textarea id="np-test-head" rows="2" placeholder="Paste a news headline…" style="width:100%;background:#0e1626;border:1px solid #243049;color:#dfe7f5;border-radius:6px;padding:6px 8px;font-size:13px"></textarea>'+
      '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">source: '+
      '<input id="np-test-src" value="Test (Reuters)" style="background:#0e1626;border:1px solid #243049;color:#dfe7f5;border-radius:5px;padding:4px 6px;width:150px;font-size:12px">'+
      '<button class="btn on" onclick="npSendTest()">Send Test News</button></div>'+
      '<div style="display:flex;gap:6px;flex-wrap:wrap">samples: '+btns+'</div>'+
      '<div id="np-test-result" style="font-size:12px;color:#cfe0ff"></div></div>';
  }

  async function loadPenny(){
    let s; try{ s=await(await fetch('/api/penny/state')).json(); }catch(e){ return; }
    const panel=$('penny-panel'); if(!panel) return;
    const dot = s.scanner_always_on ? 'live' : 'off';
    const tgl = s.enabled?'Pause entries':'Enable entries'; const tglCls = s.enabled?'on':'off';
    const pos = (s.positions||[]).map(p=>
      '<tr><td><b>'+esc(p.ticker)+'</b><div class="sub" style="font-size:10px">'+esc(p.name)+'</div></td>'+
      '<td>'+p.qty+'</td><td>$'+p.entry+'</td><td>$'+p.price+'</td>'+
      '<td>$'+p.stop+(p.trailing?' <span class="badge live">trail</span>':'')+'</td>'+
      '<td>$'+p.tp+'</td>'+
      '<td class="'+(p.pnl>=0?'g':'r')+'">'+fmt(p.pnl)+' ('+p.pnl_pct+'%)</td>'+
      '<td class="sub">'+p.held_days+'d</td></tr>').join('');
    const wl = (s.watchlist||[]).map(w=>{
      const sig=w.signal||{}, action=sig.action||'--';
      const v = w.rejected ? '<span class="badge" style="background:#7f1d1d">REJECTED</span>'
             : (action==='BUY'||action==='STRONG BUY' ? '<span class="badge live">'+esc(action)+'</span>'
             : action==='RESEARCH' ? '<span class="badge" style="background:#1d4f73">RESEARCH</span>'
             : action==='WATCH' ? '<span class="badge">WATCH</span>'
             : '<span class="badge" style="background:#374151">'+esc(action)+'</span>');
      const cf=w.confirmation||{};
      const why = w.rejected ? esc(w.rejected) : esc(sig.why||'');
      const cat = (w.catalysts||[]).length ? '<div class="sub" style="color:#fbbf24;font-size:10px">'+esc(w.catalysts[0])+'</div>':'';
      return '<tr><td><b>'+esc(w.ticker)+'</b></td><td>$'+w.price+'</td>'+
             '<td class="'+(w.spread_pct>4?'r':'')+'">'+w.spread_pct+'%</td>'+
             '<td>'+v+(sig.candidate_action==='BUY'||sig.candidate_action==='STRONG BUY'
               ?'<div class="sub">confirm '+(cf.observations||0)+'/'+(cf.required||2)+'</div>':'')+
             '</td><td class="sub">'+why+cat+'</td></tr>';}).join('');
    const hist = (s.history||[]).slice(0,12).map(h=>
      '<tr><td><b>'+esc(h.ticker)+'</b></td><td>'+h.qty+'</td><td>$'+h.entry+'</td><td>$'+h.exit+'</td>'+
      '<td class="'+(h.pnl>=0?'g':'r')+'">'+fmt(h.pnl)+' ('+h.pnl_pct+'%)</td>'+
      '<td class="sub">'+esc(h.reason)+'</td><td class="sub">-$'+h.spread_cost+'</td></tr>').join('');
    panel.innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">AI Penny Stock (paper)</span>'+
        '<span class="badge">US penny stocks - '+esc(s.ai_model||'AI')+'</span>'+
        '<span class="badge live">scanner live</span>'+
        '<span class="badge">'+(s.enabled?'entries on':'entries paused')+'</span>'+
        '<span class="badge '+(s.market_open?'live':'')+'">'+(s.market_open?'market open':'market closed')+'</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="togglePenny()">'+tgl+'</button>'+

        '<button class="btn reset" onclick="resetPenny()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      '<div class="sub" style="padding:0 14px 6px">open '+s.open_count+'/'+s.max_open+
        ' - scans '+s.scan_count+' - today '+fmt(s.day_pnl)+
        ' - next scan '+(s.scan_in_progress?'running':(s.next_scan_in_sec||0)+'s')+
        ' - <span style="color:#f87171">spread paid '+fmt(-Math.abs(s.spread_paid||0))+'</span></div>'+
      '<div class="sub" style="padding:0 14px 6px">forward edge: '+
        esc((s.forward_validation||{}).status||'COLLECTING')+' - '+
        esc((s.forward_validation||{}).reason||'waiting for evidence')+'</div>'+
      '<div class="sub" style="padding:0 14px 6px">validation power: '+
        esc((((s.forward_validation||{}).feasibility||{}).summary)||
            'not assessable: no exact prospective outcomes')+'</div>'+
      '<div class="sub" style="padding:0 14px 8px;color:#fbbf24">'+esc(s.rules||'')+'</div>'+
      (s.last_error?'<div class="sub" style="padding:0 14px 8px;color:var(--red)">'+esc(s.last_error)+'</div>':'')+
      '<div class="sub" style="padding:0 14px 10px;color:#9ca3af">'+esc(s.note||'')+'</div>'+
      '<div class="ph">Open positions</div>'+
      (pos?'<table class="tbl"><tr><th>ticker</th><th>qty</th><th>entry</th><th>now</th><th>stop</th><th>target</th><th>P&L</th><th>held</th></tr>'+pos+'</table>':'<div class="empty">no open positions</div>')+
      '<div class="ph">Latest scan - AI verdicts and rejections</div>'+
      (wl?'<table class="tbl"><tr><th>ticker</th><th>price</th><th>spread</th><th>verdict</th><th>reason / catalyst</th></tr>'+wl+'</table>':'<div class="empty">no scan yet</div>')+
      '<div class="ph">Closed trades</div>'+
      (hist?'<table class="tbl"><tr><th>ticker</th><th>qty</th><th>entry</th><th>exit</th><th>P&L</th><th>why</th><th>spread</th></tr>'+hist+'</table>':'<div class="empty">no closed trades</div>')+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>';
  }
  async function togglePenny(){ let s; try{s=await(await fetch('/api/penny/state')).json();}catch(e){return;} await fetch('/api/penny/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadPenny(); }
  async function resetPenny(){ if(!confirm('Reset the AI penny stock paper account to $100?'))return; await fetch('/api/penny/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadPenny(); }
  async function pennyScan(){ await fetch('/api/penny/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadPenny(); }
  async function loadNewsPaper(){
    let s; try{ s=await(await fetch('/api/newspaper/state')).json(); }catch(e){ return; }
    const panel=$('newspaper-panel'); if(!panel) return;
    if(!s.running){ panel.innerHTML='<div class="bhead"><span class="bname" style="color:#60a5fa">🟦 News Paper Bot</span></div><div class="empty">bot not running</div>'; return; }
    if(!document.getElementById('np-stats')){           // build static shell ONCE (so inputs do not get wiped)
      panel.innerHTML='<div id="np-head"></div><div id="np-stats"></div>'+
        '<div class="ph">Open positions</div><div id="np-positions"></div>'+
        '<div class="ph">Closed trades</div><div id="np-trades"></div>'+
        '<div class="ph">Decision log — every news, TRADE or SKIP with reason</div><div id="np-decisions"></div>'+
        '<div class="ph">Recent news processed</div><div id="np-news"></div>'+
        npSettingsHTML(s)+npTestHTML(s);
    }
    document.getElementById('np-head').innerHTML=npHeadHTML(s);
    document.getElementById('np-stats').innerHTML=npStatsHTML(s);
    document.getElementById('np-positions').innerHTML=npPositions(s);
    document.getElementById('np-trades').innerHTML=npTrades(s);
    document.getElementById('np-decisions').innerHTML=npDecisions(s);
    document.getElementById('np-news').innerHTML=npNews(s);
  }
  async function npStart(){ await fetch('/api/newspaper/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNewsPaper(); }
  async function npStop(){ await fetch('/api/newspaper/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNewsPaper(); }
  async function npReset(){ if(!confirm('Reset the News Paper Bot account (clear trades + decisions, restore starting balance)?'))return; await fetch('/api/newspaper/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNewsPaper(); }
  function npFill(i){ const t=document.getElementById('np-test-head'); if(t) t.value=NP_SAMPLES[i]; }
  async function npSaveSettings(){
    const o={}; NP_FIELDS.forEach(f=>{const el=document.getElementById('np-set-'+f[0]); if(el) o[f[0]]=parseFloat(el.value);});
    await fetch('/api/newspaper/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)});
    const m=document.getElementById('np-set-msg'); if(m){ m.textContent='saved ✓'; setTimeout(()=>m.textContent='',1500); }
    loadNewsPaper();
  }
  async function npSendTest(){
    const h=(document.getElementById('np-test-head')||{}).value||''; const src=(document.getElementById('np-test-src')||{}).value||'Test (Reuters)';
    if(!h.trim()) return;
    let r; try{ r=await(await fetch('/api/newspaper/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({headline:h,source:src})})).json(); }catch(e){ return; }
    const d=r.decision||{}; const el=document.getElementById('np-test-result');
    if(el) el.innerHTML='<b style="color:'+(d.decision==='TRADE'?'#6ee7a0':'#f6c177')+'">'+(d.decision||'?')+'</b> · '+esc(d.symbol||'-')+' '+esc(d.direction||'')+' · score '+(d.impact)+' · conf '+(d.confidence)+' · '+esc(d.category||'')+'<br><span class="muted">'+esc(d.reason||'')+'</span>';
    loadNewsPaper();
  }

  // ================= per-bot "up since reset" timer badge (all panels) =================
  const BOT_PANELS={'news-panel':'newsbot','sniper-panel':'sniperbot','arb-panel':'arb','cryptalmaker-panel':'cryptalmaker','cryptalgelmaker-panel':'cryptalgelmaker','cryptalgeo-panel':'cryptalgeo','fv-panel':'fv',
    'trend-panel':'trend','ainews-panel':'ainews','tvstrats-panel':'tvstrats','meanrev-panel':'meanrev',
    'newsmomo-panel':'newsmomo','rsi2noatr-panel':'rsi2noatr','rsi2atr-panel':'rsi2atr','newspaper-panel':'newspaper','ictsm-panel':'ictsm','ict-panel':'ictbot','ictfreq-panel':'ictfreqbot',
    'freq-panel':'freqbot','freqtp-panel':'freqtpbot','freqtrend-panel':'freqtrendbot','freq5-panel':'freq5bot',
    'freqtf-panel':'freqtfbot','ts-panel':'tsbot','apexvwap-panel':'apexvwap','lucidcont-panel':'lucidcont','lucidpass-panel':'lucidpass','nqmr15-panel':'nqmr15','nr7-panel':'nr7','nr7aggr-panel':'nr7aggr','cot-panel':'cotbot','nw-panel':'nwbot','onchain-panel':'onchainbot'};
  function upStr(s){ s=Math.max(0,Math.floor(s)); const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),ss=s%60;
    return d>0?(d+'d '+h+'h '+m+'m'):(h>0?(h+'h '+m+'m'):(m>0?(m+'m '+ss+'s'):(ss+'s'))); }
  async function paintUptimes(){
    let map; try{ map=await(await fetch('/api/uptimes')).json(); }catch(e){ return; }
    for(const pid in BOT_PANELS){ const secs=map[BOT_PANELS[pid]]; const panel=document.getElementById(pid);
      // set a custom property on the panel ELEMENT (not its children) -> the ::after footer
      // shows it and is never wiped by innerHTML re-renders. JSON.stringify quotes it for CSS content.
      if(panel&&secs!=null) panel.style.setProperty('--uptime', JSON.stringify('⏱ up '+upStr(secs)));
    }
  }
  setInterval(paintUptimes,1000); paintUptimes();

  // Pin any paper bot visually. Stored locally and re-applied after panel refreshes.
  const PIN_KEY='paper_pinned_bot_panels';
  const PIN_SEED_KEY='paper_pinned_bot_panels_seed_v5';
  const DEFAULT_PINNED_BOTS=['lucidcont-panel','lucidpass-panel','nqmr15-panel','nr7-panel','nr7aggr-panel','apexvwap-panel','ictsm-panel'];
  function getPinnedBots(){
    let set;
    try{set=new Set(JSON.parse(localStorage.getItem(PIN_KEY)||'[]'));}catch(e){set=new Set();}
    try{
      if(localStorage.getItem(PIN_SEED_KEY)!=='1'){
        DEFAULT_PINNED_BOTS.forEach(id=>set.add(id));
        localStorage.setItem(PIN_KEY, JSON.stringify(Array.from(set)));
        localStorage.setItem(PIN_SEED_KEY, '1');
      }
    }catch(e){
      DEFAULT_PINNED_BOTS.forEach(id=>set.add(id));
    }
    return set;
  }
  function savePinnedBots(set){
    try{localStorage.setItem(PIN_KEY, JSON.stringify(Array.from(set)));}catch(e){}
  }
  function togglePaperPin(id){
    const set=getPinnedBots();
    if(set.has(id)) set.delete(id); else set.add(id);
    savePinnedBots(set);
    applyPaperPins();
  }
  function applyPaperPins(){
    const set=getPinnedBots();
    document.querySelectorAll('#wrap .bot[id]').forEach(panel=>{
      const pinned=set.has(panel.id);
      panel.classList.toggle('pinned', pinned);
      const head=panel.querySelector('.bhead');
      if(!head || head.querySelector('.pinbtn')) return;
      const btn=document.createElement('button');
      btn.type='button';
      btn.className='pinbtn'+(pinned?' on':'');
      btn.title='Pin/highlight this paper bot';
      btn.textContent='📌';
      btn.onclick=function(ev){ev.preventDefault(); ev.stopPropagation(); togglePaperPin(panel.id);};
      const spacer=head.querySelector('.spacer');
      head.insertBefore(btn, spacer || head.firstChild);
    });
    document.querySelectorAll('#wrap .bot[id] .pinbtn').forEach(btn=>{
      const panel=btn.closest('.bot[id]');
      btn.classList.toggle('on', !!(panel && set.has(panel.id)));
    });
  }
  let pinRAF=0;
  const pinRoot=document.getElementById('wrap');
  if(pinRoot){
    new MutationObserver(function(){
      if(pinRAF) return;
      pinRAF=requestAnimationFrame(function(){pinRAF=0; applyPaperPins();});
    }).observe(pinRoot,{childList:true,subtree:true});
  }
  setInterval(applyPaperPins,1000); applyPaperPins();

  function onchainSignalBlock(s){
    const rows=(s.signals||[]).slice(0,10);
    if(!rows.length) return '<div class="empty">No on-chain signals yet. First run arms scanners at the latest block.</div>';
    return rows.map(x=>{
      const cls=(Number(x.score||0)>=0)?'open':'loss';
      const usd=x.usd?(' - '+money(x.usd)):'';
      return '<div class="ln '+cls+'"><span class="lt">'+tstr(x.t)+'</span><span><b>'+fmt(x.score)+'</b> '+
        esc((x.chain?x.chain+' - ':'')+(x.label||''))+usd+'</span></div>';
    }).join('');
  }
  function onchainWalletBlock(s){
    const rows=(s.learned_wallets||[]).slice(0,8);
    if(!rows.length) return '<div class="empty">No proven wallets learned yet.</div>';
    return rows.map(w=>'<div class="ln open"><span class="lt">'+fmt(w.score)+'</span><span><b>'+
      esc(String(w.addr||'').slice(0,8)+'...'+String(w.addr||'').slice(-6))+'</b></span></div>').join('');
  }
  async function loadOnchain(){
    let s; try{ s=await(await fetch('/api/onchainbot/state')).json(); }catch(e){ return; }
    if(!s.running){ $('onchain-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">On-chain Radar (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable';
    const tglCls=s.enabled?'on':'off';
    const chains=(s.chains||[]).map(c=>{
      if(c.error) return c.chain+' error';
      if(c.latest&&c.cursor) return c.chain+' '+Math.max(0, c.latest-c.cursor)+' blk behind';
      return c.chain+' armed';
    }).join(' | ');
    const bias='score '+fmt(s.score)+' / '+String(s.bias||'flat').toUpperCase();
    const comps=(s.components||[]).slice(0,2).map(c=>fmt(c.score)+' '+c.label).join(' | ');
    const errTxt=s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    $('onchain-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">On-chain Radar (paper)</span>'+
        '<span class="badge">'+esc(s.timeframe||'event stream')+' - perp '+(s.leverage||20)+'x</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleOnchain()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetOnchain()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+(s.enabled?'scanning':'paused')+' - '+esc(bias)+(chains?(' - '+esc(chains)):'')+(comps?(' - '+esc(comps)):'')+'</div>'+
      errTxt+
      '<div class="ph">Open position</div>'+posBlock(s)+
      '<div class="ph">Recent on-chain signals</div><div class="feed">'+onchainSignalBlock(s)+'</div>'+
      '<div class="ph">Learned wallets</div><div class="feed">'+onchainWalletBlock(s)+'</div>'+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
    if(s.price) $('px').textContent='BTC '+money(s.price);
  }
  async function toggleOnchain(){ let s; try{s=await(await fetch('/api/onchainbot/state')).json();}catch(e){return;} await fetch('/api/onchainbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadOnchain(); }
  async function resetOnchain(){ if(!confirm('Reset the On-chain Radar paper account?'))return; await fetch('/api/onchainbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadOnchain(); }

  function polyHistoryBlock(s, cls, sgn){
    const rows=s.history||[];
    if(!rows.length) return '<div class="empty">No closed positions yet. Copied sells/resolved markets will appear here.</div>';
    const body=rows.map(h=>{
      const pnl=Number(h.pnl||0);
      const when=h.t?new Date(Number(h.t)*1000).toLocaleTimeString():'-';
      return '<div class="poly-row">'+
        '<span class="poly-num">'+when+'</span>'+
        '<span><span class="side '+(h.outcome==='Yes'?'long':'short')+'">'+esc(h.outcome||'-')+'</span></span>'+
        '<span class="poly-num">'+(h.entry!=null?h.entry:'-')+' -> '+(h.exit!=null?h.exit:'-')+'</span>'+
        '<span class="poly-num '+cls(pnl)+'">'+sgn(pnl.toFixed(4))+'</span>'+
        '<span>'+esc(h.reason||'closed')+'</span>'+
        '<span class="poly-title" title="'+esc(h.title||'')+'">'+esc(h.title||'')+'</span>'+
      '</div>';
    }).join('');
    return '<div class="poly-closed"><div class="poly-head"><span>closed</span><span>outcome</span><span>entry -> exit</span><span>P&L</span><span>reason</span><span>market</span></div>'+body+'</div>';
  }

  async function loadPoly(){
    let s; try{ s=await(await fetch('/api/polybot/state')).json(); }catch(e){ return; }
    const panel=document.getElementById('poly-panel'); if(!panel) return;
    const cls=v=>Number(v)>=0?'pos':'neg'; const sgn=v=>(Number(v)>=0?'+':'')+v;
    const posRows=(s.positions||[]).map(p=>'<div class="posrow" style="padding:5px 14px"><div class="top"><span class="side '+(p.outcome==='Yes'?'long':'short')+'">'+esc(p.outcome)+'</span> <span>$'+p.value+'</span> <span class="'+cls(p.upnl)+'">'+sgn(p.upnl)+'</span></div><div class="muted" style="font-size:11px">cur '+p.cur+' · '+esc(p.title)+'</div></div>').join('')||'<div class="empty">no open positions yet — waiting for @huskyvs to trade</div>';
    const hist=(s.history||[]).map(h=>'<div class="det" style="padding:2px 14px"><span><span class="side '+(h.outcome==='Yes'?'long':'short')+'">'+esc(h.outcome)+'</span> '+h.entry+'→'+h.exit+' <span class="muted">'+esc(h.title||'')+'</span></span> <span class="'+cls(h.pnl)+'">'+sgn(h.pnl)+'</span></div>').join('')||'<div class="empty">no closed trades yet</div>';
    const opens=(s.opens||[]).map(o=>'<div class="det" style="padding:2px 14px"><span class="muted">'+esc(String(o.msg))+'</span></div>').join('');
    const closedWindow=polyHistoryBlock(s, cls, sgn);
    panel.innerHTML='<div class="bhead"><span class="dot '+(s.running?'on':'off')+'"></span><span class="bname">🟣 Polymarket Copy · @'+esc(s.username||'huskyvs')+'</span><span class="spacer"></span><button class="btn reset" onclick="resetPoly()">reset</button></div>'+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      '<div class="sub" style="padding:2px 14px 8px">Balance <b>$'+s.balance+'</b> · Equity <b>$'+s.equity+'</b> · P&L <b class="'+cls(s.pnl)+'">'+sgn(s.pnl)+' ('+sgn(s.pnl_pct)+'%)</b> · '+s.trades+' copied trades · WR '+s.win_rate+'%</div>'+
      '<div class="ph">Open positions ('+(s.positions||[]).length+')</div><div class="feed">'+posRows+'</div>'+
      '<div class="ph">Closed positions / copied sells ('+(s.history||[]).length+')</div>'+closedWindow+
      '<div class="ph">Activity</div><div class="feed">'+opens+'</div>';
  }
  async function resetPoly(){ if(!confirm('Reset the Polymarket copy paper account to $100?'))return; await fetch('/api/polybot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadPoly(); }

  function renderFreqPanel(panel, s, title, toggleFn, resetFn){
    if(!s.running){ $(panel).innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">'+esc(title)+'</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable';
    const tglCls=s.enabled?'on':'off';
    let ind='';
    if(s.rsi!=null) ind='RSI '+s.rsi;
    if(s.bb_lower!=null) ind+=(ind?' · ':'')+'BB-low '+px1(s.bb_lower);
    if(s.ema_fast!=null&&s.ema_slow!=null) ind+=(ind?' · ':'')+'EMA '+px1(s.ema_fast)+'/'+px1(s.ema_slow);
    if(s.htf_trend) ind+=(ind?' · ':'')+'1h '+String(s.htf_trend).toUpperCase();
    if(s.flow_pct!=null) ind+=(ind?' · ':'')+'flow '+s.flow_pct+'% buy';
    const errTxt = s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    $(panel).innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">'+esc(title)+'</span>'+
        '<span class="badge">'+(s.timeframe||'5m')+' · perp '+(s.leverage||20)+'x</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="'+toggleFn+'()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="'+resetFn+'()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+(s.enabled?'live':'paused')+(ind?(' · '+esc(ind)):'')+'</div>'+
      errTxt+
      '<div class="ph">Open position</div>'+posBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function loadFreq(){
    let s; try{ s=await(await fetch('/api/freqbot/state')).json(); }catch(e){ return; }
    renderFreqPanel('freq-panel', s, 'Freqtrade-style (paper)', 'toggleFreq', 'resetFreq');
  }
  async function toggleFreq(){ let s; try{s=await(await fetch('/api/freqbot/state')).json();}catch(e){return;} await fetch('/api/freqbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadFreq(); }
  async function resetFreq(){ if(!confirm('Reset the Freqtrade-style paper account?'))return; await fetch('/api/freqbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadFreq(); }

  async function loadFreqTP(){
    let s; try{ s=await(await fetch('/api/freqtpbot/state')).json(); }catch(e){ return; }
    renderFreqPanel('freqtp-panel', s, 'Freqtrade-style (paper improved TP/SL)', 'toggleFreqTP', 'resetFreqTP');
  }
  async function toggleFreqTP(){ let s; try{s=await(await fetch('/api/freqtpbot/state')).json();}catch(e){return;} await fetch('/api/freqtpbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadFreqTP(); }
  async function resetFreqTP(){ if(!confirm('Reset the improved TP/SL Freqtrade paper account?'))return; await fetch('/api/freqtpbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadFreqTP(); }

  async function loadFreqTrend(){
    let s; try{ s=await(await fetch('/api/freqtrendbot/state')).json(); }catch(e){ return; }
    renderFreqPanel('freqtrend-panel', s, 'Freqtrade-style (paper trend TP/SL)', 'toggleFreqTrend', 'resetFreqTrend');
  }
  async function toggleFreqTrend(){ let s; try{s=await(await fetch('/api/freqtrendbot/state')).json();}catch(e){return;} await fetch('/api/freqtrendbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadFreqTrend(); }
  async function resetFreqTrend(){ if(!confirm('Reset the trend TP/SL Freqtrade paper account?'))return; await fetch('/api/freqtrendbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadFreqTrend(); }

  async function loadFreq5(){
    let s; try{ s=await(await fetch('/api/freq5bot/state')).json(); }catch(e){ return; }
    renderFreqPanel('freq5-panel', s, 'Freqtrade-style (improved TP/SL 5%)', 'toggleFreq5', 'resetFreq5');
  }
  async function toggleFreq5(){ let s; try{s=await(await fetch('/api/freq5bot/state')).json();}catch(e){return;} await fetch('/api/freq5bot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadFreq5(); }
  async function resetFreq5(){ if(!confirm('Reset the improved TP/SL 5% Freqtrade paper account?'))return; await fetch('/api/freq5bot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadFreq5(); }

  async function loadFreqTF(){
    let s; try{ s=await(await fetch('/api/freqtfbot/state')).json(); }catch(e){ return; }
    renderFreqPanel('freqtf-panel', s, 'Freqtrade-style (1m trend+flow confirmed 5%)', 'toggleFreqTF', 'resetFreqTF');
  }
  async function toggleFreqTF(){ let s; try{s=await(await fetch('/api/freqtfbot/state')).json();}catch(e){return;} await fetch('/api/freqtfbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadFreqTF(); }
  async function resetFreqTF(){ if(!confirm('Reset the trend+flow confirmed Freqtrade paper account?'))return; await fetch('/api/freqtfbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadFreqTF(); }

  async function loadTS(){
    let s; try{ s=await(await fetch('/api/tsbot/state')).json(); }catch(e){ return; }
    if(!s.running){ $('ts-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">Trend-Sweep VWAP (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable';
    const tglCls=s.enabled?'on':'off';
    const trendTxt = s.trend?('trend '+s.trend.toUpperCase()):'trend —';
    let lvl='';
    if(s.pdl!=null) lvl='PDL '+px1(s.pdl)+' · PDH '+px1(s.pdh);
    if(s.vwap!=null) lvl+=(lvl?' · ':'')+'VWAP '+px1(s.vwap);
    const dayTxt='today '+(s.trades_today||0)+'/3';
    const errTxt = s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    $('ts-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">Trend-Sweep VWAP (paper)</span>'+
        '<span class="badge">'+esc(trendTxt)+'</span>'+
        '<span class="badge">4H·5m</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleTS()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetTS()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+(s.enabled?'live':'paused')+(lvl?(' · '+esc(lvl)):'')+' · '+dayTxt+'</div>'+
      errTxt+
      '<div class="ph">Open position</div>'+posBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleTS(){ let s; try{s=await(await fetch('/api/tsbot/state')).json();}catch(e){return;} await fetch('/api/tsbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadTS(); }
  async function resetTS(){ if(!confirm('Reset the Trend-Sweep paper account?'))return; await fetch('/api/tsbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadTS(); }

  function apexPosBlock(s){
    if(!s.positions || !s.positions.length) return '<div class="empty">No open ES position.</div>';
    return s.positions.map(p=>{
      const up=p.pnl>=0;
      const extra=(p.pnl_R!=null)?(' ('+fmt(p.pnl_R)+'R)'):'';
      return '<div class="posrow"><div class="top">'+
        '<span class="side '+p.side+'">'+p.side.toUpperCase()+' - '+(p.qty||1)+' ES contract</span>'+
        '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl)+extra+'</span></div>'+
        '<div class="det"><span>entry '+px1(p.entry)+'</span><span>stop '+px1(p.stop)+'</span><span>+1R '+px1(p.tp1)+'</span><span>target '+px1(p.tp2)+'</span></div>'+
        (p.news?'<div class="det" style="white-space:normal"><span style="color:var(--muted)">'+esc(p.news)+'</span></div>':'')+
        '</div>';
    }).join('');
  }
  async function loadApexVWAP(){
    let s; try{ s=await(await fetch('/api/apexvwap/state')).json(); }catch(e){ return; }
    if(!s.running){ $('apexvwap-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">Apex ES VWAP ORB (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable';
    const tglCls=s.enabled?'on':'off';
    const sn=s.snap||{};
    const ind='ES '+px1(sn.es)+' - OR '+px1(sn.or_low)+'/'+px1(sn.or_high)+' - VWAP '+px1(sn.vwap)+' - EMA '+px1(sn.ema9)+'/'+px1(sn.ema20)+' - breakout '+esc(sn.breakout||'none');
    const apex='target left '+money(s.target_left)+' - drawdown room '+money(s.drawdown_room)+' - EOD threshold '+money(s.eod_threshold)+' - today '+fmt(s.day_pnl)+' - trades '+(s.trades_today||0)+'/'+(s.max_trades_day||2);
    const errTxt=s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    $('apexvwap-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">Apex ES VWAP ORB (paper)</span>'+
        '<span class="badge">ES 5m - NQ confirm - 1 contract</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleApexVWAP()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetApexVWAP()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+' - '+ind+'</div>'+
      '<div class="sub" style="padding:0 14px 8px">'+apex+'</div>'+
      errTxt+
      '<div class="ph">Open position</div>'+apexPosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleApexVWAP(){ let s; try{s=await(await fetch('/api/apexvwap/state')).json();}catch(e){return;} await fetch('/api/apexvwap/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadApexVWAP(); }
  async function resetApexVWAP(){ if(!confirm('Reset the Apex ES VWAP ORB paper account to $50,000?'))return; await fetch('/api/apexvwap/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadApexVWAP(); }

  function nr7SetupBlock(s){
    const r=(s.setups||[]);
    if(!r.length) return '<div class="empty">No NR7 setup armed today (only fires the day after a narrowest-range-of-7 session).</div>';
    return r.map(x=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side '+(x.fired?'long':'')+'">'+esc(x.mkt)+(x.fired?' · fired':' · armed')+'</span>'+
      '<span style="color:var(--muted)">range '+px1(x.range)+'</span></div>'+
      '<div class="det"><span>buy &gt; '+px1(x.hi)+'</span><span>sell &lt; '+px1(x.lo)+'</span></div></div>').join('');
  }
  function nr7PosBlock(s){
    const r=(s.positions||[]);
    if(!r.length) return '<div class="empty">No open position.</div>';
    return r.map(p=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side '+p.side+'">'+esc(p.mkt)+' '+p.side.toUpperCase()+' ×'+p.qty+'</span>'+
      '<span style="color:'+(p.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl)+' ('+fmt(p.pnl_R)+'R)</span></div>'+
      '<div class="det"><span>'+esc(p.news||'')+'</span><span>@'+px1(p.entry)+' · stop '+px1(p.stop)+' · tgt '+px1(p.tp2)+'</span></div></div>').join('');
  }
  function lucidSetupBlock(s){
    const r=(s.setups||[]);
    if(!r.length) return '<div class="empty">Waiting for ES/NQ/CL candles.</div>';
    return r.map(x=>'<div class="posrow" style="padding:6px 14px"><div class="top">'+
      '<span class="side">'+esc(x.mkt)+' - '+esc(x.name||'')+'</span>'+
      '<span style="color:var(--muted)">'+(x.price!=null?px1(x.price):'-')+'</span></div>'+
      '<div class="det"><span>'+esc(x.status||'scanning')+'</span></div></div>').join('');
  }
  function lucidFeedDetailsBlock(s){
    const r=(s.feed_details||[]);
    if(!r.length) return '';
    const parts=r.map(x=>{
      const label=(x.key||'').replace('_VWAP',' ').replace('_TURTLE',' ').replace('_NR7',' ');
      const when=x.latest_closed_tbilisi||x.latest_closed_utc||'no candles';
      const lag=x.lag_sec!=null ? ', lag '+Math.round(Number(x.lag_sec)/60)+'m' : '';
      const state=x.state==='outside_session'?'outside session':(x.stale?'STALE':(x.state||'fresh'));
      return esc(label+': '+when+' '+state+lag);
    });
    return '<div class="sub" style="padding:0 14px 8px;color:#93c5fd">latest exact candles: '+parts.join(' | ')+'</div>';
  }
  function lucidDot(s){
    const status=String(s.status||'').toLowerCase();
    const err=String(s.data_error||'').toLowerCase();
    if(!s.enabled || s.failed) return 'off';
    if(status.includes('blocked') || err.includes('stale') || err.includes('required') || err.includes('differs')) return 'watch';
    return 'on';
  }
  async function loadLucidCont(){
    let s; try{ s=await(await fetch('/api/lucidcont/state')).json(); }catch(e){ return; }
    if(!s.running){ $('lucidcont-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">Lucid 50K Monthly Pass Basket - Continuous (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=lucidDot(s);
    const invalid=!!s.strategy_invalidated;
    const tgl=invalid?'Invalidated':(s.enabled?'Pause':'Enable'); const tglCls=s.enabled?'on':'off';
    const ntf=s.telegram_enabled?'TG On':'TG Off'; const ntfCls=s.telegram_enabled?'on':'off';
    const dailyStop=Number(s.daily_loss_limit)>=999999?'daily stop off':'daily stop '+money(s.daily_loss_limit);
    const guard='target left '+money(s.target_left)+' - drawdown room '+money(s.drawdown_room)+' - floor '+money(s.floor)+' - risk/trade '+money(s.risk_per_trade)+' - '+dailyStop+' - today '+fmt(s.day_pnl);
    const phase='<span class="badge '+(s.passed?'live':'')+'">'+esc(s.phase||'')+'</span>';
    const errTxt=s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    const tgTxt=s.telegram_enabled ? ('signals -> '+esc(s.telegram_target||'Telegram')+(s.telegram_ready?' ready':' waiting for Telegram connection')) : 'signals off';
    const feedTxt=s.live_feed_status? '<div class="sub" style="padding:0 14px 8px;color:#7fb5ff">feed: '+esc(s.live_feed_status)+'</div>':'';
    const feedDetailTxt=lucidFeedDetailsBlock(s);
    const identityTxt=s.strategy_fingerprint ? '<div class="sub" style="padding:0 14px 8px;color:#a7f3d0">strategy: '+esc(s.strategy_version||'')+' / '+esc(s.strategy_fingerprint)+'</div>' : '';
    const sourceTxt=s.source_match_required ? '<div class="sub" style="padding:0 14px 8px;color:'+(s.source_match?'#22c55e':'var(--red)')+'">source: backtest '+esc(s.backtest_feed_family||'unknown')+' / live '+esc(s.live_feed_family||'unknown')+' - '+(s.source_match?'match':'DIFFERS')+'</div>' : '';
    const rtTxt='<div class="sub" style="padding:0 14px 8px;color:'+(s.exact_realtime_ready?'#22c55e':'#facc15')+'">realtime: '+(s.exact_realtime_ready?'READY':'not ready')+' - '+esc(s.exact_realtime_status||'')+'</div>';
    const entryTxt=s.realtime_entry_required ? '<div class="sub" style="padding:0 14px 8px;color:'+(s.realtime_entry_ready?'#22c55e':'var(--red)')+'">entries: '+(s.realtime_entry_ready?'allowed':'blocked')+' - '+esc(s.realtime_entry_status||'')+'</div>' : '';
    const tgErr=s.telegram_error? '<div class="sub" style="padding:0 14px 8px;color:var(--red)">Telegram: '+esc(s.telegram_error)+'</div>':'';
    const nextTxt=s.next_signal_window? '<div class="sub" style="padding:0 14px 8px;color:#facc15">'+esc(s.next_signal_window)+'</div>':'';
    const note=s.backtest_note? '<div class="sub" style="padding:0 14px 8px;color:var(--red)">'+esc(s.backtest_note)+'</div>':'';
    $('lucidcont-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">Lucid 50K Monthly Pass Basket - Continuous (paper)</span>'+
        '<span class="badge">'+esc(s.symbols||'')+' - '+esc(s.timeframe||'')+'</span>'+phase+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleLucidCont()" '+(invalid?'disabled':'')+'>'+tgl+'</button>'+
        '<button class="btn '+ntfCls+'" onclick="toggleLucidContNotify()">'+ntf+'</button>'+
        '<button class="btn reset" onclick="resetLucidCont()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      '<div class="sub" style="padding:0 14px 8px">'+guard+'</div>'+
      '<div class="sub" style="padding:0 14px 8px">'+tgTxt+'</div>'+
      feedTxt+
      feedDetailTxt+
      identityTxt+
      sourceTxt+
      rtTxt+
      entryTxt+
      nextTxt+
      note+
      tgErr+
      errTxt+
      '<div class="ph">Component scanners</div>'+lucidSetupBlock(s)+
      '<div class="ph">Open positions</div>'+nr7PosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleLucidCont(){ let s; try{s=await(await fetch('/api/lucidcont/state')).json();}catch(e){return;} await fetch('/api/lucidcont/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadLucidCont(); }
  async function toggleLucidContNotify(){ let s; try{s=await(await fetch('/api/lucidcont/state')).json();}catch(e){return;} await fetch('/api/lucidcont/notify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.telegram_enabled})}); loadLucidCont(); }
  async function resetLucidCont(){ if(!confirm('Reset the Lucid continuous paper account to $50,000? The invalidated strategy will remain paused.'))return; await fetch('/api/lucidcont/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadLucidCont(); }

  async function loadLucidPass(){
    let s; try{ s=await(await fetch('/api/lucidpass/state')).json(); }catch(e){ return; }
    if(!s.running){ $('lucidpass-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">Lucid 50K Monthly Pass Basket - Execution Audited (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=lucidDot(s);
    const invalid=!!s.strategy_invalidated;
    const tgl=invalid?'Invalidated':(s.enabled?'Pause':'Enable'); const tglCls=s.enabled?'on':'off';
    const ntf=s.telegram_enabled?'TG On':'TG Off'; const ntfCls=s.telegram_enabled?'on':'off';
    const dailyStop=Number(s.daily_loss_limit)>=999999?'daily stop off':'daily stop '+money(s.daily_loss_limit);
    const guard='target left '+money(s.target_left)+' - drawdown room '+money(s.drawdown_room)+' - floor '+money(s.floor)+' - risk/trade '+money(s.risk_per_trade)+' - '+dailyStop+' - today '+fmt(s.day_pnl);
    const phase='<span class="badge '+(s.passed?'live':'')+'">'+esc(s.phase||'')+'</span>';
    const errTxt=s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    const tgTxt=s.telegram_enabled ? ('signals -> '+esc(s.telegram_target||'Telegram')+(s.telegram_ready?' ready':' waiting for Telegram connection')) : 'signals off';
    const feedTxt=s.live_feed_status? '<div class="sub" style="padding:0 14px 8px;color:#7fb5ff">feed: '+esc(s.live_feed_status)+'</div>':'';
    const feedDetailTxt=lucidFeedDetailsBlock(s);
    const identityTxt=s.strategy_fingerprint ? '<div class="sub" style="padding:0 14px 8px;color:#a7f3d0">strategy: '+esc(s.strategy_version||'')+' / '+esc(s.strategy_fingerprint)+'</div>' : '';
    const sourceTxt=s.source_match_required ? '<div class="sub" style="padding:0 14px 8px;color:'+(s.source_match?'#22c55e':'var(--red)')+'">source: backtest '+esc(s.backtest_feed_family||'unknown')+' / live '+esc(s.live_feed_family||'unknown')+' - '+(s.source_match?'match':'DIFFERS')+'</div>' : '';
    const rtTxt='<div class="sub" style="padding:0 14px 8px;color:'+(s.exact_realtime_ready?'#22c55e':'#facc15')+'">realtime: '+(s.exact_realtime_ready?'READY':'not ready')+' - '+esc(s.exact_realtime_status||'')+'</div>';
    const entryTxt=s.realtime_entry_required ? '<div class="sub" style="padding:0 14px 8px;color:'+(s.realtime_entry_ready?'#22c55e':'var(--red)')+'">entries: '+(s.realtime_entry_ready?'allowed':'blocked')+' - '+esc(s.realtime_entry_status||'')+'</div>' : '';
    const tgErr=s.telegram_error? '<div class="sub" style="padding:0 14px 8px;color:var(--red)">Telegram: '+esc(s.telegram_error)+'</div>':'';
    const nextTxt=s.next_signal_window? '<div class="sub" style="padding:0 14px 8px;color:#facc15">'+esc(s.next_signal_window)+'</div>':'';
    const note=s.backtest_note? '<div class="sub" style="padding:0 14px 8px;color:var(--red)">'+esc(s.backtest_note)+'</div>':'';
    $('lucidpass-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">Lucid 50K Monthly Pass Basket - Execution Audited (paper)</span>'+
        '<span class="badge">'+esc(s.symbols||'')+' - '+esc(s.timeframe||'')+'</span>'+phase+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleLucidPass()" '+(invalid?'disabled':'')+'>'+tgl+'</button>'+
        '<button class="btn '+ntfCls+'" onclick="toggleLucidPassNotify()">'+ntf+'</button>'+
        '<button class="btn reset" onclick="resetLucidPass()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      '<div class="sub" style="padding:0 14px 8px">'+guard+'</div>'+
      '<div class="sub" style="padding:0 14px 8px">'+tgTxt+'</div>'+
      feedTxt+
      feedDetailTxt+
      identityTxt+
      sourceTxt+
      rtTxt+
      entryTxt+
      nextTxt+
      note+
      tgErr+
      errTxt+
      '<div class="ph">Component scanners</div>'+lucidSetupBlock(s)+
      '<div class="ph">Open positions</div>'+nr7PosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleLucidPass(){ let s; try{s=await(await fetch('/api/lucidpass/state')).json();}catch(e){return;} await fetch('/api/lucidpass/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadLucidPass(); }
  async function toggleLucidPassNotify(){ let s; try{s=await(await fetch('/api/lucidpass/state')).json();}catch(e){return;} await fetch('/api/lucidpass/notify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.telegram_enabled})}); loadLucidPass(); }
  async function resetLucidPass(){ if(!confirm('Reset the execution-audited Lucid pass ledger to $50,000? Its audited trade history will be cleared.'))return; await fetch('/api/lucidpass/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadLucidPass(); }

  async function loadNQMR15(){
    let s; try{ s=await(await fetch('/api/nqmr15/state')).json(); }catch(e){ return; }
    if(!s.running){ $('nqmr15-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">NQ 15m Mean Reversion (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable'; const tglCls=s.enabled?'on':'off';
    const apex='target left '+money(s.target_left)+' - drawdown room '+money(s.drawdown_room)+' - floor '+money(s.floor)+' - risk/trade '+money(s.risk_per_trade)+' - today '+fmt(s.day_pnl);
    const phase='<span class="badge '+(s.trail_locked?'live':'')+'">'+esc(s.phase||'')+'</span>';
    const errTxt=s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    const note=s.backtest_note? '<div class="sub" style="padding:0 14px 8px;color:#fbbf24">'+esc(s.backtest_note)+'</div>':'';
    $('nqmr15-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">NQ 15m Mean Reversion (paper)</span>'+
        '<span class="badge">'+esc(s.symbols||'')+' - '+esc(s.timeframe||'')+' - VWAP2s + TurtleSoup + 80-20</span>'+phase+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleNQMR15()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetNQMR15()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      '<div class="sub" style="padding:0 14px 8px">'+apex+'</div>'+
      note+
      errTxt+
      '<div class="ph">Open positions</div>'+nr7PosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleNQMR15(){ let s; try{s=await(await fetch('/api/nqmr15/state')).json();}catch(e){return;} await fetch('/api/nqmr15/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadNQMR15(); }
  async function resetNQMR15(){ if(!confirm('Reset the NQ 15m Mean Reversion paper account to $50,000? It will stay enabled.'))return; await fetch('/api/nqmr15/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNQMR15(); }

  async function loadNR7(){
    let s; try{ s=await(await fetch('/api/nr7/state')).json(); }catch(e){ return; }
    if(!s.running){ $('nr7-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">⭐ NR7 Breakout Apex (ES+NQ+CL)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable'; const tglCls=s.enabled?'on':'off';
    const apex='target left '+money(s.target_left)+' · drawdown room '+money(s.drawdown_room)+' · floor '+money(s.floor)+' · risk/trade '+money(s.risk_per_trade)+' · today '+fmt(s.day_pnl);
    const phase='<span class="badge '+(s.trail_locked?'live':'')+'">'+esc(s.phase||'')+'</span>';
    const errTxt=s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    $('nr7-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">⭐ NR7 Breakout Apex (ES+NQ+CL)</span>'+
        '<span class="badge">'+esc(s.symbols||'')+' · '+esc(s.timeframe||'')+'</span>'+phase+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleNR7()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetNR7()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      '<div class="sub" style="padding:0 14px 8px">'+apex+'</div>'+
      errTxt+
      '<div class="ph">NR7 setups today</div>'+nr7SetupBlock(s)+
      '<div class="ph">Open positions</div>'+nr7PosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleNR7(){ let s; try{s=await(await fetch('/api/nr7/state')).json();}catch(e){return;} await fetch('/api/nr7/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadNR7(); }
  async function resetNR7(){ if(!confirm('Reset the NR7 Apex paper account to $50,000?'))return; await fetch('/api/nr7/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNR7(); }

  async function loadNR7Aggr(){
    let s; try{ s=await(await fetch('/api/nr7aggr/state')).json(); }catch(e){ return; }
    if(!s.running){ $('nr7aggr-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">⚡ NR7 Aggressive (NR7 + NQ reversion)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable'; const tglCls=s.enabled?'on':'off';
    const apex='target left '+money(s.target_left)+' · drawdown room '+money(s.drawdown_room)+' · floor '+money(s.floor)+' · risk/trade '+money(s.risk_per_trade)+' · today '+fmt(s.day_pnl);
    const phase='<span class="badge '+(s.trail_locked?'live':'')+'">'+esc(s.phase||'')+'</span>';
    const errTxt=s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    $('nr7aggr-panel').innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">⚡ NR7 Aggressive (NR7 + NQ reversion)</span>'+
        '<span class="badge">'+esc(s.symbols||'')+' · '+esc(s.timeframe||'')+'</span>'+phase+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleNR7Aggr()">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetNR7Aggr()">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+esc(s.status||'')+'</div>'+
      '<div class="sub" style="padding:0 14px 4px">'+apex+'</div>'+
      '<div class="sub" style="padding:0 14px 8px;color:#f59e0b">⚠ adds NQ VWAP-2σ + Turtle-Soup reversion for speed — single-market & overfit-suspect, expect choppier live than backtest.</div>'+
      errTxt+
      '<div class="ph">NR7 setups today</div>'+nr7SetupBlock(s)+
      '<div class="ph">Open positions</div>'+nr7PosBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleNR7Aggr(){ let s; try{s=await(await fetch('/api/nr7aggr/state')).json();}catch(e){return;} await fetch('/api/nr7aggr/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadNR7Aggr(); }
  async function resetNR7Aggr(){ if(!confirm('Reset the NR7 Aggressive paper account to $50,000?'))return; await fetch('/api/nr7aggr/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadNR7Aggr(); }

  async function loadOB(tf){
    const panel='ob-panel-'+tf;
    let s; try{ s=await(await fetch('/api/obbot/state?tf='+tf)).json(); }catch(e){ return; }
    if(!s.running){ $(panel).innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">OB / Smart-Money '+tf+' (paper)</span></div><div class="empty">bot not running</div>'; return; }
    const dot=s.enabled?'on':'off';
    const tgl=s.enabled?'Pause':'Enable';
    const tglCls=s.enabled?'on':'off';
    const trendTxt=s.trend?('trend '+String(s.trend).toUpperCase()):'trend —';
    let ind='';
    if(s.rsi!=null) ind='RSI '+s.rsi;
    if(s.vwap!=null) ind+=(ind?' · ':'')+'VWAP '+px1(s.vwap);
    const errTxt = s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
    $(panel).innerHTML =
      '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">OB / Smart-Money '+tf+' (paper)</span>'+
        '<span class="badge">'+esc(trendTxt)+'</span>'+
        '<span class="badge">'+tf+' · perp ATR-lev</span>'+
        '<span class="spacer"></span>'+
        '<button class="btn '+tglCls+'" onclick="toggleOB(\''+tf+'\')">'+tgl+'</button>'+
        '<button class="btn reset" onclick="resetOB(\''+tf+'\')">reset</button></div>'+
      statHead(s)+
      '<div class="sub" style="padding:7px 14px">'+(s.enabled?'live':'paused')+(ind?(' · '+esc(ind)):'')+' · loosened entry (smart-money OR order-block)</div>'+
      errTxt+
      '<div class="ph">Open position</div>'+posBlock(s)+
      '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
      '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
  }
  async function toggleOB(tf){ let s; try{s=await(await fetch('/api/obbot/state?tf='+tf)).json();}catch(e){return;} await fetch('/api/obbot/toggle?tf='+tf,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadOB(tf); }
  async function resetOB(tf){ if(!confirm('Reset the OB / Smart-Money '+tf+' paper account?'))return; await fetch('/api/obbot/reset?tf='+tf,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadOB(tf); }

  function loadCOT(){
    return fetch('/api/cotbot/state').then(r=>r.json()).then(s=>{
      if(!s.running){ $('cot-panel').innerHTML='<div class="bhead"><span class="dot off"></span><span class="bname">COT Crowded-Positioning Fade</span></div><div class="empty">bot not running</div>'; return; }
      const dot=s.enabled?'on':'off';
      const tgl=s.enabled?'Pause':'Enable';
      const tglCls=s.enabled?'on':'off';
      const crowd=s.crowd?String(s.crowd).replaceAll('_',' '):'crowd -';
      let ind='';
      if(s.score!=null) ind='crowd score '+fmt(s.score);
      if(s.rsi_d!=null) ind+=(ind?' Â· ':'')+'daily RSI '+s.rsi_d;
      if(s.ema_fast!=null&&s.ema_slow!=null) ind+=(ind?' Â· ':'')+'15m EMA '+px1(s.ema_fast)+'/'+px1(s.ema_slow);
      const errTxt = s.data_error? '<div class="sub" style="padding:6px 14px;color:var(--red)">data: '+esc(s.data_error)+'</div>':'';
      $('cot-panel').innerHTML =
        '<div class="bhead"><span class="dot '+dot+'"></span><span class="bname">COT Crowded-Positioning Fade</span>'+
          '<span class="badge">'+esc(crowd)+'</span>'+
          '<span class="badge">'+(s.timeframe||'15m')+' Â· perp '+(s.leverage||10)+'x</span>'+
          '<span class="spacer"></span>'+
          '<button class="btn '+tglCls+'" onclick="toggleCOT()">'+tgl+'</button>'+
          '<button class="btn reset" onclick="resetCOT()">reset</button></div>'+
        statHead(s)+
        '<div class="sub" style="padding:7px 14px">'+(s.enabled?'live':'paused')+(ind?(' Â· '+esc(ind)):'')+' Â· fades crowded positioning after reversal confirmation</div>'+
        errTxt+
        '<div class="ph">Open position</div>'+posBlock(s)+
        '<div class="ph">Activity</div><div class="feed">'+logBlock(s)+'</div>'+
        '<div class="ph">Closed trades</div><div class="hist">'+histBlock(s)+'</div>';
    }).catch(e=>{});
  }
  async function toggleCOT(){ let s; try{s=await(await fetch('/api/cotbot/state')).json();}catch(e){return;} await fetch('/api/cotbot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadCOT(); }
  async function resetCOT(){ if(!confirm('Reset the COT paper account?'))return; await fetch('/api/cotbot/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); loadCOT(); }

  function loadAll(){ loadLucidCont(); loadLucidPass(); loadNQMR15(); loadNR7(); loadNR7Aggr(); loadApexVWAP(); loadNewsAI(); loadSniper(); loadArb(); loadCryptalMaker(); loadCryptalGelMaker(); loadCryptalGeo(); loadFV(); loadTrend(); loadAINews(); loadClaudeHaiku(); loadTVStrats(); loadMeanRev(); loadNewsMomo(); loadRSI2NoATR(); loadRSI2ATR(); loadPatternBots(); loadNewsPaper(); loadICTSM(); loadICT(); loadICTFreq(); loadFreq(); loadFreqTP(); loadFreqTrend(); loadFreq5(); loadFreqTF(); loadTS(); loadOB('1m'); loadOB('5m'); loadOB('15m'); loadCOT(); loadNW(); loadOnchain(); loadPoly(); }
  loadAll(); setInterval(loadAll, 2000);
</script>
</body>
</html>
"""


async def start_dashboard(market=None, broker=None, nwbot=None,
                          stock_market=None, stock_bot=None, lighter=None,
                          lighter_bot=None, lighter_lock=None, ict_bot=None, ict_freq_bot=None,
                          freq_bot=None,
                          freq_tp_bot=None, freq_trend_bot=None,
                          freq_5_bot=None, freq_tf_bot=None,
                          trend_sweep_bot=None, apex_vwap_bot=None, lucid_cont_bot=None,
                          lucid_pass_bot=None, ob_bots=None, cot_bot=None,
                          onchain_bot=None, poly_bot=None, ict_sm_bot=None, nq_mr_15m_bot=None, nr7_bot=None,
                          nr7_aggr_bot=None):
    """Start the web server inside the bot's asyncio loop."""
    global _MARKET, _BROKER, _NWBOT, _STOCK_MARKET, _STOCK_BOT, _LIGHTER, _LIGHTERBOT, _LIGHTER_LOCK
    global _ICTBOT, _ICTFREQBOT, _FREQBOT, _FREQTPBOT, _FREQTRENDBOT, _FREQ5BOT, _FREQTFBOT, _TSBOT, _APEXVWAPBOT, _LUCIDCONTBOT, _LUCIDPASSBOT, _NQMR15BOT, _OB_BOTS, _COTBOT, _ONCHAINBOT, _POLYBOT
    global _ICTSMBOT, _NR7BOT, _NR7AGGRBOT
    _MARKET, _BROKER, _NWBOT = market, broker, nwbot
    _ICTBOT = ict_bot
    _ICTSMBOT = ict_sm_bot
    _ICTFREQBOT = ict_freq_bot
    _FREQBOT = freq_bot
    _FREQTPBOT = freq_tp_bot
    _FREQTRENDBOT = freq_trend_bot
    _FREQ5BOT = freq_5_bot
    _FREQTFBOT = freq_tf_bot
    _TSBOT = trend_sweep_bot
    _APEXVWAPBOT = apex_vwap_bot
    _LUCIDCONTBOT = lucid_cont_bot
    _LUCIDPASSBOT = lucid_pass_bot
    _NQMR15BOT = nq_mr_15m_bot
    _NR7BOT = nr7_bot
    _NR7AGGRBOT = nr7_aggr_bot
    _OB_BOTS = ob_bots or {}
    _COTBOT = cot_bot
    _ONCHAINBOT = onchain_bot
    _POLYBOT = poly_bot
    _STOCK_MARKET, _STOCK_BOT = stock_market, stock_bot
    _LIGHTER = lighter
    _LIGHTERBOT = lighter_bot
    if lighter_lock is not None:        # share ONE lock with the bot so signed calls never collide
        _LIGHTER_LOCK = lighter_lock
    WHALES.on_add = push_trade          # push every new trade to the browser instantly (SSE)
    app = web.Application(middlewares=[_uptime_mw, _scroll_keep_mw])
    app.add_routes([
        web.get("/", _index),
        web.get("/api/uptimes", _uptimes),
        web.get("/orderbook", _book_page),
        web.get("/copytrade", _copy_page),
        web.get("/api/copytrade", _copy_state),
        web.post("/api/copytrade/reset", _copy_reset),
        web.get("/api/candles", _candles),
        web.get("/ict", _ictsm_page),
        web.get("/api/ictsm", _ictsm_data),
        web.get("/api/marketstats", _marketstats),
        web.get("/api/news", _news),
        web.get("/api/news/stream", _news_stream),
        web.get("/treeofalpha", _toa_page),
        web.get("/api/toa", _toa),
        web.get("/api/toa/stream", _toa_stream),
        web.get("/api/orderbook", _orderbook),
        web.get("/api/whales", _whales),
        web.get("/api/whales/stream", _trades_stream),
        web.get("/api/manual/state", _manual_state),
        web.post("/api/manual/start", _manual_start),
        web.post("/api/manual/order", _manual_order),
        web.post("/api/manual/close", _manual_close),
        web.post("/api/manual/modify", _manual_modify),
        web.post("/api/manual/reset", _manual_reset),
        web.get("/api/bot/state", _bot_state),
        web.post("/api/bot/reset", _bot_reset),
        web.get("/api/nwbot/state", _nw_state),
        web.post("/api/nwbot/toggle", _nw_toggle),
        web.post("/api/nwbot/reset", _nw_reset),
        web.post("/api/nwbot/strategy", _nw_strategy),
        web.get("/api/newsbot/state", _newsbot_state),
        web.post("/api/newsbot/toggle", _newsbot_toggle),
        web.post("/api/newsbot/reset", _newsbot_reset),
        web.post("/api/newsbot/mode", _newsbot_mode),
        web.get("/api/sniperbot/state", _sniperbot_state),
        web.post("/api/sniperbot/toggle", _sniperbot_toggle),
        web.post("/api/sniperbot/reset", _sniperbot_reset),
        web.post("/api/sniperbot/mode", _sniperbot_mode),
        web.get("/api/arb/state", _arb_state),
        web.post("/api/arb/toggle", _arb_toggle),
        web.post("/api/arb/reset", _arb_reset),
        web.post("/api/arb/lev", _arb_lev),
        web.get("/airdrops", _airdrops_page),
        web.get("/api/airdrops/state", _airdrops_state),
        web.post("/api/airdrops/bankroll", _airdrops_bankroll),
        web.post("/api/airdrops/refresh", _airdrops_refresh),
        web.get("/api/cryptalmaker/state", _cryptal_maker_state),
        web.post("/api/cryptalmaker/toggle", _cryptal_maker_toggle),
        web.post("/api/cryptalmaker/reset", _cryptal_maker_reset),
        web.get("/api/cryptalgelmaker/state", _cryptal_gel_maker_state),
        web.post("/api/cryptalgelmaker/toggle", _cryptal_gel_maker_toggle),
        web.post("/api/cryptalgelmaker/reset", _cryptal_gel_maker_reset),
        web.get("/api/cryptalgeo/state", _cryptal_geo_state),
        web.post("/api/cryptalgeo/toggle", _cryptal_geo_toggle),
        web.post("/api/cryptalgeo/reset", _cryptal_geo_reset),
        web.post("/api/cryptalgeo/scan", _cryptal_geo_scan),
        web.get("/api/fv/state", _fv_state),
        web.post("/api/fv/toggle", _fv_toggle),
        web.post("/api/fv/reset", _fv_reset),
        web.post("/api/fv/lev", _fv_lev),
        web.get("/api/trend/state", _trend_state),
        web.post("/api/trend/toggle", _trend_toggle),
        web.post("/api/trend/reset", _trend_reset),
        web.post("/api/trend/risk", _trend_risk),
        web.get("/api/ainews/state", _ainews_state),
        web.post("/api/ainews/toggle", _ainews_toggle),
        web.post("/api/ainews/reset", _ainews_reset),
        web.get("/api/claudehaiku/state", _claudehaiku_state),
        web.post("/api/claudehaiku/toggle", _claudehaiku_toggle),
        web.post("/api/claudehaiku/reset", _claudehaiku_reset),
        web.get("/api/tvstrats/state", _tvstrats_state),
        web.post("/api/tvstrats/toggle", _tvstrats_toggle),
        web.post("/api/tvstrats/reset", _tvstrats_reset),
        web.get("/api/meanrev/state", _meanrev_state),
        web.post("/api/meanrev/toggle", _meanrev_toggle),
        web.post("/api/meanrev/reset", _meanrev_reset),
        web.get("/api/newsmomo/state", _newsmomo_state),
        web.post("/api/newsmomo/toggle", _newsmomo_toggle),
        web.post("/api/newsmomo/reset", _newsmomo_reset),
        web.get("/api/rsi2noatr/state", _rsi2noatr_state),
        web.post("/api/rsi2noatr/toggle", _rsi2noatr_toggle),
        web.post("/api/rsi2noatr/reset", _rsi2noatr_reset),
        web.get("/api/rsi2atr/state", _rsi2atr_state),
        web.post("/api/rsi2atr/toggle", _rsi2atr_toggle),
        web.post("/api/rsi2atr/reset", _rsi2atr_reset),
        web.get("/api/patternbots/state", _patternbots_state),
        web.post("/api/patternbots/toggle", _patternbots_toggle),
        web.post("/api/patternbots/reset", _patternbots_reset),
        web.get("/api/newspaper/state", _newspaper_state),
        web.post("/api/newspaper/start", _newspaper_start),
        web.post("/api/newspaper/stop", _newspaper_stop),
        web.post("/api/newspaper/reset", _newspaper_reset),
        web.post("/api/newspaper/settings", _newspaper_settings),
        web.post("/api/newspaper/test", _newspaper_test),
        web.get("/penny", _penny_page),
        web.get("/api/penny/state", _penny_state),
        web.post("/api/penny/toggle", _penny_toggle),
        web.post("/api/penny/reset", _penny_reset),
        web.post("/api/penny/scan", _penny_scan),
        web.get("/paper", _paper_page),
        *lucid_lab_web.routes(),
        web.get("/ict", _ictlab_page),
        web.get("/api/ictlab/state", _ictlab_state),
        web.post("/api/ictlab/toggle", _ictlab_toggle),
        web.post("/api/ictlab/reset", _ictlab_reset),
        web.get("/api/ictbot/state", _ict_state),
        web.post("/api/ictbot/toggle", _ict_toggle),
        web.post("/api/ictbot/reset", _ict_reset),
        web.get("/api/ictsm/state", _ictsm_state),
        web.post("/api/ictsm/toggle", _ictsm_toggle),
        web.post("/api/ictsm/reset", _ictsm_reset),
        web.get("/api/ictfreqbot/state", _ictfreq_state),
        web.post("/api/ictfreqbot/toggle", _ictfreq_toggle),
        web.post("/api/ictfreqbot/reset", _ictfreq_reset),
        web.get("/api/freqbot/state", _freq_state),
        web.post("/api/freqbot/toggle", _freq_toggle),
        web.post("/api/freqbot/reset", _freq_reset),
        web.get("/api/freqtpbot/state", _freqtp_state),
        web.post("/api/freqtpbot/toggle", _freqtp_toggle),
        web.post("/api/freqtpbot/reset", _freqtp_reset),
        web.get("/api/freqtrendbot/state", _freqtrend_state),
        web.post("/api/freqtrendbot/toggle", _freqtrend_toggle),
        web.post("/api/freqtrendbot/reset", _freqtrend_reset),
        web.get("/api/freq5bot/state", _freq5_state),
        web.post("/api/freq5bot/toggle", _freq5_toggle),
        web.post("/api/freq5bot/reset", _freq5_reset),
        web.get("/api/freqtfbot/state", _freqtf_state),
        web.post("/api/freqtfbot/toggle", _freqtf_toggle),
        web.post("/api/freqtfbot/reset", _freqtf_reset),
        web.get("/api/tsbot/state", _ts_state),
        web.post("/api/tsbot/toggle", _ts_toggle),
        web.post("/api/tsbot/reset", _ts_reset),
        web.get("/api/apexvwap/state", _apexvwap_state),
        web.post("/api/apexvwap/toggle", _apexvwap_toggle),
        web.post("/api/apexvwap/reset", _apexvwap_reset),
        web.get("/api/lucidcont/state", _lucidcont_state),
        web.post("/api/lucidcont/toggle", _lucidcont_toggle),
        web.post("/api/lucidcont/notify", _lucidcont_notify),
        web.post("/api/lucidcont/reset", _lucidcont_reset),
        web.get("/api/lucidpass/state", _lucidpass_state),
        web.post("/api/lucidpass/toggle", _lucidpass_toggle),
        web.post("/api/lucidpass/notify", _lucidpass_notify),
        web.post("/api/lucidpass/reset", _lucidpass_reset),
        web.get("/api/nqmr15/state", _nqmr15_state),
        web.post("/api/nqmr15/toggle", _nqmr15_toggle),
        web.post("/api/nqmr15/reset", _nqmr15_reset),
        web.get("/api/nr7/state", _nr7_state),
        web.post("/api/nr7/toggle", _nr7_toggle),
        web.post("/api/nr7/reset", _nr7_reset),
        web.get("/api/nr7aggr/state", _nr7aggr_state),
        web.post("/api/nr7aggr/toggle", _nr7aggr_toggle),
        web.post("/api/nr7aggr/reset", _nr7aggr_reset),
        web.get("/api/obbot/state", _ob_state),
        web.post("/api/obbot/toggle", _ob_toggle),
        web.post("/api/obbot/reset", _ob_reset),
        web.get("/api/cotbot/state", _cot_state),
        web.post("/api/cotbot/toggle", _cot_toggle),
        web.post("/api/cotbot/reset", _cot_reset),
        web.get("/api/onchainbot/state", _onchain_state),
        web.post("/api/onchainbot/toggle", _onchain_toggle),
        web.post("/api/onchainbot/reset", _onchain_reset),
        web.get("/api/polybot/state", _poly_state),
        web.post("/api/polybot/reset", _poly_reset),
        web.get("/funding", _funding_page),
        web.get("/api/funding", _funding_state),
        web.post("/api/funding/toggle", _funding_toggle),
        web.post("/api/funding/reset", _funding_reset),
        web.get("/journal", _journal_page),
        web.get("/api/journal", _journal),
        web.get("/api/cvd", _cvd),
        web.get("/api/bigtrades", _bigtrades),
        web.get("/lighter", _lighter_page),
        web.get("/api/lighter/state", _lighter_state),
        web.post("/api/lighter/order", _lighter_order),
        web.post("/api/lighter/close", _lighter_close),
        web.post("/api/lighter/setlev", _lighter_setlev),
        web.get("/markets", _markets_page),
        web.get("/api/lighter/markets", _lighter_markets_api),
        web.get("/api/lighter/candles", _lighter_candles_api),
        web.get("/api/lighter/pnl", _lighter_pnl),
        web.get("/lighterbot", _lighterbot_page),
        web.get("/api/lighterbot/state", _lighterbot_state),
        web.post("/api/lighterbot/toggle", _lighterbot_toggle),
        web.post("/api/lighterbot/leverage", _lighterbot_leverage),
        web.post("/api/lighterbot/strategy", _lighterbot_strategy),
        web.post("/api/lighterbot/limitmode", _lighterbot_limitmode),
        web.get("/api/lighterbot/flow", _lighterbot_flow),
        web.post("/api/lighterbot/close", _lighterbot_close),
        web.post("/api/lighterbot/reset", _lighterbot_reset),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    global PORT
    PORT = _pick_free_port()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    if PORT != 8000:
        print(f"[dashboard] NOTE: port 8000 was blocked by Windows; using {PORT} instead.")
    asyncio.create_task(WHALES.run())          # live trade tape
    asyncio.create_task(_lighter_tape_feed())  # feed Lighter's own trades into that tape
    asyncio.create_task(_manual_mark_loop())   # mark manual positions
    asyncio.create_task(COPY.run())            # paper copy-trading of Hyperliquid wallets
    asyncio.create_task(FUNDING.run())         # funding-settlement timing bot (Lighter, paper)
    NEWSAI.attach(market)                       # give the AI news bot the live price feed
    asyncio.create_task(NEWSAI.manage_loop())   # AI news bot: mark/exit loop
    SNIPER.attach(market)                       # give the no-AI sniper the live price feed
    asyncio.create_task(SNIPER.manage_loop())   # no-AI sniper: mark/exit loop
    CROSSARB.attach(market, WHALES)             # cross-exchange arb: Binance + Hyperliquid prices
    asyncio.create_task(CROSSARB.manage_loop()) # cross-exchange arb: watch gap, hedge, converge
    asyncio.create_task(CRYPTALGEOSCANNER.manage_loop()) # every hedgeable Georgian-exchange market
    asyncio.create_task(GEORGIANVENUES.manage_loop()) # registered Georgian fixed-quote venues
    asyncio.create_task(CRYPTALMAKER.manage_loop()) # Cryptal maker fill -> Binance delta hedge (paper)
    asyncio.create_task(CRYPTALGELMAKER.manage_loop()) # Cryptal BTC-TOGEL maker -> Binance hedge (paper)
    asyncio.create_task(CRYPTALGEOBOT.manage_loop()) # one $100 collector follows the best non-BTC market
    asyncio.create_task(AIRDROPS.manage_loop())  # Airdrop Radar: rescan DeFiLlama for tokenless protocols
    FVTRACK.attach(market, WHALES)              # fair-value tracking: leaders' consensus vs Lighter
    asyncio.create_task(FVTRACK.manage_loop())  # fair-value tracking: trade Lighter's deviation -> FV
    TREND.attach(market)                        # Crypto Trend Breakout + ATR (daily, BTC/ETH/SOL)
    asyncio.create_task(TREND.manage_loop())    # daily trend-following: regime -> breakout/pullback -> trail
    AINEWS.attach(market)                       # AI News Trading Bot (Google News RSS -> LLM sentiment)
    asyncio.create_task(AINEWS.manage_loop())   # every ~60s: headlines -> sentiment score -> BTC position
    CLAUDEHAIKU.attach(market, WHALES)           # Claude Haiku live-feed news bot ($100/20x paper BTC)
    asyncio.create_task(CLAUDEHAIKU.manage_loop()) # every ~1s: mark open paper position + hit AI SL/TP
    TVSTRATS.attach(market)                     # 12 TradingView-style strategies ($100/20x each, BTC)
    asyncio.create_task(TVSTRATS.manage_loop()) # every ~60s: each strategy's signal -> all-in 20x paper trade
    MEANREV.attach(market)                      # Bollinger+RSI+200SMA mean reversion ($100/20x, BTC 1h)
    asyncio.create_task(MEANREV.manage_loop())  # every ~60s: lower-BB touch + RSI<35 + >200SMA -> long; exit 20SMA
    NEWSMOMO.attach(market)                     # news-momentum: 1.5s pre-news >=0.08% move -> bet direction ($100/20x)
    asyncio.create_task(NEWSMOMO.manage_loop()) # samples price ~3x/s + manages the 5% trailing exit; entries fire via on_news
    RSI2NOATR.attach(market)                    # RSI2 EMA50 scalper, no ATR filter ($100/10x, BTC 15m)
    asyncio.create_task(RSI2NOATR.manage_loop())# 15m RSI2 mean-reversion with daily stop and cooldown
    RSI2ATR.attach(market)                      # RSI2 EMA50 scalper, ATR normal-volatility filter ($100/10x)
    asyncio.create_task(RSI2ATR.manage_loop())  # 15m RSI2 mean-reversion with ATR filter, daily stop and cooldown
    for _pattern_bot in PATTERN_BOTS.values():
        _pattern_bot.attach(market)             # all-pattern consensus optimized paper bots (BTC + futures)
        asyncio.create_task(_pattern_bot.manage_loop())
    NEWSPAPER.attach(market)                    # News Paper Bot: rule-based news -> paper trades ($10k, BTC/ETH/SOL, blue panel)
    asyncio.create_task(PENNY.manage_loop())    # AI penny-stock scanner: screen -> dossier -> AI verdict -> paper trade
    asyncio.create_task(NEWSPAPER.manage_loop())# samples 3 symbols/s, marks positions, hits SL/TP/time; entries fire via on_news
    asyncio.create_task(_toa_listener())       # Tree of Alpha live news + Twitter feed (instant)
    print(f"[dashboard]  >>> Tree of Alpha live news:  http://{HOST}:{PORT}/treeofalpha")
    print(f"[dashboard]  >>> open in your browser:  http://{HOST}:{PORT}"
          f"   (crypto chart)   and   http://{HOST}:{PORT}/stocks   (stocks)")
    return runner


# ===========================================================================
# The web page (HTML + CSS + JS). Plain string so braces are literal.
# ===========================================================================
PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC · Chart + Paper Trading</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<script src="https://s3.tradingview.com/tv.js"></script>
<style>
  :root{
    --bg:#0a0b0e; --panel:#101217; --panel2:#15181f; --line:#1d212b;
    --txt:#d7dbe4; --muted:#737a89; --amber:#f5c518; --green:#26a69a; --red:#ef5350;
    --mono:'IBM Plex Mono', ui-monospace, monospace;
    --sans:'IBM Plex Sans', system-ui, sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans)}
  #topbar{display:flex;align-items:center;gap:14px;padding:12px 18px;
    border-bottom:1px solid var(--line);background:var(--panel)}
  .ticker{font-family:var(--mono);font-weight:600;font-size:18px;letter-spacing:.5px}
  .ticker .dot{color:var(--amber)}
  #topbar select{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
    border-radius:6px;padding:5px 9px;font-family:var(--mono);font-size:13px}
  .spacer{flex:1}
  #status{font-family:var(--mono);font-size:13px;color:var(--muted)}
  #count{font-family:var(--mono);font-size:13px;color:var(--amber)}
  .nav{font-family:var(--mono);font-size:13px;color:var(--amber);text-decoration:none;
    border:1px solid var(--line);padding:5px 10px;border-radius:6px}
  .nav:hover{background:var(--panel2)}
  .tradebtn{font-family:var(--mono);font-size:13px;color:#0a0b0e;background:var(--amber);
    border:none;cursor:pointer;padding:6px 12px;border-radius:6px;font-weight:600}
  .tradebtn:hover{filter:brightness(1.08)}
  #chartwrap{position:relative;height:64vh;min-height:380px;border-bottom:1px solid var(--line)}
  #chart{position:absolute;inset:0}
  .slrow{display:flex;gap:6px;align-items:center}
  .slrow input{flex:1}
  .setbtn{background:#1b1f29;border:1px solid var(--line);color:var(--amber);border-radius:5px;cursor:pointer;font-family:var(--mono);font-size:11px;padding:6px 8px;white-space:nowrap}
  .setbtn:hover{border-color:var(--amber)}
  .setbtn.arm{background:var(--amber);color:#0a0b0e;font-weight:600}
  .placehint{color:var(--muted);font-size:11px;line-height:1.4;font-family:var(--mono);margin-top:6px}
  .placehint.active{color:var(--amber)}
  .ovbtn{background:transparent;border:1px solid var(--line);color:var(--muted);border-radius:6px;cursor:pointer;font-family:var(--mono);font-size:12px;padding:6px 10px}
  .ovbtn:hover{border-color:var(--amber);color:var(--txt)}
  .ovbtn.on{background:var(--amber);color:#0a0b0e;border-color:var(--amber);font-weight:600}
  #cvdlabel{font-family:var(--mono);font-size:11px;color:#7da9c9;padding:4px 18px;background:var(--panel);border-bottom:1px solid var(--line);display:none}
  #mstats{font-family:var(--mono);font-size:12px;color:var(--muted);padding:5px 18px;background:var(--panel);border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap;row-gap:3px}
  #mstats .ms-sep{opacity:.35}
  #mstats b{font-weight:600;color:var(--txt)}
  #cvdlabel.show{display:block}
  #tvwrap{position:relative;height:64vh;min-height:380px;border-bottom:1px solid var(--line)}
  #tvchart{position:absolute;inset:0}
  .tvbtn{background:#243042;color:#cfe0f5;border:1px solid #2f3f56}
  .tvbtn:hover{background:#2c3a4f}
  .tvbtn.active{background:#1a6fd4;color:#fff;border-color:#1a6fd4}
  .soundbtn{background:transparent;border:1px solid var(--line);color:var(--txt);
    border-radius:6px;cursor:pointer;font-size:14px;padding:3px 9px;line-height:1.2}
  .soundbtn:hover{border-color:var(--amber)}
  .soundbtn.off{opacity:.45}
  .flowwin{background:#12161d;color:var(--txt);border:1px solid var(--line);border-radius:5px;
    padding:1px 5px;font-family:var(--mono);font-size:10px}
  #popup{position:absolute;max-width:340px;max-height:60%;overflow:auto;z-index:30;
    background:#0e1116ee;backdrop-filter:blur(4px);border:1px solid var(--line);
    border-left:3px solid var(--amber);border-radius:8px;padding:10px 12px;
    box-shadow:0 10px 30px #000a;display:none;font-size:13px;line-height:1.45}
  #popup .ph{font-family:var(--mono);color:var(--amber);font-size:12px;margin-bottom:4px}
  #popup .pb{color:var(--txt);white-space:pre-wrap}
  #popup .pn+.pn{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
  #popup .close{position:sticky;top:0;float:right;color:var(--muted);cursor:pointer;font-size:14px}
  #feedwrap{padding:10px 0 40px}
  #feedwrap h3{font-family:var(--mono);font-weight:500;font-size:13px;color:var(--muted);
    text-transform:uppercase;letter-spacing:1px;margin:14px 18px 8px}
  .item{display:grid;grid-template-columns:74px 96px 1fr;gap:10px;align-items:start;
    padding:8px 18px;border-bottom:1px solid #14171e}
  .item:hover{background:var(--panel)}
  .item .t{font-family:var(--mono);font-size:12px;color:var(--muted)}
  .b{font-family:var(--mono);font-size:11px;padding:2px 7px;border-radius:5px;
    text-align:center;white-space:nowrap}
  .b.skip{background:#1b1f29;color:#8b93a4}
  .b.news{background:#10202e;color:#58c1ff;border:1px solid #1c3447}
  .b.trade{background:#12251f;color:var(--green);border:1px solid #1e3a31}
  .b.trade.bear{background:#2a1416;color:var(--red);border-color:#3a1e20}
  .item .src{font-family:var(--mono);font-size:11px;color:#586173}
  .item .tx{font-size:13px;color:var(--txt)}
  .item .meta{display:flex;flex-direction:column;gap:3px}

  /* ---- manual paper trading drawer ---- */
  #tradepanel{position:fixed;top:0;right:0;width:352px;height:100vh;overflow:auto;
    background:#0c0e13;border-left:1px solid var(--line);z-index:50;display:none}
  body.trading{padding-right:352px}
  body.trading #tradepanel{display:block}
  .tp-head{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;
    border-bottom:1px solid var(--line);font-family:var(--mono);font-weight:600;font-size:14px;background:var(--panel)}
  .tp-x{cursor:pointer;color:var(--muted)}
  .tp-start{padding:16px;display:flex;flex-direction:column;gap:10px}
  .tp-start label{font-family:var(--mono);font-size:12px;color:var(--muted)}
  .tp-stats{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border-bottom:1px solid var(--line)}
  .tp-stat{background:#0c0e13;padding:11px 16px;font-family:var(--mono)}
  .tp-stat .l{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
  .tp-stat .v{font-size:15px;font-weight:600;margin-top:2px}
  .tp-form{padding:13px 16px;display:flex;flex-direction:column;gap:8px;border-bottom:1px solid var(--line)}
  .tp-form label{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
  .tp-form input, .tp-form select, .tp-start input{width:100%;background:#15181f;border:1px solid var(--line);
    border-radius:6px;color:var(--txt);padding:8px 9px;font-family:var(--mono);font-size:13px}
  .tp-pcts{display:flex;gap:6px}
  .tp-pcts button{flex:1;background:#15181f;border:1px solid var(--line);color:var(--muted);
    border-radius:6px;padding:5px;font-family:var(--mono);font-size:11px;cursor:pointer}
  .tp-pcts button:hover{color:var(--txt);border-color:var(--amber)}
  .tp-side{display:flex;gap:8px;margin-top:6px}
  .tp-side button{flex:1;border:none;border-radius:7px;padding:12px;font-family:var(--mono);
    font-weight:700;font-size:13px;cursor:pointer;color:#fff}
  .tp-long{background:#13a06a} .tp-long:hover{background:#15b074}
  .tp-short{background:#e0455a} .tp-short:hover{background:#ee4f64}
  .tp-start button{background:var(--amber);color:#0a0b0e;border:none;border-radius:7px;padding:11px;
    font-weight:700;font-family:var(--mono);cursor:pointer;font-size:13px}
  .tp-err{color:var(--red);font-size:12px;font-family:var(--mono);min-height:15px}
  .pos{padding:10px 16px;border-bottom:1px solid #14171e;font-family:var(--mono);font-size:12px}
  .pos .top{display:flex;justify-content:space-between;align-items:center}
  .lng{color:var(--green);font-weight:600} .sht{color:var(--red);font-weight:600}
  .pos .det{color:var(--muted);font-size:11px;margin-top:4px;display:flex;justify-content:space-between;gap:6px;align-items:center}
  .closebtn{background:#1b1f29;border:1px solid var(--line);color:var(--txt);border-radius:5px;
    padding:4px 10px;font-size:11px;cursor:pointer;font-family:var(--mono)}
  .closebtn:hover{border-color:var(--red);color:var(--red)}
  .tp-reset{text-align:center;color:var(--muted);font-size:11px;font-family:var(--mono);padding:12px;cursor:pointer}
  .tp-reset:hover{color:var(--red)}
  .tp-note{color:var(--muted);font-size:11px;line-height:1.45;font-family:var(--mono)}
  .tp-bot-head{padding:12px 16px;font-family:var(--mono);font-size:12px;color:var(--amber);
    text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--line);
    border-top:3px solid var(--line);background:var(--panel)}
  .nwbtn{background:#1a6fd4;color:#fff}
  .nwbtn:hover{background:#2280ea}
  .nw-top{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
  .nw-state{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:12px}
  .nw-dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block}
  .nw-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
  .nw-dot.watch{background:var(--amber);box-shadow:0 0 6px var(--amber)}
  .nw-dot.off{background:var(--red)}
  .nw-tgl{background:#1b1f29;border:1px solid var(--line);color:var(--txt);border-radius:6px;
    padding:5px 12px;font-family:var(--mono);font-size:11px;cursor:pointer}
  .nw-tgl:hover{border-color:var(--amber)}
  .nw-rule{padding:10px 16px;color:var(--muted);font-size:11px;line-height:1.5;font-family:var(--mono);border-bottom:1px solid var(--line)}
  .nw-log{max-height:190px;overflow:auto;font-family:var(--mono);font-size:11px;padding:4px 0}
  .nw-log .ln{padding:4px 16px;border-bottom:1px solid #14171e;display:flex;gap:8px}
  .nw-log .ln .lt{color:#5a6273;white-space:nowrap}
  .nw-log .open{color:var(--amber)} .nw-log .win{color:var(--green)} .nw-log .loss{color:var(--red)}
  .nw-log .skip{color:#6b7282} .nw-log .info{color:#9aa3b2} .nw-log .error{color:var(--red)}
  .flowbar{padding:10px 16px;border-bottom:1px solid var(--line)}
  .flowtxt{font-family:var(--mono);font-size:12px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
  .flowtxt .fbuy{color:var(--green);font-weight:600}
  .flowtxt .fsell{color:var(--red);font-weight:600}
  .flowtxt .flbl{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.4px}
  .flowtxt .fmut{color:var(--muted)}
  .flowtrack{height:6px;border-radius:3px;background:var(--red);overflow:hidden}
  .flowfill{height:100%;background:var(--green);transition:width .3s}
  .ticker#mktsel{cursor:pointer;user-select:none;border-radius:6px;padding:2px 8px}
  .ticker#mktsel:hover{background:#1b1f29}
  .mktcaret{color:var(--muted);font-size:11px}
  #mktdrop{display:none;position:absolute;top:46px;left:10px;z-index:120;width:380px;max-height:70vh;
    overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px;
    box-shadow:0 10px 30px rgba(0,0,0,.5)}
  #mktq{width:calc(100% - 20px);margin:10px;background:var(--panel2,#1b1f29);border:1px solid var(--line);
    color:var(--txt);font-family:var(--mono);font-size:13px;border-radius:7px;padding:8px 10px}
  .mktrow{display:grid;grid-template-columns:1.3fr 1fr 0.9fr 0.9fr;gap:6px;align-items:center;
    padding:8px 12px;cursor:pointer;font-family:var(--mono);font-size:12px;border-top:1px solid #14171e}
  .mktrow:hover{background:#1b1f29}.mktrow .mname{font-weight:600;color:var(--txt)}
  .mktrow .mr{text-align:right}.mktrow .pos{color:var(--green)}.mktrow .neg{color:var(--red)}
  .mkthd{display:grid;grid-template-columns:1.3fr 1fr 0.9fr 0.9fr;gap:6px;padding:6px 12px;
    color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:0;background:var(--panel)}
  .mkthd .mr{text-align:right}

  /* Professional dark trading-terminal skin (design only) */
  :root{
    --bg:#05070a;--panel:#0a0d12;--panel2:#10151d;--line:#1d2633;
    --txt:#edf3fb;--muted:#7c8798;--amber:#f2b84b;--green:#19c37d;--red:#ff4d5f;
  }
  html,body{background:#05070a;color:var(--txt);font-size:13px}
  body{background:linear-gradient(180deg,#05070a 0%,#070a0f 52%,#05070a 100%)}
  #topbar{
    position:sticky;top:0;z-index:100;flex-wrap:wrap;row-gap:8px;padding:9px 12px;
    background:#070a0f;border-bottom:1px solid #263244;box-shadow:0 12px 28px rgba(0,0,0,.28)
  }
  .ticker{font-size:16px;text-transform:uppercase;letter-spacing:.12em;color:#f5f8fc}
  #topbar select,.nav,.tradebtn,.ovbtn,.soundbtn,.flowwin,.nw-tgl,.closebtn,.setbtn{
    border-radius:4px;border-color:#263244;background:#0e131b;color:#cdd6e3;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.025)
  }
  .nav:hover,.tradebtn:hover,.ovbtn:hover,.soundbtn:hover,.nw-tgl:hover,.closebtn:hover,.setbtn:hover{
    background:#121925;border-color:#3a4658;color:#fff;filter:none
  }
  .tradebtn{color:#0b0f14;background:var(--amber);border:1px solid #8f6b2b}
  .tradebtn.nwbtn{background:#0f3767;border-color:#275a8b;color:#e5f1ff}
  .ovbtn.on,.setbtn.arm{background:var(--amber);border-color:#b88932;color:#090c10}
  #mstats,#cvdlabel{background:#070b11;border-bottom:1px solid #1a2331;color:#7c8798}
  #chartwrap,#tvwrap{height:66vh;min-height:430px;background:#05070a;border-bottom:1px solid #263244}
  #feedwrap{background:#05070a;padding-top:12px}
  #feedwrap h3{color:#8793a5;letter-spacing:.12em;margin-left:14px}
  .item{
    grid-template-columns:82px 104px minmax(0,1fr);padding:8px 14px;
    border-bottom:1px solid #121a26;background:#070b11
  }
  .item:nth-child(even){background:#080d13}
  .item:hover{background:#0d131d}
  .b{border-radius:4px;letter-spacing:.04em}
  #tradepanel{width:388px;background:#070b11;border-left:1px solid #263244;box-shadow:-20px 0 36px rgba(0,0,0,.28)}
  body.trading{padding-right:388px}
  .tp-head,.tp-bot-head{
    background:linear-gradient(180deg,#0f141d,#0a0e15);border-color:#202a39;
    text-transform:uppercase;letter-spacing:.07em
  }
  .tp-stats,.stats{background:#202a39;border-color:#202a39}
  .tp-stat,.stat{background:#080c12}
  .tp-stat .l,.stat .l,.stat .k{color:#6f7b8d;letter-spacing:.08em}
  .tp-stat .v,.stat .v{color:#edf3fb;font-variant-numeric:tabular-nums}
  .tp-form,.tp-start,.flowbar,.nw-rule,.nw-top,.pos{border-color:#182130;background:#070b11}
  .tp-form input,.tp-form select,.tp-start input,#mktq,#view-lighter input[type=number]{
    border-radius:4px;background:#0e131b;border-color:#263244;color:#edf3fb
  }
  .tp-long{background:#0f8f5c}.tp-short{background:#c93d4d}
  .pos:hover{background:#0d131d}
  .pos .det{color:#8793a5}
  .flowtrack{height:7px;background:#3a1720}
  .flowfill{background:#139b66}
  #popup{border-radius:6px;background:#080c12f2;border-color:#263244;border-left-color:var(--amber)}
  #mktdrop{background:#080c12;border-color:#263244;border-radius:6px}
  .mktrow{border-top:1px solid #121a26}.mktrow:hover{background:#0d131d}
  /* ---- Airdrop Radar: golden entry point to /airdrops ---- */
  .airdrop-btn{
    position:relative;overflow:hidden;display:inline-block;text-decoration:none;
    border:1px solid #fcd34d;border-radius:999px;padding:5px 18px;margin-left:6px;
    font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;
    color:#3b2503!important;vertical-align:middle;
    background:linear-gradient(100deg,#b45309,#f59e0b 22%,#fde68a 46%,#f59e0b 70%,#b45309);
    background-size:220% 100%;
    animation:airdropSweep 3.4s linear infinite,airdropGlow 1.9s ease-in-out infinite alternate;
    box-shadow:0 0 14px rgba(245,158,11,.55),0 2px 10px rgba(0,0,0,.35)
  }
  .airdrop-btn:hover{filter:brightness(1.12)}
  .airdrop-btn::after{
    content:"";position:absolute;top:0;left:-60%;width:38%;height:100%;
    background:linear-gradient(90deg,transparent,rgba(255,255,255,.75),transparent);
    transform:skewX(-20deg);animation:airdropSheen 3.4s ease-in-out infinite
  }
  @keyframes airdropSweep{0%{background-position:0% 0}100%{background-position:220% 0}}
  @keyframes airdropGlow{
    from{box-shadow:0 0 10px rgba(245,158,11,.42),0 2px 10px rgba(0,0,0,.35)}
    to{box-shadow:0 0 26px rgba(253,224,71,.85),0 2px 10px rgba(0,0,0,.35)}
  }
  @keyframes airdropSheen{0%,62%{left:-60%}100%{left:130%}}
  @media (prefers-reduced-motion:reduce){
    .airdrop-btn,.airdrop-btn::after{animation:none}
  }
</style>
</head>
<body>
  <div id="topbar">
    <span class="ticker" id="mktsel" onclick="toggleMktDrop(event)" title="Click to switch market (all Lighter markets)">
      <span id="mktname">BTC</span><span class="dot">/</span>USDC <span class="mktcaret">&#9662;</span></span>
    <div id="mktdrop">
      <input id="mktq" placeholder="Search market…" autocomplete="off" oninput="renderMktDrop()">
      <div id="mktlist"></div>
    </div>
    <a class="airdrop-btn" href="/airdrops">✦ Airdrops</a>
    <a class="nav" href="/treeofalpha" style="color:#58c1ff;border-color:#235">📰 Tree of Alpha →</a>
    <a class="nav" href="/orderbook">Order Book + Trades →</a>
    <a class="navgold" href="/penny"><span class="ng-i">◆</span>AI Penny Stock Desk<span class="ng-a">→</span></a>
    <a class="nav" href="/copytrade">Copy Traders →</a>
    <a class="nav" href="/funding">Funding Bot →</a>
    <a class="nav" href="/paper">Paper Trading →</a>
    <a class="nav" href="/ict">ICT Lab →</a>
    <a class="nav" href="/lucid-lab" style="color:var(--amber);border-color:#6b4d19">Lucid Strategy Lab →</a>
    <button class="tradebtn" onclick="openDrawer('manual')">Manual Paper Trading</button>
    <button class="tradebtn nwbtn" onclick="openDrawer('nw')">Whale Follow Bot</button>
    <button class="tradebtn" style="border-color:var(--red);color:var(--red)" onclick="openDrawer('lighter')">Lighter · REAL</button>
    <button class="tradebtn" style="background:var(--red);color:#fff;border-color:var(--red);font-weight:700" onclick="openDrawer('lbot')">⚡ Lighter BOT · REAL</button>
    <select id="interval">
      <option value="1m">1m</option>
      <option value="5m">5m</option>
      <option value="15m">15m</option>
      <option value="1h">1h</option>
      <option value="2h">2h</option>
      <option value="4h">4h</option>
      <option value="1d">1D</option>
      <option value="1w">1W</option>
      <option value="1M">1M</option>
    </select>
    <button class="tradebtn tvbtn" onclick="toggleTV()">TradingView</button>
    <button class="ovbtn" id="cvdbtn" onclick="toggleCVD()" title="cumulative volume delta">CVD</button>
    <button class="ovbtn" id="bigbtn" onclick="toggleBig()" title="mark big trades on the chart">Whale prints</button>
    <a class="nav" href="/journal" style="margin-left:4px">Journal →</a>
    <a class="nav" href="/lighter" style="margin-left:4px;color:var(--red)">Lighter · REAL →</a>
        <a class="nav" href="/markets" style="margin-left:4px;color:var(--amber)">Markets →</a>
    <span class="spacer"></span>
    <button class="soundbtn" id="soundbtn" onclick="toggleSound()" title="news sound on/off">🔔</button>
    <span id="count">0 news</span>
    <span id="status">connecting…</span>
  </div>
  <div id="mstats">
    <span id="ms-vol" title="Realized volatility: the typical size of a 1-minute BTC price move (1 standard deviation). The first number is the last 30 minutes; 'avg' is the ~4-hour baseline. Colour shows current vs that average — green = calmer than usual, amber = around normal, red = much hotter.">volatility —</span>
    <span class="ms-sep">·</span>
    <span id="ms-volm" title="Trading volume across the 9 tracked exchanges (trades ≥ $10k). '% of avg' compares the last 5 minutes to the average 5-minute pace over the past hour.">volume —</span>
    <span class="ms-sep" id="ms-news-sep" style="display:none">·</span>
    <span id="ms-news" style="display:none" title="How the market reacted to the latest headline: change in VOLUME and in VOLATILITY in the window after it vs an equal window just before it. Positive = more trading / bigger price swings after the news; negative = less. This is activity, NOT price direction. Appears ~30s after a headline and adjusts for a few minutes."></span>
  </div>
  <div id="cvdlabel"></div>

  <div id="chartwrap">
    <div id="chart"></div>
    <div id="popup"></div>
  </div>

  <div id="tvwrap" style="display:none"><div id="tvchart"></div></div>

  <div id="feedwrap">
    <h3>News feed — every message received</h3>
    <div id="feed"></div>
  </div>

  <!-- right drawer: two switchable views (manual / news+whale bot) -->
  <div id="tradepanel">
    <div class="tp-head"><span id="tp-title">Manual Paper Trading</span> <span class="tp-x" onclick="closeDrawer()">✕</span></div>

    <div id="view-manual">
      <div id="tp-start">
        <div class="tp-start">
          <label>Start with how much (USDT)?</label>
          <input id="tp-bal" type="number" value="1000" min="1">
          <button onclick="startManual()">Start paper trading</button>
          <div class="tp-note">Fake money. Opens market LONG/SHORT at the live BTC price, with leverage, stop-loss and take-profit. Completely separate from the news bot.</div>
        </div>
      </div>

      <div id="tp-live" style="display:none">
        <div class="tp-stats">
          <div class="tp-stat"><div class="l">Balance (free)</div><div class="v" id="tp-balv">—</div></div>
          <div class="tp-stat"><div class="l">Equity</div><div class="v" id="tp-eqv">—</div></div>
          <div class="tp-stat"><div class="l">Total P&amp;L</div><div class="v" id="tp-pnlv">—</div></div>
          <div class="tp-stat"><div class="l">BTC price</div><div class="v" id="tp-pxv">—</div></div>
        </div>
        <div class="flowbar">
          <div class="flowtxt"><span id="tp-flowtxt">whale flow…</span><select id="tp-flowwin" class="flowwin"><option value="15">15s</option><option value="30" selected>30s</option><option value="60">1m</option><option value="300">5m</option><option value="900">15m</option><option value="3600">1h</option></select></div>
          <div class="flowtrack"><div class="flowfill" id="tp-flowfill"></div></div>
        </div>
        <div class="tp-form">
          <label>Leverage</label>
          <select id="tp-lev"><option>1</option><option>3</option><option selected>5</option><option>10</option><option>25</option><option>50</option></select>
          <label>Size — margin (USDT)</label>
          <input id="tp-margin" type="number" placeholder="e.g. 100">
          <div class="tp-pcts"><button onclick="setPct(0.25)">25%</button><button onclick="setPct(0.5)">50%</button><button onclick="setPct(0.75)">75%</button><button onclick="setPct(1)">100%</button></div>
          <label>Stop-loss price (optional)</label>
          <div class="slrow"><input id="tp-sl" type="number" placeholder="BTC price"><button type="button" class="setbtn" id="setbtn-sl" onclick="armPlace('sl')">📍 chart</button></div>
          <label>Take-profit price (optional)</label>
          <div class="slrow"><input id="tp-tp" type="number" placeholder="BTC price"><button type="button" class="setbtn" id="setbtn-tp" onclick="armPlace('tp')">📍 chart</button></div>
          <div class="placehint" id="tp-placehint">Tip: lines for entry/SL/TP draw on the chart — drag SL/TP to move them.</div>
          <div class="tp-side"><button class="tp-long" onclick="order('long')">LONG / BUY</button><button class="tp-short" onclick="order('short')">SHORT / SELL</button></div>
          <div class="tp-err" id="tp-err"></div>
        </div>
        <div id="tp-poss"></div>
        <div class="tp-reset" onclick="resetManual()">reset account</div>
      </div>

      <div class="tp-bot-head">Bot (news) trading</div>
      <div id="tp-bot-body"></div>
    </div>

    <div id="view-lighter" style="display:none">
      <style>
        #view-lighter .lt-tabs,#view-lighter .lt-sides{display:flex;gap:6px;margin:0 16px 8px}
        #view-lighter .lt-tab{flex:1;font-family:var(--mono);font-size:12px;padding:8px 0;border:1px solid var(--line);background:var(--panel2);color:var(--muted);border-radius:7px;cursor:pointer;text-align:center}
        #view-lighter .lt-tab.active{color:var(--txt);border-color:var(--amber)}
        #view-lighter .lt-side{flex:1;font-family:var(--mono);font-size:13px;font-weight:600;padding:9px 0;border:1px solid var(--line);background:var(--panel2);color:var(--muted);border-radius:7px;cursor:pointer;text-align:center}
        #view-lighter .lt-side.buy.active{background:#13a06a;border-color:#13a06a;color:#fff}
        #view-lighter .lt-side.sell.active{background:#e0455a;border-color:#e0455a;color:#fff}
        #view-lighter .lt-row{display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;color:var(--muted);padding:3px 16px}
        #view-lighter .lt-row b{color:var(--txt);font-weight:500}
        #view-lighter .lt-rng{width:calc(100% - 32px);margin:6px 16px 0;accent-color:var(--amber)}
        #view-lighter .lt-rng.size{accent-color:#13a06a}
        #view-lighter .lt-lab{display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;color:var(--muted);margin:11px 16px 0}
        #view-lighter .lt-lab b{color:var(--amber)}
        #view-lighter .lt-chk{display:flex;align-items:center;gap:7px;font-family:var(--mono);font-size:12px;color:var(--muted);margin:11px 16px 0;cursor:pointer}
        #view-lighter .lt-submit{display:block;width:calc(100% - 32px);margin:12px 16px 4px;font-family:var(--mono);font-size:14px;font-weight:600;border:none;border-radius:8px;padding:12px 0;cursor:pointer;color:#fff}
        #view-lighter .lt-submit.buy{background:#13a06a} #view-lighter .lt-submit.buy:hover{background:#15b074}
        #view-lighter .lt-submit.sell{background:#e0455a} #view-lighter .lt-submit.sell:hover{background:#ee4f64}
        #view-lighter .lt-submit:disabled{opacity:.45;cursor:not-allowed}
        #view-lighter input[type=number]{width:calc(100% - 32px);margin:0 16px;font-family:var(--mono);font-size:14px;background:var(--panel2);border:1px solid var(--line);border-radius:7px;color:var(--txt);padding:9px 11px}
        #view-lighter label.fld{display:block;font-family:var(--mono);font-size:11px;color:var(--muted);margin:11px 16px 4px}
        #view-lighter a.lt-link{display:block;text-align:center;font-family:var(--mono);font-size:11px;color:var(--muted);text-decoration:none;border:1px solid var(--line);border-radius:7px;padding:8px;margin:10px 16px 0}
        #view-lighter a.lt-link:hover{color:var(--txt)}
      </style>
      <div class="tp-note" style="padding:11px 16px 0;color:#ffb4b4">⚠ REAL money — your live Lighter perps account. You choose the size; there is no cap.</div>
      <div id="lt-unconfigured" style="display:none"></div>
      <div id="lt-body" style="display:none">
        <div class="tp-stats">
          <div class="tp-stat"><div class="l">Perps equity</div><div class="v" id="lt-balv">—</div></div>
          <div class="tp-stat"><div class="l">Available</div><div class="v" id="lt-availv">—</div></div>
          <div class="tp-stat"><div class="l">Unrealized P&amp;L</div><div class="v" id="lt-pnlv">—</div></div>
          <div class="tp-stat"><div class="l">BTC price</div><div class="v" id="lt-pxv">—</div></div>
        </div>
        <div class="flowbar">
          <div class="flowtxt"><span id="lt-flowtxt">whale flow…</span><select id="lt-flowwin" class="flowwin" onchange="loadLighter()"><option value="15">15s</option><option value="30" selected>30s</option><option value="60">1m</option><option value="300">5m</option><option value="900">15m</option><option value="3600">1h</option></select></div>
          <div class="flowtrack"><div class="flowfill" id="lt-flowfill"></div></div>
        </div>

        <div class="lt-tabs" style="margin-top:12px">
          <div class="lt-tab active" id="lt-ot-market" onclick="setLtType('market')">Market</div>
          <div class="lt-tab" id="lt-ot-limit" onclick="setLtType('limit')">Limit</div>
        </div>
        <div class="lt-sides">
          <div class="lt-side buy active" id="lt-side-buy" onclick="setLtSide('buy')">Buy / Long</div>
          <div class="lt-side sell" id="lt-side-sell" onclick="setLtSide('sell')">Sell / Short</div>
        </div>

        <div class="lt-lab"><span>Leverage</span><b id="lt-levval">5x</b></div>
        <input id="lt-lev" class="lt-rng" type="range" min="1" max="50" step="1" value="5" oninput="onLev()">

        <div id="lt-pricewrap" style="display:none">
          <label class="fld">Limit price (USDC)</label>
          <input id="lt-price" type="number" placeholder="BTC price" oninput="recalcLt()">
        </div>

        <label class="fld">Margin (USDC)</label>
        <input id="lt-margin" type="number" placeholder="e.g. 20" oninput="onMargin()">
        <div class="lt-lab"><span>Size (% of available)</span><b id="lt-pctval">0%</b></div>
        <input id="lt-pct" class="lt-rng size" type="range" min="0" max="100" step="1" value="0" oninput="onPct()">

        <label class="lt-chk"><input type="checkbox" id="lt-reduce"> Reduce-only (close / trim existing)</label>

        <div style="margin-top:12px">
          <div class="lt-row"><span>Order value</span><b id="lt-ov">—</b></div>
          <div class="lt-row"><span>Order size</span><b id="lt-os">—</b></div>
          <div class="lt-row"><span>Position margin</span><b id="lt-pm">—</b></div>
          <div class="lt-row"><span>Est. liq. price (rough)</span><b id="lt-liq">—</b></div>
          <div class="lt-row"><span>Fees</span><b>0% · Lighter is zero-fee</b></div>
        </div>

        <button class="lt-submit buy" id="lt-submit" onclick="submitLighter()">Place LONG</button>
        <div class="tp-err" id="lt-err" style="margin:2px 16px 0"></div>
        <a class="lt-link" href="https://app.lighter.xyz" target="_blank">Deposit / move Spot ⇄ Perps on Lighter ↗</a>

        <div id="lt-poss" style="margin-top:6px"></div>

        <div class="tp-stats" id="lt-mstats" style="margin-top:12px;display:none">
          <div class="tp-stat"><div class="l">Realized P&amp;L (manual)</div><div class="v" id="lt-ms-pnl">—</div></div>
          <div class="tp-stat"><div class="l">Win rate</div><div class="v" id="lt-ms-wr">—</div></div>
          <div class="tp-stat"><div class="l">Trades</div><div class="v" id="lt-ms-n">—</div></div>
          <div class="tp-stat"><div class="l">Profit factor</div><div class="v" id="lt-ms-pf">—</div></div>
        </div>
        <div id="lt-ms-detail" style="display:none;color:var(--muted);font-size:11px;margin:4px 16px 0;font-family:var(--mono)"></div>

        <div class="tp-reset" onclick="loadLighter()">refresh</div>
      </div>
    </div>

    <div id="view-nw" style="display:none">
      <div class="nw-top">
        <div class="nw-state"><span id="nw-dot" class="nw-dot"></span><span id="nw-statetxt">…</span></div>
        <button id="nw-toggle" class="nw-tgl" onclick="nwToggle()">Pause</button>
      </div>
      <div style="display:flex;gap:8px;align-items:center;padding:6px 16px 0;flex-wrap:wrap">
        <span style="font-family:var(--mono);font-size:11px;color:var(--muted)">Strategy</span>
        <select id="nw-strat" onchange="nwSetStrategy()" style="font-family:var(--mono);font-size:12px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;color:var(--txt);padding:5px 7px"></select>
        <span id="nw-stratmsg" style="font-family:var(--mono);font-size:10px;color:var(--muted)"></span>
      </div>
      <div class="tp-stats">
        <div class="tp-stat"><div class="l">Balance</div><div class="v" id="nw-balv">—</div></div>
        <div class="tp-stat"><div class="l">Equity</div><div class="v" id="nw-eqv">—</div></div>
        <div class="tp-stat"><div class="l">Total P&amp;L</div><div class="v" id="nw-pnlv">—</div></div>
        <div class="tp-stat"><div class="l">Win rate · trades</div><div class="v" id="nw-winv">—</div></div>
      </div>
      <div class="flowbar">
        <div class="flowtxt"><span id="nw-flowtxt">whale flow…</span><select id="nw-flowwin" class="flowwin"><option value="15">15s</option><option value="30">30s</option><option value="60" selected>1m</option><option value="300">5m</option><option value="900">15m</option><option value="3600">1h</option></select></div>
        <div class="flowtrack"><div class="flowfill" id="nw-flowfill"></div></div>
      </div>
      <div class="nw-rule">No AI. On news it first checks for a <b>reaction</b> (volume <b>and</b> volatility rising), then opens <b>in the direction price moved</b> &mdash; it trades the chart reaction, not the flow. It exits on a <b>volatility trailing stop</b> or when the <b>1-min flow flips</b> against it (time-stop as a backstop).</div>
      <div id="nw-poss"></div>
      <div class="tp-bot-head">activity log</div>
      <div id="nw-log" class="nw-log"></div>
      <div class="tp-bot-head">recent trades</div>
      <div id="nw-hist"></div>
      <div class="tp-reset" onclick="nwReset()">reset bot</div>
    </div>

    <div id="view-lbot" style="display:none">
      <div class="nw-top">
        <div class="nw-state"><span id="lb-dot" class="nw-dot"></span><span id="lb-statetxt">…</span></div>
        <button id="lb-toggle" class="nw-tgl" onclick="lbToggle()">Pause</button>
      </div>
      <div class="tp-stats">
        <div class="tp-stat"><div class="l">Balance</div><div class="v" id="lb-balv">—</div></div>
        <div class="tp-stat"><div class="l">Realized P&amp;L</div><div class="v" id="lb-pnlv">—</div></div>
        <div class="tp-stat"><div class="l">Win rate · trades</div><div class="v" id="lb-winv">—</div></div>
        <div class="tp-stat"><div class="l">BTC price</div><div class="v" id="lb-pxv">—</div></div>
      </div>
      <div class="flowbar">
        <div class="flowtxt"><span id="lb-flowtxt">whale flow…</span><select id="lb-flowwin" class="flowwin" onchange="loadLBot(); lbFlowStart()"><option value="5">5s</option><option value="10">10s</option><option value="15">15s</option><option value="30">30s</option><option value="60" selected>1m</option><option value="300">5m</option><option value="900">15m</option><option value="3600">1h</option></select></div>
        <div class="flowtrack"><div class="flowfill" id="lb-flowfill"></div></div>
      </div>
      <div class="nw-rule"><b>REAL money on Lighter.</b> Same strategy as the paper News+Whale bot: on news, after an abnormal reaction (volume + volatility), it opens <b>in the direction price moved</b>, then exits on a volatility trailing stop or when the <b>1-min flow flips</b>. <b>Pause</b> stops new trades; <b>Close</b> flattens now.</div>
      <div style="display:flex;gap:8px;align-items:center;padding:10px 16px 4px;flex-wrap:wrap">
        <span style="font-family:var(--mono);font-size:11px;color:var(--muted)">Leverage</span>
        <input id="lb-lev" type="number" min="1" step="1" oninput="lbLevTouched=true" style="width:62px;font-family:var(--mono);font-size:13px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;color:var(--txt);padding:6px 8px">
        <button class="nw-tgl" onclick="lbSetLev()">Set</button>
        <span id="lb-levmax" style="font-family:var(--mono);font-size:10px;color:var(--muted)"></span>
        <span style="font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:6px">Strategy</span>
        <select id="lb-strat" onchange="lbSetStrategy()" style="font-family:var(--mono);font-size:12px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;color:var(--txt);padding:5px 7px"></select>
        <button id="lb-limit" class="nw-tgl" style="margin-left:8px" onclick="lbToggleLimit()" title="Whale Copy mirror only: ENTER with an IOC limit at the paper bot's EXACT entry price (fill there or skip). The CLOSE is always a market order.">Limit mode: OFF</button>
        <button class="nw-tgl" style="margin-left:auto;border-color:var(--red);color:var(--red)" onclick="lbClose()">Close position now</button>
      </div>
      <div id="lb-msg" style="font-family:var(--mono);font-size:11px;padding:0 16px;min-height:14px;color:var(--muted)"></div>
      <div id="lb-poss"></div>
      <div class="tp-bot-head">activity log</div>
      <div id="lb-log" class="nw-log"></div>
      <div class="tp-bot-head">recent trades · with the news that triggered each</div>
      <div id="lb-hist"></div>
      <div class="tp-reset" onclick="lbReset()">reset stats (win rate · trades · P&amp;L)</div>
    </div>
  </div>

<script>
  const $ = (id) => document.getElementById(id);
  const chartEl = $('chart'), popup = $('popup');

  const chart = LightweightCharts.createChart(chartEl, {
    autoSize: true,
    layout: { background: { color: '#0a0b0e' }, textColor: '#aab0bd', fontFamily: "'IBM Plex Mono', monospace" },
    grid: { vertLines: { color: '#14171e' }, horzLines: { color: '#14171e' } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#1d212b' },
    rightPriceScale: { borderColor: '#1d212b' },
    crosshair: { mode: 0 },
  });
  const series = chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
    wickUpColor: '#26a69a', wickDownColor: '#ef5350',
  });

  const INT = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '2h': 7200,
                '4h': 14400, '1d': 86400, '1w': 604800, '1M': 2592000 };
  const intervalSec = () => INT[$('interval').value];
  const snap = (ts) => Math.floor(ts / intervalSec()) * intervalSec();
  let newsCache = [];

  function escapeHtml(s){ return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

  // ---------- market selector (ALL Lighter markets) ----------
  window._activeMarket = 'BTC';                 // clean name, used for trading + candles
  window._activeMarketSym = 'BTC/USDC:USDC';
  let _mkts = [];
  async function loadMarketsList(){
    try{ const r = await (await fetch('/api/lighter/markets')).json();
      if(r.ok && r.markets){ _mkts = r.markets; renderMktDrop(); } }catch(e){}
  }
  function toggleMktDrop(e){ if(e) e.stopPropagation();
    const d=$('mktdrop'); const show=d.style.display!=='block';
    d.style.display=show?'block':'none';
    if(show){ if(!_mkts.length) loadMarketsList(); const q=$('mktq'); if(q){ q.value=''; q.focus(); } renderMktDrop(); }
  }
  document.addEventListener('click', function(e){
    const d=$('mktdrop'), s=$('mktsel');
    if(d && d.style.display==='block' && !d.contains(e.target) && s && !s.contains(e.target)) d.style.display='none';
  });
  function renderMktDrop(){
    const box=$('mktlist'); if(!box) return;
    const q=(($('mktq')||{}).value||'').toLowerCase();
    let rows=_mkts.filter(m=>(m.symbol||'').toLowerCase().includes(q));
    rows.sort((a,b)=>(b.volume||0)-(a.volume||0));
    let html='<div class="mkthd"><span>Market</span><span class="mr">Price</span><span class="mr">24h</span><span class="mr">Funding</span></div>';
    html+=rows.slice(0,150).map(m=>{
      const ch=(m.change==null)?'':(m.change>=0?'pos':'neg'); const fc=(m.funding==null)?'':(m.funding>=0?'pos':'neg');
      const pr=(m.price!=null)?Number(m.price).toLocaleString(undefined,{maximumFractionDigits:(m.price<1?6:2)}):'—';
      return '<div class="mktrow" data-mkt="'+m.symbol+'" data-sym="'+(m.market||'')+'">'+
        '<span class="mname">'+m.symbol+'</span><span class="mr">'+pr+'</span>'+
        '<span class="mr '+ch+'">'+(m.change!=null?((m.change>=0?'+':'')+m.change.toFixed(2)+'%'):'—')+'</span>'+
        '<span class="mr '+fc+'">'+(m.funding!=null?((m.funding>=0?'+':'')+(m.funding*100).toFixed(4)+'%'):'—')+'</span></div>';
    }).join('');
    box.innerHTML=html;
  }
  function selectMarket(name, sym){
    window._activeMarket=name; window._activeMarketSym=sym||(name+'/USDC:USDC');
    $('mktname').textContent=name;
    $('mktdrop').style.display='none';
    const lm=$('lt-market'); if(lm) lm.textContent=name+'-PERP';
    window._refitChart=true;       // auto-fit the chart to the new market's data (like TradingView)
    loadCandles();
    if(typeof loadLighter==='function') loadLighter();
  }
  // delegated click on the market list (avoids any inline-onclick quote escaping)
  (function(){ const ml=$('mktlist'); if(ml) ml.addEventListener('click', function(e){
    const row=e.target.closest('.mktrow'); if(!row) return;
    selectMarket(row.getAttribute('data-mkt'), row.getAttribute('data-sym'));
  }); })();
  loadMarketsList();

  async function loadCandles(){
    try{
      const mk = window._activeMarket || 'BTC';
      let j;
      if(mk==='BTC'){                              // BTC keeps the fast Binance feed + live tick
        j = await (await fetch('/api/candles?interval=' + $('interval').value)).json();
      } else {                                     // any other market -> Lighter's OWN candles
        const lj = await (await fetch('/api/lighter/candles?market='+encodeURIComponent(window._activeMarketSym||mk)+'&tf='+$('interval').value)).json();
        j = { candles:(lj.candles||[]).map(c=>({time:c.t,open:c.o,high:c.h,low:c.l,close:c.c})), error:lj.error };
      }
      if (j.candles && j.candles.length){
        series.setData(j.candles);
        _liveCandle = Object.assign({}, j.candles[j.candles.length - 1]);  // the forming candle, kept live
        if (window._refitChart){       // reset zoom/scale to fit the new market, then stop
          try { chart.timeScale().fitContent(); } catch(e){}
          window._refitChart = false;
        }
        $('status').textContent = '● live';
        $('status').style.color = '#26a69a';
      } else {
        $('status').textContent = 'no price data (' + (j.error || 'empty') + ')';
        $('status').style.color = '#ef5350';
      }
    } catch(e){ $('status').textContent = 'chart error'; $('status').style.color = '#ef5350'; }
  }

  // ---- REAL-TIME chart: the forming candle tracks the LIVE price tick-by-tick, driven by
  // the same trade stream + window._px that the P&L uses — so chart and P&L move together,
  // not 2s apart. The 2s loadCandles poll only re-syncs history + rolls to a new candle. ----
  let _liveCandle = null, mainTradeES = null;
  function updateLiveCandle(px){
    if (!_liveCandle || !px) return;
    if ((window._activeMarket||'BTC') !== 'BTC') return;   // px is BTC's tick; other markets refresh via poll
    if (px > _liveCandle.high) _liveCandle.high = px;
    if (px < _liveCandle.low)  _liveCandle.low  = px;
    _liveCandle.close = px;
    try { series.update(_liveCandle); } catch(e){}
  }
  function openMainTradeStream(){
    try { if (mainTradeES) mainTradeES.close(); } catch(e){}
    mainTradeES = new EventSource('/api/whales/stream');
    mainTradeES.onmessage = (e) => { try {
      const t = JSON.parse(e.data);
      if (t.ex === 'binance' && t.price){      // match the Binance candle source
        window._px = t.price;                  // always-on live price (feeds chart + P&L)
        updateLiveCandle(t.price);
        if (typeof ltRepaintPnl === 'function') ltRepaintPnl();   // P&L moves on EVERY tick (instant)
        lbRepaintPnl();
      }
    } catch(_){} };
    mainTradeES.onerror = () => {};            // EventSource auto-reconnects
  }
  openMainTradeStream();

  // markers = news dots + (optionally) whale-print arrows, merged into one set
  let bigShow = false, bigMarkers = [];
  let cvdShow = false, cvdSeries = null;
  function applyAllMarkers(){
    // a yellow dot for EVERY news item (bot headlines + Tree of Alpha). To keep a
    // busy minute (the Twitter firehose) from burying the chart, draw at most ONE
    // dot per candle — click it to see all the news at that time.
    const seen = new Set();
    const news = [];
    for(const n of newsCache){
      const t = snap(n.ts);
      if(seen.has(t)) continue;
      seen.add(t);
      news.push({ time: t, position:'aboveBar', color:'#f5c518', shape:'circle' });
    }
    const all = news.concat(bigShow ? bigMarkers : []);
    all.sort((a,b) => a.time - b.time);
    series.setMarkers(all);
  }
  function renderMarkers(){ applyAllMarkers(); }

  async function loadBig(){
    if (!bigShow) return;
    try {
      const j = await (await fetch('/api/bigtrades?interval='+$('interval').value)).json();
      bigMarkers = (j.trades||[]).map(t => {
        const buy = t.side === 'buy';
        const size = t.usd >= 1000000 ? 3 : (t.usd >= 400000 ? 2 : 1);
        return { time: t.time, position: buy?'belowBar':'aboveBar',
                 color: buy?'#26a69a':'#ef5350', shape: buy?'arrowUp':'arrowDown', size: size };
      });
    } catch(e){ bigMarkers = []; }
    applyAllMarkers();
  }
  function toggleBig(){
    bigShow = !bigShow; $('bigbtn').classList.toggle('on', bigShow);
    try { localStorage.setItem('nw_big', bigShow?'1':''); } catch(e){}
    if (bigShow) loadBig(); else { bigMarkers = []; applyAllMarkers(); }
  }

  function ensureCvd(){
    if (cvdSeries) return;
    series.priceScale().applyOptions({ scaleMargins:{ top:0.05, bottom:0.30 } });   // candles in top 70%
    cvdSeries = chart.addLineSeries({ priceScaleId:'cvd', color:'#7da9c9', lineWidth:2,
                                      priceLineVisible:false, lastValueVisible:true,
                                      priceFormat:{ type:'custom', minMove:1, formatter:(v)=>{
                                        const a=Math.abs(v), s=v<0?'-':'';
                                        return a>=1e6 ? s+'$'+(a/1e6).toFixed(2)+'M'
                                             : a>=1e3 ? s+'$'+(a/1e3).toFixed(0)+'k'
                                             : s+'$'+a.toFixed(0); } } });
    chart.priceScale('cvd').applyOptions({ scaleMargins:{ top:0.74, bottom:0 }, borderVisible:false });
  }
  function removeCvd(){
    if (cvdSeries){ try{ chart.removeSeries(cvdSeries); }catch(e){} cvdSeries = null; }
    series.priceScale().applyOptions({ scaleMargins:{ top:0.1, bottom:0.1 } });       // candles back to full
  }
  async function loadCVD(){
    if (!cvdShow || !cvdSeries) return;
    try {
      const j = await (await fetch('/api/cvd?interval='+$('interval').value)).json();
      const data = (j.cvd||[]).filter((p,i,a)=> i===0 || p.time > a[i-1].time);
      cvdSeries.setData(data);
      $('cvdlabel').textContent = 'CVD — cumulative (buy − sell) of $'+(j.min_usd||0).toLocaleString()+'+ trades over the buffered window. Divergence is the signal: price makes a new high but CVD doesn\'t → buying is drying up.';
    } catch(e){}
  }
  function toggleCVD(){
    cvdShow = !cvdShow; $('cvdbtn').classList.toggle('on', cvdShow);
    $('cvdlabel').classList.toggle('show', cvdShow);
    try { localStorage.setItem('nw_cvd', cvdShow?'1':''); } catch(e){}
    if (cvdShow){ ensureCvd(); loadCVD(); } else { removeCvd(); }
  }

  function badge(n){
    if (n.decision === 'NEWS')                 // raw Tree of Alpha headline (not an AI verdict)
      return '<span class="b news">NEWS</span>';
    if (!n.traded && (n.decision === '…' || n.decision === 'analyzing'))
      return '<span class="b skip">…</span>';
    if (n.traded){
      const bear = n.direction === 'bearish' ? ' bear' : '';
      return '<span class="b trade'+bear+'">TRADE '+escapeHtml(n.direction)+'</span>';
    }
    return '<span class="b skip">SKIP</span>';
  }

  function renderFeed(){
    const f = $('feed'); f.innerHTML = '';
    newsCache.forEach((n, i) => {
      const t = new Date(n.ts*1000).toLocaleTimeString();
      const d = document.createElement('div');
      d.className = 'item';
      d.innerHTML =
        '<span class="t">'+t+'</span>' +
        '<div class="meta">'+badge(n)+'<span class="src">'+escapeHtml(n.source)+'</span></div>' +
        '<div class="tx">'+escapeHtml(n.text)+'</div>';
      d.onclick = () => showPopupForTime(snap(n.ts), null);
      f.appendChild(d);
    });
  }

  // ---------- news sound ----------
  let audioCtx = null, soundOn = true, lastNewsTs = 0, newsReady = false;
  function initAudio(){
    try{
      if(!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if(audioCtx.state === 'suspended') audioCtx.resume();
    }catch(e){}
  }
  function chime(){
    if(!soundOn) return;
    initAudio(); if(!audioCtx) return;
    const now = audioCtx.currentTime;
    [880, 1320].forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain(), t = now + i*0.13;
      o.type = 'sine'; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.3, t+0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t+0.2);
      o.connect(g); g.connect(audioCtx.destination); o.start(t); o.stop(t+0.22);
    });
  }
  function toggleSound(){
    soundOn = !soundOn;
    $('soundbtn').textContent = soundOn ? '🔔' : '🔕';
    $('soundbtn').classList.toggle('off', !soundOn);
    try{ localStorage.setItem('nw_sound', soundOn ? '1' : '0'); }catch(e){}
    if(soundOn) chime();          // test beep + unlocks audio (this click is the gesture)
  }

  async function loadNews(){
    try{
      const r = await fetch('/api/news');
      const j = await r.json();
      newsCache = j.news || [];
      $('count').textContent = newsCache.length + ' news';
      renderMarkers(); renderFeed();
      // chime only for the bot's analysed headlines, not the Tree of Alpha firehose
      const maxTs = newsCache.reduce((m, n) => (n.decision === 'NEWS') ? m : Math.max(m, n.ts || 0), 0);
      if(newsReady && maxTs > lastNewsTs) chime();   // a genuinely new headline arrived
      if(maxTs > lastNewsTs) lastNewsTs = maxTs;
      newsReady = true;
    } catch(e){}
  }

  // INSTANT push, two kinds over the same stream:
  //  1) the headline the moment it arrives (badge "…"),
  //  2) the AI's verdict a moment later (same ts+text) -> we UPDATE that row in place,
  //     flipping its badge to TRADE/SKIP. This is why no 2s polling is needed.
  function _ingestNews(n){
    if(!n || !n.ts) return;
    const i = newsCache.findIndex(x => x.ts === n.ts && x.text === n.text);
    if(i >= 0){                       // already have this row -> merge the verdict onto it
      newsCache[i] = Object.assign(newsCache[i], n);
      renderMarkers(); renderFeed();
      return;
    }
    newsCache.unshift(n);
    if(newsCache.length > 200) newsCache.length = 200;   // keep only the latest 200 (perf)
    $('count').textContent = newsCache.length + ' news';
    renderMarkers(); renderFeed();
    if(newsReady && n.decision !== 'NEWS') chime();      // don't chime on the Twitter firehose
    if((n.ts||0) > lastNewsTs) lastNewsTs = n.ts;
  }
  (function _openNewsStream(){
    try{
      const es = new EventSource('/api/news/stream');
      es.onmessage = (e) => { try{ _ingestNews(JSON.parse(e.data)); }catch(_){} };
      es.onerror = () => {};        // browser auto-reconnects the stream
    }catch(e){}
  })();

  // ---------- market stats strip (volatility + multi-exchange volume) ----------
  function abbrevUsd(n){
    n = n || 0;
    if(n >= 1e9) return '$'+(n/1e9).toFixed(1)+'B';
    if(n >= 1e6) return '$'+(n/1e6).toFixed(1)+'M';
    if(n >= 1e3) return '$'+(n/1e3).toFixed(0)+'K';
    return '$'+Math.round(n);
  }
  async function loadMarketStats(){
    try{
      const j = await (await fetch('/api/marketstats')).json();
      const ve = $('ms-vol');
      if(j.volatility_pct != null){
        let h = 'volatility <b>'+j.volatility_pct.toFixed(2)+'%</b>';
        if(j.volatility_avg_pct != null)
          h += ' <span style="color:var(--muted)">· avg '+j.volatility_avg_pct.toFixed(2)+'%</span>';
        ve.innerHTML = h;
        const a = j.volatility_avg_pct || 0.2;            // colour vs its own average
        ve.style.color = j.volatility_pct >= a*1.6 ? 'var(--red)'
                       : (j.volatility_pct >= a*0.9 ? 'var(--amber)' : 'var(--green)');
      } else { ve.textContent = 'volatility —'; ve.style.color = 'var(--muted)'; }

      const me = $('ms-volm');
      let txt = 'volume —';
      if(j.volume_5m_usd){
        txt = 'volume <b>'+abbrevUsd(j.volume_5m_usd)+'</b>';
        if(j.volume_avg_usd) txt += ' · <span style="color:var(--muted)">avg '+abbrevUsd(j.volume_avg_usd)+'</span>';
        if(j.volume_pct_of_avg != null){
          const d = j.volume_pct_of_avg - 100;          // signed % vs that average
          txt += ' · <b>'+(d>=0?'+':'')+d+'%</b>';
        }
      } else if(j.volume_pct_of_avg != null){
        txt = 'volume <b>'+j.volume_pct_of_avg+'%</b> of avg';
      }
      me.innerHTML = txt;
      me.title = 'Volume = total ≥$10k trades across the 9 exchanges over the last 5 min (in USDT). '
               + '"avg" = the average 5-min volume over the past hour. The % is how far current volume '
               + 'is above (+) or below (−) that average. This is trading activity — NOT price direction.';

      // after-news reaction: volume AND volatility (neutral colour — about activity, not price)
      const ne = $('ms-news'), ns = $('ms-news-sep');
      const parts = [];
      if(j.news_change_pct != null){ const d=j.news_change_pct; parts.push('volume <b>'+(d>=0?'+':'')+d+'%</b>'); }
      if(j.vola_change_pct != null){ const d=j.vola_change_pct; parts.push('volatility <b>'+(d>=0?'+':'')+d+'%</b>'); }
      if(parts.length){
        ne.innerHTML = 'after news → '+parts.join(' · ');
        ne.style.display = ''; ns.style.display = '';
      } else { ne.style.display = 'none'; ns.style.display = 'none'; }
    }catch(e){}
  }

  function showPopupForTime(t, point){
    const hits = newsCache.filter(n => snap(n.ts) === t);
    if (!hits.length){ popup.style.display = 'none'; return; }
    popup.innerHTML = '<span class="close" onclick="document.getElementById(\'popup\').style.display=\'none\'">✕</span>' +
      hits.map(n => {
        const when = new Date(n.ts*1000).toLocaleString();
        const head = n.traded ? ('TRADE '+n.direction+' '+n.coins+' ('+n.conviction+')') : 'SKIP';
        return '<div class="pn"><div class="ph">'+when+'  ·  '+escapeHtml(head)+'</div>' +
               '<div class="pb">'+escapeHtml(n.text)+'</div></div>';
      }).join('');
    let x = 60, y = 50;
    if (point){ x = Math.min(point.x + 18, chartEl.clientWidth - 350); y = Math.min(point.y + 12, chartEl.clientHeight - 80); }
    popup.style.left = Math.max(8, x) + 'px';
    popup.style.top  = Math.max(8, y) + 'px';
    popup.style.display = 'block';
  }

  chart.subscribeClick(param => {
    if (param.point && armMode){ placeFromClick(param.point.y); return; }
    if (!param.time){ popup.style.display = 'none'; return; }
    showPopupForTime(param.time, param.point);
  });

  $('interval').addEventListener('change', () => { loadCandles(); renderMarkers(); loadCVD(); loadBig(); });

  // ---------- manual paper trading ----------
  function openDrawer(view){
    if (document.body.classList.contains('trading') && drawerView === view){ closeDrawer(); return; }
    drawerView = view;
    document.body.classList.add('trading');
    document.getElementById('view-manual').style.display  = view === 'manual' ? 'block' : 'none';
    document.getElementById('view-nw').style.display      = view === 'nw' ? 'block' : 'none';
    document.getElementById('view-lighter').style.display = view === 'lighter' ? 'block' : 'none';
    document.getElementById('view-lbot').style.display    = view === 'lbot' ? 'block' : 'none';
    document.getElementById('tp-title').textContent =
        view === 'nw' ? 'Whale Follow Bot' : (view === 'lighter' ? 'Lighter · REAL MONEY' : (view === 'lbot' ? 'Lighter BOT · REAL MONEY' : 'Manual Paper Trading'));
    try { localStorage.setItem('nw_drawer', view); } catch(e){}
    if (view === 'nw') loadNW();
    if (view === 'lighter') { loadLighter(); ltPnlStart(); } else { ltPnlStop(); }
    if (view === 'lbot') { loadLBot(); lbFlowStart(); } else { lbFlowStop(); }
  }
  function closeDrawer(){
    document.body.classList.remove('trading');
    lbFlowStop();
    try { localStorage.setItem('nw_drawer', ''); } catch(e){}
  }
  let drawerView = 'manual';
  const fmt = (n) => (n>=0?'+':'') + Number(n).toFixed(2);
  const money = (n) => '$' + Number(n).toLocaleString(undefined, {maximumFractionDigits:2});
  function setPct(f){ const m=$('tp-margin'); if(m && window._bal!=null) m.value=(window._bal*f).toFixed(2); }

  function renderFlow(buyPct, txtId, fillId){
    const t = $(txtId), f = $(fillId);
    if (buyPct == null){
      if (t) t.innerHTML = 'whale flow: <span class="fmut">no trades yet</span>';
      if (f) f.style.width = '50%'; return;
    }
    const sell = Math.round(100 - buyPct);
    if (t) t.innerHTML = '<span class="fbuy">'+Math.round(buyPct)+'% buy</span> <span class="fmut">·</span> <span class="fsell">'+sell+'% sell</span>';
    if (f) f.style.width = buyPct + '%';
  }

  async function loadManual(){
    const fw = $('tp-flowwin') ? $('tp-flowwin').value : 60;
    let s; try{ s = await (await fetch('/api/manual/state?fw='+fw)).json(); } catch(e){ return; }
    $('tp-start').style.display = s.active ? 'none' : 'block';
    $('tp-live').style.display  = s.active ? 'block' : 'none';
    if (s.price) window._px = s.price;   // keep live price fresh; never zero it (kept the P&L stuck)
    if (s.active){
      window._bal = s.balance;
      $('tp-balv').textContent = money(s.balance);
      $('tp-eqv').textContent  = money(s.equity);
      $('tp-pxv').textContent  = s.price ? money(s.price) : '—';
      const p = $('tp-pnlv'); p.textContent = fmt(s.total_pnl);
      p.style.color = s.total_pnl >= 0 ? 'var(--green)' : 'var(--red)';
      renderPositions(s.positions);
      renderFlow(s.flow_buy, 'tp-flowtxt', 'tp-flowfill');
    }
    syncManualLines(s);
  }

  function renderPositions(poss){
    const el = $('tp-poss'); if(!el) return;
    if(!poss || !poss.length){ el.innerHTML = '<div class="tp-note" style="padding:12px 16px">No open positions.</div>'; return; }
    el.innerHTML = poss.map(p => {
      const up = p.pnl >= 0;
      return '<div class="pos"><div class="top">' +
        '<span class="'+(p.side==='long'?'lng':'sht')+'">'+p.side.toUpperCase()+' &middot; '+p.leverage+'x</span>' +
        '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl)+' ('+fmt(p.pnl_pct)+'%)</span></div>' +
        '<div class="det"><span>entry '+money(p.entry)+'</span><span>'+p.qty+' BTC</span><span>margin '+money(p.margin)+'</span></div>' +
        '<div class="det"><span>liq '+money(p.liq)+(p.sl?' &middot; SL '+money(p.sl):'')+(p.tp?' &middot; TP '+money(p.tp):'')+'</span>' +
        '<button class="closebtn" onclick="closePos(\''+p.id+'\')">Close</button></div></div>';
    }).join('');
  }

  async function startManual(){
    const bal = parseFloat($('tp-bal').value || '0');
    await fetch('/api/manual/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({balance:bal})});
    loadManual();
  }
  async function order(side){
    const err = $('tp-err'); if(err) err.textContent = '';
    const body = { side: side, margin: parseFloat($('tp-margin').value||'0'),
                   leverage: parseFloat($('tp-lev').value||'1'),
                   sl: parseFloat($('tp-sl').value||'0'), tp: parseFloat($('tp-tp').value||'0') };
    const r = await (await fetch('/api/manual/order', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})).json();
    if (r.error && err) err.textContent = r.error; else loadManual();
  }
  async function closePos(id){
    await fetch('/api/manual/close', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:id})});
    loadManual();
  }
  async function resetManual(){
    if (confirm('Reset the manual paper account? This clears your balance and positions.')){
      await fetch('/api/manual/reset', {method:'POST'}); _lineKey=''; loadManual();
    }
  }

  // ---------- REAL-MONEY Lighter drawer (Lighter-style panel) ----------
  window._ltfree = null; window._ltprice = 0; window._ltside = 'buy'; window._lttype = 'market'; window._ltmax = 50;
  window._ltHasPos = false; let _ltLevTimer = null;

  function onLev(){
    $('lt-levval').textContent = $('lt-lev').value+'x'; recalcLt();
    // Pre-set leverage on Lighter ~0.6s after the user stops dragging, so the ORDER itself
    // skips the slow inline leverage transaction (that was the ~10s delay).
    clearTimeout(_ltLevTimer);
    _ltLevTimer = setTimeout(function(){
      fetch('/api/lighter/setlev',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({leverage:parseFloat($('lt-lev').value||'1'),
          market:(window._activeMarket||'BTC')})}).catch(function(){});
    }, 600);
  }
  function onMargin(){
    const m=parseFloat($('lt-margin').value||'0');
    if(window._ltfree && window._ltfree>0){ const pct=Math.max(0,Math.min(100, Math.round(m/window._ltfree*100))); $('lt-pct').value=pct; $('lt-pctval').textContent=pct+'%'; }
    recalcLt();
  }
  function onPct(){
    const pct=parseFloat($('lt-pct').value||'0'); $('lt-pctval').textContent=Math.round(pct)+'%';
    if(window._ltfree!=null){ $('lt-margin').value=(window._ltfree*pct/100).toFixed(2); }
    recalcLt();
  }
  function setLtSide(side){
    window._ltside=side;
    $('lt-side-buy').classList.toggle('active', side==='buy');
    $('lt-side-sell').classList.toggle('active', side==='sell');
    const b=$('lt-submit'); b.classList.toggle('buy', side==='buy'); b.classList.toggle('sell', side==='sell');
    b.textContent='Place '+(side==='buy'?'LONG':'SHORT')+(window._lttype==='limit'?' (limit)':'');
    recalcLt();
  }
  function setLtType(t){
    window._lttype=t;
    $('lt-ot-market').classList.toggle('active', t==='market');
    $('lt-ot-limit').classList.toggle('active', t==='limit');
    $('lt-pricewrap').style.display = t==='limit'?'block':'none';
    const b=$('lt-submit'); b.textContent='Place '+(window._ltside==='buy'?'LONG':'SHORT')+(t==='limit'?' (limit)':'');
    recalcLt();
  }
  function _ltRefPrice(){
    if(window._lttype==='limit'){ const p=parseFloat(($('lt-price')||{}).value||'0'); if(p>0) return p; }
    return window._ltprice||0;
  }
  function recalcLt(){
    const margin=parseFloat($('lt-margin').value||'0');
    const lev=parseFloat($('lt-lev').value||'1');
    const price=_ltRefPrice();
    const ov=margin*lev;
    $('lt-ov').textContent = ov>0?money(ov):'—';
    $('lt-pm').textContent = margin>0?money(margin):'—';
    if(ov>0 && price>0){
      $('lt-os').textContent = (ov/price).toLocaleString(undefined,{maximumFractionDigits:6})+' BTC';
      const liq = window._ltside==='buy' ? price*(1-1/lev) : price*(1+1/lev);   // rough, ignores maint. margin
      $('lt-liq').textContent = money(liq)+' ≈';
    } else { $('lt-os').textContent='—'; $('lt-liq').textContent='—'; }
  }

  async function loadLighter(){
    const fw = $('lt-flowwin') ? $('lt-flowwin').value : 60;
    let s; try{ s = await (await fetch('/api/lighter/state?fw='+fw)).json(); } catch(e){ return; }
    const un=$('lt-unconfigured'), bd=$('lt-body');
    if(!un||!bd) return;
    if(!s.configured){
      un.style.display='block'; bd.style.display='none';
      un.innerHTML='<div class="tp-note" style="padding:12px 16px">Lighter isn\'t set up yet.<br>Missing in config.py: '+((s.missing||[]).join(", ")||"?")+'<br>Fill them in and restart the bot.</div>';
      return;
    }
    if(!s.ok){
      un.style.display='block'; bd.style.display='none';
      un.innerHTML='<div class="tp-note" style="padding:12px 16px">Lighter error:<br>'+(s.error||"unknown")+'</div>';
      return;
    }
    un.style.display='none'; bd.style.display='block';
    window._ltfree = (s.balance? s.balance.free : null);
    window._ltprice = s.price || 0;
    if(s.max_leverage){ window._ltmax=s.max_leverage; const sl=$('lt-lev'); if(sl){ sl.max=s.max_leverage; if(parseFloat(sl.value)>s.max_leverage){ sl.value=s.max_leverage; $('lt-levval').textContent=s.max_leverage+'x'; } } }
    $('lt-balv').textContent   = s.balance? money(s.balance.total):'—';
    $('lt-availv').textContent = s.balance? money(s.balance.free):'—';
    $('lt-pxv').textContent    = s.price? money(s.price):'—';
    let pnl=0, has=false;
    (s.positions||[]).forEach(p=>{ if(p.pnl!=null){ pnl+=Number(p.pnl); has=true; } });
    const pe=$('lt-pnlv'); pe.textContent = has? fmt(pnl):'—'; pe.style.color = pnl>=0?'var(--green)':'var(--red)';
    renderFlow(s.flow_buy, 'lt-flowtxt', 'lt-flowfill');
    renderLtPositions(s.positions);
    window._ltHasPos = !!(s.positions && s.positions.length);
    const _p0 = (s.positions||[])[0];
    window._ltPosSize = _p0 ? _p0.size : null;   // fed to close() so it skips the slow read
    window._ltPosSide = _p0 ? _p0.side : null;
    renderLtManualStats(s.manual_stats);
    const on = !!s.trading_enabled;
    $('lt-submit').disabled=!on;
    const err=$('lt-err');
    if(err){
      if(!on){ err.style.color='var(--muted)'; err.textContent='Real trading is OFF — set LIGHTER_TRADING_ENABLED = True in config.py and restart.'; }
      else if(err.textContent.indexOf('OFF')>=0){ err.textContent=''; }
    }
    recalcLt();
  }

  function renderLtManualStats(st){
    const box=$('lt-mstats'), det=$('lt-ms-detail'); if(!box) return;
    if(!st || !st.n){ box.style.display='none'; if(det) det.style.display='none'; return; }
    box.style.display='';
    const pe=$('lt-ms-pnl'); pe.textContent=fmt(st.pnl); pe.style.color=st.pnl>=0?'var(--green)':'var(--red)';
    $('lt-ms-wr').textContent=(st.winrate!=null?st.winrate+'%':'—');
    $('lt-ms-n').textContent=st.n+'  ('+st.wins+'W / '+st.losses+'L)';
    $('lt-ms-pf').textContent=(st.profit_factor!=null?st.profit_factor:'—');
    if(det){ det.style.display='block';
      det.textContent='avg win '+fmt(st.avg_win)+' · avg loss '+fmt(st.avg_loss)+' · best '+fmt(st.best)+' · worst '+fmt(st.worst); }
  }

  let _ltPnlTimer=null;
  function ltRepaintPnl(){
    // Anchor to LIGHTER'S OWN exact unrealized P&L (polled ~1x/s via /api/lighter/pnl) and
    // interpolate between polls with the live tick — so the number MATCHES Lighter and moves
    // in real time, instead of lagging the slow account poll or using Binance's price.
    const a=window._ltAnchor, px=window._px;
    let pnl=null;
    if(a && a.pnl!=null){
      if(px && a.mark && (window._activeMarket||'BTC')==='BTC'){
        const dir=(a.side==='long')?1:-1;
        pnl = a.pnl + (px - a.mark)*a.size*dir;       // Lighter's exact P&L + live tick delta
      } else { pnl = a.pnl; }
    } else {
      const p=window._ltLivePos;                       // fallback before the first anchor lands
      if(p && px && p.entry && (p.market||'BTC')==='BTC')
        pnl=(p.side==='long') ? p.qty*(px-p.entry) : p.qty*(p.entry-px);
    }
    if(pnl==null) return;
    const el=document.getElementById('lt-pos-pnl');
    if(el){ el.textContent=fmt(pnl); el.style.color=pnl>=0?'var(--green)':'var(--red)'; }
    const hv=$('lt-pnlv'); if(hv){ hv.textContent=fmt(pnl); hv.style.color=pnl>=0?'var(--green)':'var(--red)'; }
  }
  async function ltPnlPoll(){
    try{
      const j=await (await fetch('/api/lighter/pnl')).json();
      if(j && j.ok && j.has){
        window._ltAnchor={pnl:j.pnl, mark:j.price, size:Math.abs(j.size||0), side:j.side, ts:Date.now()};
        if(window._ltLivePos){ if(j.entry) window._ltLivePos.entry=j.entry; if(j.size) window._ltLivePos.qty=Math.abs(j.size); if(j.side) window._ltLivePos.side=j.side; }
        ltRepaintPnl();
      } else if(j && j.ok && !j.has){ window._ltAnchor=null; }
    }catch(e){}
  }
  // poll Lighter only ~every 2s (cached server-side too) so we never hit its rate limit; the
  // per-tick interpolation already makes the displayed P&L move instantly between polls.
  function ltPnlStart(){ ltPnlStop(); ltPnlPoll(); _ltPnlTimer=setInterval(ltPnlPoll, 2000); }
  function ltPnlStop(){ if(_ltPnlTimer){ clearInterval(_ltPnlTimer); _ltPnlTimer=null; } }
  function renderLtPositions(poss){
    const el=$('lt-poss'); if(!el) return;
    if(!poss || !poss.length){ el.innerHTML='<div class="tp-note" style="padding:12px 16px">No open positions.</div>'; window._ltLivePos=null; return; }
    const p0=poss[0];
    window._ltLivePos={entry:Number(p0.entry)||0, qty:Number(p0.size)||0, side:p0.side, market:(window._activeMarket||'BTC')};
    el.innerHTML = poss.map(p=>{
      const up=(p.pnl||0)>=0, long=(p.side==='long');
      const sz=Number(p.size).toLocaleString(undefined,{maximumFractionDigits:6});
      return '<div class="pos"><div class="top">' +
        '<span class="'+(long?'lng':'sht')+'">'+String(p.side||'').toUpperCase()+(p.leverage?' &middot; '+Math.round(p.leverage)+'x':'')+'</span>' +
        '<span id="lt-pos-pnl" style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl||0)+'</span></div>' +
        '<div class="det"><span>entry '+money(p.entry)+'</span><span>'+sz+' BTC</span></div>' +
        '<div class="det"><span></span><button class="closebtn" onclick="closeLighter()">Close</button></div></div>';
    }).join('');
    ltRepaintPnl();
  }

  async function submitLighter(){
    const err=$('lt-err'); if(err) err.textContent='';
    const side=window._ltside, type=window._lttype;
    let margin=parseFloat($('lt-margin').value||'0');
    const lev=parseFloat($('lt-lev').value||'1');
    const reduceOnly=$('lt-reduce') ? $('lt-reduce').checked : false;
    const price = type==='limit' ? parseFloat(($('lt-price')||{}).value||'0') : 0;
    if(!margin || margin<=0){ if(err){ err.style.color='var(--red)'; err.textContent='enter a margin amount first.'; } return; }
    // Opening a perp with 100% of available as margin leaves no room for the position to stay
    // above maintenance margin, so Lighter ACCEPTS the tx but fills NOTHING (the "placed but no
    // position" bug). Leave a small buffer for an OPENING order — exactly like the bot's 0.95 —
    // so it actually fills. (reduce-only closes post no new margin, so don't cap those.)
    if(!reduceOnly && window._ltfree && margin > window._ltfree*0.96){
      margin = +(window._ltfree*0.96).toFixed(2);
    }
    if(type==='limit' && (!price||price<=0)){ if(err){ err.style.color='var(--red)'; err.textContent='enter a limit price.'; } return; }
    const word = side==='buy'?'LONG':'SHORT';
    if(err){ err.style.color='var(--muted)'; err.textContent='placing '+word+' …'; }
    const body={ side:side, margin:margin, leverage:lev, type:type, reduceOnly:reduceOnly,
                 market:(window._activeMarket||'BTC') };
    if(type==='limit') body.price=price;
    let r; try{ r = await (await fetch('/api/lighter/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json(); }
    catch(e){ if(err){ err.style.color='var(--red)'; err.textContent='order failed: '+e; } return; }
    if(!r.ok){ if(err){ err.style.color='var(--red)'; err.textContent='order failed: '+(r.error||'unknown'); } loadLighter(); return; }
    // A resting LIMIT order is correct as soon as it's accepted (it won't fill until touched).
    if(type==='limit'){
      if(err){ err.style.color='var(--green)'; err.textContent='✓ limit '+word+' resting @ '+money(price); }
      loadLighter(); return;
    }
    // For a MARKET order, do NOT claim success on tx-accept — VERIFY the position actually
    // opened (Lighter settles it ~1-2s later). This is what stops the false "placed" message.
    if(reduceOnly){ if(err){ err.style.color='var(--muted)'; err.textContent='reduce order sent — refreshing…'; } setTimeout(loadLighter, 1500); return; }
    const _oms = (r.ms!=null?r.ms:'?');
    // OPTIMISTIC FILL: the order returned OK with the fill (size/avg price), so show the position
    // INSTANTLY — exactly like Lighter's website — instead of waiting ~2s for an account fetch.
    // Then reconcile with Lighter's real numbers (live P&L, exact entry) a few times in the bg.
    const oside = (side==='buy') ? 'long' : 'short';
    window._ltHasPos = true;
    window._ltPosSize = Number(r.amount) || window._ltPosSize;
    window._ltPosSide = oside;
    if (typeof renderLtPositions === 'function')
      renderLtPositions([{side:oside, size:r.amount, entry:r.average, pnl:0, leverage:lev}]);
    if(err){ err.style.color='var(--green)'; err.textContent='✓ '+word+' filled ('+_oms+'ms).'; }
    setTimeout(loadLighter, 700); setTimeout(loadLighter, 2000); setTimeout(loadLighter, 4000);
  }

  async function closeLighter(){
    const err=$('lt-err'); if(err){ err.style.color='var(--muted)'; err.textContent='closing …'; }
    // pass the position size+side the panel already shows so the server skips the slow read
    const body = {market:(window._activeMarket||'BTC')};
    if(window._ltPosSize && window._ltPosSide){ body.size=window._ltPosSize; body.side=window._ltPosSide; }
    let r; try{ r = await (await fetch('/api/lighter/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json(); }
    catch(e){ if(err){ err.style.color='var(--red)'; err.textContent='close failed: '+e; } return; }
    if(r.ok){
      // OPTIMISTIC: clear the position from the panel INSTANTLY, then reconcile in the background
      window._ltHasPos=false; window._ltPosSize=null; window._ltPosSide=null;
      if(typeof renderLtPositions==='function') renderLtPositions([]);
      if(err){ err.style.color='var(--green)'; err.textContent='✓ closed ('+(r.ms!=null?r.ms:'?')+'ms).'; }
      setTimeout(loadLighter, 700); setTimeout(loadLighter, 2000); setTimeout(loadLighter, 4000);
    } else {
      if(err){ err.style.color='var(--red)'; err.textContent='close failed: '+(r.error||'unknown'); }
      setTimeout(loadLighter, 1000);
    }
  }

  // ---------- chart lines for the manual position: entry / SL / TP ----------
  // Lines draw on the custom chart only (not the TradingView widget). SL/TP are
  // draggable, and the 📍 buttons let you click the chart to place them.
  let entryLine=null, slLine=null, tpLine=null;
  let slPrice=0, tpPrice=0;
  let openPosId=null, openSide=null;
  let armMode=null;            // 'sl' | 'tp' : waiting for a chart click
  let dragTarget=null;         // 'sl' | 'tp' : a drag is in progress
  let _lineKey='';
  const LS = (LightweightCharts.LineStyle || {Solid:0, Dashed:2});
  const HINT = 'Tip: entry/SL/TP draw on the chart — drag the SL/TP lines to move them.';

  function _clearLines(){
    [entryLine, slLine, tpLine].forEach(l => { if(l){ try{ series.removePriceLine(l); }catch(e){} } });
    entryLine = slLine = tpLine = null; slPrice = tpPrice = 0;
  }
  function _mkLine(price, color, width, title){
    return series.createPriceLine({price:price, color:color, lineWidth:width,
      lineStyle:LS.Dashed, axisLabelVisible:true, title:title});
  }
  function syncManualLines(s){
    if (dragTarget) return;                        // never fight an active drag
    const pos = (s.active && s.positions && s.positions.length) ? s.positions[0] : null;
    const pendSl = parseFloat($('tp-sl').value||'0')||0;
    const pendTp = parseFloat($('tp-tp').value||'0')||0;
    const key = JSON.stringify([!!s.active, tvOn, pos?pos.id:0, pos?pos.side:'',
      pos?pos.entry:0, pos?pos.sl:0, pos?pos.tp:0, pos?0:pendSl, pos?0:pendTp]);
    if (key === _lineKey) return;                  // nothing changed -> no flicker
    _lineKey = key;
    _clearLines();
    openPosId = pos?pos.id:null; openSide = pos?pos.side:null;
    if (!s.active || tvOn) return;                 // can't draw on the TV widget
    if (pos){
      entryLine = series.createPriceLine({price:pos.entry,
        color: pos.side==='long'?'#26a69a':'#ef5350', lineWidth:2, lineStyle:LS.Solid,
        axisLabelVisible:true, title:pos.side.toUpperCase()+' entry'});
      if (pos.sl){ slPrice=pos.sl; slLine=_mkLine(pos.sl,'#ef5350',2,'SL ✋'); }
      if (pos.tp){ tpPrice=pos.tp; tpLine=_mkLine(pos.tp,'#26a69a',2,'TP ✋'); }
    } else {
      if (pendSl){ slPrice=pendSl; slLine=_mkLine(pendSl,'#ef5350',1,'SL (pending)'); }
      if (pendTp){ tpPrice=pendTp; tpLine=_mkLine(pendTp,'#26a69a',1,'TP (pending)'); }
    }
  }

  // ---------- chart lines for the BOT's open position: entry / SL / TP ----------
  // Read-only (the bot manages them, not you). They redraw whenever the bot's entry, SL
  // or TP change — so the SL/TP lines visibly MOVE on the chart as the bot trails them or
  // (in Copy-Trade mode) as the followed wallet's stop/target moves. Drawn on the custom
  // chart only; independent of the manual position's draggable lines.
  let botEntryLine=null, botSlLine=null, botTpLine=null, _botLineKey='';
  function _clearBotLines(){
    [botEntryLine, botSlLine, botTpLine].forEach(l => { if(l){ try{ series.removePriceLine(l); }catch(e){} } });
    botEntryLine = botSlLine = botTpLine = null;
  }
  function syncBotLines(pos){
    if (tvOn || !pos){                              // can't draw on TV widget / no bot position
      if (_botLineKey !== ''){ _clearBotLines(); _botLineKey=''; }
      return;
    }
    const key = JSON.stringify([pos.side, pos.entry, pos.sl||0, pos.tp||0]);
    if (key === _botLineKey) return;                // nothing changed -> no flicker
    _botLineKey = key;
    _clearBotLines();
    const up = (pos.side==='long');
    botEntryLine = series.createPriceLine({price:pos.entry, color: up?'#26a69a':'#ef5350',
      lineWidth:2, lineStyle:LS.Solid, axisLabelVisible:true, title:'BOT '+String(pos.side).toUpperCase()+' entry'});
    if (pos.sl) botSlLine = series.createPriceLine({price:pos.sl, color:'#ef5350', lineWidth:2,
      lineStyle:LS.Dashed, axisLabelVisible:true, title:'BOT SL'});
    if (pos.tp) botTpLine = series.createPriceLine({price:pos.tp, color:'#26a69a', lineWidth:2,
      lineStyle:LS.Dashed, axisLabelVisible:true, title:'BOT TP'});
  }
  // Keep the bot lines live even when the bot drawer is closed (so they show whenever the
  // bot holds a position). When the drawer IS open, loadLBot already calls syncBotLines.
  async function pollBotLines(){
    if (tvOn){ syncBotLines(null); return; }
    let s; try{ s = await (await fetch('/api/lighterbot/state')).json(); }catch(e){ return; }
    syncBotLines(s && s.position ? s.position : null);
  }

  function _resetHint(){ const h=$('tp-placehint'); if(h){ h.classList.remove('active'); h.textContent=HINT; } }
  function flashHint(msg){ const h=$('tp-placehint'); if(!h) return; h.classList.add('active'); h.textContent=msg; setTimeout(_resetHint, 2500); }
  function armPlace(which){
    if (tvOn){ flashHint('Turn off TradingView mode to place levels on the custom chart.'); return; }
    armMode = (armMode===which) ? null : which;
    if($('setbtn-sl')) $('setbtn-sl').classList.toggle('arm', armMode==='sl');
    if($('setbtn-tp')) $('setbtn-tp').classList.toggle('arm', armMode==='tp');
    const h=$('tp-placehint');
    if (armMode && h){ h.classList.add('active'); h.textContent='Click on the chart to set '+(armMode==='sl'?'STOP-LOSS':'TAKE-PROFIT')+'.'; }
    else _resetHint();
  }
  function placeFromClick(y){
    const price = series.coordinateToPrice(y);
    const which = armMode; armMode=null;
    if($('setbtn-sl')) $('setbtn-sl').classList.remove('arm');
    if($('setbtn-tp')) $('setbtn-tp').classList.remove('arm');
    _resetHint();
    if (price==null || price<=0 || !which) return;
    applyLevel(which, price);
  }
  async function applyLevel(which, price){
    price = Math.round(price*100)/100;
    if (openPosId){
      const px = window._px||0;                    // keep level on the correct side of price
      if (px){
        if (openSide==='long'){ price = which==='sl' ? Math.min(price, px*0.999) : Math.max(price, px*1.001); }
        else { price = which==='sl' ? Math.max(price, px*1.001) : Math.min(price, px*0.999); }
        price = Math.round(price*100)/100;
      }
      const body = {id: openPosId}; body[which] = price;
      await fetch('/api/manual/modify', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      _lineKey=''; loadManual();
    } else {
      $(which==='sl'?'tp-sl':'tp-tp').value = price;
      _lineKey=''; // force redraw of the pending line on next tick
      if (which==='sl'){ if(slLine){try{series.removePriceLine(slLine);}catch(e){}} slPrice=price; slLine=_mkLine(price,'#ef5350',1,'SL (pending)'); }
      else { if(tpLine){try{series.removePriceLine(tpLine);}catch(e){}} tpPrice=price; tpLine=_mkLine(price,'#26a69a',1,'TP (pending)'); }
    }
  }

  // ----- drag the SL / TP lines -----
  function _nearLine(y, price){
    if(!price) return false;
    const ly = series.priceToCoordinate(price);
    return ly!=null && Math.abs(y-ly) <= 7;
  }
  chartEl.addEventListener('pointerdown', (e)=>{
    if (tvOn || armMode) return;
    const y = e.clientY - chartEl.getBoundingClientRect().top;
    if (slLine && _nearLine(y, slPrice)) dragTarget='sl';
    else if (tpLine && _nearLine(y, tpPrice)) dragTarget='tp';
    else return;
    chart.applyOptions({handleScroll:false, handleScale:false});
    chartEl.style.cursor='ns-resize';
    e.preventDefault();
  });
  window.addEventListener('pointermove', (e)=>{
    const y = e.clientY - chartEl.getBoundingClientRect().top;
    if (!dragTarget){
      if (!tvOn && !armMode && ((slLine && _nearLine(y,slPrice)) || (tpLine && _nearLine(y,tpPrice)))) chartEl.style.cursor='ns-resize';
      else if (!armMode && chartEl.style.cursor==='ns-resize') chartEl.style.cursor='';
      return;
    }
    const price = series.coordinateToPrice(y);
    if (price==null || price<=0) return;
    const line = dragTarget==='sl'?slLine:tpLine;
    if (line){ try{ line.applyOptions({price:price, title:dragTarget.toUpperCase()+' '+Math.round(price)}); }catch(e){} }
    if (dragTarget==='sl') slPrice=price; else tpPrice=price;
  });
  window.addEventListener('pointerup', ()=>{
    if (!dragTarget) return;
    const which=dragTarget, price=(which==='sl'?slPrice:tpPrice);
    dragTarget=null;
    chart.applyOptions({handleScroll:true, handleScale:true});
    chartEl.style.cursor='';
    applyLevel(which, price);
  });

  async function loadBot(){
    let s; try{ s = await (await fetch('/api/bot/state')).json(); } catch(e){ return; }
    const el = $('tp-bot-body'); if(!el) return;
    let h = '<div class="tp-stats"><div class="tp-stat"><div class="l">Bot equity</div><div class="v">'+(s.equity!=null?money(s.equity):'—')+'</div></div>' +
            '<div class="tp-stat"><div class="l">Open positions</div><div class="v">'+(s.positions?s.positions.length:0)+'</div></div></div>';
    if (s.positions && s.positions.length){
      h += s.positions.map(p => '<div class="pos"><div class="top"><span class="'+(p.side==='long'?'lng':'sht')+'">'+p.symbol+' '+p.side.toUpperCase()+' &middot; '+p.leverage+'x</span>' +
        '<span style="color:'+(p.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl)+' ('+fmt(p.pnl_pct)+'%)</span></div>' +
        '<div class="det"><span>entry '+money(p.entry)+'</span><span>margin '+money(p.margin)+'</span></div></div>').join('');
    }
    if (s.history && s.history.length){
      h += '<div class="tp-note" style="padding:9px 16px 4px">recent bot trades</div>';
      h += s.history.map(t => '<div class="pos" style="padding:6px 16px"><div class="top"><span class="'+(t.side==='long'?'lng':'sht')+'">'+t.symbol+' '+t.side.toUpperCase()+'</span>' +
        '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>' +
        '<div class="det"><span>'+escapeHtml(t.reason)+'</span><span>exit '+money(t.exit)+'</span></div></div>').join('');
    }
    el.innerHTML = h;
  }

  // ---------- news + whale bot panel ----------
  async function loadNW(){
    const nfw = $('nw-flowwin') ? $('nw-flowwin').value : 60;
    let s; try{ s = await (await fetch('/api/nwbot/state?fw='+nfw)).json(); } catch(e){ return; }
    if (!s.running){
      $('nw-statetxt').textContent = 'bot not running (start the program)';
      $('nw-dot').className = 'nw-dot off'; return;
    }
    const dot = $('nw-dot'), tgl = $('nw-toggle');
    if (!s.enabled){ dot.className='nw-dot off'; $('nw-statetxt').textContent='paused'; tgl.textContent='Resume'; }
    else if (s.watching){ dot.className='nw-dot watch'; $('nw-statetxt').textContent='watching news for a signal…'; tgl.textContent='Pause'; }
    else { dot.className='nw-dot on'; $('nw-statetxt').textContent='live · waiting for news'; tgl.textContent='Pause'; }

    $('nw-balv').textContent = money(s.balance);
    $('nw-eqv').textContent  = money(s.equity);
    const p = $('nw-pnlv'); p.textContent = fmt(s.total_pnl);
    p.style.color = s.total_pnl >= 0 ? 'var(--green)' : 'var(--red)';
    $('nw-winv').innerHTML = s.trades ? (Math.round(s.wins/s.trades*100)+'% <span style="color:#5a6273;font-size:11px">· '+s.trades+'</span>') : '—';
    renderFlow(s.flow_pct, 'nw-flowtxt', 'nw-flowfill');

    const nwSel = $('nw-strat');
    if (nwSel && s.strategies){
      const want = Object.keys(s.strategies).join(',');
      if (nwSel.dataset.keys !== want){
        nwSel.innerHTML = Object.entries(s.strategies).map(function(kv){ return '<option value="'+kv[0]+'">'+escapeHtml(kv[1])+'</option>'; }).join('');
        nwSel.dataset.keys = want;
      }
      if (s.strategy) nwSel.value = s.strategy;
    }

    const poss = $('nw-poss');
    if (s.positions && s.positions.length){
      poss.innerHTML = s.positions.map(p => {
        const up = p.pnl >= 0;
        return '<div class="pos"><div class="top">' +
          '<span class="'+(p.side==='long'?'lng':'sht')+'">'+p.side.toUpperCase()+' &middot; '+p.leverage+'x</span>' +
          '<span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl)+' ('+fmt(p.pnl_pct)+'%)</span></div>' +
          '<div class="det"><span>entry '+money(p.entry)+'</span><span>flow '+p.entry_flow+'%</span><span>margin '+money(p.margin)+'</span></div>' +
          '<div class="det"><span>liq '+money(p.liq)+'</span><span>'+p.qty+' BTC</span></div>' +
          ((p.sl||p.tp) ? '<div class="det"><span>SL '+money(p.sl)+'</span><span>TP '+money(p.tp)+'</span></div>' : '') +
          (p.news ? '<div class="det" style="white-space:normal"><span style="color:var(--muted)">'+escapeHtml(p.news)+'</span></div>' : '') +
          '</div>';
      }).join('');
    } else { poss.innerHTML = '<div class="tp-note" style="padding:10px 16px">No open position.</div>'; }

    const log = $('nw-log');
    log.innerHTML = (s.log && s.log.length)
      ? s.log.map(l => '<div class="ln"><span class="lt">'+new Date(l.t*1000).toLocaleTimeString()+'</span><span class="'+l.kind+'">'+escapeHtml(l.msg)+'</span></div>').join('')
      : '<div class="tp-note" style="padding:10px 16px">No activity yet.</div>';

    const hist = $('nw-hist');
    hist.innerHTML = (s.history && s.history.length)
      ? s.history.map(t => '<div class="pos" style="padding:6px 16px"><div class="top"><span class="'+(t.side==='long'?'lng':'sht')+'">'+t.side.toUpperCase()+'</span>' +
          '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>' +
          '<div class="det"><span>'+escapeHtml(t.reason)+'</span><span>'+money(t.entry)+' → '+money(t.exit)+'</span></div></div>').join('')
      : '<div class="tp-note" style="padding:10px 16px">No closed trades yet.</div>';
  }
  async function nwToggle(){
    let s; try{ s = await (await fetch('/api/nwbot/state')).json(); }catch(e){ return; }
    await fetch('/api/nwbot/toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled: !s.enabled})});
    loadNW();
  }
  async function nwReset(){
    if (confirm('Reset the News + Whale bot? Clears its balance, positions and log.')){
      await fetch('/api/nwbot/reset', {method:'POST'}); loadNW();
    }
  }
  async function nwSetStrategy(){
    const sel = $('nw-strat'); if(!sel) return;
    const msg = $('nw-stratmsg');
    let r; try{ r = await (await fetch('/api/nwbot/strategy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({strategy: sel.value})})).json(); }
    catch(e){ if(msg) msg.textContent='error: '+e; loadNW(); return; }
    if (msg){ msg.style.color = r.ok ? 'var(--green)' : 'var(--red)'; msg.textContent = r.ok ? ('✓ '+(r.strategy_label||sel.value)) : (r.error||'?'); }
    loadNW();   // re-sync the dropdown to the server (reverts if the switch was refused)
  }

  // ---------- Lighter REAL-MONEY bot drawer (mirrors the News+Whale panel) ----------
  let lbLevTouched = false;
  // Repaint the open position's P&L from the LIVE price (window._px, fed by the price
  // poll + the trade stream) so it tracks the chart instantly instead of lagging the
  // 2s state poll. Cheap: just updates one <span>.
  function lbRepaintPnl(){
    const p = window._lbPos, px = window._px, el = document.getElementById('lb-pos-pnl');
    if (!el || !p || !px || !p.entry) return;
    const pnl = (p.side === 'long') ? p.qty * (px - p.entry) : p.qty * (p.entry - px);
    const margin = (p.leverage ? p.entry * p.qty / p.leverage : 0);
    const pct = margin ? (pnl / margin * 100) : null;
    el.textContent = fmt(pnl) + (pct!=null ? ' ('+(pct>=0?'+':'')+pct.toFixed(2)+'%)' : '');
    el.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
  }
  async function loadLBot(){
    const fw = $('lb-flowwin') ? $('lb-flowwin').value : 60;
    let s; try{ s = await (await fetch('/api/lighterbot/state?fw='+fw)).json(); }catch(e){ return; }
    if (s.price) window._px = s.price;          // keep the live price fresh while on this panel
    const dot = $('lb-dot'), tgl = $('lb-toggle');
    if (s.error && s.enabled===undefined){ $('lb-statetxt').textContent='bot not running'; dot.className='nw-dot off'; return; }
    if (!s.enabled){ dot.className='nw-dot off'; $('lb-statetxt').textContent='paused'; tgl.textContent='Resume'; }
    else if (s.watching){ dot.className='nw-dot watch'; $('lb-statetxt').textContent='watching news for a signal…'; tgl.textContent='Pause'; }
    else { dot.className='nw-dot on'; $('lb-statetxt').textContent='live · waiting for news'; tgl.textContent='Pause'; }
    $('lb-statetxt').textContent += s.trading_enabled ? '  ·  trading ON' : '  ·  dry-run';

    const _bt = (s.balance_total!=null) ? s.balance_total : s.balance_free;
    const _bf = s.balance_free;
    $('lb-balv').innerHTML = (_bt!=null)
      ? money(_bt) + ((_bf!=null && Math.abs(_bt-_bf)>0.01) ? ' <span style="color:#5a6273;font-size:11px">· usable '+money(_bf)+'</span>' : '')
      : '\u2014';
    const p = $('lb-pnlv'); p.textContent = fmt(s.net_pnl||0); p.style.color = (s.net_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)';
    $('lb-winv').innerHTML = s.trades ? (Math.round(s.wins/s.trades*100)+'% <span style="color:#5a6273;font-size:11px">· '+s.trades+'</span>') : '—';
    $('lb-pxv').textContent = s.price ? money(s.price) : '—';
    renderFlow(s.flow_pct, 'lb-flowtxt', 'lb-flowfill');

    if (!lbLevTouched && s.leverage!=null) $('lb-lev').value = s.leverage;
    $('lb-levmax').textContent = s.max_leverage ? ('max '+s.max_leverage+'x') : '';

    const lbSel = $('lb-strat');
    if (lbSel && s.strategies){
      const want = Object.keys(s.strategies).join(',');
      if (lbSel.dataset.keys !== want){
        lbSel.innerHTML = Object.entries(s.strategies).map(function(kv){ return '<option value="'+kv[0]+'">'+escapeHtml(kv[1])+'</option>'; }).join('');
        lbSel.dataset.keys = want;
      }
      if (s.strategy) lbSel.value = s.strategy;
    }
    // Limit-mode toggle button: reflect ON/OFF, and only make it relevant for the mirror strategy.
    const lbLim = $('lb-limit');
    if (lbLim){
      const on = !!s.limit_mode;
      lbLim.textContent = 'Limit mode: ' + (on ? 'ON' : 'OFF');
      lbLim.style.borderColor = on ? 'var(--green)' : 'var(--line)';
      lbLim.style.color = on ? 'var(--green)' : 'var(--muted)';
      lbLim.style.display = (s.strategy === 'whale_follow_100k') ? '' : 'none';
    }
    // When the scalper is active, lock the flow-window selector to the window IT decides on,
    // so the bar's number is the exact one the bot acts on (no 60s-vs-10s confusion).
    const _fwSel = $('lb-flowwin');
    if (_fwSel && s.strategy === 'scalper' && s.scalp_window) _fwSel.value = String(s.scalp_window);

    const msg = $('lb-msg');
    if (!s.configured){ msg.style.color='var(--red)'; msg.textContent='Lighter not set up: '+((s.why_not||[]).join(', ')||'?'); }
    else if (!s.trading_enabled){ msg.style.color='var(--muted)'; msg.textContent='Dry-run — set LIGHTER_TRADING_ENABLED = True in config.py and restart for real orders.'; }
    else if (!s.live_ok && s.live_error){ msg.style.color='var(--red)'; msg.textContent='Lighter: '+s.live_error; }
    else if (msg.dataset.sticky!=='1'){ msg.textContent=''; }

    const poss = $('lb-poss'), pos = s.position;
    if (pos){
      // cache the position so we can repaint its P&L LIVE from the price feed
      // (between the 2s state polls) instead of waiting for the next poll.
      window._lbPos = {side:pos.side, entry:pos.entry, qty:pos.qty, leverage:pos.leverage};
      const _pct0 = (pos.pnl_pct!=null) ? ' ('+(pos.pnl_pct>=0?'+':'')+Number(pos.pnl_pct).toFixed(2)+'%)' : '';
      poss.innerHTML = '<div class="pos"><div class="top">' +
        '<span class="'+(pos.side==='long'?'lng':'sht')+'">'+pos.side.toUpperCase()+' &middot; '+pos.leverage+'x</span>' +
        '<span id="lb-pos-pnl" style="font-weight:600">'+fmt(pos.pnl)+_pct0+'</span></div>' +
        '<div class="det"><span>entry '+money(pos.entry)+'</span><span>'+pos.qty+' BTC</span></div>' +
        '<div class="det" style="white-space:normal"><span>'+escapeHtml(pos.news||'')+'</span></div></div>';
      lbRepaintPnl();
    } else {
      window._lbPos = null;
      const extra = (s.live_positions && s.live_positions.length) ? ' (a position exists on Lighter the bot isn\'t tracking — use Close / it will reconcile)' : '';
      poss.innerHTML = '<div class="tp-note" style="padding:10px 16px">No open position — waiting for news.'+extra+'</div>';
    }
    syncBotLines(s.position || null);     // draw/update the bot's entry/SL/TP lines on the chart

    const log = $('lb-log');
    log.innerHTML = (s.log && s.log.length)
      ? s.log.map(l => '<div class="ln"><span class="lt">'+new Date(l.t*1000).toLocaleTimeString()+'</span><span class="'+l.kind+'">'+escapeHtml(l.msg)+'</span></div>').join('')
      : '<div class="tp-note" style="padding:10px 16px">No activity yet.</div>';

    const hist = $('lb-hist');
    hist.innerHTML = (s.history && s.history.length)
      ? s.history.map(t => '<div class="pos" style="padding:6px 16px"><div class="top"><span class="'+(t.side==='long'?'lng':'sht')+'">'+t.side.toUpperCase()+'</span>' +
          '<span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div>' +
          '<div class="det"><span>'+escapeHtml(t.reason||'')+'</span><span>'+money(t.entry)+' → '+money(t.exit)+'</span></div>' +
          '<div class="det" style="white-space:normal"><span style="color:var(--muted)">'+escapeHtml(t.news||'')+'</span></div></div>').join('')
      : '<div class="tp-note" style="padding:10px 16px">No closed trades yet.</div>';
  }
  function lbFlash(text, ok){ const m=$('lb-msg'); m.style.color=ok?'var(--green)':'var(--red)'; m.textContent=text; m.dataset.sticky='1'; setTimeout(()=>{ m.dataset.sticky='0'; }, 3500); }
  async function lbToggle(){
    let s; try{ s = await (await fetch('/api/lighterbot/state')).json(); }catch(e){ return; }
    const want = !s.enabled;
    if (want && !confirm('Arm the bot? It will place REAL leveraged orders on your Lighter account when news hits.')) return;
    await fetch('/api/lighterbot/toggle', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled: want})});
    loadLBot();
  }
  async function lbSetLev(){
    const v = parseFloat($('lb-lev').value);
    if (!v || v < 1){ lbFlash('enter a leverage of 1 or more', false); return; }
    let r; try{ r = await (await fetch('/api/lighterbot/leverage', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({leverage: v})})).json(); }catch(e){ lbFlash('error: '+e, false); return; }
    lbLevTouched = false;
    lbFlash(r.ok ? ('✓ leverage set to '+r.leverage+'x') : ('leverage: '+(r.error||'?')), !!r.ok);
    loadLBot();
  }
  async function lbSetStrategy(){
    const sel = $('lb-strat'); if(!sel) return;
    let r; try{ r = await (await fetch('/api/lighterbot/strategy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({strategy: sel.value})})).json(); }
    catch(e){ lbFlash('error: '+e, false); loadLBot(); return; }
    if (r.ok) lbFlash('✓ strategy: '+(r.strategy_label||sel.value)+' — press Resume', true);
    else { lbFlash('could not switch: '+(r.error||'?'), false); loadLBot(); }   // revert to server value
  }
  async function lbToggleLimit(){
    let r; try{ r = await (await fetch('/api/lighterbot/limitmode', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})})).json(); }catch(e){ lbFlash('error: '+e, false); return; }
    if (r.ok) lbFlash(r.limit_mode ? '✓ Limit mode ON — IOC limit ENTRY at paper price, market close' : '✓ Limit mode OFF — market orders', true);
    else lbFlash('limit mode: '+(r.error||'?'), false);
    loadLBot();
  }
  async function lbClose(){
    let r; try{ r = await (await fetch('/api/lighterbot/close', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})})).json(); }catch(e){ lbFlash('error: '+e, false); return; }
    lbFlash(r.ok ? '✓ close sent' : ('close: '+(r.error||'nothing to close')), !!r.ok);
    loadLBot();
  }
  async function lbReset(){
    if (!confirm('Reset the bot\'s stats? Clears win rate, trade count, realized P&L and the log. Your real Lighter balance and any open position are NOT touched.')) return;
    try{ await fetch('/api/lighterbot/reset', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})}); }catch(e){ lbFlash('error: '+e, false); return; }
    lbFlash('✓ stats reset', true);
    loadLBot();
  }

  // ---------- TradingView full chart (real drawing/Fib/forecast tools) ----------
  let tvCreated = false, tvOn = false;
  function toggleTV(){
    tvOn = !tvOn;
    $('chartwrap').style.display = tvOn ? 'none' : 'block';
    $('tvwrap').style.display    = tvOn ? 'block' : 'none';
    $('interval').style.display  = tvOn ? 'none' : '';
    document.querySelector('.tvbtn').classList.toggle('active', tvOn);
    try { localStorage.setItem('nw_tv', tvOn ? '1' : ''); } catch(e){}
    if (tvOn && !tvCreated && window.TradingView){
      tvCreated = true;
      new TradingView.widget({
        container_id: 'tvchart', autosize: true, symbol: 'BINANCE:BTCUSDT',
        interval: '1', timezone: 'Etc/UTC', theme: 'dark', style: '1', locale: 'en',
        hide_side_toolbar: false, hide_top_toolbar: false, allow_symbol_change: true,
        withdateranges: true, details: false, toolbar_bg: '#0a0b0e'
      });
    }
  }

  loadCandles(); loadNews();
  setInterval(loadCandles, 2000);
  // News is now fully push-driven: the SSE stream (/api/news/stream) delivers BOTH the
  // headline and the AI verdict the instant they happen, and _ingestNews updates rows in
  // place. So this is no longer a 2s busy-poll (which re-read the whole DB every 2s, per
  // browser, at ~71ms a hit) — just a slow 30s self-heal that re-syncs anything a dropped
  // stream may have missed. Live updates feel instant; request volume drops ~15x.
  setInterval(loadNews, 30000);
  setInterval(loadMarketStats, 1000); loadMarketStats();
  setInterval(() => { loadCVD(); loadBig(); }, 1000);
  loadManual(); loadBot(); loadNW();
  setInterval(loadManual, 1000);
  setInterval(loadBot, 2500);
  setInterval(loadNW, 1500);
  setInterval(() => { if (drawerView==='lighter' && document.body.classList.contains('trading')) loadLighter(); }, 5000);
  setInterval(() => { if (drawerView==='lbot' && document.body.classList.contains('trading')) loadLBot(); }, 2000);
  // Bot chart-lines: when the lbot drawer is open, loadLBot draws them; otherwise this keeps
  // them live so the bot's entry/SL/TP show on the chart whenever it holds a position.
  setInterval(() => { if (drawerView !== 'lbot') pollBotLines(); }, 3000); pollBotLines();
  // Single render tick: chart's forming candle AND the P&L from the SAME live price (window._px),
  // 4x/sec, so they move in lock-step. The trade stream also fires both instantly on each trade.
  setInterval(() => { if (window._px) updateLiveCandle(window._px); lbRepaintPnl(); ltRepaintPnl(); }, 250);

  // ---- LIVE flow bar: poll the SERVER's flow (the EXACT number the bot decides on) ----
  // ~5x/sec, over the selected window, updating only the bar. We use the server value
  // (not a browser-side estimate from the lossy trade stream) so what you SEE is what the
  // bot ACTS ON — the bar and the bot's flow-exit always agree.
  function lbFlowStart(){}   // no-op kept for call sites (server poll below handles it)
  function lbFlowStop(){}
  let _lbFlowBusy = false;
  setInterval(async () => {
    if (_lbFlowBusy || drawerView !== 'lbot' || !document.body.classList.contains('trading')) return;
    _lbFlowBusy = true;
    try {
      const fw = $('lb-flowwin') ? $('lb-flowwin').value : 60;
      const f = await (await fetch('/api/lighterbot/flow?fw='+fw)).json();
      renderFlow(f.flow_pct, 'lb-flowtxt', 'lb-flowfill');
      if (f.price) { window._px = f.price; lbRepaintPnl(); }   // live price → fast open-position P&L
    } catch(e) {} finally { _lbFlowBusy = false; }
  }, 200);

  // remember the manual flow-window selector
  try {
    const fwSel = $('tp-flowwin'), fwSaved = localStorage.getItem('nw_flowwin');
    if (fwSel){
      if (fwSaved) fwSel.value = fwSaved;
      fwSel.addEventListener('change', () => {
        try { localStorage.setItem('nw_flowwin', fwSel.value); } catch(e){}
        loadManual();
      });
    }
  } catch(e){}

  // remember the news+whale bot flow-window selector
  try {
    const nwSel = $('nw-flowwin'), nwSaved = localStorage.getItem('nw_botflowwin');
    if (nwSel){
      if (nwSaved) nwSel.value = nwSaved;
      nwSel.addEventListener('change', () => {
        try { localStorage.setItem('nw_botflowwin', nwSel.value); } catch(e){}
        loadNW();
      });
    }
  } catch(e){}

  // remember the other manual controls (leverage, size, starting balance)
  function remember(id, key){
    const el = $(id); if(!el) return;
    try { const v = localStorage.getItem(key); if (v!==null && v!=='') el.value = v; } catch(e){}
    const save = () => { try { localStorage.setItem(key, el.value); } catch(e){} };
    el.addEventListener('change', save);
    el.addEventListener('input', save);
  }
  remember('tp-lev', 'man_lev');
  remember('tp-margin', 'man_margin');
  remember('tp-bal', 'man_bal');

  // restore the drawer + TradingView mode the way the user left them
  try {
    const saved = localStorage.getItem('nw_drawer');
    if (saved === 'manual' || saved === 'nw' || saved === 'lighter' || saved === 'lbot') openDrawer(saved);
    if (localStorage.getItem('nw_tv') === '1') toggleTV();
    if (localStorage.getItem('nw_cvd') === '1') toggleCVD();
    if (localStorage.getItem('nw_big') === '1') toggleBig();
    if (localStorage.getItem('nw_sound') === '0'){
      soundOn = false; $('soundbtn').textContent = '🔕'; $('soundbtn').classList.add('off');
    }
  } catch(e){}
  document.addEventListener('click', initAudio, { once: true });  // unlock audio on first interaction
</script>
</body>
</html>
"""


# ===========================================================================
# The ORDER BOOK + WHALES page (separate page at /orderbook)
# ===========================================================================
ORDERBOOK_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC · Order Book + Trades</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#08090c; --panel:#0f1116; --panel2:#15181f; --line:#1c202a; --line2:#252a36;
    --txt:#d7dbe4; --muted:#6f7787; --amber:#f5c518; --green:#22c55e; --red:#f04452;
    --c-white:#c6ccd8; --c-blue:#3b82f6; --c-purple:#b061f5; --c-green:#26d07c; --c-dim:#2a3340;
    --bin:'IBM Plex Mono', ui-monospace, monospace; --sans:'IBM Plex Sans', system-ui, sans-serif;
    --ex-binance:#f0b90b; --ex-bybit:#ff7a45; --ex-okx:#c9cdd6; --ex-coinbase:#3d6dff; --ex-hyperliquid:#50d2c1;
    --ex-kraken:#8a6dff; --ex-gate:#e6557a; --ex-kucoin:#24d6a3; --ex-bitget:#00c2ff;
  }
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px}
  #topbar{display:flex;align-items:center;gap:16px;padding:13px 20px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#13161d,#0f1116)}
  .ticker{font-family:var(--bin);font-weight:600;font-size:19px;letter-spacing:.5px}
  .ticker .dot{color:var(--amber)}
  .nav{font-family:var(--bin);font-size:13px;color:var(--amber);text-decoration:none;border:1px solid var(--line2);padding:6px 11px;border-radius:7px}
  .nav:hover{background:var(--panel2)}
  .spacer{flex:1}
  .srcs{font-family:var(--bin);font-size:11.5px;color:var(--muted)}
  .srcs b{color:var(--txt);font-weight:500}
  .src-ok{color:var(--c-green)} .src-bad{color:var(--red)}
  #wrap{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px;max-width:1700px;margin:0 auto}
  @media(max-width:980px){#wrap{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .phead{display:flex;align-items:center;gap:12px;padding:13px 16px 10px;flex-wrap:wrap}
  .phead h2{font-family:var(--bin);font-size:14px;font-weight:600;margin:0;letter-spacing:.3px}
  .hpct{font-family:var(--bin);font-size:12.5px}
  .hpct .buy{color:var(--green);font-weight:600} .hpct .sell{color:var(--red);font-weight:600}
  .hpct .sep{color:var(--muted);margin:0 7px}
  .legend{display:flex;gap:11px;font-family:var(--bin);font-size:10.5px;color:var(--muted);margin-left:auto;flex-wrap:wrap}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:4px;vertical-align:middle}
  .sw-white{background:var(--c-white)} .sw-blue{background:var(--c-blue)} .sw-purple{background:var(--c-purple)} .sw-green{background:var(--c-green)} .sw-dim{background:var(--c-dim)}
  .barrow{padding:0 16px 12px}
  .imb-bar{display:flex;height:8px;border-radius:5px;overflow:hidden;background:#0a0c10;border:1px solid var(--line)}
  .imb-buy{background:linear-gradient(90deg,#1f7a4d,var(--green))}
  .imb-sell{background:linear-gradient(90deg,var(--red),#7a1f28)}
  #twin{background:#12161d;color:var(--txt);border:1px solid var(--line);border-radius:6px;
    padding:3px 8px;font-family:var(--bin);font-size:11px;margin-left:2px}
  .soundbtn{background:transparent;border:1px solid var(--line);color:var(--txt);
    border-radius:6px;cursor:pointer;font-size:14px;padding:3px 9px;line-height:1.2;margin-right:8px}
  .soundbtn:hover{border-color:var(--amber)}
  .soundbtn.off{opacity:.45}
  .wsrc{padding:8px 16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);font-family:var(--bin);font-size:11px;color:var(--muted)}
  /* order book */
  .colhdr{display:grid;grid-template-columns:82px 1fr 84px 96px;gap:8px;padding:8px 16px;border-top:1px solid var(--line);font-family:var(--bin);font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
  .book{max-height:68vh;overflow:auto}
  .brow{position:relative;display:grid;grid-template-columns:82px 1fr 84px 96px;gap:8px;padding:3.5px 16px;font-family:var(--bin);font-size:12.5px;align-items:center}
  .brow .bar{position:absolute;right:0;top:1px;bottom:1px;opacity:.15;z-index:0;border-radius:3px 0 0 3px}
  .bar.white{background:var(--c-white)} .bar.blue{background:var(--c-blue)} .bar.purple{background:var(--c-purple)} .bar.green{background:var(--c-green)} .bar.dim{background:var(--c-dim)}
  .brow>*{position:relative;z-index:1}
  .brow .px{font-weight:600} .brow .ex{font-weight:600;font-size:11.5px} .brow .bt{color:#8b93a4;text-align:right}
  .usd{text-align:right} .usd-dim{color:var(--muted)}
  .chip{padding:2px 8px;border-radius:6px;font-weight:600;font-size:11.5px}
  .chip.white{background:var(--c-white);color:#0a0b0e} .chip.blue{background:var(--c-blue);color:#fff}
  .chip.purple{background:var(--c-purple);color:#fff} .chip.green{background:var(--c-green);color:#06210f}
  .midrow{display:flex;justify-content:center;gap:10px;padding:9px;background:#0c0e13;font-family:var(--bin);font-size:14px;font-weight:600;border-top:1px solid var(--line2);border-bottom:1px solid var(--line2)}
  .midrow .lbl{color:var(--muted);font-weight:400}
  /* whale tape */
  .tape{max-height:72vh;overflow:auto}
  .wrow{display:grid;grid-template-columns:74px 96px 52px 72px 96px 1fr;gap:8px;align-items:center;padding:7px 16px;border-bottom:1px solid #12151c;font-family:var(--bin);font-size:12.5px}
  .wrow:hover{background:var(--panel2)}
  .wt{color:var(--muted);font-size:11px} .wex{font-weight:600}
  .wside{font-size:10.5px;padding:2px 7px;border-radius:5px;text-align:center;font-weight:600}
  .wside.buy{background:#103024;color:var(--green)} .wside.sell{background:#2e1216;color:var(--red)}
  .wpx{color:var(--txt)} .wbtc{color:#8b93a4;font-size:11.5px}
  .wud{justify-self:end}
  .empty{padding:26px 16px;color:var(--muted);font-size:13px;font-family:var(--bin);line-height:1.5}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ticker">BTC<span class="dot">·</span>Consolidated</span>
    <a class="nav" href="/">&larr; Chart</a>
    <span class="spacer"></span>
    <button class="soundbtn" id="soundbtn" onclick="toggleSound()" title="news sound on/off">🔔</button>
    <span class="srcs" id="srcs">loading exchanges…</span>
  </div>

  <div id="wrap">
    <!-- ORDER BOOK -->
    <div class="panel">
      <div class="phead">
        <h2>Order Book — all exchanges merged</h2>
        <span class="hpct" id="book-pct"></span>
        <div class="legend">
          <span><i class="sw-dim"></i>&lt;$1M</span><span><i class="sw-white"></i>$1M</span>
          <span><i class="sw-blue"></i>$5M</span><span><i class="sw-purple"></i>$20M</span><span><i class="sw-green"></i>$50M+</span>
        </div>
      </div>
      <div class="barrow"><div class="imb-bar" id="book-bar"></div></div>
      <div class="colhdr"><span>Price (USDT)</span><span>Exchange</span><span style="text-align:right">BTC</span><span style="text-align:right">USD value</span></div>
      <div class="book">
        <div id="asks"></div>
        <div class="midrow"><span class="lbl">mid</span><span id="mid">—</span></div>
        <div id="bids"></div>
      </div>
    </div>

    <!-- WHALE TRADES -->
    <div class="panel">
      <div class="phead">
        <h2>Trades — live · all exchanges</h2>
        <select id="twin" title="time window for the tape and the buy/sell %">
          <option value="60">1m</option>
          <option value="300">5m</option>
          <option value="900">15m</option>
          <option value="3600">1h</option>
        </select>
        <span class="hpct" id="whale-pct"></span>
        <div class="legend">
          <span><i class="sw-white"></i>$250K</span><span><i class="sw-blue"></i>$1M</span>
          <span><i class="sw-purple"></i>$5M</span><span><i class="sw-green"></i>$20M+</span>
        </div>
      </div>
      <div class="barrow"><div class="imb-bar" id="whale-bar"></div></div>
      <div class="wsrc" id="wsrc">trade feeds: …</div>
      <div class="tape"><div id="tape"></div></div>
    </div>
  </div>

<script>
  // ---- size color thresholds (USD), white<blue<purple<green. Edit freely. ----
  const BOOK  = [[50e6,'green'],[20e6,'purple'],[5e6,'blue'],[1e6,'white']];   // per level
  const WHALE = [[20e6,'green'],[5e6,'purple'],[1e6,'blue'],[250e3,'white']];  // per trade
  const bucket = (usd, table) => { for (const [t,c] of table) if (usd>=t) return c; return 'dim'; };
  const EX = { binance:['Binance','var(--ex-binance)'], bybit:['Bybit','var(--ex-bybit)'],
               okx:['OKX','var(--ex-okx)'], coinbase:['Coinbase','var(--ex-coinbase)'],
               hyperliquid:['Hyperliquid','var(--ex-hyperliquid)'],
               kraken:['Kraken','var(--ex-kraken)'], gate:['Gate','var(--ex-gate)'],
               kucoin:['KuCoin','var(--ex-kucoin)'], bitget:['Bitget','var(--ex-bitget)'] };
  // any exchange not in EX (e.g. one you add to config) still shows, in grey
  const exMeta = (k)=> EX[k] || [k.charAt(0).toUpperCase()+k.slice(1), '#9aa3b2'];

  function fmtUsd(n){ const a=Math.abs(n);
    if(a>=1e9) return '$'+(n/1e9).toFixed(2)+'B';
    if(a>=1e6) return '$'+(n/1e6).toFixed(1)+'M';
    if(a>=1e3) return '$'+(n/1e3).toFixed(0)+'K';
    return '$'+Math.round(n); }
  const chip = (usd, table) => { const c = bucket(usd, table);
    return c==='dim' ? '<span class="usd-dim">'+fmtUsd(usd)+'</span>' : '<span class="chip '+c+'">'+fmtUsd(usd)+'</span>'; };

  // USD-weighted buy/sell split -> header text + slim bar
  function setPressure(buy, sell, pctId, barId, bWord, sWord){
    const tot=(buy+sell)||1, bp=Math.round(buy/tot*100), sp=100-bp;
    document.getElementById(pctId).innerHTML =
      '<span class="buy">'+bp+'% '+bWord+'</span><span class="sep">·</span><span class="sell">'+sp+'% '+sWord+'</span>';
    document.getElementById(barId).innerHTML =
      '<div class="imb-buy" style="width:'+bp+'%"></div><div class="imb-sell" style="width:'+sp+'%"></div>';
  }

  async function loadBook(){
    let book; try { book = await (await fetch('/api/orderbook')).json(); } catch(e){ return; }
    // imbalance from the visible near-mid levels, weighted by USD
    const bidUsd = book.bids.reduce((a,r)=>a+r.usd,0);
    const askUsd = book.asks.reduce((a,r)=>a+r.usd,0);
    setPressure(bidUsd, askUsd, 'book-pct', 'book-bar', 'buy', 'sell');

    const all = book.asks.concat(book.bids);
    const maxUsd = Math.max(1, ...all.map(r=>r.usd));
    const row = (r, side) => {
      const c = bucket(r.usd, BOOK);
      const w = Math.max(1.5, r.usd/maxUsd*100);
      const pxc = side==='ask' ? 'var(--red)' : 'var(--green)';
      const ex = EX[r.ex] || [r.ex,'#888'];
      return '<div class="brow"><div class="bar '+c+'" style="width:'+w+'%"></div>' +
        '<span class="px" style="color:'+pxc+'">'+Math.round(r.price).toLocaleString()+'</span>' +
        '<span class="ex" style="color:'+ex[1]+'">'+ex[0]+'</span>' +
        '<span class="bt">'+r.btc.toFixed(2)+'</span>' +
        '<span class="usd">'+chip(r.usd, BOOK)+'</span></div>';
    };
    document.getElementById('asks').innerHTML = book.asks.slice().reverse().map(r=>row(r,'ask')).join('');
    document.getElementById('bids').innerHTML = book.bids.map(r=>row(r,'bid')).join('');
    document.getElementById('mid').textContent = book.mid ? Math.round(book.mid).toLocaleString() : '—';
    const s = book.sources||{};
    const bkeys = Object.keys(s).length ? Object.keys(s) : Object.keys(EX);
    const parts = bkeys.map(k => '<span class="'+(s[k]?'src-ok':'src-bad')+'">'+exMeta(k)[0]+'</span>');
    document.getElementById('srcs').innerHTML = '<b>order book:</b> ' + parts.join('  ');
  }

  function fmtDur(s){ s=Math.max(0,s|0); if(s<90) return s+'s'; if(s<5400) return Math.round(s/60)+'m'; return (s/3600).toFixed(1)+'h'; }

  // The tape grows incrementally: each refresh we only APPEND trades newer than
  // the last cursor (seq) and remove ones that aged out of the window. No redraw
  // of the whole list -> no row cap and no scroll jump.
  let tapeSeq = 0, tapeWin = null, lastFeedsHTML = '';
  const MAX_TAPE_ROWS = 100000; // effectively uncapped: show EVERY trade in the window.
                               // The 1-minute age-out is the real limiter. NOTE: this many
                               // DOM rows will make the tab sluggish/freeze in a busy minute —
                               // that's a browser limit, not a code cap. (Lower it if it locks up.)
  function makeWRow(w){
    const tm = new Date(w.ts).toLocaleTimeString();
    const ex = exMeta(w.ex);
    const d = document.createElement('div');
    d.className = 'wrow'; d.dataset.ts = w.ts;
    d.innerHTML = '<span class="wt">'+tm+'</span>' +
      '<span class="wex" style="color:'+ex[1]+'">'+ex[0]+'</span>' +
      '<span class="wside '+(w.side==='buy'?'buy':'sell')+'">'+w.side.toUpperCase()+'</span>' +
      '<span class="wpx">'+Math.round(w.price).toLocaleString()+'</span>' +
      '<span class="wbtc">'+w.btc.toFixed(3)+' BTC</span>' +
      '<span class="wud">'+chip(w.usd, WHALE)+'</span>';
    return d;
  }
  // Add trades to the top of the tape. Used by BOTH the live stream (one trade at a
  // time) and the slow safety-net poll (a batch). Dedups by seq so the two paths can
  // never double-add, and caps the DOM so the browser never re-lays-out a giant list.
  function appendTrades(list, cutoff){
    const tape = document.getElementById('tape');
    const fresh = (list || []).filter(w => !w.seq || w.seq > tapeSeq);
    if (fresh.length){
      const ph = tape.querySelector('.empty'); if (ph) ph.remove();
      const scroller = tape.parentElement;
      const atTop = scroller ? scroller.scrollTop <= 4 : true;
      // reading scrollHeight forces a layout — only do it if the user scrolled into history
      const beforeH = (scroller && !atTop) ? scroller.scrollHeight : 0;
      const frag = document.createDocumentFragment();
      for (const w of fresh){ frag.appendChild(makeWRow(w)); }     // newest-first preserved
      tape.insertBefore(frag, tape.firstChild);                    // prepend at the top
      if (scroller){
        if (atTop) scroller.scrollTop = 0;                         // watching the live top -> stay
        else scroller.scrollTop += (scroller.scrollHeight - beforeH);  // reading history -> hold place
      }
      for (const w of fresh){ if (w.seq && w.seq > tapeSeq) tapeSeq = w.seq; }
    }
    // trim from the bottom: cap the row count AND drop anything aged out of the window
    let count = tape.childElementCount;
    let last = tape.lastElementChild;
    while (last && (count > MAX_TAPE_ROWS || (cutoff && last.dataset.ts && (+last.dataset.ts) < cutoff))){
      const prev = last.previousElementSibling; last.remove(); last = prev; count--;
    }
    if (!tape.firstElementChild){
      tape.innerHTML = '<div class="empty">Waiting for the next trade… it appears here the instant it happens.</div>';
    }
  }

  // INSTANT: each trade is PUSHED over a live connection the moment it lands — no poll,
  // no interval. The browser appends it immediately.
  let tradeES = null;
  function openTradeStream(){
    try { if (tradeES) tradeES.close(); } catch(e){}
    tradeES = new EventSource('/api/whales/stream');
    tradeES.onmessage = (e) => { try { appendTrades([JSON.parse(e.data)], 0); } catch(_){} };
    tradeES.onerror = () => {};   // EventSource reconnects on its own
  }

  // Slow safety-net poll (~1.5s): refreshes the pressure bar, the feed-status line and
  // the in-window count, ages out old rows, and re-syncs any trade the stream missed.
  async function loadWhales(){
    const win = +document.getElementById('twin').value;
    const tape = document.getElementById('tape');
    if (win !== tapeWin){ tapeWin = win; tapeSeq = 0; tape.innerHTML = ''; }  // window changed -> rebuild
    let data;
    try { data = await (await fetch('/api/whales?window='+win+'&since='+tapeSeq)).json(); } catch(e){ return; }
    setPressure(data.buy_usd||0, data.sell_usd||0, 'whale-pct', 'whale-bar', 'buying', 'selling');
    const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const ws = data.sources||{};
    const fs = data.feed_status||{};
    const fkeys = Object.keys(ws).length ? Object.keys(ws) : Object.keys(EX);
    let feeds = '<b>trade feeds:</b> ' +
      fkeys.map(k =>
        '<span class="'+(ws[k]===false?'src-bad':(ws[k]?'src-ok':''))+'" title="'+esc(fs[k]||'')+'">'+exMeta(k)[0]+'</span>').join('  ');
    feeds += '  <span style="color:#5a6273">· '+(data.count||0).toLocaleString()+' trades in window</span>';
    // spell out WHY any down feed is silent (geo-block / timeout / etc.) right in the panel
    const down = fkeys.filter(k => ws[k]===false && fs[k]);
    if (down.length){
      feeds += '<div style="color:#c9893a;margin-top:5px;font-size:11px">'+
        down.map(k => esc(exMeta(k)[0])+': '+esc(fs[k])).join('<br>')+'</div>';
    }
    if ((data.window||60) > (data.span||0) + 5){
      feeds += '  <span style="color:#c9893a">· only ~'+fmtDur(data.span||0)+' buffered</span>';
    }
    if (feeds !== lastFeedsHTML){ document.getElementById('wsrc').innerHTML = feeds; lastFeedsHTML = feeds; }
    appendTrades(data.new || [], data.cutoff_ts || 0);   // catch anything the stream missed + age out
  }
  // remember the chosen time window across page changes
  const twinSel = document.getElementById('twin');
  try { const sv = localStorage.getItem('nw_twin'); if (sv) twinSel.value = sv; } catch(e){}
  twinSel.addEventListener('change', async () => {
    try { localStorage.setItem('nw_twin', twinSel.value); } catch(e){}
    try { if (tradeES) tradeES.close(); } catch(e){}   // pause the stream while we rebuild
    await loadWhales();                                 // rebuild the tape for the new window
    openTradeStream();                                  // resume the live stream
  });

  // ---------- news sound (so you hear it on this tab too) ----------
  let audioCtx = null, soundOn = true, lastNewsTs = 0, newsReady = false;
  function initAudio(){
    try{
      if(!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if(audioCtx.state === 'suspended') audioCtx.resume();
    }catch(e){}
  }
  function chime(){
    if(!soundOn) return;
    initAudio(); if(!audioCtx) return;
    const now = audioCtx.currentTime;
    [880, 1320].forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain(), t = now + i*0.13;
      o.type = 'sine'; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.3, t+0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t+0.2);
      o.connect(g); g.connect(audioCtx.destination); o.start(t); o.stop(t+0.22);
    });
  }
  function toggleSound(){
    soundOn = !soundOn;
    document.getElementById('soundbtn').textContent = soundOn ? '🔔' : '🔕';
    document.getElementById('soundbtn').classList.toggle('off', !soundOn);
    try{ localStorage.setItem('nw_sound', soundOn ? '1' : '0'); }catch(e){}
    if(soundOn) chime();
  }
  async function loadNewsSound(){
    try{
      const j = await (await fetch('/api/news')).json();
      const items = j.news || [];
      const maxTs = items.reduce((m, n) => Math.max(m, n.ts || 0), 0);
      if(newsReady && maxTs > lastNewsTs) chime();
      if(maxTs > lastNewsTs) lastNewsTs = maxTs;
      newsReady = true;
    }catch(e){}
  }
  try {
    if (localStorage.getItem('nw_sound') === '0'){
      soundOn = false; document.getElementById('soundbtn').textContent = '🔕';
      document.getElementById('soundbtn').classList.add('off');
    }
  } catch(e){}
  document.addEventListener('click', initAudio, { once: true });
  loadNewsSound();
  setInterval(loadNewsSound, 4000);

  loadBook();
  setInterval(loadBook, 2000);
  // Fill the tape ONCE, then open the live stream — doing it in this order means the
  // initial history can't be skipped by a trade that arrives mid-fetch. Then a gentle
  // 1.5s poll just keeps the stats/aging fresh and re-syncs anything the stream missed.
  loadWhales().finally(() => { openTradeStream(); setInterval(loadWhales, 1500); });
</script>
</body>
</html>
"""


# ===========================================================================
# The COPY-TRADERS page (/copytrade): 4 Hyperliquid wallets mirrored into paper
# accounts, each with PnL, win/loss, open positions, TP/SL and a live event feed.
# Always-on — every wallet runs; there is no enable/disable dropdown by design.
# ===========================================================================
COPYTRADE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Copy Traders · Hyperliquid · Paper</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#08090c; --panel:#0f1116; --panel2:#15181f; --line:#1c202a; --line2:#252a36;
    --txt:#d7dbe4; --muted:#6f7787; --amber:#f5c518; --green:#22c55e; --red:#f04452; --blue:#3b82f6;
    --bin:'IBM Plex Mono', ui-monospace, monospace; --sans:'IBM Plex Sans', system-ui, sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px}
  #topbar{display:flex;align-items:center;gap:14px;padding:13px 20px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#13161d,#0f1116);flex-wrap:wrap}
  .ticker{font-family:var(--bin);font-weight:600;font-size:18px;letter-spacing:.5px}
  .ticker .dot{color:var(--amber)}
  .nav{font-family:var(--bin);font-size:13px;color:var(--amber);text-decoration:none;border:1px solid var(--line2);padding:6px 11px;border-radius:7px}
  .nav:hover{background:var(--panel2)}
  .sub{font-family:var(--bin);font-size:11.5px;color:var(--muted)}
  .spacer{flex:1}
  .resetall{background:transparent;border:1px solid var(--line2);color:var(--muted);border-radius:7px;cursor:pointer;font-family:var(--bin);font-size:12px;padding:6px 11px}
  .resetall:hover{border-color:var(--red);color:var(--red)}
  .soundbtn{background:transparent;border:1px solid var(--line2);color:var(--txt);border-radius:7px;cursor:pointer;font-size:14px;padding:4px 10px;line-height:1.2}
  .soundbtn:hover{border-color:var(--amber)} .soundbtn.off{opacity:.45}
  /* live news ticker — same stream the main page uses, so you hear news here too */
  #newsbar{display:flex;align-items:center;gap:9px;padding:8px 20px;border-bottom:1px solid var(--line);background:#0c0e13;font-family:var(--bin);font-size:12.5px}
  #newsbar .live{color:var(--green);font-size:9px}
  #newsticker{color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #newsticker .src{color:var(--muted)}
  #newsbar.flash{animation:nf .9s ease}
  @keyframes nf{from{background:#15233a}to{background:#0c0e13}}
  #grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;padding:16px;max-width:1500px;margin:0 auto}
  @media(max-width:980px){#grid{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
  .chead{display:flex;align-items:center;gap:10px;padding:14px 16px 10px}
  .cname{font-family:var(--bin);font-size:17px;font-weight:600}
  .badge{font-family:var(--bin);font-size:10px;padding:2px 7px;border-radius:5px;border:1px solid var(--line2);color:var(--muted)}
  .badge.on{color:var(--green);border-color:#1c5}
  .badge.off{color:var(--red);border-color:#722}
  .blurb{font-size:11.5px;color:var(--muted);margin-left:auto;text-align:right;max-width:52%}
  .addr{font-family:var(--bin);font-size:10.5px;color:var(--muted);padding:0 16px 8px}
  .addr a{color:var(--blue);text-decoration:none}
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  .stat{background:var(--panel);padding:10px 12px}
  .stat .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .stat .v{font-family:var(--bin);font-size:16px;font-weight:600;margin-top:3px}
  .pos{color:var(--green)} .neg{color:var(--red)}
  /* two side-by-side panes: the wallet (leader) vs my paper copy */
  .panes{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border-bottom:1px solid var(--line)}
  .pane{background:var(--panel);padding:0 0 6px}
  .ph{font-family:var(--bin);font-size:11px;font-weight:600;letter-spacing:.4px;padding:9px 13px 7px;display:flex;align-items:center;gap:6px}
  .ph .av{margin-left:auto;font-weight:400;color:var(--muted)}
  .wpnl{font-family:var(--bin);font-size:11px;color:var(--muted);padding:3px 13px 7px;display:flex;gap:13px;flex-wrap:wrap;border-bottom:1px solid #12151c}
  .wpnl .wp i{font-style:normal;color:#6f7787}
  .wpnl.empty2{color:var(--red);font-weight:500}
  .ph.lead{color:#7fd1ff;background:#0c1620;border-bottom:1px solid #14202b}
  .ph.mine{color:#86efac;background:#0b1810;border-bottom:1px solid #122a18}
  .prow{padding:6px 13px;border-bottom:1px solid #12151c}
  .prow .l1{display:flex;align-items:center;gap:7px;font-family:var(--bin);font-size:12.5px}
  .prow .l2{font-family:var(--bin);font-size:10.5px;color:var(--muted);margin-top:3px;line-height:1.5}
  .prow b{color:var(--txt);font-weight:600}
  .prow i{color:var(--muted);font-style:normal;font-size:10.5px}
  .pnl{margin-left:auto;font-weight:600;font-family:var(--bin)}
  .side{font-size:10px;padding:2px 7px;border-radius:5px;text-align:center;font-weight:600}
  .side.long{background:#103024;color:var(--green)} .side.short{background:#2e1216;color:var(--red)}
  b.tp{color:var(--green)} b.sl{color:var(--red)} b.no{color:#555}
  .empty{color:var(--muted);font-size:11.5px;font-family:var(--bin);padding:10px 13px}
  .loghdr{font-family:var(--bin);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;padding:10px 16px 6px}
  .feed{max-height:200px;overflow:auto;border-top:1px solid var(--line)}
  .frow{display:grid;grid-template-columns:46px 1fr;gap:8px;font-family:var(--bin);font-size:11.5px;padding:5px 16px;border-bottom:1px solid #11141a}
  .frow .t{color:var(--muted)}
  .frow .k-open b{color:var(--green)} .frow .k-close b{color:var(--txt)}
  .frow .k-trim b{color:var(--amber)} .frow .k-add b{color:var(--blue)}
  .creset{background:transparent;border:none;color:var(--muted);cursor:pointer;font-family:var(--bin);font-size:11px;text-decoration:underline}
  .creset:hover{color:var(--red)}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ticker">Copy<span class="dot">·</span>Traders</span>
    <a class="nav" href="/">&larr; Chart</a>
    <a class="nav" href="/orderbook">Order Book →</a>
    <span class="sub" id="sub">paper · mirroring 4 Hyperliquid wallets live</span>
    <span class="spacer"></span>
    <button class="soundbtn" id="soundbtn" onclick="toggleSound()" title="news sound on/off">🔔</button>
    <button class="resetall" onclick="resetAll()">reset all</button>
  </div>
  <div id="newsbar"><span class="live">●</span><span id="newsticker">— waiting for news… (the AI processes these in the background regardless of this page) —</span></div>
  <div id="grid"><div class="empty" style="padding:30px">loading…</div></div>

<script>
  const fmtUsd = (v,sign=true)=>{const s=v>=0?(sign?'+':''):'-';return s+'$'+Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:0});};
  const fmtPx  = p=> p>=1000? p.toLocaleString(undefined,{maximumFractionDigits:0}) : (p>=1? p.toFixed(3) : p.toFixed(6));
  const ago = ts=>{const s=(Date.now()-ts)/1000; if(s<60)return Math.floor(s)+'s'; if(s<3600)return Math.floor(s/60)+'m'; if(s<86400)return Math.floor(s/3600)+'h'; return Math.floor(s/86400)+'d';};
  const cls = v=> v>=0?'pos':'neg';

  const fmtBig = v=> '$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:0});
  function trig(p){
    const tp = p.tp? `<b class="tp">TP ${fmtPx(p.tp)}</b>` : '';
    const sl = p.sl? `<b class="sl">SL ${fmtPx(p.sl)}</b>` : `<b class="no">no SL</b>`;
    return (tp? tp+' · ':'') + sl;
  }
  // a row in the WALLET pane (real money, their own size/PnL)
  function leaderRow(p){
    const lev = p.lev? `<i>${p.lev}x</i>` : '';
    const mark = p.mark? fmtPx(p.mark) : '—';
    return `<div class="prow">
      <div class="l1"><span class="side ${p.dir}">${p.dir.toUpperCase()}</span> <b>${p.coin}</b> ${lev}
        <span class="pnl ${cls(p.upnl)}">${fmtUsd(p.upnl)}</span></div>
      <div class="l2">size ${fmtBig(p.notional)} · @${fmtPx(p.entry)} → ${mark} · ${trig(p)}</div>
    </div>`;
  }
  // a row in MY COPY pane (paper $100, scaled, same direction & TP/SL)
  function myRow(p){
    return `<div class="prow">
      <div class="l1"><span class="side ${p.dir}">${p.dir.toUpperCase()}</span> <b>${p.coin}</b>
        <span class="pnl ${cls(p.upnl)}">${fmtUsd(p.upnl,true)}</span></div>
      <div class="l2">@${fmtPx(p.entry)} → ${fmtPx(p.mark)} · ${trig(p)}</div>
    </div>`;
  }
  function feedRow(e){
    return `<div class="frow k-${e.kind}"><span class="t">${ago(e.ts)}</span><span><b>${e.note}</b></span></div>`;
  }
  // the WALLET's OWN real PnL (from Hyperliquid) so you can compare it to your copy
  function leaderPnlLine(a){
    if(a.leader_empty) return `<div class="wpnl empty2">⚠ wallet is EMPTY ($0) — it withdrew its funds, so there is nothing to copy</div>`;
    const lp=a.leader_pnl||{};
    if(!lp.day && !lp.week && !lp.month) return `<div class="wpnl">wallet PnL: <span class="wp">loading…</span></div>`;
    const part=(w,lbl)=>{ const x=lp[w]; if(!x) return '';
      const pct = x.pct==null? '' : ` (${x.pct>=0?'+':''}${x.pct.toFixed(1)}%)`;
      return `<span class="wp"><i>${lbl}</i> <b class="${cls(x.pnl)}">${fmtUsd(x.pnl)}${pct}</b></span>`; };
    return `<div class="wpnl">wallet PnL: ${part('day','24h')}${part('week','7d')}${part('month','30d')}</div>`;
  }
  function card(a){
    const wr = a.win_rate==null? '—' : a.win_rate.toFixed(0)+'%';
    const lead = a.leader_positions.length? a.leader_positions.map(leaderRow).join('') : '<div class="empty">flat — wallet holds nothing</div>';
    const mine = a.positions.length? a.positions.map(myRow).join('') : '<div class="empty">flat — no copy open</div>';
    const feed = a.events.length? a.events.map(feedRow).join('') : '<div class="frow"><span class="t"></span><span class="empty" style="padding:0">waiting for the wallet to trade…</span></div>';
    return `<div class="card">
      <div class="chead">
        <span class="cname">${a.name}</span>
        <span class="badge ${a.online?'on':'off'}">${a.online?'LIVE':'…'}</span>
        <span class="blurb">${a.blurb}</span>
      </div>
      <div class="addr">${a.addr} · <a href="https://hypurrscan.io/address/${a.addr}" target="_blank">hypurrscan ↗</a>
        · <button class="creset" onclick="resetOne('${a.addr}')">reset</button></div>
      <div class="stats">
        <div class="stat"><div class="k">My equity</div><div class="v">$${a.equity.toLocaleString(undefined,{maximumFractionDigits:2})}</div></div>
        <div class="stat"><div class="k">My PnL</div><div class="v ${cls(a.realized)}">${fmtUsd(a.realized)}<span style="font-size:11px"> (${a.realized>=0?'+':''}${a.realized_pct.toFixed(1)}%)</span></div></div>
        <div class="stat"><div class="k">Win / Loss</div><div class="v">${a.wins}<span style="color:var(--muted)">/</span>${a.losses}</div></div>
        <div class="stat"><div class="k">Win rate</div><div class="v">${wr}</div></div>
      </div>
      <div class="panes">
        <div class="pane">
          <div class="ph lead">📡 WALLET — live positions<span class="av">acct ${fmtBig(a.leader_av)}</span></div>
          ${leaderPnlLine(a)}
          ${lead}
        </div>
        <div class="pane">
          <div class="ph mine">🟢 MY COPY (paper) — positions<span class="av">uPnL ${fmtUsd(a.upnl)}</span></div>
          ${mine}
        </div>
      </div>
      <div class="loghdr">My copy trades — live log (open · close · TP/SL hit · flip)</div>
      <div class="feed">${feed}</div>
    </div>`;
  }
  async function load(){
    let d; try{ d = await (await fetch('/api/copytrade')).json(); }catch(e){ return; }
    document.getElementById('sub').textContent =
      `paper · $${d.start_balance.toLocaleString()} each · ${d.ws_connected?'🟢 LIVE WebSocket — instant push':'🔴 reconnecting…'} · left = the wallet, right = my exact copy`;
    document.getElementById('grid').innerHTML = d.accounts.map(card).join('');
  }
  async function resetAll(){ if(!confirm('Reset ALL 4 paper accounts to $100?'))return;
    await fetch('/api/copytrade/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); load(); }
  async function resetOne(addr){ if(!confirm('Reset this paper account to $100?'))return;
    await fetch('/api/copytrade/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({addr})}); load(); }

  // ---- news sound + live ticker (subscribes to the SAME /api/news/stream the main
  //      page uses, so you hear & see news on THIS page too). The AI that reacts to
  //      news runs in the bot's backend and is unaffected by which page you view. ----
  let audioCtx=null, soundOn=true, lastNewsTs=0;
  function initAudio(){ try{ if(!audioCtx) audioCtx=new (window.AudioContext||window.webkitAudioContext)(); if(audioCtx.state==='suspended') audioCtx.resume(); }catch(e){} }
  function chime(){ if(!soundOn) return; initAudio(); if(!audioCtx) return; const now=audioCtx.currentTime;
    [880,1320].forEach((f,i)=>{ const o=audioCtx.createOscillator(),g=audioCtx.createGain(),t=now+i*0.13;
      o.type='sine'; o.frequency.value=f; g.gain.setValueAtTime(0.0001,t); g.gain.exponentialRampToValueAtTime(0.3,t+0.02);
      g.gain.exponentialRampToValueAtTime(0.0001,t+0.2); o.connect(g); g.connect(audioCtx.destination); o.start(t); o.stop(t+0.22); }); }
  function toggleSound(){ soundOn=!soundOn; const b=document.getElementById('soundbtn');
    b.textContent=soundOn?'🔔':'🔕'; b.classList.toggle('off',!soundOn);
    try{ localStorage.setItem('nw_sound', soundOn?'1':'0'); }catch(e){} if(soundOn) chime(); }
  function showNews(n){
    const bar=document.getElementById('newsbar'), t=document.getElementById('newsticker');
    const src=(n.source||'').replace('tg:','');
    t.innerHTML = `<b>${(n.text||'').slice(0,160)}</b> <span class="src">· ${src}</span>`;
    bar.classList.remove('flash'); void bar.offsetWidth; bar.classList.add('flash');
  }
  (function(){
    try{ if(localStorage.getItem('nw_sound')==='0'){ soundOn=false; const b=document.getElementById('soundbtn'); b.textContent='🔕'; b.classList.add('off'); } }catch(e){}
    document.addEventListener('click', initAudio, { once:true });   // unlock audio on first click
    try{
      const es=new EventSource('/api/news/stream');
      es.onmessage=(e)=>{ try{ const n=JSON.parse(e.data); if(!n||!n.text) return; showNews(n);
        if((n.ts||0)>lastNewsTs){ lastNewsTs=n.ts||0; chime(); } }catch(_){} };
      es.onerror=()=>{};        // browser auto-reconnects
    }catch(e){}
  })();

  load(); setInterval(load, 1000);
</script>
</body>
</html>
"""


# ===========================================================================
# The FUNDING BOT page (/funding) — replaces the old Stocks page. Plays the price
# move around Lighter's hourly funding settlement: bet 40s before, close 20s after.
# ===========================================================================
FUNDING_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Funding Settlement Bot · Lighter · Paper</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{--bg:#08090c;--panel:#0f1116;--panel2:#15181f;--line:#1c202a;--line2:#252a36;
    --txt:#d7dbe4;--muted:#6f7787;--amber:#f5c518;--green:#22c55e;--red:#f04452;--blue:#3b82f6;
    --bin:'IBM Plex Mono',ui-monospace,monospace;--sans:'IBM Plex Sans',system-ui,sans-serif;}
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px}
  #topbar{display:flex;align-items:center;gap:14px;padding:13px 20px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#13161d,#0f1116);flex-wrap:wrap}
  .ticker{font-family:var(--bin);font-weight:600;font-size:18px;letter-spacing:.5px}
  .ticker .dot{color:var(--amber)}
  .nav{font-family:var(--bin);font-size:13px;color:var(--amber);text-decoration:none;border:1px solid var(--line2);padding:6px 11px;border-radius:7px}
  .nav:hover{background:var(--panel2)}
  .sub{font-family:var(--bin);font-size:11.5px;color:var(--muted)}
  .spacer{flex:1}
  .btn{font-family:var(--bin);font-size:12.5px;border-radius:7px;cursor:pointer;padding:7px 14px;border:1px solid var(--line2);background:transparent;color:var(--txt)}
  .btn.on{background:#103024;border-color:#1c5;color:var(--green)} .btn.off{background:#2e1216;border-color:#722;color:var(--red)}
  .btn.reset{color:var(--muted)} .btn.reset:hover{border-color:var(--red);color:var(--red)}
  #wrap{max-width:1400px;margin:0 auto;padding:16px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:980px){#wrap{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .ph{font-family:var(--bin);font-size:11px;font-weight:600;letter-spacing:.4px;color:var(--muted);text-transform:uppercase;padding:11px 15px 9px;border-bottom:1px solid var(--line)}
  /* countdown hero */
  .hero{grid-column:1/-1;background:linear-gradient(180deg,#12161d,#0f1116);border:1px solid var(--line);border-radius:12px;padding:16px 20px;display:flex;align-items:center;gap:24px;flex-wrap:wrap}
  .cd{font-family:var(--bin);font-weight:600;font-size:40px;letter-spacing:1px;line-height:1}
  .cd small{display:block;font-size:11px;color:var(--muted);letter-spacing:.5px;margin-top:5px;text-transform:uppercase;font-weight:400}
  .cd.entry{color:var(--amber)} .cd.exit{color:var(--blue)}
  .phasebadge{font-family:var(--bin);font-size:12px;padding:5px 12px;border-radius:7px;border:1px solid var(--line2);color:var(--muted)}
  .phasebadge.live{color:var(--amber);border-color:var(--amber)}
  /* stats */
  .stats{grid-column:1/-1;display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .stat{background:var(--panel);padding:12px 14px}
  .stat .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .stat .v{font-family:var(--bin);font-size:18px;font-weight:600;margin-top:4px}
  .pos{color:var(--green)} .neg{color:var(--red)}
  .row{display:grid;align-items:center;font-family:var(--bin);font-size:12.5px;padding:7px 15px;border-bottom:1px solid #12151c}
  .lbrow{grid-template-columns:1fr 92px 64px}
  .lbrow.bet{background:#0c1620}
  .side{font-size:10px;padding:2px 7px;border-radius:5px;font-weight:600}
  .side.long{background:#103024;color:var(--green)} .side.short{background:#2e1216;color:var(--red)}
  .open-card{padding:14px 16px;font-family:var(--bin)}
  .open-card .big{font-size:15px;font-weight:600}
  .open-card .l2{color:var(--muted);font-size:11.5px;margin-top:6px;line-height:1.7}
  .empty{color:var(--muted);font-size:12px;font-family:var(--bin);padding:16px}
  .thead,.trow{display:grid;grid-template-columns:128px 1fr 52px 70px 78px 78px 78px 62px;gap:6px;font-family:var(--bin);font-size:11.5px;padding:6px 15px;align-items:center}
  .thead{color:var(--muted);text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--line)}
  .trow{border-bottom:1px solid #11141a}
  .tbl{max-height:46vh;overflow:auto}
  .feed{max-height:200px;overflow:auto}
  .frow{display:grid;grid-template-columns:54px 1fr;gap:8px;font-family:var(--bin);font-size:11.5px;padding:5px 15px;border-bottom:1px solid #11141a}
  .frow .t{color:var(--muted)}
  .k-open b{color:var(--green)} .k-close b{color:var(--txt)} .k-funding b{color:var(--amber)} .k-skip b{color:var(--muted)} .k-win b{color:var(--green)} .k-loss b{color:var(--red)}
  .note{grid-column:1/-1;font-family:var(--bin);font-size:11px;color:var(--muted);padding:0 4px}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ticker">Funding<span class="dot">·</span>Settlement Bot</span>
    <a class="nav" href="/">&larr; Chart</a>
    <a class="nav" href="/copytrade">Copy Traders →</a>
    <span class="sub" id="sub">paper · real Lighter data · bet 40s before settlement, close 20s after</span>
    <span class="spacer"></span>
    <button class="btn" id="togglebtn" onclick="toggle()">…</button>
    <button class="btn reset" onclick="resetBot()">reset</button>
  </div>
  <div id="wrap"><div class="empty" style="grid-column:1/-1;padding:30px">loading…</div></div>

<script>
  const $=id=>document.getElementById(id);
  const fmtUsd=(v,s=true)=>{const sg=v>=0?(s?'+':''):'-';return sg+'$'+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});};
  const fmtPx=p=>p>=1000?p.toLocaleString(undefined,{maximumFractionDigits:1}):(p>=1?p.toFixed(4):p.toPrecision(4));
  const cls=v=>v>=0?'pos':'neg';
  const ago=ts=>{const s=(Date.now()-ts*1000)/1000;if(s<60)return Math.floor(s)+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h';return Math.floor(s/86400)+'d';};
  const tstr=ts=>{const d=new Date(ts*1000);return d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});};
  let S=null;

  function lbRow(r,isBet){
    const dir=r.rate>0?'long':'short';
    return `<div class="row lbrow ${isBet?'bet':''}">
      <span>${r.market.replace('/USDC:USDC','').replace('/USDC','')}</span>
      <span class="${cls(r.rate)}">${(r.rate*100>=0?'+':'')}${(r.rate*100).toFixed(4)}%</span>
      <span class="side ${dir}">${dir.toUpperCase()}</span></div>`;
  }
  function tradeRow(t){
    const m=t.market.replace('/USDC:USDC','').replace('/USDC','');
    return `<div class="trow">
      <span style="color:var(--muted)">${tstr(t.closed_at)}</span>
      <span><span class="side ${t.side}">${t.side.toUpperCase()}</span> ${m}</span>
      <span class="${cls(t.rate)}">${(t.rate*100).toFixed(3)}%</span>
      <span class="neg">-$${t.funding_loss.toFixed(3)}</span>
      <span>${fmtPx(t.entry_px)}→${fmtPx(t.exit_px)}</span>
      <span class="${cls(t.price_pnl)}">${fmtUsd(t.price_pnl)}</span>
      <span class="${cls(t.net_pnl)}"><b>${fmtUsd(t.net_pnl)}</b></span>
      <span class="${cls(t.pct)}">${t.pct>=0?'+':''}${t.pct.toFixed(2)}%</span></div>`;
  }
  function feedRow(e){return `<div class="frow k-${e.kind}"><span class="t">${ago(e.t)}</span><span><b>${e.msg}</b></span></div>`;}

  function render(){
    if(!S)return;
    const b=$('togglebtn'); b.textContent=S.enabled?'⏸ PAUSE':'▶ ENABLE'; b.className='btn '+(S.enabled?'on':'off');
    const thr=S.min_funding_pct||0;
    const thrTxt = thr<=0 ? 'bets EVERY market' : `only |funding| ≥ ${thr.toFixed(2)}%`;
    $('sub').textContent=`paper · $${S.bet_usd} per market · enter ${S.entry_lead}s before settlement · exit each position once it's net-profitable · ${thrTxt}`;
    const wr=S.win_rate==null?'—':S.win_rate.toFixed(0)+'%';
    const lb=S.leaderboard.length?S.leaderboard.map(r=>lbRow(r, thr<=0 || Math.abs(r.rate)*100>=thr)).join(''):'<div class="empty">loading funding rates…</div>';
    let openHtml='<div class="empty">flat — waiting for the next settlement window</div>';
    if(S.opens && S.opens.length){
      openHtml = S.opens.map(o=>`<div class="row" style="grid-template-columns:1fr 64px 1fr 78px 84px">
        <span><span class="side ${o.side}">${o.side.toUpperCase()}</span> ${o.market.replace('/USDC:USDC','')}</span>
        <span class="${cls(o.rate)}">${(o.rate*100).toFixed(3)}%</span>
        <span style="color:var(--muted)">@${fmtPx(o.entry_px)}→${fmtPx(o.mark_px)}${o.funding_booked?' · funded':' · pre-settle'}</span>
        <span style="color:var(--muted)">px ${fmtUsd(o.upnl)}</span>
        <span class="${cls(o.net)}"><b>net ${fmtUsd(o.net)}</b></span></div>`).join('');
    }
    const trades=S.trades.length?S.trades.map(tradeRow).join(''):'<div class="empty">no trades yet — they appear after the first settlement</div>';
    const feed=S.log.length?S.log.map(feedRow).join(''):'<div class="frow"><span class="t"></span><span class="empty" style="padding:0">waiting…</span></div>';
    // preserve scroll position of the log + trade table across the 1.5s refresh
    const _of=document.querySelector('.feed'), _ot=document.querySelector('.tbl');
    const _fs=_of?_of.scrollTop:0, _ts=_ot?_ot.scrollTop:0;
    $('wrap').innerHTML=`
      <div class="hero">
        <div class="cd" id="cd">--:--<small>until next settlement (top of hour UTC)</small></div>
        <span class="phasebadge" id="phase">idle</span>
        <div style="flex:1"></div>
        <div style="text-align:right;font-family:var(--bin);font-size:12px;color:var(--muted)">next settlement<br><span style="color:var(--txt)" id="settletime">—</span></div>
      </div>
      <div class="stats">
        <div class="stat"><div class="k">Balance</div><div class="v">$${S.balance.toLocaleString(undefined,{maximumFractionDigits:2})}</div></div>
        <div class="stat"><div class="k">Net P&L</div><div class="v ${cls(S.net_pnl)}">${fmtUsd(S.net_pnl)} <span style="font-size:11px">(${S.net_pnl>=0?'+':''}${S.net_pct.toFixed(2)}%)</span></div></div>
        <div class="stat"><div class="k">Win / Loss</div><div class="v">${S.wins}<span style="color:var(--muted)">/</span>${S.losses}</div></div>
        <div class="stat"><div class="k">Win rate</div><div class="v">${wr}</div></div>
        <div class="stat"><div class="k">Funding paid</div><div class="v neg">-$${S.total_funding_paid.toFixed(3)}</div></div>
      </div>
      <div class="panel"><div class="ph">Open positions ${S.opens&&S.opens.length?'('+S.opens.length+') · net '+fmtUsd(S.open_net)+' · each closes when net > 0':''}</div>${openHtml}
        <div class="ph" style="border-top:1px solid var(--line)">Live action log</div><div class="feed">${feed}</div></div>
      <div class="panel"><div class="ph">Funding rates now — it bets EVERY market each settlement (top 10 shown)</div>
        <div class="row lbrow" style="color:var(--muted);text-transform:uppercase;font-size:10px">
          <span>market</span><span>funding</span><span>bet dir</span></div>${lb}</div>
      <div class="panel" style="grid-column:1/-1"><div class="ph">Trade history — every bet, funding loss, and net result</div>
        <div class="thead"><span>closed</span><span>market</span><span>fund%</span><span>fund loss</span><span>entry→exit</span><span>price P&L</span><span>NET</span><span>%</span></div>
        <div class="tbl">${trades}</div></div>
      <div class="note">Paper mode: real Lighter funding + prices, simulated fills. We always sit on the funding-paying side, so "funding loss" is booked at settlement; the bet is that the price move beats it. Flip to real money once the timing proves out.</div>`;
    const _nf=document.querySelector('.feed'); if(_nf)_nf.scrollTop=_fs;
    const _nt=document.querySelector('.tbl'); if(_nt)_nt.scrollTop=_ts;
    tickCountdown();
  }
  function tickCountdown(){
    if(!S)return;
    const now=Date.now()/1000 + (S._skew||0);
    const settle=S.next_settle;
    const t=settle-now; const cd=$('cd'); const ph=$('phase'); const st=$('settletime');
    if(!cd)return;
    if(st) st.textContent=tstr(settle);
    let secs=Math.max(0,t), label='until settlement (top of hour UTC)', kls='';
    if(S.opens && S.opens.length){ label='in trade — closing each position when net-profitable'; kls='exit'; }
    else if(t<=S.entry_lead && t>0){ label='⚡ ENTRY WINDOW — betting now'; kls='entry'; }
    const mm=Math.floor(secs/60), ss=Math.floor(secs%60);
    cd.className='cd '+kls;
    cd.innerHTML=`${String(mm).padStart(2,'0')}:${String(ss).padStart(2,'0')}<small>${label}</small>`;
    if(ph){ const inTrade=(S.opens&&S.opens.length); const live=(t<=S.entry_lead);
      ph.textContent = inTrade?'IN TRADE':(t<=S.entry_lead&&t>0?'ENTERING':(S.enabled?'armed':'paused'));
      ph.className='phasebadge'+(live||inTrade?' live':''); }
  }
  async function load(){
    try{ const d=await(await fetch('/api/funding')).json(); d._skew=(d.now - Date.now()/1000); S=d; render(); }catch(e){}
  }
  async function toggle(){ if(!S)return; await fetch('/api/funding/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!S.enabled})}); load(); }
  async function resetBot(){ if(!confirm('Reset the funding bot paper account?'))return; await fetch('/api/funding/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); load(); }
  load(); setInterval(load,1500); setInterval(tickCountdown,250);
</script>
</body>
</html>
"""


# ===========================================================================
# The STOCKS page (kept as dead code; route removed — replaced by the Funding bot)
# ===========================================================================
STOCK_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stocks · AI news bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root{--bg:#0a0b0e;--panel:#101217;--panel2:#15181f;--line:#1d212b;--txt:#d7dbe4;
    --muted:#737a89;--amber:#f5c518;--green:#26a69a;--red:#ef5350;
    --mono:'IBM Plex Mono',ui-monospace,monospace;--sans:'IBM Plex Sans',system-ui,sans-serif;}
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans)}
  #topbar{display:flex;align-items:center;gap:13px;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
  .ticker{font-family:var(--mono);font-weight:600;font-size:18px;letter-spacing:.5px}
  .ticker .dot{color:var(--amber)}
  #topbar select{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:5px 9px;font-family:var(--mono);font-size:13px}
  .nav{font-family:var(--mono);font-size:13px;color:var(--amber);text-decoration:none;border:1px solid var(--line);padding:5px 10px;border-radius:6px}
  .nav:hover{background:var(--panel2)}
  .botbtn{font-family:var(--mono);font-size:13px;color:#0a0b0e;background:var(--amber);border:none;cursor:pointer;padding:6px 12px;border-radius:6px;font-weight:600}
  .botbtn:hover{filter:brightness(1.08)}
  .soundbtn{background:transparent;border:1px solid var(--line);color:var(--txt);border-radius:6px;cursor:pointer;font-size:14px;padding:3px 9px;line-height:1.2}
  .soundbtn:hover{border-color:var(--amber)}
  .soundbtn.off{opacity:.45}
  .spacer{flex:1}
  #count{font-family:var(--mono);font-size:13px;color:var(--amber)}
  #status{font-family:var(--mono);font-size:12px;color:var(--muted)}
  #chartwrap{position:relative;height:62vh;min-height:360px;border-bottom:1px solid var(--line)}
  #chart{position:absolute;inset:0}
  #popup{position:absolute;max-width:340px;max-height:60%;overflow:auto;z-index:30;background:#0e1116ee;backdrop-filter:blur(4px);border:1px solid var(--line);border-left:3px solid var(--amber);border-radius:8px;padding:10px 12px;box-shadow:0 10px 30px #000a;display:none;font-size:13px;line-height:1.45}
  #popup .ph{font-family:var(--mono);color:var(--amber);font-size:12px;margin-bottom:4px}
  #popup .pb{color:var(--txt);white-space:pre-wrap}
  #popup .pn+.pn{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
  #popup .close{position:sticky;top:0;float:right;color:var(--muted);cursor:pointer;font-size:14px}
  #feedwrap{padding:10px 0 40px}
  #feedwrap h3{font-family:var(--mono);font-weight:500;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin:14px 18px 8px}
  .item{display:grid;grid-template-columns:74px 150px 1fr;gap:10px;align-items:start;padding:8px 18px;border-bottom:1px solid #14171e}
  .item:hover{background:var(--panel)}
  .item .t{font-family:var(--mono);font-size:12px;color:var(--muted)}
  .b{font-family:var(--mono);font-size:11px;padding:2px 7px;border-radius:5px;text-align:center;white-space:nowrap}
  .b.skip{background:#1b1f29;color:#8b93a4}
  .b.trade{background:#12251f;color:var(--green);border:1px solid #1e3a31}
  .b.trade.bear{background:#2a1416;color:var(--red);border-color:#3a1e20}
  .item .meta{display:flex;flex-direction:column;gap:3px}
  .item .sym{font-family:var(--mono);font-size:11px;color:#8b93a4}
  .item .tx{font-size:13px;color:var(--txt)}
  /* drawer */
  #sbpanel{position:fixed;top:0;right:0;width:360px;height:100vh;overflow:auto;background:#0c0e13;border-left:1px solid var(--line);z-index:50;display:none}
  body.sb{padding-right:360px}
  body.sb #sbpanel{display:block}
  .sb-head{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line);font-family:var(--mono);font-weight:600;font-size:14px;background:var(--panel)}
  .sb-x{cursor:pointer;color:var(--muted)}
  .sb-top{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}
  .sb-state{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:12px}
  .dot8{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block}
  .dot8.on{background:var(--green);box-shadow:0 0 6px var(--green)}
  .dot8.off{background:var(--red)}
  .tgl{background:#1b1f29;border:1px solid var(--line);color:var(--txt);border-radius:6px;padding:5px 12px;font-family:var(--mono);font-size:11px;cursor:pointer}
  .tgl:hover{border-color:var(--amber)}
  .stats{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border-bottom:1px solid var(--line)}
  .stat{background:#0c0e13;padding:11px 16px;font-family:var(--mono)}
  .stat .l{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
  .stat .v{font-size:15px;font-weight:600;margin-top:2px}
  .rule{padding:10px 16px;color:var(--muted);font-size:11px;line-height:1.5;font-family:var(--mono);border-bottom:1px solid var(--line)}
  .pos{padding:10px 16px;border-bottom:1px solid #14171e;font-family:var(--mono);font-size:12px}
  .pos .top{display:flex;justify-content:space-between;align-items:center}
  .lng{color:var(--green);font-weight:600}.sht{color:var(--red);font-weight:600}
  .pos .det{color:var(--muted);font-size:11px;margin-top:4px;display:flex;justify-content:space-between;gap:6px}
  .head2{padding:12px 16px;font-family:var(--mono);font-size:12px;color:var(--amber);text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--line);border-top:3px solid var(--line);background:var(--panel)}
  .log{max-height:200px;overflow:auto;font-family:var(--mono);font-size:11px;padding:4px 0}
  .log .ln{padding:4px 16px;border-bottom:1px solid #14171e;display:flex;gap:8px}
  .log .ln .lt{color:#5a6273;white-space:nowrap}
  .log .open{color:var(--amber)}.log .win{color:var(--green)}.log .loss{color:var(--red)}
  .log .skip{color:#6b7282}.log .info{color:#9aa3b2}.log .error{color:var(--red)}
  .note{color:var(--muted);font-size:11px;line-height:1.45;font-family:var(--mono)}
  .reset{text-align:center;color:var(--muted);font-size:11px;font-family:var(--mono);padding:12px;cursor:pointer}
  .reset:hover{color:var(--red)}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ticker">Stocks<span class="dot">·</span>AI</span>
    <a class="nav" href="/">&larr; Crypto</a>
    <select id="symbol"></select>
    <select id="interval">
      <option value="1m">1m</option>
      <option value="5m" selected>5m</option>
      <option value="15m">15m</option>
      <option value="1h">1h</option>
      <option value="1d">1D</option>
      <option value="1w">1W</option>
    </select>
    <button class="botbtn" onclick="toggleBot()">Stock News Bot</button>
    <span class="spacer"></span>
    <button class="soundbtn" id="soundbtn" onclick="toggleSound()" title="news sound on/off">🔔</button>
    <span id="count">0 news</span>
    <span id="status">connecting…</span>
  </div>

  <div id="chartwrap"><div id="chart"></div><div id="popup"></div></div>

  <div id="feedwrap">
    <h3>News feed — every message (the bot judges each one)</h3>
    <div id="feed"></div>
  </div>

  <div id="sbpanel">
    <div class="sb-head"><span>Stock News Bot</span><span class="sb-x" onclick="toggleBot()">✕</span></div>
    <div class="sb-top">
      <div class="sb-state"><span id="sb-dot" class="dot8"></span><span id="sb-statetxt">…</span></div>
      <button id="sb-toggle" class="tgl" onclick="botToggle()">Pause</button>
    </div>
    <div class="stats">
      <div class="stat"><div class="l">Balance</div><div class="v" id="sb-balv">—</div></div>
      <div class="stat"><div class="l">Equity</div><div class="v" id="sb-eqv">—</div></div>
      <div class="stat"><div class="l">Total P&amp;L</div><div class="v" id="sb-pnlv">—</div></div>
      <div class="stat"><div class="l">Win rate · trades</div><div class="v" id="sb-winv">—</div></div>
    </div>
    <div class="rule">Gemini reads every headline and decides if it moves one of your stocks. On a TRADE call it buys/shorts that name, then exits on +<span id="tpr">4</span>% / −<span id="slr">2</span>% / time.</div>
    <div id="sb-poss"></div>
    <div class="head2">activity log</div>
    <div id="sb-log" class="log"></div>
    <div class="head2">recent trades</div>
    <div id="sb-hist"></div>
    <div class="reset" onclick="botReset()">reset bot</div>
  </div>

<script>
  const $ = (id) => document.getElementById(id);
  const chartEl = $('chart'), popup = $('popup');
  const chart = LightweightCharts.createChart(chartEl, {
    autoSize: true,
    layout:{background:{color:'#0a0b0e'},textColor:'#aab0bd',fontFamily:"'IBM Plex Mono',monospace"},
    grid:{vertLines:{color:'#14171e'},horzLines:{color:'#14171e'}},
    timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#1d212b'},
    rightPriceScale:{borderColor:'#1d212b'}, crosshair:{mode:0},
  });
  const series = chart.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,wickUpColor:'#26a69a',wickDownColor:'#ef5350'});
  const INT = {'1m':60,'5m':300,'15m':900,'1h':3600,'1d':86400,'1w':604800};
  const intervalSec = () => INT[$('interval').value] || 300;
  const snap = (ts) => Math.floor(ts/intervalSec())*intervalSec();
  let newsCache = [];
  function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
  const fmt = (n)=>(n>=0?'+':'')+Number(n).toFixed(2);
  const money = (n)=>'$'+Number(n).toLocaleString(undefined,{maximumFractionDigits:2});

  async function loadSymbols(){
    try{
      const j = await (await fetch('/api/stock/symbols')).json();
      const sel = $('symbol');
      sel.innerHTML = (j.symbols||[]).map(s=>'<option>'+s+'</option>').join('');
      let saved=null; try{ saved=localStorage.getItem('stk_sym'); }catch(e){}
      if(saved && (j.symbols||[]).includes(saved)) sel.value = saved;
    }catch(e){}
  }

  async function loadCandles(){
    try{
      const sym = $('symbol').value || 'AAPL';
      const r = await fetch('/api/stock/candles?symbol='+sym+'&interval='+$('interval').value);
      const j = await r.json();
      if(j.candles && j.candles.length){
        series.setData(j.candles);
        $('status').textContent = '● ' + sym;
        $('status').style.color = '#26a69a';
      } else {
        series.setData([]);
        $('status').textContent = j.error || 'no data';
        $('status').style.color = '#ef5350';
      }
      renderMarkers();
    }catch(e){ $('status').textContent='chart error'; $('status').style.color='#ef5350'; }
  }

  function renderMarkers(){
    const sym = $('symbol').value;
    const m = newsCache.filter(n => (n.symbols||[]).includes(sym)).map(n => ({
      time: snap(n.ts), position:'aboveBar',
      color: n.traded ? (n.direction==='bearish'?'#ef5350':'#26a69a') : '#f5c518',
      shape:'circle',
    }));
    m.sort((a,b)=>a.time-b.time);
    series.setMarkers(m);
  }

  function badge(n){
    if(n.traded){ const bear = n.direction==='bearish'?' bear':''; return '<span class="b trade'+bear+'">TRADE '+esc(n.direction)+'</span>'; }
    return '<span class="b skip">SKIP</span>';
  }
  function renderFeed(){
    const f=$('feed'); f.innerHTML='';
    newsCache.forEach(n=>{
      const t=new Date(n.ts*1000).toLocaleTimeString();
      const syms=(n.symbols&&n.symbols.length)?n.symbols.join(' '):'—';
      const d=document.createElement('div'); d.className='item';
      d.innerHTML='<span class="t">'+t+'</span><div class="meta">'+badge(n)+'<span class="sym">'+esc(syms)+'</span></div><div class="tx">'+esc(n.text)+'</div>';
      f.appendChild(d);
    });
  }
  async function loadNews(){
    try{
      const j = await (await fetch('/api/stock/news')).json();
      newsCache = j.news || [];
      $('count').textContent = newsCache.length + ' news';
      renderFeed(); renderMarkers();
      const maxTs = newsCache.reduce((m,n)=>Math.max(m,n.ts||0),0);
      if(newsReady && maxTs>lastNewsTs) chime();
      if(maxTs>lastNewsTs) lastNewsTs=maxTs;
      newsReady=true;
    }catch(e){}
  }

  // ---- sound ----
  let audioCtx=null, soundOn=true, lastNewsTs=0, newsReady=false;
  function initAudio(){ try{ if(!audioCtx) audioCtx=new (window.AudioContext||window.webkitAudioContext)(); if(audioCtx.state==='suspended') audioCtx.resume(); }catch(e){} }
  function chime(){ if(!soundOn) return; initAudio(); if(!audioCtx) return; const now=audioCtx.currentTime; [880,1320].forEach((f,i)=>{ const o=audioCtx.createOscillator(),g=audioCtx.createGain(),t=now+i*0.13; o.type='sine'; o.frequency.value=f; g.gain.setValueAtTime(0.0001,t); g.gain.exponentialRampToValueAtTime(0.3,t+0.02); g.gain.exponentialRampToValueAtTime(0.0001,t+0.2); o.connect(g); g.connect(audioCtx.destination); o.start(t); o.stop(t+0.22); }); }
  function toggleSound(){ soundOn=!soundOn; $('soundbtn').textContent=soundOn?'🔔':'🔕'; $('soundbtn').classList.toggle('off',!soundOn); try{localStorage.setItem('nw_sound',soundOn?'1':'0');}catch(e){} if(soundOn) chime(); }

  // ---- drawer ----
  function toggleBot(){ document.body.classList.toggle('sb'); try{localStorage.setItem('stk_drawer', document.body.classList.contains('sb')?'1':'');}catch(e){} if(document.body.classList.contains('sb')) loadBot(); }

  async function loadBot(){
    let s; try{ s = await (await fetch('/api/stock/bot/state')).json(); }catch(e){ return; }
    if(!s.running){ $('sb-statetxt').textContent='bot not running'; $('sb-dot').className='dot8 off'; return; }
    const dot=$('sb-dot'), tgl=$('sb-toggle');
    if(!s.enabled){ dot.className='dot8 off'; $('sb-statetxt').textContent='paused'; tgl.textContent='Resume'; }
    else if(!s.market_ok){ dot.className='dot8 off'; $('sb-statetxt').textContent='no Alpaca key / prices off'; tgl.textContent='Pause'; }
    else { dot.className='dot8 on'; $('sb-statetxt').textContent='live · reading news'; tgl.textContent='Pause'; }
    $('sb-balv').textContent=money(s.balance);
    $('sb-eqv').textContent=money(s.equity);
    const p=$('sb-pnlv'); p.textContent=fmt(s.total_pnl); p.style.color=s.total_pnl>=0?'var(--green)':'var(--red)';
    $('sb-winv').innerHTML = s.trades ? (Math.round(s.wins/s.trades*100)+'% <span style="color:#5a6273;font-size:11px">· '+s.trades+'</span>') : '—';
    const poss=$('sb-poss');
    if(s.positions && s.positions.length){
      poss.innerHTML = s.positions.map(p=>{ const up=p.pnl>=0;
        return '<div class="pos"><div class="top"><span class="'+(p.side==='long'?'lng':'sht')+'">'+p.symbol+' '+p.side.toUpperCase()+'</span><span style="color:'+(up?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(p.pnl)+' ('+fmt(p.pnl_pct)+'%)</span></div><div class="det"><span>entry '+money(p.entry)+(p.price?' → '+money(p.price):'')+'</span><span>margin '+money(p.margin)+'</span></div></div>';
      }).join('');
    } else { poss.innerHTML='<div class="note" style="padding:10px 16px">No open positions.</div>'; }
    const log=$('sb-log');
    log.innerHTML = (s.log&&s.log.length) ? s.log.map(l=>'<div class="ln"><span class="lt">'+new Date(l.t*1000).toLocaleTimeString()+'</span><span class="'+l.kind+'">'+esc(l.msg)+'</span></div>').join('') : '<div class="note" style="padding:10px 16px">No activity yet.</div>';
    const hist=$('sb-hist');
    hist.innerHTML = (s.history&&s.history.length) ? s.history.map(t=>'<div class="pos" style="padding:6px 16px"><div class="top"><span class="'+(t.side==='long'?'lng':'sht')+'">'+t.symbol+' '+t.side.toUpperCase()+'</span><span style="color:'+(t.pnl>=0?'var(--green)':'var(--red)')+';font-weight:600">'+fmt(t.pnl)+'</span></div><div class="det"><span>'+esc(t.reason)+'</span><span>'+money(t.entry)+' → '+money(t.exit)+'</span></div></div>').join('') : '<div class="note" style="padding:10px 16px">No closed trades yet.</div>';
  }
  async function botToggle(){ let s; try{ s=await (await fetch('/api/stock/bot/state')).json(); }catch(e){return;} await fetch('/api/stock/bot/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!s.enabled})}); loadBot(); }
  async function botReset(){ if(confirm('Reset the stock bot? Clears its balance, positions and log.')){ await fetch('/api/stock/bot/reset',{method:'POST'}); loadBot(); } }

  chart.subscribeClick(param=>{
    if(!param.time){ popup.style.display='none'; return; }
    const sym=$('symbol').value;
    const hits=newsCache.filter(n=>(n.symbols||[]).includes(sym) && snap(n.ts)===param.time);
    if(!hits.length){ popup.style.display='none'; return; }
    popup.innerHTML='<span class="close" onclick="document.getElementById(\'popup\').style.display=\'none\'">✕</span>'+hits.map(n=>{
      const when=new Date(n.ts*1000).toLocaleString();
      const head=n.traded?('TRADE '+n.direction+' '+(n.symbols||[]).join(',')):'SKIP';
      return '<div class="pn"><div class="ph">'+when+'  ·  '+esc(head)+'</div><div class="pb">'+esc(n.text)+'</div></div>';
    }).join('');
    const pt=param.point||{x:60,y:50};
    popup.style.left=Math.max(8,Math.min(pt.x+18,chartEl.clientWidth-350))+'px';
    popup.style.top=Math.max(8,Math.min(pt.y+12,chartEl.clientHeight-80))+'px';
    popup.style.display='block';
  });

  $('symbol').addEventListener('change', ()=>{ try{localStorage.setItem('stk_sym',$('symbol').value);}catch(e){} loadCandles(); });
  $('interval').addEventListener('change', ()=>{ try{localStorage.setItem('stk_int',$('interval').value);}catch(e){} loadCandles(); });

  (async ()=>{
    await loadSymbols();
    try{ const si=localStorage.getItem('stk_int'); if(si) $('interval').value=si; }catch(e){}
    loadCandles(); loadNews(); loadBot();
    setInterval(loadCandles, 15000);
    setInterval(loadNews, 4000);
    setInterval(loadBot, 2500);
    try{
      if(localStorage.getItem('nw_sound')==='0'){ soundOn=false; $('soundbtn').textContent='🔕'; $('soundbtn').classList.add('off'); }
      if(localStorage.getItem('stk_drawer')==='1') document.body.classList.add('sb');
    }catch(e){}
    document.addEventListener('click', initAudio, { once:true });
  })();
</script>
</body>
</html>
"""


# ===========================================================================
# The PERFORMANCE / JOURNAL page (/journal): win rate, avg win/loss, profit
# factor, expectancy, an equity curve, exit-reason breakdown, recent trades.
# ===========================================================================
JOURNAL_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Performance · Journal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#0a0b0e;--panel:#101217;--panel2:#15181f;--line:#1d212b;--txt:#d7dbe4;
    --muted:#737a89;--amber:#f5c518;--green:#26a69a;--red:#ef5350;
    --mono:'IBM Plex Mono',ui-monospace,monospace;--sans:'IBM Plex Sans',system-ui,sans-serif;}
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans)}
  #topbar{display:flex;align-items:center;gap:13px;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
  .ttl{font-family:var(--mono);font-weight:600;font-size:18px;letter-spacing:.5px}
  .ttl .dot{color:var(--amber)}
  .nav{font-family:var(--mono);font-size:13px;color:var(--amber);text-decoration:none;border:1px solid var(--line);padding:5px 10px;border-radius:6px}
  .nav:hover{background:var(--panel2)}
  #topbar select{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:5px 9px;font-family:var(--mono);font-size:13px}
  .spacer{flex:1}
  .wrap{max-width:1100px;margin:0 auto;padding:18px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
  .card .l{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .card .v{font-family:var(--mono);font-size:22px;font-weight:600;margin-top:5px}
  .card .s{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
  .sec{font-family:var(--mono);font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin:8px 2px 10px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:18px}
  .empty{color:var(--muted);font-family:var(--mono);font-size:13px;padding:40px 10px;text-align:center}
  .reasons{display:flex;flex-wrap:wrap;gap:8px}
  .rchip{font-family:var(--mono);font-size:11px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:5px 9px;color:var(--txt)}
  .rchip b{color:var(--amber)}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
  th{text-align:left;color:var(--muted);font-weight:500;text-transform:uppercase;font-size:10px;letter-spacing:.5px;padding:6px 8px;border-bottom:1px solid var(--line)}
  td{padding:6px 8px;border-bottom:1px solid #14171e}
  .lng{color:var(--green)}.sht{color:var(--red)}
  .pos{color:var(--green)}.neg{color:var(--red)}
  .acct{color:var(--muted)}
  .note{color:var(--muted);font-size:11px;font-family:var(--mono);line-height:1.5;margin-top:6px}
  .resetbtn{font-family:var(--mono);font-size:13px;color:var(--red);background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:5px 11px;cursor:pointer}
  .resetbtn:hover{border-color:var(--red);background:#1b1316}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ttl">Performance<span class="dot">·</span>Journal</span>
    <a class="nav" href="/">&larr; Crypto</a>
    <a class="nav" href="/funding">Funding Bot</a>
    <select id="acct" onchange="load()">
      <option value="all">All accounts</option>
      <option value="manual">Manual</option>
      <option value="news">Crypto news bot</option>
      <option value="whale">News + Whale bot</option>
      <option value="stock">Stock news bot</option>
    </select>
    <button class="resetbtn" id="resetbtn" onclick="resetJournal()">↺ Reset</button>
    <span class="spacer"></span>
    <span id="sub" style="font-family:var(--mono);font-size:12px;color:var(--muted)"></span>
  </div>

  <div class="wrap">
    <div class="cards" id="cards"></div>
    <div class="sec">Equity curve — cumulative P&amp;L over your closed trades</div>
    <div class="panel"><div id="eqchart"><div class="empty">Loading…</div></div>
      <div class="note" id="eqnote"></div>
    </div>
    <div class="sec">How trades closed</div>
    <div class="panel"><div class="reasons" id="reasons"></div></div>
    <div class="sec">Recent trades</div>
    <div class="panel" style="padding:4px 0"><div id="trades"></div></div>
  </div>

<script>
  const $=(id)=>document.getElementById(id);
  const ACCT={news:'news bot',manual:'manual',whale:'whale bot',stock:'stock bot'};
  const RESET_EP={manual:'/api/manual/reset',news:'/api/bot/reset',whale:'/api/nwbot/reset',stock:'/api/stock/bot/reset'};
  const RESET_NAME={manual:'Manual paper account',news:'Crypto news bot',whale:'News + Whale bot',stock:'Stock news bot'};

  async function resetJournal(){
    const a=$('acct').value;
    if(a==='all'){
      if(!confirm('Reset ALL FOUR accounts?\n\nClears balance, open positions and trade history for: Manual, Crypto news bot, News + Whale bot, and Stock news bot.\n\nThis cannot be undone.')) return;
      await Promise.all(Object.values(RESET_EP).map(u=>fetch(u,{method:'POST'}).catch(()=>{})));
    } else {
      if(!confirm('Reset the '+RESET_NAME[a]+'?\n\nClears its balance, open positions and trade history.\n\nThis cannot be undone.')) return;
      await fetch(RESET_EP[a],{method:'POST'}).catch(()=>{});
    }
    load();
  }
  const money=(n)=>(n<0?'-$':'$')+Math.abs(Number(n)).toLocaleString(undefined,{maximumFractionDigits:2});
  const fmtUsd=(n)=>(n>=0?'+':'-')+'$'+Math.abs(Math.round(n)).toLocaleString();

  function card(l,v,cls,s){ return '<div class="card"><div class="l">'+l+'</div><div class="v'+(cls?' '+cls:'')+'">'+v+'</div>'+(s?'<div class="s">'+s+'</div>':'')+'</div>'; }

  function drawEquity(eq){
    const el=$('eqchart');
    const pts = (eq||[]).map(e=>e.v);
    if(pts.length<2){ el.innerHTML='<div class="empty">Not enough closed trades yet — make a few (or let a bot run) and they\'ll plot here.</div>'; return; }
    const W=1000,H=240,pad=30;
    let min=Math.min(0,...pts), max=Math.max(0,...pts); if(min===max)max=min+1;
    const X=i=>pad+i/(pts.length-1)*(W-2*pad);
    const Y=v=>H-pad-(v-min)/(max-min)*(H-2*pad);
    const poly=pts.map((v,i)=>X(i).toFixed(1)+','+Y(v).toFixed(1)).join(' ');
    const up=pts[pts.length-1]>=0, col=up?'#26a69a':'#ef5350';
    const z=Y(0);
    let s='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none" style="width:100%;height:240px;display:block">';
    s+='<line x1="'+pad+'" y1="'+z.toFixed(1)+'" x2="'+(W-pad)+'" y2="'+z.toFixed(1)+'" stroke="#2a2f3a" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>';
    s+='<polyline points="'+poly+'" fill="none" stroke="'+col+'" stroke-width="2" vector-effect="non-scaling-stroke"/>';
    s+='<text x="6" y="14" fill="#737a89" font-size="11">'+fmtUsd(max)+'</text>';
    s+='<text x="6" y="'+(H-8)+'" fill="#737a89" font-size="11">'+fmtUsd(min)+'</text>';
    s+='</svg>';
    el.innerHTML=s;
  }

  async function load(){
    const acct=$('acct').value;
    $('resetbtn').textContent = acct==='all' ? '↺ Reset all' : '↺ Reset '+(ACCT[acct]||acct);
    let s; try{ s=await (await fetch('/api/journal?account='+acct)).json(); }catch(e){ return; }
    // sub line with counts
    const c=s.counts||{};
    $('sub').textContent='manual '+(c.manual||0)+' · news '+(c.news||0)+' · whale '+(c.whale||0)+' · stock '+(c.stock||0)+' trades';
    // cards
    const pf = (s.profit_factor!=null) ? s.profit_factor : (s.has_wins?'∞':'—');
    const wr = s.trades? s.win_rate+'%' : '—';
    $('cards').innerHTML =
      card('Net P&L', s.trades?fmtUsd(s.total_pnl):'—', s.total_pnl>=0?'pos':'neg', s.trades?(s.trades+' trades'):'') +
      card('Win rate', wr, '', s.trades?(s.wins+'W · '+s.losses+'L'):'') +
      card('Profit factor', pf, (s.profit_factor!=null&&s.profit_factor>=1)||(s.profit_factor==null&&s.has_wins)?'pos':(s.profit_factor!=null?'neg':''), 'gross win ÷ loss') +
      card('Expectancy', s.trades?fmtUsd(s.expectancy):'—', s.expectancy>=0?'pos':'neg', 'avg per trade') +
      card('Avg win', s.wins?money(s.avg_win):'—','pos') +
      card('Avg loss', s.losses?money(s.avg_loss):'—','neg') +
      card('Best', s.trades?fmtUsd(s.best):'—','pos') +
      card('Worst', s.trades?fmtUsd(s.worst):'—','neg');
    drawEquity(s.equity);
    // note about win rate vs payoff
    if(s.trades){
      $('eqnote').textContent = 'A high win rate alone doesn\'t mean profitable — what matters is win rate combined with avg win vs avg loss. Profit factor above 1.0 (and a rising curve) is the real test.';
    } else { $('eqnote').textContent=''; }
    // reasons
    const r=s.reasons||{};
    const keys=Object.keys(r);
    $('reasons').innerHTML = keys.length ? keys.sort((a,b)=>r[b]-r[a]).map(k=>'<span class="rchip">'+k+' <b>'+r[k]+'</b></span>').join('') : '<span class="note">No closed trades yet.</span>';
    // recent table
    const rows=s.recent||[];
    if(!rows.length){ $('trades').innerHTML='<div class="empty">No closed trades yet.</div>'; return; }
    let h='<table><thead><tr><th>When</th><th>Account</th><th>Symbol</th><th>Side</th><th>Entry → Exit</th><th>Reason</th><th style="text-align:right">P&L</th></tr></thead><tbody>';
    rows.forEach(t=>{
      const when=t.closed_at?new Date(t.closed_at*1000).toLocaleString():'—';
      h+='<tr><td class="acct">'+when+'</td><td class="acct">'+(ACCT[t.account]||t.account)+'</td><td>'+(t.symbol||'')+'</td>'+
         '<td class="'+(t.side==='long'?'lng':'sht')+'">'+(t.side||'').toUpperCase()+'</td>'+
         '<td>'+money(t.entry)+' → '+money(t.exit)+'</td><td class="acct">'+(t.reason||'')+'</td>'+
         '<td style="text-align:right" class="'+(t.pnl>=0?'pos':'neg')+'">'+fmtUsd(t.pnl)+'</td></tr>';
    });
    h+='</tbody></table>';
    $('trades').innerHTML=h;
  }
  load();
  setInterval(load, 5000);
</script>
</body>
</html>
"""


# ===========================================================================
# REAL-MONEY Lighter page
# ===========================================================================
LIGHTER_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lighter · REAL MONEY</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0b0e; --panel:#101217; --panel2:#15181f; --line:#1d212b;
    --txt:#d7dbe4; --muted:#737a89; --amber:#f5c518; --green:#26a69a; --red:#ef5350;
    --mono:'IBM Plex Mono', ui-monospace, monospace;
    --sans:'IBM Plex Sans', system-ui, sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans)}
  #topbar{display:flex;align-items:center;gap:14px;padding:11px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
  .ttl{font-family:var(--mono);font-weight:600;letter-spacing:.5px}
  .ttl .dot{color:var(--red);margin:0 6px}
  a.nav{color:var(--muted);text-decoration:none;font-family:var(--mono);font-size:13px}
  a.nav:hover{color:var(--txt)}
  .spacer{flex:1}
  .wrap{max-width:920px;margin:0 auto;padding:18px 16px 60px}
  .banner{background:#2a0f12;border:1px solid var(--red);border-radius:10px;padding:12px 15px;margin-bottom:18px;font-family:var(--mono);font-size:13px;color:#ffb4b4;line-height:1.5}
  .banner b{color:#fff}
  .badge{display:inline-block;font-family:var(--mono);font-size:10px;padding:2px 7px;border-radius:5px;border:1px solid var(--line);color:var(--muted);margin-left:8px;text-transform:uppercase}
  .badge.on{color:#08130f;background:var(--green);border-color:var(--green)}
  .badge.off{color:#1b1207;background:var(--amber);border-color:var(--amber)}
  .badge.test{color:#1b1207;background:var(--amber);border-color:var(--amber)}
  .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
  .card .l{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .card .v{font-family:var(--mono);font-size:22px;font-weight:600;margin-top:5px}
  .sec{font-family:var(--mono);font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin:8px 2px 10px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:18px}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
  th{text-align:left;color:var(--muted);font-weight:500;text-transform:uppercase;font-size:10px;letter-spacing:.5px;padding:6px 8px;border-bottom:1px solid var(--line)}
  td{padding:7px 8px;border-bottom:1px solid #14171e}
  .lng{color:var(--green)}.sht{color:var(--red)}
  .pos{color:var(--green)}.neg{color:var(--red)}
  .empty{color:var(--muted);font-family:var(--mono);font-size:13px;padding:26px 10px;text-align:center;line-height:1.6}
  .trade{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap}
  .fld{display:flex;flex-direction:column;gap:5px}
  .fld label{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .fld input{font-family:var(--mono);font-size:15px;background:var(--panel2);border:1px solid var(--line);border-radius:7px;color:var(--txt);padding:9px 11px;width:150px}
  .btn{font-family:var(--mono);font-size:14px;font-weight:600;border:none;border-radius:7px;padding:11px 22px;cursor:pointer}
  .btn.long{background:var(--green);color:#08130f}
  .btn.short{background:var(--red);color:#1b0c0c}
  .btn:disabled{opacity:.4;cursor:not-allowed}
  .btn.close{background:var(--panel2);color:var(--red);border:1px solid var(--line);font-weight:500;padding:5px 12px;font-size:12px}
  .btn.close:hover{border-color:var(--red)}
  .ghost{font-family:var(--mono);font-size:12px;color:var(--muted);background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:6px 11px;cursor:pointer}
  .ghost:hover{color:var(--txt)}
  .msg{font-family:var(--mono);font-size:12px;margin-top:14px;line-height:1.5;white-space:pre-wrap}
  .msg.err{color:var(--red)}
  .msg.ok{color:var(--green)}
  .note{color:var(--muted);font-size:11px;font-family:var(--mono);line-height:1.6;margin-top:10px}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ttl">Lighter<span class="dot">●</span>REAL MONEY</span>
    <a class="nav" href="/">&larr; Crypto</a>
    <a class="nav" href="/journal">Journal</a>
    <span class="spacer"></span>
    <button class="ghost" onclick="load()">Refresh</button>
  </div>
  <div class="wrap">
    <div class="banner">
      <b>&#9888; REAL MONEY.</b> This page trades your actual Lighter perps account. Orders spend real funds and can lose money. Nothing here is financial advice.
      <span id="badges"></span>
    </div>

    <div class="cards">
      <div class="card"><div class="l">BTC price</div><div class="v" id="px">&mdash;</div></div>
      <div class="card"><div class="l">Perps collateral</div><div class="v" id="bal">&mdash;</div></div>
      <div class="card"><div class="l">Available</div><div class="v" id="avail">&mdash;</div></div>
    </div>

    <div class="sec">Open positions</div>
    <div class="panel"><div id="positions"><div class="empty">&hellip;</div></div></div>

    <div class="sec">Place order &mdash; BTC perps</div>
    <div class="panel">
      <div class="trade">
        <div class="fld">
          <label>Size (USD notional)</label>
          <input id="usd" type="number" min="0" step="1" placeholder="e.g. 25" />
        </div>
        <button class="btn long"  id="longbtn"  onclick="place('buy')">LONG</button>
        <button class="btn short" id="shortbtn" onclick="place('sell')">SHORT</button>
      </div>
      <div class="note">
        Size is the position's dollar value. Margin used depends on the leverage you set for BTC inside Lighter's own app.
        Market orders fill right away; a small fee/spread applies. You choose the amount &mdash; there is no cap.
      </div>
      <div class="msg" id="msg"></div>
    </div>
  </div>

<script>
function fmtUsd(x){ if(x===null||x===undefined||isNaN(x)) return "\u2014"; return "$"+Number(x).toLocaleString(undefined,{maximumFractionDigits:2}); }
function fmtNum(x,d){ if(x===null||x===undefined||isNaN(x)) return "\u2014"; return Number(x).toLocaleString(undefined,{maximumFractionDigits:(d===undefined?2:d)}); }

let TRADING=false, busy=false;

async function load(){
  if(busy) return;
  busy=true;
  try{
    const r = await fetch("/api/lighter/state");
    render(await r.json());
  }catch(e){
    const m=document.getElementById("msg"); m.className="msg err"; m.textContent="could not reach the bot: "+e;
  }finally{ busy=false; }
}

function render(s){
  const badges=document.getElementById("badges");
  const msg=document.getElementById("msg");

  if(!s.configured){
    badges.innerHTML='<span class="badge off">not set up</span>';
    document.getElementById("positions").innerHTML =
      '<div class="empty">Lighter isn\'t set up yet.<br>Missing in config.py: '+((s.missing||[]).join(", ")||"?")+'<br>Fill them in and restart the bot.</div>';
    setButtons(false); return;
  }
  if(!s.ok){
    badges.innerHTML='<span class="badge off">error</span>';
    document.getElementById("positions").innerHTML='<div class="empty">Lighter error:<br>'+(s.error||"unknown")+'</div>';
    setButtons(false); return;
  }

  TRADING = !!s.trading_enabled;
  let b = (TRADING?'<span class="badge on">trading ON</span>':'<span class="badge off">trading OFF</span>');
  if(s.testnet) b += '<span class="badge test">testnet</span>';
  badges.innerHTML = b;

  document.getElementById("px").textContent    = fmtUsd(s.price);
  document.getElementById("bal").textContent   = s.balance? fmtUsd(s.balance.total):"\u2014";
  document.getElementById("avail").textContent = s.balance? fmtUsd(s.balance.free):"\u2014";

  const pos = s.positions||[];
  const box = document.getElementById("positions");
  if(!pos.length){
    box.innerHTML='<div class="empty">no open positions</div>';
  }else{
    let h='<table><tr><th>Market</th><th>Side</th><th>Size</th><th>Entry</th><th>Unrealized PnL</th><th>Lev</th><th></th></tr>';
    for(const p of pos){
      const long=(p.side==="long"); const pnl=p.pnl;
      h+='<tr><td>'+(p.symbol||"")+'</td>'
        +'<td class="'+(long?"lng":"sht")+'">'+String(p.side||"").toUpperCase()+'</td>'
        +'<td>'+fmtNum(p.size,6)+'</td>'
        +'<td>'+fmtNum(p.entry,2)+'</td>'
        +'<td class="'+((pnl||0)>=0?"pos":"neg")+'">'+fmtUsd(pnl)+'</td>'
        +'<td>'+(p.leverage?fmtNum(p.leverage,0)+"x":"\u2014")+'</td>'
        +'<td><button class="btn close" onclick="closePos()">Close</button></td></tr>';
    }
    h+='</table>'; box.innerHTML=h;
  }

  setButtons(TRADING);
  if(!TRADING){ msg.className="msg"; msg.textContent="Real trading is OFF. To enable: set  LIGHTER_TRADING_ENABLED = True  in config.py and restart."; }
}

function setButtons(on){
  document.getElementById("longbtn").disabled=!on;
  document.getElementById("shortbtn").disabled=!on;
}

async function place(side){
  const usd = parseFloat(document.getElementById("usd").value);
  const msg = document.getElementById("msg");
  if(!usd || usd<=0){ msg.className="msg err"; msg.textContent="enter a dollar amount first."; return; }
  const word = side==="buy"?"LONG":"SHORT";
  msg.className="msg"; msg.textContent="placing "+word+" \u2026";
  setButtons(false);
  try{
    const r = await fetch("/api/lighter/order",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({side:side,usd:usd})});
    const d = await r.json();
    if(d.ok){ msg.className="msg ok"; msg.textContent="\u2713 "+word+" "+(d.type||"market")+" placed: "+fmtNum(d.amount,6)+" BTC (~"+fmtUsd(d.notional)+")  \u00b7  id "+(d.id||"?")+(d.lev_note?"  ["+d.lev_note+"]":""); }
    else{ msg.className="msg err"; msg.textContent="order failed: "+(d.error||"unknown"); }
  }catch(e){ msg.className="msg err"; msg.textContent="order failed: "+e; }
  load();
}

async function closePos(){
  const msg=document.getElementById("msg");
  msg.className="msg"; msg.textContent="closing \u2026";
  try{
    const r = await fetch("/api/lighter/close",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})});
    const d = await r.json();
    if(d.ok){ msg.className="msg ok"; msg.textContent="\u2713 close sent: "+fmtNum(d.closed,6)+" BTC  \u00b7  id "+(d.id||"?"); }
    else{ msg.className="msg err"; msg.textContent="close failed: "+(d.error||"unknown"); }
  }catch(e){ msg.className="msg err"; msg.textContent="close failed: "+e; }
  load();
}

load();
setInterval(load, 8000);
</script>
</body>
</html>
"""

LIGHTERBOT_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lighter Bot · REAL MONEY</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0b0e; --panel:#101217; --panel2:#15181f; --line:#1d212b;
    --txt:#d7dbe4; --muted:#737a89; --amber:#f5c518; --green:#26a69a; --red:#ef5350;
    --mono:'IBM Plex Mono', ui-monospace, monospace;
    --sans:'IBM Plex Sans', system-ui, sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans)}
  #topbar{display:flex;align-items:center;gap:14px;padding:11px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
  .ttl{font-family:var(--mono);font-weight:600;letter-spacing:.5px}
  .ttl .dot{color:var(--red);margin:0 6px}
  a.nav{color:var(--muted);text-decoration:none;font-family:var(--mono);font-size:13px}
  a.nav:hover{color:var(--txt)}
  .spacer{flex:1}
  .wrap{max-width:980px;margin:0 auto;padding:18px 16px 60px}
  .banner{background:#2a0f12;border:1px solid var(--red);border-radius:10px;padding:12px 15px;margin-bottom:16px;font-family:var(--mono);font-size:13px;color:#ffb4b4;line-height:1.5}
  .banner b{color:#fff}
  .badge{display:inline-block;font-family:var(--mono);font-size:10px;padding:2px 7px;border-radius:5px;border:1px solid var(--line);color:var(--muted);margin-left:8px;text-transform:uppercase}
  .badge.on{color:#08130f;background:var(--green);border-color:var(--green)}
  .badge.off{color:#1b1207;background:var(--amber);border-color:var(--amber)}
  .badge.watch{color:#1b1207;background:var(--amber);border-color:var(--amber)}
  .ctrls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 15px;margin-bottom:16px}
  .bigbtn{font-family:var(--mono);font-size:14px;font-weight:600;border:1px solid var(--line);border-radius:8px;padding:11px 20px;cursor:pointer;background:var(--panel2);color:var(--txt)}
  .bigbtn:disabled{opacity:.4;cursor:not-allowed}
  .bigbtn.run{background:var(--green);color:#08130f;border-color:var(--green)}
  .bigbtn.pause{background:var(--amber);color:#1b1207;border-color:var(--amber)}
  .bigbtn.kill{background:var(--red);color:#1b0c0c;border-color:var(--red)}
  .bigbtn.kill:hover{filter:brightness(1.08)}
  .levwrap{display:flex;align-items:center;gap:7px;margin-left:auto}
  .levwrap label{font-family:var(--mono);font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .levwrap input{font-family:var(--mono);font-size:14px;background:var(--panel2);border:1px solid var(--line);border-radius:7px;color:var(--txt);padding:8px 10px;width:70px}
  .ghost{font-family:var(--mono);font-size:12px;color:var(--muted);background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:8px 12px;cursor:pointer}
  .ghost:hover{color:var(--txt)}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
  .card .l{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .card .v{font-family:var(--mono);font-size:20px;font-weight:600;margin-top:5px}
  .sec{font-family:var(--mono);font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin:8px 2px 10px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
  th{text-align:left;color:var(--muted);font-weight:500;text-transform:uppercase;font-size:10px;letter-spacing:.5px;padding:6px 8px;border-bottom:1px solid var(--line)}
  td{padding:7px 8px;border-bottom:1px solid #14171e;vertical-align:top}
  .lng{color:var(--green)}.sht{color:var(--red)}
  .pos{color:var(--green)}.neg{color:var(--red)}
  .news{color:var(--muted);max-width:360px;white-space:normal;line-height:1.4}
  .empty{color:var(--muted);font-family:var(--mono);font-size:13px;padding:26px 10px;text-align:center;line-height:1.6}
  .btn.close{background:var(--panel2);color:var(--red);border:1px solid var(--line);font-weight:500;padding:5px 12px;font-size:12px;font-family:var(--mono);border-radius:6px;cursor:pointer}
  .btn.close:hover{border-color:var(--red)}
  .msg{font-family:var(--mono);font-size:12px;margin-top:6px;line-height:1.5;white-space:pre-wrap;min-height:16px}
  .msg.err{color:var(--red)} .msg.ok{color:var(--green)}
  .note{color:var(--muted);font-size:11px;font-family:var(--mono);line-height:1.6;margin-top:6px}
  .logrow{font-family:var(--mono);font-size:12px;padding:4px 0;border-bottom:1px solid #14171e;color:var(--txt)}
  .logrow .tt{color:var(--muted);margin-right:8px}
  .logrow.skip{color:var(--muted)} .logrow.open{color:var(--green)} .logrow.win{color:var(--green)} .logrow.loss{color:var(--red)} .logrow.error{color:var(--red)}
</style>
</head>
<body>
  <div id="topbar">
    <span class="ttl">Lighter Bot<span class="dot">&#9679;</span>REAL MONEY &middot; News+Whale</span>
    <a class="nav" href="/">&larr; Crypto</a>
    <a class="nav" href="/lighter">Manual Lighter</a>
    <a class="nav" href="/journal">Paper Journal</a>
    <span class="spacer"></span>
    <span id="badges"></span>
    <button class="ghost" onclick="load()">Refresh</button>
  </div>
  <div class="wrap">
    <div class="banner">
      <b>&#9888; REAL MONEY, autonomous.</b> Same strategy as the paper News+Whale bot, but it places REAL leveraged orders on your Lighter account on its own and can lose your funds.
      <b>PAUSE</b> stops new trades. <b>CLOSE</b> flattens now. It won't place a single real order until <b>LIGHTER_TRADING_ENABLED = True</b> in config.py &mdash; until then it logs what it WOULD do.
    </div>

    <div class="ctrls">
      <button class="bigbtn" id="togglebtn" onclick="toggleBot()">&hellip;</button>
      <button class="bigbtn kill" id="killbtn" onclick="closeNow()">CLOSE POSITION NOW</button>
      <div class="levwrap">
        <label>Leverage</label>
        <input id="lev" type="number" min="1" step="1" />
        <button class="ghost" onclick="setLev()">Set</button>
        <span class="note" id="levmax" style="margin:0 0 0 4px"></span>
      </div>
      <div class="levwrap">
        <label>Strategy</label>
        <select id="strat" onchange="setStrategy()" style="font-family:var(--mono);font-size:13px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;color:var(--txt);padding:6px 8px"></select>
      </div>
      <div class="levwrap">
        <label>Stats</label>
        <button class="btn close" onclick="resetStats()">&#8634; Reset win rate</button>
      </div>
    </div>
    <div class="msg" id="msg"></div>

    <div class="cards">
      <div class="card"><div class="l">BTC price</div><div class="v" id="px">&mdash;</div></div>
      <div class="card"><div class="l">Free balance</div><div class="v" id="bal">&mdash;</div></div>
      <div class="card"><div class="l">Bot realized P&amp;L</div><div class="v" id="pnl">&mdash;</div></div>
      <div class="card"><div class="l">Win rate &middot; trades</div><div class="v" id="win">&mdash;</div></div>
    </div>

    <div class="sec">Current position</div>
    <div class="panel"><div id="position"><div class="empty">&hellip;</div></div></div>

    <div class="sec">Trade journal &mdash; every bot trade + the news that triggered it</div>
    <div class="panel"><div id="journal"><div class="empty">&hellip;</div></div></div>

    <div class="sec">Activity log</div>
    <div class="panel"><div id="log"><div class="empty">&hellip;</div></div></div>
  </div>

<script>
function fmtUsd(x){ if(x===null||x===undefined||isNaN(x)) return "\u2014"; var n=Number(x); return (n<0?"-$":"$")+Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:2}); }
function fmtNum(x,d){ if(x===null||x===undefined||isNaN(x)) return "\u2014"; return Number(x).toLocaleString(undefined,{maximumFractionDigits:(d===undefined?2:d)}); }
function clk(t){ try{ return new Date(t*1000).toLocaleTimeString(); }catch(e){ return ""; } }
function esc(s){ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

let ENABLED=false, busy=false, levTouched=false;
document.getElementById("lev").addEventListener("input", function(){ levTouched=true; });

async function load(){
  if(busy) return; busy=true;
  try{ const r = await fetch("/api/lighterbot/state"); render(await r.json()); }
  catch(e){ const m=document.getElementById("msg"); m.className="msg err"; m.textContent="could not reach the bot: "+e; }
  finally{ busy=false; }
}

function render(s){
  if(s.error && !s.configured===undefined){ /* fallthrough */ }
  const badges=document.getElementById("badges");
  ENABLED = !!s.enabled;
  let b = ENABLED ? '<span class="badge on">running</span>' : '<span class="badge off">paused</span>';
  b += s.trading_enabled ? '<span class="badge on">trading ON</span>' : '<span class="badge off">dry-run</span>';
  if(s.watching) b += '<span class="badge watch">watching news</span>';
  badges.innerHTML = b;

  const tb=document.getElementById("togglebtn");
  tb.textContent = ENABLED ? "\u275a\u275a  PAUSE" : "\u25b6  RESUME";
  tb.className = "bigbtn " + (ENABLED ? "pause" : "run");

  document.getElementById("px").textContent  = fmtUsd(s.price);
  document.getElementById("bal").textContent = (s.balance_free!=null)? fmtUsd(s.balance_free) : "\u2014";
  const pnlEl=document.getElementById("pnl");
  pnlEl.textContent = fmtUsd(s.net_pnl); pnlEl.className = "v " + ((s.net_pnl||0)>=0?"pos":"neg");
  document.getElementById("win").innerHTML = s.trades ? (Math.round(s.wins/s.trades*100)+'% <span style="color:#5a6273;font-size:12px">\u00b7 '+s.trades+'</span>') : "\u2014";

  if(!levTouched && s.leverage!=null) document.getElementById("lev").value = s.leverage;
  document.getElementById("levmax").textContent = s.max_leverage ? ("max "+s.max_leverage+"x on Lighter") : "";

  const sel=document.getElementById("strat");
  if(sel && s.strategies){
    const want = Object.keys(s.strategies).join(",");
    if(sel.dataset.keys !== want){
      sel.innerHTML = Object.entries(s.strategies).map(function(kv){ return '<option value="'+kv[0]+'">'+esc(kv[1])+'</option>'; }).join('');
      sel.dataset.keys = want;
    }
    if(s.strategy) sel.value = s.strategy;     // server is the source of truth
  }

  const msg=document.getElementById("msg");
  if(!s.configured){
    msg.className="msg err";
    msg.textContent="Lighter isn't set up. Missing in config.py: "+((s.why_not||[]).join(", ")||"?")+". Fill them in and restart.";
  } else if(!s.trading_enabled){
    msg.className="msg"; msg.textContent="Dry-run: set  LIGHTER_TRADING_ENABLED = True  in config.py and restart to place REAL orders. Until then the bot only logs what it would do.";
  } else if(!s.live_ok && s.live_error){
    msg.className="msg err"; msg.textContent="Lighter read error: "+s.live_error;
  } else if(s.strategy==="fv_live" && msg.dataset.sticky!=="1"){
    msg.className="msg"; msg.textContent=(s.fv_status||"watching fair-value deviation")+
      " · enters at ±"+(s.fv_enter_bps||"?")+" bps, exits near ±"+(s.fv_exit_bps||"?")+" bps";
  } else if(s.strategy==="tv_bb_rsi_live" && msg.dataset.sticky!=="1"){
    msg.className="msg"; msg.textContent="Mirroring paper Bollinger + RSI Double (ChartArt): 5m BB+RSI entries, exit when paper exits.";
  } else if(msg.dataset.sticky!=="1"){
    msg.className="msg"; msg.textContent="";
  }

  // current position (the bot's own view, with live P&L)
  const pbox=document.getElementById("position");
  const p=s.position;
  if(!p){
    let extra="";
    if((s.live_positions||[]).length){ extra='<br><span style="color:#5a6273">(a position exists on Lighter the bot isn\'t tracking \u2014 the bot will reconcile, or use CLOSE / the Manual page)</span>'; }
    const wait = s.strategy==="fv_live" ? "watching fair-value deviation" : (s.strategy==="tv_bb_rsi_live" ? "mirroring paper Bollinger + RSI Double" : "waiting for news");
    pbox.innerHTML='<div class="empty">no position &mdash; '+wait+extra+'</div>';
  } else {
    const long=(p.side==="long");
    let h='<table><tr><th>Side</th><th>Entry</th><th>Size (BTC)</th><th>Lev</th><th>Unrealized P&amp;L</th><th>Triggered by</th><th></th></tr>';
    h+='<tr><td class="'+(long?"lng":"sht")+'">'+String(p.side).toUpperCase()+'</td>'
      +'<td>'+fmtUsd(p.entry)+'</td><td>'+fmtNum(p.qty,6)+'</td><td>'+fmtNum(p.leverage,0)+'x</td>'
      +'<td class="'+((p.pnl||0)>=0?"pos":"neg")+'">'+fmtUsd(p.pnl)+'</td>'
      +'<td class="news">'+esc(p.news||"\u2014")+'</td>'
      +'<td><button class="btn close" onclick="closeNow()">Close</button></td></tr></table>';
    pbox.innerHTML=h;
  }

  // journal: every closed trade + the news headline
  const jbox=document.getElementById("journal");
  const hist=s.history||[];
  if(!hist.length){ jbox.innerHTML='<div class="empty">no trades yet</div>'; }
  else{
    let h='<table><tr><th>Time</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Reason</th><th>News</th></tr>';
    for(const t of hist){
      const long=(t.side==="long");
      h+='<tr><td>'+clk(t.closed_at)+'</td>'
        +'<td class="'+(long?"lng":"sht")+'">'+String(t.side).toUpperCase()+'</td>'
        +'<td>'+fmtUsd(t.entry)+'</td><td>'+fmtUsd(t.exit)+'</td>'
        +'<td class="'+((t.pnl||0)>=0?"pos":"neg")+'">'+fmtUsd(t.pnl)+'</td>'
        +'<td>'+esc(t.reason||"")+(t.ok===false?' <span style="color:#ef5350">[close failed]</span>':'')+'</td>'
        +'<td class="news">'+esc(t.news||"\u2014")+'</td></tr>';
    }
    h+='</table>'; jbox.innerHTML=h;
  }

  // activity log
  const lbox=document.getElementById("log");
  const log=s.log||[];
  if(!log.length){ lbox.innerHTML='<div class="empty">nothing yet</div>'; }
  else{
    let h='';
    for(const e of log){ h+='<div class="logrow '+(e.kind||"info")+'"><span class="tt">'+clk(e.t)+'</span>'+esc(e.msg)+'</div>'; }
    lbox.innerHTML=h;
  }
}

function flash(text, ok){ const m=document.getElementById("msg"); m.className="msg "+(ok?"ok":"err"); m.textContent=text; m.dataset.sticky="1"; setTimeout(()=>{ m.dataset.sticky="0"; }, 4000); }

async function toggleBot(){
  const want = !ENABLED;
  if(want && !confirm("Arm the bot? It will place REAL leveraged orders on your Lighter account when news hits.")) return;
  try{
    const r=await fetch("/api/lighterbot/toggle",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:want})});
    const d=await r.json();
    if(d.ok){ flash(d.enabled?"\u2713 bot RUNNING \u2014 armed for real trades on news":"\u2713 bot PAUSED \u2014 no new trades", true); }
    else flash("could not change: "+(d.error||"?"), false);
  }catch(e){ flash("error: "+e, false); }
  load();
}

async function setLev(){
  const v=parseFloat(document.getElementById("lev").value);
  if(!v||v<1){ flash("enter a leverage of 1 or more", false); return; }
  try{
    const r=await fetch("/api/lighterbot/leverage",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({leverage:v})});
    const d=await r.json();
    if(d.ok){ levTouched=false; flash("\u2713 leverage set to "+d.leverage+"x", true); }
    else flash("could not set leverage: "+(d.error||"?"), false);
  }catch(e){ flash("error: "+e, false); }
  load();
}

async function setStrategy(){
  const sel=document.getElementById("strat"); if(!sel) return;
  const v=sel.value;
  try{
    const r=await fetch("/api/lighterbot/strategy",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({strategy:v})});
    const d=await r.json();
    if(d.ok){ flash("\u2713 strategy: "+(d.strategy_label||v)+" \u2014 press RESUME to run it", true); }
    else { flash("could not switch: "+(d.error||"?"), false); load(); }   // revert dropdown to server value
  }catch(e){ flash("error: "+e, false); load(); }
}

async function resetStats(){
  if(!confirm("Reset the bot's stats? Clears win rate, trade count, realized P&L and the log. Your real Lighter balance and any open position are NOT touched.")) return;
  try{ await fetch("/api/lighterbot/reset",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})}); }
  catch(e){ flash("error: "+e, false); return; }
  flash("\u2713 stats reset", true);
  load();
}

async function closeNow(){
  flash("closing \u2026", true);
  try{
    const r=await fetch("/api/lighterbot/close",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({})});
    const d=await r.json();
    if(d.ok) flash("\u2713 close sent", true);
    else flash("close: "+(d.error||"nothing to close"), false);
  }catch(e){ flash("error: "+e, false); }
  load();
}

load();
setInterval(load, 3000);
</script>
</body>
</html>
"""
