"""
backtest.py — event-driven backtester.

Design choices that keep it honest:
  - Decisions use only CLOSED candles. A signal at bar i is decided at i's close; the position is
    then managed from bar i+1 onward — no peeking inside the signal bar.
  - Per-bar context (4H trend, prev-day H/L, intraday VWAP, ATR) is rebuilt from data up to the
    current bar only.
  - Slippage AND taker fees are charged on every fill (entry and each exit leg).
  - If a bar touches both the stop and a target, the STOP is assumed to fill first (conservative).
"""
from __future__ import annotations
from datetime import datetime, timezone

from . import indicators, data_feed, metrics
from .config import Config
from .strategy import TrendSweepStrategy
from .risk import RiskManager, position_size
from .trade_logger import TradeCSV, get_logger


def _utc_date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Backtester:
    def __init__(self, cfg: Config, log=None):
        self.cfg = cfg
        self.log = log or get_logger("trend_sweep_bot.backtest")

    # ---------------------------------------------------------------- context
    def _daily_prev_map(self, c1d):
        """date -> (prev_day_high, prev_day_low)."""
        out = {}
        prev = None
        for c in c1d:
            d = _utc_date(c["t"])
            if prev is not None:
                out[d] = (prev["h"], prev["l"])
            prev = c
        return out

    def run(self, c5, c4h, c1d, trades_csv=None):
        cfg = self.cfg
        strat = TrendSweepStrategy(cfg)
        risk = RiskManager(cfg)
        csv = TradeCSV(trades_csv) if trades_csv else None

        prev_map = self._daily_prev_map(c1d)
        fee = cfg.fee_bps / 1e4
        slip = cfg.slippage_bps / 1e4

        balance = cfg.start_balance
        equity_curve = []
        trades = []
        pos = None                          # open position dict or None

        # rolling pointers for context
        i4 = 0                              # number of 4H bars closed at-or-before current 5m bar
        trend = None
        last_trend_key = -1
        cur_day = None
        vwap_num = vwap_den = 0.0

        atr_len = cfg.atr_len
        for i in range(len(c5)):
            bar = c5[i]
            t = bar["t"]
            day = _utc_date(t)

            # --- intraday VWAP (reset each UTC day, accumulate up to and incl. this bar) ---
            if day != cur_day:
                cur_day, vwap_num, vwap_den = day, 0.0, 0.0
            tp = (bar["h"] + bar["l"] + bar["c"]) / 3.0
            vwap_num += tp * bar["v"]
            vwap_den += bar["v"]
            vwap = (vwap_num / vwap_den) if vwap_den > 0 else None

            # --- 4H trend (recompute only when a new 4H bar has closed) ---
            while i4 < len(c4h) and (c4h[i4]["t"] + 14400) <= t:
                i4 += 1
            if i4 != last_trend_key:
                trend = indicators.classify_trend(c4h[:i4], cfg.swing_width,
                                                  cfg.trend_ema_len, cfg.trend_lookback) if i4 >= 6 else None
                last_trend_key = i4

            # --- prev-day high/low ---
            pdh, pdl = prev_map.get(day, (None, None))

            # --- ATR(5m) on closed bars up to i ---
            atr_val = indicators.atr(c5[max(0, i - atr_len - 1):i + 1], atr_len)

            # ---- manage an open position on THIS bar (uses bar high/low) ----
            if pos is not None:
                closed = self._manage(pos, bar, i, fee, slip, balance)
                if closed:
                    balance += closed["realized"]
                    trades.append(closed["rec"])
                    risk.on_close(closed["rec"]["pnl"])
                    if csv:
                        csv.write(closed["rec"])
                    self.log.info("EXIT %s %s @ %.2f  pnl=$%.2f (%.2fR)  %s",
                                  closed["rec"]["side"], cfg.symbol, closed["rec"]["exit"],
                                  closed["rec"]["pnl"], closed["rec"]["r_multiple"], closed["rec"]["reason"])
                    pos = None

            # ---- look for a NEW entry only when flat ----
            if pos is None:
                ctx = {"trend": trend, "pdh": pdh, "pdl": pdl, "vwap": vwap, "atr": atr_val}
                sig = strat.on_bar(c5, i, ctx)
                if sig:
                    ok, why = risk.can_trade(t)
                    if ok:
                        pos = self._open(sig, i, balance, fee, slip)
                        if pos is not None:
                            balance -= pos["entry_fee"]
                            risk.on_open(t)
                            self.log.info("ENTRY %s %s @ %.2f  stop %.2f  tp1 %.2f  tp2 %.2f  qty %.5f",
                                          sig["side"], cfg.symbol, pos["entry"], pos["stop"],
                                          pos["tp1"], pos["tp2"], pos["qty0"])

            # ---- equity sample (mark open position at bar close) ----
            eq = balance
            if pos is not None:
                mark = bar["c"]
                eq += (mark - pos["entry"]) * pos["qty"] if pos["side"] == "long" \
                    else (pos["entry"] - mark) * pos["qty"]
            equity_curve.append((t, eq))

        m = metrics.compute(trades, equity_curve, cfg.start_balance)
        return {"metrics": m, "trades": trades, "equity_curve": equity_curve}

    # ---------------------------------------------------------------- open
    def _open(self, sig, i, balance, fee, slip):
        side = sig["side"]
        # entry fills a touch worse than the close (taker slippage)
        entry = sig["entry"] * (1 + slip) if side == "long" else sig["entry"] * (1 - slip)
        stop = sig["stop"]
        qty = position_size(balance, self.cfg.risk_pct, entry, stop)
        if qty <= 0:
            return None
        risk_dollars = qty * abs(entry - stop)
        entry_fee = fee * entry * qty
        return {"side": side, "entry": entry, "stop": stop, "stop0": stop,
                "tp1": sig["tp1"], "tp2": sig["tp2"], "qty": qty, "qty0": qty,
                "tp1_done": False, "opened_i": i, "risk_dollars": risk_dollars,
                "entry_fee": entry_fee, "realized": 0.0, "reason": sig["reason"]}

    # ---------------------------------------------------------------- manage / exit
    def _manage(self, pos, bar, i, fee, slip, balance):
        cfg = self.cfg
        side = pos["side"]
        hi, lo, close = bar["h"], bar["l"], bar["c"]
        bars_held = i - pos["opened_i"]

        def fill(price, worse_down):
            # worse_down=True => fill below price (bad for a long exit / good-side modeled as slippage)
            return price * (1 - slip) if worse_down else price * (1 + slip)

        def close_leg(qty, price, reason, final):
            exit_px = fill(price, side == "long")
            leg_pnl = (exit_px - pos["entry"]) * qty if side == "long" else (pos["entry"] - exit_px) * qty
            exit_fee = fee * exit_px * qty
            pos["realized"] += leg_pnl - exit_fee
            pos["qty"] -= qty
            if final or pos["qty"] <= 1e-12:
                total = pos["realized"] - pos["entry_fee"]
                r = total / pos["risk_dollars"] if pos["risk_dollars"] else 0.0
                rec = {"closed_at": bar["t"], "symbol": cfg.symbol, "side": side,
                       "entry": round(pos["entry"], 2), "exit": round(exit_px, 2),
                       "qty": round(pos["qty0"], 6), "stop": round(pos["stop0"], 2),
                       "tp1": round(pos["tp1"], 2), "tp2": round(pos["tp2"], 2),
                       "pnl": round(total, 2), "r_multiple": round(r, 2),
                       "reason": reason, "bars_held": bars_held}
                return {"realized": total, "rec": rec}
            return None

        # STOP first (conservative when a bar straddles both stop and target)
        if side == "long":
            if lo <= pos["stop"]:
                return close_leg(pos["qty"], pos["stop"], "stop_loss" if not pos["tp1_done"] else "breakeven", True)
            if not pos["tp1_done"] and hi >= pos["tp1"]:
                close_leg(pos["qty0"] * cfg.tp1_size, pos["tp1"], "tp1_vwap", False)
                pos["tp1_done"] = True
                if cfg.move_stop_be_after_tp1:
                    pos["stop"] = pos["entry"]
            if hi >= pos["tp2"]:
                return close_leg(pos["qty"], pos["tp2"], "tp2_2R", True)
        else:
            if hi >= pos["stop"]:
                return close_leg(pos["qty"], pos["stop"], "stop_loss" if not pos["tp1_done"] else "breakeven", True)
            if not pos["tp1_done"] and lo <= pos["tp1"]:
                close_leg(pos["qty0"] * cfg.tp1_size, pos["tp1"], "tp1_vwap", False)
                pos["tp1_done"] = True
                if cfg.move_stop_be_after_tp1:
                    pos["stop"] = pos["entry"]
            if lo <= pos["tp2"]:
                return close_leg(pos["qty"], pos["tp2"], "tp2_2R", True)

        # hard time stop
        if bars_held >= cfg.max_hold_bars:
            return close_leg(pos["qty"], close, "time_stop", True)
        return None
