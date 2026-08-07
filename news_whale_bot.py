"""
================================================================================
 news_whale_bot.py  —  "NEWS + WHALE BOT"  (fast, mechanical, no AI)
================================================================================
A third, SEPARATE paper account that trades BTC automatically. It does NOT use
the AI. The logic is mechanical, fast, and based on CONFLUENCE of signals:

  1. A news message arrives -> the bot starts WATCHING (re-checks ~1x/sec) using
     only live in-memory data (no slow network calls), so it reacts fast.
  2. REACTION GATE — it only continues if the market is actually reacting to the
     news: BOTH volume AND volatility must be rising vs the pre-news baseline.
  3. DIRECTION — it then votes with four signals: taker flow (buy vs sell %),
     CVD (net buying/selling since the news), whale prints (net big trades), and
     price move since the news. If a net majority (>= NW_ENTER_VOTES) agree, it
     opens a market position that way. (flow/CVD/whale all come from the same
     trade tape, so they usually agree — together they confirm one thing: is the
     tape net buying or selling.)
  4. It then EXITS on the same signals, not a fixed target: a VOLATILITY trailing
     stop (trails the best price by a volatility-scaled distance — this both caps
     the loss and locks in profit as the trade runs your way) plus a FLOW-FLIP
     exit (it bails when flow / CVD / whale prints / price turn against the
     position). A liquidation backstop and a max-hold time-stop sit underneath.
  5. Every entry, exit, skip, profit and loss is logged (console + the UI panel
     + appended to data/news_whale_trades.csv). Tuning knobs are at the top of
     this file (sizing NW_RISK_FRAC, entry NW_ENTER_VOTES, exit NW_EXIT_VOTES /
     NW_TRAIL_VOL_MULT, ...).

Everything is paper money and completely separate from the manual account and
from the AI news bot. Honest caveat: a retail news+flow bot is racing real HFT;
this is a hypothesis you can watch and measure, not a money printer.
================================================================================
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict

import config
import orderbook

CSV_PATH = os.path.join(config.DATA_DIR, "news_whale_trades.csv")
STATE_PATH = os.path.join(config.DATA_DIR, "news_whale_state.json")

# ---- selectable strategies (shown in the website dropdown) -------------------
STRATEGIES = {
    "whale_follow": "Whale Follow (≥$250k)",  # FOLLOW big tape prints with fixed SL/TP (default)
    "whale_follow_100k": "Whale Follow 100k (flow stop)",  # ≥$100k prints; stop = 30s flow flip
    "news_whale": "News + Whale",   # original mechanical confluence (gate + flow votes)
    "gemini":     "Gemini AI",      # AI (Gemini) reads the news and decides side + SL/TP
    "gptnews":    "GPTnews",        # AI (Groq / gpt-oss-120b) — same idea, different model
}
DEFAULT_STRATEGY = "whale_follow"

# ---- confluence tuning (edit HERE, not config.py) ---------------------------
NW_CHECK_SECONDS   = 0.5      # how often to re-check during the watch (faster = quicker entry)
NW_MIN_ELAPSED     = 3        # secs of post-news data needed before judging (can't measure 0s)
NW_BASELINE_MIN    = 15       # minutes of recent history that define the market's "normal"
NW_REACT_MULT      = 1.4      # ADAPTIVE GATE: the move right after the news must be >= this many
                              #   times the market's recent NORMAL (volume AND volatility). LOWER
                              #   catches smaller moves (more trades, more noise); HIGHER = big only
NW_ENTER_VOTES     = 2        # net direction votes (of 4) needed to enter. LOWER = more trades

# --- AI strategies (gemini / gptnews) ---
# Every headline is sent to the AI, which itself decides skip-or-trade. We only
# BOUND how many analyses run at once (so a news burst can't spawn unbounded tasks)
# — we never single-flight them into a gate that silently drops the rest and could
# leave the bot stuck "watching" forever after one busy moment.
NW_MAX_INFLIGHT    = 4        # max concurrent AI analyses in flight
NW_AI_TIMEOUT      = 12.0     # hard cap (s) on ONE AI decision; the bot can never hang on it

# --- money ---
NW_RISK_FRAC       = 1.0      # margin per trade as a fraction of balance (1.0 = bet it ALL)

# --- exits: signal-driven, NOT fixed % (these replace the old SL/TP) ---
NW_EXIT_VOTES      = 0        # exit when the direction votes fade to this or below (flow flipped)
NW_EXIT_CONFIRM    = 3        # the flip must hold this many seconds before exiting (anti-whipsaw)
NW_TRAIL_VOL_MULT  = 3.0      # volatility trailing stop distance = this x current volatility ...
NW_TRAIL_MIN_PCT   = 0.002    #   ... but never tighter than 0.2% behind the best price
NW_TRAIL_MAX_PCT   = 0.02     #   ... and never wider than 2%
try:
    NW_BIG_TRADE_USD = float(config.BIG_TRADE_USD)   # what counts as a "whale print"
except Exception:
    NW_BIG_TRADE_USD = 100000.0

# --- WHALE FOLLOW strategy: copy any single big print on the live cross-exchange tape ---
# When a trade >= WHALE_FOLLOW_MIN_USD prints (buy -> LONG, sell -> SHORT), open a paper
# position that way and manage it with a FIXED stop-loss + take-profit (the user asked for a
# clear exit rule). A cooldown stops it firing on every print in a burst; a max-hold cuts a
# trade whose move never came. Tune all of it here. (Honest note: copying tape prints as a
# taker is a momentum scalp racing real HFT — this is a paper hypothesis to MEASURE, not a
# guaranteed edge; watch the win rate before ever trusting it.)
WHALE_FOLLOW_MIN_USD  = 250000   # follow any single trade at least this big (USD)
WHALE_FOLLOW_TP_USDT  = 1.50     # TAKE-PROFIT: close the trade when its P&L reaches +$1.50
WHALE_FOLLOW_SL_USDT  = 0.80     # STOP-LOSS:   close the trade when its P&L reaches -$0.80
WHALE_FOLLOW_COOLDOWN = 30       # seconds to wait after a trade before following the next print
WHALE_FOLLOW_MAX_HOLD = 600      # if neither SL nor TP hits within this many secs, cut it (10 min)

# --- WHALE FOLLOW 100k: open on any >= $100k print, then ride it with NO fixed take-profit and
#     NO fixed stop-loss — the ONLY exit is the 30-second order flow. While you're long, if the
#     buy-flow over the last 30s falls below 60% (the tape turned), close instantly; mirror for a
#     short. So it holds as long as the flow keeps pushing your way and bails the moment it flips. ---
WHALE100_MIN_USD    = 100000     # follow any single trade at least this big (USD)
WHALE100_FLOW_WINDOW = 30        # seconds of tape used for the flow exit
WHALE100_FLOW_EXIT  = 0.60       # EXIT the instant the flow on YOUR side over 30s falls below 60%
WHALE100_COOLDOWN   = 30         # seconds to wait after a close before following the next print
WHALE100_MAX_HOLD   = 1800       # pure-safety backstop (30 min) — the flow is the real exit
WHALE100_GRACE_SEC  = 2          # tiny anti-noise grace right after entry (so it can't self-close)

# --- Lighter-realistic execution for Whale Follow 100k paper ------------------
WHALE100_LIGHTER_EXECUTION = True
WHALE100_LIGHTER_MAX_AGE_SEC = 6.0
WHALE100_LIGHTER_ENTRY_SLIP_USD = 2.0
WHALE100_LIGHTER_EXIT_SLIP_USD = 2.0
WHALE100_LIGHTER_MAX_ADVERSE_BASIS_USD = 4.0
WHALE100_LIGHTER_MARGIN_BUFFER = 0.95


@dataclass
class NWPosition:
    id: str
    side: str            # "long" | "short"
    entry: float
    qty: float           # BTC
    margin: float
    leverage: float
    opened_at: float
    entry_flow: float    # dominant-side fraction at entry (e.g. 0.61)
    news: str            # the headline that triggered it
    peak: float = 0.0    # best price reached since entry (for the trailing stop)
    sl: float = 0.0      # stop-loss PRICE (Gemini strategy sets this; 0 = unused)
    tp: float = 0.0      # take-profit PRICE (Gemini strategy sets this; 0 = unused)
    exec_model: str = ""
    signal_price: float = 0.0
    entry_drift: float = 0.0

    def unrealized(self, mark: float) -> float:
        if self.side == "long":
            return self.qty * (mark - self.entry)
        return self.qty * (self.entry - mark)

    def liq_price(self) -> float:
        if self.side == "long":
            return self.entry * (1 - 1 / self.leverage)
        return self.entry * (1 + 1 / self.leverage)


class NewsWhaleBot:
    def __init__(self, market, whales):
        self.market = market           # MarketData (live BTC price)
        self.whales = whales           # orderbook.WhaleTracker (live flow)
        self.enabled = config.NW_ENABLED
        self.strategy = DEFAULT_STRATEGY   # which entry/exit ruleset to use (see STRATEGIES)
        self.balance = float(config.NW_START_BALANCE)
        self.start_balance = float(config.NW_START_BALANCE)
        self.positions: dict[str, NWPosition] = {}
        self.history: list[dict] = []
        self.log: list[dict] = []
        self._watching = False            # single-flight guard for the MECHANICAL watch only
        self._inflight = 0                # count of AI analyses currently running (AI strategies)
        self._flip: dict[str, int] = {}   # per-position: consecutive secs the exit signal held
        self._wf_last_seq = None        # last tape seq seen by whale-follow (None = start fresh)
        self._wf_cooldown_ts = 0.0      # last whale-follow entry time (for the cooldown)
        self._exec_skips = 0
        self._exec_last = ""
        self._exec_last_px = None
        self._exec_last_age = None
        self._exec_last_note_ts = 0.0
        self._load()                   # restore balance / trades / win rate

    # ---- persistence: survive restarts ----
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "balance": self.balance,
                    "start_balance": self.start_balance,
                    "enabled": self.enabled,
                    "strategy": self.strategy,
                    "history": self.history,
                    "log": self.log[:60],
                    "positions": [asdict(p) for p in self.positions.values()],
                    "exec_skips": self._exec_skips,
                    "exec_last": self._exec_last,
                }, f)
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
            saved_strat = d.get("strategy")
            if saved_strat in STRATEGIES:
                self.strategy = saved_strat
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self._exec_skips = int(d.get("exec_skips", self._exec_skips) or 0)
            self._exec_last = str(d.get("exec_last", self._exec_last) or "")
            for pd in d.get("positions", []) or []:
                try:
                    p = NWPosition(**pd)
                    self.positions[p.id] = p
                except Exception:
                    pass
            print(f"[news+whale] restored: balance ${self.balance:.2f}, "
                  f"{len(self.history)} past trades, {len(self.positions)} open")
        except Exception:
            pass

    # ---- helpers ----
    def _btc(self):
        try:
            return self.market.price("BTCUSDT")
        except Exception:
            return None

    def _uses_lighter_exec(self):
        return bool(WHALE100_LIGHTER_EXECUTION and self.strategy == "whale_follow_100k")

    def _lighter_last_price(self):
        """Fresh Lighter trade price from the shared tape, used as a top-of-book proxy."""
        now_ms = time.time() * 1000.0
        for t in reversed(self.whales.tape):
            if t.get("ex") != "lighter":
                continue
            try:
                px = float(t.get("price") or 0)
                ts = float(t.get("ts") or 0)
            except Exception:
                return None, None
            age = max(0.0, (now_ms - ts) / 1000.0)
            if px > 0 and age <= WHALE100_LIGHTER_MAX_AGE_SEC:
                self._exec_last_px = px
                self._exec_last_age = age
                return px, age
            return None, age
        return None, None

    def _exec_skip(self, msg, save=False):
        self._exec_skips += 1
        self._exec_last = msg
        now = time.time()
        if now - self._exec_last_note_ts >= 5.0:
            self._exec_last_note_ts = now
            self._note(msg, "skip")
            if save:
                self._save()

    def _mark_price(self):
        if self._uses_lighter_exec():
            px, _age = self._lighter_last_price()
            if px:
                return px
            return None
        return self._btc()

    def _lighter_entry_fill(self, side, signal_px):
        px, age = self._lighter_last_price()
        if not px:
            age_txt = "unknown" if age is None else f"{age:.1f}s old"
            self._exec_skip(f"Lighter paper entry skipped: no fresh Lighter price ({age_txt})", True)
            return None
        if side == "long":
            fill = px + WHALE100_LIGHTER_ENTRY_SLIP_USD
            adverse = fill - float(signal_px or px)
        else:
            fill = px - WHALE100_LIGHTER_ENTRY_SLIP_USD
            adverse = float(signal_px or px) - fill
        if adverse > WHALE100_LIGHTER_MAX_ADVERSE_BASIS_USD:
            self._exec_skip(
                f"Lighter paper entry skipped: adverse basis ${adverse:.2f} > "
                f"${WHALE100_LIGHTER_MAX_ADVERSE_BASIS_USD:.2f}", True)
            return None
        self._exec_last = (f"Lighter fill {side} ${fill:,.1f} "
                           f"(tape ${px:,.1f}, age {age:.1f}s, drift ${adverse:+.2f})")
        return fill, adverse, age

    def _lighter_exit_fill(self, pos, fallback_px=None, quiet=False):
        if getattr(pos, "exec_model", "") != "lighter_sim":
            return fallback_px
        px, age = self._lighter_last_price()
        if not px:
            if not quiet:
                age_txt = "unknown" if age is None else f"{age:.1f}s old"
                self._exec_skip(f"Lighter paper exit waiting: no fresh Lighter price ({age_txt})")
            return None
        if pos.side == "long":
            return px - WHALE100_LIGHTER_EXIT_SLIP_USD
        return px + WHALE100_LIGHTER_EXIT_SLIP_USD

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[news+whale] {msg}")

    # ---- live in-memory signals (fast: newest-first, stop early; no network) ----
    def _vol_usd(self, t_from, t_to):
        """Taker volume (USDT, >=$10k trades, all exchanges) between two epoch secs."""
        lo_ms, hi_ms, stop_ms = t_from * 1000, t_to * 1000, t_from * 1000 - 15000
        s = 0.0
        for t in reversed(self.whales.tape):
            ts = t["ts"]
            if ts < stop_ms:
                break
            if lo_ms <= ts <= hi_ms:
                s += t["usd"]
        return s

    def _cvd_since(self, t0):
        """Cumulative (buy - sell) USD since t0 secs. >0 = net buying."""
        cut_ms, stop_ms = t0 * 1000, t0 * 1000 - 15000
        s = 0.0
        for t in reversed(self.whales.tape):
            ts = t["ts"]
            if ts < stop_ms:
                break
            if ts >= cut_ms:
                s += t["usd"] if t["side"] == "buy" else -t["usd"]
        return s

    def _whale_net_since(self, t0):
        """Net 'whale print' USD (only the big trades) since t0. >0 = whales buying."""
        cut_ms, stop_ms = t0 * 1000, t0 * 1000 - 15000
        s = 0.0
        for t in reversed(self.whales.tape):
            ts = t["ts"]
            if ts < stop_ms:
                break
            if ts >= cut_ms and t["usd"] >= NW_BIG_TRADE_USD:
                s += t["usd"] if t["side"] == "buy" else -t["usd"]
        return s

    def _vol_pct(self, t_from, t_to):
        """Realized RANGE volatility from actual TRADE prices in [t_from, t_to]
        (high-low as % of mid). Newest-first scan, stops early. No mark-price
        smoothing, so it tracks the real swing you see on the candles."""
        lo_ms, hi_ms, stop_ms = t_from * 1000.0, t_to * 1000.0, t_from * 1000.0 - 15000.0
        lo = hi = None
        for t in reversed(self.whales.tape):
            ts = t["ts"]
            if ts < stop_ms:
                break
            if lo_ms <= ts <= hi_ms:
                p = t["price"]
                if lo is None or p < lo:
                    lo = p
                if hi is None or p > hi:
                    hi = p
        if lo is None or lo <= 0:
            return None
        mid = (lo + hi) / 2.0
        return (hi - lo) / mid * 100.0 if mid > 0 else None

    def _baseline(self, t0):
        """The market's NORMAL over the prior ~NW_BASELINE_MIN minutes (ending a few
        secs before the news): average per-minute price range (volatility) and average
        per-minute volume. Returns (vol_per_min_pct, volume_per_min_usd), or (None, None)
        if there isn't enough history yet (need >= 2 minutes of trades)."""
        end = t0 - 5.0
        start = t0 - NW_BASELINE_MIN * 60.0
        stop_ms = start * 1000.0 - 15000.0
        rng = {}     # minute bucket -> [lo, hi]
        vol = {}     # minute bucket -> volume usd
        for t in reversed(self.whales.tape):
            ts = t["ts"]
            if ts < stop_ms:
                break
            s = ts / 1000.0
            if start <= s <= end:
                m = int(s // 60)
                p = t["price"]
                lh = rng.get(m)
                if lh is None:
                    rng[m] = [p, p]
                else:
                    if p < lh[0]:
                        lh[0] = p
                    if p > lh[1]:
                        lh[1] = p
                vol[m] = vol.get(m, 0.0) + t["usd"]
        if len(rng) < 2:
            return None, None
        ranges = [(hi - lo) / ((hi + lo) / 2.0) * 100.0 for lo, hi in rng.values() if (hi + lo) > 0]
        vol_per_min = (sum(ranges) / len(ranges)) if ranges else None
        volume_per_min = (sum(vol.values()) / len(vol)) if vol else None
        return vol_per_min, volume_per_min

    def _price_ago(self, secs):
        """Price roughly `secs` seconds ago, from the live feed (None if unknown)."""
        try:
            hist = self.market._hist.get("BTCUSDT")
        except Exception:
            hist = None
        if not hist:
            return None
        target = time.time() - secs
        best = None
        for ts, p in list(hist):
            if ts <= target:
                best = p
            else:
                break
        return best

    def _recent_votes(self, window):
        """Net direction votes over a TRAILING window (used to time the exit).
        Same four signals as the entry: flow, CVD, whale prints, recent price."""
        now = time.time()
        buy, sell = self.whales.flow(window)
        tot = buy + sell
        buy_pct = buy / tot if tot > 0 else 0.5
        cvd = self._cvd_since(now - window)
        wnet = self._whale_net_since(now - window)
        px = self._btc()
        then = self._price_ago(window)
        moved = (px - then) / then if (px and then) else 0.0
        v = 0
        v += 1 if buy_pct >= config.NW_FLOW_ENTER else (-1 if (1 - buy_pct) >= config.NW_FLOW_ENTER else 0)
        v += 1 if cvd > 0 else (-1 if cvd < 0 else 0)
        v += 1 if wnet > 0 else (-1 if wnet < 0 else 0)
        v += 1 if moved >= config.NW_MIN_MOVE_PCT else (-1 if moved <= -config.NW_MIN_MOVE_PCT else 0)
        return v, buy_pct, moved

    # ---- triggered by every news message (fast, non-blocking) ----
    async def on_news(self, source, text):
        if not self.enabled:
            return
        if self.strategy in ("whale_follow", "whale_follow_100k"):
            return                       # tape-driven, not news-driven — ignore news entirely
        if not (text and text.strip()):
            return
        if self.strategy in ("gemini", "gptnews"):
            # AI strategies: send EVERY headline to the AI (it decides skip/trade).
            # Bound concurrency instead of single-flighting, so a burst of headlines
            # all get analysed and the bot can NEVER get stuck "watching" and go quiet.
            if self._inflight >= NW_MAX_INFLIGHT:
                self._note(f"AI busy ({self._inflight} analysing) → skipped: "
                           f"{text.strip()[:50]}", "skip")
                return
            if self.strategy == "gemini":
                import gemini_trader
                decide_fn, tag = gemini_trader.decide, "Gemini"
            else:
                import gpt_news
                decide_fn, tag = gpt_news.decide, "GPTnews"
            asyncio.create_task(self._watch_ai(source, text, decide_fn, tag))
        else:
            # Mechanical strategy: one watch at a time (it holds a multi-second window).
            if self._watching or len(self.positions) >= config.NW_MAX_CONCURRENT:
                return
            asyncio.create_task(self._watch(source, text))

    def _ai_snapshot(self, px):
        """Tiny live context handed to the AI alongside the headline."""
        then = self._price_ago(60)
        chg = round((px - then) / then * 100, 3) if (px and then) else None
        try:
            buy, sell = self.whales.flow(60)
            tot = buy + sell
            flow = round(buy / tot * 100, 1) if tot > 0 else None
        except Exception:
            flow = None
        return {"price": round(px, 2) if px else None, "change_1m_pct": chg, "flow_buy_pct": flow}

    async def _watch_ai(self, source, text, decide_fn, tag):
        """AI strategies ('Gemini AI' / 'GPTnews'): send the headline to the AI the
        instant it arrives. The AI decides trade/skip, direction, and the stop-loss +
        take-profit. If it says trade, open a paper position with that plan; manage_loop
        then exits on the AI's SL/TP. `decide_fn` is the model-specific analyzer."""
        self._inflight += 1
        try:
            headline = (text or "").strip().replace("\n", " ")[:200]
            px = self._btc()
            if not px:
                self._note(f"{tag}: no BTC price yet → skipped: {headline[:60]}", "skip")
                return
            try:                              # hard cap so a slow/hung API can't stall the bot
                d = await asyncio.wait_for(decide_fn(headline, self._ai_snapshot(px)),
                                           timeout=NW_AI_TIMEOUT)
            except asyncio.TimeoutError:
                self._note(f"{tag} timed out after {NW_AI_TIMEOUT:g}s → skipped: {headline[:60]}",
                           "error")
                return
            if d.error:
                self._note(f"{tag} error ({d.error[:70]}) → skipped: {headline[:60]}", "error")
                return
            if not d.trade or d.direction not in ("long", "short"):
                self._note(f"{tag} → SKIP ({d.reason[:60]}) [{d.latency_ms:.0f}ms]: {headline[:60]}",
                           "skip")
                return
            # Analysis always ran; enforce the open-position cap only HERE, at the
            # point of actually opening — so a held position never blocks the AI feed.
            if len(self.positions) >= config.NW_MAX_CONCURRENT:
                self._note(f"{tag} wanted {d.direction.upper()} but already at "
                           f"{len(self.positions)} open → skipped", "skip")
                return
            px2 = self._btc() or px
            if d.direction == "long":
                sl = px2 * (1 - d.stop_pct / 100.0)
                tp = px2 * (1 + d.tp_pct / 100.0)
            else:
                sl = px2 * (1 + d.stop_pct / 100.0)
                tp = px2 * (1 - d.tp_pct / 100.0)
            detail = (f"{tag} {d.confidence} · SL {d.stop_pct:.2f}% TP {d.tp_pct:.2f}% · "
                      f"{d.reason[:50]} [{d.latency_ms:.0f}ms]")
            self._open(d.direction, px2, 0.5, detail, sl=sl, tp=tp, news=headline)
        except Exception as e:
            self._note(f"{tag} watch error: {type(e).__name__} {e}", "error")
        finally:
            self._inflight = max(0, self._inflight - 1)

    async def _watch(self, source, text):
        self._watching = True
        headline = (text or "").strip().replace("\n", " ")[:90]
        t0 = time.time()
        start_px = self._btc()
        base_vol, base_volume = self._baseline(t0)        # the market's recent NORMAL
        if not base_vol or not base_volume:
            self._note(f"news → no baseline yet (need ~2+ min of data) → skipped: {headline}", "skip")
            self._watching = False
            return
        self._note(f"news → watching for a move ≥ {NW_REACT_MULT:g}× normal "
                   f"(normal: {base_vol:.3f}%/min swing, ${base_volume:,.0f}/min): {headline}")
        try:
            while (time.time() - t0 < config.NW_WATCH_SECONDS and self.enabled
                   and len(self.positions) < config.NW_MAX_CONCURRENT):
                await asyncio.sleep(NW_CHECK_SECONDS)
                now = time.time()
                elapsed = now - t0
                px = self._btc()
                if not px or not start_px or elapsed < NW_MIN_ELAPSED:
                    continue

                # ---- ADAPTIVE GATE: is the reaction abnormal vs the recent normal? ----
                # Scale the short post-news window up to a per-minute figure so it is
                # comparable to the baseline. A normal-sized move lands near 1.0×; a real
                # reaction (small in a calm market, or big in any market) lands well above.
                post_range = self._vol_pct(t0, now)               # price swing since the news
                if post_range is None:
                    continue
                post_vol_pm = post_range * (60.0 / elapsed) ** 0.5
                post_volume_pm = self._vol_usd(t0, now) / elapsed * 60.0
                vol_x = post_vol_pm / base_vol
                volume_x = post_volume_pm / base_volume
                if not (vol_x >= NW_REACT_MULT and volume_x >= NW_REACT_MULT):
                    continue

                # ---- DIRECTION: trade the way the CHART reacted (price move since the news) ----
                # The gate already confirmed an abnormal move (volume AND volatility). Take the
                # DIRECTION straight from PRICE — go with the move, not the flow. Flow can diverge
                # from price ("absorption"), so flow is used to MANAGE the exit, not pick the side.
                moved = (px - start_px) / start_px
                buy, sell = self.whales.flow(config.NW_FLOW_WINDOW)   # 1-min flow (context + exit)
                tot = buy + sell
                buy_pct = buy / tot if tot > 0 else 0.5
                detail = (f"price {moved*100:+.2f}% since news · {vol_x:.1f}× swing · "
                          f"{volume_x:.1f}× volume · 1m flow {buy_pct*100:.0f}% buy")
                if moved >= config.NW_MIN_MOVE_PCT:
                    self._open("long", px, buy_pct, detail)
                    return
                if moved <= -config.NW_MIN_MOVE_PCT:
                    self._open("short", px, 1 - buy_pct, detail)
                    return
                # abnormal move, but price hasn't picked a clear direction yet → keep watching
            if not self.positions:
                self._note(f"no clear directional move within {config.NW_WATCH_SECONDS:g}s → skipped", "skip")
        except Exception as e:
            self._note(f"watch error: {type(e).__name__} {e}", "error")
        finally:
            self._watching = False

    async def _book_bid_fraction(self):
        """Near-mid bid USD / (bid+ask USD). >0.5 = bids heavier (bullish)."""
        try:
            ob = await orderbook.fetch_all_orderbooks()
            mid = ob.get("mid")
            if not mid:
                return None
            lo, hi = mid * 0.997, mid * 1.003           # within 0.3% of mid
            bid_usd = sum(b["usd"] for b in ob.get("bids", []) if b["price"] >= lo)
            ask_usd = sum(a["usd"] for a in ob.get("asks", []) if a["price"] <= hi)
            t = bid_usd + ask_usd
            return bid_usd / t if t > 0 else None
        except Exception:
            return None

    # ---- open / close ----
    def _open(self, side, price, flow_pct, detail="", sl=0.0, tp=0.0, news="",
              exec_model="", signal_price=0.0, entry_drift=0.0):
        buffer = WHALE100_LIGHTER_MARGIN_BUFFER if exec_model == "lighter_sim" else 1.0
        margin = min(round(self.balance * NW_RISK_FRAC * buffer, 2), round(self.balance, 2))
        if margin < 1:
            self._note("wanted to enter but balance too low → skipped", "skip")
            return
        lev = config.NW_LEVERAGE
        qty = margin * lev / price
        pos = NWPosition(id=uuid.uuid4().hex[:6], side=side, entry=price, qty=qty,
                         margin=margin, leverage=lev, opened_at=time.time(),
                         entry_flow=flow_pct, news=news, peak=price,
                         sl=round(sl, 2), tp=round(tp, 2),
                         exec_model=exec_model,
                         signal_price=round(float(signal_price or 0.0), 2),
                         entry_drift=round(float(entry_drift or 0.0), 2))
        self.balance -= margin
        self.positions[pos.id] = pos
        self._note(f"OPEN {side.upper()} @ ${price:,.0f}  ·  {detail} · "
                   f"margin ${margin:.2f} {lev}x", "open")
        self._save()
        return True

    def _close(self, pos, price, reason):
        fill_price = self._lighter_exit_fill(pos, price)
        if fill_price is None:
            return False
        price = fill_price
        self.positions.pop(pos.id, None)
        pnl = pos.unrealized(price)
        self.balance += pos.margin + pnl
        rec = {"id": pos.id, "side": pos.side, "entry": round(pos.entry, 2),
               "exit": round(price, 2), "qty": round(pos.qty, 5),
               "margin": round(pos.margin, 2), "leverage": pos.leverage,
               "pnl": round(pnl, 2), "reason": reason,
               "opened_at": pos.opened_at, "closed_at": time.time(),
               "exec_model": getattr(pos, "exec_model", ""),
               "signal_price": round(getattr(pos, "signal_price", 0.0), 2),
               "entry_drift": round(getattr(pos, "entry_drift", 0.0), 2)}
        self.history.insert(0, rec)
        self.history = self.history
        self._note(f"CLOSE {pos.side.upper()} @ ${price:,.0f}  ·  {reason}  ·  "
                   f"P&L ${pnl:+.2f}", "win" if pnl >= 0 else "loss")
        self._append_csv(rec)
        self._save()
        return True

    def _append_csv(self, rec):
        try:
            new = not os.path.exists(CSV_PATH)
            with open(CSV_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["closed_at", "side", "entry", "exit", "qty",
                                "margin", "leverage", "pnl", "reason"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["closed_at"])),
                            rec["side"], rec["entry"], rec["exit"], rec["qty"],
                            rec["margin"], rec["leverage"], rec["pnl"], rec["reason"]])
        except Exception:
            pass

    # ---- WHALE FOLLOW: copy the newest big tape print, with a fixed SL/TP ----
    def _whale_follow_entry(self):
        tape = self.whales.tape
        if not tape:
            return
        latest_seq = tape[-1]["seq"]
        if self._wf_last_seq is None:        # first run: ignore pre-existing prints, start fresh
            self._wf_last_seq = latest_seq
            return
        # newest-first scan for the latest qualifying print that's NEW since we last looked
        signal = None
        for t in reversed(tape):
            if t["seq"] <= self._wf_last_seq:
                break
            if t["usd"] >= WHALE_FOLLOW_MIN_USD:
                signal = t
                break
        self._wf_last_seq = latest_seq        # mark everything current as seen
        if signal is None:
            return
        now = time.time()
        if now - self._wf_cooldown_ts < WHALE_FOLLOW_COOLDOWN:
            return                            # don't fire on every print in a burst
        px = self._btc()
        if not px:
            return
        side = "long" if signal["side"] == "buy" else "short"
        # convert the USDT P&L targets into the SL/TP PRICE levels the exit engine uses:
        # P&L = qty * price_move, so price_move for $X of P&L = X / qty. (qty = margin*lev/px,
        # exactly how _open will size it, so the realized close lands on +$TP / -$SL.)
        margin = min(round(self.balance * NW_RISK_FRAC, 2), round(self.balance, 2))
        qty = margin * config.NW_LEVERAGE / px
        if qty <= 0:
            return
        if side == "long":
            sl = px - WHALE_FOLLOW_SL_USDT / qty
            tp = px + WHALE_FOLLOW_TP_USDT / qty
        else:
            sl = px + WHALE_FOLLOW_SL_USDT / qty
            tp = px - WHALE_FOLLOW_TP_USDT / qty
        detail = (f"follow {signal['side'].upper()} ${signal['usd']:,.0f} on {signal['ex']} "
                  f"· TP +${WHALE_FOLLOW_TP_USDT:.2f} / SL -${WHALE_FOLLOW_SL_USDT:.2f}")
        self._open(side, px, 0.5, detail, sl=sl, tp=tp, news=detail)
        self._wf_cooldown_ts = now

    def _whale100_entry(self):
        """Follow >= $100k prints. NO fixed TP/SL — the only exit is the 30s flow, in manage_loop."""
        tape = self.whales.tape
        if not tape:
            return
        latest_seq = tape[-1]["seq"]
        if self._wf_last_seq is None:
            self._wf_last_seq = latest_seq
            return
        signal = None
        for t in reversed(tape):
            if t["seq"] <= self._wf_last_seq:
                break
            if t["usd"] >= WHALE100_MIN_USD:
                signal = t
                break
        self._wf_last_seq = latest_seq
        if signal is None:
            return
        now = time.time()
        if now - self._wf_cooldown_ts < WHALE100_COOLDOWN:
            return
        side = "long" if signal["side"] == "buy" else "short"
        try:
            signal_px = float(signal.get("price") or 0)
        except Exception:
            signal_px = 0.0
        if signal_px <= 0:
            signal_px = self._btc() or 0.0
        fill = self._lighter_entry_fill(side, signal_px)
        if fill is None:
            return
        px, drift, age = fill
        detail = (f"follow {signal['side'].upper()} ${signal['usd']:,.0f} on {signal['ex']} "
                  f"- Lighter sim slip ${WHALE100_LIGHTER_ENTRY_SLIP_USD:.2f} "
                  f"drift ${drift:+.2f} age {age:.1f}s "
                  f"- exit when 30s on-side flow < {WHALE100_FLOW_EXIT*100:.0f}%")
        opened = self._open(side, px, 0.5, detail, sl=0.0, tp=0.0, news=detail,
                            exec_model="lighter_sim", signal_price=signal_px,
                            entry_drift=drift)   # no TP/SL - flow is the exit
        if opened:
            self._wf_cooldown_ts = now
        return
        detail = (f"follow {signal['side'].upper()} ${signal['usd']:,.0f} on {signal['ex']} "
                  f"· exit when 30s on-side flow < {WHALE100_FLOW_EXIT*100:.0f}%")
        self._open(side, px, 0.5, detail, sl=0.0, tp=0.0, news=detail)   # no TP/SL — flow is the exit
        self._wf_cooldown_ts = now

    # ---- runs every second: mark + SIGNAL-DRIVEN exits ----
    async def manage_loop(self):
        while True:
            await asyncio.sleep(1.0)
            # WHALE FOLLOW: when flat, copy the latest big print on the live tape (continuous,
            # not news-driven). The position it opens carries an SL/TP, handled by the exit below.
            if self.enabled and not self.positions:
                try:
                    if self.strategy == "whale_follow":
                        self._whale_follow_entry()
                    elif self.strategy == "whale_follow_100k":
                        self._whale100_entry()
                except Exception as e:
                    self._note(f"whale-follow error: {type(e).__name__}: {str(e)[:80]}", "error")
            if not self.positions:
                self._flip.clear()
                continue
            px = self._mark_price()
            if not px:
                continue
            now = time.time()
            # volatility trailing-stop distance (wider when choppy, tighter when calm)
            vol = self._vol_pct(now - 90, now)
            dist = NW_TRAIL_MIN_PCT
            if vol is not None:
                dist = max(NW_TRAIL_MIN_PCT, min(NW_TRAIL_MAX_PCT, NW_TRAIL_VOL_MULT * vol / 100.0))
            for pos in list(self.positions.values()):
                held = now - pos.opened_at
                if pos.peak <= 0:
                    pos.peak = pos.entry

                # --- WHALE FOLLOW 100k: NO fixed TP/SL — the ONLY exit is the 30-SECOND FLOW ---
                if self.strategy == "whale_follow_100k":
                    # EXIT the instant the flow on your side over the last 30s falls below the
                    # threshold (long: buy% < 60% -> the tape turned to selling -> get out).
                    if held >= WHALE100_GRACE_SEC:
                        buy, sell = self.whales.flow(WHALE100_FLOW_WINDOW)
                        tot = buy + sell
                        if tot > 0:
                            on_side = (buy / tot) if pos.side == "long" else (sell / tot)
                            if on_side < WHALE100_FLOW_EXIT:
                                self._close(pos, px, f"flow_exit ({on_side*100:.0f}% on-side "
                                            f"< {WHALE100_FLOW_EXIT*100:.0f}% over {WHALE100_FLOW_WINDOW}s)")
                                self._wf_cooldown_ts = now; self._flip.pop(pos.id, None); continue
                    # pure-safety backstops only (NOT a profit/stop target) — anti-blowup + max-hold
                    if pos.unrealized(px) <= -pos.margin * 0.98:
                        self._close(pos, px, "liquidation"); self._wf_cooldown_ts = now
                        self._flip.pop(pos.id, None); continue
                    if held >= WHALE100_MAX_HOLD:
                        self._close(pos, px, "time_stop"); self._wf_cooldown_ts = now
                        self._flip.pop(pos.id, None); continue
                    continue

                # --- GEMINI strategy: fixed AI-set stop-loss / take-profit ---
                if pos.sl or pos.tp:
                    hit = None
                    if pos.side == "long":
                        if pos.tp and px >= pos.tp:
                            hit = "take_profit"
                        elif pos.sl and px <= pos.sl:
                            hit = "stop_loss"
                    else:
                        if pos.tp and px <= pos.tp:
                            hit = "take_profit"
                        elif pos.sl and px >= pos.sl:
                            hit = "stop_loss"
                    if hit is None:        # backstops
                        _maxhold = (WHALE_FOLLOW_MAX_HOLD if self.strategy == "whale_follow"
                                    else config.NW_MAX_HOLD_MIN * 60)
                        if pos.unrealized(px) <= -pos.margin * 0.98:
                            hit = "liquidation"
                        elif held >= _maxhold:
                            hit = "time_stop"
                    if hit:
                        self._close(pos, px, hit)
                        self._flip.pop(pos.id, None)
                    continue           # AI-managed position: skip the mechanical exits below

                # 1) VOLATILITY TRAILING STOP — trails the best price; this is both the
                #    initial stop-loss AND the profit-lock as the trade runs your way.
                if pos.side == "long":
                    pos.peak = max(pos.peak, px)
                    if px <= pos.peak * (1 - dist):
                        self._close(pos, px, f"trail_stop ({dist*100:.2f}% off peak)")
                        self._flip.pop(pos.id, None); continue
                else:
                    pos.peak = min(pos.peak, px)
                    if px >= pos.peak * (1 + dist):
                        self._close(pos, px, f"trail_stop ({dist*100:.2f}% off low)")
                        self._flip.pop(pos.id, None); continue

                # 2) FLOW-FLIP SIGNAL EXIT — recompute flow/CVD/whales/price; when they
                #    turn against the position (and hold for a few secs), get out.
                votes, buy_pct, moved = self._recent_votes(config.NW_FLOW_WINDOW)
                dir_score = votes if pos.side == "long" else -votes
                if dir_score <= NW_EXIT_VOTES:
                    self._flip[pos.id] = self._flip.get(pos.id, 0) + 1
                    if self._flip[pos.id] >= NW_EXIT_CONFIRM:
                        self._close(pos, px, f"signal_exit (votes {votes:+d}, flow {buy_pct*100:.0f}%)")
                        self._flip.pop(pos.id, None); continue
                else:
                    self._flip[pos.id] = 0

                # 3) SAFETY NETS — liquidation backstop + max-hold time-stop.
                if pos.unrealized(px) <= -pos.margin * 0.98:
                    self._close(pos, px, "liquidation"); self._flip.pop(pos.id, None); continue
                if held >= config.NW_MAX_HOLD_MIN * 60:
                    self._close(pos, px, "time_stop"); self._flip.pop(pos.id, None); continue


    # ---- controls + snapshot ----
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED' if self.enabled else 'PAUSED'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_strategy(self, name):
        name = (name or "").strip()
        if name not in STRATEGIES:
            return {"ok": False, "error": f"unknown strategy '{name}'"}
        if name == self.strategy:
            return {"ok": True, "strategy": self.strategy, "strategy_label": STRATEGIES[name]}
        if self.positions:
            return {"ok": False, "error": "close open positions before switching strategy"}
        self.strategy = name
        self._note(f"strategy set to {STRATEGIES[name]}")
        self._save()
        return {"ok": True, "strategy": self.strategy, "strategy_label": STRATEGIES[name]}

    def reset(self):
        self.enabled = config.NW_ENABLED
        self.balance = float(config.NW_START_BALANCE)
        self.start_balance = float(config.NW_START_BALANCE)
        self.positions = {}
        self.history = []
        self.log = []
        self._watching = False
        self._inflight = 0
        self._exec_skips = 0
        self._exec_last = ""
        self._exec_last_px = None
        self._exec_last_age = None
        self._note("bot reset")
        self._save()
        return {"ok": True}

    def state(self, flow_window_sec=None):
        px = self._mark_price()
        positions, equity = [], self.balance
        win = int(flow_window_sec) if flow_window_sec else config.NW_FLOW_WINDOW
        buy, sell = self.whales.flow(win)
        tot = buy + sell
        buy_pct = round(buy / tot * 100, 1) if tot > 0 else None
        for p in self.positions.values():
            pnl_px = self._lighter_exit_fill(p, px, quiet=True)
            if pnl_px is None:
                pnl_px = px
            up = p.unrealized(pnl_px) if pnl_px else 0.0
            equity += p.margin + up
            positions.append({
                "id": p.id, "side": p.side, "entry": round(p.entry, 2),
                "mark": round(pnl_px, 2) if pnl_px else None,
                "qty": round(p.qty, 5), "margin": round(p.margin, 2),
                "leverage": p.leverage, "liq": round(p.liq_price(), 2),
                "entry_flow": round(p.entry_flow * 100, 1),
                "sl": round(p.sl, 2), "tp": round(p.tp, 2), "news": p.news,
                "exec_model": getattr(p, "exec_model", ""),
                "signal_price": round(getattr(p, "signal_price", 0.0), 2),
                "entry_drift": round(getattr(p, "entry_drift", 0.0), 2),
                "pnl": round(up, 2),
                "pnl_pct": round(up / p.margin * 100, 2) if p.margin else 0})
        wins = sum(1 for h in self.history if h["pnl"] > 0)     # break-even is NOT a win
        return {
            "enabled": self.enabled, "watching": self._watching,
            "strategy": self.strategy,
            "strategy_label": STRATEGIES.get(self.strategy, self.strategy),
            "strategies": STRATEGIES,
            "price": round(px, 2) if px else None,
            "exec_model": "lighter_sim" if self._uses_lighter_exec() else "paper_mark",
            "exec_skips": self._exec_skips,
            "exec_last": self._exec_last,
            "lighter_price": round(self._exec_last_px, 2) if self._exec_last_px else None,
            "lighter_age": round(self._exec_last_age, 2) if self._exec_last_age is not None else None,
            "flow_pct": buy_pct,                       # current live buy %
            "flow_window": win,
            "balance": round(self.balance, 2),
            "equity": round(equity, 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(equity - self.start_balance, 2),
            "trades": len(self.history),
            "wins": wins,
            "positions": positions,
            "history": self.history,
            "log": self.log[:25],
        }
