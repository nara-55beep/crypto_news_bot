"""
================================================================================
 tv_strategies_paper.py  —  TRADINGVIEW STRATEGY PACK  (paper)
================================================================================
A pack of well-known TradingView-style strategies, each run as its OWN paper
account on BTC: $100 start, ALL-IN 20x every trade, BTC/USDT daily-ish data.

Each strategy is implemented from its STANDARD, PUBLIC indicator logic (Bollinger,
RSI, MACD, SMA200, SuperTrend, PMax, Hull MA, Awesome Oscillator, Stoch-RSI, ATR,
Squeeze Momentum, market-structure) — NOT a copy of any author's Pine source.
Where a script is proprietary/complex (3Commas, SMC, PMax, "Flawless Victory ML"),
this is a faithful standard implementation of the documented rules, not the original.

Strategies:
  1  Bollinger + RSI Double (ChartArt)
  2  MACD + SMA200 (ChartArt)
  3  SuperTrend (ATR 10 x3)
  4  MACD bull-cross + RSI was oversold 5 bars ago (long only)
  5  PMax (MA over SuperTrend)
  6  3Commas-style (EMA fast/slow cross)
  7  Hull Suite (HMA trend)
  8  AO + Stoch-RSI + ATR (SerdarYILMAZ)
  9  Flawless Victory (15m BTC: BB + RSI + MFI)
 10  Smart Money Concepts (structure break / BOS)
 11  Squeeze Momentum (LazyBear)
 12  MACD multi-timeframe (4h MACD histogram)

Each signal fn returns the DESIRED position for the last CLOSED bar: "long",
"short", or None (flat). The engine flips/exits when that changes, with a 20x
liquidation backstop so a paper account can't go below $0. All paper, real data.
================================================================================
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid

import numpy as np
import pandas as pd

import config

STATE_PATH = os.path.join(config.DATA_DIR, "tv_strategies_state.json")

SYMBOL    = "BTC/USDT"
START_BAL = 100.0
LEVERAGE  = 20.0          # all-in 20x on every trade
FEE       = 0.0           # ZERO — Lighter (the real venue) has no trading fees, so paper must not fake one
POLL_SEC  = 30           # 5m chart → check twice a minute
FETCH     = 500           # 5m bars per timeframe (need 200 for SMA200 + warmup)


# --------------------------------------------------------------------------- #
#  indicators (pandas/numpy; standard public formulas)
# --------------------------------------------------------------------------- #
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def sma(s, n): return s.rolling(n).mean()

def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    ag = up.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    al = dn.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    return (100 - 100/(1 + ag/al.replace(0, np.nan))).fillna(50)

def macd(s, f=12, sl=26, sig=9):
    line = ema(s, f) - ema(s, sl); signal = ema(line, sig)
    return line, signal, line - signal

def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()

def wma(s, n):
    w = np.arange(1, n+1)
    return s.rolling(n).apply(lambda x: np.dot(x, w)/w.sum(), raw=True)

def hma(s, n):
    return wma(2*wma(s, n//2) - wma(s, n), int(math.sqrt(n)))

def stochrsi(s, n=14, k=3, d=3):
    r = rsi(s, n); lo = r.rolling(n).min(); hi = r.rolling(n).max()
    st = (100*(r-lo)/(hi-lo).replace(0, np.nan)).fillna(50)
    kk = st.rolling(k).mean(); dd = kk.rolling(d).mean()
    return kk, dd

def mfi(df, n=14):
    tp = (df["high"]+df["low"]+df["close"])/3; mf = tp*df["volume"]
    pos = mf.where(tp > tp.shift(), 0.0); neg = mf.where(tp < tp.shift(), 0.0)
    mr = pos.rolling(n).sum()/neg.rolling(n).sum().replace(0, np.nan)
    return (100 - 100/(1+mr)).fillna(50)

def supertrend(df, period=10, mult=3.0):
    """Return trend direction series: +1 uptrend (price above), -1 downtrend."""
    a = atr(df, period); hl2 = (df["high"]+df["low"])/2
    up = hl2 - mult*a; dn = hl2 + mult*a
    n = len(df); cl = df["close"].values
    upv, dnv = up.values, dn.values
    fu = np.full(n, np.nan); fd = np.full(n, np.nan); dir_ = np.ones(n)
    for i in range(1, n):
        fu[i] = max(upv[i], fu[i-1]) if cl[i-1] > (fu[i-1] if fu[i-1] == fu[i-1] else -1e18) else upv[i]
        fd[i] = min(dnv[i], fd[i-1]) if cl[i-1] < (fd[i-1] if fd[i-1] == fd[i-1] else 1e18) else dnv[i]
        if cl[i] > (fd[i-1] if fd[i-1] == fd[i-1] else 1e18):
            dir_[i] = 1
        elif cl[i] < (fu[i-1] if fu[i-1] == fu[i-1] else -1e18):
            dir_[i] = -1
        else:
            dir_[i] = dir_[i-1]
    return pd.Series(dir_, index=df.index)

def pmax(df, period=10, mult=3.0, malen=10):
    """PMax: an EMA trailed by an ATR band; returns (ma, pmax_line)."""
    a = atr(df, period); ma = ema(df["close"], malen)
    lb = ma - mult*a; ub = ma + mult*a
    n = len(df); mav = ma.values
    flb = np.full(n, np.nan); fub = np.full(n, np.nan); pm = np.full(n, np.nan); d = np.ones(n)
    for i in range(1, n):
        flb[i] = max(lb.values[i], flb[i-1]) if mav[i-1] > (flb[i-1] if flb[i-1] == flb[i-1] else -1e18) else lb.values[i]
        fub[i] = min(ub.values[i], fub[i-1]) if mav[i-1] < (fub[i-1] if fub[i-1] == fub[i-1] else 1e18) else ub.values[i]
        if mav[i] > (fub[i-1] if fub[i-1] == fub[i-1] else 1e18):
            d[i] = 1
        elif mav[i] < (flb[i-1] if flb[i-1] == flb[i-1] else -1e18):
            d[i] = -1
        else:
            d[i] = d[i-1]
        pm[i] = flb[i] if d[i] == 1 else fub[i]
    return ma, pd.Series(pm, index=df.index)


def prep(df):
    """Add the full indicator bundle once per timeframe."""
    d = df.copy()
    d["sma200"] = sma(d["close"], 200)
    d["rsi"] = rsi(d["close"], 14)
    d["bb_mid"] = sma(d["close"], 20); sd = d["close"].rolling(20).std()
    d["bb_up"] = d["bb_mid"] + 2*sd; d["bb_lo"] = d["bb_mid"] - 2*sd
    d["macd"], d["macd_sig"], d["macd_hist"] = macd(d["close"])
    d["atr"] = atr(d, 14)
    d["st_dir"] = supertrend(d, 10, 3.0)
    d["ma_p"], d["pmax"] = pmax(d, 10, 3.0, 10)
    d["ema_fast"] = ema(d["close"], 9); d["ema_slow"] = ema(d["close"], 21)
    d["hma"] = hma(d["close"], 55)
    med = (d["high"]+d["low"])/2; d["ao"] = sma(med, 5) - sma(med, 34)
    d["srsi_k"], d["srsi_d"] = stochrsi(d["close"])
    d["mfi"] = mfi(d, 14)
    # squeeze momentum (LazyBear): momentum sign of detrended source
    hh = d["high"].rolling(20).max(); ll = d["low"].rolling(20).min()
    d["sqz_mom"] = d["close"] - ((hh+ll)/2 + sma(d["close"], 20))/2
    rng = (d["high"]-d["low"]).rolling(20).mean()
    kc_u = d["bb_mid"] + 1.5*rng; kc_l = d["bb_mid"] - 1.5*rng
    d["sqz_on"] = (d["bb_lo"] > kc_l) & (d["bb_up"] < kc_u)
    d["swing_hi"] = d["high"].rolling(20).max().shift(1)
    d["swing_lo"] = d["low"].rolling(20).min().shift(1)
    return d


def _x(a):  # safe float
    try:
        return float(a)
    except Exception:
        return float("nan")


# --------------------------------------------------------------------------- #
#  strategy signal functions  ->  "long" | "short" | None  (for last closed bar)
# --------------------------------------------------------------------------- #
def s_bb_rsi(df, cur):
    r = df.iloc[-2]
    if pd.isna(r["bb_lo"]) or pd.isna(r["rsi"]):
        return cur
    if r["close"] < r["bb_lo"] and r["rsi"] < 30:
        return "long"
    if r["close"] > r["bb_up"] and r["rsi"] > 70:
        return "short"
    if cur == "long" and r["close"] >= r["bb_mid"]:
        return None
    if cur == "short" and r["close"] <= r["bb_mid"]:
        return None
    return cur

def s_macd_sma200(df, cur):
    r = df.iloc[-2]
    if pd.isna(r["sma200"]) or pd.isna(r["macd"]):
        return cur
    if r["macd"] > r["macd_sig"] and r["close"] > r["sma200"]:
        return "long"
    if r["macd"] < r["macd_sig"] and r["close"] < r["sma200"]:
        return "short"
    return cur

def s_supertrend(df, cur):
    r = df.iloc[-2]
    if pd.isna(r["st_dir"]):
        return cur
    return "long" if r["st_dir"] > 0 else "short"

def s_macd_rsi5(df, cur):
    r, p = df.iloc[-2], df.iloc[-3]
    r5 = df.iloc[-7] if len(df) >= 7 else None
    if r5 is None or pd.isna(r["macd"]):
        return cur
    bull_cross = p["macd"] <= p["macd_sig"] and r["macd"] > r["macd_sig"]
    if bull_cross and r5["rsi"] < 30:                 # long-only
        return "long"
    if cur == "long" and (p["macd"] >= p["macd_sig"] and r["macd"] < r["macd_sig"]):
        return None
    return cur

def s_pmax(df, cur):
    r = df.iloc[-2]
    if pd.isna(r["pmax"]) or pd.isna(r["ma_p"]):
        return cur
    return "long" if r["ma_p"] > r["pmax"] else "short"

def s_3commas(df, cur):                                # EMA 9/21 cross (signal-bot style)
    r = df.iloc[-2]
    if pd.isna(r["ema_slow"]):
        return cur
    return "long" if r["ema_fast"] > r["ema_slow"] else "short"

def s_hull(df, cur):
    if len(df) < 4 or pd.isna(df.iloc[-2]["hma"]) or pd.isna(df.iloc[-4]["hma"]):
        return cur
    return "long" if df.iloc[-2]["hma"] > df.iloc[-4]["hma"] else "short"

def s_ao_srsi(df, cur):
    r, p = df.iloc[-2], df.iloc[-3]
    if pd.isna(r["ao"]) or pd.isna(r["srsi_k"]):
        return cur
    k_up = p["srsi_k"] <= p["srsi_d"] and r["srsi_k"] > r["srsi_d"]
    k_dn = p["srsi_k"] >= p["srsi_d"] and r["srsi_k"] < r["srsi_d"]
    if r["ao"] > 0 and k_up:
        return "long"
    if r["ao"] < 0 and k_dn:
        return "short"
    if cur == "long" and r["ao"] < 0:
        return None
    if cur == "short" and r["ao"] > 0:
        return None
    return cur

def s_flawless(df, cur):                               # 15m BTC: BB + RSI + MFI
    r = df.iloc[-2]
    if pd.isna(r["bb_lo"]) or pd.isna(r["mfi"]):
        return cur
    if r["close"] < r["bb_lo"] and r["rsi"] > 42 and r["mfi"] < 60:
        return "long"
    if r["close"] > r["bb_up"] and r["rsi"] > 70:
        return None                                    # Flawless is long-biased: exit, don't short
    if cur == "long" and r["close"] >= r["bb_mid"] and r["rsi"] > 70:
        return None
    return cur

def s_smc(df, cur):                                    # simplified market-structure break (BOS)
    r = df.iloc[-2]
    if pd.isna(r["swing_hi"]) or pd.isna(r["swing_lo"]):
        return cur
    if r["close"] > r["swing_hi"]:
        return "long"
    if r["close"] < r["swing_lo"]:
        return "short"
    return cur

def s_squeeze(df, cur):
    r = df.iloc[-2]
    if pd.isna(r["sqz_mom"]):
        return cur
    if bool(r["sqz_on"]):                              # inside the squeeze -> wait for release
        return cur
    return "long" if r["sqz_mom"] > 0 else "short"

def s_macd_mtf(df, cur):                               # MACD histogram on this (higher) timeframe
    r = df.iloc[-2]
    if pd.isna(r["macd_hist"]):
        return cur
    return "long" if r["macd_hist"] > 0 else "short"


# key, display name, timeframe, signal fn
# All strategies run on the 5-minute chart (per user request).
STRATS = [
    ("bb_rsi",     "Bollinger + RSI Double (ChartArt)",        "5m", s_bb_rsi),
    ("macd_sma",   "MACD + SMA200 (ChartArt)",                 "5m", s_macd_sma200),
    ("supertrend", "SuperTrend (ATR 10×3)",                    "5m", s_supertrend),
    ("macd_rsi5",  "MACD bull-cross + RSI oversold 5 bars ago","5m", s_macd_rsi5),
    ("pmax",       "PMax (MA over SuperTrend)",                "5m", s_pmax),
    ("threecommas","3Commas-style (EMA 9/21 cross)",           "5m", s_3commas),
    ("hull",       "Hull Suite (HMA trend)",                   "5m", s_hull),
    ("ao_srsi",    "AO + Stoch-RSI + ATR (SerdarYILMAZ)",      "5m", s_ao_srsi),
    ("flawless",   "Flawless Victory (BB+RSI+MFI)",            "5m", s_flawless),
    ("smc",        "Smart Money Concepts (structure break)",   "5m", s_smc),
    ("squeeze",    "Squeeze Momentum (LazyBear)",              "5m", s_squeeze),
    ("macd_mtf",   "MACD multi-timeframe",                     "5m", s_macd_mtf),
]


class TVStrategiesBot:
    def __init__(self, market=None):
        self.market = market
        self._ex = None
        self.acct: dict[str, dict] = {}
        for k, name, tf, _ in STRATS:
            self.acct[k] = {"enabled": True, "balance": START_BAL, "pos": None,
                            "history": [], "log": [], "signal": None}
        self.prices = {}            # tf -> last price (BTC); also "_btc" current
        self.btc = 0.0
        self.status = "starting…"
        self._load()

    def attach(self, market):
        self.market = market

    # ---- persistence ----
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({"acct": {k: {kk: a[kk] for kk in ("enabled", "balance", "pos", "history", "log")}
                                    for k, a in self.acct.items()}}, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            d = json.load(open(STATE_PATH))
            for k, a in (d.get("acct") or {}).items():
                if k in self.acct:
                    self.acct[k].update({kk: a.get(kk, self.acct[k][kk])
                                         for kk in ("enabled", "balance", "pos", "history", "log")})
            print(f"[tvstrats] restored {len(self.acct)} strategy accounts")
        except Exception:
            pass

    def _note(self, k, msg, kind="info"):
        a = self.acct[k]
        a["log"].insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        a["log"] = a["log"][:40]
        print(f"[tvstrats:{k}] {msg}")

    # ---- data ----
    def _ensure_ex(self):
        if self._ex is None:
            import ccxt
            self._ex = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
        return self._ex

    def _fetch(self, tf):
        rows = self._ensure_ex().fetch_ohlcv(SYMBOL, tf, limit=FETCH)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).astype(float)
        return df[["open", "high", "low", "close", "volume"]]

    # ---- loop ----
    async def manage_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL_SEC)
            try:
                tfs = sorted({tf for _, _, tf, _ in STRATS})
                data = {}
                for tf in tfs:
                    raw = await loop.run_in_executor(None, self._fetch, tf)
                    data[tf] = prep(raw)
                self.btc = float(data[tfs[0]]["close"].iloc[-1]) if tfs else 0.0
                # current BTC price (forming bar of each tf shares ~same last price)
                for k, name, tf, fn in STRATS:
                    df = data.get(tf)
                    if df is None or len(df) < 60:
                        continue
                    price = float(df["close"].iloc[-1])
                    self._run_one(k, fn, df, price)
                self.status = f"BTC ${self.btc:,.0f} · {sum(1 for a in self.acct.values() if a['pos'])} positions open"
                self._save()
            except Exception as e:
                self.status = f"loop error: {type(e).__name__}: {str(e)[:70]}"

    def _run_one(self, k, fn, df, price):
        a = self.acct[k]
        cur = a["pos"]["side"] if a["pos"] else None
        # 1) liquidation backstop first
        if a["pos"]:
            p = a["pos"]; long = p["side"] == "long"
            if (long and price <= p["liq"]) or ((not long) and price >= p["liq"]):
                self._close(k, price, "liquidation"); cur = None
        # 2) strategy signal
        try:
            desired = fn(df, cur)
        except Exception as e:
            self._note(k, f"signal error: {type(e).__name__}", "error"); return
        a["signal"] = desired
        if a["pos"] and desired != cur:                 # flip or exit
            self._close(k, price, "signal_exit" if desired is None else f"flip_to_{desired}")
            cur = None
        if (not a["pos"]) and a["enabled"] and desired in ("long", "short"):
            self._open(k, price, desired)

    # ---- trade exec (all-in 20x) ----
    def _equity(self, k):
        a = self.acct[k]; eq = a["balance"]
        if a["pos"] and self.btc:
            p = a["pos"]; long = p["side"] == "long"
            eq += p["qty"] * ((self.btc - p["entry"]) if long else (p["entry"] - self.btc))
        return eq

    def _open(self, k, price, side):
        a = self.acct[k]; eq = self._equity(k)
        if eq <= 0:
            return
        notional = eq * LEVERAGE
        qty = notional / price
        a["balance"] -= notional * FEE
        liq = price * (1 - 1/LEVERAGE) if side == "long" else price * (1 + 1/LEVERAGE)
        a["pos"] = {"side": side, "entry": price, "qty": qty, "notional": round(notional, 2),
                    "margin": round(eq, 2), "liq": liq, "opened_at": time.time()}
        self._note(k, f"OPEN {side.upper()} @ ${price:,.1f} · ${notional:,.0f} (20x on ${eq:,.2f})", "open")

    def _close(self, k, price, reason):
        a = self.acct[k]; p = a["pos"]
        if not p:
            return
        long = p["side"] == "long"
        pnl = p["qty"] * ((price - p["entry"]) if long else (p["entry"] - price)) - p["notional"] * FEE
        pnl = max(pnl, -p["margin"])                    # isolated 20x margin
        a["balance"] = max(0.0, a["balance"] + pnl)
        a["history"].insert(0, {"side": p["side"], "entry": round(p["entry"], 1), "exit": round(price, 1),
                                "pnl": round(pnl, 2), "reason": reason,
                                "opened_at": p["opened_at"], "closed_at": time.time()})
        a["history"] = a["history"]
        a["pos"] = None
        self._note(k, f"CLOSE {p['side'].upper()} @ ${price:,.1f} · {reason} · P&L ${pnl:+.2f}",
                   "win" if pnl >= 0 else "loss")

    # ---- controls ----
    def set_enabled(self, key, on):
        if key in self.acct:
            self.acct[key]["enabled"] = bool(on)
            self._note(key, f"{'ENABLED' if on else 'PAUSED'}")
            self._save()
        return {"ok": True}

    def reset(self, key=None):
        keys = [key] if key else list(self.acct.keys())
        for k in keys:
            if k in self.acct:
                self.acct[k] = {"enabled": True, "balance": START_BAL, "pos": None,
                                "history": [], "log": [], "signal": None}
        self._save()
        return {"ok": True}

    # ---- snapshot ----
    def state(self):
        out = []
        for k, name, tf, _ in STRATS:
            a = self.acct[k]; eq = self._equity(k)
            pos = None
            if a["pos"]:
                p = a["pos"]; long = p["side"] == "long"
                up = p["qty"] * ((self.btc - p["entry"]) if long else (p["entry"] - self.btc)) if self.btc else 0.0
                pos = {"side": p["side"], "entry": round(p["entry"], 1), "notional": p["notional"],
                       "liq": round(p["liq"], 1), "mark": round(self.btc, 1) if self.btc else None,
                       "upnl": round(up, 2), "opened_at": p.get("opened_at")}
            wins = sum(1 for h in a["history"] if h["pnl"] > 0)
            out.append({
                "key": k, "name": name, "tf": tf, "enabled": a["enabled"], "signal": a["signal"],
                "balance": round(a["balance"], 2), "equity": round(eq, 2),
                "pnl": round(eq - START_BAL, 2), "pnl_pct": round((eq/START_BAL - 1)*100, 2),
                "trades": len(a["history"]), "wins": wins, "position": pos,
                "last": (a["log"][0]["msg"] if a["log"] else ""),
                "log": a["log"][:6],
                "history": a["history"],
            })
        return {"status": self.status, "btc": round(self.btc, 1) if self.btc else None,
                "start_balance": START_BAL, "leverage": LEVERAGE, "strategies": out}
