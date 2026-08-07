"""
================================================================================
 news_reactor_bot.py  —  "NEWS REACTOR"  (paper, self-contained, from scratch)
================================================================================
A brand-new news-reaction paper trader. It does NOT share code with any other
bot — its own brain, its own scoring, its own sizing, its own exits.

How it actually trades, step by step:

 1. A headline lands on the site's news feed (Tree of Alpha Latest News + Twitter,
    plus any Telegram/RSS source). It goes straight to this bot.

 2. The bot asks an AI ONE question per headline and forces a compact JSON answer
    that scores the news on its OWN scale:
        tradeable          – is this actually market-moving & actionable right now?
        asset / direction  – the single best coin to express it, and which way
        impact   (0-100)   – how big a market reaction to expect
        confidence (0-100) – how sure the model is of the call
        expected_move_pct  – how far it thinks the coin moves on this news
        hold_minutes       – how long the edge should last

 3. GATE — it trades only if tradeable AND impact and confidence both clear the
    threshold set by the "aggressiveness" mode you pick in the UI.

 4. CONVICTION-WEIGHTED SIZING (this bot's idea) — the bet size scales with the
    news: margin = bankroll * MAX_RISK * (impact/100) * (confidence/100). A huge,
    high-confidence headline bets big; a marginal one bets small.

 5. NEWS-MAGNITUDE EXITS (this bot's idea) — the take-profit is the move the AI
    expects, and the stop is a fraction of it (so reward > risk by construction),
    bounded to sane limits. Hold time is the AI's own estimate. A liquidation
    backstop sits underneath.

Everything is paper money, on real live prices, fully isolated from your other
accounts. Toggle / reset / pick the mode on the /paper page.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, asdict

import aiohttp

import config

STATE_PATH = os.path.join(config.DATA_DIR, "news_reactor_state.json")

# ---- bankroll & risk (this bot's own knobs) ---------------------------------
START_BALANCE   = 100.0      # its own separate paper balance (USDT)
LEVERAGE        = 5          # fixed, modest — exposure is scaled by sizing, not leverage
MAX_RISK_FRAC   = 0.30       # the MOST of the bankroll one trade can put up as margin
MIN_MARGIN      = 3.0        # don't bother opening sub-$3 positions
MAX_CONCURRENT  = 6          # never hold more than this many at once
MAX_PICKS_PER_HEADLINE = 4   # most coins ONE headline can open at once (the AI picks who)
MAX_INFLIGHT    = 5          # cap concurrent AI calls so a news burst can't pile up
AI_TIMEOUT      = 12.0       # hard cap (s) on one decision

# news-magnitude exit bounds (% price move)
TP_MIN_PCT, TP_MAX_PCT = 0.004, 0.05     # clamp the AI's expected move into [0.4%, 5%]
SL_RATIO               = 0.55            # stop distance = TP distance * this (reward-skewed)
HOLD_MIN, HOLD_MAX     = 5, 720          # clamp the AI's hold estimate (minutes)

# ---- aggressiveness modes (shown in the UI) ---------------------------------
# each maps to the (min impact, min confidence) a headline must clear to trade.
MODES = {
    "conservative": {"label": "Conservative (only big, sure news)", "impact": 70, "conf": 70},
    "balanced":     {"label": "Balanced (default)",                 "impact": 55, "conf": 55},
    "aggressive":   {"label": "Aggressive (acts on more news)",     "impact": 42, "conf": 45},
}
DEFAULT_MODE = "balanced"

# ---- the brain: this bot's OWN prompt (not shared with anything) -------------
# The AI lists EVERY coin a headline genuinely moves — the NEWS decides how many:
# usually one (only LINK, only PEPE…), sometimes several, often none.
_PROMPT = (
    "You are a crypto markets news desk. You receive ONE news headline and a tiny "
    "live price snapshot. Decide whether it is a real, market-moving, tradable "
    "catalyst for crypto perpetuals RIGHT NOW. Most headlines are noise, chit-chat, "
    "opinions, ads, or already priced in -> tradeable=false. Truncated/unclear -> "
    "tradeable=false.\n"
    "List EVERY coin this specific headline genuinely moves, and how to trade each — "
    "let the NEWS decide the count, do NOT force a number. A coin-specific story "
    "usually affects exactly ONE coin (e.g. only LINK, or only PEPE). A broad/macro "
    "or risk-on/off story typically moves BTC and/or ETH. A genuinely multi-coin "
    "story (e.g. 'SEC approves SOL, XRP and ADA ETFs', or a sector-wide event) "
    "affects several. Do NOT pad the list — include a coin ONLY if THIS headline "
    "really moves it. Each coin must be from this tradable set: "
    "BTC ETH SOL BNB XRP ADA DOGE AVAX LINK DOT MATIC LTC BCH ATOM UNI TRX NEAR APT "
    "ARB OP FIL INJ SUI SEI TIA RUNE AAVE ETC FTM ALGO PEPE WIF ENA.\n"
    "Reply with ONLY compact JSON, no prose:\n"
    "{\"tradeable\":true|false,\"picks\":[{\"asset\":\"TICKER\",\"direction\":\"long\""
    "|\"short\",\"impact\":0-100,\"confidence\":0-100,\"expected_move_pct\":number,"
    "\"hold_minutes\":number,\"why\":\"max 10 words\"}]}\n"
    "If nothing is tradable, return tradeable:false and picks:[]."
)


@dataclass
class Trade:
    id: str
    symbol: str
    coin: str
    side: str               # long | short
    entry: float
    qty: float
    margin: float
    leverage: float
    sl: float               # stop-loss PRICE
    tp: float               # take-profit PRICE
    opened_at: float
    hold_min: float
    impact: int
    confidence: int
    why: str
    headline: str

    def pnl(self, mark: float) -> float:
        return self.qty * (mark - self.entry) if self.side == "long" else self.qty * (self.entry - mark)

    def liq(self) -> float:
        return self.entry * (1 - 1 / self.leverage) if self.side == "long" else self.entry * (1 + 1 / self.leverage)


class NewsReactorBot:
    def __init__(self, market=None):
        self.market = market
        self.enabled = True
        self.mode = DEFAULT_MODE
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.open: dict[str, Trade] = {}
        self.history: list[dict] = []
        self.log: list[dict] = []
        self._inflight = 0
        self._seen = deque(maxlen=800)
        self._seen_set: set[str] = set()
        self._cooldown_until = 0.0          # pause AI calls until this ts after a rate-limit
        self._load()

    # ---- wiring -----------------------------------------------------------
    def attach(self, market):
        self.market = market

    # ---- persistence ------------------------------------------------------
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"balance": self.balance, "start_balance": self.start_balance,
                           "enabled": self.enabled, "mode": self.mode,
                           "history": self.history, "log": self.log[:60],
                           "open": [asdict(t) for t in self.open.values()]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.balance = float(d.get("balance", self.balance))
            self.start_balance = float(d.get("start_balance", self.start_balance))
            self.enabled = bool(d.get("enabled", self.enabled))
            if d.get("mode") in MODES:
                self.mode = d["mode"]
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            for td in d.get("open", []) or []:
                try:
                    t = Trade(**td); self.open[t.id] = t
                except Exception:
                    pass
            print(f"[reactor] restored: ${self.balance:.2f}, {len(self.history)} trades, {len(self.open)} open")
        except Exception:
            pass

    # ---- helpers ----------------------------------------------------------
    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[reactor] {msg}")

    def _price(self, symbol):
        try:
            return self.market.price(symbol) if self.market else None
        except Exception:
            return None

    @staticmethod
    def _key(text):
        return " ".join((text or "").lower().split())[:160]

    def _symbol_of(self, asset):
        a = (asset or "").upper().lstrip("$").strip()
        return config.ASSET_TO_SYMBOL.get(a)

    # ---- the AI call: fully self-contained (OpenAI-compatible endpoint) ----
    async def _ask_ai(self, headline):
        snap = {}
        try:
            for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                p = self._price(sym)
                if p:
                    snap[sym.replace("USDT", "")] = round(p, 2)
        except Exception:
            pass
        user = f"HEADLINE: {headline}\nPRICES: {snap or 'n/a'}"
        # Uses GROQ (the free "GPTnews" model) — its OpenAI-compatible endpoint.
        url = config.GROQ_BASE_URL.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}", "Content-Type": "application/json"}
        body = {"model": config.GROQ_MODEL, "temperature": 0.2,
                "messages": [{"role": "system", "content": _PROMPT},
                             {"role": "user", "content": user}]}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, json=body,
                              timeout=aiohttp.ClientTimeout(total=AI_TIMEOUT)) as r:
                status = r.status
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = None
        # APIs return errors as {"error":{...}} OR a JSON array [{"error":{...}}].
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            raise RuntimeError(f"HTTP {status}: bad reply")
        if status != 200 or "error" in data:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            msg = str(err.get("message") or data.get("error") or f"HTTP {status}")
            if status == 429 or "RESOURCE_EXHAUSTED" in str(data) or "quota" in msg.lower() \
                    or "rate limit" in msg.lower():
                raise RuntimeError(f"rate-limited (HTTP {status})")
            raise RuntimeError(f"Groq HTTP {status}: {msg[:80]}")
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("Groq returned no content (empty)")
        return self._parse(text)

    @staticmethod
    def _parse(text):
        t = (text or "").strip()
        if t.startswith("```"):
            t = t.strip("`")
            if t[:4].lower() == "json":
                t = t[4:]
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1:
            raise ValueError("no JSON in AI reply")
        return json.loads(t[i:j + 1])

    # ---- entrypoint: every headline on the site comes here -----------------
    async def on_news(self, source, text):
        if not self.enabled:
            return
        if time.time() < self._cooldown_until:    # backing off after a rate-limit — skip quietly
            return
        headline = (text or "").strip().replace("\n", " ")
        if len(headline) < 8:
            return
        k = self._key(headline)
        if k in self._seen_set:
            return
        self._seen.append(k); self._seen_set.add(k)
        if len(self._seen) == self._seen.maxlen:
            self._seen_set = set(self._seen)
        if self._inflight >= MAX_INFLIGHT:
            self._note(f"AI busy → skipped: {headline[:55]}", "skip")
            return
        asyncio.create_task(self._react(headline[:300]))

    async def _react(self, headline):
        self._inflight += 1
        try:
            try:
                d = await asyncio.wait_for(self._ask_ai(headline), timeout=AI_TIMEOUT + 2)
            except asyncio.TimeoutError:
                self._note(f"AI timed out → skipped: {headline[:50]}", "error"); return
            except Exception as e:
                msg = str(e) or type(e).__name__
                if "rate-limited" in msg:
                    # back off for 60s so we stop hammering the quota and spamming the log
                    if time.time() >= self._cooldown_until:
                        self._note(f"AI {msg} — pausing 60s", "error")
                    self._cooldown_until = time.time() + 60
                else:
                    self._note(f"AI error ({msg[:60]}) → skipped: {headline[:40]}", "error")
                return

            if not bool(d.get("tradeable")):
                self._note(f"PASS (not market-moving): {headline[:55]}", "skip"); return
            picks = d.get("picks")
            if not isinstance(picks, list) or not picks:
                self._note(f"PASS (no coins affected): {headline[:55]}", "skip"); return

            # Trade EVERY coin the AI flagged that clears the gate — one, several, or
            # none. The news (via the AI) decides which coins; we just size & open each.
            gate = MODES[self.mode]
            acted = 0
            for p in picks:
                if acted >= MAX_PICKS_PER_HEADLINE:
                    break
                if not isinstance(p, dict):
                    continue
                impact = int(float(p.get("impact", 0) or 0))
                conf = int(float(p.get("confidence", 0) or 0))
                direction = str(p.get("direction", "")).lower()
                coin = str(p.get("asset") or "").upper().lstrip("$").strip()
                symbol = self._symbol_of(coin)
                why = str(p.get("why", ""))[:80]
                if direction not in ("long", "short") or not symbol:
                    continue
                if impact < gate["impact"] or conf < gate["conf"]:
                    self._note(f"PASS {coin} (impact {impact}/conf {conf} < {self.mode}): "
                               f"{headline[:38]}", "skip")
                    continue
                exp_move = abs(float(p.get("expected_move_pct", 0) or 0)) / 100.0
                hold = float(p.get("hold_minutes", 60) or 60)
                if self._enter(symbol, direction, impact, conf, exp_move, hold, why, headline):
                    acted += 1
        finally:
            self._inflight = max(0, self._inflight - 1)

    def _enter(self, symbol, side, impact, conf, exp_move, hold, why, headline):
        if len(self.open) >= MAX_CONCURRENT:
            self._note(f"at {MAX_CONCURRENT} open → skipped {symbol}", "skip"); return False
        if any(t.symbol == symbol for t in self.open.values()):
            self._note(f"already in {symbol} → skipped", "skip"); return False
        px = self._price(symbol)
        if not px:
            self._note(f"no price for {symbol} → skipped", "skip"); return False

        coin = symbol.replace("USDT", "")
        # conviction-weighted sizing: bigger, surer news -> bigger bet
        score = (impact / 100.0) * (conf / 100.0)
        margin = round(min(self.balance * MAX_RISK_FRAC * score, self.balance * MAX_RISK_FRAC), 2)
        if margin < MIN_MARGIN:
            self._note(f"size too small (${margin:.2f}) → skipped {coin}", "skip"); return False
        margin = min(margin, round(self.balance, 2))

        # news-magnitude exits: TP = expected move (bounded); SL = a fraction of it
        tp_pct = min(max(exp_move if exp_move > 0 else TP_MIN_PCT, TP_MIN_PCT), TP_MAX_PCT)
        sl_pct = tp_pct * SL_RATIO
        if side == "long":
            tp, sl = px * (1 + tp_pct), px * (1 - sl_pct)
        else:
            tp, sl = px * (1 - tp_pct), px * (1 + sl_pct)
        hold = max(HOLD_MIN, min(hold, HOLD_MAX))
        qty = margin * LEVERAGE / px

        t = Trade(id=uuid.uuid4().hex[:6], symbol=symbol, coin=coin, side=side, entry=px,
                  qty=qty, margin=margin, leverage=LEVERAGE, sl=sl, tp=tp,
                  opened_at=time.time(), hold_min=hold, impact=impact, confidence=conf,
                  why=why, headline=headline)
        self.balance -= margin
        self.open[t.id] = t
        self._note(f"ENTER {side.upper()} {coin} @ {px:g} · impact {impact}/conf {conf} · "
                   f"margin ${margin:.2f} · TP {tp_pct*100:.2f}%/SL {sl_pct*100:.2f}% · {why}", "open")
        self._save()
        return True

    def _exit(self, t, px, reason):
        self.open.pop(t.id, None)
        pnl = t.pnl(px)
        self.balance += t.margin + pnl
        self.history.insert(0, {"id": t.id, "coin": t.coin, "side": t.side,
                                "entry": round(t.entry, 8), "exit": round(px, 8),
                                "margin": round(t.margin, 2), "pnl": round(pnl, 2),
                                "reason": reason, "impact": t.impact, "confidence": t.confidence,
                                "headline": t.headline, "closed_at": time.time()})
        self.history = self.history
        self._note(f"EXIT {t.side.upper()} {t.coin} @ {px:g} · {reason} · P&L ${pnl:+.2f}",
                   "win" if pnl >= 0 else "loss")
        self._save()

    # ---- mark / exit loop -------------------------------------------------
    async def manage_loop(self):
        while True:
            await asyncio.sleep(2.0)
            if not self.open:
                continue
            now = time.time()
            for t in list(self.open.values()):
                px = self._price(t.symbol)
                if not px:
                    continue
                if t.pnl(px) <= -t.margin * 0.97:
                    self._exit(t, px, "liquidation"); continue
                if t.side == "long":
                    if px >= t.tp: self._exit(t, px, "take_profit"); continue
                    if px <= t.sl: self._exit(t, px, "stop_loss"); continue
                else:
                    if px <= t.tp: self._exit(t, px, "take_profit"); continue
                    if px >= t.sl: self._exit(t, px, "stop_loss"); continue
                if (now - t.opened_at) / 60.0 >= t.hold_min:
                    self._exit(t, px, "time_stop")

    # ---- controls + snapshot ---------------------------------------------
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_mode(self, mode):
        mode = (mode or "").strip().lower()
        if mode not in MODES:
            return {"ok": False, "error": f"unknown mode '{mode}'"}
        self.mode = mode
        self._note(f"mode set to {MODES[mode]['label']}")
        self._save()
        return {"ok": True, "mode": mode}

    def reset(self):
        self.enabled = True
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.open = {}
        self.history = []
        self.log = []
        self._inflight = 0
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self):
        positions, equity = [], self.balance
        for t in self.open.values():
            px = self._price(t.symbol)
            up = t.pnl(px) if px else 0.0
            equity += t.margin + up
            positions.append({
                "id": t.id, "coin": t.coin, "side": t.side, "entry": t.entry,
                "qty": round(t.qty, 6), "margin": round(t.margin, 2), "leverage": t.leverage,
                "liq": round(t.liq(), 8), "stop": t.sl, "tp1": t.tp, "mark": px,
                "impact": t.impact, "confidence": t.confidence,
                "news": f"{t.coin} · impact {t.impact}/conf {t.confidence} · {t.why}",
                "pnl": round(up, 2), "pnl_pct": round(up / t.margin * 100, 2) if t.margin else 0})
        wins = sum(1 for h in self.history if h["pnl"] > 0)
        return {
            "enabled": self.enabled, "analyzing": self._inflight,
            "mode": self.mode, "modes": {k: v["label"] for k, v in MODES.items()},
            "balance": round(self.balance, 2), "equity": round(equity, 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(equity - self.start_balance, 2),
            "trades": len(self.history), "wins": wins,
            "positions": positions, "history": self.history, "log": self.log[:25],
        }
