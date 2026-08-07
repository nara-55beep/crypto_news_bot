"""
claude_haiku_paper.py - Claude Haiku live-news paper trader

Paper-only bot:
  - watches the site's existing live news feed via on_news()
  - asks Claude Haiku whether the headline is an immediate BTC catalyst
  - if yes, opens a simulated BTC position with $100 account, 20x leverage
  - uses the AI's own per-headline stop-loss, take-profit, and max-hold
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque

import config

STATE_PATH = os.path.join(config.DATA_DIR, "claude_haiku_paper_state.json")

SYMBOL = "BTCUSDT"
START_BAL = 100.0
LEVERAGE = 20.0
FEE = 0.0
POLL_SEC = 1.0
MAX_INFLIGHT = 4
AI_TIMEOUT = 18.0

# AI output safety rails. Values are BTC price-move percentages, not margin PnL.
STOP_MIN, STOP_MAX = 0.05, 3.0
TP_MIN, TP_MAX = 0.05, 8.0
HOLD_MIN, HOLD_MAX = 1.0, 180.0

MODEL = getattr(config, "CLAUDE_HAIKU_PAPER_MODEL", "claude-haiku-4-5")

SYSTEM_PROMPT = """You are Claude Haiku acting as a fast professional crypto news trader.

You trade a BTC perpetual PAPER account. Your job is to decide whether ONE live headline is an immediate BTC market catalyst.

Rules:
- Trade only news that can move BTC now, within seconds to minutes.
- Skip delayed/conditional/future news: "next month", "if approved", "plans to", "may", "could", "proposal", "rumor", scheduled events, speeches later, filings that matter only after future approval.
- Skip ordinary politics, stock/company news, opinions, generic market commentary, repeated/old news, vague/truncated headlines, and anything without a direct BTC/crypto or global risk channel.
- Trade real immediate catalysts: surprise Fed/CPI/PCE/jobs/rates, ETF approval/rejection/large flows, major exchange hack/halt/insolvency, stablecoin depeg, legal/regulatory shock, major geopolitical escalation/de-escalation, huge verified BTC buy/sell.
- If unsure, SKIP.

If you trade, decide the full plan yourself:
- direction: long if bullish/risk-on for BTC, short if bearish/risk-off.
- stop_loss_pct: BTC price move against entry before cutting. Typical 0.15-0.80, wider only for huge news.
- take_profit_pct: BTC price move in favor. Reward should usually be >= risk.
- max_hold_minutes: how long this headline edge should last before closing if SL/TP did not hit. Immediate news trades usually decay in 3-30 minutes.
- confidence: low, medium, or high.

The bot runs 20x leverage, so a 5% BTC move against entry is liquidation. Keep stops below liquidation.

Reply ONLY compact JSON:
{"trade":true|false,"direction":"long"|"short","stop_loss_pct":number,"take_profit_pct":number,"max_hold_minutes":number,"confidence":"low"|"medium"|"high","reason":"max 10 words"}"""


def _clamp(x, lo, hi, default):
    try:
        v = float(x)
    except Exception:
        return default
    if v != v:
        return default
    return max(lo, min(hi, v))


def _safe_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t[:4].lower() == "json":
            t = t[4:].strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return {}
    return {}


class ClaudeHaikuPaperBot:
    def __init__(self, market=None, whales=None):
        self.market = market
        self.whales = whales
        self.enabled = True
        self.balance = START_BAL
        self.pos: dict | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.decisions: list[dict] = []
        self.recent_news: list[dict] = []
        self.price = 0.0
        self.status = "starting"
        self._hist = deque(maxlen=900)          # (ts, price)
        self._seen = deque(maxlen=1000)
        self._seen_set: set[str] = set()
        self._sem: asyncio.Semaphore | None = None
        self._client = None
        self._cooldown_until = 0.0
        self._load()

    def attach(self, market, whales=None):
        self.market = market
        if whales is not None:
            self.whales = whales

    # ---- persistence --------------------------------------------------
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "enabled": self.enabled,
                    "balance": self.balance,
                    "pos": self.pos,
                    "history": self.history[:200],
                    "log": self.log[:80],
                    "decisions": self.decisions[:200],
                    "recent_news": self.recent_news[:80],
                }, f)
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
            self.decisions = d.get("decisions", []) or []
            self.recent_news = d.get("recent_news", []) or []
            print(f"[claude-haiku-paper] restored: ${self.balance:.2f}, "
                  f"{len(self.history)} trades, {'1 open' if self.pos else 'flat'}")
        except Exception as e:
            print(f"[claude-haiku-paper] load error: {e}")

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:80]
        print(f"[claude-haiku-paper] {msg}")

    # ---- market snapshot ----------------------------------------------
    def _px(self):
        try:
            p = self.market.price(SYMBOL) if self.market else None
            if p:
                self.price = float(p)
        except Exception:
            pass
        return self.price

    def _change_pct(self, secs):
        if not self._hist:
            return None
        now = time.time()
        if now - self._hist[0][0] < secs * 0.5:
            return None
        target = now - secs
        past = min(self._hist, key=lambda tp: abs(tp[0] - target))[1]
        px = self.price or self._px()
        if not past or not px:
            return None
        return (px - past) / past * 100.0

    def _flow_pct(self, sec):
        if self.whales is None:
            return None
        now_ms = time.time() * 1000.0
        buy = sell = 0.0
        try:
            for t in reversed(self.whales.tape):
                if now_ms - float(t.get("ts") or 0) > sec * 1000.0:
                    break
                usd = float(t.get("usd") or 0.0)
                if t.get("side") == "buy":
                    buy += usd
                else:
                    sell += usd
        except Exception:
            return None
        tot = buy + sell
        return (buy / tot * 100.0) if tot > 0 else None

    def _snapshot(self):
        px = self._px()
        flow_1m = self._flow_pct(60)
        flow_5m = self._flow_pct(300)
        flow_15m = self._flow_pct(900)
        return {
            "price": round(px, 2) if px else None,
            "change_10s_pct": round(self._change_pct(10) or 0.0, 3),
            "change_1m_pct": round(self._change_pct(60) or 0.0, 3),
            "flow_1m": None if flow_1m is None else round(flow_1m, 1),
            "flow_5m": None if flow_5m is None else round(flow_5m, 1),
            "flow_15m": None if flow_15m is None else round(flow_15m, 1),
        }

    # ---- Claude --------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(
                api_key=getattr(config, "ANTHROPIC_API_KEY", ""),
                timeout=getattr(config, "CLAUDE_TIMEOUT_SEC", AI_TIMEOUT),
            )
        return self._client

    @staticmethod
    def _anthropic_text(resp):
        return "".join(getattr(b, "text", "") for b in getattr(resp, "content", []) if getattr(b, "type", "") == "text")

    async def _ask_claude(self, source, headline, snap):
        if not getattr(config, "ANTHROPIC_API_KEY", ""):
            raise RuntimeError("ANTHROPIC_API_KEY missing in config.py")
        user = (
            f"SOURCE: {source or 'unknown'}\n"
            f"HEADLINE: {headline}\n\n"
            "LIVE BTC SNAPSHOT:\n"
            f"price={snap.get('price')}\n"
            f"change_10s={snap.get('change_10s_pct')}%\n"
            f"change_1m={snap.get('change_1m_pct')}%\n"
            f"flow_1m={snap.get('flow_1m')}% buy\n"
            f"flow_5m={snap.get('flow_5m')}% buy\n"
            f"flow_15m={snap.get('flow_15m')}% buy"
        )
        resp = await self._get_client().messages.create(
            model=MODEL,
            max_tokens=420,
            temperature=0.1,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
        data = _safe_json(self._anthropic_text(resp) or "{}")
        if not data:
            raise RuntimeError("Claude reply not parseable as JSON")
        return data

    def _normalize_decision(self, data):
        tv = data.get("trade")
        trade = (str(tv).strip().lower() in ("true", "yes", "1")) if isinstance(tv, str) else bool(tv)
        direction = str(data.get("direction", "")).lower().strip()
        if direction not in ("long", "short"):
            trade = False
            direction = ""
        sl = _clamp(data.get("stop_loss_pct"), STOP_MIN, STOP_MAX, 0.0)
        tp = _clamp(data.get("take_profit_pct"), TP_MIN, TP_MAX, 0.0)
        hold = _clamp(data.get("max_hold_minutes"), HOLD_MIN, HOLD_MAX, 15.0)
        if trade and (sl <= 0 or tp <= 0):
            trade = False
        return {
            "trade": trade,
            "direction": direction,
            "stop_loss_pct": sl,
            "take_profit_pct": tp,
            "max_hold_minutes": hold,
            "confidence": str(data.get("confidence", "low")).lower()[:20],
            "reason": str(data.get("reason", ""))[:120],
        }

    # ---- news entrypoint -----------------------------------------------
    @staticmethod
    def _key(text):
        return " ".join((text or "").lower().split())[:220]

    def on_news(self, source="", text=""):
        if not self.enabled:
            return
        headline = (text or "").strip().replace("\n", " ")
        if len(headline) < 8:
            return
        k = self._key(headline)
        if k in self._seen_set:
            return
        self._seen.append(k)
        self._seen_set.add(k)
        if len(self._seen) == self._seen.maxlen:
            self._seen_set = set(self._seen)
        self.recent_news.insert(0, {"t": time.time(), "source": source, "headline": headline[:260]})
        self.recent_news = self.recent_news[:80]
        try:
            asyncio.create_task(self._react(source, headline[:500]))
        except RuntimeError:
            pass

    async def _react(self, source, headline):
        if self._sem is None:
            self._sem = asyncio.Semaphore(MAX_INFLIGHT)
        async with self._sem:
            if time.time() < self._cooldown_until:
                return
            px = self._px()
            if not px:
                self._record_decision(headline, source, {"trade": False, "reason": "no BTC price"}, 0, "skip")
                return
            snap = self._snapshot()
            t0 = time.perf_counter()
            try:
                raw = await asyncio.wait_for(self._ask_claude(source, headline, snap), timeout=AI_TIMEOUT + 2)
                dec = self._normalize_decision(raw)
                latency = (time.perf_counter() - t0) * 1000.0
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:100]}"
                if "401" in msg or "authentication" in msg.lower() or "unauthorized" in msg.lower():
                    self._cooldown_until = time.time() + 300
                self._note(f"Claude error -> skip: {msg}", "error")
                self._record_decision(headline, source, {"trade": False, "reason": msg}, 0, "error")
                return

            kind = "open" if dec["trade"] else "skip"
            self._record_decision(headline, source, dec, latency, kind)
            if not dec["trade"]:
                self._note(f"PASS {dec.get('reason') or 'not immediate'}: {headline[:70]}", "skip")
                return

            side = dec["direction"]
            if self.pos:
                if self.pos.get("side") == side:
                    self._note(f"AI {side.upper()} but already in same direction: {dec['reason']}", "skip")
                    return
                self._close(px, f"ai_flip_new_headline ({dec['reason']})")
            self._open(px, side, dec, headline)

    def _record_decision(self, headline, source, dec, latency_ms, kind):
        self.decisions.insert(0, {
            "t": time.time(),
            "source": source,
            "headline": headline[:260],
            "decision": "TRADE" if dec.get("trade") else "SKIP",
            "side": dec.get("direction") or "",
            "sl": dec.get("stop_loss_pct"),
            "tp": dec.get("take_profit_pct"),
            "hold": dec.get("max_hold_minutes"),
            "confidence": dec.get("confidence", ""),
            "reason": dec.get("reason", ""),
            "latency_ms": round(latency_ms, 0) if latency_ms else 0,
            "kind": kind,
        })
        self.decisions = self.decisions[:200]
        self._save()

    # ---- position loop --------------------------------------------------
    async def manage_loop(self):
        while True:
            try:
                px = self._px()
                if px:
                    self._hist.append((time.time(), px))
                    if self.pos:
                        self._manage(px)
                self._status()
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:80]}", "error")
            await asyncio.sleep(POLL_SEC)

    def _open(self, price, side, dec, headline):
        eq = self._equity()
        if eq <= 0:
            self._note("wanted trade but account equity is zero", "skip")
            return
        notional = eq * LEVERAGE
        qty = notional / price
        sl_pct = dec["stop_loss_pct"] / 100.0
        tp_pct = dec["take_profit_pct"] / 100.0
        if side == "long":
            stop = price * (1 - sl_pct)
            tp = price * (1 + tp_pct)
            liq = price * (1 - 1 / LEVERAGE)
        else:
            stop = price * (1 + sl_pct)
            tp = price * (1 - tp_pct)
            liq = price * (1 + 1 / LEVERAGE)
        self.pos = {
            "id": uuid.uuid4().hex[:8],
            "side": side,
            "entry": price,
            "qty": qty,
            "notional": round(notional, 2),
            "margin": round(eq, 2),
            "leverage": LEVERAGE,
            "stop": stop,
            "tp": tp,
            "liq": liq,
            "opened_at": time.time(),
            "max_hold_min": dec["max_hold_minutes"],
            "confidence": dec["confidence"],
            "reason": dec["reason"],
            "news": headline[:260],
        }
        self._note(f"OPEN {side.upper()} @ ${price:,.1f} - Claude {dec['confidence']} - "
                   f"SL {dec['stop_loss_pct']:.2f}% / TP {dec['take_profit_pct']:.2f}% - "
                   f"{dec['reason']}", "open")
        self._save()

    def _manage(self, price):
        p = self.pos
        if not p:
            return
        side = p["side"]
        reason = None
        if side == "long":
            if price <= p["liq"]:
                reason = "liquidation"
            elif price <= p["stop"]:
                reason = "ai_stop_loss"
            elif price >= p["tp"]:
                reason = "ai_take_profit"
        else:
            if price >= p["liq"]:
                reason = "liquidation"
            elif price >= p["stop"]:
                reason = "ai_stop_loss"
            elif price <= p["tp"]:
                reason = "ai_take_profit"
        if reason is None and (time.time() - p["opened_at"]) >= float(p.get("max_hold_min", 15)) * 60.0:
            reason = "ai_time_stop"
        if reason:
            self._close(price, reason)

    def _close(self, price, reason):
        p = self.pos
        if not p:
            return
        long = p["side"] == "long"
        pnl = p["qty"] * ((price - p["entry"]) if long else (p["entry"] - price)) - p["notional"] * FEE
        pnl = max(pnl, -float(p.get("margin", START_BAL)))
        self.balance = max(0.0, self.balance + pnl)
        self.history.insert(0, {
            "side": p["side"],
            "entry": round(p["entry"], 1),
            "exit": round(price, 1),
            "pnl": round(pnl, 2),
            "reason": reason,
            "confidence": p.get("confidence"),
            "news": p.get("news"),
            "opened_at": p["opened_at"],
            "closed_at": time.time(),
        })
        self.history = self.history[:200]
        self.pos = None
        self._note(f"CLOSE {p['side'].upper()} @ ${price:,.1f} - {reason} - P&L ${pnl:+.2f}",
                   "win" if pnl >= 0 else "loss")
        self._save()

    # ---- controls / state -----------------------------------------------
    def _equity(self):
        eq = self.balance
        if self.pos and self.price:
            p = self.pos
            if p["side"] == "long":
                eq += p["qty"] * (self.price - p["entry"])
            else:
                eq += p["qty"] * (p["entry"] - self.price)
        return max(0.0, eq)

    def _status(self):
        eq = self._equity()
        auth = "key set" if getattr(config, "ANTHROPIC_API_KEY", "") else "missing key"
        self.status = (f"Claude Haiku live-feed AI - {auth} - "
                       f"{'holding ' + self.pos['side'] if self.pos else 'flat'} - "
                       f"equity ${eq:,.2f} - {'enabled' if self.enabled else 'paused'}")

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
        self.decisions = []
        self.recent_news = []
        self._seen.clear()
        self._seen_set.clear()
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        px = self._px()
        eq = self._equity()
        positions = []
        if self.pos:
            p = self.pos
            pnl = 0.0
            if px:
                pnl = p["qty"] * ((px - p["entry"]) if p["side"] == "long" else (p["entry"] - px))
            margin = float(p.get("margin", 0.0) or 0.0)
            positions.append({
                "side": p["side"],
                "entry": round(p["entry"], 1),
                "qty": round(p["qty"], 6),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / margin * 100.0, 2) if margin else 0.0,
                "stop": round(p["stop"], 1),
                "tp1": round(p["tp"], 1),
                "liq": round(p["liq"], 1),
                "leverage": LEVERAGE,
                "news": p.get("news", ""),
            })
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        return {
            "enabled": self.enabled,
            "status": self.status,
            "model": MODEL,
            "price": round(px, 1) if px else None,
            "balance": round(self.balance, 2),
            "equity": round(eq, 2),
            "start_balance": START_BAL,
            "total_pnl": round(eq - START_BAL, 2),
            "total_pnl_pct": round((eq / START_BAL - 1) * 100.0, 2) if START_BAL else 0.0,
            "positions": positions,
            "trades": len(self.history),
            "wins": wins,
            "history": self.history[:80],
            "log": self.log[:30],
            "decisions": self.decisions[:20],
            "recent_news": self.recent_news[:20],
            "leverage": LEVERAGE,
            "running": True,
        }
