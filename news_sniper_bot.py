"""
================================================================================
 news_sniper_bot.py  —  "NEWS SNIPER"  (paper, NO AI, instant)
================================================================================
A news-reaction paper trader that uses ZERO AI. No model call, no network round
trip — it reads the headline TEXT directly with a deterministic rules engine, so
it reacts the microsecond a headline lands.

Strategy (entirely different from the other bots, which wait for price/flow):

 1. COIN DETECTION — pull the coin straight out of the words. It scans the
    headline for cashtags ($PEPE), bare tickers (LINK), and full names (chainlink,
    bitcoin…) and maps them to a tradable symbol. So the NEWS itself names the coin
    — one, several, or none. No model needed.

 2. LEXICON + EVENT SCORING — score the headline with a curated, weighted
    dictionary of bullish vs bearish words and high-signal event phrases
    (listing / ETF approval / partnership vs hack / lawsuit / delist / depeg…),
    with simple negation handling ("not approved" flips sign). Net score's SIGN is
    the direction; its SIZE is the conviction.

 3. FIRE — if the score clears the chosen threshold and a coin was named, it opens
    a paper position instantly: conviction-weighted size, conviction-tiered TP/SL,
    a short hold (news alpha decays fast), and a per-coin cooldown.

Macro headlines with no coin but a strong macro word (Fed/CPI/ETF/SEC/tariff…)
default to BTC. All paper money, real prices, isolated account. Controls on /paper.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, asdict

import config

STATE_PATH = os.path.join(config.DATA_DIR, "news_sniper_state.json")

# ---- account / risk ---------------------------------------------------------
START_BALANCE   = 100.0
LEVERAGE        = 5
MAX_RISK_FRAC   = 0.30        # most of bankroll one trade can margin
MIN_MARGIN      = 3.0
MAX_CONCURRENT  = 6
MAX_PICKS_PER_HEADLINE = 4
PER_COIN_COOLDOWN = 90        # seconds before the same coin can be re-fired

# ---- aggressiveness modes: minimum |score| needed to fire -------------------
MODES = {
    "conservative": {"label": "Conservative (only strong, clear words)", "min": 3.5},
    "balanced":     {"label": "Balanced (default)",                      "min": 2.2},
    "aggressive":   {"label": "Aggressive (acts on faint signals)",      "min": 1.2},
}
DEFAULT_MODE = "balanced"

# conviction tiers by |score| -> (tp%, sl%, hold minutes)
def _tier(s):
    a = abs(s)
    if a >= 4.0:  return "high",   0.035, 0.020, 120
    if a >= 2.2:  return "medium", 0.020, 0.012, 75
    return "low", 0.012, 0.008, 40

# ---- the "brain": a curated lexicon (THIS is the strategy, tune freely) ------
# multi-word phrases checked as substrings (catch strong, specific events first)
PHRASES = {
    # bullish events
    "etf approv": 3.5, "spot etf": 1.8, "will list": 3.0, "to list": 2.2,
    "gets listed": 2.8, "now listed": 2.8, "lists ": 2.0, "listing": 2.2,
    "lawsuit dismissed": 3.0, "case dismissed": 2.6, "charges dropped": 2.6,
    "strategic reserve": 2.5, "adds to balance sheet": 2.2, "buyback": 2.0,
    "partnership with": 1.8, "mainnet launch": 2.0, "record high": 2.0,
    "all-time high": 2.0, "approved by": 1.5, "greenlight": 2.5,
    # bearish events
    "files for bankruptcy": 3.6, "chapter 11": 3.0, "funds stolen": 3.2,
    "security breach": 2.8, "private keys": 1.8, "exit scam": 3.2,
    "cease and desist": 2.2, "wells notice": 2.6, "token unlock": 1.6,
    "flash crash": 2.6, "sell-off": 1.6, "depeg": 3.0, "rug pull": 3.2,
    "class action": 2.0, "halts withdrawals": 3.2, "pauses withdrawals": 3.0,
}
# single-word stems (matched on whole tokens or by prefix where unambiguous)
POS_WORDS = {
    "approved": 2.2, "approval": 2.2, "approves": 2.2, "partnership": 2.0,
    "partner": 1.2, "integrates": 1.5, "integration": 1.5, "adopts": 2.0,
    "adoption": 2.0, "launches": 1.5, "launch": 1.3, "upgrade": 1.2,
    "acquires": 2.0, "acquired": 2.0, "invests": 1.5, "investment": 1.3,
    "funding": 1.3, "raises": 1.3, "surge": 1.6, "surges": 1.6, "soars": 1.6,
    "rally": 1.5, "rallies": 1.5, "bullish": 1.6, "wins": 1.6, "won": 1.4,
    "wins": 1.6, "settles": 1.2, "settlement": 1.2, "unveils": 1.0,
    "milestone": 1.0, "inflows": 1.6, "accumulate": 1.4, "accumulation": 1.4,
}
NEG_WORDS = {
    "hack": 3.0, "hacked": 3.0, "exploit": 3.0, "exploited": 3.0,
    "drained": 2.8, "stolen": 2.6, "breach": 2.2, "lawsuit": 2.0, "sues": 2.0,
    "sued": 2.0, "charged": 1.8, "investigation": 2.0, "investigating": 2.0,
    "subpoena": 2.0, "ban": 2.4, "banned": 2.4, "bans": 2.4, "delisted": 3.0,
    "delist": 3.0, "halt": 2.4, "halted": 2.4, "suspends": 2.0, "suspended": 2.0,
    "bankruptcy": 3.0, "bankrupt": 3.0, "insolvent": 3.0, "insolvency": 3.0,
    "liquidated": 1.6, "liquidation": 1.4, "dump": 1.5, "dumps": 1.5,
    "crash": 2.0, "crashes": 2.0, "plunge": 2.0, "plunges": 2.0, "selloff": 1.6,
    "bearish": 1.6, "fraud": 2.6, "scam": 2.2, "outage": 1.6, "exploiter": 2.4,
    "rugged": 3.0, "downtime": 1.4, "vulnerability": 1.8, "phishing": 1.6,
}
NEGATORS = {"not", "no", "denies", "denied", "deny", "rejects", "rejected",
            "isn't", "isnt", "won't", "wont", "fails", "failed", "without"}
MACRO_WORDS = {"fed", "fomc", "powell", "rate", "rates", "cpi", "ppi", "inflation",
               "tariff", "tariffs", "sec", "etf", "jobs", "nfp", "interest",
               "treasury", "trump", "war", "sanctions", "sanction", "recession"}


@dataclass
class Trade:
    id: str
    symbol: str
    coin: str
    side: str
    entry: float
    qty: float
    margin: float
    leverage: float
    sl: float
    tp: float
    opened_at: float
    hold_min: float
    tier: str
    score: float
    why: str
    headline: str

    def pnl(self, mark): return self.qty * (mark - self.entry) if self.side == "long" else self.qty * (self.entry - mark)
    def liq(self): return self.entry * (1 - 1 / self.leverage) if self.side == "long" else self.entry * (1 + 1 / self.leverage)


class NewsSniperBot:
    def __init__(self, market=None):
        self.market = market
        self.enabled = True
        self.mode = DEFAULT_MODE
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.open: dict[str, Trade] = {}
        self.history: list[dict] = []
        self.log: list[dict] = []
        self._seen = deque(maxlen=800)
        self._seen_set: set[str] = set()
        self._cooldown: dict[str, float] = {}     # symbol -> last-fired ts
        # coin name lookup (lowercased full names, len>=4, minus ambiguous words)
        self._names = {k.lower(): v for k, v in config.ASSET_TO_SYMBOL.items()
                       if len(k) >= 4 and k.lower() not in {"near"}}
        # canonical ticker per symbol = the SHORTEST key mapping to it (so SOLUSDT -> "SOL")
        self._ticker: dict[str, str] = {}
        for k, v in config.ASSET_TO_SYMBOL.items():
            if v not in self._ticker or len(k) < len(self._ticker[v]):
                self._ticker[v] = k
        self._load()

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
            print(f"[sniper] restored: ${self.balance:.2f}, {len(self.history)} trades, {len(self.open)} open")
        except Exception:
            pass

    # ---- helpers ----------------------------------------------------------
    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[sniper] {msg}")

    def _price(self, symbol):
        try:
            return self.market.price(symbol) if self.market else None
        except Exception:
            return None

    @staticmethod
    def _key(text):
        return " ".join((text or "").lower().split())[:160]

    # ---- coin detection: pull tickers/names straight out of the text -------
    def _detect_coins(self, text):
        found, seen = [], set()
        def add(sym):
            if sym and sym not in seen:
                seen.add(sym)
                found.append((self._ticker.get(sym, sym.replace("USDT", "")), sym))
        # $cashtags
        for m in re.findall(r"\$([A-Za-z]{2,6})", text):
            add(config.ASSET_TO_SYMBOL.get(m.upper()))
        # bare UPPERCASE tickers (uppercase requirement kills 'etc.'/'near'/'apt' collisions)
        for m in re.findall(r"\b[A-Z]{2,5}\b", text):
            add(config.ASSET_TO_SYMBOL.get(m))
        # lowercase full names (bitcoin, ethereum, chainlink, pepe…)
        for tok in re.findall(r"[a-z]{4,}", text.lower()):
            add(self._names.get(tok))
        return found

    # ---- the scoring engine (pure rules, instant) -------------------------
    def _score(self, text):
        low = " " + text.lower() + " "
        score = 0.0
        hits = []
        # phrases: substring match, but flip sign if a negator sits just before it
        for phrase, w in PHRASES.items():
            idx = low.find(phrase)
            if idx == -1:
                continue
            pre = set(low[max(0, idx - 25):idx].split())
            negated = bool(pre & NEGATORS)
            score += (-w if negated else w)
            hits.append(("!" if negated else "") + phrase)
        # single words: negation window flips the next few tokens after a negator
        toks = re.findall(r"[a-z']+", low)
        neg = 0
        for tok in toks:
            if tok in NEGATORS:
                neg = 3
                continue
            w = POS_WORDS.get(tok, 0.0) - NEG_WORDS.get(tok, 0.0)
            if w:
                score += (-w if neg > 0 else w)
                hits.append(("!" if neg > 0 else "") + tok)
            if neg > 0:
                neg -= 1
        return score, hits

    @staticmethod
    def _has_macro(text):
        toks = set(re.findall(r"[a-z]+", text.lower()))
        return bool(toks & MACRO_WORDS)

    # ---- entrypoint: synchronous, microsecond-fast ------------------------
    def on_news(self, source, text):
        if not self.enabled:
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

        score, hits = self._score(headline)
        gate = MODES[self.mode]["min"]
        if abs(score) < gate:
            return                                   # not enough signal — stay quiet
        side = "long" if score > 0 else "short"

        coins = self._detect_coins(headline)
        if not coins:
            if self._has_macro(headline):
                coins = [("BTC", "BTCUSDT")]          # macro-only → trade the market leader
            else:
                return
        why = ", ".join(hits[:4])[:80]
        acted = 0
        for coin, symbol in coins:
            if acted >= MAX_PICKS_PER_HEADLINE:
                break
            if self._fire(symbol, coin, side, score, why, headline):
                acted += 1

    def _fire(self, symbol, coin, side, score, why, headline):
        now = time.time()
        if len(self.open) >= MAX_CONCURRENT:
            return False
        if any(t.symbol == symbol for t in self.open.values()):
            return False
        if now - self._cooldown.get(symbol, 0.0) < PER_COIN_COOLDOWN:
            return False
        px = self._price(symbol)
        if not px:
            return False
        tier, tp_pct, sl_pct, hold = _tier(score)
        weight = max(0.2, min(1.0, abs(score) / 5.0))
        margin = round(min(self.balance * MAX_RISK_FRAC * weight, self.balance * MAX_RISK_FRAC), 2)
        if margin < MIN_MARGIN:
            return False
        margin = min(margin, round(self.balance, 2))
        if side == "long":
            tp, sl = px * (1 + tp_pct), px * (1 - sl_pct)
        else:
            tp, sl = px * (1 - tp_pct), px * (1 + sl_pct)
        qty = margin * LEVERAGE / px
        t = Trade(id=uuid.uuid4().hex[:6], symbol=symbol, coin=coin, side=side, entry=px,
                  qty=qty, margin=margin, leverage=LEVERAGE, sl=sl, tp=tp,
                  opened_at=now, hold_min=hold, tier=tier, score=round(score, 2),
                  why=why, headline=headline)
        self.balance -= margin
        self.open[t.id] = t
        self._cooldown[symbol] = now
        self._note(f"FIRE {side.upper()} {coin} @ {px:g} · score {score:+.1f} ({tier}) · "
                   f"margin ${margin:.2f} · TP {tp_pct*100:.1f}%/SL {sl_pct*100:.1f}% · [{why}]", "open")
        self._save()
        return True

    def _exit(self, t, px, reason):
        self.open.pop(t.id, None)
        pnl = t.pnl(px)
        self.balance += t.margin + pnl
        self.history.insert(0, {"id": t.id, "coin": t.coin, "side": t.side,
                                "entry": round(t.entry, 8), "exit": round(px, 8),
                                "margin": round(t.margin, 2), "pnl": round(pnl, 2),
                                "reason": reason, "score": t.score, "tier": t.tier,
                                "headline": t.headline, "closed_at": time.time()})
        self.history = self.history
        self._note(f"CLOSE {t.side.upper()} {t.coin} @ {px:g} · {reason} · P&L ${pnl:+.2f}",
                   "win" if pnl >= 0 else "loss")
        self._save()

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
        self._cooldown = {}
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
                "news": f"{t.coin} · score {t.score:+.1f} ({t.tier}) · {t.why}",
                "pnl": round(up, 2), "pnl_pct": round(up / t.margin * 100, 2) if t.margin else 0})
        wins = sum(1 for h in self.history if h["pnl"] > 0)
        return {
            "enabled": self.enabled,
            "mode": self.mode, "modes": {k: v["label"] for k, v in MODES.items()},
            "balance": round(self.balance, 2), "equity": round(equity, 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(equity - self.start_balance, 2),
            "trades": len(self.history), "wins": wins,
            "positions": positions, "history": self.history, "log": self.log[:25],
        }
