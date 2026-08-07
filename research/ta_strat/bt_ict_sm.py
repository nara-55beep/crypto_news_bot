"""
bt_ict_sm.py — faithful backtest of the ACTUAL "ICT SM Trades" bot (ict_sm_paper /
ict_bot.ICTBot) on 1-minute BTC over the max window.

It does NOT reimplement the strategy — it drives the bot's own methods bar-by-bar in the
exact order its live manage_loop() uses:
  manage open pos (intrabar path) -> roll day -> killzone -> HTF -> SCAN -> SWEPT -> ARMED/fill
with the same 200x1m / 150x5m / 200x1h rolling windows the live loop fetches, and HTF
candles revealed only once fully closed (no look-ahead). 5m/1h are resampled from the 1m.

P&L note: the bot banks 50% at TP1 via _partial() (updates balance, NOT the trade record's
pnl field) and books the rest at _close(). So we measure TRUE per-trade P&L as the BALANCE
DELTA across each trade (captured by wrapping _open/_close), and build the real compounding
equity curve from bot.balance — the only correct accounting.
"""
from __future__ import annotations
import os, sys, time
from collections import Counter
import numpy as np, pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import ict_sm_paper

CACHE = os.path.join(ROOT, "research", "ta_strat", "cache")
W1M, W5M, W1H = 200, 150, 200
LEN5, LEN1H = 300, 3600


class FakeMarket:
    px = 0.0
    def price(self, *_):  return self.px


def to_candles(df):
    t = ((df.index - pd.Timestamp("1970-01-01")) // pd.Timedelta("1s")).astype("int64").to_numpy()
    o, h, l, c, v = (df.open.to_numpy(), df.high.to_numpy(), df.low.to_numpy(),
                     df.close.to_numpy(), df.volume.to_numpy())
    return [{"t": int(t[i]), "o": float(o[i]), "h": float(h[i]), "l": float(l[i]),
             "c": float(c[i]), "v": float(v[i])} for i in range(len(df))]


def resample(df, rule):
    return df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def load_1m():
    p_max = os.path.join(CACHE, "BTC_1m_max.csv")
    p = p_max if os.path.exists(p_max) else os.path.join(CACHE, "BTC_1m.csv")
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    return df[~df.index.duplicated(keep="last")].sort_index(), os.path.basename(p)


def main():
    df1, src = load_1m()
    print(f"[data] {src}: {len(df1):,} 1m bars  {df1.index[0]} -> {df1.index[-1]} "
          f"({(df1.index[-1]-df1.index[0]).days}d)", flush=True)
    c1 = to_candles(df1); c5 = to_candles(resample(df1, "5min")); c1h = to_candles(resample(df1, "1h"))
    n = len(c1)

    bot = ict_sm_paper.ICTSMTradesBot(FakeMarket())
    bot._save = lambda *a, **k: None
    bot._note = lambda *a, **k: None
    bot.reset(); bot.enabled = True

    # ---- capture TRUE per-trade P&L via balance delta (includes the TP1 partial) ----
    trades, eq = [], [100.0]
    orig_open, orig_close = bot._open, bot._close
    st = {"before": None, "ctx": None}

    def open_wrap(side, entry, stop, tp1, tp2, raid, pool):
        before = bot.balance
        orig_open(side, entry, stop, tp1, tp2, raid, pool)
        if bot.pos is not None:
            st["before"] = before
            st["ctx"] = {"side": side, "entry": entry, "qty0": bot.pos.qty0,
                         "risk_usd": bot.pos.risk_usd, "notional": bot.pos.qty0 * entry}

    def close_wrap(price, reason):
        ctx, before = st["ctx"], st["before"]
        orig_close(price, reason)
        if ctx is not None:
            pnl = bot.balance - before
            trades.append({**ctx, "exit": price, "reason": reason, "pnl": pnl,
                           "rr": pnl / ctx["risk_usd"] if ctx["risk_usd"] else 0.0})
            eq.append(bot.balance)
            st["ctx"] = None

    bot._open, bot._close = open_wrap, close_wrap

    i5 = i1h = 0
    t0 = time.time()
    for i in range(n):
        cur = c1[i]; close_t = cur["t"] + 60
        while i5 + 1 < len(c5) and c5[i5 + 1]["t"] + LEN5 <= close_t:
            i5 += 1
        while i1h + 1 < len(c1h) and c1h[i1h + 1]["t"] + LEN1H <= close_t:
            i1h += 1
        hi5 = i5 + 1 if (c5[i5]["t"] + LEN5 <= close_t) else i5
        hi1h = i1h + 1 if (c1h[i1h]["t"] + LEN1H <= close_t) else i1h
        bot._cs1m = c1[max(0, i - W1M + 1): i + 1]
        bot._cs5m = c5[max(0, hi5 - W5M): hi5]
        bot._cs1h = c1h[max(0, hi1h - W1H): hi1h]
        if not bot._cs1h:
            continue

        fm = bot.market
        if bot.pos is not None:
            side = bot.pos.side
            seq = (cur["l"], cur["h"], cur["c"]) if side == "long" else (cur["h"], cur["l"], cur["c"])
            for px in seq:
                if bot.pos is None:
                    break
                fm.px = px; bot._manage_fill(px)
        fm.px = cur["c"]

        ts = cur["t"]; bot._last_bar_t = ts
        bot._roll_day(ts); bot.kz = bot._killzone(ts)
        if bot.pos is not None:
            continue
        if bot.kz is None:
            bot._reset_setup(); continue
        bot._htf()
        if not bot._can_trade_today():
            continue
        if bot.phase == "SCAN":
            bot._scan(ts)
        if bot.phase == "SWEPT":
            bot._swept_to_armed()
        if bot.phase in ("ARMED", "PENDING"):
            bot._try_fill()
        if i and i % 200000 == 0:
            print(f"  ...{i:,}/{n:,}  trades={len(trades)}  bal=${bot.balance:,.2f}  ({time.time()-t0:.0f}s)", flush=True)

    if bot.pos is not None:
        bot._close(c1[-1]["c"], "end_of_data")

    report(trades, eq, df1, bot.balance)


def _stats(pnls, eq, start=100.0):
    pnls = np.array(pnls, float)
    eq = np.array(eq, float)
    peak = np.maximum.accumulate(eq); dd = (eq - peak) / peak
    wins = pnls[pnls > 1e-9]; losses = pnls[pnls < -1e-9]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    return dict(n=len(pnls), win=len(wins) / max(len(pnls), 1), roi=eq[-1] / start - 1,
                final=eq[-1], profit=eq[-1] - start, maxdd=dd.min(), pf=pf)


def report(trades, eq, df1, final_balance):
    days = max((df1.index[-1] - df1.index[0]).days, 1)
    print("\n" + "=" * 78)
    print("ICT SM Trades — BACKTEST (1m BTC, faithful replay of the actual bot)")
    print("=" * 78)
    if not trades:
        print("NO TRADES over the window (killzone-gated ICT setups are rare)."); return
    pnls = [t["pnl"] for t in trades]
    m = _stats(pnls, eq)
    rr = np.array([t["rr"] for t in trades])
    print(f"window           : {df1.index[0].date()} -> {df1.index[-1].date()}  ({days} days)")
    print(f"trades           : {m['n']}   ({m['n']/days*30:.1f}/mo)")
    print(f"win rate         : {m['win']*100:.1f}%")
    print(f"ROI (on $100)    : {m['roi']*100:+.1f}%   (final ${m['final']:.2f})")
    print(f"net profit       : ${m['profit']:+.2f}")
    print(f"max drawdown     : {m['maxdd']*100:.1f}%")
    print(f"profit factor    : {m['pf']:.2f}")
    print(f"avg / best / worst trade : {rr.mean():+.2f}R / ${max(pnls):+.2f} / ${min(pnls):+.2f}")
    print(f"exit reasons     : {dict(Counter(t['reason'] for t in trades))}")

    print("\ncost stress (haircut per trade, round-trip on full notional — pessimistic taker):")
    for bps in (0, 1, 2, 5):
        adj, e = [], [100.0]
        bal = 100.0
        for t in trades:
            net = t["pnl"] - t["notional"] * (bps / 1e4) * 2
            adj.append(net); bal += net; e.append(bal)
        mm = _stats(adj, e)
        print(f"  {bps}bps/side : ROI {mm['roi']*100:+7.1f}%  win {mm['win']*100:4.1f}%  "
              f"PF {mm['pf']:.2f}  maxDD {mm['maxdd']*100:5.1f}%  profit ${mm['profit']:+.2f}")
    pd.DataFrame(trades).to_csv(os.path.join(CACHE, "ict_sm_bt_trades.csv"), index=False)
    print(f"\nsaved {len(trades)} trades -> cache/ict_sm_bt_trades.csv")


if __name__ == "__main__":
    main()
