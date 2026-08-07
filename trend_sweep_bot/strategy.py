"""
strategy.py — the sweep -> consolidation -> breakout state machine.

It is fed CLOSED entry-timeframe (5m) candles one at a time, together with a context built by the
caller (4H trend, previous-day high/low, daily VWAP, ATR). It emits an entry signal dict or None.
It never looks at future bars: the consolidation is measured on the bars BEFORE the current one,
and the breakout is the current bar's close — so a signal at bar i is fully decided at i's close.

Signal dict:
  {"side": "long"|"short", "entry", "stop", "tp1", "tp2", "range_high", "range_low", "reason"}
"""
from __future__ import annotations
from . import indicators


class TrendSweepStrategy:
    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        """Clear the setup state (called on a new sweep, expiry, or after emitting a signal)."""
        self.swept = False
        self.sweep_side = None        # "long" looks for a PDL sweep, "short" a PDH sweep
        self.sweep_age = 0

    # ------------------------------------------------------------------
    def on_bar(self, c5, i, ctx):
        """c5: full list of 5m candles. i: index of the just-closed bar. ctx: dict with
        trend / pdh / pdl / vwap / atr. Returns a signal dict or None."""
        cfg = self.cfg
        trend = ctx.get("trend")
        pdh, pdl = ctx.get("pdh"), ctx.get("pdl")
        atr_val = ctx.get("atr")
        if trend is None or atr_val is None or pdh is None or pdl is None:
            self.reset()
            return None

        bar = c5[i]

        # ---- (1) detect / age the liquidity sweep --------------------------------
        if not self.swept:
            if trend == "bull" and bar["l"] <= pdl * (1 + cfg.sweep_tol):
                self.swept, self.sweep_side, self.sweep_age = True, "long", 0
            elif trend == "bear" and bar["h"] >= pdh * (1 - cfg.sweep_tol):
                self.swept, self.sweep_side, self.sweep_age = True, "short", 0
            return None

        # a sweep only stays "armed" for a limited window, and only while the trend agrees
        self.sweep_age += 1
        if self.sweep_age > cfg.setup_valid_bars or \
                (self.sweep_side == "long" and trend != "bull") or \
                (self.sweep_side == "short" and trend != "bear"):
            self.reset()
            return None

        # ---- (2) consolidation on the bars BEFORE this one -----------------------
        w = cfg.cons_min
        if i < w + 1:
            return None
        window = c5[i - w:i]                      # the w bars immediately before the breakout bar
        cons = indicators.consolidation(window, atr_val, cfg.body_atr_mult, cfg.range_atr_mult)
        if cons is None:
            return None
        rh, rl = cons

        # ---- (3) breakout of the consolidation in the trend direction ------------
        ref = bar["c"] if cfg.breakout_use_close else (bar["h"] if self.sweep_side == "long" else bar["l"])
        if self.sweep_side == "long":
            if not (ref > rh):
                return None
            entry = bar["c"]
            stop = rl - cfg.stop_buf_atr * atr_val
            if entry <= stop:
                return None
            risk = entry - stop
            tp1 = ctx["vwap"]
            tp2 = entry + cfg.tp2_R * risk
            if tp1 is None or tp1 <= entry:
                if cfg.require_vwap_tp:
                    return None
                tp1 = tp2
            if (tp1 - entry) / risk < cfg.min_rr:
                return None
            sig = self._signal("long", entry, stop, tp1, tp2, rh, rl)
        else:
            if not (ref < rl):
                return None
            entry = bar["c"]
            stop = rh + cfg.stop_buf_atr * atr_val
            if entry >= stop:
                return None
            risk = stop - entry
            tp1 = ctx["vwap"]
            tp2 = entry - cfg.tp2_R * risk
            if tp1 is None or tp1 >= entry:
                if cfg.require_vwap_tp:
                    return None
                tp1 = tp2
            if (entry - tp1) / risk < cfg.min_rr:
                return None
            sig = self._signal("short", entry, stop, tp1, tp2, rh, rl)

        self.reset()                              # one entry per sweep
        return sig

    def _signal(self, side, entry, stop, tp1, tp2, rh, rl):
        r = abs(entry - stop)
        return {"side": side, "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                "range_high": rh, "range_low": rl, "risk": r,
                "reason": f"{side} breakout of {rl:.2f}-{rh:.2f} after sweep · R={r:.2f}"}
