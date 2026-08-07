"""
nr7_aggr_paper.py - the AGGRESSIVE NR7 bot: the validated NR7 breakout (ES+NQ+CL, fully reused from
nr7_paper.NR7PaperBot) PLUS the NQ mean-reversion that adds the frequency to pass Apex fast:
  * VWAP-2sigma fade  - when NQ stretches >=2 std from the session mean, fade back to the mean
  * Turtle Soup       - sweep of the prior day's high/low then close back inside -> fade
Both run on NQ as a separate engine on the SAME $50k account, one reversion position at a time.

IMPORTANT (honesty): the NQ reversion is single-market and in-sample/overfit. In backtest it carries
80% of the profit, but live it will be far choppier and weaker than the curve suggests. NR7 is the
robust core; the reversion is the fast-but-fragile speed-booster.  Paper only.
"""
from __future__ import annotations

import math
import os
import time
import uuid

import numpy as np

import config
from nr7_paper import NR7PaperBot, MARKETS, _minute, _daily, FLAT_MIN, COMMISSION_RT

NQ = "NQ=F"
MNQ_PV, MNQ_TICK = 2.0, 0.25
K_BAND = 2.0          # fade at +/- 2 std from the session mean
MIN_BARS = 15         # need this many bars before the band is meaningful


class NR7AggressivePaperBot(NR7PaperBot):
    NAME = "NR7 Aggressive (NR7 + NQ reversion)"

    def __init__(self):
        super().__init__()
        self.mr_pos = None
        self._mr_last_bar = 0

    def _path(self) -> str:
        return os.path.join(config.DATA_DIR, "nr7_aggr_state.json")

    # ---- run NR7 (parent) then the NQ reversion ----
    def _tick(self):
        super()._tick()                                   # NR7 on ES+NQ+CL
        d = self._df.get(NQ)
        if d is None or d.empty:
            return
        today = d[d["day"].astype(str) == str(d.iloc[-1]["day"])].reset_index(drop=True)
        if today.empty:
            return
        cur = today.iloc[-1]
        bts = int(cur["dt_utc"].timestamp())
        if self._mr_last_bar == bts:
            return
        self._mr_last_bar = bts
        if self.mr_pos is not None:
            self._manage_mr(cur)
        if self.mr_pos is None and self.enabled:
            self._scan_mr(today, cur, d)

    def _scan_mr(self, today, cur, d):
        minute = _minute(cur["dt_ny"])
        if minute < 9 * 60 + 45 or minute > 14 * 60:       # NY-session, not too late
            return
        close = float(cur["close"]); side = 0; strat = entry = stop = target = None
        tp = (today["high"] + today["low"] + today["close"]) / 3.0
        if len(tp) >= MIN_BARS:
            mean = float(tp.mean()); sd = float(tp.std())
            if sd > 0:
                if close >= mean + K_BAND * sd:
                    side, strat, entry, stop, target = -1, "VWAP2s", close, mean + (K_BAND + 1) * sd, mean
                elif close <= mean - K_BAND * sd:
                    side, strat, entry, stop, target = 1, "VWAP2s", close, mean - (K_BAND + 1) * sd, mean
        if side == 0:                                      # Turtle Soup on prior-day extreme
            daily = _daily(d[d["day"].astype(str) < str(cur["day"])])
            if len(daily) >= 1:
                pdh, pdl = float(daily.iloc[-1]["high"]), float(daily.iloc[-1]["low"])
                if float(cur["high"]) > pdh and close < pdh:
                    side, strat, entry, stop = -1, "Turtle", close, float(cur["high"]) + 2 * MNQ_TICK
                    target = entry - 2 * (stop - entry)
                elif float(cur["low"]) < pdl and close > pdl:
                    side, strat, entry, stop = 1, "Turtle", close, float(cur["low"]) - 2 * MNQ_TICK
                    target = entry + 2 * (entry - stop)
        if side == 0:
            return
        r = abs(entry - stop)
        if r <= 0 or abs(target - entry) <= 0:
            return
        risk = self._risk_usd(); qty = max(1, int(math.floor(risk / (r * MNQ_PV))))
        self.mr_pos = {"id": uuid.uuid4().hex[:6], "strat": strat, "side": "long" if side > 0 else "short",
                       "entry": entry, "qty": qty, "stop": stop, "target": target, "r": r,
                       "opened_at": time.time(), "risk": risk}
        self._note(f"OPEN {strat} {self.mr_pos['side'].upper()} MNQ @ {entry:.2f} "
                   f"stop {stop:.2f} tgt {target:.2f} ({qty} micro)", "open")

    def _manage_mr(self, bar):
        p = self.mr_pos; side = 1 if p["side"] == "long" else -1
        hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
        xpx = rsn = None
        if side > 0:
            if lo <= p["stop"]: xpx, rsn = p["stop"], "stop"
            elif hi >= p["target"]: xpx, rsn = p["target"], "target"
        else:
            if hi >= p["stop"]: xpx, rsn = p["stop"], "stop"
            elif lo <= p["target"]: xpx, rsn = p["target"], "target"
        if rsn is None and _minute(bar["dt_ny"]) >= FLAT_MIN:
            xpx, rsn = cl, "eod"
        if rsn:
            pnl = side * (xpx - p["entry"]) * p["qty"] * MNQ_PV - p["qty"] * COMMISSION_RT
            self._book(pnl)
            self.history.insert(0, {"mkt": "MNQ·" + p["strat"], "side": p["side"], "entry": round(p["entry"], 2),
                                    "exit": round(xpx, 2), "qty": p["qty"], "pnl": round(pnl, 2),
                                    "reason": rsn, "closed_at": time.time()})
            self._note(f"CLOSE {p['strat']} MNQ @ {xpx:.2f} - {rsn} - P&L ${pnl:+.2f}",
                       "win" if pnl >= 0 else "loss")
            self.mr_pos = None

    def equity(self) -> float:
        eq = super().equity()
        if self.mr_pos is not None:
            px = self.prices.get(NQ, self.mr_pos["entry"]); side = 1 if self.mr_pos["side"] == "long" else -1
            eq += side * (px - self.mr_pos["entry"]) * self.mr_pos["qty"] * MNQ_PV
        return eq

    def state(self):
        s = super().state()
        s["name"] = self.NAME
        if self.mr_pos is not None:
            p = self.mr_pos; px = self.prices.get(NQ, p["entry"]); side = 1 if p["side"] == "long" else -1
            up = side * (px - p["entry"]) * p["qty"] * MNQ_PV
            s["positions"].append({"mkt": "MNQ·" + p["strat"], "side": p["side"], "entry": round(p["entry"], 2),
                                   "qty": p["qty"], "stop": round(p["stop"], 2), "tp2": round(p["target"], 2),
                                   "pnl": round(up, 2), "pnl_R": round(up / max(p["risk"], 1e-9), 2),
                                   "news": f"NQ {p['strat']} reversion (overfit-suspect)"})
        return s

    def reset(self):
        self.mr_pos = None; self._mr_last_bar = 0
        return super().reset()
