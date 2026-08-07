"""
ict_bot.py — ICT "2022 model" (sweep -> market-structure-shift -> fair-value-gap) PAPER bot.

This is a faithful, mechanical encoding of the spec: it is a stop-run reversal model whose
entire edge lives in the R, not the win rate. It trades BTC on Lighter's own candles, PAPER only
(simulated fills against closed 1-minute candles). Nothing here places a real order.

Pipeline, exactly as specced:
  1. SETUP (HTF, 1h): dealing range from confirmed swings, equilibrium (EQ) split into
     discount (longs only) / premium (shorts only); catalog liquidity pools; IRL<->ERL bias.
  2. TIME GATE: only inside the killzones (London / NY AM / NY PM, New-York time).
  3. TRIGGER (LTF, 1m), five conditions in order: sweep -> short-term-high -> MSS (body close) ->
     displacement filter (>=1.5*ATR14 & body>=60% of range) -> FVG (3-candle gap, CE = midpoint).
  4. EXECUTION: limit at CE; stop = raid_low - buffer; TP1 = 2R or EQ (exit 50%, stop->BE+1tick);
     TP2 = opposing external pool - front-run offset; require structural R:R >= 2.5 else skip.
  5. RISK: size = 0.5% equity / stop_distance; max 2 trades/day; halt the day at -2R.

State machine: SCAN -> SWEPT -> ARMED -> PENDING -> FILLED (mirrors the spec's pseudocode).
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover - fallback if tzdata missing
    _NY = timezone(timedelta(hours=-5))             # EST (no DST) — good enough for the gate

import lighter_markets

# ---- account / sizing knobs (paper) --------------------------------------------------------------
START_BALANCE   = 100.0      # paper account starting equity (USD)
RISK_PCT        = 0.005      # size = 0.5% of equity / stop_distance  (spec: 0.25-0.5%)
MAX_TRADES_DAY  = 2          # spec: max 2 trades/day
DAY_HALT_R      = -2.0       # spec: halt the day at -2R realized
MIN_RR          = 2.5        # spec: require structural R:R (TP2-entry)/(entry-stop) >= 2.5
SETUP_EXPIRY_BARS = 20       # spec: setup expires 20 bars after the MSS if untouched
TICK            = 0.1        # BTC price tick used for the "+1 tick" breakeven nudge / front-run

# ---- trigger / filter knobs (straight from the spec) --------------------------------------------
SWING_W          = 2         # swing high/low = high(low) exceeds the 2 candles each side
DISP_ATR_MULT    = 1.5       # displacement: break-candle true range >= 1.5 * ATR(14)
DISP_BODY_FRAC   = 0.60      # displacement: body >= 60% of candle range
DISP_LEG_ATR     = 2.0       # OR the whole leg from raid_low >= 2 * ATR
FVG_MIN_ATR      = 0.25      # gap height must be >= 0.25 * ATR (discard micro-gaps)
MINPEN_ATR       = 0.1       # sweep penetration: low < L - 0.1*ATR (crypto)
BUFFER_ATR       = 0.1       # stop buffer below raid_low: 0.1 * ATR(1m) (crypto)
STOP_MAX_ATR5    = 1.5       # skip if stop distance > 1.5 * ATR(5m) (sweep too violent)
FRONTRUN_TICKS   = 2         # TP2 = opposing pool minus this many ticks (front-run the pool)

STRATEGIES = {"ict2022": "ICT 2022 model (sweep -> MSS -> FVG)"}


# ============================ indicator helpers ============================
def _tr(c, pc):
    return max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc))


def atr(cs, period=14):
    """Average true range over the last `period` candles. None if not enough data."""
    if len(cs) < period + 1:
        return None
    trs = [_tr(cs[i], cs[i - 1]["c"]) for i in range(1, len(cs))]
    last = trs[-period:]
    return sum(last) / len(last) if last else None


def swing_high_idx(cs, w=SWING_W):
    """Indexes of confirmed swing highs: high exceeds the highs of w candles each side."""
    out = []
    for i in range(w, len(cs) - w):
        h = cs[i]["h"]
        if all(h > cs[i - j]["h"] for j in range(1, w + 1)) and \
           all(h > cs[i + j]["h"] for j in range(1, w + 1)):
            out.append(i)
    return out


def swing_low_idx(cs, w=SWING_W):
    out = []
    for i in range(w, len(cs) - w):
        l = cs[i]["l"]
        if all(l < cs[i - j]["l"] for j in range(1, w + 1)) and \
           all(l < cs[i + j]["l"] for j in range(1, w + 1)):
            out.append(i)
    return out


def _ny(ts):
    return datetime.fromtimestamp(ts, _NY)


def in_killzone(ts):
    """True inside London 02:00-05:00, NY AM 09:30-11:00, NY PM 13:30-15:30 (New-York time)."""
    d = _ny(ts)
    mins = d.hour * 60 + d.minute
    if d.weekday() >= 5:                            # weekend — no killzone
        return None
    if 120 <= mins < 300:
        return "London"
    if 570 <= mins < 660:
        return "NY-AM"                              # 10:00-11:00 is the "silver bullet" sub-window
    if 810 <= mins < 930:
        return "NY-PM"
    return None


# ============================ position ============================
@dataclass
class ICTPos:
    id: str
    side: str               # "long" | "short"
    entry: float
    qty: float              # current size (base)
    qty0: float             # original size (for the 50% partial)
    stop: float             # current stop price
    tp1: float
    tp2: float
    raid: float             # raid_low / raid_high that defined the stop
    R: float                # risk per unit = abs(entry - stop)
    risk_usd: float         # initial dollar risk (= R * qty0) — the unit for realized-R accounting
    pool: float             # the pool level that was swept (no re-entry on it after a stop-out)
    opened_at: float
    half: bool = False      # TP1 taken (50% out, stop moved to BE)
    be: bool = False        # stop sitting at breakeven

    def unrealized(self, mark):
        if not mark:
            return 0.0
        return self.qty * (mark - self.entry) if self.side == "long" \
            else self.qty * (self.entry - mark)


# ============================ the bot ============================
class ICTBot:
    NAME = "ICT 2022 model"
    STRATEGY_KEY = "ict2022"
    STRATEGY_LABEL = "ICT 2022 model (sweep -> MSS -> FVG)"
    STRATEGIES = STRATEGIES

    RISK_PCT = RISK_PCT
    MAX_TRADES_DAY = MAX_TRADES_DAY
    DAY_HALT_R = DAY_HALT_R
    MIN_RR = MIN_RR
    SETUP_EXPIRY_BARS = SETUP_EXPIRY_BARS
    DISP_ATR_MULT = DISP_ATR_MULT
    DISP_BODY_FRAC = DISP_BODY_FRAC
    DISP_LEG_ATR = DISP_LEG_ATR
    FVG_MIN_ATR = FVG_MIN_ATR
    MINPEN_ATR = MINPEN_ATR
    BUFFER_ATR = BUFFER_ATR
    STOP_MAX_ATR5 = STOP_MAX_ATR5
    FRONTRUN_TICKS = FRONTRUN_TICKS

    def __init__(self, market):
        self.market = market                 # MarketData (live BTC price, for the panel + equity)
        self.enabled = False
        self.strategy = self.STRATEGY_KEY
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos: "ICTPos | None" = None
        self.history: list[dict] = []
        self.log: list[dict] = []

        # state machine
        self.phase = "SCAN"                  # SCAN -> SWEPT -> ARMED -> PENDING -> FILLED
        self.setup: dict = {}                # working setup (pool, raid, sth, fvg, entry, stop, ...)
        self.bias = None                     # "long" | "short" | None  (HTF draw on liquidity)
        self.kz = None                       # current killzone name or None
        self.rh = self.rl = self.eq = None   # dealing range high / low / equilibrium
        self._blocked_pools: set = set()     # pools already stopped out today (no re-entry)

        # day risk accounting (spec: 2 trades/day, halt at -2R)
        self._day = None                     # NY date string of the current trading day
        self._trades_today = 0
        self._day_R = 0.0

        # candle bookkeeping
        self._last_bar_t = 0                 # timestamp of the newest 1m candle we've processed
        self._cs1m: list = []
        self._cs5m: list = []
        self._cs1h: list = []
        self._err = ""
        # candle-fetch throttling (so we never hammer Lighter -> RateLimitExceeded 23000).
        # Each timeframe is refetched on its OWN slow cadence; HTF barely changes minute to minute.
        self._t1m = 0.0
        self._t5m = 0.0
        self._t1h = 0.0
        self._rl_until = 0.0                 # if Lighter rate-limits us, pause ALL fetches until this

        self._load()

    # ---- persistence ----
    def _path(self):
        return os.path.join("data", "ict_bot_state.json")

    def _save(self):
        try:
            os.makedirs("data", exist_ok=True)
            with open(self._path(), "w") as f:
                json.dump({
                    "enabled": self.enabled, "balance": self.balance,
                    "start_balance": self.start_balance,
                    "history": self.history, "log": self.log[:60],
                    "blocked": list(self._blocked_pools),
                    "day": self._day, "trades_today": self._trades_today, "day_R": self._day_R,
                }, f)
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
            self._blocked_pools = set(d.get("blocked", []))
            self._day = d.get("day")
            self._trades_today = int(d.get("trades_today", 0))
            self._day_R = float(d.get("day_R", 0.0))
            # Honor the CURRENT configured starting balance until the bot has actually traded,
            # so changing START_BALANCE (e.g. to $100) takes effect without a manual reset.
            if not self.history:
                self.balance = START_BALANCE
                self.start_balance = START_BALANCE
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

    # ---- day roll / risk gates ----
    def _roll_day(self, ts):
        day = _ny(ts).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._trades_today = 0
            self._day_R = 0.0
            self._blocked_pools.clear()

    def _can_trade_today(self):
        if self._trades_today >= self.MAX_TRADES_DAY:
            return False
        if self._day_R <= self.DAY_HALT_R:
            return False
        return True

    def _killzone(self, ts):
        return in_killzone(ts)

    # ============================ HTF setup (section 1) ============================
    def _htf(self):
        """Dealing range, equilibrium and the HTF bias (IRL<->ERL alternation, mechanical)."""
        h = self._cs1h
        if len(h) < 4 * SWING_W + 2:
            return
        shi, sli = swing_high_idx(h), swing_low_idx(h)
        if not shi or not sli:
            return
        rh = h[shi[-1]]["h"]
        rl = h[sli[-1]]["l"]
        if rh <= rl:
            return
        self.rh, self.rl, self.eq = rh, rl, (rh + rl) / 2.0

        # Bias from the most recent ERL purge: a HTF candle that traded THROUGH a range extreme
        # and CLOSED back inside the range. Low purged -> draw flips up -> long; high purged ->
        # short. Tiebreak: direction of the most recent HTF displacement (last strong bar).
        bias = None
        for i in range(len(h) - 1, max(0, len(h) - 10), -1):
            c = h[i]
            if c["l"] < rl and c["c"] > rl:
                bias = "long"; break
            if c["h"] > rh and c["c"] < rh:
                bias = "short"; break
        if bias is None:
            recent = h[-6:]
            big = max(recent, key=lambda c: c["h"] - c["l"])
            bias = "long" if big["c"] >= big["o"] else "short"
        self.bias = bias

    # ============================ liquidity pools (section 1) ============================
    def _day_bounds(self, ts, days_back=0):
        """[start,end) epoch secs of the NY trading day `days_back` days before the day of ts."""
        d = _ny(ts).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back)
        return d.timestamp(), (d + timedelta(days=1)).timestamp()

    def _hl_between(self, cs, t0, t1):
        rows = [c for c in cs if t0 <= c["t"] < t1]
        if not rows:
            return None, None
        return max(c["h"] for c in rows), min(c["l"] for c in rows)

    def _pools(self, ts):
        """Catalog low-pools (sweep targets for longs) and high-pools (for shorts), with labels.
        Priority order per spec: PDH/PDL, prior week H/L, session extremes, equal highs/lows,
        untapped HTF swings, plus the dealing-range extremes (ERL)."""
        lows, highs = [], []
        h1 = self._cs1h
        m1 = self._cs1m

        # previous day high/low (PDH/PDL)
        t0, t1 = self._day_bounds(ts, 1)
        pdh, pdl = self._hl_between(h1, t0, t1)
        if pdh:
            highs.append((pdh, "PDH")); lows.append((pdl, "PDL"))

        # previous week high/low
        nowd = _ny(ts)
        wk_start = (nowd - timedelta(days=nowd.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        pw_end = wk_start.timestamp()
        pw_start = (wk_start - timedelta(days=7)).timestamp()
        pwh, pwl = self._hl_between(h1, pw_start, pw_end)
        if pwh:
            highs.append((pwh, "PWH")); lows.append((pwl, "PWL"))

        # session extremes today: Asia 20:00-00:00 ET (prev evening) and London after 05:00 ET
        day0 = nowd.replace(hour=0, minute=0, second=0, microsecond=0)
        asia0 = (day0 - timedelta(hours=4)).timestamp()      # 20:00 prev day
        asia1 = day0.timestamp()                             # 00:00
        ah, al = self._hl_between(m1, asia0, asia1)
        if ah:
            highs.append((ah, "AsiaH")); lows.append((al, "AsiaL"))
        lon0 = (day0 + timedelta(hours=5)).timestamp()       # 05:00 ET
        lh, ll = self._hl_between(m1, lon0, ts)
        if lh:
            highs.append((lh, "LonH")); lows.append((ll, "LonL"))

        # relative equal highs/lows on the 1m (two swings within tol = max(2 ticks, 0.05*ATR_htf))
        a1h = atr(h1) or 0.0
        tol = max(2 * TICK, 0.05 * a1h)
        sh = [m1[i]["h"] for i in swing_high_idx(m1)][-12:]
        sl = [m1[i]["l"] for i in swing_low_idx(m1)][-12:]
        for i in range(len(sh)):
            for j in range(i + 1, len(sh)):
                if abs(sh[i] - sh[j]) <= tol:
                    highs.append((max(sh[i], sh[j]), "EQH"))
        for i in range(len(sl)):
            for j in range(i + 1, len(sl)):
                if abs(sl[i] - sl[j]) <= tol:
                    lows.append((min(sl[i], sl[j]), "EQL"))

        # untapped HTF swings within a 20-60 bar lookback
        for i in swing_high_idx(h1):
            if 20 <= (len(h1) - i) <= 60:
                highs.append((h1[i]["h"], "HTF-SwH"))
        for i in swing_low_idx(h1):
            if 20 <= (len(h1) - i) <= 60:
                lows.append((h1[i]["l"], "HTF-SwL"))

        # dealing-range extremes = external range liquidity (ERL)
        if self.rh:
            highs.append((self.rh, "ERL-High")); lows.append((self.rl, "ERL-Low"))

        return lows, highs

    # ============================ trigger (section 2) ============================
    def _find_fvg(self, cs, lo, hi, side):
        """Find a 3-candle fair value gap inside cs[lo:hi+1]. Returns (top, bottom, ce) or None.
        Bullish gap: low[k+1] > high[k-1]. Bearish: high[k+1] < low[k-1]. Gap >= 0.25*ATR."""
        a = atr(self._cs1m) or 0.0
        minh = self.FVG_MIN_ATR * a
        for k in range(hi, lo, -1):
            if k - 1 < 0 or k + 1 >= len(cs):
                continue
            c1, c3 = cs[k - 1], cs[k + 1]
            if side == "long":
                gap = c3["l"] - c1["h"]
                if gap >= minh and gap > 0:
                    top, bot = c3["l"], c1["h"]
                    return top, bot, (top + bot) / 2.0
            else:
                gap = c1["l"] - c3["h"]
                if gap >= minh and gap > 0:
                    top, bot = c1["l"], c3["h"]
                    return top, bot, (top + bot) / 2.0
        return None

    def _scan(self, ts):
        """SCAN -> SWEPT: detect a sweep of a pool that sits in discount (long) / premium (short)."""
        if self.bias is None or self.eq is None:
            return
        m = self._cs1m
        if len(m) < 5:
            return
        a1 = atr(m)
        if not a1:
            return
        minpen = self.MINPEN_ATR * a1
        lows, highs = self._pools(ts)

        if self.bias == "long":
            # only longs in discount: price below EQ
            if m[-1]["c"] >= self.eq:
                return
            pools = sorted({round(l, 1) for l, _ in lows if l and l < m[-1]["c"] + a1}, reverse=True)
            for L in pools:
                if L in self._blocked_pools:
                    continue
                # sweep bar in the last 3 closed candles
                for t in range(len(m) - 3, len(m) - 1):
                    if t < 1:
                        continue
                    bar = m[t]
                    if bar["l"] < L - minpen:
                        reclaim = bar["c"] > L or (t + 1 < len(m) and m[t + 1]["c"] > L)
                        if not reclaim:
                            continue                       # close stayed below = breakout, stand down
                        raid = min(bar["l"], m[t + 1]["l"]) if t + 1 < len(m) else bar["l"]
                        self.setup = {"pool": L, "raid": raid, "sweep_idx": t}
                        self.phase = "SWEPT"
                        self._note(f"SWEPT {L:,.1f} (pool) · raid_low {raid:,.1f} · {self.kz} · bias LONG", "open")
                        return
        else:
            if m[-1]["c"] <= self.eq:
                return
            pools = sorted({round(h, 1) for h, _ in highs if h and h > m[-1]["c"] - a1})
            for L in pools:
                if L in self._blocked_pools:
                    continue
                for t in range(len(m) - 3, len(m) - 1):
                    if t < 1:
                        continue
                    bar = m[t]
                    if bar["h"] > L + minpen:
                        reclaim = bar["c"] < L or (t + 1 < len(m) and m[t + 1]["c"] < L)
                        if not reclaim:
                            continue
                        raid = max(bar["h"], m[t + 1]["h"]) if t + 1 < len(m) else bar["h"]
                        self.setup = {"pool": L, "raid": raid, "sweep_idx": t}
                        self.phase = "SWEPT"
                        self._note(f"SWEPT {L:,.1f} (pool) · raid_high {raid:,.1f} · {self.kz} · bias SHORT", "open")
                        return

    def _swept_to_armed(self):
        """SWEPT -> ARMED: define the short-term high/low, then require an MSS body-close through it
        with a valid displacement leg and an FVG inside it. Build entry/stop/targets + R:R gate."""
        m = self._cs1m
        s = self.setup
        si = s["sweep_idx"]
        if self.bias == "long":
            # STH = most recent fractal high on the down-leg into the pool, just before the sweep
            sth_idx = None
            for i in range(si - 1, max(0, si - 12), -1):
                if 1 <= i < len(m) - 1 and m[i]["h"] > m[i - 1]["h"] and m[i]["h"] > m[i + 1]["h"]:
                    sth_idx = i
                    break
            if sth_idx is None:
                return
            sth = m[sth_idx]["h"]
            # MSS: first candle AFTER the sweep whose BODY closes above the STH (wicks don't count)
            mss_idx = None
            for i in range(si + 1, len(m)):
                if max(m[i]["o"], m[i]["c"]) and m[i]["c"] > sth and m[i]["o"] < m[i]["c"]:
                    if m[i]["c"] > sth:                    # body close above
                        mss_idx = i
                        break
            if mss_idx is None:
                return
            mss = m[mss_idx]
            # displacement filter
            a14 = atr(m) or 0.0
            rng = mss["h"] - mss["l"]
            body = abs(mss["c"] - mss["o"])
            leg = mss["h"] - s["raid"]
            disp = (rng >= self.DISP_ATR_MULT * a14 and rng > 0 and body >= self.DISP_BODY_FRAC * rng) \
                or (leg >= self.DISP_LEG_ATR * a14)
            if not disp:
                self._note(f"MSS at {mss['c']:,.1f} but no displacement (grind) → stand down", "skip")
                self._reset_setup()
                return
            # FVG inside the displacement leg (sweep_idx .. mss_idx)
            fvg = self._find_fvg(m, max(0, si - 1), mss_idx, "long")
            if not fvg:
                self._note("displacement but no qualifying FVG in the leg → stand down", "skip")
                self._reset_setup()
                return
            top, bot, ce = fvg
            entry = ce
            a5 = atr(self._cs5m) or a14
            buffer = self.BUFFER_ATR * a14
            stop = s["raid"] - buffer
            if entry <= stop:
                self._reset_setup(); return
            if (entry - stop) > self.STOP_MAX_ATR5 * a5:
                self._note("stop distance > 1.5*ATR(5m) — sweep too violent → skip", "skip")
                self._reset_setup(); return
            if entry >= self.eq:                            # entry must be in discount
                self._note("FVG entry not in discount (>= EQ) → skip", "skip")
                self._reset_setup(); return
            tp2_pool = self._opposing_pool("long", entry)
            tp2 = (tp2_pool - self.FRONTRUN_TICKS * TICK) if tp2_pool else None
            if tp2 is None:
                self._reset_setup(); return
            rr = (tp2 - entry) / (entry - stop)
            if rr < self.MIN_RR:
                self._note(f"structural R:R {rr:.2f} < {self.MIN_RR} → skip", "skip")
                self._reset_setup(); return
            tp1 = min(entry + 2 * (entry - stop), self.eq) if self.eq > entry else entry + 2 * (entry - stop)
            self.setup.update({"sth": sth, "mss_idx": mss_idx, "fvg_top": top, "fvg_bot": bot,
                               "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                               "armed_bar": len(m) - 1, "rr": rr})
            self.phase = "ARMED"
            self._note(f"ARMED LONG · entry(CE) {entry:,.1f} · stop {stop:,.1f} · "
                       f"TP1 {tp1:,.1f} · TP2 {tp2:,.1f} · R:R {rr:.2f}", "open")
        else:
            sth_idx = None
            for i in range(si - 1, max(0, si - 12), -1):
                if 1 <= i < len(m) - 1 and m[i]["l"] < m[i - 1]["l"] and m[i]["l"] < m[i + 1]["l"]:
                    sth_idx = i
                    break
            if sth_idx is None:
                return
            stl = m[sth_idx]["l"]
            mss_idx = None
            for i in range(si + 1, len(m)):
                if m[i]["c"] < stl and m[i]["o"] > m[i]["c"]:
                    mss_idx = i
                    break
            if mss_idx is None:
                return
            mss = m[mss_idx]
            a14 = atr(m) or 0.0
            rng = mss["h"] - mss["l"]
            body = abs(mss["c"] - mss["o"])
            leg = s["raid"] - mss["l"]
            disp = (rng >= self.DISP_ATR_MULT * a14 and rng > 0 and body >= self.DISP_BODY_FRAC * rng) \
                or (leg >= self.DISP_LEG_ATR * a14)
            if not disp:
                self._note(f"MSS at {mss['c']:,.1f} but no displacement (grind) → stand down", "skip")
                self._reset_setup(); return
            fvg = self._find_fvg(m, max(0, si - 1), mss_idx, "short")
            if not fvg:
                self._note("displacement but no qualifying FVG in the leg → stand down", "skip")
                self._reset_setup(); return
            top, bot, ce = fvg
            entry = ce
            a5 = atr(self._cs5m) or a14
            buffer = self.BUFFER_ATR * a14
            stop = s["raid"] + buffer
            if entry >= stop:
                self._reset_setup(); return
            if (stop - entry) > self.STOP_MAX_ATR5 * a5:
                self._note("stop distance > 1.5*ATR(5m) — sweep too violent → skip", "skip")
                self._reset_setup(); return
            if entry <= self.eq:
                self._note("FVG entry not in premium (<= EQ) → skip", "skip")
                self._reset_setup(); return
            tp2_pool = self._opposing_pool("short", entry)
            tp2 = (tp2_pool + self.FRONTRUN_TICKS * TICK) if tp2_pool else None
            if tp2 is None:
                self._reset_setup(); return
            rr = (entry - tp2) / (stop - entry)
            if rr < self.MIN_RR:
                self._note(f"structural R:R {rr:.2f} < {self.MIN_RR} → skip", "skip")
                self._reset_setup(); return
            tp1 = max(entry - 2 * (entry - stop), self.eq) if self.eq < entry else entry - 2 * (entry - stop)
            # entry-stop is negative for shorts above; recompute risk magnitude correctly
            tp1 = max(entry - 2 * (stop - entry), self.eq) if self.eq < entry else entry - 2 * (stop - entry)
            self.setup.update({"sth": stl, "mss_idx": mss_idx, "fvg_top": top, "fvg_bot": bot,
                               "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
                               "armed_bar": len(m) - 1, "rr": rr})
            self.phase = "ARMED"
            self._note(f"ARMED SHORT · entry(CE) {entry:,.1f} · stop {stop:,.1f} · "
                       f"TP1 {tp1:,.1f} · TP2 {tp2:,.1f} · R:R {rr:.2f}", "open")

    def _opposing_pool(self, side, entry):
        """The nearest external pool in the trade's direction (the draw): a high above for longs,
        a low below for shorts. Falls back to the dealing-range extreme (ERL)."""
        lows, highs = self._pools(time.time())
        if side == "long":
            cand = [h for h, _ in highs if h and h > entry]
            return min(cand) if cand else (self.rh if self.rh and self.rh > entry else None)
        cand = [l for l, _ in lows if l and l < entry]
        return max(cand) if cand else (self.rl if self.rl and self.rl < entry else None)

    def _reset_setup(self):
        self.phase = "SCAN"
        self.setup = {}

    # ============================ execution / fills (section 3) ============================
    def _try_fill(self):
        """ARMED/PENDING: the CE limit. Fill when a 1m candle trades through the CE. Pre-fill
        invalidation: body-close beyond the FVG (inverted gap), 20-bar expiry, or a 2R run away."""
        if self.phase not in ("ARMED", "PENDING"):
            return
        self.phase = "PENDING"
        m = self._cs1m
        s = self.setup
        last = m[-1]
        entry, stop = s["entry"], s["stop"]

        # expiry: 20 bars after the MSS
        if len(m) - 1 - s.get("armed_bar", len(m) - 1) > self.SETUP_EXPIRY_BARS:
            self._note("setup expired (20 bars, unfilled) → cancel", "skip")
            self._reset_setup(); return

        if self.bias == "long":
            # inverted gap: a body close below the FVG bottom kills it
            if last["c"] < s["fvg_bot"]:
                self._note("body closed below FVG (inverted gap) → cancel", "skip")
                self._reset_setup(); return
            # 2R move toward target before fill → cancelled (missed it)
            if last["h"] >= entry + 2 * (entry - stop):
                self._note("ran 2R toward target without filling → cancel", "skip")
                self._reset_setup(); return
            if last["l"] <= entry:                          # limit touched → fill at CE
                self._open("long", entry, stop, s["tp1"], s["tp2"], s["raid"], s["pool"])
        else:
            if last["c"] > s["fvg_top"]:
                self._note("body closed above FVG (inverted gap) → cancel", "skip")
                self._reset_setup(); return
            if last["l"] <= entry - 2 * (stop - entry):
                self._note("ran 2R toward target without filling → cancel", "skip")
                self._reset_setup(); return
            if last["h"] >= entry:
                self._open("short", entry, stop, s["tp1"], s["tp2"], s["raid"], s["pool"])

    def _open(self, side, entry, stop, tp1, tp2, raid, pool):
        if not self._can_trade_today():
            self._note("daily risk gate hit (2 trades or -2R) → no entry", "skip")
            self._reset_setup(); return
        risk_per_unit = abs(entry - stop)
        if risk_per_unit <= 0:
            self._reset_setup(); return
        risk_usd = self.equity() * self.RISK_PCT
        qty = risk_usd / risk_per_unit
        self.pos = ICTPos(id=uuid.uuid4().hex[:6], side=side, entry=entry, qty=qty, qty0=qty,
                          stop=stop, tp1=tp1, tp2=tp2, raid=raid, R=risk_per_unit, risk_usd=risk_usd,
                          pool=pool, opened_at=time.time())
        self._trades_today += 1
        self.phase = "FILLED"
        self._note(f"FILLED {side.upper()} @ {entry:,.1f} · {qty:.5f} BTC · risk ${risk_usd:.2f} "
                   f"· stop {stop:,.1f} · TP1 {tp1:,.1f} · TP2 {tp2:,.1f}", "open")
        self._save()

    def _manage_fill(self, px):
        """FILLED: walk the open position against the live mark `px` — stop, TP1 (50% + BE), TP2.
        Fills are booked at the level price (limit-style) for a clean paper result."""
        p = self.pos
        if p is None or not px:
            return
        if p.side == "long":
            if px <= p.stop:                                # stop / breakeven
                self._close(p.stop, "stop_loss" if not p.be else "breakeven")
                return
            if not p.half and px >= p.tp1:                  # TP1: bank 50%, move stop to BE+1tick
                self._partial(p.tp1)
                p.stop = p.entry + TICK
                p.be = True
                p.half = True
                self._note(f"TP1 {p.tp1:,.1f} · 50% out · stop → breakeven {p.stop:,.1f}", "win")
            if px >= p.tp2:                                 # TP2: final
                self._close(p.tp2, "take_profit_TP2")
        else:
            if px >= p.stop:
                self._close(p.stop, "stop_loss" if not p.be else "breakeven")
                return
            if not p.half and px <= p.tp1:
                self._partial(p.tp1)
                p.stop = p.entry - TICK
                p.be = True
                p.half = True
                self._note(f"TP1 {p.tp1:,.1f} · 50% out · stop → breakeven {p.stop:,.1f}", "win")
            if px <= p.tp2:
                self._close(p.tp2, "take_profit_TP2")

    def _partial(self, price):
        p = self.pos
        half = p.qty0 * 0.5
        pnl = half * (price - p.entry) if p.side == "long" else half * (p.entry - price)
        self.balance += pnl
        self._day_R += pnl / p.risk_usd if p.risk_usd else 0.0
        p.qty -= half

    def _close(self, price, reason):
        p = self.pos
        pnl = p.unrealized(price)
        self.balance += pnl
        r_mult = (pnl) / p.risk_usd if p.risk_usd else 0.0
        # add the closing leg's R to the day; the partial already added its own
        self._day_R += r_mult
        if reason in ("stop_loss",):
            self._blocked_pools.add(round(p.pool, 1))       # no re-entry on this pool after a stop
        rec = {"id": p.id, "side": p.side, "entry": round(p.entry, 1), "exit": round(price, 1),
               "qty": round(p.qty0, 5), "pnl": round(pnl, 2), "reason": reason,
               "rr": round(r_mult, 2), "opened_at": p.opened_at, "closed_at": time.time()}
        self.history.insert(0, rec)
        self.history = self.history
        self._note(f"CLOSE {p.side.upper()} @ {price:,.1f} · {reason} · P&L ${pnl:+.2f} "
                   f"({r_mult:+.2f}R) · day {self._day_R:+.2f}R", "win" if pnl >= 0 else "loss")
        self.pos = None
        self._reset_setup()
        self._save()

    # ============================ main loop ============================
    async def _refresh_candles(self, now):
        """Fetch candles on a GENTLE, staggered cadence so we never trip Lighter's rate limit
        (23000 Too Many Requests). 1m is the only fast one; 5m/1h barely move bar-to-bar. On a
        rate-limit error we back off ALL fetches for a minute and keep using the cached candles."""
        if now < self._rl_until:
            return
        try:
            if now - self._t1m >= 20:                        # 1m: ~3 requests/min (was 12/min)
                self._cs1m = await asyncio.to_thread(lighter_markets.candles, "BTC", "1m", 200)
                self._t1m = now
            elif now - self._t5m >= 90:                      # one TF per tick — never all three at once
                self._cs5m = await asyncio.to_thread(lighter_markets.candles, "BTC", "5m", 150)
                self._t5m = now
            elif now - self._t1h >= 300:
                self._cs1h = await asyncio.to_thread(lighter_markets.candles, "BTC", "1h", 200)
                self._t1h = now
            self._err = ""
        except Exception as e:
            msg = str(e)
            if "RateLimit" in type(e).__name__ or "23000" in msg or "Too Many" in msg:
                self._rl_until = now + 60.0
                self._err = "rate-limited by Lighter — backing off 60s (using cached candles)"
            else:
                self._err = f"{type(e).__name__}: {str(e)[:120]}"

    async def manage_loop(self):
        await asyncio.sleep(3.0)
        while True:
            await asyncio.sleep(15.0)                        # gentle loop; Lighter-friendly
            try:
                now = time.time()
                await self._refresh_candles(now)
                # Manage an OPEN position off the FREE live price every tick (no Lighter call),
                # so stops/TPs react fast even while candle fetches are throttled.
                px = self._btc()
                if self.pos is not None and px:
                    self._manage_fill(px)
                if not self._cs1m:
                    continue
                newest = self._cs1m[-1]["t"]
                # only advance the SCAN/ARM state machine on a NEWLY CLOSED 1m bar
                if newest == self._last_bar_t:
                    continue
                self._last_bar_t = newest
                ts = newest
                self._roll_day(ts)
                self.kz = self._killzone(ts)
                self._htf()                                  # refresh dealing range + bias each bar

                if self.pos is not None:
                    continue                                 # one position at a time
                if not self.enabled:
                    self._reset_setup(); continue
                if self.kz is None:
                    self._reset_setup(); continue            # outside killzones: stand down
                if not self._can_trade_today():
                    continue

                # state machine: SCAN -> SWEPT -> ARMED -> PENDING -> FILLED
                if self.phase == "SCAN":
                    self._scan(ts)
                if self.phase == "SWEPT":
                    self._swept_to_armed()
                if self.phase in ("ARMED", "PENDING"):
                    self._try_fill()
            except Exception as e:
                self._note(f"loop error: {type(e).__name__}: {str(e)[:100]}", "error")

    # ============================ controls / state ============================
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED — will paper-trade the ICT model on signals" if self.enabled
                   else "bot PAUSED — no new setups")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_strategy(self, name):
        # single strategy; kept for dashboard parity
        if name and name not in self.STRATEGIES:
            return {"ok": False, "error": f"unknown strategy '{name}'"}
        return {"ok": True, "strategy": self.strategy, "strategy_label": self.STRATEGIES[self.strategy]}

    def reset(self):
        self.balance = START_BALANCE
        self.start_balance = START_BALANCE
        self.pos = None
        self.history = []
        self.log = []
        self._reset_setup()
        self._blocked_pools.clear()
        self._trades_today = 0
        self._day_R = 0.0
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
                "qty": round(p.qty, 5), "stop": round(p.stop, 1),
                "tp1": round(p.tp1, 1), "tp2": round(p.tp2, 1),
                "raid": round(p.raid, 1), "half": p.half, "be": p.be,
                "pnl": round(up, 2),
                "pnl_R": round(up / p.risk_usd, 2) if p.risk_usd else 0.0,
                "news": f"swept {p.pool:,.1f} · stop {p.stop:,.1f} · TP2 {p.tp2:,.1f}",
            })
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        return {
            "name": self.NAME,
            "enabled": self.enabled,
            "running": True,
            "strategy": self.strategy,
            "strategy_label": self.STRATEGIES[self.strategy],
            "strategies": self.STRATEGIES,
            "phase": self.phase,
            "bias": self.bias,
            "killzone": self.kz,
            "price": round(px, 2) if px else None,
            "eq": round(self.eq, 1) if self.eq else None,
            "range_high": round(self.rh, 1) if self.rh else None,
            "range_low": round(self.rl, 1) if self.rl else None,
            "balance": round(self.balance, 2),
            "equity": round(self.equity(), 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(self.equity() - self.start_balance, 2),
            "trades": len(self.history),
            "wins": wins,
            "trades_today": self._trades_today,
            "day_R": round(self._day_R, 2),
            "data_error": self._err,
            "positions": positions,
            "history": self.history,
            "log": self.log[:25],
        }


class ICTFrequentBot(ICTBot):
    """A separate, more active ICT copy that keeps sweep->MSS->FVG but loosens strict gates."""

    NAME = "ICT 2022 model (frequent paper)"
    STRATEGY_KEY = "ict2022_frequent"
    STRATEGY_LABEL = "ICT frequent (expanded window, sweep -> MSS -> FVG)"
    STRATEGIES = {STRATEGY_KEY: STRATEGY_LABEL}

    RISK_PCT = 0.003
    MAX_TRADES_DAY = 6
    DAY_HALT_R = -2.0
    MIN_RR = 2.0
    SETUP_EXPIRY_BARS = 30
    DISP_ATR_MULT = 1.0
    DISP_BODY_FRAC = 0.45
    DISP_LEG_ATR = 1.25
    FVG_MIN_ATR = 0.08
    MINPEN_ATR = 0.05
    BUFFER_ATR = 0.08
    STOP_MAX_ATR5 = 2.25

    def _path(self):
        return os.path.join("data", "ict_bot_frequent_state.json")

    def _killzone(self, ts):
        d = _ny(ts)
        if d.weekday() >= 5:
            return None
        mins = d.hour * 60 + d.minute
        if 60 <= mins < 1020:
            return "Active"
        return None

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - frequent ICT paper copy" if self.enabled
                   else "bot PAUSED - no new setups")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        out = super().reset()
        self.log = []
        self._note("bot reset (frequent ICT paper account back to $%.0f)" % START_BALANCE)
        self._save()
        return out

    def state(self, **_):
        s = super().state(**_)
        s["active_window"] = "NY 01:00-17:00 weekdays"
        s["min_rr"] = self.MIN_RR
        s["risk_pct"] = self.RISK_PCT
        s["max_trades_day"] = self.MAX_TRADES_DAY
        return s
