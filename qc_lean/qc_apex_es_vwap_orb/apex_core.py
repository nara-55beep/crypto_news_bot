import math
from apex_signals import signal_for


def simulate_trade(g, entry_i, sig_i, side, spec, cfg, or_hi, or_lo):
    if entry_i >= len(g):
        return None, sig_i
    sig = g[sig_i]
    entry_bar = g[entry_i]
    tick = spec["tick"]
    point = spec["point"]
    slip = tick * spec.get("slip", 1.0)
    fee = spec.get("fee", 2.0)
    entry = entry_bar["open"] + side * slip

    if cfg.get("name") == "nr7_breakout":
        if sig.get("prev_high") is None or sig.get("prev_low") is None:
            return None, sig_i
        entry_i = sig_i
        entry_bar = sig
        entry = (sig["prev_high"] + tick) if side > 0 else (sig["prev_low"] - tick)
        entry += side * slip

    if cfg["stop_mode"] == "bar":
        stop = sig["low"] - 2 * tick if side > 0 else sig["high"] + 2 * tick
        stop_dist = abs(entry - stop)
        max_dist = cfg["stop_atr"] * sig["atr14"] * 1.6
        if stop_dist > max_dist:
            return None, sig_i
    elif cfg["stop_mode"] == "nr7":
        if sig.get("prev_high") is None or sig.get("prev_low") is None:
            return None, sig_i
        stop = (sig["prev_low"] - tick) if side > 0 else (sig["prev_high"] + tick)
    else:
        stop_dist = cfg["stop_atr"] * sig["atr14"]
        stop = entry - side * stop_dist

    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return None, sig_i

    risk_per_contract = stop_dist * point + slip * point + fee
    qty = int(min(cfg["max_contracts"], math.floor(cfg["risk_usd"] / risk_per_contract)))
    if qty < 1:
        return None, sig_i

    target_mode = cfg.get("target_mode", "fixed")
    if target_mode == "partial":
        return simulate_partial_trade(g, entry_i, entry_bar, side, qty, point, slip, fee, entry, stop, cfg["rr"] * stop_dist), entry_i

    target_enabled = target_mode == "fixed"
    trail_enabled = target_mode != "eod"
    target = entry + side * cfg["rr"] * stop_dist
    tp1 = entry + side * stop_dist
    stop_cur = stop
    hit_tp1 = False
    best = entry
    exit_px = entry
    exit_i = entry_i
    reason = "eod"

    for j in range(entry_i, len(g)):
        row = g[j]
        if row["minute"] > 15 * 60 + 55:
            break
        hi = row["high"]
        lo = row["low"]
        close = row["close"]
        if side > 0:
            best = max(best, hi)
            stopped = lo <= stop_cur
            target_hit = target_enabled and hi >= target
            tp1_hit = hi >= tp1
        else:
            best = min(best, lo)
            stopped = hi >= stop_cur
            target_hit = target_enabled and lo <= target
            tp1_hit = lo <= tp1

        if stopped:
            exit_px = stop_cur - side * slip
            reason = "stop" if not hit_tp1 else "trail"
            exit_i = j
            break

        if trail_enabled and (not hit_tp1) and tp1_hit:
            hit_tp1 = True
            stop_cur = entry + side * cfg.get("profit_lock_r", 0.0) * stop_dist

        if trail_enabled and hit_tp1:
            if side > 0:
                stop_cur = max(stop_cur, best - cfg["trail_r"] * stop_dist)
            else:
                stop_cur = min(stop_cur, best + cfg["trail_r"] * stop_dist)
            trail_hit = lo <= stop_cur if side > 0 else hi >= stop_cur
            if trail_hit:
                exit_px = stop_cur - side * slip
                reason = "trail"
                exit_i = j
                break

        if target_hit:
            exit_px = target - side * slip
            reason = "target"
            exit_i = j
            break

        exit_px = close
        exit_i = j

    if reason == "eod":
        exit_px = g[exit_i]["close"] - side * slip

    pnl = side * (exit_px - entry) * qty * point - qty * fee
    return {
        "day": entry_bar["day"],
        "side": side,
        "qty": qty,
        "entry": entry,
        "exit": exit_px,
        "pnl": pnl,
        "reason": reason,
    }, exit_i


def simulate_partial_trade(g, entry_i, entry_bar, side, qty, point, slip, fee, entry, stop, rr_dist):
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return None
    target = entry + side * rr_dist
    tp1 = entry + side * stop_dist
    stop_cur = stop
    remaining = qty
    take_qty = qty // 2
    partial_taken = False
    realized = 0.0
    exit_i = entry_i
    exit_px = entry
    reason = "eod"

    for j in range(entry_i, len(g)):
        row = g[j]
        if row["minute"] > 15 * 60 + 55:
            break
        hi = row["high"]
        lo = row["low"]
        close = row["close"]
        stopped = lo <= stop_cur if side > 0 else hi >= stop_cur
        target_hit = hi >= target if side > 0 else lo <= target
        tp1_hit = hi >= tp1 if side > 0 else lo <= tp1

        if stopped:
            exit_px = stop_cur - side * slip
            realized += side * (exit_px - entry) * remaining * point
            reason = "be" if partial_taken else "stop"
            exit_i = j
            remaining = 0
            break

        if target_hit:
            exit_px = target - side * slip
            realized += side * (exit_px - entry) * remaining * point
            reason = "target"
            exit_i = j
            remaining = 0
            break

        if (not partial_taken) and tp1_hit:
            partial_taken = True
            stop_cur = entry
            if take_qty > 0:
                exit_px = tp1 - side * slip
                realized += side * (exit_px - entry) * take_qty * point
                remaining -= take_qty

        exit_px = close
        exit_i = j

    if remaining > 0:
        exit_px = g[exit_i]["close"] - side * slip
        realized += side * (exit_px - entry) * remaining * point

    pnl = realized - qty * fee
    return {
        "day": entry_bar["day"],
        "side": side,
        "qty": qty,
        "entry": entry,
        "exit": exit_px,
        "pnl": pnl,
        "reason": reason,
    }


def summarize(equity, trades, max_dd, worst_day, first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion=2000.0):
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    pf = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
    win_rate = 100.0 * sum(1 for t in trades if t["pnl"] > 0) / len(trades) if trades else 0.0
    profit = equity - 50000.0
    active_days = sum(1 for pnl in daily.values() if abs(pnl) > 1e-9)
    best_day = max(daily.values()) if daily else 0.0
    best_trade = max([t["pnl"] for t in trades], default=0.0)
    best_day_share = max(0.0, best_day) / max(profit, 1.0) if profit > 0 else 0.0
    best_trade_share = max(0.0, best_trade) / max(profit, 1.0) if profit > 0 else 0.0
    return {
        "final": equity,
        "profit": profit,
        "trades": len(trades),
        "active_days": active_days,
        "win_rate": win_rate,
        "pf": pf,
        "max_dd": max_dd,
        "min_cushion": min_cushion,
        "worst_day": worst_day,
        "best_day": best_day,
        "best_trade": best_trade,
        "best_day_share": best_day_share,
        "best_trade_share": best_trade_share,
        "consistency_share": best_day_share,
        "consistency_ok": best_day_share <= 0.30,
        "expectancy": profit / len(trades) if trades else 0.0,
        "breached": first_breach,
        "eval_pass": eval_pass,
        "pass_day": pass_day,
        "breach_day": breach_day,
        "pass_profit": pass_profit,
        "breach_profit": breach_profit,
        "daily": daily,
    }
