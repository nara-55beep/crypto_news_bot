"""
ob_strategy_paper.py — in-process PAPER port of the Reddit Freqtrade "AdvancedStrategyHyperopt_4h"
(order-block / smart-money 4h strategy). No external Freqtrade runtime; pure pandas/numpy.

It faithfully reproduces the strategy's pandas logic — EMA(12/26), RSI(14), ATR(14), rolling VWAP(50),
volume spikes, order blocks with a 50-bar expiration, smart-money + order-block-test confluence
entries (long AND short), and trend-stop / RSI exits — then manages the trade with the strategy's own
exits: risk-based stop, the minimal_roi table, a trailing stop, and ATR-based dynamic leverage.

PAPER ONLY, $100, perp. Notes on faithfulness:
  - talib EMA/RSI/ATR/SMA are replicated with standard (Wilder) pandas formulas.
  - Freqtrade's ROI/stoploss/trailing are applied here as PRICE-move ratios (the most intuitive
    reading); Freqtrade-futures applies them to leveraged profit, which differs — documented so you
    know the paper numbers are an honest approximation of those exits, not bit-identical.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass

import numpy as np
import pandas as pd

import lighter_markets

START_BALANCE = 100.0
DEFAULT_TIMEFRAME = "1m"
# seconds between Lighter candle pulls per timeframe (the lighter_markets 3s cache + this throttle
# keep many OB bots from tripping the rate limit; lower TFs pull a little more often)
_FETCH_INTERVAL = {"1m": 30, "5m": 60, "15m": 90, "4h": 120}

# ---- strategy constants (the strategy's *_default values) ----
EMA_FAST = 12
EMA_SLOW = 26
RSI_LEN = 14
ATR_LEN = 14
VWAP_WIN = 50
OB_EXPIRATION = 50
IMPULSE_ATR_MULT = 1.5
OB_PENETRATION = 0.005
OB_VOLUME_MULT = 1.5
VWAP_PROXIMITY = 0.01
ENTRY_RSI_LONG_MIN, ENTRY_RSI_LONG_MAX = 40, 65
ENTRY_RSI_SHORT_MIN, ENTRY_RSI_SHORT_MAX = 35, 60
EXIT_RSI_LONG, EXIT_RSI_SHORT = 70, 30
TREND_STOP_WIN = 3

# Entry confluence: the original strategy requires the smart-money signal AND the order-block test
# on the SAME bar, which almost never happens (a breakout and a pullback-into-OB at once). LOOSE_ENTRY
# requires only ONE of them (still gated by the RSI band, EMA trend and volume) so the bots actually
# trade. Set False to restore the strict AND behavior.
LOOSE_ENTRY = True

# ---- management constants ----
MAX_RISK = 0.015                 # hp_max_risk_per_trade default (risk-based stop, price ratio)
TRAIL_POSITIVE = 0.015           # hp_trailing_stop_positive default
TRAIL_OFFSET = 0.025             # hp_trailing_stop_positive_offset default
MINIMAL_ROI = {0: 0.08, 240: 0.06, 480: 0.04, 720: 0.03, 1440: 0.02}  # minutes -> price-profit target


# ============================ indicators (talib replacements) ============================
def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s, n):
    d = s.diff()
    gain = d.clip(lower=0.0)
    loss = (-d).clip(lower=0.0)
    ag = gain.ewm(alpha=1.0 / n, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(100.0)


def _atr(df, n):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def _rolling_vwap(df, win):
    pv = (df["close"] * df["volume"]).rolling(win).sum()
    v = df["volume"].rolling(win).sum()
    return pv / v.replace(0, np.nan)


def compute(candles):
    """Port of populate_indicators + entry + exit. Returns the indicator DataFrame (or None)."""
    if len(candles) < OB_EXPIRATION + 5:
        return None
    df = pd.DataFrame([{"open": c["o"], "high": c["h"], "low": c["l"],
                        "close": c["c"], "volume": c["v"]} for c in candles]).reset_index(drop=True)

    df["ema_fast"] = _ema(df["close"], EMA_FAST)
    df["ema_slow"] = _ema(df["close"], EMA_SLOW)
    df["rsi"] = _rsi(df["close"], RSI_LEN)
    df["vwap"] = _rolling_vwap(df, VWAP_WIN)
    df["atr"] = _atr(df, ATR_LEN)

    df["volume_avg"] = df["volume"].rolling(20).mean()
    df["volume_spike"] = (df["volume"] >= df["volume"].rolling(20).max()) | (df["volume"] > df["volume_avg"] * 3.0)
    df["bullish_volume_spike_valid"] = df["volume_spike"] & (df["close"] > df["vwap"])
    df["bearish_volume_spike_valid"] = df["volume_spike"] & (df["close"] < df["vwap"])

    df["swing_high"] = df["high"].rolling(TREND_STOP_WIN).max()
    df["swing_low"] = df["low"].rolling(TREND_STOP_WIN).min()
    df["structure_break_bull"] = df["close"] > df["swing_high"].shift(1)
    df["structure_break_bear"] = df["close"] < df["swing_low"].shift(1)

    df["uptrend"] = df["ema_fast"] > df["ema_slow"]
    df["downtrend"] = df["ema_fast"] < df["ema_slow"]
    df["price_above_vwap"] = df["close"] > df["vwap"]
    df["price_below_vwap"] = df["close"] < df["vwap"]
    df["vwap_distance"] = (df["close"] - df["vwap"]).abs() / df["vwap"]

    df["bullish_impulse"] = ((df["close"] > df["open"]) &
                             ((df["high"] - df["low"]) > df["atr"] * IMPULSE_ATR_MULT) &
                             df["bullish_volume_spike_valid"])
    df["bearish_impulse"] = ((df["close"] < df["open"]) &
                             ((df["high"] - df["low"]) > df["atr"] * IMPULSE_ATR_MULT) &
                             df["bearish_volume_spike_valid"])

    ob_bull = df["bullish_impulse"] & (df["close"].shift(1) < df["open"].shift(1))
    df["bullish_ob_high"] = np.where(ob_bull, df["high"].shift(1), np.nan)
    df["bullish_ob_low"] = np.where(ob_bull, df["low"].shift(1), np.nan)
    ob_bear = df["bearish_impulse"] & (df["close"].shift(1) > df["open"].shift(1))
    df["bearish_ob_high"] = np.where(ob_bear, df["high"].shift(1), np.nan)
    df["bearish_ob_low"] = np.where(ob_bear, df["low"].shift(1), np.nan)

    # order-block expiration: carry each OB level forward up to OB_EXPIRATION bars, then drop it
    for base in ["bullish_ob_high", "bullish_ob_low", "bearish_ob_high", "bearish_ob_low"]:
        exp = f"{base}_expire"
        df[exp] = 0
        vals = df[base].to_numpy(dtype=float).copy()
        exps = np.zeros(len(df), dtype=int)
        for i in range(1, len(df)):
            cur, prev, pe = vals[i], vals[i - 1], exps[i - 1]
            if not np.isnan(cur) and np.isnan(prev):
                exps[i] = 1
            elif not np.isnan(prev):
                if np.isnan(cur):
                    vals[i], exps[i] = prev, pe + 1
                else:
                    exps[i] = 1
            else:
                exps[i] = 0
            if exps[i] > OB_EXPIRATION:
                vals[i], exps[i] = np.nan, 0
        df[base] = vals

    df["smart_money_signal"] = (df["bullish_volume_spike_valid"] & df["price_above_vwap"] &
                                df["structure_break_bull"] & df["uptrend"]).astype(int)
    df["ob_support_test"] = ((df["low"] <= df["bullish_ob_high"]) &
                             (df["close"] > (df["bullish_ob_low"] * (1 + OB_PENETRATION))) &
                             (df["volume"] > df["volume_avg"] * OB_VOLUME_MULT) &
                             df["uptrend"] & df["price_above_vwap"])
    df["smart_money_short"] = (df["bearish_volume_spike_valid"] & df["price_below_vwap"] &
                               df["structure_break_bear"] & df["downtrend"]).astype(int)
    df["ob_resistance_test"] = ((df["high"] >= df["bearish_ob_low"]) &
                                (df["close"] < (df["bearish_ob_high"] * (1 - OB_PENETRATION))) &
                                (df["volume"] > df["volume_avg"] * OB_VOLUME_MULT) &
                                df["downtrend"] & df["price_below_vwap"])

    df["trend_stop_long"] = df["low"].rolling(TREND_STOP_WIN).min().shift(1)
    df["trend_stop_short"] = df["high"].rolling(TREND_STOP_WIN).max().shift(1)

    # entry / exit signals (booleans on each bar)
    if LOOSE_ENTRY:
        long_core = (df["smart_money_signal"] > 0) | df["ob_support_test"]
        short_core = (df["smart_money_short"] > 0) | df["ob_resistance_test"]
    else:
        long_core = (df["smart_money_signal"] > 0) & df["ob_support_test"]
        short_core = (df["smart_money_short"] > 0) & df["ob_resistance_test"]
    df["enter_long"] = (long_core &
                        (df["rsi"] > ENTRY_RSI_LONG_MIN) & (df["rsi"] < ENTRY_RSI_LONG_MAX) &
                        (df["close"] > df["ema_slow"]) & (df["volume"] > 0))
    df["enter_short"] = (short_core &
                         (df["rsi"] < ENTRY_RSI_SHORT_MAX) & (df["rsi"] > ENTRY_RSI_SHORT_MIN) &
                         (df["close"] < df["ema_slow"]) & (df["volume"] > 0))
    df["exit_long"] = (((df["close"] < df["trend_stop_long"]) | (df["rsi"] > EXIT_RSI_LONG)) & (df["volume"] > 0))
    df["exit_short"] = (((df["close"] > df["trend_stop_short"]) | (df["rsi"] < EXIT_RSI_SHORT)) & (df["volume"] > 0))
    return df


def dynamic_leverage(atr, close, max_leverage=20.0):
    """Port of the strategy's leverage(): tier the base 20x down by ATR%, floor 5x."""
    if not close or close <= 0 or atr is None or np.isnan(atr) or atr <= 0:
        return min(10.0, max_leverage)
    atr_pct = (atr / close) * 100.0
    base = 20.0
    if atr_pct > 5.0:
        lev = base * 0.5
    elif atr_pct > 3.0:
        lev = base * 0.7
    elif atr_pct > 2.0:
        lev = base * 0.85
    else:
        lev = base * 1.0
    return min(max(5.0, lev), max_leverage)


# ============================ position / bot ============================
@dataclass
class OBPos:
    id: str
    side: str
    entry: float
    qty: float
    stake: float
    leverage: float
    opened_at: float
    peak: float = 0.0
    trail_on: bool = False
    note: str = ""

    def unrealized(self, mark):
        if not mark:
            return 0.0
        return self.qty * (mark - self.entry) if self.side == "long" else self.qty * (self.entry - mark)


class OBStrategyBot:
    def __init__(self, market, timeframe=DEFAULT_TIMEFRAME):
        self.market = market
        self.timeframe = timeframe
        self.strategy = f"ob_smart_money_{timeframe}"
        self.label = f"OB / Smart-Money {timeframe} (Reddit Freqtrade port)"
        self._strategies = {self.strategy: self.label}
        self.enabled = False
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos: "OBPos | None" = None
        self.history: list[dict] = []
        self.log: list[dict] = []

        self._cs = []
        self._last_bar_t = 0
        self._row = {}                  # latest indicator row snapshot (for the panel + exits)
        self._err = ""
        self._t_fetch = 0.0
        self._rl_until = 0.0
        self._load()

    # ---- persistence ----
    def _path(self):
        return os.path.join("data", f"ob_strategy_{self.timeframe}_state.json")

    def _save(self):
        try:
            os.makedirs("data", exist_ok=True)
            with open(self._path(), "w") as f:
                json.dump({"enabled": self.enabled, "balance": self.balance,
                           "start_balance": self.start_balance,
                           "history": self.history, "log": self.log[:60]}, f)
        except Exception:
            pass

    def _load(self):
        try:
            with open(self._path()) as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", False))
            self.balance = float(d.get("balance", START_BALANCE))
            self.start_balance = float(d.get("start_balance", START_BALANCE))
            self.history = d.get("history", [])
            self.log = d.get("log", [])
            if not self.history:
                self.balance = START_BALANCE
                self.start_balance = START_BALANCE
            else:
                # When FLAT, balance must equal start + realized PnL. A position locks the full margin
                # (balance -> ~0); if saved/restarted mid-trade the open position isn't persisted, so
                # the saved balance is corrupt and would show a phantom loss. Rebuild it from history.
                self.balance = round(self.start_balance + sum(h.get("pnl", 0.0) for h in self.history), 2)
        except Exception:
            pass

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "msg": msg, "kind": kind})
        self.log = self.log[:60]

    def _btc(self):
        try:
            return self.market.price("BTCUSDT")
        except Exception:
            return None

    def equity(self):
        eq = self.balance
        if self.pos is not None:
            eq += self.pos.unrealized(self._btc() or self.pos.entry)
        return eq

    async def _refresh(self, now):
        interval = _FETCH_INTERVAL.get(self.timeframe, 60)
        if now < self._rl_until or (now - self._t_fetch) < interval:
            return
        try:
            self._cs = await asyncio.to_thread(lighter_markets.candles, "BTC", self.timeframe, 300)
            self._t_fetch = now
            self._err = ""
        except Exception as e:
            msg = str(e)
            if "RateLimit" in type(e).__name__ or "23000" in msg or "Too Many" in msg:
                self._rl_until = now + 60.0
                self._err = "rate-limited by Lighter — backing off 60s"
            else:
                self._err = f"{type(e).__name__}: {str(e)[:120]}"

    # ---- trade lifecycle ----
    def _open(self, side, price, lev, note):
        stake = round(self.balance, 2)
        if stake < 1 or not price:
            return
        qty = stake * lev / price
        self.balance -= stake
        self.pos = OBPos(id=uuid.uuid4().hex[:6], side=side, entry=price, qty=qty, stake=stake,
                         leverage=lev, opened_at=time.time(), peak=price, note=note)
        self._note(f"{side.upper()} @ {price:,.1f} · {note} · margin ${stake:.2f} {lev:.0f}x", "open")
        self._save()

    def _close(self, price, reason):
        p = self.pos
        pnl = p.unrealized(price)
        self.balance += p.stake + pnl
        rec = {"side": p.side, "entry": round(p.entry, 1), "exit": round(price, 1),
               "qty": round(p.qty, 6), "pnl": round(pnl, 2),
               "pnl_pct": round(pnl / p.stake * 100, 2) if p.stake else 0.0,
               "reason": reason, "opened_at": p.opened_at, "closed_at": time.time()}
        self.history.insert(0, rec)
        self.history = self.history
        self._note(f"CLOSE {p.side.upper()} @ {price:,.1f} · {reason} · P&L ${pnl:+.2f} "
                   f"({rec['pnl_pct']:+.2f}%)", "win" if pnl >= 0 else "loss")
        self.pos = None
        self._save()

    def _manage(self, px):
        """Price-based exits between bars: liquidation, risk stop, ROI table, trailing stop."""
        p = self.pos
        if p is None or not px:
            return
        profit = (px - p.entry) / p.entry if p.side == "long" else (p.entry - px) / p.entry
        held_min = (time.time() - p.opened_at) / 60.0

        if p.unrealized(px) <= -p.stake * 0.98:
            self._close(px, "liquidation"); return
        if profit <= -MAX_RISK:
            self._close(px, f"risk_stop (-{MAX_RISK*100:.1f}%)"); return
        roi = None
        for m in sorted(MINIMAL_ROI):
            if held_min >= m:
                roi = MINIMAL_ROI[m]
        if roi is not None and profit >= roi:
            self._close(px, f"roi {roi*100:.0f}%"); return
        if not p.trail_on and profit >= TRAIL_OFFSET:
            p.trail_on = True
        if p.trail_on:
            if p.side == "long":
                p.peak = max(p.peak, px)
                if px <= p.peak * (1 - TRAIL_POSITIVE):
                    self._close(px, "trailing_stop"); return
            else:
                p.peak = min(p.peak, px) if p.peak else px
                if px >= p.peak * (1 + TRAIL_POSITIVE):
                    self._close(px, "trailing_stop"); return

    def _bar_exit(self, row):
        """Bar-based exit signals from the strategy (trend stop / RSI)."""
        p = self.pos
        if p is None:
            return
        if p.side == "long" and bool(row.get("exit_long")):
            self._close(self._btc() or p.entry, "exit_signal (trend/RSI)")
        elif p.side == "short" and bool(row.get("exit_short")):
            self._close(self._btc() or p.entry, "exit_signal (trend/RSI)")

    # ---- main loop ----
    async def manage_loop(self):
        await asyncio.sleep(6.0)
        while True:
            await asyncio.sleep(15.0)
            try:
                now = time.time()
                await self._refresh(now)
                px = self._btc()
                if self.pos is not None and px:
                    self._manage(px)
                if len(self._cs) < 2:
                    continue
                # Act on CLOSED bars only. The feed's last candle is the still-forming current bar;
                # the strategy's volume-spike / impulse / structure-break signals need a COMPLETED
                # bar, so we drop it and evaluate the bar that just closed (like Freqtrade's
                # process_only_new_candles). Evaluating the forming bar was why nothing ever triggered.
                closed = self._cs[:-1]
                newest = closed[-1]["t"]
                if newest == self._last_bar_t:
                    continue
                self._last_bar_t = newest
                df = compute(closed)
                if df is None or len(df) < 2:
                    continue
                row = df.iloc[-1].to_dict()
                self._row = {k: (None if (isinstance(v, float) and np.isnan(v)) else v)
                             for k, v in row.items()
                             if k in ("close", "rsi", "atr", "vwap", "ema_fast", "ema_slow",
                                      "uptrend", "downtrend", "enter_long", "enter_short")}
                if self.pos is not None:
                    self._bar_exit(row)
                elif self.enabled:
                    price = self._btc() or float(row["close"])
                    lev = dynamic_leverage(float(row["atr"]), float(row["close"]))
                    if bool(row.get("enter_long")):
                        self._open("long", price, lev, "OB long: smart-money + OB support + RSI")
                    elif bool(row.get("enter_short")):
                        self._open("short", price, lev, "OB short: smart-money + OB resist + RSI")
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:100]}", "error")

    # ---- controls / state ----
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED — paper-trading the OB / smart-money 4h strategy" if self.enabled
                   else "bot PAUSED — no new entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_strategy(self, name):
        if name and name != self.strategy:
            return {"ok": False, "error": f"unknown strategy '{name}'"}
        return {"ok": True, "strategy": self.strategy, "strategy_label": self.label}

    def reset(self):
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos = None
        self.history = []
        self.log = []
        self._note("bot reset (paper account back to $%.0f)" % START_BALANCE)
        self._save()
        return {"ok": True}

    def state(self, **_):
        px = self._btc()
        r = self._row
        positions = []
        if self.pos is not None:
            p = self.pos
            up = p.unrealized(px) if px else 0.0
            positions.append({
                "id": p.id, "side": p.side, "entry": round(p.entry, 1),
                "qty": round(p.qty, 6), "leverage": round(p.leverage, 0),
                "liq": round(p.entry * (1 - 1.0 / p.leverage), 1) if p.side == "long"
                       else round(p.entry * (1 + 1.0 / p.leverage), 1),
                "pnl": round(up, 2), "pnl_pct": round(up / p.stake * 100, 2) if p.stake else 0.0,
                "news": p.note + (" · trailing" if p.trail_on else ""),
            })
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        trend = "up" if r.get("uptrend") else ("down" if r.get("downtrend") else None)
        return {
            "name": f"OB / Smart-Money {self.timeframe}",
            "enabled": self.enabled,
            "running": True,
            "strategy": self.strategy,
            "strategy_label": self.label,
            "strategies": self._strategies,
            "timeframe": self.timeframe,
            "market": "perp",
            "trend": trend,
            "rsi": round(r["rsi"], 1) if r.get("rsi") is not None else None,
            "vwap": round(r["vwap"], 1) if r.get("vwap") is not None else None,
            "price": round(px, 2) if px else None,
            "balance": round(self.balance, 2),
            "equity": round(self.equity(), 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(self.equity() - self.start_balance, 2),
            "trades": len(self.history),
            "wins": wins,
            "data_error": self._err,
            "positions": positions,
            "history": self.history,
            "log": self.log[:25],
        }
