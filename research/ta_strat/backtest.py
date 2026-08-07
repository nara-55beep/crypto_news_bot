"""
backtest.py — realistic leverage-aware intraday backtester.

Design principles (anti-overfit / honesty):
  * NO look-ahead: signals are decided on bar CLOSE i, the trade is entered at bar
    i+1 OPEN. Indicators use only data up to the signal bar.
  * Intrabar path: each in-trade bar checks STOP / LIQUIDATION first (adverse),
    then TARGET (favorable) — the conservative ordering when both are in-range.
  * Costs: taker slippage (bps) charged on BOTH entry and exit; optional funding
    charged per 8h held. Tested across a grid so the cost cliff is visible.
  * Leverage + liquidation: isolated margin. A position is liquidated if price
    moves against it by ~ (1/L - maint). On liquidation the trade loses ~the full
    margin allocated (capped at -100% of risked capital).
  * Two sizing modes:
       'risk'  : risk a fixed fraction of equity to the stop (pro sizing). Effective
                 leverage = notional/equity, capped at L_max.
       'allin' : notional = equity * L every trade (the "$100 at 20x all-in" convention).
  * Equity compounds from a $100 start. Per-trade ledger -> full metric suite.

A 'signal' array is +1 (go long), -1 (go short), 0 (no entry). Exits are handled by
stop / target / trailing / time-stop / end-of-day / opposite-signal, per StratParams.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

MAINT_MARGIN = 0.004      # 0.4% BTC maintenance margin (Binance tier-1 approx)


@dataclass
class StratParams:
    # sizing
    sizing: str = "risk"          # 'risk' or 'allin'
    risk_frac: float = 0.05       # risk 5% of equity to stop (sizing='risk')
    leverage: float = 10.0        # fixed leverage (sizing='allin') / cap (sizing='risk')
    lev_cap: float = 20.0         # hard leverage cap in 'risk' mode
    start_equity: float = 100.0
    # exits (units = ATR multiples unless *_pct)
    stop_atr: float = 1.5
    target_atr: float = 3.0       # 0 -> no fixed target
    trail_atr: float = 0.0        # 0 -> no trailing; else trail by N*ATR from peak
    time_stop_bars: int = 0       # 0 -> none; else force-exit after N bars
    eod_flat: bool = False        # exit at last bar of UTC day
    allow_short: bool = True
    # costs
    slip_bps: float = 2.0         # taker slippage per side
    funding_bps_8h: float = 1.0   # funding drag per 8h held
    bars_per_hour: float = 4.0    # 15m=4, 5m=12, 1m=60 (for funding accrual)


def _metrics(trades: pd.DataFrame, equity_curve: pd.Series, params: StratParams,
             bars_index: pd.DatetimeIndex) -> dict:
    if len(trades) == 0:
        return {"n_trades": 0, "note": "no trades"}
    pnl = trades["pnl_usd"].values
    rmult = trades["r_mult"].values
    wins = trades[trades["pnl_usd"] > 0]
    losses = trades[trades["pnl_usd"] <= 0]
    eq = equity_curve
    peak = eq.cummax()
    dd = eq / peak - 1.0
    maxdd = dd.min()
    final = eq.iloc[-1]
    start = params.start_equity
    roi = final / start - 1.0
    gross_win = wins["pnl_usd"].sum()
    gross_loss = abs(losses["pnl_usd"].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else np.inf
    # time-based Sharpe from daily equity changes
    daily = eq.resample("1D").last().dropna()
    dret = daily.pct_change().dropna()
    sharpe = (dret.mean() / dret.std() * np.sqrt(365)) if dret.std() > 0 else 0.0
    span_days = max((bars_index[-1] - bars_index[0]).days, 1)
    # max consecutive losses
    streak = mx = 0
    for p in pnl:
        if p <= 0:
            streak += 1; mx = max(mx, streak)
        else:
            streak = 0
    liqs = int((trades["reason"] == "liq").sum())
    return {
        "n_trades": int(len(trades)),
        "win_rate": round(len(wins) / len(trades), 4),
        "roi": round(roi, 4),
        "final_equity": round(final, 2),
        "profit_usd": round(final - start, 2),
        "max_dd": round(maxdd, 4),
        "profit_factor": round(pf, 3) if pf != np.inf else None,
        "avg_trade_usd": round(pnl.mean(), 4),
        "expectancy_R": round(np.nanmean(rmult), 3),
        "avg_win_usd": round(wins["pnl_usd"].mean(), 3) if len(wins) else 0.0,
        "avg_loss_usd": round(losses["pnl_usd"].mean(), 3) if len(losses) else 0.0,
        "sharpe": round(sharpe, 2),
        "max_consec_loss": int(mx),
        "liquidations": liqs,
        "trades_per_day": round(len(trades) / span_days, 2),
        "exposure_days": span_days,
    }


def run_backtest(df: pd.DataFrame, signal: np.ndarray, atr: np.ndarray,
                 params: StratParams) -> tuple[pd.DataFrame, pd.Series, dict]:
    """
    df: OHLCV with DatetimeIndex (UTC, ascending). signal/atr: arrays aligned to df,
    decided AT bar close. Returns (trades_df, equity_curve, metrics).
    """
    o = df["open"].values; h = df["high"].values
    l = df["low"].values;  c = df["close"].values
    idx = df.index
    n = len(df)
    day = idx.normalize().values

    equity = params.start_equity
    eq_times = [idx[0]]; eq_vals = [equity]
    trades = []

    i = 0
    mm = MAINT_MARGIN
    while i < n - 1:
        s = signal[i]
        if s == 0 or (s < 0 and not params.allow_short):
            i += 1; continue
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            i += 1; continue

        side = 1 if s > 0 else -1
        entry_i = i + 1
        entry_px = o[entry_i] * (1 + side * params.slip_bps / 1e4)
        stop_dist = params.stop_atr * a
        if stop_dist <= 0:
            i += 1; continue
        stop_px = entry_px - side * stop_dist
        tgt_px = entry_px + side * params.target_atr * a if params.target_atr > 0 else None

        # ---- position sizing ----
        if params.sizing == "allin":
            lev = params.leverage
            notional = equity * lev
        else:  # risk-based
            stop_frac = stop_dist / entry_px
            notional = (params.risk_frac * equity) / stop_frac
            lev = notional / equity
            if lev > params.lev_cap:
                lev = params.lev_cap; notional = equity * lev
        qty = notional / entry_px
        # liquidation price (isolated): adverse move of (1/lev - mm)
        liq_move = max(1.0 / lev - mm, 1e-9)
        liq_px = entry_px * (1 - side * liq_move)

        # ---- walk forward through the trade ----
        peak = entry_px
        trail_stop = stop_px if params.trail_atr > 0 else None
        exit_px = None; reason = None; exit_i = None
        for j in range(entry_i, n):
            hi, lo, cl = h[j], l[j], c[j]
            # update trailing
            if params.trail_atr > 0:
                if side > 0:
                    peak = max(peak, hi)
                    trail_stop = max(trail_stop, peak - params.trail_atr * a)
                else:
                    peak = min(peak, lo)
                    trail_stop = min(trail_stop, peak + params.trail_atr * a)
                eff_stop = (max(stop_px, trail_stop) if side > 0
                            else min(stop_px, trail_stop))
            else:
                eff_stop = stop_px

            # adverse checks FIRST (conservative)
            if side > 0:
                if lo <= liq_px:
                    exit_px = liq_px; reason = "liq"; exit_i = j; break
                if lo <= eff_stop:
                    exit_px = eff_stop; reason = "stop"; exit_i = j; break
                if tgt_px is not None and hi >= tgt_px:
                    exit_px = tgt_px; reason = "target"; exit_i = j; break
            else:
                if hi >= liq_px:
                    exit_px = liq_px; reason = "liq"; exit_i = j; break
                if hi >= eff_stop:
                    exit_px = eff_stop; reason = "stop"; exit_i = j; break
                if tgt_px is not None and lo <= tgt_px:
                    exit_px = tgt_px; reason = "target"; exit_i = j; break
            # time stop
            if params.time_stop_bars and (j - entry_i + 1) >= params.time_stop_bars:
                exit_px = cl; reason = "time"; exit_i = j; break
            # end-of-day flat
            if params.eod_flat and (j + 1 >= n or day[j + 1] != day[entry_i]):
                exit_px = cl; reason = "eod"; exit_i = j; break
        if exit_px is None:
            exit_px = c[n - 1]; reason = "end"; exit_i = n - 1

        # apply exit slippage (favorable exits pay slip too; liq is a hard fill)
        if reason != "liq":
            exit_px = exit_px * (1 - side * params.slip_bps / 1e4)

        # ---- pnl ----
        held_bars = exit_i - entry_i + 1
        funding = (held_bars / params.bars_per_hour / 8.0) * params.funding_bps_8h / 1e4
        gross_ret = side * (exit_px / entry_px - 1.0)
        pnl_frac = lev * (gross_ret - funding)        # return on equity (margin)
        pnl_frac = max(pnl_frac, -1.0)                # can't lose more than the account
        if reason == "liq":
            pnl_frac = -1.0 * min(1.0, lev * liq_move)  # ~full margin loss
            pnl_frac = max(pnl_frac, -1.0)
        pnl_usd = equity * pnl_frac
        r_mult = (side * (exit_px / entry_px - 1.0) * entry_px) / stop_dist  # in R units

        equity = max(equity + pnl_usd, 0.0)
        eq_times.append(idx[exit_i]); eq_vals.append(equity)
        trades.append({
            "entry_time": idx[entry_i], "exit_time": idx[exit_i], "side": side,
            "entry_px": entry_px, "exit_px": exit_px, "reason": reason,
            "held_bars": held_bars, "lev": round(lev, 2),
            "pnl_frac": pnl_frac, "pnl_usd": pnl_usd, "r_mult": r_mult,
        })
        if equity <= 0.01:
            break
        i = exit_i + 1   # re-enter only after current trade closes (no pyramiding)

    trades_df = pd.DataFrame(trades)
    equity_curve = pd.Series(eq_vals, index=pd.DatetimeIndex(eq_times))
    equity_curve = equity_curve[~equity_curve.index.duplicated(keep="last")]
    metrics = _metrics(trades_df, equity_curve, params, idx)
    return trades_df, equity_curve, metrics


def fmt_metrics(m: dict, tag: str = "") -> str:
    if m.get("n_trades", 0) == 0:
        return f"{tag:<40} NO TRADES"
    return (f"{tag:<40} trades {m['n_trades']:>4}  win {m['win_rate']*100:>4.0f}%  "
            f"ROI {m['roi']*100:>7.0f}%  PF {str(m['profit_factor']):>5}  "
            f"maxDD {m['max_dd']*100:>5.0f}%  Sharpe {m['sharpe']:>5}  "
            f"liq {m['liquidations']:>3}  $/{m['final_equity']:>8.0f}")
