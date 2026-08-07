"""
================================================================================
 lighter_news_bot.py  —  REAL-MONEY "News + Whale" bot, trading on Lighter
================================================================================
This is a REAL-MONEY copy of news_whale_bot.py. Same strategy, same signals,
same exits — but instead of paper fills it places REAL market orders on your
Lighter perps account (via lighter_live) and exits by closing the real position.

  * Same adaptive reaction gate (move must be >= NW_REACT_MULT x the recent
    normal in BOTH volume and volatility) and same 4-signal direction vote.
  * Same exits: volatility trailing stop + flow-flip + max-hold time stop.
  * Sizing mirrors the paper bot: it uses your FREE balance x LIVE_RISK_FRAC as
    margin, at the leverage you set (changeable live from the page).

==================  REAL MONEY — READ THIS  ==================
- This opens and closes leveraged positions on its own. You can lose the money.
- PAUSE stops it opening new trades; CLOSE flattens the position right now.
- It will not place a single real order unless LIGHTER_TRADING_ENABLED = True in
  config.py. Until then it runs and logs what it WOULD do (a safe dry-run).
- All Lighter calls run in a worker thread so they never freeze the dashboard.
================================================================================
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from collections import deque
from dataclasses import dataclass, asdict

import config

# Share the EXACT strategy knobs with the paper bot so behaviour is identical.
from news_whale_bot import (
    NW_CHECK_SECONDS, NW_MIN_ELAPSED, NW_BASELINE_MIN, NW_REACT_MULT, NW_ENTER_VOTES,
    NW_EXIT_VOTES, NW_EXIT_CONFIRM, NW_TRAIL_VOL_MULT, NW_TRAIL_MIN_PCT, NW_TRAIL_MAX_PCT,
    NW_BIG_TRADE_USD,
)
from fv_track_paper import (
    ENTER_BPS as FV_ENTER_BPS, EXIT_BPS as FV_EXIT_BPS,
    STOP_WIDEN_BPS as FV_STOP_WIDEN_BPS, MAX_HOLD_SEC as FV_MAX_HOLD_SEC,
    STALE_MS as FV_STALE_MS, MIN_LEADERS as FV_MIN_LEADERS, LEADERS as FV_LEADERS,
)

CSV_PATH = os.path.join(config.DATA_DIR, "lighter_bot_trades.csv")
STATE_PATH = os.path.join(config.DATA_DIR, "lighter_bot_state.json")

# ---- REAL-MONEY tuning (edit HERE) ------------------------------------------
LIVE_LEVERAGE       = 3        # leverage the bot opens with (change it live from the page)
LIVE_RISK_FRAC      = 1.0      # margin per trade as a fraction of FREE balance (1.0 = use it
                               #   all, exactly like the paper bot). Lower it to risk less.
LIVE_MAX_CONCURRENT = 1        # one position at a time
LIVE_FLOW_WINDOW    = 60        # ALWAYS judge flow on the 1-minute window (entry context + exit)
LIVE_STATE_REFRESH  = 5.0      # secs between background reads of your real balance/position
LIVE_MIN_MARGIN     = 1.0      # don't try to open with less than this many USDC of margin
LIVE_SETTLE_SECS    = 2.0      # after closing, wait this long for the fill to settle on Lighter
                               #   before reading the new balance (so realized P&L is the REAL one)
LIVE_RECONCILE_GRACE   = 8.0   # secs after opening before a "flat" read is trusted (gives Lighter
                               #   time to reflect the new position; guards the open-time race)
LIVE_RECONCILE_CONFIRM = 2     # consecutive FRESH flat reads required to declare an external close
                               #   (one stale/glitchy snapshot must never flatten our tracking)
LIVE_ADOPT_BLOCK_SECS  = 30.0  # after a bot close, ignore matching stale Lighter positions this long
LIVE_MARGIN_BUFFER     = 0.95  # use at most this fraction of free balance as margin, so taker
                               #   fees/slippage never push the order over 100% of collateral and
                               #   get the fill rejected. Set to 1.0 to use the whole balance.

# ---- selectable strategies (shown in the website dropdown) -------------------
# key -> human label. The key is what gets persisted; the label is what the page shows.
STRATEGIES = {
    "flow_news": "Flow + News",   # original: wait for an abnormal reaction, then ride it
    "newsplus":  "News+",         # react instantly to the candle, strict stop, flow take-profit
    "flowplus":  "Flow+",         # pure flow: enter on flow lean, exit when flow crosses 50%
    "gptnews":   "GPTnews",       # AI (Groq) decides side + stop-loss + take-profit itself
    "claudenews": "claude news sonnet",  # Claude picks side + stop/take-profit; exit RUNS that plan
    "scalper":   "Scalper (flow MM)",   # CONTINUOUS flow-momentum scalp, not news-driven
    "maker":     "Maker mean-reversion", # CONTINUOUS post-only limit MM: buy dips below fair value
    "whale_follow": "Whale Copy (≥$250k)", # CONTINUOUS: copy any big tape print (buy→long/sell→short)
    "whale_follow_100k": "Whale Copy 100k (mirror paper)",  # MIRRORS the paper Whale-Follow-100k bot
    "whale100_live": "Whale Follow 100k LIVE (flow stop · limit)",  # INDEPENDENT real-money copy, limit fills
    "freq_live": "Freqtrade-style LIVE (mirror paper · limit)",  # MIRRORS the paper Freqtrade-style bot
    "freq_improved_live": "Freqtrade-style (paper improved TP/SL)",  # MIRRORS the improved TP/SL paper copy
    "freq_trend_live": "Freqtrade-style (paper trend TP/SL)",  # MIRRORS the trend TP/SL paper copy
    "freq5_live": "Freqtrade-style (improved TP/SL 5%)",  # MIRRORS the paper improved TP/SL 5% bot EXACTLY
    "tv_bb_rsi_live": "Bollinger + RSI Double (ChartArt)",  # MIRRORS the TradingView paper bb_rsi strategy
    "rsi2atr_live": "RSI2 EMA50 Scalper (mirror paper ATR)",  # MIRRORS the RSI2 EMA50 ATR-filter paper bot
    "flowtrail": "Flow Trail (60% flow · trailing stop)",  # enter on >=60% 1m flow; trailing stop exit
    "newswhale": "News+Whale Trail (news · follow ≥$200k whale · trailing stop)",  # news-triggered
    "fv_live": "Fair-Value Tracking LIVE (paper rules - IOC limit)",  # Lighter deviation vs leader FV
}
DEFAULT_STRATEGY = "flow_news"

# Strategies that are CONTINUOUS or pure paper-MIRRORS — they must NEVER open a
# news-triggered trade. The freq mirrors (freq*_live) ONLY copy their paper bot; if any of
# them is missing here, a news headline would make the REAL bot open a trade of its own
# (the "OPEN LONG … since news …" bug). One source of truth, checked in on_news AND _watch.
NO_NEWS_STRATEGIES = frozenset({
    "scalper", "maker", "whale_follow", "whale_follow_100k", "flowtrail", "whale100_live",
    "freq_live", "freq_improved_live", "freq_trend_live", "freq5_live", "tv_bb_rsi_live", "fv_live",
    "rsi2atr_live",
})

# ---- "Whale Copy" strategy knobs (REAL money: copy big cross-exchange tape prints) ----
# The same idea as the paper "Whale Follow Bot", but it places REAL Lighter orders: the instant
# a single trade >= WHALE_MIN_USD prints on the live tape (any exchange), open that way (buy→long,
# sell→short) and exit on a FIXED dollar take-profit / stop-loss. A cooldown stops it firing on
# every print in a burst. HONEST: this copies prints that have ALREADY moved the price, as a taker
# racing HFT — it is NOT a proven edge. Start with real trading OFF and watch it; size small.
WHALE_MIN_USD      = 250000      # follow any single trade at least this big (USD)
WHALE_TP_USDT      = 1.50        # TAKE-PROFIT: close when the position's P&L reaches +$1.50
WHALE_SL_USDT      = 0.80        # STOP-LOSS:   close when the position's P&L reaches -$0.80
WHALE_COOLDOWN     = 30          # seconds to wait after a close before copying the next print
WHALE_MAX_HOLD     = 600         # cut the trade if neither TP nor SL hits within this long (10 min)

# ---- "Whale Copy 100k (flow stop)" knobs (REAL money) ----
# EXACT 1:1 copy of the paper "Whale Follow 100k (flow stop)" bot: open on any >= WHALE100_MIN_USD
# print, exit when the 30s flow on your side drops below WHALE100_FLOW_EXIT, with the SAME grace,
# liquidation backstop and max-hold the paper bot uses. No fixed TP/SL.
WHALE100_MIN_USD   = 100000      # follow any single trade at least this big (USD)
WHALE100_FLOW_WINDOW = 30        # seconds of taker flow used for the exit
WHALE100_FLOW_EXIT = 0.60        # EXIT the instant the flow on YOUR side over 30s drops below 60%
WHALE100_COOLDOWN  = 30          # seconds to wait after a close before copying the next print
MIRROR_MAX_ENTRY_DIFF = 15.0     # (no longer used) — the whale-100k mirror now ALWAYS enters via a
                                 # best-price IOC limit capped at the paper entry, so it can never
                                 # fill worse than paper; there is no market-order chase path to cap.

# ---- "Flow Trail" strategy knobs (REAL money) ----
# Enter the way the taker flow leans over FLOWTRAIL_WINDOW (>= FLOWTRAIL_ENTER% buy -> long, >= that%
# sell -> short). Exit is a pure TRAILING STOP: the stop sits FLOWTRAIL_TRAIL_USD behind the BEST
# price reached and only ratchets in your favour — the instant price falls back to it, it CLOSES. A
# rising move drags the stop up and locks the profit; if price drops right after entry the worst case
# is a small ~trail-sized loss. It does NOT hold a losing position open.
FLOWTRAIL_ENTER    = 60.0        # enter when taker buy-flow (or sell-flow) over the window >= this %
FLOWTRAIL_WINDOW   = 60          # seconds of taker flow used for the entry decision (1 MINUTE)
FLOWTRAIL_TRAIL_USD = 5.0        # trailing-stop distance ($) behind the best price. Smaller = tighter
                                 # stop = smaller loss AND smaller locked profit (too tight churns)
FLOWTRAIL_COOLDOWN = 5           # seconds to wait after a close before re-entering
FLOWTRAIL_LIMIT_SLIP = 0.0003    # entry/exit fill is a TOP-OF-BOOK marketable limit capped this far
                                 # (0.03% ~ $18 on BTC) past the best price — NOT a 2% market order.
                                 # Smaller = closer to best price but more orders that don't fill.
WHALE100_MAX_HOLD  = 1800        # hard max-hold backstop (30 min) — same as the paper bot
WHALE100_GRACE_SEC = 2           # grace after entry before the flow can close it — same as paper

# ---- "Fair-Value Tracking LIVE" knobs ---------------------------------------
# Same signal as fv_track_paper.py: leader-venue fair value vs Lighter's own last
# trade. LIVE entry is stricter than paper: one IOC limit at the Lighter signal
# price, fill there-or-better or skip. Close uses a tight marketable-limit cap.
FV_LIVE_ATTEMPT_COOLDOWN = 3.0
FV_LIVE_CLOSE_SLIP = FLOWTRAIL_LIMIT_SLIP
FV_LIVE_IOC_ATTEMPTS = 10
FV_LIVE_IOC_DELAY_MS = 40

# ---- "News+Whale Trail" strategy knobs (REAL money) ----
# News-TRIGGERED whale-follow entry: when a headline prints, watch the live trade tape (up to
# NWT_GIVEUP_SECS) for a single trade of >= NWT_WHALE_MIN_USD ($200k). The FIRST such whale picks
# the side — they BOUGHT -> go long, they SOLD -> go short — and we follow that direction. No flow,
# no confluence: one big trader's bet after the news is the whole signal. If no $200k+ trade prints
# within the giveup window -> skip the headline (no trade).
# EXIT is a PERCENTAGE TRAILING STOP: the stop sits NWT_TRAIL_PCT behind the BEST price reached and
# only ratchets in our favour. As price runs our way the stop FOLLOWS it (up for a long, down for a
# short) and locks the gain; the moment price retraces NWT_TRAIL_PCT from the best, it CLOSES. The
# stop never loosens, so a winner can't round-trip into a loss and a loser is cut at ~the trail.
NWT_WHALE_MIN_USD = 200000.0     # a trade must be >= this ($200k) to count as the whale we follow
NWT_TRAIL_PCT     = 0.03         # trailing-stop distance = 3% behind the best price (~$1,920 on BTC
                                 #   @ $64k). Wider = rides the trend longer but gives back more on a
                                 #   reversal; tighter = locks profit sooner but gets shaken out more.
NWT_GIVEUP_SECS   = 30           # watch this long after the headline for a $200k+ print, then skip
                                 #   (the news edge is fast — don't wait around forever)
NWT_CHECK_SECS    = 0.5          # poll interval while watching the tape for the whale print

# ---- "News+" strategy knobs (INSTANT flow-direction entry, flow take-profit, strict stop) ---
# ENTRY: the instant a headline prints, pick the side from ORDER FLOW — no waiting for a
# price move. Buyers in control -> long, sellers -> short. Fires sub-second.
NEWSPLUS_FLOW_WINDOW = 10       # seconds of recent tape used to read flow direction at entry
NEWSPLUS_FLOW_DIR    = 0.50     # need one side >= this share to call a direction. 0.50 = decide on
                                #   the FIRST tick from whichever side is leaning (instant, no wait).
                                #   Raise (e.g. 0.55) to wait for a clearer side at the cost of speed.
NEWSPLUS_GIVEUP_SECS = 20       # if flow never shows a clear side within this, skip the headline
# EXIT take-profit (X3 + ratchet X4): a tight trailing stop that tightens once you're
# in profit, plus the flow-flip exit. Whichever triggers first wins.
NEWSPLUS_TRAIL       = 0.003    # base trailing-stop distance from the best price (0.30%)
NEWSPLUS_TRAIL_TIGHT = 0.0015   # tightened trail once in profit (0.15%) — locks the win on a stall
NEWSPLUS_TRAIL_ARM   = 0.003    # profit (0.30%) that arms the tight trail
# EXIT stop-loss (S1 + S3): a hard fixed stop, plus a "no-progress" time cut.
NEWSPLUS_HARD_STOP   = 0.003    # hard stop at -0.30% vs entry (≈ -6% of margin at 20x)
NEWSPLUS_EARLY_SECS  = 12       # ONE-TIME early check this many secs after opening: if the move
NEWSPLUS_EARLY_MIN   = 0.000015 #   in our favour (up for long / down for short) isn't >= 0.0015%
                                #   by then, close — it didn't react. (0.0015% ≈ $1 at $64k.)
NEWSPLUS_NOPROG_SECS = 50       # if not in profit within this many secs, cut it
NEWSPLUS_EXIT_CONFIRM = 2       # flow must point AGAINST the position this many secs before we close
                                #   (manage_loop ticks ~1×/sec, so 2 ≈ a 2-second confirmation)

# ---- "Flow+" strategy knobs (pure flow: enter on the lean, exit when flow crosses 50%) ----
FLOWPLUS_FLOW_WINDOW  = 60      # seconds of tape used to read flow (entry direction AND exit)
FLOWPLUS_GIVEUP_SECS  = 20      # if there's no tape to read flow within this, skip the headline
FLOWPLUS_EXIT_CONFIRM = 2       # flow must sit on the WRONG side of 50% this many secs before closing
                                #   (anti-whipsaw at the 50% line; set to 1 for instant exit)

# ---- "GPTnews" strategy knobs (AI picks the side AND manages the exit live) -------
GPTNEWS_MANAGE_SECS  = 4.0      # how often the AI re-checks the OPEN trade to decide hold/close
GPTNEWS_BACKSTOP_PCT = 0.04     # CATASTROPHIC flash-crash seatbelt (price move vs entry). The AI
                                #   has no fixed stop — this only fires if price runs ~4% against
                                #   you (≈ -80% of margin at 20x) while the AI is mid-decision, so a
                                #   flash move can't liquidate you. 0 = disable (NOT recommended).

# ---- "claude news haiku" strategy knobs (trade the AI's OWN plan to completion) -----
# The AI sets a stop-loss and take-profit at entry. Instead of babysitting the trade to a
# scratch (the old every-4s AI manage closed ~every trade at ±0.01% — only 2 of 231 ever
# reached a target), we now RUN THE PLAN: hard stop at the AI's stop, BANK at the AI's
# take-profit and then ride any runner on a tight trail, and lock breakeven once the trade
# is halfway to target so a winner can't round-trip into a loss.
CLAUDE_RUN_TRAIL     = 0.0015   # once take-profit is reached, trail this far (0.15%) off the
                                #   best price — banks >= TP but lets a strong move keep running.
CLAUDE_LOCK_ARM_FRAC = 0.5      # when profit reaches this fraction of the TP distance, move the
                                #   stop to breakeven (entry) so a developed winner can't go red.
CLAUDE_NOPROG_SEC    = 120      # a news edge is fast: if the trade isn't green by now, it didn't
                                #   react → cut it and free the capital for the next headline.
CLAUDE_MAX_HOLD_SEC  = 600      # hard max hold (10 min). The news edge decays in minutes; never
                                #   sit in a stale trade for the 45-min generic backstop.

# ---- "Scalper" strategy knobs (CONTINUOUS flow-momentum scalp — NOT news-driven) ----
# Runs in manage_loop, not on news. Bet the way flow leans HARD (>= SCALP_ENTER_PCT on one
# side); ride while it STAYS that strong; close the INSTANT that side's flow falls back below
# the threshold. Lighter has NO fees, so this is a clean flow-pressure ride. A wide safety
# stop + max-hold sit underneath purely to prevent a flash-crash liquidation.
SCALP_FLOW_WINDOW  = 60        # seconds of tape used to read the flow lean (1-minute flow)
SCALP_ENTER_PCT    = 0.80      # ENTER when one side's flow >= this (LONG on buy%, SHORT on sell%);
                               #   EXIT the instant that side's flow falls back BELOW this.
SCALP_MIN_VOL_USD  = 150000    # need >= this much flow in the window (else the tape is too thin)
SCALP_STOP_PCT     = 0.005     # SAFETY hard stop (anti-liquidation only) at -0.5% price move
SCALP_MAX_HOLD     = 300       # seconds max hold (safety backstop)
SCALP_COOLDOWN     = 4         # seconds to wait after a close before re-entering
SCALP_SIZE_MULT    = 0.5       # bet half-size on scalps

# ---- "Maker mean-reversion" strategy knobs (CONTINUOUS post-only limit market-making) ----
# Backtests proved TAKER scalping loses (the spread you cross > the edge). The fix is to be
# the MAKER: rest a POST-ONLY limit BUY a little BELOW fair value, so when a dip comes to us
# we get filled as maker (we EARN the half-spread instead of paying it), then rest a POST-ONLY
# limit SELL back at/above fair value to bank the reversion. A hard MARKET stop caps the one
# real risk — adverse selection (getting filled right before price keeps falling / trends).
# NOTE: maker FILLS can't be backtested from candles, so treat this as EXPERIMENTAL: run it
# in paper/dry-run first, watch the fill rate and the stop rate, and size small.
MAKER_FV_WINDOW    = 180       # seconds of mid-price history that define "fair value" (the mean)
MAKER_TREND_WINDOW = 3600      # seconds for the slow trend filter (don't buy dips in a hard downtrend)
MAKER_BAND_BPS     = 12        # rest the BUY this many bps BELOW fair value (the dip we wait for)
MAKER_TP_BPS       = 12        # rest the SELL this many bps ABOVE entry (reversion + spread we capture)
MAKER_STOP_BPS     = 35        # MARKET-close this far below entry (adverse-selection cap; taker exit)
MAKER_REQUOTE_BPS  = 6         # if fair value drifts more than this from our resting bid, reprice it
MAKER_ORDER_TTL    = 45        # cancel an unfilled resting BUY after this many seconds (re-evaluate)
MAKER_MAX_HOLD     = 600       # if the exit limit hasn't filled in this long, market-out (give up edge)
MAKER_LEVERAGE     = 2         # SAFE low leverage — this is NOT a place for 20x
MAKER_SIZE_MULT    = 0.5       # fraction of the usual margin to risk per quote (start small)
MAKER_MIN_TREND    = -0.004    # skip quoting if mid is > 0.4% BELOW the slow trend mean (falling knife)


async def _to_thread(fn, *a, **k):
    """Run a blocking call off the event loop (CCXT/Lighter calls are blocking)."""
    try:
        return await asyncio.to_thread(fn, *a, **k)
    except AttributeError:                       # Python < 3.9 fallback
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **k))


@dataclass
class LivePos:
    side: str            # "long" | "short"
    entry: float
    qty: float           # BTC
    leverage: float
    opened_at: float
    news: str
    peak: float = 0.0
    early_done: bool = False   # passed the News+ early-move check (so we only check once)
    bal_open: float = 0.0      # REAL Lighter total balance at open — for exact realized P&L
    sl: float = 0.0            # stop-loss PRICE (GPTnews sets this; 0 = unused)
    tp: float = 0.0            # take-profit PRICE (GPTnews sets this; 0 = unused)
    tp_armed: bool = False     # claudenews: take-profit was reached → now RIDING on a trail
    be_locked: bool = False    # claudenews: ran halfway to TP → stop moved up to breakeven

    entry_fv: float = 0.0      # FV live: leader fair value at entry
    entry_dev: float = 0.0     # FV live: signed FV-vs-Lighter deviation in bps at entry

    def unrealized(self, mark: float) -> float:
        if not mark:
            return 0.0
        return self.qty * (mark - self.entry) if self.side == "long" \
            else self.qty * (self.entry - mark)


class LighterNewsBot:
    def __init__(self, market, whales, lighter, call_lock=None):
        self.market = market           # MarketData (live BTC price, same as paper bot)
        self.whales = whales           # orderbook.WhaleTracker (live flow, same tape)
        self.lighter = lighter         # lighter_live.LighterLive (real account)
        self.enabled = bool(getattr(config, "LIGHTER_BOT_ENABLED", False))
        self.leverage = float(getattr(config, "LIGHTER_BOT_LEVERAGE", LIVE_LEVERAGE))
        self.risk_frac = float(LIVE_RISK_FRAC)
        self.strategy = DEFAULT_STRATEGY      # which entry/exit ruleset to use (see STRATEGIES)
        self.pos: "LivePos | None" = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self._watching = False
        self._flip = 0                 # consecutive secs the exit signal has held
        self._live: dict = {}          # cached lighter.state()
        self._live_ts = 0.0
        self._busy = False             # serialize the bot's own order/close
        self._flat_confirm = 0         # consecutive FRESH live reads showing flat while we hold
        self._recon_ts = 0.0           # _live_ts of the last snapshot we counted toward reconcile
        self._last_close_ts = 0.0      # when we last closed; blocks re-adopting a just-closed position
                                       # off a STALE cache (that re-recorded the same trade repeatedly)
        self._last_close_sig = None    # side/entry/qty fingerprint of the position we just closed
        self._gpt_close_reason = None  # set by the background AI exit-manager when it says CLOSE
        self._gpt_manage_ts = 0.0      # last time we asked the AI to manage the open trade
        self._gpt_manage_busy = False  # an AI manage call is in flight
        self._scalp_cooldown_ts = 0.0  # wait this-recent after a scalp close before re-entering
        self._ft_cd_ts = 0.0           # flow-trail: last close time (for the re-entry cooldown)
        self._whale_last_seq = None    # last tape seq seen by whale-copy (None = start fresh)
        self._whale_cd_ts = 0.0        # last whale-copy entry/close time (for the cooldown)
        self._paper_bot = None         # paper Whale-Follow bot to MIRROR (set in main.py)
        self._freq_bot = None          # paper Freqtrade-style bot to MIRROR (set in main.py)
        self._freq_tried_id = None     # paper freq position id we've already attempted (one try, no chase)
        self._freq_improved_bot = None # paper Freqtrade-style improved TP/SL bot to MIRROR
        self._freq_improved_tried_id = None
        self._freq_trend_bot = None    # paper Freqtrade-style trend TP/SL bot to MIRROR (set in main.py)
        self._freq_trend_tried_id = None
        self._freq_5_bot = None        # paper Freqtrade-style improved TP/SL 5% bot to MIRROR (set in main.py)
        self._freq_5_tried_id = None
        self._tvstrats_bot = None      # TradingView Strategy Pack paper bot to MIRROR (set in main.py)
        self._tv_bb_rsi_tried_id = None
        self._rsi2atr_bot = None       # RSI2 EMA50 ATR-filter paper bot to MIRROR (set in main.py)
        self._rsi2_tried_id = None
        self._limit_mode = False       # TOGGLE: mirror via IOC LIMIT orders at the paper's exact
                                       # entry/exit price (OFF = the normal market-order mirror)
        self._whale_tried_pid = None   # limit mode: the paper position id we've already attempted
                                       # (one IOC-limit try per paper trade — no retrying)
        # ---- maker mean-reversion state ----
        self._mid_hist = deque(maxlen=4000)   # (ts, mid) samples for fair-value / trend means
        self._maker_phase = "idle"     # idle → bid_open → long (managing exit) ; resets on flat
        self._maker_bid_px = 0.0       # price of our resting post-only BUY (0 = none)
        self._maker_bid_ts = 0.0       # when we placed the resting BUY (for TTL)
        self._maker_ask_px = 0.0       # price of our resting post-only SELL exit (0 = none)
        self._maker_busy = False       # a maker order action (place/cancel) is in flight
        self._maker_synced = False     # cleared stale resting orders once at startup
        self._fv_attempt_ts = 0.0
        self._fv_fv = None
        self._fv_lt = None
        self._fv_dev_bps = None
        self._fv_n_leaders = 0
        self._fv_status = "starting"
        self._refresh_inflight = False # a background balance/position refresh is running
        self._lev_warm_ts = 0.0        # last time we pre-set leverage in the background
        self._lev_warm_busy = False    # a background leverage pre-set is in flight
        self._lock = call_lock or asyncio.Lock()   # SHARED with the manual panel: Lighter
                                                    # signed calls must never run concurrently
        self._load()

    # ---- persistence ----
    def _save(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "enabled": self.enabled,
                    "leverage": self.leverage,
                    "strategy": self.strategy,
                    "limit_mode": self._limit_mode,
                    "history": self.history[:200],
                    "log": self.log[:60],
                    "pos": asdict(self.pos) if self.pos else None,
                }, f)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH) as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.leverage = float(d.get("leverage", self.leverage))
            self._limit_mode = bool(d.get("limit_mode", False))
            saved_strat = d.get("strategy")
            if saved_strat in STRATEGIES:
                self.strategy = saved_strat
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            p = d.get("pos")
            if p:
                try:
                    self.pos = LivePos(**p)
                except Exception:
                    self.pos = None
            print(f"[lighter-bot] restored: {len(self.history)} past trades, "
                  f"{'a' if self.pos else 'no'} saved open position")
        except Exception:
            pass

    # ---- helpers (copied from news_whale_bot — pure functions of whales/market) ----
    def _btc(self):
        try:
            return self.market.price("BTCUSDT")
        except Exception:
            return None

    def _flow(self, window):
        """Buy/sell flow over the window from the SAME cross-exchange trade tape the
        'Trades · live · all exchanges' panel shows — so the bot's flow, its bar, and
        that panel all agree. Returns (buy_usd, sell_usd)."""
        try:
            return self.whales.flow(window)
        except Exception:
            return 0.0, 0.0

    async def trade_feed_loop(self):
        """Keep Lighter's own trade tape fresh (~1x/sec) so flow matches the Lighter
        screen. Runs only when Lighter is configured; off-thread so it never blocks."""
        await asyncio.sleep(2.0)
        while True:
            try:
                if self.lighter.configured():
                    await _to_thread(self.lighter.poll_trades)
            except Exception:
                pass
            await asyncio.sleep(1.0)

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:60]
        print(f"[lighter-bot] {msg}")

    def _ai_snapshot(self, px):
        """Tiny live context handed to the AI alongside the headline (GPTnews)."""
        then = self._price_ago(60)
        chg = round((px - then) / then * 100, 3) if (px and then) else None
        try:
            buy, sell = self._flow(60)
            tot = buy + sell
            flow = round(buy / tot * 100, 1) if tot > 0 else None
        except Exception:
            flow = None
        return {"price": round(px, 2) if px else None, "change_1m_pct": chg, "flow_buy_pct": flow}

    def _vol_usd(self, t_from, t_to):
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
        cut_ms, stop_ms = t0 * 1000, t0 * 1000 - 15000
        s = 0.0
        for t in reversed(self.whales.tape):
            ts = t["ts"]
            if ts < stop_ms:
                break
            if ts >= cut_ms:
                s += t["usd"] if t["side"] == "buy" else -t["usd"]
        return s

    def _whale_net_since(self, t0, min_usd=None):
        """Net 'whale print' USD since t0 — sum(+buys, -sells) of trades >= min_usd. >0 = whales
        net buying, <0 = net selling. min_usd defaults to NW_BIG_TRADE_USD ($100k); callers that
        only want bigger whales (e.g. News+Whale Trail wants $200k+) pass their own threshold."""
        thr = NW_BIG_TRADE_USD if min_usd is None else min_usd
        cut_ms, stop_ms = t0 * 1000, t0 * 1000 - 15000
        s = 0.0
        for t in reversed(self.whales.tape):
            ts = t["ts"]
            if ts < stop_ms:
                break
            if ts >= cut_ms and t["usd"] >= thr:
                s += t["usd"] if t["side"] == "buy" else -t["usd"]
        return s

    def _vol_pct(self, t_from, t_to):
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
        end = t0 - 5.0
        start = t0 - NW_BASELINE_MIN * 60.0
        stop_ms = start * 1000.0 - 15000.0
        rng, vol = {}, {}
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
        now = time.time()
        buy, sell = self._flow(window)            # Lighter's OWN flow
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

    # ---- live account (cached; refreshed off-thread) ----
    async def _refresh_live(self):
        try:
            # state() is a READ (ccxt) — do NOT hold the signed-order lock for it, or a manual
            # order would wait behind this background refresh (that was the ~10s open latency).
            st = await _to_thread(self.lighter.state)
            self._live = st or {}
        except Exception as e:
            self._live = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        self._live_ts = time.time()

    async def _bg_refresh(self):
        """Non-blocking wrapper so the manage loop never stalls on a balance refresh."""
        try:
            await self._refresh_live()
        finally:
            self._refresh_inflight = False

    async def _warm_leverage(self):
        """Background: pre-set leverage on Lighter so the next ORDER skips the slow inline
        leverage tx. set_leverage no-ops when the leverage is already cached, so this is cheap."""
        try:
            await _to_thread(self.lighter.set_leverage, self.leverage)
        except Exception:
            pass
        finally:
            self._lev_warm_busy = False

    def _free_balance(self):
        b = (self._live.get("balance") or {})
        try:
            return float(b.get("free") or b.get("total") or 0.0)
        except Exception:
            return 0.0

    def _total_balance(self):
        """Real total USDC collateral on Lighter. When flat this is the ground-truth
        balance; the change in it across a round trip IS the exact realized P&L."""
        b = (self._live.get("balance") or {})
        try:
            return float(b.get("total") or b.get("free") or 0.0)
        except Exception:
            return 0.0

    def _live_position(self):
        for p in (self._live.get("positions") or []):
            if (p.get("size") or 0):
                return p
        return None

    # ---- triggered by every news message (fast, non-blocking) ----
    async def on_news(self, source, text):
        if not self.enabled or self._watching or self.pos is not None:
            return
        if self.strategy in NO_NEWS_STRATEGIES:
            return   # CONTINUOUS / mirror strategies — not news-triggered (the freq mirrors
                     # ONLY copy their paper bot; they must never open a news trade of their own)
        if self.strategy == "claudenews":
            return   # claudenews now REACTS to the news feed's signal (on_claude_signal) — it no
                     # longer makes its own AI call here; the feed does the one Sonnet "thinking".
        if not self.lighter.configured():
            return
        asyncio.create_task(self._watch(source, text))

    async def on_claude_signal(self, headline, d):
        """REACT IMMEDIATELY to a Claude decision already computed by the NEWS FEED — no AI call
        of our own. Fired by pipeline the instant the feed's Sonnet analysis returns. Only acts
        when this bot is on 'claudenews', enabled, flat and idle. d is a claude_news decision
        (trade, direction, stop_pct, tp_pct, confidence, reason)."""
        if (self.strategy != "claudenews" or not self.enabled or self.pos is not None
                or self._busy or not self.lighter.configured()):
            return
        if d is None or getattr(d, "error", None):
            return
        if not getattr(d, "trade", False) or getattr(d, "direction", "") not in ("long", "short"):
            return
        px = self._btc()
        if not px:
            return
        if d.direction == "long":
            sl_px = px * (1 - d.stop_pct / 100.0); tp_px = px * (1 + d.tp_pct / 100.0)
        else:
            sl_px = px * (1 + d.stop_pct / 100.0); tp_px = px * (1 - d.tp_pct / 100.0)
        size_mult = 1.0 if str(d.confidence).lower() == "high" else 0.5
        detail = (f"Feed signal · Claude {d.confidence} ({size_mult:g}x) · stop -{d.stop_pct:.2f}% / "
                  f"tp +{d.tp_pct:.2f}% · {str(d.reason)[:40]}")
        await self._open(d.direction, px, 0.5, detail, headline,
                         sl=sl_px, tp=tp_px, size_mult=size_mult)

    async def _watch(self, source, text):
        if self.strategy in NO_NEWS_STRATEGIES:
            return   # hard stop: continuous/mirror strategies never trade the news, no matter the caller
        self._watching = True
        try:
            headline = (text or "").strip().replace("\n", " ")[:90]
            t0 = time.time()
            start_px = self._btc()
            if self.strategy == "newsplus":
                await self._watch_newsplus(headline, t0, start_px)
                return
            if self.strategy == "flowplus":
                await self._watch_flowplus(headline, t0, start_px)
                return
            if self.strategy == "gptnews":
                await self._watch_gptnews(headline, t0, start_px)
                return
            if self.strategy == "claudenews":
                await self._watch_claudenews(headline, t0, start_px)
                return
            if self.strategy == "newswhale":
                await self._watch_newswhale(headline, t0, start_px)
                return
            await self._watch_flow_news(headline, t0, start_px)
        except Exception as e:
            self._note(f"watch error: {type(e).__name__} {e}", "error")
        finally:
            self._watching = False

    async def _watch_flow_news(self, headline, t0, start_px):
        """'Flow + News' (original): wait for an abnormal reaction (volume AND
        volatility), then open in the direction price moved."""
        base_vol, base_volume = self._baseline(t0)
        if not base_vol or not base_volume:
            self._note(f"news → no baseline yet (need ~2+ min of data) → skipped: {headline}", "skip")
            return
        self._note(f"news → watching for a move ≥ {NW_REACT_MULT:g}× normal: {headline}")
        while (time.time() - t0 < config.NW_WATCH_SECONDS and self.enabled
               and self.pos is None):
            await asyncio.sleep(NW_CHECK_SECONDS)
            now = time.time()
            elapsed = now - t0
            px = self._btc()
            if not px or not start_px or elapsed < NW_MIN_ELAPSED:
                continue
            # ADAPTIVE GATE (identical to the paper bot)
            post_range = self._vol_pct(t0, now)
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
            buy, sell = self._flow(LIVE_FLOW_WINDOW)              # Lighter's OWN 1-min flow
            tot = buy + sell
            buy_pct = buy / tot if tot > 0 else 0.5
            detail = (f"price {moved*100:+.2f}% since news · {vol_x:.1f}× swing · "
                      f"{volume_x:.1f}× volume · 1m flow {buy_pct*100:.0f}% buy")
            if moved >= config.NW_MIN_MOVE_PCT:
                await self._open("long", px, buy_pct, detail, headline)
                return
            if moved <= -config.NW_MIN_MOVE_PCT:
                await self._open("short", px, 1 - buy_pct, detail, headline)
                return
            # abnormal move, but price hasn't picked a clear direction yet → keep watching
        if self.pos is None:
            self._note(f"no clear directional move within {config.NW_WATCH_SECONDS:g}s → skipped", "skip")

    async def _watch_newsplus(self, headline, t0, start_px):
        """'News+': INSTANT entry. The moment the news prints, pick the side from ORDER
        FLOW (no waiting for a price move) and fire — buyers in control → long, sellers
        → short. If flow is too balanced to call a side, wait a tick and re-check until
        one appears or we give up. Exits are handled in manage_loop."""
        self._note(f"news → News+ instant: side from flow "
                   f"(≥ {NEWSPLUS_FLOW_DIR*100:.0f}% one way over {NEWSPLUS_FLOW_WINDOW:g}s): {headline}")
        while (time.time() - t0 < NEWSPLUS_GIVEUP_SECS and self.enabled
               and self.pos is None):
            px = self._btc()
            buy, sell = self._flow(NEWSPLUS_FLOW_WINDOW)         # Lighter's OWN flow
            tot = buy + sell
            if px and tot > 0:
                buy_pct = buy / tot
                if buy_pct >= NEWSPLUS_FLOW_DIR:
                    side, fp = "long", buy_pct
                elif buy_pct <= (1 - NEWSPLUS_FLOW_DIR):
                    side, fp = "short", 1 - buy_pct
                else:
                    side = None                       # flow too balanced → wait for a clear side
                if side:
                    detail = (f"flow {buy_pct*100:.0f}% buy over {NEWSPLUS_FLOW_WINDOW:g}s "
                              f"→ {side.upper()} [News+ instant]")
                    await self._open(side, px, fp, detail, headline)
                    return
            await asyncio.sleep(NW_CHECK_SECONDS)     # checked immediately first, then re-poll
        if self.pos is None:
            self._note(f"News+ → flow stayed balanced (no clear side) within "
                       f"{NEWSPLUS_GIVEUP_SECS:g}s → skipped", "skip")

    async def _watch_flowplus(self, headline, t0, start_px):
        """'Flow+': pure flow. The instant the news prints, bet the way flow is
        leaning — any majority counts (>50% buy → long, <50% buy → short). The exit
        (manage_loop) closes the moment flow crosses back past 50%."""
        self._note(f"news → Flow+ instant: side = flow majority over "
                   f"{FLOWPLUS_FLOW_WINDOW:g}s (>50% buy → long): {headline}")
        while (time.time() - t0 < FLOWPLUS_GIVEUP_SECS and self.enabled
               and self.pos is None):
            px = self._btc()
            buy, sell = self._flow(FLOWPLUS_FLOW_WINDOW)         # Lighter's OWN flow
            tot = buy + sell
            if px and tot > 0:
                buy_pct = buy / tot
                side = "long" if buy_pct >= 0.50 else "short"
                fp = buy_pct if side == "long" else (1 - buy_pct)
                detail = f"flow {buy_pct*100:.0f}% buy → {side.upper()} [Flow+]"
                await self._open(side, px, fp, detail, headline)
                return
            await asyncio.sleep(NW_CHECK_SECONDS)     # checked immediately first, then re-poll
        if self.pos is None:
            self._note(f"Flow+ → no flow to read within {FLOWPLUS_GIVEUP_SECS:g}s → skipped", "skip")

    async def _watch_gptnews(self, headline, t0, start_px):
        """'GPTnews' (REAL money): send the headline to the AI (Groq) the instant it
        arrives. The AI decides trade/skip, direction, and the stop-loss + take-profit.
        If it says trade, open a REAL Lighter position with that plan; manage_loop exits
        on the AI's SL/TP."""
        px = self._btc()
        if not px:
            self._note(f"GPTnews: no BTC price yet → skipped: {headline[:60]}", "skip")
            return
        import gpt_news
        d = await gpt_news.decide(headline, self._ai_snapshot(px))
        if not self.enabled or self.pos is not None:
            return
        if d.error:
            self._note(f"GPTnews error ({d.error[:70]}) → skipped: {headline[:60]}", "error")
            return
        if not d.trade or d.direction not in ("long", "short"):
            self._note(f"GPTnews → SKIP ({d.reason[:60]}) [{d.latency_ms:.0f}ms]: {headline[:60]}",
                       "skip")
            return
        px2 = self._btc() or px
        # No fixed SL/TP — the AI watches the open trade and decides the exit itself.
        self._gpt_close_reason = None
        self._gpt_manage_ts = time.time()   # first AI exit-check fires ~GPTNEWS_MANAGE_SECS later
        self._gpt_manage_busy = False
        detail = (f"GPTnews {d.confidence} · AI-managed exit · {d.reason[:50]} "
                  f"[{d.latency_ms:.0f}ms]")
        await self._open(d.direction, px2, 0.5, detail, headline)

    async def _watch_claudenews(self, headline, t0, start_px):
        """'claude news haiku' (REAL money): identical flow to GPTnews, but the model is
        Claude WITH adaptive thinking. The AI decides trade/skip, direction and SL/TP;
        manage_loop runs the AI-managed exit (which also uses Claude — see _gpt_manage_check)."""
        px = self._btc()
        if not px:
            self._note(f"Claude: no BTC price yet → skipped: {headline[:60]}", "skip")
            return
        import claude_news
        d = await claude_news.decide(headline, self._ai_snapshot(px))
        if not self.enabled or self.pos is not None:
            return
        if d.error:
            self._note(f"Claude error ({d.error[:70]}) → skipped: {headline[:60]}", "error")
            return
        if not d.trade or d.direction not in ("long", "short"):
            self._note(f"Claude → SKIP ({d.reason[:60]}) [{d.latency_ms:.0f}ms]: {headline[:60]}",
                       "skip")
            return
        px2 = self._btc() or px
        # TRADE THE AI's OWN PLAN: turn its stop_loss_pct / take_profit_pct (BTC price-move %)
        # into real SL/TP prices and hand them to the bracket exit (_exit_claudenews). The old
        # path threw these away and let an every-4s AI babysitter scratch the trade at ±0.01%.
        if d.direction == "long":
            sl_px = px2 * (1 - d.stop_pct / 100.0)
            tp_px = px2 * (1 + d.tp_pct / 100.0)
        else:
            sl_px = px2 * (1 + d.stop_pct / 100.0)
            tp_px = px2 * (1 - d.tp_pct / 100.0)
        # CONVICTION SIZING: bet full on high conviction, half on medium (accuracy/risk control).
        size_mult = 1.0 if str(d.confidence).lower() == "high" else 0.5
        detail = (f"Claude {d.confidence} ({size_mult:g}x) · plan stop -{d.stop_pct:.2f}% / "
                  f"tp +{d.tp_pct:.2f}% · {d.reason[:40]} [{d.latency_ms:.0f}ms]")
        await self._open(d.direction, px2, 0.5, detail, headline,
                         sl=sl_px, tp=tp_px, size_mult=size_mult)

    def _should_adopt(self, now):
        """True when we should adopt an untracked REAL Lighter position (self-heal). Requires the bot
        enabled, a configured account, and a live snapshot showing a position — AND that the snapshot
        post-dates our last close by the reconcile grace. That last guard is the fix for the duplicate
        trades: right after a close the CACHED snapshot is stale (still shows the now-closed position),
        and without this we'd re-adopt + re-close + re-record the SAME trade every tick until the
        background refresh caught up."""
        if not (self.enabled and self.lighter.configured() and self._live.get("ok")):
            return False
        lp = self._live_position()
        if lp is None:
            return False
        if not (self._live_ts > self._last_close_ts
                and (now - self._last_close_ts) >= LIVE_RECONCILE_GRACE):
            return False
        sig = self._last_close_sig or {}
        if sig and (now - float(sig.get("ts") or 0.0)) < LIVE_ADOPT_BLOCK_SECS:
            try:
                same_side = (lp.get("side") or "") == sig.get("side")
                same_entry = abs(float(lp.get("entry") or 0.0) - float(sig.get("entry") or 0.0)) <= 1.0
                q0 = abs(float(sig.get("qty") or 0.0))
                q1 = abs(float(lp.get("size") or 0.0))
                same_qty = abs(q1 - q0) <= max(0.000001, q0 * 0.01)
                if same_side and same_entry and same_qty:
                    return False
            except Exception:
                return False
        return True

    def _remember_closed_pos(self, pos, ts=None):
        try:
            self._last_close_sig = {
                "side": pos.side,
                "entry": float(pos.entry or 0.0),
                "qty": abs(float(pos.qty or 0.0)),
                "ts": float(ts or time.time()),
            }
        except Exception:
            self._last_close_sig = {"ts": float(ts or time.time())}

    def _adopt_live_position(self, lp):
        """Take ownership of a REAL Lighter position the bot isn't tracking — one left open across a
        restart, an unfilled limit close, or opened externally. The active strategy's exit then
        manages and closes it, so the bot NEVER gets stuck refusing to trade because the account
        already holds a position it forgot about."""
        try:
            side = lp.get("side") or "long"
            qty = abs(float(lp.get("size") or 0))
            if qty <= 0:
                return
            entry = float(lp.get("entry") or 0) or (self._btc() or 0.0)
        except Exception:
            return
        self.pos = LivePos(side=side, entry=entry, qty=qty, leverage=self.leverage,
                           opened_at=time.time(), news="adopted existing Lighter position",
                           peak=entry, bal_open=self._total_balance())
        self._flat_confirm = 0
        self._note(f"adopted existing Lighter {side.upper()} {qty:.6f} BTC @ ${entry:,.1f} — "
                   f"now managing it with the active strategy (it will exit normally)", "open")
        self._save()

    # ---- open / close: REAL orders on Lighter ----
    async def _open(self, side, price, flow_pct, detail, headline, sl=0.0, tp=0.0, size_mult=1.0,
                    leverage=None, slippage=None):
        if self._busy or self.pos is not None:
            return
        lev = float(leverage or self.leverage)     # per-trade leverage (maker uses a SAFE low one)
        self._busy = True
        try:
            # Refresh the REAL account FIRST: (a) size off a fresh balance and, critically,
            # (b) refuse to open on top of a position that is already on Lighter. An untracked
            # leftover (e.g. a prior trade that didn't close) would otherwise NET against this
            # order — a buy landing on a stale short leaves you SHORT while the bot thinks it's
            # long, which is exactly how the displays end up disagreeing with the account.
            await self._refresh_live()
            existing = self._live_position()
            if existing is not None and (existing.get("size") or 0):
                # Don't stack onto a position that's already on the account. Instead of refusing
                # forever, ADOPT it so the strategy manages/closes it — the bot self-heals. But not
                # if we JUST closed (Lighter can lag a few secs); the grace avoids re-adopting a
                # position that's actually already gone.
                if self.pos is None and self._should_adopt(time.time()):
                    self._adopt_live_position(existing)
                return
            free = self._free_balance()
            # Keep a buffer: using 100% of collateral as margin gets the fill rejected for fees.
            # size_mult scales the bet by conviction (high=full, medium=half).
            margin = round(free * self.risk_frac * LIVE_MARGIN_BUFFER * max(0.1, size_mult), 2)
            if margin < LIVE_MIN_MARGIN:
                self._note(f"wanted {side.upper()} but free balance ${free:.2f} is too low → skip", "skip")
                return
            order_side = "buy" if side == "long" else "sell"
            async with self._lock:
                # Pass our live price so the SDK skips a get_best_price round-trip (faster fill).
                # slippage (when set, e.g. Flow Trail) caps the fill at ~the best price = a top-of-
                # book marketable LIMIT instead of a market order that can pay up to 2%.
                res = await _to_thread(self.lighter.order, order_side, None, margin,
                                       lev, "market", price, False, False, "BTC", False, slippage)
            if not res.get("ok"):
                # most common: real trading is OFF -> this is the safe dry-run path
                self._note(f"WOULD OPEN {side.upper()} ({detail}) — not placed: {res.get('error')}", "skip")
                return
            note = res.get("lev_note")
            # READ BACK the REAL position so side/entry/qty match the account EXACTLY. The order
            # result only carries the REFERENCE price (~best price), not the true fill — trusting
            # it was why the logged entry ($63,036) differed from the real fill ($62,881.8). Retry
            # a few times in case the fill hasn't propagated to the positions endpoint yet.
            real_side = side
            real_entry = float(res.get("average") or 0) or price or 0.0
            real_qty = float(res.get("amount") or 0)
            for _ in range(4):
                await self._refresh_live()
                lp = self._live_position()
                if lp is not None and (lp.get("size") or 0):
                    real_side = (lp.get("side") or side)
                    real_entry = float(lp.get("entry") or 0) or real_entry
                    real_qty = abs(float(lp.get("size") or 0)) or real_qty
                    break
                await asyncio.sleep(0.5)
            if real_side != side:
                # The account ended up on the OTHER side than intended (e.g. netted against a
                # leftover). Manage the REAL side so the trailing stop and Close act correctly.
                self._note(f"⚠ intended {side.upper()} but Lighter shows {real_side.upper()} — "
                           f"managing the REAL position", "error")
            self.pos = LivePos(side=real_side, entry=real_entry, qty=real_qty, leverage=lev,
                               opened_at=time.time(), news=headline, peak=real_entry,
                               bal_open=self._total_balance(),   # baseline for exact realized P&L
                               sl=round(sl, 2), tp=round(tp, 2))  # GPTnews AI stop / target
            self._note(f"OPEN {real_side.upper()} @ ${real_entry:,.1f} · {detail} · "
                       f"margin ${margin:.2f} {lev:g}x" + (f" · {note}" if note else ""), "open")
            self._flat_confirm = 0
            self._save()
        except Exception as e:
            self._note(f"open error: {type(e).__name__}: {str(e)[:120]}", "error")
        finally:
            self._busy = False

    async def _close(self, reason, exit_px=None, slippage=None):
        pos = self.pos
        if pos is None or self._busy:
            return
        self._busy = True
        try:
            async with self._lock:
                # fast path: hand the SDK our size+side (reduce-only, so a stale size is safe) so the
                # close skips the slow position read; slippage caps the close fill near the best price.
                res = await _to_thread(self.lighter.close, None, pos.qty, pos.side, "BTC", slippage)
            px = exit_px or self._btc() or pos.entry
            ok = bool(res.get("ok"))
            est = pos.unrealized(px)
            rec = {"side": pos.side, "entry": round(pos.entry, 2), "exit": round(px, 2),
                   "qty": round(pos.qty, 6), "leverage": pos.leverage, "pnl": round(est, 2),
                   "real": False, "pnl_est": round(est, 2),
                   "reason": reason, "news": pos.news, "opened_at": pos.opened_at,
                   "closed_at": time.time(), "ok": ok,
                   "error": "" if ok else str(res.get("error", ""))[:140]}
            self.history.insert(0, rec)
            self.history = self.history[:200]
            tail = "" if ok else f"  [CLOSE FAILED: {rec['error']} — check Lighter!]"
            self._note(f"CLOSE {pos.side.upper()} @ ${px:,.0f} · {reason} · P&L ${est:+.2f} (est)"
                       f"{tail}", "win" if est >= 0 else "loss")
            bal_open = pos.bal_open
            # FREE the bot the instant the close order is placed — do NOT block on the 2s settle.
            close_ts = time.time()
            self._remember_closed_pos(pos, close_ts)
            self.pos = None
            self._flip = 0
            self._last_close_ts = close_ts   # block stale-cache re-adoption of this closed position
            self._save()
            asyncio.create_task(self._refresh_live())   # refresh the cache ASAP so it reflects the close
            # Correct to EXACT realized P&L (real balance change) + write the CSV, in the BACKGROUND.
            if ok and bal_open:
                asyncio.create_task(self._settle_close_pnl(rec, bal_open))
            else:
                self._append_csv(rec)
        except Exception as e:
            self._note(f"close error: {type(e).__name__}: {str(e)[:120]}", "error")
        finally:
            self._busy = False

    async def _settle_close_pnl(self, rec, bal_open):
        """Background: after the close, read the real balance change for EXACT realized P&L,
        update the (already-shown) record, and log the CSV. The close itself never waits on this."""
        try:
            await asyncio.sleep(LIVE_SETTLE_SECS)
            await self._refresh_live()
            tot = self._total_balance()
            if tot:
                rec["pnl"] = round(tot - bal_open, 2)
                rec["real"] = True
                self._save()
        except Exception:
            pass
        self._append_csv(rec)

    def _append_csv(self, rec):
        try:
            new = not os.path.exists(CSV_PATH)
            with open(CSV_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["closed_at", "side", "entry", "exit", "qty", "leverage",
                                "pnl", "reason", "news"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["closed_at"])),
                            rec["side"], rec["entry"], rec["exit"], rec["qty"], rec["leverage"],
                            rec["pnl"], rec["reason"], rec.get("news", "")])
        except Exception:
            pass

    # ---- runs every second: refresh, reconcile, SIGNAL-DRIVEN exits ----
    async def manage_loop(self):
        await asyncio.sleep(2.0)
        while True:
            # Scalper needs fast detection (it acts the instant flow crosses 80%); news
            # strategies are fine at 1s.
            await asyncio.sleep(0.3 if self.strategy in ("scalper", "flowtrail", "newswhale",
                                                         "whale100_live") else 1.0)
            now = time.time()
            # Refresh the cached balance/position in the BACKGROUND so it NEVER stalls the
            # entry/exit checks. The old blocking refresh added ~1-2s of lag every 5s.
            if now - self._live_ts >= LIVE_STATE_REFRESH and not self._refresh_inflight:
                self._refresh_inflight = True
                asyncio.create_task(self._bg_refresh())

            # Keep leverage PRE-SET on Lighter in the background so the actual order never sends
            # the slow leverage transaction inline — this is what makes the bot's fills as fast as
            # the manual panel (it only sends a tx when the leverage actually changes).
            if (self.enabled and self.lighter.configured() and not self._lev_warm_busy
                    and (now - self._lev_warm_ts) >= 20.0):
                self._lev_warm_busy = True
                self._lev_warm_ts = now
                asyncio.create_task(self._warm_leverage())

            # Reconcile: if WE think we hold a position but Lighter shows flat, it was
            # closed elsewhere (manual close, liquidation, etc.). Two guards keep this from
            # firing on stale/glitchy data and flattening a position that is actually live:
            #   1) The snapshot must be FRESH — taken at least LIVE_RECONCILE_GRACE secs AFTER
            #      we opened. The pre-open snapshot naturally shows flat; trusting it was the
            #      bug that closed real positions for ~$0 right after opening.
            #   2) We need LIVE_RECONCILE_CONFIRM *consecutive distinct* fresh reads showing
            #      flat. One empty/late read can't end the position on its own.
            if (self.pos is not None and self._live.get("ok")
                    and self._live_position() is None):
                fresh = self._live_ts >= self.pos.opened_at + LIVE_RECONCILE_GRACE
                new_read = self._live_ts > self._recon_ts
                if fresh and new_read:
                    self._recon_ts = self._live_ts
                    self._flat_confirm += 1
                    if self._flat_confirm >= LIVE_RECONCILE_CONFIRM:
                        px = self._btc() or self.pos.entry
                        est = self.pos.unrealized(px)
                        # exact realized P&L from the real balance change (we're flat now)
                        tot = self._total_balance()
                        real_pnl = (tot - self.pos.bal_open) if (self.pos.bal_open and tot) else None
                        pnl = round(real_pnl if real_pnl is not None else est, 2)
                        rec = {"side": self.pos.side, "entry": round(self.pos.entry, 2),
                               "exit": round(px, 2), "qty": round(self.pos.qty, 6),
                               "leverage": self.pos.leverage, "pnl": pnl,
                               "real": real_pnl is not None, "pnl_est": round(est, 2),
                               "reason": "closed_externally", "news": self.pos.news,
                               "opened_at": self.pos.opened_at, "closed_at": now,
                               "ok": True, "error": ""}
                        self.history.insert(0, rec); self.history = self.history[:200]
                        self._note(f"position no longer on Lighter (closed externally, or the "
                                   f"order never filled) · ~P&L ${pnl:+.2f}",
                                   "win" if pnl >= 0 else "loss")
                        self._append_csv(rec)
                        self._remember_closed_pos(self.pos, now)
                        self._last_close_ts = now
                        self.pos = None; self._flip = 0; self._flat_confirm = 0; self._save()
                        continue
                # not yet confirmed (or snapshot too old to trust) → keep managing the position
            elif self._flat_confirm:
                # Lighter shows our position again (or read failed) → it never went away.
                self._flat_confirm = 0

            # SIDE/ENTRY RESYNC: keep our tracked position glued to the REAL Lighter position. If
            # the account is on the OTHER side than we think (e.g. a trade that netted against a
            # leftover), adopt reality so the trailing stop manages it correctly and Close sends a
            # reduce-only on the RIGHT side (a wrong-side reduce-only is rejected and never flattens).
            if self.pos is not None:
                lp = self._live_position()
                if lp is not None and (lp.get("size") or 0):
                    rs = lp.get("side")
                    if rs and rs != self.pos.side:
                        self._note(f"⚠ resync: Lighter holds {rs.upper()} but bot tracked "
                                   f"{self.pos.side.upper()} — adopting the REAL side so Close works",
                                   "error")
                        self.pos.side = rs
                        self.pos.entry = float(lp.get("entry") or 0) or self.pos.entry
                        self.pos.qty = abs(float(lp.get("size") or 0)) or self.pos.qty
                        self.pos.peak = self.pos.entry   # restart the trail from the real entry

            # MAKER samples the live mid every tick to build its fair-value / trend means,
            # whether flat or in a position (it needs them in both states).
            if self.strategy == "maker":
                m = self._btc()
                if m:
                    self._mid_hist.append((now, m))

            if self.pos is None:
                # SELF-HEAL: if Lighter holds a position we aren't tracking (an unfilled limit close,
                # one left open across a restart, or opened externally), ADOPT it so the strategy
                # manages/closes it — instead of the open-guard refusing to ever trade again. The
                # grace inside _should_adopt stops us re-adopting a position we just closed (which
                # was re-recording the same trade over and over off a stale cache).
                if self._should_adopt(now):
                    self._adopt_live_position(self._live_position())
            if self.pos is None:
                self._flip = 0
                self._gpt_close_reason = None
                # SCALPER is continuous: when flat, look for a flow-momentum entry every tick.
                if self.strategy == "scalper" and self.enabled and self.lighter.configured():
                    await self._scalper_entry(now)
                # MAKER is continuous: when flat, manage the resting post-only BUY (place / reprice /
                # cancel) and promote to a long the instant it fills.
                elif self.strategy == "maker" and self.lighter.configured():
                    await self._maker_flat(now)
                # WHALE FOLLOW is continuous: when flat, copy the latest >= MIN_USD tape print.
                elif self.strategy == "whale_follow" and self.enabled and self.lighter.configured():
                    await self._whale_follow_entry(now)
                elif self.strategy == "whale_follow_100k" and self.enabled and self.lighter.configured():
                    await self._whale100_entry(now)
                # FLOW TRAIL is continuous: when flat, enter the way the flow leans (>= 60%).
                elif self.strategy == "flowtrail" and self.enabled and self.lighter.configured():
                    await self._flowtrail_entry(now)
                # WHALE 100k LIVE is continuous: independently copy >= $100k prints via best-price limit.
                elif self.strategy == "whale100_live" and self.enabled and self.lighter.configured():
                    await self._whale100_live_entry(now)
                # FREQ LIVE is continuous: mirror the paper Freqtrade-style bot via best-price limit.
                elif self.strategy == "freq_live" and self.enabled and self.lighter.configured():
                    await self._freq_mirror_entry(now)
                elif self.strategy == "freq_improved_live" and self.enabled and self.lighter.configured():
                    await self._freq_improved_mirror_entry(now)
                elif self.strategy == "freq_trend_live" and self.enabled and self.lighter.configured():
                    await self._freq_trend_mirror_entry(now)
                elif self.strategy == "freq5_live" and self.enabled and self.lighter.configured():
                    await self._freq_5_mirror_entry(now)
                elif self.strategy == "tv_bb_rsi_live" and self.enabled and self.lighter.configured():
                    await self._tv_bb_rsi_mirror_entry(now)
                elif self.strategy == "rsi2atr_live" and self.enabled and self.lighter.configured():
                    await self._rsi2_mirror_entry(now)
                elif self.strategy == "fv_live" and self.enabled and self.lighter.configured():
                    await self._fv_live_entry(now)
                continue
            px = self._btc()
            if not px:
                continue
            # exits depend on the selected strategy
            if self.strategy == "newsplus":
                await self._exit_newsplus(now, px)
            elif self.strategy == "flowplus":
                await self._exit_flowplus(now, px)
            elif self.strategy == "gptnews":
                await self._exit_gptnews(now, px)
            elif self.strategy == "claudenews":
                await self._exit_claudenews(now, px)
            elif self.strategy == "scalper":
                await self._exit_scalper(now, px)
            elif self.strategy == "maker":
                await self._exit_maker(now, px)
            elif self.strategy == "whale_follow":
                await self._exit_whale_follow(now, px)
            elif self.strategy == "whale_follow_100k":
                await self._exit_whale100(now, px)
            elif self.strategy == "whale100_live":
                await self._exit_whale100_live(now, px)
            elif self.strategy == "freq_live":
                await self._exit_freq_mirror(now, px)
            elif self.strategy == "freq_improved_live":
                await self._exit_freq_improved_mirror(now, px)
            elif self.strategy == "freq_trend_live":
                await self._exit_freq_trend_mirror(now, px)
            elif self.strategy == "freq5_live":
                await self._exit_freq_5_mirror(now, px)
            elif self.strategy == "tv_bb_rsi_live":
                await self._exit_tv_bb_rsi_mirror(now, px)
            elif self.strategy == "rsi2atr_live":
                await self._exit_rsi2_mirror(now, px)
            elif self.strategy == "fv_live":
                await self._exit_fv_live(now, px)
            elif self.strategy == "flowtrail":
                await self._exit_flowtrail(now, px)
            elif self.strategy == "newswhale":
                await self._exit_newswhale(now, px)   # percentage trailing stop that follows price
            else:
                await self._exit_flow_news(now, px)

    def _profit_pct(self, pos, px):
        """Signed profit as a fraction, positive when the trade is in our favour."""
        if not pos.entry or not px:
            return 0.0
        return (px - pos.entry) / pos.entry if pos.side == "long" \
            else (pos.entry - px) / pos.entry

    async def _exit_flow_news(self, now, px):
        """'Flow + News' exits: volatility trailing stop, flow-flip, time-stop backstop."""
        pos = self.pos
        if pos is None:
            return
        vol = self._vol_pct(now - 90, now)
        dist = NW_TRAIL_MIN_PCT
        if vol is not None:
            dist = max(NW_TRAIL_MIN_PCT, min(NW_TRAIL_MAX_PCT, NW_TRAIL_VOL_MULT * vol / 100.0))
        if pos.peak <= 0:
            pos.peak = pos.entry

        # 1) VOLATILITY TRAILING STOP
        if pos.side == "long":
            pos.peak = max(pos.peak, px)
            if px <= pos.peak * (1 - dist):
                await self._close(f"trail_stop ({dist*100:.2f}% off peak)", px); return
        else:
            pos.peak = min(pos.peak, px)
            if px >= pos.peak * (1 + dist):
                await self._close(f"trail_stop ({dist*100:.2f}% off low)", px); return

        # 2) FLOW-FLIP SIGNAL EXIT
        votes, buy_pct, moved = self._recent_votes(LIVE_FLOW_WINDOW)
        dir_score = votes if pos.side == "long" else -votes
        if dir_score <= NW_EXIT_VOTES:
            self._flip += 1
            if self._flip >= NW_EXIT_CONFIRM:
                await self._close(f"signal_exit (votes {votes:+d}, flow {buy_pct*100:.0f}%)", px); return
        else:
            self._flip = 0

        # 3) MAX-HOLD TIME STOP (Lighter handles real liquidation; reconcile catches it)
        if (now - pos.opened_at) >= config.NW_MAX_HOLD_MIN * 60:
            await self._close("time_stop", px); return

    async def _exit_newsplus(self, now, px):
        """'News+' exits: strict hard stop + no-progress cut (S1+S3), and a tight,
        ratcheting trailing stop plus flow-flip take-profit (X3+X4)."""
        pos = self.pos
        if pos is None:
            return
        if pos.peak <= 0:
            pos.peak = pos.entry
        prof = self._profit_pct(pos, px)
        held = now - pos.opened_at

        # 1) HARD STOP (S1) — strict, fixed distance from entry, checked first
        if pos.side == "long":
            if px <= pos.entry * (1 - NEWSPLUS_HARD_STOP):
                await self._close(f"hard_stop (-{NEWSPLUS_HARD_STOP*100:.2f}%)", px); return
        else:
            if px >= pos.entry * (1 + NEWSPLUS_HARD_STOP):
                await self._close(f"hard_stop (-{NEWSPLUS_HARD_STOP*100:.2f}%)", px); return

        # 2a) EARLY-MOVE CHECK — one time, ~NEWSPLUS_EARLY_SECS after opening: the trade must
        #     be at least NEWSPLUS_EARLY_MIN in our favour by then, or it never reacted → cut it.
        if not pos.early_done and held >= NEWSPLUS_EARLY_SECS:
            if prof < NEWSPLUS_EARLY_MIN:
                await self._close(f"early_check ({held:.0f}s, only {prof*100:+.3f}% — need "
                                  f"{NEWSPLUS_EARLY_MIN*100:.3f}%)", px); return
            pos.early_done = True   # cleared the gate → don't check again

        # 2b) NO-PROGRESS CUT (S3) — the news edge is fast; if it isn't working, get out
        if held >= NEWSPLUS_NOPROG_SECS and prof <= 0:
            await self._close(f"no_progress ({held:.0f}s, not green)", px); return

        # 3) TRAILING TAKE-PROFIT (X3 + ratchet X4) — tighten the trail once in profit so a
        #    stall after a run locks the win
        trail = NEWSPLUS_TRAIL_TIGHT if prof >= NEWSPLUS_TRAIL_ARM else NEWSPLUS_TRAIL
        if pos.side == "long":
            pos.peak = max(pos.peak, px)
            if px <= pos.peak * (1 - trail):
                await self._close(f"trail_lock ({trail*100:.2f}% off peak)", px); return
        else:
            pos.peak = min(pos.peak, px)
            if px >= pos.peak * (1 + trail):
                await self._close(f"trail_lock ({trail*100:.2f}% off low)", px); return

        # 4) FLOW-FLIP EXIT (take-profit OR stop) — close when the tape turns against the
        #    position and HOLDS against it for NEWSPLUS_EXIT_CONFIRM secs (2s confirmation)
        votes, buy_pct, moved = self._recent_votes(LIVE_FLOW_WINDOW)
        dir_score = votes if pos.side == "long" else -votes
        if dir_score <= NW_EXIT_VOTES:
            self._flip += 1
            if self._flip >= NEWSPLUS_EXIT_CONFIRM:
                await self._close(f"flow_exit (votes {votes:+d}, flow {buy_pct*100:.0f}%)", px); return
        else:
            self._flip = 0

        # 5) MAX-HOLD backstop
        if held >= config.NW_MAX_HOLD_MIN * 60:
            await self._close("time_stop", px); return

    async def _exit_flowplus(self, now, px):
        """'Flow+' exit: close the moment flow crosses back past 50% against the
        position (long exits when buy% < 50%; short exits when buy% > 50%). A short
        confirmation avoids flicker right at the 50% line. Max-hold is the backstop;
        there is NO price stop-loss — flow alone governs (the reconcile still catches
        a real liquidation)."""
        pos = self.pos
        if pos is None:
            return
        buy, sell = self._flow(FLOWPLUS_FLOW_WINDOW)         # Lighter's OWN flow
        tot = buy + sell
        buy_pct = buy / tot if tot > 0 else 0.5
        against = (buy_pct < 0.50) if pos.side == "long" else (buy_pct > 0.50)
        if against:
            self._flip += 1
            if self._flip >= FLOWPLUS_EXIT_CONFIRM:
                await self._close(f"flow_exit (flow {buy_pct*100:.0f}% buy < 50% on its side)", px)
                return
        else:
            self._flip = 0
        if (now - pos.opened_at) >= config.NW_MAX_HOLD_MIN * 60:
            await self._close("time_stop", px); return

    # ---- "Scalper" — continuous flow-momentum scalp (runs in manage_loop, not on news) ----
    async def _scalper_entry(self, now):
        """When flat: read the live taker-flow lean; if it leans HARD, scalp that way."""
        if self._busy or (now - self._scalp_cooldown_ts) < SCALP_COOLDOWN:
            return
        buy, sell = self._flow(SCALP_FLOW_WINDOW)            # cross-exchange taker flow
        tot = buy + sell
        if tot < SCALP_MIN_VOL_USD:                          # tape too thin to trust
            return
        px = self._btc()
        if not px:
            return
        bpct = buy / tot
        if bpct >= SCALP_ENTER_PCT:
            await self._open("long", px, bpct,
                             f"Scalper · {bpct*100:.0f}% buy-flow over {SCALP_FLOW_WINDOW}s", "scalp",
                             size_mult=SCALP_SIZE_MULT)
        elif bpct <= (1 - SCALP_ENTER_PCT):
            await self._open("short", px, 1 - bpct,
                             f"Scalper · {(1-bpct)*100:.0f}% sell-flow over {SCALP_FLOW_WINDOW}s", "scalp",
                             size_mult=SCALP_SIZE_MULT)

    async def _exit_scalper(self, now, px):
        """Scalp exit: close the INSTANT the flow on our side falls below SCALP_ENTER_PCT
        (no confirmation delay). A wide safety stop + max-hold sit underneath only to prevent
        a flash-crash liquidation."""
        pos = self.pos
        if pos is None:
            return
        # safety hard stop (anti-liquidation only)
        if self._profit_pct(pos, px) <= -SCALP_STOP_PCT:
            await self._close(f"scalp_safety_stop (-{SCALP_STOP_PCT*100:.2f}%)", px)
            self._scalp_cooldown_ts = now; return
        # PRIMARY: the moment our side's flow drops below the entry threshold, close instantly
        buy, sell = self._flow(SCALP_FLOW_WINDOW)
        tot = buy + sell
        if tot > 0:
            on_side = (buy / tot) if pos.side == "long" else (sell / tot)
            if on_side < SCALP_ENTER_PCT:
                await self._close(f"flow_drop ({on_side*100:.0f}% < {SCALP_ENTER_PCT*100:.0f}%)", px)
                self._scalp_cooldown_ts = now; return
        # safety max-hold backstop
        if (now - pos.opened_at) >= SCALP_MAX_HOLD:
            await self._close("scalp_time", px); self._scalp_cooldown_ts = now; return

    # ---- "Whale Copy" — REAL money: copy any >= WHALE_MIN_USD tape print, fixed $ TP/SL ----
    async def _whale_follow_entry(self, now):
        """When flat: open a REAL position the instant a single trade >= WHALE_MIN_USD prints on
        the live cross-exchange tape (buy → long, sell → short)."""
        if self._busy or (now - self._whale_cd_ts) < WHALE_COOLDOWN:
            return
        tape = self.whales.tape
        if not tape:
            return
        latest_seq = tape[-1]["seq"]
        if self._whale_last_seq is None:        # first run: ignore old prints, start fresh
            self._whale_last_seq = latest_seq
            return
        signal = None
        for t in reversed(tape):                # newest-first: the latest NEW qualifying print
            if t["seq"] <= self._whale_last_seq:
                break
            if t["usd"] >= WHALE_MIN_USD:
                signal = t
                break
        self._whale_last_seq = latest_seq
        if signal is None:
            return
        px = self._btc()
        if not px:
            return
        side = "long" if signal["side"] == "buy" else "short"
        detail = (f"Whale copy · {signal['side'].upper()} ${signal['usd']:,.0f} on {signal['ex']} "
                  f"· TP +${WHALE_TP_USDT:.2f} / SL -${WHALE_SL_USDT:.2f}")
        await self._open(side, px, 0.5, detail, f"Whale {signal['side']} ${signal['usd']:,.0f}")
        self._whale_cd_ts = now

    async def _exit_whale_follow(self, now, px):
        """Exit on a FIXED dollar take-profit / stop-loss (the position's USD P&L), plus a
        max-hold backstop. pos.unrealized(px) is the live USD P&L on the position."""
        pos = self.pos
        if pos is None:
            return
        pnl = pos.unrealized(px)
        if pnl >= WHALE_TP_USDT:
            await self._close(f"whale_tp (+${pnl:.2f})", px); self._whale_cd_ts = now; return
        if pnl <= -WHALE_SL_USDT:
            await self._close(f"whale_sl (${pnl:.2f})", px); self._whale_cd_ts = now; return
        if (now - pos.opened_at) >= WHALE_MAX_HOLD:
            await self._close("whale_maxhold", px); self._whale_cd_ts = now; return

    # ---- "Whale Follow 100k LIVE (flow stop · limit)" — REAL money, INDEPENDENT, best-price limits ----
    # This is the EXACT strategy of the paper "Whale Follow 100k (flow stop)" bot, run for real money
    # with its OWN decisions (it does NOT mirror the paper bot): open the way any >= WHALE100_MIN_USD
    # print leans, hold while the 30s flow stays >= WHALE100_FLOW_EXIT on your side, exit the instant
    # it drops below. BOTH the entry and the exit are TOP-OF-BOOK LIMIT fills (capped at the best price
    # ± FLOWTRAIL_LIMIT_SLIP) — never a market order that walks the book — to kill the spread/slippage
    # that makes a paper edge vanish live.
    async def _whale100_live_entry(self, now):
        """When flat: open a REAL position the instant a single trade >= WHALE100_MIN_USD prints on the
        live cross-exchange tape (buy → long, sell → short), via a best-price LIMIT."""
        if self._busy or (now - self._whale_cd_ts) < WHALE100_COOLDOWN:
            return
        tape = self.whales.tape
        if not tape:
            return
        latest_seq = tape[-1]["seq"]
        if self._whale_last_seq is None:        # first run: ignore old prints, start fresh
            self._whale_last_seq = latest_seq
            return
        signal = None
        for t in reversed(tape):                # newest-first: the latest NEW qualifying print
            if t["seq"] <= self._whale_last_seq:
                break
            if t["usd"] >= WHALE100_MIN_USD:
                signal = t
                break
        self._whale_last_seq = latest_seq
        if signal is None:
            return
        px = self._btc()
        if not px:
            return
        side = "long" if signal["side"] == "buy" else "short"
        detail = (f"Whale100 LIVE · {signal['side'].upper()} ${signal['usd']:,.0f} on {signal['ex']} "
                  f"· exit when 30s on-side flow < {WHALE100_FLOW_EXIT*100:.0f}% · best-price limit")
        await self._open(side, px, 0.5, detail, f"Whale100 {signal['side']} ${signal['usd']:,.0f}",
                         slippage=FLOWTRAIL_LIMIT_SLIP)     # top-of-book LIMIT, not a market order
        self._whale_cd_ts = now

    async def _exit_whale100_live(self, now, px):
        """The 30-SECOND FLOW STOP (same as the paper bot): when the flow on our side over
        WHALE100_FLOW_WINDOW drops below WHALE100_FLOW_EXIT, close via a best-price LIMIT. A short
        grace after entry and a max-hold backstop sit underneath; liquidation is handled by Lighter."""
        pos = self.pos
        if pos is None:
            return
        held = now - pos.opened_at
        if held >= WHALE100_GRACE_SEC:
            buy, sell = self._flow(WHALE100_FLOW_WINDOW)
            tot = buy + sell
            if tot > 0:
                on_side = (buy / tot) if pos.side == "long" else (sell / tot)
                if on_side < WHALE100_FLOW_EXIT:
                    await self._close(f"flow_exit ({on_side*100:.0f}% on-side < "
                                      f"{WHALE100_FLOW_EXIT*100:.0f}% over {WHALE100_FLOW_WINDOW}s)",
                                      px, slippage=FLOWTRAIL_LIMIT_SLIP)
                    self._whale_cd_ts = now
                    return
        if held >= WHALE100_MAX_HOLD:
            await self._close("whale_maxhold", px, slippage=FLOWTRAIL_LIMIT_SLIP)
            self._whale_cd_ts = now
            return

    # ---- "Whale Copy 100k (mirror paper)" — REAL money MIRRORS the paper Whale-Follow-100k bot ----
    # No independent decisions: the real position is a pure shadow of the paper bot's whale
    # position. Paper opens → we open the same side; paper closes/flips → we close. So whatever
    # exit the paper bot uses (its 30s flow stop) drives ours too — we always do what it does.
    async def _whale100_entry(self, now):
        """When flat: open the SAME side the paper Whale-Follow-100k bot is holding — but ONLY at a
        price that is AT-OR-BETTER than the paper bot's entry. We make ONE IOC marketable-limit
        attempt per paper trade, with the limit price CAPPED at the paper's entry, so by exchange
        rules the real fill can only be the paper price or better:
            paper LONG  @ 60,000  →  we BUY  only at 60,000 or LOWER  (best ask ≤ 60,000)
            paper SHORT @ 60,000  →  we SELL only at 60,000 or HIGHER (best bid ≥ 60,000)
        If no such price is resting in the book the order does not fill, and we SKIP that paper
        trade entirely — no retry, no chasing, never pay up. (The toggle/limit_mode no longer
        gates this strategy: best-price-or-skip is ALWAYS how the whale-100k mirror enters. The
        EXIT stays an instant market mirror — see _exit_whale100.)"""
        if self._busy:
            return
        pb = self._paper_bot
        if pb is None or getattr(pb, "strategy", "") != "whale_follow_100k" or not pb.positions:
            self._whale_tried_pid = None
            return
        pid = list(pb.positions.keys())[0]
        ppos = pb.positions[pid]
        pside = ppos.side
        if pside not in ("long", "short"):
            return
        if pid == self._whale_tried_pid:
            return                          # already attempted this exact paper trade → skip (no retry)
        self._whale_tried_pid = pid         # mark BEFORE the attempt so it can never retry/chase
        await self._open_limit_mirror(pside, float(ppos.entry))

    async def _exit_whale100(self, now, px):
        """Close the instant the paper bot goes flat or flips to the other side — pure mirror."""
        pos = self.pos
        if pos is None:
            return
        pb = self._paper_bot
        paper_side = None
        if pb is not None and getattr(pb, "strategy", "") == "whale_follow_100k" and pb.positions:
            paper_side = list(pb.positions.values())[0].side
        if paper_side != pos.side:        # paper went flat / flipped / switched strategy → exit
            # The CLOSE is ALWAYS a market order — even in limit mode — so it fills immediately
            # when the paper exits. (Limit mode applies ONLY to the ENTRY now: a limit close just
            # got stuck retrying when the price moved away.)
            await self._close(f"mirror_exit (paper {paper_side or 'flat'})", px)
            if paper_side is None:
                self._whale_tried_pid = None

    # ---- "Freqtrade-style LIVE (mirror paper · limit)" — REAL money MIRRORS the paper Freq bot ----
    # Pure shadow of the paper Freqtrade-style bot (RSI + Bollinger + ROI/stop/trail): when it opens a
    # position we open the SAME side via a best-price IOC limit (fill at the paper's price-or-better,
    # else skip — no chase); when it closes (ROI / stop / trail / liquidation) we close too. So the
    # real bot does EXACTLY what the paper bot does, just with real Lighter fills.
    async def _freq_mirror_entry(self, now):
        """When flat: COPY the paper Freqtrade-style bot — open the SAME side it is holding. Close
        when it closes (see _exit_freq_mirror). Pure 1:1 copy; it just takes the trade."""
        if self._busy or self.pos is not None:
            return
        pb = self._freq_bot
        ppos = getattr(pb, "pos", None) if pb is not None else None
        if ppos is None:
            return
        pside = getattr(ppos, "side", None)
        if pside not in ("long", "short"):
            return
        pid = getattr(ppos, "id", None)
        if pid == self._freq_tried_id:
            return                          # one open per paper trade
        self._freq_tried_id = pid
        px = self._btc()
        if not px:
            return
        await self._open(pside, px, 0.5, f"Freq copy {pside}", f"Freq mirror {pside}")

    async def _exit_freq_mirror(self, now, px):
        """Close the instant the paper Freqtrade-style bot goes flat (its ROI/stop/trail fired)."""
        pos = self.pos
        if pos is None:
            return
        pb = self._freq_bot
        paper_side = None
        if pb is not None and getattr(pb, "pos", None) is not None:
            paper_side = pb.pos.side
        if paper_side != pos.side:          # paper closed / flipped → exit (market = guaranteed fill)
            await self._close(f"freq_mirror_exit (paper {paper_side or 'flat'})", px)

    async def _freq_improved_mirror_entry(self, now):
        """Copy the paper Freqtrade-style improved TP/SL bot."""
        if self._busy or self.pos is not None:
            return
        pb = self._freq_improved_bot
        ppos = getattr(pb, "pos", None) if pb is not None else None
        if ppos is None:
            return
        pside = getattr(ppos, "side", None)
        if pside not in ("long", "short"):
            return
        pid = getattr(ppos, "id", None)
        if pid == self._freq_improved_tried_id:
            return
        self._freq_improved_tried_id = pid
        px = self._btc()
        if not px:
            return
        await self._open(pside, px, 0.5, f"Freq improved copy {pside}", f"Freq improved mirror {pside}")

    async def _exit_freq_improved_mirror(self, now, px):
        """Close when the paper Freqtrade-style improved TP/SL bot goes flat or flips."""
        pos = self.pos
        if pos is None:
            return
        pb = self._freq_improved_bot
        paper_side = None
        if pb is not None and getattr(pb, "pos", None) is not None:
            paper_side = pb.pos.side
        if paper_side != pos.side:
            await self._close(f"freq_improved_mirror_exit (paper {paper_side or 'flat'})", px)

    async def _freq_trend_mirror_entry(self, now):
        """Copy the paper Freqtrade-style trend TP/SL bot."""
        if self._busy or self.pos is not None:
            return
        pb = self._freq_trend_bot
        ppos = getattr(pb, "pos", None) if pb is not None else None
        if ppos is None:
            return
        pside = getattr(ppos, "side", None)
        if pside not in ("long", "short"):
            return
        pid = getattr(ppos, "id", None)
        if pid == self._freq_trend_tried_id:
            return
        self._freq_trend_tried_id = pid
        px = self._btc()
        if not px:
            return
        await self._open(pside, px, 0.5, f"Freq trend copy {pside}", f"Freq trend mirror {pside}")

    async def _exit_freq_trend_mirror(self, now, px):
        """Close when the paper Freqtrade-style trend TP/SL bot goes flat or flips."""
        pos = self.pos
        if pos is None:
            return
        pb = self._freq_trend_bot
        paper_side = None
        if pb is not None and getattr(pb, "pos", None) is not None:
            paper_side = pb.pos.side
        if paper_side != pos.side:
            await self._close(f"freq_trend_mirror_exit (paper {paper_side or 'flat'})", px)

    async def _freq_5_mirror_entry(self, now):
        """Copy the paper Freqtrade-style improved TP/SL 5% bot: when IT opens a position and
        we're flat, open the same side (one try per paper position — no chasing)."""
        if self._busy or self.pos is not None:
            return
        pb = self._freq_5_bot
        ppos = getattr(pb, "pos", None) if pb is not None else None
        if ppos is None:
            return
        pside = getattr(ppos, "side", None)
        if pside not in ("long", "short"):
            return
        pid = getattr(ppos, "id", None)
        if pid == self._freq_5_tried_id:
            return
        self._freq_5_tried_id = pid
        px = self._btc()
        if not px:
            return
        await self._open(pside, px, 0.5, f"Freq 5% copy {pside}", f"Freq 5% mirror {pside}")

    async def _exit_freq_5_mirror(self, now, px):
        """Close when the paper Freqtrade-style improved TP/SL 5% bot goes flat or flips."""
        pos = self.pos
        if pos is None:
            return
        pb = self._freq_5_bot
        paper_side = None
        if pb is not None and getattr(pb, "pos", None) is not None:
            paper_side = pb.pos.side
        if paper_side != pos.side:
            await self._close(f"freq_5_mirror_exit (paper {paper_side or 'flat'})", px)

    async def _rsi2_mirror_entry(self, now):
        """Copy the RSI2 EMA50 ATR-filter PAPER bot (dashboard.RSI2ATR): the instant IT
        opens a position and we're flat, open the SAME side once (one try per paper
        position — no chasing). Sizing/leverage use this real bot's own settings."""
        if self._busy or self.pos is not None:
            return
        pb = self._rsi2atr_bot
        ppos = getattr(pb, "pos", None) if pb is not None else None
        if not isinstance(ppos, dict):
            self._rsi2_tried_id = None      # paper flat -> ready to copy the next open
            return
        pside = ppos.get("side")
        if pside not in ("long", "short"):
            return
        pid = ppos.get("id") or f"{ppos.get('opened_at')}:{pside}"
        if pid == self._rsi2_tried_id:
            return
        self._rsi2_tried_id = pid
        px = self._btc()
        if not px:
            return
        await self._open(pside, px, 0.5, f"RSI2 copy {pside}", f"RSI2 ATR mirror {pside}")

    async def _exit_rsi2_mirror(self, now, px):
        """Close the instant the RSI2 ATR paper bot goes flat or flips side."""
        pos = self.pos
        if pos is None:
            return
        pb = self._rsi2atr_bot
        ppos = getattr(pb, "pos", None) if pb is not None else None
        paper_side = ppos.get("side") if isinstance(ppos, dict) else None
        if paper_side != pos.side:
            await self._close(f"rsi2_mirror_exit (paper {paper_side or 'flat'})", px)
            if paper_side is None:
                self._rsi2_tried_id = None

    # ---- "News+Whale Trail" — REAL money: news-triggered whale-follow, % trailing-stop exit ----
    def _tv_bb_rsi_paper_pos(self):
        pb = self._tvstrats_bot
        acct = getattr(pb, "acct", None) if pb is not None else None
        if not isinstance(acct, dict):
            return None
        a = acct.get("bb_rsi") or {}
        p = a.get("pos")
        return p if isinstance(p, dict) else None

    async def _tv_bb_rsi_mirror_entry(self, now):
        """Copy the paper TradingView 'Bollinger + RSI Double (ChartArt)' strategy."""
        if self._busy or self.pos is not None:
            return
        ppos = self._tv_bb_rsi_paper_pos()
        if ppos is None:
            self._tv_bb_rsi_tried_id = None
            return
        pside = ppos.get("side")
        if pside not in ("long", "short"):
            return
        pentry = float(ppos.get("entry") or 0.0)
        if pentry <= 0:
            return
        pid = f"{ppos.get('opened_at')}:{pside}:{round(pentry, 2)}"
        if pid == self._tv_bb_rsi_tried_id:
            return
        self._tv_bb_rsi_tried_id = pid
        await self._open_limit_mirror(pside, pentry)

    async def _exit_tv_bb_rsi_mirror(self, now, px):
        """Close when the paper ChartArt BB+RSI strategy goes flat or flips."""
        pos = self.pos
        if pos is None:
            return
        ppos = self._tv_bb_rsi_paper_pos()
        paper_side = ppos.get("side") if ppos else None
        if paper_side != pos.side:
            await self._close(f"bb_rsi_mirror_exit (paper {paper_side or 'flat'})", px)
            if paper_side is None:
                self._tv_bb_rsi_tried_id = None

    async def _watch_newswhale(self, headline, t0, start_px):
        """On a NEWS drop, watch the live tape (up to NWT_GIVEUP_SECS) for the FIRST trade of
        >= NWT_WHALE_MIN_USD ($200k). That whale picks the side — they bought -> long, sold ->
        short — and we follow it. No flow. The exit is the percentage trailing stop in
        _exit_newswhale. No $200k+ print in time -> skip the headline."""
        # Only follow whales that print AFTER the news — baseline on the newest tape seq right now.
        base_seq = self.whales.tape[-1]["seq"] if self.whales.tape else -1
        while (time.time() - t0 < NWT_GIVEUP_SECS and self.enabled and self.pos is None
               and not self._busy):
            await asyncio.sleep(NWT_CHECK_SECS)
            signal = None
            for t in reversed(self.whales.tape):          # newest first
                if t["seq"] <= base_seq:                  # only prints since the news
                    break
                if t["usd"] >= NWT_WHALE_MIN_USD:         # first $200k+ whale wins
                    signal = t
                    break
            if signal is None:
                continue
            side = "long" if signal["side"] == "buy" else "short"
            px = self._btc()
            if not px:
                continue
            detail = (f"News+Whale · follow ${signal['usd']/1000:.0f}k {signal['side'].upper()} "
                      f"on {signal.get('ex','?')} · trail {NWT_TRAIL_PCT*100:.2f}% · limit")
            await self._open(side, px, 0.0, detail, headline,
                             slippage=FLOWTRAIL_LIMIT_SLIP)         # top-of-book limit
            return
        if self.pos is None:
            self._note(f"no ≥${NWT_WHALE_MIN_USD/1000:.0f}k whale print within "
                       f"{NWT_GIVEUP_SECS}s → skip · {headline[:50]}", "skip")

    async def _exit_newswhale(self, now, px):
        """PERCENTAGE TRAILING STOP. The stop sits NWT_TRAIL_PCT behind the BEST price reached and
        only ratchets in our favour — as price runs our way the stop FOLLOWS it and locks the gain.
        The instant price retraces NWT_TRAIL_PCT from the best, CLOSE. It never loosens, so a winner
        can't round-trip into a loss; a loser is cut at ~the trail distance."""
        pos = self.pos
        if pos is None:
            return
        if pos.peak <= 0:
            pos.peak = pos.entry
        if pos.side == "long":
            pos.peak = max(pos.peak, px)                            # highest price so far
            stop = pos.peak * (1 - NWT_TRAIL_PCT)                   # stop trails up under it
            if px <= stop:
                await self._close(f"trail_stop @ ${px:,.1f} ({NWT_TRAIL_PCT*100:.2f}% off peak "
                                  f"${pos.peak:,.1f})", px, slippage=FLOWTRAIL_LIMIT_SLIP)
        else:
            pos.peak = min(pos.peak, px)                            # lowest price so far
            stop = pos.peak * (1 + NWT_TRAIL_PCT)                   # stop trails down over it
            if px >= stop:
                await self._close(f"trail_stop @ ${px:,.1f} ({NWT_TRAIL_PCT*100:.2f}% off low "
                                  f"${pos.peak:,.1f})", px, slippage=FLOWTRAIL_LIMIT_SLIP)

    # ---- "Flow Trail" — REAL money: enter on >=60% flow, exit on a breakeven-floored trail ----
    async def _flowtrail_entry(self, now):
        """When flat: open the way the taker flow leans over FLOWTRAIL_WINDOW. >= FLOWTRAIL_ENTER%
        buy -> long; >= that% sell -> short. No entry during the post-close cooldown."""
        if self._busy or (now - self._ft_cd_ts) < FLOWTRAIL_COOLDOWN:
            return
        buy, sell = self._flow(FLOWTRAIL_WINDOW)
        tot = buy + sell
        if tot <= 0:
            return
        buy_pct = buy / tot * 100.0
        if buy_pct >= FLOWTRAIL_ENTER:
            side = "long"
        elif (100.0 - buy_pct) >= FLOWTRAIL_ENTER:
            side = "short"
        else:
            return
        px = self._btc()
        if not px:
            return
        detail = (f"Flow Trail · {side.upper()} on flow {buy_pct:.0f}% buy · "
                  f"trail ${FLOWTRAIL_TRAIL_USD:.0f} · limit")
        await self._open(side, px, round(buy_pct, 1), detail, f"FlowTrail {side}",
                         slippage=FLOWTRAIL_LIMIT_SLIP)   # top-of-book limit, not a market order

    async def _exit_flowtrail(self, now, px):
        """Pure TRAILING STOP. The stop sits FLOWTRAIL_TRAIL_USD behind the BEST price reached and
        only ratchets in your favour; the moment price falls back to it, CLOSE — even if that's a
        small loss. (It does NOT hold a losing position.) A long entered at E starts with the stop
        at E - trail; if price drops to it, close at ~-trail; if price rises, the stop trails up and
        locks the gain."""
        pos = self.pos
        if pos is None:
            return
        if pos.peak <= 0:
            pos.peak = pos.entry
        if pos.side == "long":
            pos.peak = max(pos.peak, px)                       # best (highest) price so far
            if px <= pos.peak - FLOWTRAIL_TRAIL_USD:           # fell back to the trailing stop
                await self._close(f"trail_stop @ ${px:,.1f} (peak ${pos.peak:,.1f})", px,
                                  slippage=FLOWTRAIL_LIMIT_SLIP)
                self._ft_cd_ts = now
        else:
            pos.peak = min(pos.peak, px)                       # best (lowest) price so far
            if px >= pos.peak + FLOWTRAIL_TRAIL_USD:
                await self._close(f"trail_stop @ ${px:,.1f} (low ${pos.peak:,.1f})", px,
                                  slippage=FLOWTRAIL_LIMIT_SLIP)
                self._ft_cd_ts = now

    # ---- LIMIT-MODE mirror (toggle): IOC limit orders at the paper bot's EXACT prices ----
    async def _open_limit_mirror(self, side, limit_price):
        """Mirror the paper trade at the BEST possible price that is at-or-better than the paper's
        entry. We send ONE IOC marketable-limit order with the limit CAPPED at `limit_price` (the
        paper bot's entry). Exchange limit rules then guarantee the fill is the paper price or
        better, and never worse:
            long  (BUY)  → fills against resting asks ≤ limit_price  → entry ≤ paper price
            short (SELL) → fills against resting bids ≥ limit_price  → entry ≥ paper price
        Whatever the best resting price is, that's what we get (price improvement is kept). If no
        price at-or-better than the paper's entry is resting, the IOC cancels with no fill and we
        SKIP this paper trade — that's the "did not find a best price → skip" case. We read the
        REAL position back to learn the actual fill before we hold anything."""
        if self._busy or self.pos is not None or not limit_price:
            return
        lev = float(self.leverage)
        self._busy = True
        try:
            if not self._live.get("balance"):
                await self._refresh_live()
            free = self._free_balance()
            margin = round(free * self.risk_frac * LIVE_MARGIN_BUFFER, 2)
            if margin < LIVE_MIN_MARGIN:
                self._note(f"limit-mirror {side.upper()} but free ${free:.2f} too low → skip", "skip")
                return
            order_side = "buy" if side == "long" else "sell"
            better = "≤" if side == "long" else "≥"
            async with self._lock:
                # ioc=True, limit=paper entry → "fill at this price-or-better, else cancel (no rest)"
                res = await _to_thread(self.lighter.order, order_side, None, margin, lev,
                                       "limit", limit_price, False, False, "BTC", True)
            if not res.get("ok"):
                self._note(f"WOULD LIMIT {side.upper()} @ ${limit_price:,.1f} — not placed: "
                           f"{res.get('error')}", "skip")
                return
            # IOC may fill fully, partially, or not at all — the only truth is the real position.
            await self._refresh_live()
            lp = self._live_position()
            if lp and (lp.get("size") or 0) and lp.get("side") == side:
                entry = float(lp.get("entry") or limit_price)
                self.pos = LivePos(side=side, entry=entry, qty=abs(float(lp["size"])), leverage=lev,
                                   opened_at=time.time(), news=f"Mirror-limit {side}", peak=entry,
                                   bal_open=self._total_balance())
                self._flat_confirm = 0
                improve = (limit_price - entry) if side == "long" else (entry - limit_price)
                self._note(f"LIMIT {side.upper()} FILLED @ ${entry:,.1f} "
                           f"({better} paper ${limit_price:,.1f}, "
                           f"{'+' if improve >= 0 else ''}${improve:,.1f} better)", "open")
                self._save()
            else:
                # No resting price at-or-better than the paper's entry → no fill → skip (no chase).
                self._note(f"no price {better} paper ${limit_price:,.1f} for {side.upper()} "
                           f"→ SKIP this trade (won't pay up / chase)", "skip")
        except Exception as e:
            self._note(f"limit-open error: {type(e).__name__}: {str(e)[:120]}", "error")
        finally:
            self._busy = False

    def set_limit_mode(self, on):
        self._limit_mode = bool(on)
        self._note(f"LIMIT-ORDER mirror {'ON' if self._limit_mode else 'OFF'} "
                   f"({'IOC limit ENTRY at paper price · market close' if self._limit_mode else 'market orders'})")
        self._save()
        return {"ok": True, "limit_mode": self._limit_mode}

    # ---- "Maker mean-reversion" — CONTINUOUS post-only limit market-making ----
    # ---- "Fair-Value Tracking LIVE" - Lighter deviation vs leader consensus ----
    def _fv_median(self, vals):
        vals = sorted([float(v) for v in vals if v])
        n = len(vals)
        if not n:
            return None
        if n % 2:
            return vals[n // 2]
        return (vals[n // 2 - 1] + vals[n // 2]) / 2.0

    def _fv_live_prices(self):
        out = {}
        if self.whales is not None:
            now_ms = time.time() * 1000.0
            wanted = set(FV_LEADERS) | {"lighter"}
            scanned = 0
            for t in reversed(self.whales.tape):
                ex = t.get("ex")
                if ex in wanted and ex not in out:
                    if now_ms - float(t.get("ts") or 0.0) <= FV_STALE_MS:
                        out[ex] = t.get("price")
                    else:
                        out[ex] = None
                if all(e in out for e in wanted):
                    break
                scanned += 1
                if scanned > 30000:
                    break
            out = {ex: float(p) for ex, p in out.items() if p}
        if "binance" not in out:
            px = self._btc()
            if px:
                out["binance"] = float(px)
        return out

    def _fv_quote(self):
        prices = self._fv_live_prices()
        leaders = [prices[ex] for ex in FV_LEADERS if ex in prices]
        fv = self._fv_median(leaders)
        lt = prices.get("lighter")
        n_leaders = len(leaders)
        self._fv_fv = fv
        self._fv_lt = lt
        self._fv_n_leaders = n_leaders
        if fv is None or lt is None or lt <= 0 or n_leaders < FV_MIN_LEADERS:
            self._fv_dev_bps = None
            miss = "Lighter" if lt is None else f"{n_leaders} leader(s)"
            self._fv_status = f"waiting for fresh FV feed ({miss})"
            return None, None, None, n_leaders
        dev = (fv - lt) / lt * 1e4
        self._fv_dev_bps = dev
        self._fv_status = f"FV {fv:,.1f} vs Lighter {lt:,.1f} ({dev:+.1f} bps)"
        return fv, lt, dev, n_leaders

    async def _fv_live_entry(self, now):
        if self._busy or self.pos is not None:
            return
        if now - self._fv_attempt_ts < FV_LIVE_ATTEMPT_COOLDOWN:
            return
        fv, lt, dev, _ = self._fv_quote()
        if fv is None or lt is None or dev is None:
            return
        if abs(dev) < FV_ENTER_BPS:
            return
        side = "long" if dev > 0 else "short"
        self._fv_attempt_ts = now
        await self._open_fv_limit(side, lt, fv, dev)

    async def _open_fv_limit(self, side, limit_price, fv, dev):
        """Open with one IOC limit at the paper-quality Lighter price. If it cannot
        fill there-or-better, skip instead of chasing the disappearing edge."""
        if self._busy or self.pos is not None or not limit_price:
            return
        lev = float(self.leverage)
        self._busy = True
        try:
            await self._refresh_live()
            existing = self._live_position()
            if existing is not None and (existing.get("size") or 0):
                if self.pos is None and self._should_adopt(time.time()):
                    self._adopt_live_position(existing)
                return
            free = self._free_balance()
            margin = round(free * self.risk_frac * LIVE_MARGIN_BUFFER, 2)
            if margin < LIVE_MIN_MARGIN:
                self._note(f"FV live {side.upper()} but free ${free:.2f} too low -> skip", "skip")
                return
            order_side = "buy" if side == "long" else "sell"
            better = "<=" if side == "long" else ">="
            async with self._lock:
                res = await _to_thread(self.lighter.order_best_ioc, order_side, None, margin, lev,
                                       round(limit_price, 2), "BTC",
                                       FV_LIVE_IOC_ATTEMPTS, FV_LIVE_IOC_DELAY_MS)
            if not res.get("ok"):
                self._note(f"WOULD FV BEST-IOC {side.upper()} cap ${limit_price:,.1f} - "
                           f"not placed: {res.get('error')}", "skip")
                return
            await self._refresh_live()
            lp = self._live_position()
            if lp and (lp.get("size") or 0) and lp.get("side") == side:
                entry = float(lp.get("entry") or limit_price)
                qty = abs(float(lp.get("size") or 0.0))
                self.pos = LivePos(side=side, entry=entry, qty=qty, leverage=lev,
                                   opened_at=time.time(),
                                   news=(f"Fair-Value LIVE: FV {fv:,.1f}, Lighter {limit_price:,.1f}, "
                                         f"dev {dev:+.1f}bps"),
                                   peak=entry, bal_open=self._total_balance(),
                                   entry_fv=float(fv), entry_dev=float(dev))
                self._flat_confirm = 0
                improve = (limit_price - entry) if side == "long" else (entry - limit_price)
                self._note(f"FV LIMIT {side.upper()} FILLED @ ${entry:,.1f} "
                           f"({better} signal ${limit_price:,.1f}, {improve:+.1f} better, "
                           f"best-IOC attempt {res.get('attempts','?')}) - dev {dev:+.1f}bps, {lev:g}x", "open")
                self._save()
            else:
                self._note(f"FV best-IOC sent but no {side.upper()} position appeared "
                           f"({better} signal ${limit_price:,.1f}) -> skipped/no fill", "skip")
        except Exception as e:
            self._note(f"FV live open error: {type(e).__name__}: {str(e)[:120]}", "error")
        finally:
            self._busy = False

    async def _exit_fv_live(self, now, px):
        pos = self.pos
        if pos is None:
            return
        fv, lt, dev, _ = self._fv_quote()
        held = now - pos.opened_at
        if dev is None or lt is None:
            if held >= FV_MAX_HOLD_SEC:
                await self._close("fv_time_stop (feed stale)", px, slippage=FV_LIVE_CLOSE_SLIP)
            return
        entry_dev = float(pos.entry_dev or dev)
        reason = None
        if abs(dev) <= FV_EXIT_BPS:
            reason = "fv_converged"
        elif abs(dev) >= abs(entry_dev) + FV_STOP_WIDEN_BPS:
            reason = "fv_diverged_stop"
        elif held >= FV_MAX_HOLD_SEC:
            reason = "fv_time_stop"
        if reason:
            await self._close(f"{reason} (dev {entry_dev:+.1f}->{dev:+.1f}bps)",
                              lt, slippage=FV_LIVE_CLOSE_SLIP)

    # State machine (single position, like the rest of the bot):
    #   idle  --place post-only BUY below fair value-->  bid_open
    #   bid_open  --our BUY fills (a position appears)-->  long  (rest a post-only SELL exit)
    #   long  --SELL exit fills / stop / max-hold-->  flat  --> idle
    # We EARN the half-spread by always adding liquidity; the hard MARKET stop caps the only
    # real risk (adverse selection). Maker fills can't be backtested from candles — paper first.
    def _maker_mean(self, now, window, need):
        cut = now - window
        vals = [p for (t, p) in self._mid_hist if t >= cut]
        return (sum(vals) / len(vals)) if len(vals) >= need else None

    def _maker_reset(self):
        self._maker_phase = "idle"; self._maker_bid_px = 0.0
        self._maker_bid_ts = 0.0; self._maker_ask_px = 0.0

    async def _maker_cancel_all(self, why=""):
        try:
            async with self._lock:
                res = await _to_thread(self.lighter.cancel_all)
            if not res.get("ok") and "no orders" not in str(res.get("error", "")).lower():
                self._note(f"maker cancel_all ({why}) → {res.get('error')}", "skip")
        except Exception as e:
            self._note(f"maker cancel error ({why}): {type(e).__name__}: {str(e)[:80]}", "error")

    async def _maker_flat(self, now):
        """FLAT: keep a single post-only BUY resting BAND bps below fair value while the regime
        is OK; promote to a long the moment it fills; reprice/cancel as fair value moves."""
        if self._maker_busy:
            return
        # one-time startup sync: clear any stale orders left on the book from a prior run
        if not getattr(self, "_maker_synced", False):
            await self._maker_cancel_all("startup"); self._maker_synced = True; self._maker_reset()

        # FILL DETECTION: a live position appeared while our BUY was resting → we're long now
        if self._maker_phase == "bid_open" and self._live_position() is not None:
            entry = self._maker_bid_px or (self._btc() or 0.0)
            lp = self._live_position() or {}
            qty = abs(float(lp.get("size") or lp.get("contracts") or 0.0)) or 0.0
            self.pos = LivePos(side="long", entry=entry, qty=qty, leverage=MAKER_LEVERAGE,
                               opened_at=now, news="Maker mean-reversion", peak=entry,
                               bal_open=self._total_balance(),
                               sl=round(entry * (1 - MAKER_STOP_BPS / 1e4), 2),
                               tp=round(entry * (1 + MAKER_TP_BPS / 1e4), 2))
            self._note(f"maker FILLED long @ ${entry:,.2f} (post-only) · resting exit next", "open")
            self._maker_phase = "long"; self._maker_bid_px = 0.0; self._maker_ask_px = 0.0
            self._flat_confirm = 0; self._save()
            return

        # if we hold leftover phase but are flat with no resting bid → clean slate
        if self._maker_phase not in ("idle", "bid_open"):
            await self._maker_cancel_all("reset"); self._maker_reset()

        # only QUOTE when trading is enabled; if disabled, pull any resting bid
        if not self.enabled:
            if self._maker_phase == "bid_open":
                await self._maker_cancel_all("disabled"); self._maker_reset()
            return

        fv = self._maker_mean(now, MAKER_FV_WINDOW, 5)
        tr = self._maker_mean(now, MAKER_TREND_WINDOW, 30)
        mid = self._btc()
        if not fv or not tr or not mid:
            return
        # REGIME FILTER: don't catch a falling knife (mid far below the slow trend mean)
        if (mid - tr) / tr < MAKER_MIN_TREND:
            if self._maker_phase == "bid_open":
                await self._maker_cancel_all("regime"); self._maker_reset()
            return
        target = fv * (1 - MAKER_BAND_BPS / 1e4)
        target = min(target, mid * (1 - 1 / 1e4))         # stay strictly passive (never cross)
        if self._maker_phase == "idle":
            await self._maker_place_bid(now, target)
        elif self._maker_phase == "bid_open":
            drifted = abs(target - self._maker_bid_px) / mid * 1e4 >= MAKER_REQUOTE_BPS
            if (now - self._maker_bid_ts) >= MAKER_ORDER_TTL or drifted:
                await self._maker_cancel_all("requote"); self._maker_reset()
                await self._maker_place_bid(now, target)

    async def _maker_place_bid(self, now, price):
        self._maker_busy = True
        try:
            if not (self._live.get("balance")):
                await self._refresh_live()
            free = self._free_balance()
            margin = round(free * self.risk_frac * LIVE_MARGIN_BUFFER * MAKER_SIZE_MULT, 2)
            if margin < LIVE_MIN_MARGIN:
                self._note(f"maker: free ${free:.2f} too low to quote", "skip"); return
            async with self._lock:
                res = await _to_thread(self.lighter.order, "buy", None, margin, MAKER_LEVERAGE,
                                       "limit", round(price, 2), False, True)   # post_only=True
            if not res.get("ok"):
                self._note(f"maker WOULD BID @ ${price:,.2f} — not placed: {res.get('error')}", "skip")
                return
            self._maker_bid_px = price; self._maker_bid_ts = now; self._maker_phase = "bid_open"
            self._note(f"maker BID (post-only) @ ${price:,.2f} · margin ${margin:.2f} "
                       f"{MAKER_LEVERAGE}x", "open")
        except Exception as e:
            self._note(f"maker bid error: {type(e).__name__}: {str(e)[:90]}", "error")
        finally:
            self._maker_busy = False

    async def _maker_place_exit(self, now):
        pos = self.pos
        if pos is None:
            return
        self._maker_busy = True
        try:
            target = pos.entry * (1 + MAKER_TP_BPS / 1e4)
            usd = round(pos.qty * target, 2)                  # size the SELL to the whole position
            await self._maker_cancel_all("pre-exit")          # clear any stale ask first
            async with self._lock:
                res = await _to_thread(self.lighter.order, "sell", usd, None, None,
                                       "limit", round(target, 2), True, True)  # reduceOnly + post_only
            if not res.get("ok"):
                self._note(f"maker WOULD ASK @ ${target:,.2f} — not placed: {res.get('error')}", "skip")
                return
            self._maker_ask_px = target
            self._note(f"maker EXIT ask (post-only, reduceOnly) @ ${target:,.2f} "
                       f"(+{MAKER_TP_BPS}bps)", "open")
        except Exception as e:
            self._note(f"maker exit-place error: {type(e).__name__}: {str(e)[:90]}", "error")
        finally:
            self._maker_busy = False

    async def _exit_maker(self, now, px):
        """LONG: a post-only SELL rests at +TP bps (banks the reversion as maker). Underneath,
        a hard MARKET stop caps adverse selection, and a max-hold frees stuck capital. When the
        post-only exit fills, Lighter goes flat and the reconcile logs the realized P&L."""
        pos = self.pos
        if pos is None:
            return
        self._maker_phase = "long"
        # 1) HARD STOP — price ran against us; market out (taker) to cap the loss
        if px <= pos.entry * (1 - MAKER_STOP_BPS / 1e4):
            await self._maker_cancel_all("stop")
            await self._close(f"maker_stop (-{MAKER_STOP_BPS}bps)", px)
            self._maker_reset(); return
        # 2) MAX-HOLD — the post-only exit never filled; market out to recycle capital
        if (now - pos.opened_at) >= MAKER_MAX_HOLD:
            await self._maker_cancel_all("maxhold")
            await self._close("maker_maxhold (exit unfilled)", px)
            self._maker_reset(); return
        # 3) make sure the post-only exit ask is resting (place once)
        if self._maker_ask_px == 0 and not self._maker_busy and self.enabled:
            await self._maker_place_exit(now)

    async def _exit_claudenews(self, now, px):
        """'claude news haiku' exit: run the AI's OWN plan to completion instead of
        babysitting it to a scratch. Order:
          0) flash-crash seatbelt (anti-liquidation, instant);
          1) once the AI's take-profit is reached, RIDE the runner on a tight trail
             (banks >= TP but lets a strong move keep paying);
          2) before TP: lock breakeven once halfway there (a winner can't go red), then
             the AI's hard stop;
          3) no-progress cut (news edge is fast — if it's not green by CLAUDE_NOPROG_SEC,
             it didn't react) and a short max-hold backstop."""
        pos = self.pos
        if pos is None:
            return
        if pos.peak <= 0:
            pos.peak = pos.entry
        prof = self._profit_pct(pos, px)          # signed, + in our favour
        held = now - pos.opened_at

        # 0) CATASTROPHIC flash-crash seatbelt (instant, anti-liquidation only)
        if GPTNEWS_BACKSTOP_PCT and prof <= -GPTNEWS_BACKSTOP_PCT:
            await self._close(f"flash_stop (-{GPTNEWS_BACKSTOP_PCT*100:.2f}%)", px); return

        # arm the runner-trail the moment take-profit is reached
        if pos.tp and not pos.tp_armed:
            if (pos.side == "long" and px >= pos.tp) or (pos.side == "short" and px <= pos.tp):
                pos.tp_armed = True
                pos.peak = px                      # measure the trail from the TP level

        if pos.tp_armed:
            # 1) RIDE: trail the best price; bank when the move gives back CLAUDE_RUN_TRAIL
            if pos.side == "long":
                pos.peak = max(pos.peak, px)
                if px <= pos.peak * (1 - CLAUDE_RUN_TRAIL):
                    await self._close(f"take_profit (rode to {prof*100:+.2f}%)", px); return
            else:
                pos.peak = min(pos.peak, px)
                if px >= pos.peak * (1 + CLAUDE_RUN_TRAIL):
                    await self._close(f"take_profit (rode to {prof*100:+.2f}%)", px); return
        else:
            # 2) BREAKEVEN LOCK once profit reaches a fraction of the TP distance
            if pos.tp:
                tp_dist = abs(pos.tp - pos.entry) / pos.entry
                if (not pos.be_locked) and tp_dist > 0 and prof >= CLAUDE_LOCK_ARM_FRAC * tp_dist:
                    pos.be_locked = True
            if pos.be_locked and (
                    (pos.side == "long" and px <= pos.entry) or
                    (pos.side == "short" and px >= pos.entry)):
                await self._close("breakeven_lock (winner protected)", px); return
            # 2b) the AI's HARD STOP
            if pos.sl and (
                    (pos.side == "long" and px <= pos.sl) or
                    (pos.side == "short" and px >= pos.sl)):
                await self._close(f"stop_loss (-{abs(pos.entry-pos.sl)/pos.entry*100:.2f}%)", px); return
            # 3) NO-PROGRESS CUT — the catalyst didn't move our way fast enough
            if held >= CLAUDE_NOPROG_SEC and prof <= 0:
                await self._close(f"no_progress ({held:.0f}s, not green)", px); return

        # 4) MAX-HOLD backstop (news edge decays in minutes)
        if held >= CLAUDE_MAX_HOLD_SEC:
            await self._close("time_stop", px); return

    async def _exit_gptnews(self, now, px):
        """'GPTnews' exit: the AI watches the open trade live and decides hold/close.
        The AI call runs in the BACKGROUND so it never blocks this 1s loop. Underneath
        sit instant, AI-free safeties: a catastrophic flash-crash seatbelt, a max-hold,
        and the reconcile (which still catches a real liquidation)."""
        pos = self.pos
        if pos is None:
            return
        # 1) the AI's own decision (set by the background manager) — close now
        if self._gpt_close_reason:
            reason = self._gpt_close_reason
            self._gpt_close_reason = None
            await self._close(reason, px); return
        # 2) catastrophic flash-crash seatbelt (instant, no AI) — anti-liquidation only
        if GPTNEWS_BACKSTOP_PCT and self._profit_pct(pos, px) <= -GPTNEWS_BACKSTOP_PCT:
            await self._close(f"flash_stop (-{GPTNEWS_BACKSTOP_PCT*100:.2f}%)", px); return
        # 3) max-hold backstop
        if (now - pos.opened_at) >= config.NW_MAX_HOLD_MIN * 60:
            await self._close("time_stop", px); return
        # 4) ask the AI (throttled, non-blocking) whether to hold or close
        if not self._gpt_manage_busy and (now - self._gpt_manage_ts) >= GPTNEWS_MANAGE_SECS:
            self._gpt_manage_ts = now
            asyncio.create_task(self._gpt_manage_check(self._gpt_manage_ctx(pos, px, now)))

    def _gpt_manage_ctx(self, pos, px, now):
        prof = self._profit_pct(pos, px)
        then10 = self._price_ago(10)
        m10 = round((px - then10) / then10 * 100, 3) if (px and then10) else None
        try:
            buy, sell = self._flow(60); tot = buy + sell
            flow = round(buy / tot * 100, 1) if tot > 0 else None
        except Exception:
            flow = None
        return {"side": pos.side, "news": pos.news, "entry": round(pos.entry, 2),
                "price": round(px, 2), "pnl_pct": round(prof * 100, 3),
                "held_s": int(now - pos.opened_at), "move_10s": m10, "flow_buy_pct": flow}

    async def _gpt_manage_check(self, ctx):
        """Background: ask the AI to manage the open trade; if it says CLOSE, flag it
        so the next manage tick closes. Failures default to HOLD (the backstops cover us)."""
        self._gpt_manage_busy = True
        try:
            if self.strategy == "claudenews":     # AI exit also runs on Claude for this strategy
                import claude_news as ai
            else:
                import gpt_news as ai
            d = await ai.manage(ctx)
            if (not d.error) and d.action == "close" and self.pos is not None:
                self._gpt_close_reason = f"ai_exit ({d.reason[:50]})"
        except Exception:
            pass
        finally:
            self._gpt_manage_busy = False

    # ---- controls ----
    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note(f"bot {'ENABLED — will place REAL orders on news' if self.enabled else 'PAUSED — no new trades'}")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def set_leverage(self, lev):
        try:
            lev = float(lev)
        except Exception:
            return {"ok": False, "error": "leverage must be a number"}
        mx = (self._live or {}).get("max_leverage") or 50
        self.leverage = max(1.0, min(lev, float(mx)))
        self._note(f"leverage set to {self.leverage:g}x")
        self._save()
        return {"ok": True, "leverage": self.leverage}

    def set_strategy(self, name):
        name = (name or "").strip()
        if name not in STRATEGIES:
            return {"ok": False, "error": f"unknown strategy '{name}'"}
        if name == self.strategy:
            return {"ok": True, "strategy": self.strategy, "strategy_label": STRATEGIES[name]}
        if self.pos is not None:
            # exits are strategy-specific; switching mid-trade would manage the open
            # position with the new rules. Make the user flatten first.
            return {"ok": False, "error": "close the open position before switching strategy"}
        self.strategy = name
        self._note(f"strategy set to {STRATEGIES[name]}")
        self._save()
        return {"ok": True, "strategy": self.strategy, "strategy_label": STRATEGIES[name]}

    async def close_now(self):
        """Manual kill switch from the page: flatten whatever is open right now."""
        if self.pos is not None:
            await self._close("manual_close")
            return {"ok": True}
        # no tracked position, but try to flatten anything on the account anyway
        async with self._lock:
            res = await _to_thread(self.lighter.close)
        self._note("manual close: " + ("done" if res.get("ok") else str(res.get("error", ""))[:120]),
                   "info" if res.get("ok") else "error")
        return res

    def reset(self):
        self.history = []
        self.log = []
        self._note("bot log/history reset (your real Lighter balance is untouched)")
        self._save()
        return {"ok": True}

    def state(self, flow_window_sec=None):
        px = self._btc()
        live = self._live or {}
        win = int(flow_window_sec) if flow_window_sec else LIVE_FLOW_WINDOW
        if self.strategy == "scalper":      # show the EXACT window the scalper decides on
            win = SCALP_FLOW_WINDOW
        if self.strategy == "fv_live":
            try:
                self._fv_quote()
            except Exception:
                pass
        try:
            buy, sell = self._flow(win)               # Lighter's OWN flow (matches the screen)
            tot = buy + sell
            flow_pct = round(buy / tot * 100, 1) if tot > 0 else None
        except Exception:
            flow_pct = None
        bal = live.get("balance") or {}
        pos = None
        if self.pos is not None:
            # Prefer Lighter's REAL position numbers (entry + unrealized P&L) so the panel
            # matches your account exactly; fall back to our own estimate if not available.
            lp = self._live_position()
            side = self.pos.side
            entry = self.pos.entry
            qty = self.pos.qty
            up = self.pos.unrealized(px) if px else 0.0
            real = False
            if lp is not None:
                try:
                    # Lighter is the SOURCE OF TRUTH — take the REAL side, entry, size and P&L so
                    # the panel always matches your account (this is what stops the website showing
                    # LONG +$ while Lighter holds a losing SHORT).
                    side = lp.get("side") or side
                    entry = float(lp.get("entry") or entry) or entry
                    if lp.get("size"):
                        qty = abs(float(lp.get("size"))) or qty   # Lighter's real size
                    if lp.get("pnl") is not None:
                        up = float(lp.get("pnl")); real = True
                    elif px:                                       # recompute with the REAL side+entry
                        up = qty * (px - entry) if side == "long" else qty * (entry - px)
                except Exception:
                    pass
            lev = self.pos.leverage or 1
            margin = (entry * qty / lev) if (entry and qty and lev) else 0.0
            pnl_pct = round(up / margin * 100, 2) if margin else 0.0
            pos = {"side": side, "entry": round(entry, 2),
                   "qty": round(qty, 6), "leverage": self.pos.leverage,
                   "news": self.pos.news, "opened_at": self.pos.opened_at,
                   "pnl": round(up, 2), "pnl_pct": pnl_pct, "pnl_real": real,
                   "sl": round(self.pos.sl, 2) if self.pos.sl else 0.0,
                   "tp": round(self.pos.tp, 2) if self.pos.tp else 0.0,
                   "entry_fv": round(self.pos.entry_fv, 2) if self.pos.entry_fv else None,
                   "entry_dev": round(self.pos.entry_dev, 2) if self.pos.entry_dev else None,
                   "dev_now": round(self._fv_dev_bps, 2) if self._fv_dev_bps is not None else None}
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        return {
            "enabled": self.enabled,
            "watching": self._watching,
            "leverage": self.leverage,
            "risk_frac": self.risk_frac,
            "strategy": self.strategy,
            "strategy_label": STRATEGIES.get(self.strategy, self.strategy),
            "strategies": STRATEGIES,
            "limit_mode": self._limit_mode,
            "flow_pct": flow_pct,
            "flow_window": win,
            "scalp_window": SCALP_FLOW_WINDOW,
            "fv": round(self._fv_fv, 2) if self._fv_fv else None,
            "fv_lighter": round(self._fv_lt, 2) if self._fv_lt else None,
            "fv_dev_bps": round(self._fv_dev_bps, 2) if self._fv_dev_bps is not None else None,
            "fv_n_leaders": self._fv_n_leaders,
            "fv_status": self._fv_status,
            "fv_enter_bps": FV_ENTER_BPS,
            "fv_exit_bps": FV_EXIT_BPS,
            "configured": bool(self.lighter.configured()),
            "trading_enabled": bool(getattr(self.lighter, "trading_enabled", False)),
            "why_not": self.lighter.why_not_configured() if not self.lighter.configured() else [],
            "price": round(px, 2) if px else None,
            "max_leverage": live.get("max_leverage"),
            "live_ok": bool(live.get("ok")),
            "live_error": live.get("error") or live.get("balance_error") or "",
            "balance_free": bal.get("free"),
            "balance_total": bal.get("total"),
            "live_positions": live.get("positions") or [],
            "position": pos,
            "trades": len(self.history),
            "wins": wins,
            "net_pnl": round(sum(h.get("pnl", 0) for h in self.history), 2),
            "history": self.history[:30],
            "log": self.log[:25],
        }

# 2026-06-08: fixed phantom "closed outside the bot" messages. Root cause was the
# reconcile in manage_loop firing on a STALE pre-open snapshot (4s threshold < 5s
# refresh) and flattening tracking before a real fill propagated. Fix: reconcile now
# only trusts a FRESH live read (>= LIVE_RECONCILE_GRACE secs after opening) and needs
# LIVE_RECONCILE_CONFIRM consecutive such reads. _open also refreshes live right after
# placing the order, and sizing keeps a small margin/fee buffer (LIVE_MARGIN_BUFFER).
