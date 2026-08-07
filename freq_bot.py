"""
freq_bot.py — a Freqtrade-STYLE paper bot, fully in-process (no external code, no install, no keys).

This is NOT the Freqtrade application. It is a small, self-contained PAPER bot that reproduces the
*shape* of a classic Freqtrade spot strategy so you can run it on the /paper page next to the others:

  - one timeframe (5m), long-only spot (1x, no leverage)
  - indicator stack: RSI(14), EMA(9)/EMA(21) trend, Bollinger Bands(20, 2σ)
  - ENTRY (buy): an oversold mean-reversion bounce — RSI crosses up through BUY_RSI while price is at
    / below the lower Bollinger band, with the fast EMA not in a hard downtrend (Freqtrade "guards")
  - EXIT (sell): Freqtrade's three classic exits — a `minimal_roi` time/ROI table, a hard `stoploss`,
    a `trailing_stop`, plus an RSI-overbought sell signal

Everything is simulated against Lighter's own candles (same gentle throttling as the ICT bot so we
never trip the rate limit). It never places a real order.
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass

import lighter_markets

# ---- account ----
START_BALANCE = 100.0          # paper account (USD)
TIMEFRAME     = "5m"
LEVERAGE      = 20             # PERP leverage: margin x this = position notional. NOT spot.

# ---- strategy params (classic Freqtrade defaults / sample-strategy flavor) ----
RSI_LEN     = 14
BUY_RSI     = 35.0             # LONG entry: RSI crossing up through this (oversold bounce)
SELL_RSI    = 70.0            # exit a winner on RSI overbought (long) / oversold mirror (short)
SHORT_RSI   = 65.0            # SHORT entry: RSI crossing DOWN through this (overbought rejection) — mirror of BUY_RSI
EMA_FAST    = 9
EMA_SLOW    = 21
BB_LEN      = 20
BB_STD      = 2.0
# minimal_roi: {minutes_held: take-profit fraction}. Earliest threshold <= elapsed that the
# profit beats triggers an ROI exit (exactly how Freqtrade's minimal_roi table works).
MINIMAL_ROI = {0: 0.04, 30: 0.02, 60: 0.01, 120: 0.0}
STOPLOSS    = -0.03           # hard stop at -3% PRICE. At 20x that is ~-60% of MARGIN and fires
                              #   BEFORE the ~-5% price move that would liquidate. (Spot used -10%.)
TRAIL_OFFSET   = 0.02         # arm the trailing stop once +2% in profit (`trailing_stop_positive_offset`)
TRAIL_POSITIVE = 0.01         # then trail 1% behind the peak (`trailing_stop_positive`)

STRATEGIES = {"freq_sample": "Freqtrade-style (RSI + Bollinger + ROI/stop/trail)"}

# Improved copy: exits are based on P&L as a fraction of margin, so they scale
# correctly if leverage changes. Example: -10% margin risk means a -0.25% BTC
# move at 40x, -0.5% at 20x, and -0.2% at 50x.
IMPROVED_STOP_MARGIN = -0.10
IMPROVED_BE_TRIGGER = 0.05
IMPROVED_TRAIL_TRIGGER = 0.10
IMPROVED_TRAIL_TIERS = (
    (0.75, 0.92),
    (0.40, 0.875),
    (0.25, 0.80),
    (0.10, 0.65),
)


# ============================ indicators ============================
def ema(vals, length):
    if not vals:
        return None
    k = 2.0 / (length + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, length=RSI_LEN):
    if len(closes) < length + 1:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - length, len(closes)):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_g = gains / length
    avg_l = losses / length
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger(closes, length=BB_LEN, mult=BB_STD):
    if len(closes) < length:
        return None, None, None
    window = closes[-length:]
    mid = sum(window) / length
    var = sum((c - mid) ** 2 for c in window) / length
    sd = var ** 0.5
    return mid - mult * sd, mid, mid + mult * sd


# ============================ position ============================
@dataclass
class FreqPos:
    id: str
    side: str               # "long" or "short"
    entry: float
    qty: float
    stake: float
    opened_at: float
    peak: float = 0.0
    trail_on: bool = False
    note: str = ""
    best_pnl: float = 0.0
    stop_pnl: float = 0.0

    def unrealized(self, mark):
        if not mark:
            return 0.0
        if self.side == "short":
            return self.qty * (self.entry - mark)
        return self.qty * (mark - self.entry)


# ============================ the bot ============================
class FreqBot:
    def __init__(self, market):
        self.market = market
        self.enabled = False
        self.strategy = "freq_sample"
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos: "FreqPos | None" = None
        self.history: list[dict] = []
        self.log: list[dict] = []

        self._cs: list = []                 # 5m candles
        self._last_bar_t = 0
        self._prev_rsi = None               # for the RSI cross-up detection
        self._ind = {}                      # last indicator readout (for the panel)
        self._err = ""
        self._t_fetch = 0.0
        self._rl_until = 0.0

        self._load()

    # ---- persistence ----
    def _path(self):
        return os.path.join("data", "freq_bot_state.json")

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
            if not self.history:                # honor the configured starting balance until traded
                self.balance = START_BALANCE
                self.start_balance = START_BALANCE
            else:
                # When FLAT, balance must equal start + realized PnL. A position locks the full margin
                # (balance -> ~0); if the bot was saved/restarted mid-trade the open position isn't
                # persisted, so the saved balance is corrupt and would show a phantom -$100. Rebuild
                # balance from the closed-trade history (the correct invariant) to self-heal it.
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
            eq += self.pos.stake + self.pos.unrealized(self._btc() or self.pos.entry)
        return eq

    # ---- candles (gentle, rate-limit-safe) ----
    async def _refresh(self, now):
        if now < self._rl_until or (now - self._t_fetch) < 60:
            return
        try:
            self._cs = await asyncio.to_thread(lighter_markets.candles, "BTC", TIMEFRAME, 200)
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
    def _open(self, side, price, note):
        margin = round(self.balance, 2)         # PERP: stake the whole balance as MARGIN...
        if margin < 1 or not price:
            return
        qty = margin * LEVERAGE / price         # ...and control notional = margin x LEVERAGE
        self.balance -= margin
        self.pos = FreqPos(id=uuid.uuid4().hex[:6], side=side, entry=price, qty=qty,
                           stake=margin, opened_at=time.time(), peak=price, note=note)
        self._note(f"{side.upper()} @ {price:,.1f} · {note} · margin ${margin:.2f} {LEVERAGE}x", "open")
        self._save()

    def _close(self, price, reason):
        p = self.pos
        pnl = p.unrealized(price)
        self.balance += p.stake + pnl
        rec = {"id": p.id, "side": p.side, "entry": round(p.entry, 1), "exit": round(price, 1),
               "qty": round(p.qty, 6), "pnl": round(pnl, 2), "reason": reason,
               "pnl_pct": round(pnl / p.stake * 100, 2) if p.stake else 0.0,
               "opened_at": p.opened_at, "closed_at": time.time()}
        self.history.insert(0, rec)
        self.history = self.history
        self._note(f"CLOSE {p.side.upper()} @ {price:,.1f} · {reason} · P&L ${pnl:+.2f} "
                   f"({rec['pnl_pct']:+.2f}%)", "win" if pnl >= 0 else "loss")
        self.pos = None
        self._save()

    # ---- exits: minimal_roi table + stoploss + trailing + RSI sell signal ----
    def _manage(self, px, rsi_now):
        p = self.pos
        if p is None or not px:
            return
        profit = (px - p.entry) / p.entry if p.side == "long" else (p.entry - px) / p.entry
        held_min = (time.time() - p.opened_at) / 60.0

        # liquidation backstop: a leveraged loss that eats ~all the margin
        if p.unrealized(px) <= -p.stake * 0.98:
            self._close(px, "liquidation"); return
        if profit <= STOPLOSS:
            self._close(px, "stoploss"); return

        # minimal_roi: take the highest minute-threshold <= elapsed; exit if profit beats it
        roi = None
        for mins in sorted(MINIMAL_ROI.keys()):
            if held_min >= mins:
                roi = MINIMAL_ROI[mins]
        if roi is not None and profit >= roi:
            self._close(px, f"roi {roi*100:.0f}%"); return

        # trailing stop (arms once +TRAIL_OFFSET in profit, then trails TRAIL_POSITIVE behind the best price)
        if not p.trail_on and profit >= TRAIL_OFFSET:
            p.trail_on = True
        if p.trail_on:
            if p.side == "long":
                p.peak = max(p.peak, px)
                if px <= p.peak * (1 - TRAIL_POSITIVE):
                    self._close(px, "trailing_stop"); return
            else:
                p.peak = min(p.peak, px)
                if px >= p.peak * (1 + TRAIL_POSITIVE):
                    self._close(px, "trailing_stop"); return

        # RSI signal (only bank a winner): long exits overbought, short exits oversold (mirror)
        if rsi_now is not None and profit > 0:
            if p.side == "long" and rsi_now >= SELL_RSI:
                self._close(px, "sell_signal (RSI)")
            elif p.side == "short" and rsi_now <= (100 - SELL_RSI):
                self._close(px, "cover_signal (RSI)")

    # ---- entry signal on a newly-closed 5m bar ----
    def _check_entry(self, closes):
        r = rsi(closes)
        ef = ema(closes[-50:], EMA_FAST) if len(closes) >= EMA_FAST else None
        es = ema(closes[-80:], EMA_SLOW) if len(closes) >= EMA_SLOW else None
        bl, bm, bu = bollinger(closes)
        self._ind = {"rsi": round(r, 1) if r is not None else None,
                     "ema_fast": round(ef, 1) if ef else None,
                     "ema_slow": round(es, 1) if es else None,
                     "bb_lower": round(bl, 1) if bl else None}
        if r is None or bl is None or bu is None or ef is None or es is None:
            self._prev_rsi = r
            return
        close = closes[-1]
        # LONG: oversold bounce — RSI crosses up through BUY_RSI or price at the lower band, not in a downtrend
        crossed_up = (self._prev_rsi is not None and self._prev_rsi < BUY_RSI <= r)
        at_lower_band = close <= bl * 1.001
        not_downtrend = ef >= es * 0.997
        # SHORT (mirror): overbought rejection — RSI crosses down through SHORT_RSI or price at the upper band, not in an uptrend
        crossed_down = (self._prev_rsi is not None and self._prev_rsi > SHORT_RSI >= r)
        at_upper_band = close >= bu * 0.999
        not_uptrend = ef <= es * 1.003
        self._prev_rsi = r
        price = self._btc() or close
        if (crossed_up or at_lower_band) and not_downtrend:
            why = []
            if crossed_up:
                why.append(f"RSI cross↑{BUY_RSI:.0f} ({r:.0f})")
            if at_lower_band:
                why.append(f"@ BB lower {bl:,.0f}")
            self._open("long", price, " · ".join(why) or f"RSI {r:.0f}")
        elif (crossed_down or at_upper_band) and not_uptrend:
            why = []
            if crossed_down:
                why.append(f"RSI cross↓{SHORT_RSI:.0f} ({r:.0f})")
            if at_upper_band:
                why.append(f"@ BB upper {bu:,.0f}")
            self._open("short", price, " · ".join(why) or f"RSI {r:.0f}")

    # ---- main loop ----
    async def manage_loop(self):
        await asyncio.sleep(4.0)
        while True:
            await asyncio.sleep(15.0)
            try:
                now = time.time()
                await self._refresh(now)
                px = self._btc()
                closes = [c["c"] for c in self._cs]
                r_now = rsi(closes) if closes else None
                if self.pos is not None and px:
                    self._manage(px, r_now)
                if not self._cs:
                    continue
                newest = self._cs[-1]["t"]
                if newest == self._last_bar_t:
                    continue
                self._last_bar_t = newest
                if self.pos is None and self.enabled:
                    self._check_entry(closes)
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:100]}", "error")

    # ---- controls / state ----
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED — paper-trading the Freqtrade-style strategy" if self.enabled
                   else "bot PAUSED — no new entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_strategy(self, name):
        if name and name not in STRATEGIES:
            return {"ok": False, "error": f"unknown strategy '{name}'"}
        return {"ok": True, "strategy": self.strategy, "strategy_label": STRATEGIES[self.strategy]}

    def reset(self):
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos = None
        self.history = []
        self.log = []
        self._prev_rsi = None
        self._note("bot reset (paper account back to $%.0f)" % START_BALANCE)
        self._save()
        return {"ok": True}

    def state(self, **_):
        px = self._btc()
        positions = []
        if self.pos is not None:
            p = self.pos
            up = p.unrealized(px) if px else 0.0
            positions.append({
                "id": p.id, "side": p.side, "entry": round(p.entry, 1),
                "qty": round(p.qty, 6), "leverage": LEVERAGE,
                "liq": round(p.entry * (1 - 1.0 / LEVERAGE), 1) if p.side == "long"
                       else round(p.entry * (1 + 1.0 / LEVERAGE), 1),
                "pnl": round(up, 2),
                "pnl_pct": round(up / p.stake * 100, 2) if p.stake else 0.0,   # % of MARGIN
                "news": p.note + (" · trailing" if p.trail_on else ""),
            })
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        ind = self._ind
        return {
            "name": "Freqtrade-style",
            "enabled": self.enabled,
            "running": True,
            "strategy": self.strategy,
            "strategy_label": STRATEGIES[self.strategy],
            "strategies": STRATEGIES,
            "timeframe": TIMEFRAME,
            "leverage": LEVERAGE,
            "market": "perp",
            "rsi": ind.get("rsi"),
            "bb_lower": ind.get("bb_lower"),
            "ema_fast": ind.get("ema_fast"),
            "ema_slow": ind.get("ema_slow"),
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


class ImprovedTPSLFreqBot(FreqBot):
    """Same entries as FreqBot, but exits with margin-based stop + profit trail."""

    def _path(self):
        return os.path.join("data", "freq_bot_improved_tpsl_state.json")

    def _open(self, side, price, note):
        super()._open(side, price, note)
        if self.pos is not None:
            self.pos.best_pnl = 0.0
            self.pos.stop_pnl = round(self.pos.stake * IMPROVED_STOP_MARGIN, 2)
            self._save()

    def _lock_fraction(self, best_margin):
        for trigger, lock in IMPROVED_TRAIL_TIERS:
            if best_margin >= trigger:
                return lock
        return None

    def _manage(self, px, rsi_now):
        p = self.pos
        if p is None or not px:
            return
        pnl = p.unrealized(px)
        p.best_pnl = max(float(getattr(p, "best_pnl", 0.0)), pnl)
        best_margin = p.best_pnl / p.stake if p.stake else 0.0

        # Emergency stop: fixed account risk in margin terms, leverage-aware by design.
        emergency_stop = p.stake * IMPROVED_STOP_MARGIN
        if float(getattr(p, "stop_pnl", 0.0)) == 0.0 and best_margin < IMPROVED_BE_TRIGGER:
            p.stop_pnl = emergency_stop

        if best_margin >= IMPROVED_BE_TRIGGER:
            p.stop_pnl = max(p.stop_pnl, 0.0)
        if best_margin >= IMPROVED_TRAIL_TRIGGER:
            lock = self._lock_fraction(best_margin)
            if lock is not None:
                p.stop_pnl = max(p.stop_pnl, p.best_pnl * lock)
                p.trail_on = True

        # Liquidation backstop remains underneath the strategy stop.
        if pnl <= -p.stake * 0.98:
            self._close(px, "liquidation"); return
        if pnl <= p.stop_pnl:
            reason = "trailing_take_profit" if p.stop_pnl >= 0 else "stoploss"
            self._close(px, f"{reason} ({p.stop_pnl / p.stake * 100:+.1f}% margin)"); return

        # Optional RSI winner exit from the original strategy, but only after BE is protected.
        profit = (px - p.entry) / p.entry if p.side == "long" else (p.entry - px) / p.entry
        if rsi_now is not None and profit > 0 and p.stop_pnl >= 0:
            if p.side == "long" and rsi_now >= SELL_RSI:
                self._close(px, "sell_signal (RSI)")
            elif p.side == "short" and rsi_now <= (100 - SELL_RSI):
                self._close(px, "cover_signal (RSI)")

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - Freqtrade entries with improved TP/SL" if self.enabled
                   else "bot PAUSED - no new entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        out = super().reset()
        self.log = []
        self._note("bot reset (improved TP/SL paper account back to $%.0f)" % START_BALANCE)
        self._save()
        return out

    def state(self, **_):
        s = super().state(**_)
        s["name"] = "Freqtrade-style (paper improved TP/SL)"
        s["strategy"] = "freq_improved_tpsl"
        s["strategy_label"] = "Freqtrade-style (paper improved TP/SL)"
        s["strategies"] = {"freq_improved_tpsl": s["strategy_label"]}
        for pos in s.get("positions", []):
            if self.pos is None:
                continue
            p = self.pos
            stop_pnl = float(getattr(p, "stop_pnl", p.stake * IMPROVED_STOP_MARGIN))
            best_pnl = float(getattr(p, "best_pnl", 0.0))
            if p.qty:
                stop_price = p.entry + stop_pnl / p.qty if p.side == "long" else p.entry - stop_pnl / p.qty
                pos["stop"] = round(stop_price, 1)
            pos["best_pnl"] = round(best_pnl, 2)
            pos["stop_pnl"] = round(stop_pnl, 2)
            pos["be"] = stop_pnl >= 0
            pos["news"] = (
                f"{p.note} | best ${best_pnl:+.2f} | lock ${stop_pnl:+.2f}"
            )
        return s


class TrendFollowTPSLFreqBot(ImprovedTPSLFreqBot):
    """Same Freqtrade entries, stricter losses, looser trend-following winner exits."""

    STOP_MARGIN = -0.04
    BE_TRIGGER = 0.03
    TRAIL_TRIGGER = 0.08
    NO_PROGRESS_SEC = 180
    TRAIL_TIERS = (
        (0.40, 0.82),
        (0.20, 0.65),
        (0.08, 0.40),
    )

    def _path(self):
        return os.path.join("data", "freq_bot_trend_tpsl_state.json")

    def _open(self, side, price, note):
        super()._open(side, price, note)
        if self.pos is not None:
            self.pos.stop_pnl = round(self.pos.stake * self.STOP_MARGIN, 2)
            self.pos.best_pnl = 0.0
            self._save()

    def _lock_fraction(self, best_margin):
        for trigger, lock in self.TRAIL_TIERS:
            if best_margin >= trigger:
                return lock
        return None

    def _manage(self, px, rsi_now):
        p = self.pos
        if p is None or not px:
            return

        pnl = p.unrealized(px)
        p.best_pnl = max(float(getattr(p, "best_pnl", 0.0)), pnl)
        best_margin = p.best_pnl / p.stake if p.stake else 0.0
        emergency_stop = p.stake * self.STOP_MARGIN
        if float(getattr(p, "stop_pnl", 0.0)) == 0.0 and best_margin < self.BE_TRIGGER:
            p.stop_pnl = emergency_stop

        held_sec = time.time() - p.opened_at
        if held_sec >= self.NO_PROGRESS_SEC and pnl <= 0:
            self._close(px, "no_progress_3m"); return

        if best_margin >= self.BE_TRIGGER:
            p.stop_pnl = max(p.stop_pnl, 0.0)
        if best_margin >= self.TRAIL_TRIGGER:
            lock = self._lock_fraction(best_margin)
            if lock is not None:
                p.stop_pnl = max(p.stop_pnl, p.best_pnl * lock)
                p.trail_on = True

        if pnl <= -p.stake * 0.98:
            self._close(px, "liquidation"); return
        if pnl <= p.stop_pnl:
            reason = "trend_trailing_take_profit" if p.stop_pnl >= 0 else "tight_stoploss"
            self._close(px, f"{reason} ({p.stop_pnl / p.stake * 100:+.1f}% margin)"); return

        # Let winners run, but close once the 5m EMA trend flips against the position.
        ef = self._ind.get("ema_fast")
        es = self._ind.get("ema_slow")
        if p.stop_pnl >= 0 and ef is not None and es is not None:
            if p.side == "long" and ef < es:
                self._close(px, "trend_break (EMA)"); return
            if p.side == "short" and ef > es:
                self._close(px, "trend_break (EMA)"); return

    async def manage_loop(self):
        await asyncio.sleep(4.0)
        while True:
            await asyncio.sleep(1.0)
            try:
                now = time.time()
                await self._refresh(now)
                px = self._btc()
                closes = [c["c"] for c in self._cs]
                r_now = rsi(closes) if closes else None
                if self.pos is not None and px:
                    self._manage(px, r_now)
                if not self._cs:
                    continue
                newest = self._cs[-1]["t"]
                if newest == self._last_bar_t:
                    continue
                self._last_bar_t = newest
                if self.pos is None and self.enabled:
                    self._check_entry(closes)
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:100]}", "error")

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - Freqtrade entries with trend-follow TP/SL" if self.enabled
                   else "bot PAUSED - no new entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        out = FreqBot.reset(self)
        self.log = []
        self._note("bot reset (trend TP/SL paper account back to $%.0f)" % START_BALANCE)
        self._save()
        return out

    def state(self, **_):
        s = super().state(**_)
        s["name"] = "Freqtrade-style (paper trend TP/SL)"
        s["strategy"] = "freq_trend_tpsl"
        s["strategy_label"] = "Freqtrade-style (paper trend TP/SL)"
        s["strategies"] = {"freq_trend_tpsl": s["strategy_label"]}
        return s


class FivePctTPSLFreqBot(ImprovedTPSLFreqBot):
    """Same Freqtrade entries, -5% margin stop, then trail best profit by 5% margin."""

    STOP_MARGIN = -0.05
    BE_TRIGGER = 0.03
    TRAIL_TRIGGER = 0.05
    TRAIL_GAP = 0.05

    def _path(self):
        return os.path.join("data", "freq_bot_improved_tpsl_5_state.json")

    def _open(self, side, price, note):
        FreqBot._open(self, side, price, note)
        if self.pos is not None:
            self.pos.best_pnl = 0.0
            self.pos.stop_pnl = round(self.pos.stake * self.STOP_MARGIN, 2)
            self._save()

    def _manage(self, px, rsi_now):
        p = self.pos
        if p is None or not px:
            return

        pnl = p.unrealized(px)
        p.best_pnl = max(float(getattr(p, "best_pnl", 0.0)), pnl)
        best_margin = p.best_pnl / p.stake if p.stake else 0.0
        emergency_stop = p.stake * self.STOP_MARGIN
        if float(getattr(p, "stop_pnl", 0.0)) == 0.0 and best_margin < self.BE_TRIGGER:
            p.stop_pnl = emergency_stop

        if best_margin >= self.BE_TRIGGER:
            p.stop_pnl = max(p.stop_pnl, 0.0)
        if best_margin >= self.TRAIL_TRIGGER:
            p.stop_pnl = max(p.stop_pnl, p.best_pnl - (p.stake * self.TRAIL_GAP))
            p.trail_on = True

        if pnl <= -p.stake * 0.98:
            self._close(px, "liquidation"); return
        if pnl <= p.stop_pnl:
            reason = "trailing_take_profit_5%" if p.stop_pnl >= 0 else "stoploss_5%"
            self._close(px, f"{reason} ({p.stop_pnl / p.stake * 100:+.1f}% margin)"); return

        profit = (px - p.entry) / p.entry if p.side == "long" else (p.entry - px) / p.entry
        if rsi_now is not None and profit > 0 and p.stop_pnl >= 0:
            if p.side == "long" and rsi_now >= SELL_RSI:
                self._close(px, "sell_signal (RSI)")
            elif p.side == "short" and rsi_now <= (100 - SELL_RSI):
                self._close(px, "cover_signal (RSI)")

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - Freqtrade entries with improved TP/SL 5%" if self.enabled
                   else "bot PAUSED - no new entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        out = FreqBot.reset(self)
        self.log = []
        self._note("bot reset (improved TP/SL 5% paper account back to $%.0f)" % START_BALANCE)
        self._save()
        return out

    def state(self, **_):
        s = super().state(**_)
        s["name"] = "Freqtrade-style (improved TP/SL 5%)"
        s["strategy"] = "freq_improved_tpsl_5"
        s["strategy_label"] = "Freqtrade-style (improved TP/SL 5%)"
        s["strategies"] = {"freq_improved_tpsl_5": s["strategy_label"]}
        return s


class TrendFlowConfirmedFreqBot(FivePctTPSLFreqBot):
    """Freqtrade entries, filtered by 1h trend and 60s taker flow, with 5% margin trail."""

    ENTRY_TIMEFRAME = "1m"
    ENTRY_REFRESH_SEC = 20
    HTF_TIMEFRAME = "1h"
    FLOW_WINDOW = 60
    FLOW_CONFIRM = 0.55

    def __init__(self, market, whales=None):
        self.whales = whales
        self._htf_cs: list = []
        self._htf_t_fetch = 0.0
        self._htf_trend = None
        self._flow_pct = None
        self._flow_total = 0.0
        super().__init__(market)

    def _path(self):
        return os.path.join("data", "freq_bot_trend_flow_5_state.json")

    async def _refresh(self, now):
        if now >= self._rl_until and (now - self._t_fetch) >= self.ENTRY_REFRESH_SEC:
            try:
                self._cs = await asyncio.to_thread(
                    lighter_markets.candles, "BTC", self.ENTRY_TIMEFRAME, 240
                )
                self._t_fetch = now
                self._err = ""
            except Exception as e:
                msg = str(e)
                if "RateLimit" in type(e).__name__ or "23000" in msg or "Too Many" in msg:
                    self._rl_until = now + 60.0
                    self._err = "rate-limited by Lighter - backing off 60s"
                else:
                    self._err = f"{type(e).__name__}: {str(e)[:120]}"
        if (now - self._htf_t_fetch) < 300:
            return
        try:
            self._htf_cs = await asyncio.to_thread(lighter_markets.candles, "BTC", self.HTF_TIMEFRAME, 120)
            self._htf_t_fetch = now
            closes = [c["c"] for c in self._htf_cs]
            ef = ema(closes[-50:], EMA_FAST) if len(closes) >= EMA_FAST else None
            es = ema(closes[-80:], EMA_SLOW) if len(closes) >= EMA_SLOW else None
            if ef is not None and es is not None:
                self._htf_trend = "bull" if ef > es else ("bear" if ef < es else "flat")
                self._ind["htf_ema_fast"] = round(ef, 1)
                self._ind["htf_ema_slow"] = round(es, 1)
        except Exception as e:
            self._err = f"HTF {type(e).__name__}: {str(e)[:100]}"

    def _flow_ok(self, side):
        if self.whales is None:
            self._flow_pct = None
            self._flow_total = 0.0
            return False
        try:
            buy, sell = self.whales.flow(self.FLOW_WINDOW)
        except Exception:
            self._flow_pct = None
            self._flow_total = 0.0
            return False
        total = buy + sell
        self._flow_total = total
        if total <= 0:
            self._flow_pct = None
            return False
        buy_pct = buy / total
        self._flow_pct = buy_pct * 100.0
        return buy_pct >= self.FLOW_CONFIRM if side == "long" else buy_pct <= (1.0 - self.FLOW_CONFIRM)

    def _check_entry(self, closes):
        r = rsi(closes)
        ef = ema(closes[-50:], EMA_FAST) if len(closes) >= EMA_FAST else None
        es = ema(closes[-80:], EMA_SLOW) if len(closes) >= EMA_SLOW else None
        bl, bm, bu = bollinger(closes)
        self._ind.update({"rsi": round(r, 1) if r is not None else None,
                          "ema_fast": round(ef, 1) if ef else None,
                          "ema_slow": round(es, 1) if es else None,
                          "bb_lower": round(bl, 1) if bl else None})
        if r is None or bl is None or bu is None or ef is None or es is None:
            self._prev_rsi = r
            return

        close = closes[-1]
        crossed_up = (self._prev_rsi is not None and self._prev_rsi < BUY_RSI <= r)
        at_lower_band = close <= bl * 1.001
        not_downtrend = ef >= es * 0.997
        crossed_down = (self._prev_rsi is not None and self._prev_rsi > SHORT_RSI >= r)
        at_upper_band = close >= bu * 0.999
        not_uptrend = ef <= es * 1.003
        self._prev_rsi = r

        price = self._btc() or close
        if not price:
            return

        long_setup = (crossed_up or at_lower_band) and not_downtrend
        short_setup = (crossed_down or at_upper_band) and not_uptrend
        if long_setup and self._htf_trend == "bull" and self._flow_ok("long"):
            why = ["1h bull", f"flow {self._flow_pct:.0f}% buy"]
            if crossed_up:
                why.append(f"RSI cross↑{BUY_RSI:.0f} ({r:.0f})")
            if at_lower_band:
                why.append(f"@ BB lower {bl:,.0f}")
            self._open("long", price, " · ".join(why))
        elif short_setup and self._htf_trend == "bear" and self._flow_ok("short"):
            sell_pct = 100.0 - (self._flow_pct or 50.0)
            why = ["1h bear", f"flow {sell_pct:.0f}% sell"]
            if crossed_down:
                why.append(f"RSI cross↓{SHORT_RSI:.0f} ({r:.0f})")
            if at_upper_band:
                why.append(f"@ BB upper {bu:,.0f}")
            self._open("short", price, " · ".join(why))

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - 1m trend+flow confirmed Freqtrade 5%" if self.enabled
                   else "bot PAUSED - no new entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        out = FreqBot.reset(self)
        self.log = []
        self._note("bot reset (1m trend+flow confirmed 5% paper account back to $%.0f)" % START_BALANCE)
        self._save()
        return out

    def state(self, **_):
        s = super().state(**_)
        if self.whales is not None:
            try:
                buy, sell = self.whales.flow(self.FLOW_WINDOW)
                self._flow_total = buy + sell
                self._flow_pct = (buy / self._flow_total * 100.0) if self._flow_total > 0 else None
            except Exception:
                pass
        s["name"] = "Freqtrade-style (1m trend+flow confirmed 5%)"
        s["strategy"] = "freq_trend_flow_5_1m"
        s["strategy_label"] = "Freqtrade-style (1m trend+flow confirmed 5%)"
        s["strategies"] = {"freq_trend_flow_5_1m": s["strategy_label"]}
        s["timeframe"] = self.ENTRY_TIMEFRAME
        s["htf_trend"] = self._htf_trend
        s["flow_pct"] = round(self._flow_pct, 1) if self._flow_pct is not None else None
        s["flow_total"] = round(self._flow_total, 0)
        s["htf_ema_fast"] = self._ind.get("htf_ema_fast")
        s["htf_ema_slow"] = self._ind.get("htf_ema_slow")
        return s
