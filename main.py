"""
================================================================================
 main.py  —  ORCHESTRATOR  (run this:  python main.py)
================================================================================
Boots all layers and wires them together:

  market_data  (live prices/funding/OI)  ─┐
  telegram     (fast news)  ──> on_news ──┤
  rss          (macro news) ──> on_news ──┴─> pipeline.handle_news
                                                 -> AI -> risk -> paper_broker
  + a 1s loop that marks open positions and triggers SL/TP/time-stops

PAPER mode only. LIVE mode is intentionally NOT wired (raises) so you can't flip
a flag and accidentally trade real money — going live requires writing a real
broker on purpose. Read the README first.
================================================================================
"""

from __future__ import annotations

import asyncio
import time
import sys

# ---------------------------------------------------------------------------
# UTF-8 console — MUST run before the first print(). On Windows the console
# defaults to cp1252, so printing a non-ASCII headline (Chinese text, the "→"
# arrow in the news+whale logs, emoji, etc.) raised UnicodeEncodeError and
# killed that news task. Forcing stdout/stderr to UTF-8 with errors="replace"
# makes every print safe regardless of the news content.
# ---------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Windows DNS fix — MUST run before any network connection is opened.
# Installing ccxt also installs `aiodns`. Once aiodns is present, aiohttp
# switches its DNS resolver to aiodns (c-ares). On many Windows machines c-ares
# can't find the system DNS servers, so EVERY async HTTPS request dies with
# "Could not contact DNS servers" — which silently kills BOTH the price feed
# and all the exchange data (while a plain sync test still works, which is why
# diagnose.bat passes but the live bot doesn't). We force aiohttp back to the
# normal OS resolver (ThreadedResolver), exactly how it behaved before ccxt.
# ---------------------------------------------------------------------------
try:
    import aiohttp.resolver as _ares
    import aiohttp.connector as _aconn
    _ares.DefaultResolver = _ares.ThreadedResolver
    _aconn.DefaultResolver = _ares.ThreadedResolver
    print("[net] DNS via OS resolver (aiohttp ThreadedResolver) — Windows aiodns workaround active")
except Exception as _e:
    print(f"[net] could not set DNS resolver ({type(_e).__name__}); continuing anyway")

import config
import dedup
from market_data import MarketData
from paper_broker import PaperBroker
from risk_manager import RiskManager
import pipeline
import telegram_listener
import rss_listener
import dashboard
import news_whale_bot
import stock_data
import stock_bot
import lighter_live
import lighter_news_bot
import ict_bot
import ict_sm_paper
import freq_bot
import trend_sweep_paper
import ob_strategy_paper
import cot_fade_paper
import onchain_radar_paper
import polymarket_copy_paper
import apex_vwap_paper
import lucid_pass_paper
import lucid_pass_audited_paper
import lucid_continuous_paper
from lucid_lab.paper import LucidLabPaperBot, manage_loop as manage_lucid_lab_paper
import lucid_lab.web as lucid_lab_web
import strategy_lab.web as strategy_lab_web
import nq_mr_15m_paper
import nr7_paper
import nr7_aggr_paper


async def status_loop(broker: PaperBroker):
    """Mark-to-market every second; print a heartbeat each minute."""
    last_print = 0
    while True:
        broker.mark_to_market()
        now = time.time()
        if now - last_print >= 60:
            last_print = now
            print(f"[status] equity=${broker.equity():.2f} "
                  f"balance=${broker.balance:.2f} "
                  f"open={len(broker.positions)} "
                  f"losses_streak={broker.consecutive_losses}")
        await asyncio.sleep(1)


async def main():
    if config.MODE.upper() == "LIVE":
        raise SystemExit(
            "MODE=LIVE but no live broker is wired. This is on purpose. Prove the "
            "system in PAPER first, then write a real Binance execution module "
            "(see README) before ever setting LIVE.")

    print("=" * 70)
    print(f" CRYPTO NEWS BOT — PAPER MODE — starting balance ${config.STARTING_BALANCE_USDT}")
    print("=" * 70)

    market = MarketData()
    broker = PaperBroker(market)
    risk = RiskManager(broker)
    nwbot = news_whale_bot.NewsWhaleBot(market, dashboard.WHALES)
    ictbot = ict_bot.ICTBot(market)              # ICT 2022-model PAPER bot (sweep->MSS->FVG)
    ictsmbot = ict_sm_paper.ICTSMTradesBot(market)  # ICT SM Trades (TradingView ZtoavozZ) PAPER bot, BTC
    ictfreqbot = ict_bot.ICTFrequentBot(market)  # More active ICT copy with softer gates
    freqbot = freq_bot.FreqBot(market)           # Freqtrade-style PAPER bot (RSI/BB/ROI)
    freqtpbot = freq_bot.ImprovedTPSLFreqBot(market)  # Same entries, improved margin-based TP/SL
    freqtrendbot = freq_bot.TrendFollowTPSLFreqBot(market)  # Same entries, tighter trend-follow TP/SL
    freq5bot = freq_bot.FivePctTPSLFreqBot(market)  # Same entries, fixed 5% margin trailing gap
    freqtfbot = freq_bot.TrendFlowConfirmedFreqBot(market, dashboard.WHALES)  # Trend + flow confirmed
    tsbot = trend_sweep_paper.TrendSweepPaperBot(market)  # Trend-Sweep VWAP PAPER bot (Binance strat)
    apexvwapbot = apex_vwap_paper.ApexVWAPPaperBot()  # ES 5m VWAP + Opening Range Apex-style PAPER bot
    lucidcontbot = lucid_continuous_paper.LucidContinuousPaperBot()  # Lucid basket PAPER bot, keeps trading after pass
    # Only the pass-stop panel uses audited execution. Continuous deliberately
    # remains on the original class and its independent P&L/state population.
    lucidpassbot = lucid_pass_audited_paper.AuditedLucidPassPaperBot()
    lucidlabpaperbot = LucidLabPaperBot()  # selected three-sleeve LucidPro 25K PAPER portfolio
    lucid_lab_web.set_paper_bot(lucidlabpaperbot)
    lucid_alert_token = getattr(config, "TELEGRAM_ALERT_BOT_TOKEN", "")
    lucid_alert_chat_id = getattr(config, "TELEGRAM_ALERT_CHAT_ID", "")
    lucidcontbot.set_telegram_bot(lucid_alert_token, lucid_alert_chat_id)
    lucidpassbot.set_telegram_bot(lucid_alert_token, lucid_alert_chat_id)
    nqmr15bot = nq_mr_15m_paper.NQMR15PaperBot()  # BTC-only 1m port, $100 at 20x paper
    nr7bot = nr7_paper.NR7PaperBot()  # NR7 breakout Apex PAPER bot (ES+NQ+CL, lock-the-trail sprint) - PINNED TOP
    nr7aggrbot = nr7_aggr_paper.NR7AggressivePaperBot()  # NR7 + NQ reversion (aggressive) - PINNED below NR7
    # OB / Smart-Money PAPER bots (Reddit Freqtrade port) on three timeframes
    ob_bots = {tf: ob_strategy_paper.OBStrategyBot(market, tf) for tf in ("1m", "5m", "15m")}
    cotbot = cot_fade_paper.COTFadePaperBot(market)  # COT crowded-positioning fade PAPER bot
    onchainbot = onchain_radar_paper.OnChainRadarPaperBot(market)  # chain-wide on-chain flow PAPER bot
    polybot = polymarket_copy_paper.PolymarketCopyBot()            # Polymarket copy-trade PAPER bot (@huskyvs)
    polybot.start()                                                # self-contained daemon thread (polls Polymarket data-api)
    stock_market = stock_data.StockData()
    stock_news_bot = stock_bot.StockNewsBot(stock_market)

    # the single entry point every news source calls
    async def on_news(source: str, text: str):
        # CROSS-SOURCE DE-DUPE (first thing): the same story often arrives from more
        # than one channel/feed. The listeners only de-dupe within a single source,
        # so we catch IDENTICAL repeats from DIFFERENT sources here — once, at the one
        # place every source funnels through. Dropping it now means no duplicate dot on
        # the chart, no duplicate website post, and no duplicate AI/trade reaction.
        # JUNK FILTER: drop promo/spam messages (e.g. "follow us on X") before
        # anything sees them — not shown, not logged, no bot reacts.
        _low = (text or "").lower()
        _src = (source or "").lower()
        if _src in {s.lower() for s in getattr(config, "NEWS_IGNORE_SOURCES", [])}:
            print(f"[ignore] dropped source {source}: "
                  f"{(text or '')[:60].replace(chr(10), ' ')!r}")
            return
        if any(sub in _low for sub in getattr(config, "NEWS_IGNORE_SUBSTRINGS", [])):
            print(f"[ignore] dropped junk from {source}: "
                  f"{(text or '')[:60].replace(chr(10), ' ')!r}")
            return
        if text and text.strip() and dedup.is_duplicate(text):
            print(f"[dedup] skipped duplicate from {source}: "
                  f"{text[:60].replace(chr(10), ' ')!r}")
            return
        # REAL-MONEY BOT FIRST: fire the Lighter bot's watch BEFORE the SQLite write, the
        # browser push and the paper bots — so the money-on-the-line bot reacts at the
        # earliest possible instant (it just starts a fast non-blocking watch task).
        await lighterbot.on_news(source, text)
        # FASTEST PATH NEXT: log the headline and push it to the browser the instant it
        # arrives — before the AI — so it hits the chart/feed with ~0 delay.
        news_rowid = None
        arrival_ts = None
        if text and text.strip():
            arrival_ts = time.time()
            try:
                news_rowid = broker.log_news_fast(source, text, arrival_ts)
            except Exception:
                news_rowid = None
            try:
                dashboard.push_news({"ts": arrival_ts, "text": text[:500], "decision": "…",
                                     "direction": "", "conviction": "", "coins": "",
                                     "traded": False, "source": source})
            except Exception:
                pass
        await nwbot.on_news(source, text)            # fast: just starts a watch task
        await dashboard.NEWSAI.on_news(source, text) # AI news bot: analyse + paper-trade (non-blocking)
        try:
            dashboard.SNIPER.on_news(source, text)   # no-AI sniper: instant rules-based reaction
        except Exception:
            pass
        try:
            dashboard.NEWSMOMO.on_news(source, text) # news-momentum: 1.5s pre-news >=0.08% move -> bet direction
        except Exception:
            pass
        try:
            dashboard.NEWSPAPER.on_news(source, text)# News Paper Bot: rule-based classify -> paper trade (BTC/ETH/SOL)
        except Exception:
            pass
        try:
            dashboard.CLAUDEHAIKU.on_news(source, text) # Claude Haiku: live-feed AI paper trade (BTC, $100/20x)
        except Exception:
            pass
        await stock_news_bot.on_news(source, text)   # fast: starts its own task
        # Run the (slow) crypto AI analysis in the BACKGROUND. Awaiting it here meant a
        # headline could not be shown until the PREVIOUS headline's AI finished — when
        # that involved a web search it could block ~10-30s, which is the news lag you
        # saw. push_news already fired above, so the headline now appears instantly and
        # the AI verdict streams into its row a moment later (keyed by arrival_ts, so the
        # browser updates the existing "…" row in place — no polling needed).
        asyncio.create_task(
            pipeline.handle_news(source, text, market, broker, risk,
                                 news_rowid=news_rowid, arrival_ts=arrival_ts))

    # bring up market data first so the snapshot/prices are populated
    md_task = asyncio.create_task(market.run())
    print("[main] connecting to Binance market data...")
    if not await market.wait_until_ready(timeout=20):
        print("[main] WARNING: not all prices ready; continuing anyway")
    print("[main] market data live:", {s: market.price(s) for s in config.SYMBOLS})

    tasks = [md_task, asyncio.create_task(status_loop(broker)),
             asyncio.create_task(nwbot.manage_loop()),
             asyncio.create_task(ictbot.manage_loop()),
             asyncio.create_task(ictsmbot.manage_loop()),
             asyncio.create_task(ictfreqbot.manage_loop()),
             asyncio.create_task(freqbot.manage_loop()),
             asyncio.create_task(freqtpbot.manage_loop()),
             asyncio.create_task(freqtrendbot.manage_loop()),
             asyncio.create_task(freq5bot.manage_loop()),
             asyncio.create_task(freqtfbot.manage_loop()),
             asyncio.create_task(tsbot.manage_loop()),
             asyncio.create_task(apexvwapbot.manage_loop()),
             asyncio.create_task(lucid_pass_paper.manage_shared_loop([lucidcontbot, lucidpassbot])),
             asyncio.create_task(manage_lucid_lab_paper(lucidlabpaperbot)),
             asyncio.create_task(strategy_lab_web.desk_loop()),
             asyncio.create_task(nqmr15bot.manage_loop()),
             asyncio.create_task(nr7bot.manage_loop()),
             asyncio.create_task(nr7aggrbot.manage_loop()),
             *[asyncio.create_task(b.manage_loop()) for b in ob_bots.values()],
             asyncio.create_task(cotbot.manage_loop()),
             asyncio.create_task(onchainbot.manage_loop()),
             asyncio.create_task(stock_market.run()),
             asyncio.create_task(stock_news_bot.manage_loop())]

    # Strategy Research Bot (separate FastAPI app) — auto-start it in a background
    # thread so it's live in the /paper page WITHOUT you starting a second bot.
    import research_server
    research_server.start_research_server()

    # Web dashboard (live BTC chart + news dots) at http://127.0.0.1:8000
    lighter_obj = lighter_live.LighterLive()   # real-money Lighter page (lazy; safe if unconfigured)
    lighter_lock = asyncio.Lock()              # ONE lock for all Lighter signed calls (manual + bot)
    lighterbot = lighter_news_bot.LighterNewsBot(market, dashboard.WHALES, lighter_obj,
                                                 call_lock=lighter_lock)
    lighterbot._paper_bot = nwbot   # so the "Whale Copy 100k (mirror paper)" strategy can shadow it
    lighterbot._freq_bot = freqbot  # so the "Freqtrade-style LIVE (mirror paper)" strategy can shadow it
    lighterbot._freq_improved_bot = freqtpbot  # mirrors "Freqtrade-style (paper improved TP/SL)"
    lighterbot._freq_trend_bot = freqtrendbot  # mirrors "Freqtrade-style (paper trend TP/SL)"
    lighterbot._freq_5_bot = freq5bot  # "Freqtrade-style (improved TP/SL 5%)" live slot mirrors the paper 5% bot EXACTLY
    lighterbot._tvstrats_bot = dashboard.TVSTRATS  # mirrors "Bollinger + RSI Double (ChartArt)" from the paper TV pack
    lighterbot._rsi2atr_bot = dashboard.RSI2ATR  # mirrors "RSI2 EMA50 Scalper (paper - ATR filter)"
    tasks.append(asyncio.create_task(lighterbot.manage_loop()))   # REAL-MONEY bot manage/exit loop
    try:
        await dashboard.start_dashboard(market, broker, nwbot, stock_market, stock_news_bot,
                                        lighter=lighter_obj, lighter_bot=lighterbot,
                                        lighter_lock=lighter_lock, ict_bot=ictbot, ict_freq_bot=ictfreqbot,
                                        freq_bot=freqbot, freq_tp_bot=freqtpbot,
                                        freq_trend_bot=freqtrendbot, freq_5_bot=freq5bot,
                                        freq_tf_bot=freqtfbot, trend_sweep_bot=tsbot,
                                        apex_vwap_bot=apexvwapbot, lucid_cont_bot=lucidcontbot,
                                        lucid_pass_bot=lucidpassbot,
                                        ob_bots=ob_bots, cot_bot=cotbot,
                                        onchain_bot=onchainbot, poly_bot=polybot,
                                        ict_sm_bot=ictsmbot, nq_mr_15m_bot=nqmr15bot, nr7_bot=nr7bot,
                                        nr7_aggr_bot=nr7aggrbot)
    except OSError as e:
        # Errno 10048 (Windows) / 98 (Linux) = port 8000 already in use.
        if getattr(e, "errno", None) in (48, 98, 10048) or "in use" in str(e).lower() \
                or "10048" in str(e):
            print("\n" + "=" * 70)
            print(" ANOTHER COPY OF THE BOT IS ALREADY RUNNING (port 8000 is taken).")
            print(" Two copies at once will flood Binance and get your IP rate-limited.")
            print(" -> Close every other bot window (or end python.exe in Task Manager),")
            print("    then run this again. Stopping this duplicate now.")
            print("=" * 70 + "\n")
            return
        print(f"[dashboard] could not start: {e}")

    # Telegram (skip gracefully if creds not filled in)
    if config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH != "PASTE_HASH":
        tg = telegram_listener.build_client()
        lucid_target = getattr(config, "LUCID_SIGNAL_TELEGRAM_TARGET", "me")
        lucidcontbot.set_telegram_client(tg, lucid_target)
        lucidpassbot.set_telegram_client(tg, lucid_target)
        # self-healing: reconnects forever so the feed can't silently die
        tasks.append(asyncio.create_task(telegram_listener.run_listener_forever(tg, on_news)))
    else:
        print("[main] Telegram creds not set in config.py -> Telegram disabled")

    # RSS: built-in fast feeds (FinancialJuice, in rss_listener.py) + any
    # optional config.RSS_FEEDS. Starts if either has entries.
    if config.RSS_FEEDS or getattr(rss_listener, "FAST_FEEDS", None):
        tasks.append(asyncio.create_task(rss_listener.start_rss(on_news)))

    print("[main] running. Ctrl-C to stop.\n")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[main] stopped by user.")
