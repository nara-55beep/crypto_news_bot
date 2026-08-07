"""
================================================================================
 pipeline.py  —  TRADE DECISION ENGINE  (AI decides everything)
================================================================================
There are NO numeric thresholds here anymore. The AI itself returns TRADE or SKIP
(having weighed importance / priced-in / conviction in its own head, like a real
trader). The bot just executes the AI's verdict — subject only to hard ACCOUNT
SAFETY limits (max trades/hour, daily loss cap, etc.) that protect the $100.

EVERY message is logged to the database (with the time it ARRIVED), so the live
chart can show a yellow dot for every single one — traded or skipped.

For EVERY message you see, step by step:
  [NEWS]      the message + channel + time
  [SENT]      forwarded to the AI (every message; no keyword filter)
  [AI]        the verdict (TRADE/SKIP), direction, coins, conviction, horizon,
              how it read stocks/oil/gold/macro, the chain, the reasoning,
              and any error + raw reply
  [DECISION]  what actually happened (SKIP / BLOCKED by safety / OPENED)
================================================================================
"""

from __future__ import annotations

import time

import config
import ai_analyzer

# AI's text conviction -> fraction of normal risk to put on.
CONVICTION_SIZE = {"low": 0.4, "medium": 0.7, "high": 1.0}

# NEWS-FEED BRAIN: use the Claude "haiku" strategy (the SAME brain as the claudenews bot) so
# the feed prints an actual BTC direction on real catalysts instead of skipping almost
# everything (the generic Gemini analyzer is tuned to skip aggressively). Set False to revert.
USE_CLAUDE_FEED = True


def _claude_to_signal(news, d):
    """Adapt a claude_news.GeminiDecision into the Signal the feed + pipeline expect."""
    sig = ai_analyzer.Signal(raw=news)
    sig.decision = "TRADE" if d.trade else "SKIP"
    # direction only matters on a TRADE — don't leak a lean onto a SKIP row
    sig.direction = (("bullish" if d.direction == "long" else "bearish")
                     if (d.trade and d.direction in ("long", "short")) else "neutral")
    sig.affected_assets = ["BTC"] if (d.trade and d.direction in ("long", "short")) else []
    sig.conviction = (d.confidence or "low")
    sig.horizon = "minutes"
    sig.reasoning = d.reason or ""
    sig.market_read = (f"Claude haiku · stop {d.stop_pct:.2f}% / tp {d.tp_pct:.2f}%"
                       if d.trade else "Claude haiku · stand aside")
    sig.error = d.error or ""
    sig.latency_ms = d.latency_ms
    return sig


async def _claude_feed_decision(news, market):
    """Build the tiny BTC snapshot the haiku brain wants (price + 1m change + taker buy-flow)
    and ask it for a direction. Never raises (claude_news fails safe to a non-trade)."""
    px = chg = funding = None
    try:
        s = market.snapshot().get("BTCUSDT", {})
        px, chg, funding = s.get("price"), s.get("change_1m_pct"), s.get("funding_rate")
    except Exception:
        pass

    def _flow_pct(window):
        try:
            import dashboard
            buy, sell = dashboard.WHALES.flow(window)
            tot = buy + sell
            return round(buy / tot * 100, 1) if tot > 0 else None
        except Exception:
            return None

    snap = {"price": px, "change_1m_pct": chg, "funding": funding,
            "flow_1m": _flow_pct(60), "flow_5m": _flow_pct(300), "flow_15m": _flow_pct(900)}
    import claude_news
    d = await claude_news.decide(news, snap)
    # HAND THE SIGNAL STRAIGHT TO THE REAL LIGHTER BOT — it reacts to the SAME Sonnet decision the
    # feed shows, immediately, with no second AI call. (No-op unless it's on the 'claudenews'
    # strategy.) Fire-and-forget so placing the order never delays the feed's display.
    try:
        import dashboard
        bot = getattr(dashboard, "_LIGHTERBOT", None)
        if bot is not None and hasattr(bot, "on_claude_signal"):
            import asyncio as _aio
            _aio.create_task(bot.on_claude_signal(news, d))
    except Exception:
        pass
    return _claude_to_signal(news, d)


def _pick_symbol(sig):
    for a in sig.affected_assets:
        sym = config.ASSET_TO_SYMBOL.get(a.upper())
        if sym in config.SYMBOLS:
            return sym
    return None


def _ts() -> str:
    return time.strftime("%H:%M:%S")


async def handle_news(source: str, text: str, market, broker, risk, news_rowid=None,
                      arrival_ts=None):
    t_recv = time.perf_counter()
    t_arrival = time.time()          # wall-clock arrival -> where the chart dot goes

    def _record(sig_, traded_, note_):
        # update the row already logged on arrival (no duplicate dot); fall back to
        # a fresh insert only if we weren't given a row id (e.g. the RSS path)
        if news_rowid is not None:
            broker.update_news_decision(news_rowid, sig_, traded_, note_)
        else:
            broker.log_news(source, text, sig_, traded=traded_, note=note_, ts=t_arrival)
        # PUSH the verdict to every open browser over the live SSE stream, keyed by the
        # SAME arrival_ts the headline was pushed with on arrival. The browser finds that
        # row and flips its "…" badge to TRADE/SKIP in place — so the feed updates the
        # instant the AI finishes, with NO 2s polling. (Falls back silently if dashboard
        # isn't importable or no one is connected.)
        try:
            import dashboard
            dashboard.push_news({
                "ts": arrival_ts if arrival_ts is not None else t_arrival,
                "text": (text or "")[:500],
                "decision": getattr(sig_, "decision", None) or "SKIP",
                "direction": getattr(sig_, "direction", "") or "",
                "conviction": getattr(sig_, "conviction", "") or "",
                "coins": ",".join(getattr(sig_, "affected_assets", None) or []),
                "traded": bool(traded_),
                "source": source,
            })
        except Exception:
            pass
    if not text or not text.strip():
        return
    snippet = " ".join(text.strip().split())[:160]

    print("\n" + "-" * 72)
    print(f"[NEWS] {_ts()} ({source})")
    print(f'   "{snippet}"')

    # Optional keyword pre-filter (only if you turn ANALYZE_ALL off again).
    if not config.ANALYZE_ALL:
        low = text.lower()
        if not any(k in low for k in config.KEYWORD_PREFILTER):
            print("[SENT] skipped (ANALYZE_ALL is off and no keyword matched)")
            # still log it so it shows on the chart
            _record(ai_analyzer.Signal(raw=text), False, "prefiltered")
            return
    print("[SENT] forwarded to AI")

    # ---- AI makes the call (Claude haiku strategy for the feed; Gemini if disabled) ----
    if USE_CLAUDE_FEED:
        sig = await _claude_feed_decision(text, market)
    else:
        sig = await ai_analyzer.analyze(text, market.snapshot())

    # ---- show the AI's thinking ----
    print(f"[AI]  decision={sig.decision}  direction={sig.direction}  "
          f"conviction={sig.conviction}  horizon={sig.horizon}  "
          f"coins={sig.affected_assets}  ({sig.latency_ms:.0f}ms)")
    if sig.researched:
        print(f"      researched: {', '.join(sig.search_queries)}")
    if sig.market_read:
        print(f"      markets: {sig.market_read}")
    if sig.causal_chain:
        print(f"      chain:   {sig.causal_chain}")
    if sig.reasoning:
        print(f"      why:     {sig.reasoning}")
    if sig.error:
        print(f"      !! AI ERROR: {sig.error}")
        print(f"      raw reply: {sig.raw_response[:300]}")

    # ---- the AI said SKIP (or gave no tradable direction/coin) ----
    if not sig.wants_trade:
        why = "AI chose SKIP" if sig.decision != "TRADE" else \
              "AI said TRADE but gave no clear direction/coin"
        print(f"[DECISION] SKIP -- {why}")
        _record(sig, False, why)
        return

    symbol = _pick_symbol(sig)
    if symbol is None:
        msg = f"AI wanted {sig.affected_assets} but none are tradable here"
        print(f"[DECISION] SKIP -- {msg}")
        _record(sig, False, msg)
        return

    # ---- ACCOUNT SAFETY limits (NOT market judgment — these protect the $100) ----
    ok, why = risk.can_trade(symbol)
    if not ok:
        print(f"[DECISION] BLOCKED by safety rule -- {why} "
              f"(the AI wanted to trade; account protection said no)")
        _record(sig, False, f"safety: {why}")
        return

    # ---- execute (paper) ----
    side = "long" if sig.direction == "bullish" else "short"
    if market.price(symbol) is None:
        print(f"[DECISION] SKIP -- no live price for {symbol} yet")
        _record(sig, False, "no price yet")
        return
    size_frac = CONVICTION_SIZE.get(sig.conviction, 0.5)
    margin = risk.position_margin(size_frac)
    pos = broker.open_position(symbol, side, margin, config.MAX_LEVERAGE,
                               causal_chain=sig.causal_chain)
    if pos:
        risk.register_trade()
        _record(sig, True, f"opened {pos.id}")
        total = (time.perf_counter() - t_recv) * 1000
        print(f"[DECISION] >>> OPENED {side.upper()} {symbol} "
              f"(conviction={sig.conviction})  |  news->order {total:.0f}ms "
              f"(AI was {sig.latency_ms:.0f}ms)")
