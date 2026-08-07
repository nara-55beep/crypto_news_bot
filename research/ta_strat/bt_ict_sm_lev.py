"""
bt_ict_sm_lev.py — same faithful ICT SM Trades replay as bt_ict_sm.py, but sized at a
FIXED leverage (default 20x) on a $100 account, reported per 1-YEAR window.

Each trade's notional = current equity x LEVERAGE (compounding all-in), qty = notional/entry.
The bot's own price levels (FVG entry, stop beyond the grab candle, TP1->BE->TP2, the -2R/day
halt, 2 trades/day) are unchanged — only the position SIZE is scaled to 20x. Liquidation is
modeled: if a 1m bar's adverse extreme reaches the isolated-margin liq price
(entry*(1 -/+ (1/lev - maint))) before the stop, the trade loses the whole margin (all-in -> $0).
"""
from __future__ import annotations
import os, sys, time
from collections import Counter
import numpy as np, pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import ict_sm_paper
from bt_ict_sm import FakeMarket, to_candles, resample, load_1m, W1M, W5M, W1H, LEN5, LEN1H

LEVERAGE = 20.0
MAINT = 0.004              # maintenance margin
START = 100.0


def run():
    df1, src = load_1m()
    print(f"[data] {src}: {len(df1):,} 1m bars  {df1.index[0].date()} -> {df1.index[-1].date()}  "
          f"@ {LEVERAGE:.0f}x on ${START:.0f}\n", flush=True)
    c1 = to_candles(df1); c5 = to_candles(resample(df1, "5min")); c1h = to_candles(resample(df1, "1h"))
    n = len(c1)

    # 1-year window boundaries (epoch secs)
    start_ts = c1[0]["t"]
    win_edges = [start_ts + k * 365 * 86400 for k in range(0, 4)]
    def win_of(ts):
        for k in range(3):
            if win_edges[k] <= ts < win_edges[k + 1]:
                return k
        return None

    bot = ict_sm_paper.ICTSMTradesBot(FakeMarket())
    bot._save = lambda *a, **k: None
    bot._note = lambda *a, **k: None
    bot.reset(); bot.enabled = True

    W = {k: {"trades": [], "eq": [START], "bal": START, "blown": False, "peak": START}
         for k in range(3)}
    cur_win = [None]
    orig_open, orig_close = bot._open, bot._close
    st = {"before": None, "ctx": None, "liq": None}

    def open_wrap(side, entry, stop, tp1, tp2, raid, pool):
        k = cur_win[0]
        if k is None or W[k]["blown"]:
            return
        before = W[k]["bal"]
        bot.balance = before                     # bot sizes off equity()=balance
        orig_open(side, entry, stop, tp1, tp2, raid, pool)
        if bot.pos is not None:
            p = bot.pos
            notional = before * LEVERAGE
            p.qty = p.qty0 = notional / entry
            p.risk_usd = p.qty0 * abs(entry - stop)
            st["before"] = before
            st["ctx"] = {"side": side, "entry": entry, "qty0": p.qty0, "notional": notional,
                         "risk_usd": p.risk_usd}
            # isolated-margin liquidation price for an all-in position
            move = max(1.0 / LEVERAGE - MAINT, 1e-9)
            st["liq"] = entry * (1 - move) if side == "long" else entry * (1 + move)

    def close_wrap(price, reason):
        ctx, before, k = st["ctx"], st["before"], cur_win[0]
        orig_close(price, reason)
        if ctx is not None and k is not None:
            pnl = bot.balance - before           # bot booked the trade off `before`
            W[k]["bal"] = max(0.0, before + pnl)
            W[k]["trades"].append({**ctx, "exit": price, "reason": reason, "pnl": pnl,
                                   "rr": pnl / ctx["risk_usd"] if ctx["risk_usd"] else 0.0})
            W[k]["eq"].append(W[k]["bal"])
            if W[k]["bal"] <= 1.0:
                W[k]["blown"] = True
            st["ctx"] = None

    bot._open, bot._close = open_wrap, close_wrap

    i5 = i1h = 0; t0 = time.time()
    for i in range(n):
        cur = c1[i]; ts = cur["t"]; close_t = ts + 60
        k = win_of(ts)
        if k != cur_win[0]:                      # new 1-year window -> fresh $100 account
            cur_win[0] = k
            if k is not None:
                bot.pos = None; bot._reset_setup()
                bot.balance = W[k]["bal"]
        while i5 + 1 < len(c5) and c5[i5 + 1]["t"] + LEN5 <= close_t: i5 += 1
        while i1h + 1 < len(c1h) and c1h[i1h + 1]["t"] + LEN1H <= close_t: i1h += 1
        hi5 = i5 + 1 if (c5[i5]["t"] + LEN5 <= close_t) else i5
        hi1h = i1h + 1 if (c1h[i1h]["t"] + LEN1H <= close_t) else i1h
        bot._cs1m = c1[max(0, i - W1M + 1): i + 1]
        bot._cs5m = c5[max(0, hi5 - W5M): hi5]
        bot._cs1h = c1h[max(0, hi1h - W1H): hi1h]
        if not bot._cs1h or k is None:
            continue

        fm = bot.market
        if bot.pos is not None:
            side = bot.pos.side
            # LIQUIDATION only if the stop is WIDER than the liq distance (else the tight ICT
            # stop fills first and saves the margin — which is the usual case).
            liq, stp = st["liq"], bot.pos.stop
            stop_beyond_liq = (side == "long" and stp <= liq) or (side == "short" and stp >= liq)
            hit_liq = (liq is not None and stop_beyond_liq and
                       ((side == "long" and cur["l"] <= liq) or (side == "short" and cur["h"] >= liq)))
            if hit_liq:
                before = st["before"]
                W[k]["trades"].append({**st["ctx"], "exit": liq, "reason": "LIQUIDATED",
                                       "pnl": -before, "rr": -1.0 / (st["ctx"]["risk_usd"]/before) if before else -1})
                W[k]["bal"] = 0.0; W[k]["eq"].append(0.0); W[k]["blown"] = True
                bot.pos = None; bot._reset_setup(); st["ctx"] = None
            else:
                seq = (cur["l"], cur["h"], cur["c"]) if side == "long" else (cur["h"], cur["l"], cur["c"])
                for px in seq:
                    if bot.pos is None: break
                    fm.px = px; bot._manage_fill(px)
        fm.px = cur["c"]

        bot._last_bar_t = ts; bot._roll_day(ts); bot.kz = bot._killzone(ts)
        if bot.pos is not None or W[k]["blown"]:
            continue
        if bot.kz is None:
            bot._reset_setup(); continue
        bot._htf()
        if not bot._can_trade_today():
            continue
        if bot.phase == "SCAN": bot._scan(ts)
        if bot.phase == "SWEPT": bot._swept_to_armed()
        if bot.phase in ("ARMED", "PENDING"): bot._try_fill()
        if i and i % 400000 == 0:
            print(f"  ...{i:,}/{n:,}  ({time.time()-t0:.0f}s)", flush=True)

    report(W, win_edges, df1)


def _dd(eq):
    eq = np.array(eq, float); peak = np.maximum.accumulate(eq)
    return ((eq - peak) / peak).min()


def report(W, edges, df1):
    print("\n" + "=" * 84)
    print(f"ICT SM Trades — $100 @ {LEVERAGE:.0f}x leverage — per 1-YEAR window (1m BTC)")
    print("=" * 84)
    print(f"{'window':<26}{'trades':>7}{'win%':>7}{'ROI%':>10}{'maxDD%':>9}{'profit$':>10}{'  result'}")
    for k in range(3):
        w = W[k]; tr = w["trades"]
        d0 = pd.to_datetime(edges[k], unit="s").date(); d1 = pd.to_datetime(edges[k + 1], unit="s").date()
        if not tr:
            print(f"{str(d0)+'->'+str(d1):<26}{'0':>7}{'--':>7}{'--':>10}{'--':>9}{'--':>10}"); continue
        pnls = np.array([t["pnl"] for t in tr]); wins = (pnls > 1e-9).mean()
        roi = w["bal"] / START - 1; dd = _dd(w["eq"]); prof = w["bal"] - START
        liqs = sum(1 for t in tr if t["reason"] == "LIQUIDATED")
        res = "WIPED OUT" if w["blown"] else "survived"
        if liqs: res += f" ({liqs} liq)"
        print(f"{str(d0)+'->'+str(d1):<26}{len(tr):>7}{wins*100:>6.0f}{roi*100:>10.0f}{dd*100:>9.0f}"
              f"{prof:>10.2f}  {res}")
    # exit-reason mix + cost note from all windows
    allt = [t for k in range(3) for t in W[k]["trades"]]
    print(f"\nexit reasons (all): {dict(Counter(t['reason'] for t in allt))}")
    pd.DataFrame(allt).to_csv(os.path.join(ROOT, "research", "ta_strat", "cache", "ict_sm_lev_trades.csv"), index=False)
    print("note: entries are limit/maker; at 20x a 1bp round-trip ~= 0.4% of equity PER TRADE")
    print("      (20x cost multiplier), so any real fee/slippage compounds hard on top of the above.")


if __name__ == "__main__":
    run()
