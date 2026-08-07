"""
================================================================================
 ainews_paper.py  —  "AI NEWS TRADING BOT"  (paper)
================================================================================
A faithful paper re-implementation of the minimal AI-news bot described in
profitview.net/blog/what-i-learned-when-building-an-ai-news-trading-bot:

  * Pull recent headlines from Google News RSS (hourly query) every ~60s.
  * Send the batch of headlines to an LLM ("you are a cryptocurrency trading
    expert") and ask for a SINGLE sentiment float in [-1.0 (strong sell), +1.0
    (strong buy)].
  * Map that score straight to a position: direction = sign(score), size scales
    with |score|. Re-evaluate every minute; flip when sentiment flips.
  * Percentage stop-loss + take-profit (the author notes the bot is "too slow"
    on big macro surprises, so a stop is mandatory).

Differences from the blog: it trades BTC on PAPER (the blog used BitMEX real
orders), starts with $100, marks P&L on the live BTC price, and falls back to a
crypto keyword lexicon if the AI call fails so it never crashes. No real money.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import config

# This bot scores sentiment via GROQ directly (its own key/endpoint), so it does NOT share
# the rate-limited AI_PROVIDER quota the other AI bots use. Groq is OpenAI-compatible.

STATE_PATH = os.path.join(config.DATA_DIR, "ainews_state.json")

COIN       = "Bitcoin"
PX_SYMBOL  = "BTCUSDT"
RSS_URL    = f"https://news.google.com/rss/search?q={COIN}&tbs=qdr:h"   # last-hour news
GROQ_MODEL = "llama-3.1-8b-instant"   # this bot's own Groq model: fast + high rate limits (NOT the
                                      # shared config.GROQ_MODEL, which is a slow/limited reasoning model)
FAST_SEC   = 2                # price/position loop — near-instant reaction (no API calls)
NEWS_SEC   = 45               # news+AI refresh loop (LLM only fires when headlines CHANGE)
AI_COOLDOWN = 180             # after a rate-limit, pause AI calls this long (use keyword meanwhile)
START_BAL  = 100.0            # paper account (USDT)
FEE        = 0.0              # ZERO — Lighter (the real venue) has no trading fees
ENTER      = 0.30             # |sentiment| must exceed this to take/hold a position
LEVERAGE   = 20.0            # ALL-IN: notional = whole equity * 20x on every trade (very aggressive)
STOP_PCT   = 0.02             # 2% price stop-loss
TP_PCT     = 0.04             # 4% take-profit (2:1)
MAX_HEADLINES = 12

# crypto sentiment lexicon — only used if the AI call is unavailable/fails
_BULL = ("surge", "rally", "adopt", "approval", "approve", "etf", "partnership", "bullish",
         "gains", "soar", "record", "institutional", "buy", "inflow", "upgrade", "breakout", "halving")
_BEAR = ("hack", "ban", "crash", "lawsuit", "selloff", "plunge", "bearish", "dump", "fraud",
         "charge", "liquidation", "outflow", "exploit", "delay", "reject", "fear", "scam")


class AINewsBot:
    def __init__(self, market=None):
        self.market = market
        self.enabled = True
        self.balance = START_BAL
        self.pos: dict | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.score = 0.0
        self.engine = "AI (Groq)" if getattr(config, "GROQ_API_KEY", "") else "keyword(fallback)"
        self._gx = None                 # lazy Groq (OpenAI-compatible) client
        self.headlines: list[dict] = []
        self.price = 0.0
        self.status = "starting…"
        self._news_hash = ""            # last headline-set seen (skip the LLM when unchanged)
        self._ai_cooldown_until = 0.0   # back off AI calls after a rate-limit
        self._last_ai_log = 0.0
        self._load()

    def attach(self, market):
        self.market = market

    # ---- persistence ------------------------------------------------------
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"enabled": self.enabled, "balance": self.balance, "pos": self.pos,
                           "history": self.history, "log": self.log[:60]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.balance = float(d.get("balance", self.balance))
            self.pos = d.get("pos") or None
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            print(f"[ainews] restored: bal ${self.balance:.2f}, {len(self.history)} trades, "
                  f"{'1 open' if self.pos else 'flat'}")
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[ainews] {msg}")

    # ---- news + sentiment -------------------------------------------------
    def _fetch_headlines(self) -> list[dict]:
        """Google News RSS -> recent headlines (newest first). Blocking; run in executor."""
        import feedparser
        feed = feedparser.parse(RSS_URL)
        out = []
        now = time.time()
        for e in feed.entries:
            title = getattr(e, "title", "").strip()
            if not title:
                continue
            ts = None
            if getattr(e, "published_parsed", None):
                ts = time.mktime(e.published_parsed)
            age_min = (now - ts) / 60.0 if ts else 999
            out.append({"title": title, "age_min": round(age_min, 1)})
        out.sort(key=lambda x: x["age_min"])
        return out[:MAX_HEADLINES]

    def _groq(self):
        """Lazy GROQ client (OpenAI-compatible). Isolated from the shared AI_PROVIDER quota."""
        if self._gx is None:
            from openai import AsyncOpenAI
            self._gx = AsyncOpenAI(
                api_key=config.GROQ_API_KEY,
                base_url=getattr(config, "GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                timeout=getattr(config, "GROQ_TIMEOUT_SEC", 8.0),
            )
        return self._gx

    async def _groq_complete(self, system: str, user: str) -> str:
        r = await self._groq().chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.2, max_tokens=60,
        )
        return r.choices[0].message.content

    async def _ai_score(self, headlines: list[dict]) -> float | None:
        if not headlines or not getattr(config, "GROQ_API_KEY", ""):
            return None
        lines = "\n".join(f"- ({h['age_min']:.0f} min ago) {h['title']}" for h in headlines)
        system = "You are a cryptocurrency trading expert."
        user = (
            f"Recent {COIN} news headlines (newest first):\n{lines}\n\n"
            "Assess the overall SHORT-TERM impact on Bitcoin's price, weighting more recent "
            "headlines higher. Respond ONLY as JSON: {\"score\": x} where x is a float from "
            "-1.0 (strong sell) to 1.0 (strong buy); 0 means neutral/no edge."
        )
        try:
            raw = await self._groq_complete(system, user)
        except Exception as e:
            name = type(e).__name__
            if "RateLimit" in name or "429" in str(e):
                self._ai_cooldown_until = time.time() + AI_COOLDOWN     # back off AI for a while
                if time.time() - self._last_ai_log > 60:               # don't spam the log
                    self._note(f"AI rate-limited — pausing AI {AI_COOLDOWN // 60}m, "
                               f"scoring on keywords meanwhile", "skip")
                    self._last_ai_log = time.time()
            else:
                self._note(f"AI call failed ({name}) — keyword fallback", "skip")
            return None
        return self._parse_score(raw)

    @staticmethod
    def _parse_score(raw: str) -> float | None:
        if not raw:
            return None
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and "score" in d:
                return max(-1.0, min(1.0, float(d["score"])))
        except Exception:
            pass
        m = re.search(r"-?\d*\.?\d+", raw)        # fall back to first number in the text
        if m:
            try:
                return max(-1.0, min(1.0, float(m.group())))
            except Exception:
                return None
        return None

    def _keyword_score(self, headlines: list[dict]) -> float:
        text = " ".join(h["title"].lower() for h in headlines)
        bull = sum(text.count(w) for w in _BULL)
        bear = sum(text.count(w) for w in _BEAR)
        if bull + bear == 0:
            return 0.0
        return max(-1.0, min(1.0, (bull - bear) / (bull + bear + 3)))

    # ---- the loops --------------------------------------------------------
    async def manage_loop(self):
        # Two concurrent loops: a FAST price/position loop (instant reaction, no API),
        # and a THROTTLED news+AI loop (LLM only when headlines change → no rate-limit spam).
        await asyncio.gather(self._price_loop(), self._news_loop())

    async def _price_loop(self):
        """Near-instant: re-checks price + manages the position every FAST_SEC. No API calls."""
        while True:
            try:
                price = self._price()
                if price:
                    if self.pos is not None:
                        self._manage(price, self.score)          # stop / take-profit / sentiment flip
                    elif self.enabled and abs(self.score) >= ENTER:
                        self._open(price, self.score)
                    self._set_status(self.score)
            except Exception as e:
                self._note(f"price loop error: {type(e).__name__}: {str(e)[:80]}", "error")
            await asyncio.sleep(FAST_SEC)

    async def _news_loop(self):
        """Throttled: refresh headlines every NEWS_SEC; call the LLM ONLY when the headline
        set actually changed (and not during a rate-limit cooldown)."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                heads = await loop.run_in_executor(None, self._fetch_headlines)
                self.headlines = heads
                h = str(hash(tuple(x["title"] for x in heads)))
                if h != self._news_hash:                          # headlines changed -> re-score
                    if time.time() >= self._ai_cooldown_until:
                        score = await self._ai_score(heads)       # LLM (sets cooldown on 429)
                    else:
                        score = None                              # in cooldown -> keyword this round
                    if score is None:
                        self.score = round(self._keyword_score(heads), 3)
                        self.engine = "keyword(fallback)"
                    else:
                        self.score = round(score, 3)
                        self.engine = "AI (Groq)"
                    self._news_hash = h
                    self._save()
            except Exception as e:
                self._note(f"news loop error: {type(e).__name__}: {str(e)[:80]}", "error")
            await asyncio.sleep(NEWS_SEC)

    def _price(self) -> float:
        try:
            p = self.market.price(PX_SYMBOL) if self.market else None
            if p:
                self.price = float(p)
        except Exception:
            pass
        return self.price

    def _set_status(self, score):
        eq = self._equity()
        bias = "BUY" if score > 0 else ("SELL" if score < 0 else "flat")
        self.status = (f"{self.engine} · sentiment {score:+.2f} ({bias}) · "
                       f"{'in position' if self.pos else 'flat'} · equity ${eq:,.2f} · "
                       f"{'armed' if self.enabled else 'paused'}")

    # ---- trade management -------------------------------------------------
    def _open(self, price, score):
        side = "long" if score > 0 else "short"
        eq = self._equity()
        if eq <= 0:
            return
        notional = eq * LEVERAGE                          # ALL-IN: whole account at 20x
        qty = notional / price
        self.balance -= notional * FEE                    # fee is charged on the full (leveraged) notional
        if side == "long":
            stop, tp, liq = price * (1 - STOP_PCT), price * (1 + TP_PCT), price * (1 - 1 / LEVERAGE)
        else:
            stop, tp, liq = price * (1 + STOP_PCT), price * (1 - TP_PCT), price * (1 + 1 / LEVERAGE)
        self.pos = {"id": uuid.uuid4().hex[:6], "side": side, "entry": price, "qty": qty,
                    "notional": round(notional, 2), "stop": stop, "tp": tp, "liq": liq,
                    "lev": LEVERAGE, "margin": round(eq, 2),
                    "entry_score": round(score, 3), "opened_at": time.time()}
        self._note(f"OPEN {side.upper()} BTC @ ${price:,.1f} · sentiment {score:+.2f} · "
                   f"${notional:,.0f} notional ({LEVERAGE:g}x on ${eq:,.2f}) · "
                   f"stop ${stop:,.1f} / tp ${tp:,.1f}", "open")
        self._save()

    def _manage(self, price, score):
        p = self.pos
        long = p["side"] == "long"
        reason = None
        if long and price <= p.get("liq", 0):                 # 20x liquidation (~-5%)
            reason = "liquidation"
        elif (not long) and price >= p.get("liq", 1e18):
            reason = "liquidation"
        elif long and price <= p["stop"]:
            reason = "stop"
        elif long and price >= p["tp"]:
            reason = "take_profit"
        elif (not long) and price >= p["stop"]:
            reason = "stop"
        elif (not long) and price <= p["tp"]:
            reason = "take_profit"
        elif long and score <= -ENTER:
            reason = "sentiment_flip"
        elif (not long) and score >= ENTER:
            reason = "sentiment_flip"
        if reason:
            self._close(price, reason)

    def _close(self, price, reason):
        p = self.pos
        long = p["side"] == "long"
        pnl = p["qty"] * ((price - p["entry"]) if long else (p["entry"] - price)) - p["notional"] * FEE
        pnl = max(pnl, -p.get("margin", p["notional"] / LEVERAGE))   # isolated 20x: can't lose more than margin
        self.balance += pnl
        if self.balance < 0:
            self.balance = 0.0
        self.history.insert(0, {"side": p["side"], "entry": round(p["entry"], 1), "exit": round(price, 1),
                                "pnl": round(pnl, 2), "reason": reason, "entry_score": p["entry_score"],
                                "opened_at": p["opened_at"], "closed_at": time.time()})
        self.history = self.history
        self.pos = None
        self._note(f"CLOSE {p['side'].upper()} @ ${price:,.1f} · {reason} · P&L ${pnl:+.2f}",
                   "win" if pnl >= 0 else "loss")
        self._save()

    # ---- helpers + controls ----------------------------------------------
    def _equity(self):
        eq = self.balance
        if self.pos and self.price:
            long = self.pos["side"] == "long"
            eq += self.pos["qty"] * ((self.price - self.pos["entry"]) if long else (self.pos["entry"] - self.price))
        return eq

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.enabled = True
        self.balance = START_BAL
        self.pos = None
        self.history = []
        self.log = []
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        eq = self._equity()
        pos = None
        if self.pos:
            p = self.pos
            long = p["side"] == "long"
            upnl = p["qty"] * ((self.price - p["entry"]) if long else (p["entry"] - self.price)) if self.price else 0.0
            pos = {"side": p["side"], "entry": round(p["entry"], 1), "notional": p["notional"],
                   "stop": round(p["stop"], 1), "tp": round(p["tp"], 1), "mark": round(self.price, 1),
                   "upnl": round(upnl, 2), "entry_score": p["entry_score"],
                   "lev": p.get("lev", 1), "liq": round(p.get("liq", 0), 1)}
        wins = sum(1 for h in self.history if h["pnl"] > 0)
        return {
            "enabled": self.enabled, "engine": self.engine, "status": self.status,
            "score": self.score, "price": round(self.price, 1) if self.price else None,
            "coin": COIN, "enter": ENTER, "stop_pct": STOP_PCT, "tp_pct": TP_PCT,
            "headlines": self.headlines[:MAX_HEADLINES],
            "balance": round(self.balance, 2), "equity": round(eq, 2),
            "start_balance": START_BAL, "total_pnl": round(eq - START_BAL, 2),
            "total_pnl_pct": round((eq / START_BAL - 1) * 100, 2),
            "position": pos, "trades": len(self.history), "wins": wins,
            "history": self.history, "log": self.log[:25],
        }
