"""
engine.py — event-driven backtester for STRATEGY V2.

Walks the entry-timeframe candles (signals come from CLOSED bars — no look-ahead),
opens at most one position per symbol, and manages it the V2 way:
  * stop = atr_stop_mult * ATR,
  * take HALF off at +1R and move the stop to breakeven,
  * ATR-trail the remainder,
  * time stop: cut if it hasn't reached time_stop_min_r after time_stop_bars.
Fees + slippage are applied to every fill (including the partial). Intrabar uses a
conservative "stop-first" rule. Then it computes the standard metrics and saves them.

Honesty note: a backtest is necessary but not sufficient. Past results don't predict
the future.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from backend.data.fetcher import fetch_history_df
from backend.exchange import make_exchange
from backend.strategy.signal import Strategy, StrategyV3, StrategyV4
from backend.utils.logger import get_logger

log = get_logger("backtest")


def _compute_metrics(trades: list, equity_points: list, start_balance: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"num_trades": 0, "note": "no trades were generated for these settings"}

    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_profit = wins.sum()
    gross_loss = -losses.sum()
    final_equity = equity_points[-1][1] if equity_points else start_balance
    total_return = final_equity / start_balance - 1.0

    max_consec = consec = 0
    for p in pnls:
        if p < 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    eq = pd.Series([e for _, e in equity_points],
                   index=pd.to_datetime([t for t, _ in equity_points], utc=True)).sort_index()
    daily = eq.resample("1D").last().ffill()
    rets = daily.pct_change().dropna()
    sharpe = sortino = 0.0
    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * np.sqrt(365))
    downside = rets[rets < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = float(rets.mean() / downside.std() * np.sqrt(365))
    dd = ((eq - eq.cummax()) / eq.cummax()).min() if len(eq) else 0.0

    return {
        "num_trades": n,
        "total_return_pct": round(total_return * 100, 2),
        "final_equity": round(final_equity, 2),
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "profit_factor": round(float(gross_profit / gross_loss), 3) if gross_loss > 0 else None,
        "max_drawdown_pct": round(float(dd) * 100, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "avg_trade": round(float(pnls.mean()), 2),
        "best_trade": round(float(pnls.max()), 2),
        "worst_trade": round(float(pnls.min()), 2),
        "max_consecutive_losses": int(max_consec),
        "gross_profit": round(float(gross_profit), 2),
        "gross_loss": round(float(gross_loss), 2),
    }


def _manage_v2(O, H, L, C, A, i, side, s):
    """V2 EXIT = fixed bracket: stop at 1R, take-profit at rr*R (default 2R).

    Chosen by the exit-method comparison (exit_comparison.py): letting winners run to
    ~2R beat every 'bank early' exit (partial/ATR-trail/EMA20-trail) — those clipped the
    +2.16R average winner. Risk normalized to 1 so pnl is in R. Intrabar = stop-first."""
    n = len(C)
    slip, fee = s.slippage, s.fee_rate
    long = side == "long"
    entry = C[i] * (1 + slip) if long else C[i] * (1 - slip)
    R = A[i] * s.atr_stop_mult                       # 1R stop distance
    if R <= 0 or np.isnan(R):
        return 0.0, i + 1, None
    qty = 1.0 / R                                    # risk_amount = 1 -> pnl in R
    fee_u = lambda px, q: px * q * fee
    realized = -fee_u(entry, qty)
    stop = entry - R if long else entry + R
    tp = entry + s.rr * R if long else entry - s.rr * R

    def close(px):
        return qty * ((px - entry) if long else (entry - px)) - fee_u(px, qty)

    for j in range(i + 1, n):
        hi, lo = H[j], L[j]
        if (long and lo <= stop) or (not long and hi >= stop):              # stop-first (conservative)
            fill = stop * (1 - slip) if long else stop * (1 + slip)
            return realized + close(fill), j, {"entry": entry, "exit": fill, "reason": "stop"}
        if (long and hi >= tp) or (not long and lo <= tp):                  # take-profit at rr*R
            fill = tp * (1 - slip) if long else tp * (1 + slip)
            return realized + close(fill), j, {"entry": entry, "exit": fill, "reason": "take_profit"}

    fill = C[-1] * (1 - slip) if long else C[-1] * (1 + slip)
    return realized + close(fill), n - 1, {"entry": entry, "exit": fill, "reason": "end_of_data"}


def _manage_v3(O, H, L, C, A, EMA, i, side, s):
    """V3 (Goodman daily) management: LONG-ONLY chandelier ATR trailing stop, plus a
    'trend bent' exit when the daily close drops below the regime MA. No fixed target —
    grow the profit, ride until it bends. Risk normalized to 1 (pnl in R)."""
    n = len(C)
    slip, fee = s.slippage, s.fee_rate
    entry = C[i] * (1 + slip)                         # long-only
    R = A[i] * s.v3_atr_stop
    if R <= 0 or np.isnan(R):
        return 0.0, i + 1, None
    qty = 1.0 / R
    fee_u = lambda px, q: px * q * fee
    realized = -fee_u(entry, qty)
    stop = entry - R
    best = entry

    def close(px):
        return qty * (px - entry) - fee_u(px, qty)

    for j in range(i + 1, n):
        hi, lo, cl, atrj, ema = H[j], L[j], C[j], A[j], EMA[j]
        best = max(best, hi)
        stop = max(stop, best - s.v3_trail_atr * atrj)        # chandelier trail (ratchets up)
        if lo <= stop:                                        # trailing stop hit
            fill = stop * (1 - slip)
            return realized + close(fill), j, {"entry": entry, "exit": fill, "reason": "trail_stop"}
        if cl < ema:                                          # trend bent: close below regime MA
            fill = cl * (1 - slip)
            return realized + close(fill), j, {"entry": entry, "exit": fill, "reason": "regime_exit"}

    fill = C[-1] * (1 - slip)
    return realized + close(fill), n - 1, {"entry": entry, "exit": fill, "reason": "end_of_data"}


def _run_v4_backtest(s, symbol: str, exchange, save: bool = True) -> dict:
    """STRATEGY V4 — Momentum Sweep VWAP Breakout. Faithful to the reference backtest:
    20x ALL-IN notional from a 100 USDT account (compounding); stop = v4_atr_stop * ATR;
    take-profit at v4_rr * risk; skip setups whose stop is wider than v4_max_stop_pct;
    N-bar cooldown after each close. Entry fills at the NEXT bar's open; intrabar exits
    use the conservative stop-first rule, with slippage on the exit and taker fees on
    both sides. Single timeframe (v4_timeframe)."""
    strat = StrategyV4(s)
    tf = s.v4_timeframe
    df = fetch_history_df(exchange, symbol, tf, s.backtest_days, s.data_cache_dir)
    df = strat.prepare(df, df)
    n = len(df)
    warm = max(s.v4_ema_slow, s.v4_sweep_lookback, s.v4_atr_period, s.v4_vol_ma) + 5
    if n < warm + 50:
        return {"error": f"not enough {tf} history for {symbol} ({n} bars after warmup)"}

    O, H, L = df["open"].values, df["high"].values, df["low"].values
    A = df["atr"].values
    TS = df.index
    LB, SB = df["long_breakout"].values, df["short_breakout"].values
    LS, SS = df["long_sweep"].values, df["short_sweep"].values

    lev, fee, slip = s.v4_leverage, s.v4_fee_rate, s.v4_slippage
    astop, rr, max_sp, cool_bars = s.v4_atr_stop, s.v4_rr, s.v4_max_stop_pct, s.v4_cooldown_bars

    balance = s.v4_start_balance
    equity_points = [(TS[warm].isoformat(), round(balance, 2))]
    trades: list = []
    pos = None
    cooldown = 0

    for i in range(warm, n - 1):
        if balance <= 0:
            break
        if pos is None:
            if cooldown > 0:
                cooldown -= 1
                continue
            atr_i = A[i]
            if atr_i != atr_i or atr_i <= 0:                  # NaN/zero ATR -> can't size a stop
                continue
            side = "long" if (LB[i] or LS[i]) else ("short" if (SB[i] or SS[i]) else None)
            if side is None:
                continue
            entry = O[i + 1]                                  # fill at the NEXT bar's open (no look-ahead)
            if side == "long":
                stop = entry - atr_i * astop
                tp = entry + (entry - stop) * rr
            else:
                stop = entry + atr_i * astop
                tp = entry - (stop - entry) * rr
            stop_dist = abs(entry - stop)
            if stop_dist <= 0 or stop_dist / entry > max_sp:  # too wide for 20x -> skip
                continue
            pos = {"side": side, "entry": entry, "stop": stop, "tp": tp,
                   "notional": balance * lev, "etime": TS[i + 1], "stop_dist": stop_dist}
        else:
            side, entry = pos["side"], pos["entry"]
            stop, tp, notional = pos["stop"], pos["tp"], pos["notional"]
            hi, lo = H[i], L[i]
            exit_price = reason = None
            if side == "long":
                if lo <= stop:                                # stop-first (conservative)
                    exit_price, reason = stop * (1 - slip), "stop"
                elif hi >= tp:
                    exit_price, reason = tp * (1 - slip), "take_profit"
            else:
                if hi >= stop:
                    exit_price, reason = stop * (1 + slip), "stop"
                elif lo <= tp:
                    exit_price, reason = tp * (1 + slip), "take_profit"
            if exit_price is not None:
                pnl_pct = (exit_price - entry) / entry if side == "long" else (entry - exit_price) / entry
                net = notional * pnl_pct - notional * fee * 2
                balance += net
                risk_dollars = notional * (pos["stop_dist"] / entry)
                trades.append({
                    "mode": "backtest", "symbol": symbol, "side": side,
                    "entry_time": pos["etime"].isoformat(), "entry_price": round(entry, 4),
                    "qty": round(notional / entry, 6), "stop": round(stop, 4), "take": round(tp, 4),
                    "exit_time": TS[i].isoformat(), "exit_price": round(exit_price, 4),
                    "pnl": round(net, 2), "pnl_pct": round(net / notional * 100, 3),
                    "fees": round(notional * fee * 2, 4), "reason": reason,
                    "r": round(net / risk_dollars, 3) if risk_dollars else 0.0,
                })
                equity_points.append((TS[i].isoformat(), round(balance, 2)))
                pos = None
                cooldown = cool_bars

    metrics = _compute_metrics(trades, equity_points, s.v4_start_balance)
    result = {
        "symbol": symbol, "strategy": "V4",
        "params": {"timeframe": tf, "ema": f"{s.v4_ema_fast}/{s.v4_ema_slow}",
                   "atr_stop": s.v4_atr_stop, "rr": s.v4_rr, "max_stop_pct": s.v4_max_stop_pct,
                   "leverage": s.v4_leverage, "cooldown_bars": s.v4_cooldown_bars,
                   "start_balance": s.v4_start_balance, "fee_rate": s.v4_fee_rate,
                   "slippage": s.v4_slippage, "days": s.backtest_days},
        "metrics": metrics, "trades": trades[-500:], "equity_curve": equity_points[-2000:],
    }
    if save:
        os.makedirs(s.results_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(s.results_dir, f"backtest_{symbol.replace('/', '')}_V4_{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        log.info("V4 backtest %s saved -> %s | %s", symbol, path, metrics)
    return result


def run_backtest(settings, symbol: str, exchange=None, save: bool = True) -> dict:
    s = settings
    exchange = exchange or make_exchange(s)
    if getattr(s, "strategy_version", "v2") == "v4":
        return _run_v4_backtest(s, symbol, exchange, save=save)
    v3 = getattr(s, "strategy_version", "v2") == "v3"

    if v3:
        strat = StrategyV3(s)
        tf, days = s.v3_timeframe, max(s.backtest_days, s.v3_history_days)
        daily = fetch_history_df(exchange, symbol, tf, days, s.data_cache_dir)
        df = strat.prepare(daily, daily).dropna(subset=["ema_regime", "atr", "donch_hi"])
        need_cols = "v3"
    else:
        strat = Strategy(s)
        htf = fetch_history_df(exchange, symbol, s.htf_timeframe, s.backtest_days, s.data_cache_dir)
        entry = fetch_history_df(exchange, symbol, s.entry_timeframe, s.backtest_days, s.data_cache_dir)
        df = strat.prepare(htf, entry).dropna(subset=["ema50", "atr", "atr_floor"])
        need_cols = "v2"
    if len(df) < 60:
        return {"error": f"not enough history for {symbol} after warmup ({len(df)} bars)"}

    O, H, L, C, A = (df["open"].values, df["high"].values, df["low"].values,
                     df["close"].values, df["atr"].values)
    EMA = df["ema_regime"].values if need_cols == "v3" else None
    sig = [None] * len(df)
    for k, (_, row) in enumerate(df.iterrows()):
        sg = strat.evaluate_row(row, symbol=symbol)
        sig[k] = sg.side if sg else None

    # version-aware single-trade manager (risk normalized to 1)
    if v3:
        manage = lambda idx, sd_: _manage_v3(O, H, L, C, A, EMA, idx, sd_, s)
        stop_mult = s.v3_atr_stop
    else:
        manage = lambda idx, sd_: _manage_v2(O, H, L, C, A, idx, sd_, s)
        stop_mult = s.atr_stop_mult

    equity = s.start_balance
    equity_points = [(df.index[0].isoformat(), equity)]
    trades: list = []
    day_key, day_start_equity, last_loss_ts = None, equity, None

    i = 1
    n = len(df)
    while i < n - 1:
        t = df.index[i]
        dk = t.date()
        if dk != day_key:
            day_key, day_start_equity = dk, equity

        side = sig[i]
        if side is None:
            i += 1
            continue
        day_pnl_pct = (equity - day_start_equity) / day_start_equity
        if day_pnl_pct <= -abs(s.max_daily_loss):
            i += 1
            continue
        if last_loss_ts is not None and (t.timestamp() - last_loss_ts) < s.cooldown_minutes * 60:
            i += 1
            continue

        r, jx, info = manage(i, side)
        if info is None:
            i += 1
            continue
        risk_amount = equity * s.risk_per_trade
        pnl = r * risk_amount
        equity += pnl
        sd = A[i] * stop_mult
        qty0 = risk_amount / sd if sd else 0.0
        notional = info["entry"] * qty0
        trades.append({
            "mode": "backtest", "symbol": symbol, "side": side,
            "entry_time": t.isoformat(), "entry_price": round(info["entry"], 4),
            "qty": round(qty0, 6), "stop": round(info["entry"] - sd if side == "long" else info["entry"] + sd, 4),
            "take": None, "exit_time": df.index[jx].isoformat(), "exit_price": round(info["exit"], 4),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl / notional * 100, 3) if notional else 0.0,
            "fees": None, "reason": info["reason"], "r": round(r, 3),
        })
        equity_points.append((df.index[jx].isoformat(), equity))
        if pnl < 0:
            last_loss_ts = t.timestamp()
        i = jx + 1

    metrics = _compute_metrics(trades, equity_points, s.start_balance)
    result = {
        "symbol": symbol, "strategy": "V3" if v3 else "V2",
        "params": ({"timeframe": s.v3_timeframe, "donchian": s.v3_donchian, "regime_ma": s.v3_regime_ma,
                    "atr_stop": s.v3_atr_stop, "trail_atr": s.v3_trail_atr,
                    "risk_per_trade": s.risk_per_trade, "fee_rate": s.fee_rate, "slippage": s.slippage,
                    "days": max(s.backtest_days, s.v3_history_days)}
                   if v3 else
                   {"htf": s.htf_timeframe, "entry_tf": s.entry_timeframe, "adx_thresh": s.adx_thresh,
                    "atr_stop_mult": s.atr_stop_mult, "partial_frac": s.partial_frac,
                    "trail_atr_mult": s.trail_atr_mult, "time_stop_bars": s.time_stop_bars,
                    "risk_per_trade": s.risk_per_trade, "fee_rate": s.fee_rate, "slippage": s.slippage,
                    "days": s.backtest_days}),
        "metrics": metrics, "trades": trades[-500:], "equity_curve": equity_points[-2000:],
    }
    if save:
        os.makedirs(s.results_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(s.results_dir, f"backtest_{symbol.replace('/', '')}_{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        log.info("Backtest %s saved -> %s | %s", symbol, path, metrics)
    return result
