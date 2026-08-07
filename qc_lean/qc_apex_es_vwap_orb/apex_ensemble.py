from datetime import timedelta
from apex_format import cfg_underlying
from apex_score import score_ensemble


def build_configs():
    cfgs = []
    strategies = [
        "orb_break",
        "orb_pullback",
        "orb_fade",
        "prior_sweep",
        "vwap_reclaim",
        "volume_orb_break",
        "volume_gap_go",
        "volume_vwap_drive",
        "peer_confirmed_break",
        "peer_divergence_fade",
        "daily_trend_orb",
        "daily_breakout_go",
        "daily_pullback_reclaim",
        "gap_fade",
        "exhaustion_gap_fade_q",
        "gap_go",
        "failed_or_reversal",
        "narrow_or_break",
        "prev_narrow_break",
        "prev_day_continuation",
        "inside_day_break",
        "wide_or_fade",
        "opening_drive_pullback",
        "range_expansion_continuation",
        "midday_vwap_trend",
        "first_hour_break",
        "vwap_failure",
        "late_day_momentum",
        "late_day_reversal",
        "first_mom_q",
        "first_fade_q",
        "two_mom_q",
        "two_fade_q",
        "prior_break_eod_q",
        "opening_impulse_momentum_q",
        "opening_impulse_fade_q",
        "quality_orb_continuation",
        "vwap_trend_pullback_q",
        "failed_break_peer_reversal_q",
        "compression_expansion_q",
        "volatility_compression_accept_q",
        "balanced_prior_breakout_q",
        "multi_speed_trend_pullback_q",
        "first_hour_retest_q",
        "second_entry_trend_q",
        "vvg_late_reversal_q",
    ]
    quality_strategies = [
        "quality_orb_continuation",
        "vwap_trend_pullback_q",
        "failed_break_peer_reversal_q",
        "compression_expansion_q",
        "volatility_compression_accept_q",
        "balanced_prior_breakout_q",
        "multi_speed_trend_pullback_q",
        "first_hour_retest_q",
        "second_entry_trend_q",
    ]
    breakout_like = [
        "orb_break",
        "orb_pullback",
        "narrow_or_break",
        "prev_narrow_break",
        "opening_drive_pullback",
        "gap_go",
        "volume_orb_break",
        "volume_gap_go",
        "volume_vwap_drive",
        "peer_confirmed_break",
        "daily_trend_orb",
        "daily_breakout_go",
        "daily_pullback_reclaim",
        "prev_day_continuation",
        "inside_day_break",
        "vwap_reclaim",
        "range_expansion_continuation",
        "midday_vwap_trend",
        "first_hour_break",
        "late_day_momentum",
        "quality_orb_continuation",
        "vwap_trend_pullback_q",
        "compression_expansion_q",
        "volatility_compression_accept_q",
        "balanced_prior_breakout_q",
        "multi_speed_trend_pullback_q",
        "first_hour_retest_q",
        "second_entry_trend_q",
    ]
    later_day = ["range_expansion_continuation", "midday_vwap_trend", "first_hour_break", "vwap_failure", "late_day_momentum", "late_day_reversal"]
    open_noise = ["orb_break", "volume_orb_break", "peer_confirmed_break", "narrow_or_break", "daily_trend_orb"]
    cooldown_names = ["orb_fade", "prior_sweep", "gap_fade", "exhaustion_gap_fade_q", "failed_or_reversal", "wide_or_fade", "vwap_failure", "late_day_reversal", "failed_break_peer_reversal_q"]
    trail_names = ["quality_orb_continuation", "vwap_trend_pullback_q", "compression_expansion_q", "volatility_compression_accept_q", "balanced_prior_breakout_q", "multi_speed_trend_pullback_q", "first_hour_retest_q", "second_entry_trend_q", "opening_drive_pullback", "range_expansion_continuation", "midday_vwap_trend", "late_day_momentum"]
    runner_names = ["quality_orb_continuation", "vwap_trend_pullback_q", "multi_speed_trend_pullback_q", "first_hour_retest_q", "second_entry_trend_q", "range_expansion_continuation", "midday_vwap_trend", "late_day_momentum"]
    quality_two_trade_names = ["multi_speed_trend_pullback_q", "first_hour_retest_q", "second_entry_trend_q"]
    direct_apex_names = ["first_mom_q", "first_fade_q", "two_mom_q", "two_fade_q", "prior_break_eod_q"]
    opening_impulse_names = ["opening_impulse_momentum_q", "opening_impulse_fade_q"]
    peer_only = ["peer_confirmed_break", "peer_divergence_fade"]
    peer_markets = ["ES", "NQ", "MES", "MNQ", "MYM", "M2K"]
    for market in ["ES", "NQ", "MES", "MNQ", "MYM", "M2K", "MCL", "MGC"]:
        max_contracts = 1 if market in ["ES", "NQ"] else 6
        filter_values = ["ema", "none"] if market in ["MCL", "MGC"] else ["ema_peer", "none"]
        for name in strategies:
            if name in peer_only and market not in peer_markets:
                continue
            if name in direct_apex_names:
                if market not in ["ES", "NQ"]:
                    continue
                for side in ["both", "long", "short"]:
                    for or_bars in ([3, 6, 12] if name != "prior_break_eod_q" else [3, 6]):
                        for stop_atr in [0.25, 0.40, 0.60]:
                            for rr in [2.0, 2.5, 3.0]:
                                for filt in ["none", "ema", "ema_peer"]:
                                    for risk_usd in [350.0, 600.0, 900.0]:
                                        for target_mode in (["eod", "fixed"] if name != "prior_break_eod_q" else ["eod", "runner"]):
                                            for min_impulse_atr in ([0.0, 0.10, 0.20] if name != "prior_break_eod_q" else [0.0]):
                                                for peer_mode in ["none", "confirm"]:
                                                    for daily_mode in ["none", "align"]:
                                                        cfgs.append({
                                                            "market": market,
                                                            "name": name,
                                                            "side": side,
                                                            "entry_start": 0,
                                                            "entry_end": 11 * 60 + 30,
                                                            "stop_atr": stop_atr,
                                                            "rr": rr,
                                                            "filter": filt,
                                                            "stop_mode": "atr",
                                                            "or_bars": or_bars,
                                                            "risk_usd": risk_usd,
                                                            "risk_profile": "apex_guard",
                                                            "event_filter": "macro_skip",
                                                            "max_contracts": max_contracts,
                                                            "max_trades_day": 1,
                                                            "second_trade_mode": "any",
                                                            "daily_profit_stop": 9999.0,
                                                            "daily_loss_stop": 1000.0,
                                                            "loss_cooldown": 0,
                                                            "cooldown_loss": 250.0,
                                                            "trail_r": 1.0,
                                                            "target_mode": target_mode,
                                                            "profit_lock_r": 0.0,
                                                            "sweep_atr": 0.10,
                                                            "regime": "all",
                                                            "min_impulse_atr": min_impulse_atr,
                                                            "peer_mode": peer_mode,
                                                            "daily_mode": daily_mode,
                                                        })
                continue
            if name == "vvg_late_reversal_q":
                regime_values = ["vvg_drive"]
            elif name in opening_impulse_names:
                regime_values = ["all", "small_gap", "drive_open"]
            elif name == "failed_break_peer_reversal_q":
                regime_values = ["large_gap", "drive_open", "vvg_drive"]
            elif name in quality_strategies:
                regime_values = ["all", "small_gap", "balanced_open", "drive_open", "vvg_drive"]
            else:
                regime_values = ["all", "small_gap"] if name in breakout_like else ["all", "large_gap"]
            if name == "vvg_late_reversal_q":
                entry_end_values = [15 * 60 + 30]
                entry_start_values = [14 * 60 + 15]
            elif name in opening_impulse_names:
                entry_end_values = [10 * 60 + 15, 10 * 60 + 30]
                entry_start_values = [0]
            elif name in quality_strategies:
                entry_end_values = [11 * 60 + 30, 14 * 60]
                entry_start_values = [0, 10 * 60]
            elif name in ["late_day_momentum", "late_day_reversal"]:
                entry_end_values = [15 * 60, 15 * 60 + 30]
                entry_start_values = [14 * 60 + 15]
            else:
                entry_end_values = [11 * 60 + 30, 12 * 60 + 30, 14 * 60] if name in later_day else [10 * 60 + 30, 11 * 60 + 30]
                entry_start_values = [10 * 60 + 30] if name in later_day else ([0, 9 * 60 + 50] if name in open_noise else [0])
            name_filters = ["none"] if name in peer_only else (["ema_peer", "none"] if name in opening_impulse_names and market not in ["MCL", "MGC"] else (["ema"] if name in opening_impulse_names else (["ema_peer"] if name in quality_strategies and market not in ["MCL", "MGC"] else (["ema"] if name in quality_strategies else filter_values))))
            side_values = ["both"] if name in quality_strategies or name in opening_impulse_names or name == "vvg_late_reversal_q" else ["both", "long", "short"]
            stop_atr_values = [0.40, 0.65] if name in opening_impulse_names else ([0.75, 1.05] if name in quality_strategies or name == "vvg_late_reversal_q" else [0.65, 1.0])
            rr_values = [2.0, 2.8] if name in opening_impulse_names else ([1.5, 2.0] if name == "vvg_late_reversal_q" else ([1.8, 2.4] if name in quality_strategies else [1.5, 2.2]))
            if name == "vvg_late_reversal_q":
                max_trades_values = [1]
            elif name in opening_impulse_names:
                max_trades_values = [1]
            elif name in quality_two_trade_names:
                max_trades_values = [1, 2]
            else:
                max_trades_values = [1] if name in quality_strategies else [1, 2]
            if name == "vvg_late_reversal_q":
                risk_values = [160.0, 220.0]
            elif name in opening_impulse_names:
                risk_values = [160.0, 220.0, 300.0]
            elif name in quality_strategies:
                risk_values = [180.0, 240.0, 300.0]
            elif name in cooldown_names:
                risk_values = [160.0, 220.0]
            else:
                risk_values = [220.0]
            trail_values = [0.75, 1.0, 1.35] if name in trail_names or name in opening_impulse_names else [1.0]
            target_modes = ["fixed", "runner"] if name in runner_names or name in opening_impulse_names else ["fixed"]
            for side in side_values:
                for entry_start in entry_start_values:
                    for entry_end in entry_end_values:
                        if entry_start >= entry_end:
                            continue
                        for stop_atr in stop_atr_values:
                            for rr in rr_values:
                                for filt in name_filters:
                                    for or_bars in [3, 6]:
                                        for max_trades_day in max_trades_values:
                                            pacing = [(9999.0, 1000.0)] if max_trades_day == 1 else [(450.0, 450.0), (700.0, 700.0), (9999.0, 1000.0)]
                                            cooldown_values = [0, 1] if name in cooldown_names and max_trades_day == 2 else [0]
                                            second_trade_mode = "after_win" if name in quality_two_trade_names and max_trades_day == 2 else "any"
                                            for risk_usd in risk_values:
                                                for trail_r in trail_values:
                                                    for target_mode in target_modes:
                                                        lock_values = [0.0, 0.35] if target_mode == "runner" else [0.0]
                                                        for profit_lock_r in lock_values:
                                                            for loss_cooldown in cooldown_values:
                                                                for profit_stop, loss_stop in pacing:
                                                                    for regime in regime_values:
                                                                        cfgs.append({
                                                                            "market": market,
                                                                            "name": name,
                                                                            "side": side,
                                                                            "entry_start": entry_start,
                                                                            "entry_end": entry_end,
                                                                            "stop_atr": stop_atr,
                                                                            "rr": rr,
                                                                            "filter": filt,
                                                                            "stop_mode": "atr",
                                                                            "or_bars": or_bars,
                                                                            "risk_usd": risk_usd,
                                                                            "risk_profile": "apex_guard",
                                                                            "event_filter": "macro_skip",
                                                                            "max_contracts": max_contracts,
                                                                            "max_trades_day": max_trades_day,
                                                                            "second_trade_mode": second_trade_mode,
                                                                            "daily_profit_stop": profit_stop,
                                                                            "daily_loss_stop": loss_stop,
                                                                            "loss_cooldown": loss_cooldown,
                                                                            "cooldown_loss": 250.0,
                                                                            "trail_r": trail_r,
                                                                            "target_mode": target_mode,
                                                                            "profit_lock_r": profit_lock_r,
                                                                            "sweep_atr": 0.10,
                                                                            "regime": regime,
                                                                        })
    return cfgs


def build_ensembles(evaluated, days, split):
    seeds = diverse_seeds(evaluated, 36)
    combos = []
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            members = [seeds[i], seeds[j]]
            if ensemble_members_ok(members):
                combos.append(evaluate_ensemble(members, days, split))

    top = seeds[:16]
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            for k in range(j + 1, len(top)):
                members = [top[i], top[j], top[k]]
                if ensemble_members_ok(members):
                    combos.append(evaluate_ensemble(members, days, split))

    combos.sort(key=lambda x: x[0], reverse=True)
    return combos[:80]


def diverse_seeds(evaluated, limit):
    seeds = []
    seen = set()
    for score, cfg, train, hold, full, roll in evaluated:
        if not ensemble_seed_ok(hold, full):
            continue
        key = (cfg_underlying(cfg), cfg["name"], cfg["side"], cfg.get("entry_start", 0), cfg["entry_end"], cfg["or_bars"], cfg["filter"], cfg.get("regime", "all"))
        if key in seen:
            continue
        seen.add(key)
        seeds.append({"score": score, "cfg": cfg, "train": train, "hold": hold, "full": full, "roll": roll})
        if len(seeds) >= limit:
            break
    return seeds


def ensemble_seed_ok(hold, full):
    if full["trades"] < 8:
        return False
    if full.get("active_days", full["trades"]) < 5:
        return False
    if full["profit"] <= -500.0:
        return False
    if hold["profit"] <= -500.0:
        return False
    if full.get("best_day_share", 0.0) > 0.65:
        return False
    if full.get("best_trade_share", 0.0) > 0.55:
        return False
    return True


def ensemble_members_ok(members):
    shapes = set()
    names = set()
    underlyings = set()
    for member in members:
        cfg = member["cfg"]
        shape = (cfg_underlying(cfg), cfg["name"], cfg["side"], cfg.get("regime", "all"))
        if shape in shapes:
            return False
        shapes.add(shape)
        names.add(cfg["name"])
        underlyings.add(cfg_underlying(cfg))
    return len(names) > 1 or len(underlyings) > 1


def evaluate_ensemble(members, days, split):
    daily = priority_daily(members, days)
    train = summarize_daily(daily, days, end_day=split)
    hold = summarize_daily(daily, days, start_day=split)
    full = summarize_daily(daily, days)
    attach_eval_stop(full, replay_apex_window(daily, days, days[0], days[-1]))
    stress_daily = priority_daily(members, days, "stress")
    stress = summarize_daily(stress_daily, days)
    attach_stress(full, stress)
    attach_path_stress(full, path_order_stress(full["daily"], days))
    roll = rolling_30d_from_daily(full["daily"], days)
    return (score_ensemble(train, hold, full, roll), members, train, hold, full, roll)


def priority_daily(members, days, source="full"):
    daily = {}
    for d in days:
        for member in members:
            src = member["full"].get(source, member["full"])
            pnl = src["daily"].get(d, 0.0)
            if abs(pnl) > 1e-9:
                daily[d] = pnl
                break
        else:
            daily[d] = 0.0
    return daily


def attach_stress(full, stress):
    full["stress"] = stress
    full["stress_profit"] = stress["profit"]
    full["stress_breached"] = stress["breached"]
    full["stress_eval_pass"] = stress["eval_pass"]
    full["stress_pf"] = stress["pf"]


def attach_eval_stop(full, eval_stop):
    full["eval_stop"] = eval_stop
    full["eval_stop_pass"] = eval_stop["eval_pass"]
    full["eval_stop_breached"] = eval_stop["breached"]
    full["eval_stop_profit"] = eval_stop["profit"]
    full["eval_stop_cushion"] = eval_stop.get("min_cushion", 2000.0)
    full["eval_stop_trades"] = eval_stop.get("trades", 0)
    full["eval_stop_active_days"] = eval_stop.get("active_days", full["eval_stop_trades"])
    full["eval_stop_best_day"] = eval_stop.get("best_day", 0.0)
    full["eval_stop_best_trade"] = eval_stop.get("best_trade", 0.0)
    full["eval_stop_consistency_share"] = eval_stop.get("consistency_share", eval_stop.get("best_day_share", 0.0))
    full["eval_stop_best_trade_share"] = eval_stop.get("best_trade_share", 0.0)
    full["eval_stop_day"] = eval_stop["pass_day"] if eval_stop["eval_pass"] else eval_stop["breach_day"]


def attach_path_stress(full, path_stress):
    full["path_stress"] = path_stress
    full["path_profit"] = path_stress["profit"]
    full["path_breached"] = path_stress["breached"]
    full["path_eval_pass"] = path_stress["eval_pass"]
    full["path_max_dd"] = path_stress["max_dd"]
    full["path_cluster"] = path_stress["clustered_losses"]


def summarize_daily(daily, days, start_day=None, end_day=None):
    equity = 50000.0
    peak = 50000.0
    threshold = 48000.0
    max_dd = 0.0
    worst_day = 0.0
    trades = 0
    wins = 0.0
    losses = 0.0
    win_count = 0
    first_breach = False
    eval_pass = False
    pass_day = None
    breach_day = None
    pass_profit = None
    breach_profit = None
    used_daily = {}
    min_cushion = 2000.0

    for d in days:
        if start_day is not None and d < start_day:
            continue
        if end_day is not None and d > end_day:
            continue
        pnl = daily.get(d, 0.0)
        used_daily[d] = pnl
        if abs(pnl) > 1e-9:
            trades += 1
            if pnl > 0:
                wins += pnl
                win_count += 1
            else:
                losses += -pnl
        equity += pnl
        min_cushion = min(min_cushion, equity - threshold)
        max_dd = min(max_dd, equity - peak)
        peak = max(peak, equity)
        worst_day = min(worst_day, pnl)

        # Apex DLL pauses trading; the EOD trailing threshold is the eval breach.
        if (not first_breach) and equity <= threshold:
            first_breach = True
            breach_day = d
            breach_profit = equity - 50000.0
        if (not first_breach) and (not eval_pass) and equity >= 53000.0:
            eval_pass = True
            pass_day = d
            pass_profit = equity - 50000.0
        threshold = max(threshold, peak - 2000.0)

    pf = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
    win_rate = 100.0 * win_count / trades if trades else 0.0
    profit = equity - 50000.0
    active_days = sum(1 for pnl in used_daily.values() if abs(pnl) > 1e-9)
    best_day = max(used_daily.values()) if used_daily else 0.0
    best_day_share = max(0.0, best_day) / max(profit, 1.0) if profit > 0 else 0.0
    return {
        "final": equity,
        "profit": profit,
        "trades": trades,
        "active_days": active_days,
        "win_rate": win_rate,
        "pf": pf,
        "max_dd": max_dd,
        "min_cushion": min_cushion,
        "worst_day": worst_day,
        "best_day": best_day,
        "best_trade": best_day,
        "best_day_share": best_day_share,
        "best_trade_share": best_day_share,
        "consistency_share": best_day_share,
        "consistency_ok": best_day_share <= 0.30,
        "expectancy": profit / trades if trades else 0.0,
        "breached": first_breach,
        "eval_pass": eval_pass,
        "pass_day": pass_day,
        "breach_day": breach_day,
        "pass_profit": pass_profit,
        "breach_profit": breach_profit,
        "daily": used_daily,
    }


def rolling_30d_from_daily(daily, days):
    total = 0
    passed = 0
    breached = 0
    profits = []
    worst_dd = 0.0
    worst_day = 0.0
    min_cushion = 2000.0
    pass_buckets = {}
    q = quarter_stats(daily, days)
    mo = month_stats(daily, days)
    wf = walk_forward_stats(daily, days)
    for idx, start in enumerate(days):
        end = start + timedelta(days=30)
        if end > days[-1]:
            continue
        m = replay_apex_window(daily, days, start, end)
        total += 1
        if m["eval_pass"]:
            passed += 1
            bucket = min(3, int(idx * 4 / max(len(days), 1)))
            pass_buckets[bucket] = pass_buckets.get(bucket, 0) + 1
        breached += 1 if m["breached"] and not m["eval_pass"] else 0
        profits.append(m["profit"])
        worst_dd = min(worst_dd, m["max_dd"])
        worst_day = min(worst_day, m["worst_day"])
        min_cushion = min(min_cushion, m.get("min_cushion", 2000.0))
    return {
        "total": total,
        "passed": passed,
        "breached": breached,
        "pass_rate": passed / total if total else 0.0,
        "breach_rate": breached / total if total else 0.0,
        "pass_bucket_pos": len(pass_buckets),
        "pass_bucket_max_share": max(pass_buckets.values()) / passed if passed else 0.0,
        "min_profit": min(profits) if profits else 0.0,
        "max_profit": max(profits) if profits else 0.0,
        "avg_profit": sum(profits) / len(profits) if profits else 0.0,
        "worst_dd": worst_dd,
        "worst_day": worst_day,
        "min_cushion": min_cushion,
        "q_pos": q["pos"],
        "q_min": q["min"],
        "q_avg": q["avg"],
        "m_total": mo["total"],
        "m_pos": mo["pos"],
        "m_min": mo["min"],
        "m_avg": mo["avg"],
        "wf_total": wf["total"],
        "wf_pos": wf["pos"],
        "wf_min": wf["min"],
        "wf_avg": wf["avg"],
        "wf_breached": wf["breached"],
    }


def quarter_stats(daily, days):
    if not days:
        return {"pos": 0, "min": 0.0, "avg": 0.0}
    step = max(1, len(days) // 4)
    parts = [days[:step], days[step:2 * step], days[2 * step:3 * step], days[3 * step:]]
    pnls = [sum(daily.get(d, 0.0) for d in part) for part in parts if part]
    return {
        "pos": sum(1 for pnl in pnls if pnl > 0),
        "min": min(pnls) if pnls else 0.0,
        "avg": sum(pnls) / len(pnls) if pnls else 0.0,
    }


def month_key(d, idx):
    if hasattr(d, "year") and hasattr(d, "month"):
        return (d.year, d.month)
    return idx // 21


def month_stats(daily, days):
    buckets = {}
    for idx, d in enumerate(days):
        buckets.setdefault(month_key(d, idx), 0.0)
        buckets[month_key(d, idx)] += daily.get(d, 0.0)
    pnls = list(buckets.values())
    return {
        "total": len(pnls),
        "pos": sum(1 for pnl in pnls if pnl > 0),
        "min": min(pnls) if pnls else 0.0,
        "avg": sum(pnls) / len(pnls) if pnls else 0.0,
    }


def walk_forward_stats(daily, days):
    if not days:
        return {"total": 0, "pos": 0, "min": 0.0, "avg": 0.0, "breached": 0}
    step = max(1, len(days) // 5)
    chunks = [days[i:i + step] for i in range(0, len(days), step)]
    if len(chunks) > 5:
        chunks = chunks[:4] + [sum(chunks[4:], [])]
    profits = []
    breached = 0
    for chunk in chunks:
        if not chunk:
            continue
        m = replay_apex_window(daily, chunk, chunk[0], chunk[-1])
        profits.append(m["profit"])
        breached += 1 if m["breached"] and not m["eval_pass"] else 0
    return {
        "total": len(profits),
        "pos": sum(1 for pnl in profits if pnl > 0),
        "min": min(profits) if profits else 0.0,
        "avg": sum(profits) / len(profits) if profits else 0.0,
        "breached": breached,
    }


def path_order_stress(daily, days, cluster=3):
    ordered = [(d, daily.get(d, 0.0)) for d in days]
    worst = sorted([x for x in ordered if x[1] < 0.0], key=lambda x: x[1])[:cluster]
    worst_days = set(d for d, _ in worst)
    stressed = worst + [x for x in ordered if x[0] not in worst_days]
    stress_days = list(range(len(stressed)))
    stress_daily = {i: pnl for i, (_, pnl) in enumerate(stressed)}
    end = stress_days[-1] if stress_days else -1
    result = replay_apex_window(stress_daily, stress_days, 0, end)
    result["clustered_losses"] = len(worst)
    return result


def replay_apex_window(daily, days, start, end):
    equity = 50000.0
    peak = 50000.0
    threshold = 48000.0
    breached = False
    eval_pass = False
    pass_day = None
    breach_day = None
    pass_profit = None
    breach_profit = None
    max_dd = 0.0
    worst_day = 0.0
    min_cushion = 2000.0
    trades = 0
    used_daily = {}
    for d in days:
        if d < start or d > end:
            continue
        pnl = daily.get(d, 0.0)
        used_daily[d] = pnl
        if abs(pnl) > 1e-9:
            trades += 1
        equity += pnl
        min_cushion = min(min_cushion, equity - threshold)
        max_dd = min(max_dd, equity - peak)
        worst_day = min(worst_day, pnl)
        # Apex DLL pauses trading; the EOD trailing threshold is the eval breach.
        if equity <= threshold:
            breached = True
            breach_day = d
            breach_profit = equity - 50000.0
            break
        if equity >= 53000.0:
            eval_pass = True
            pass_day = d
            pass_profit = equity - 50000.0
            break
        peak = max(peak, equity)
        threshold = max(threshold, peak - 2000.0)
    profit = equity - 50000.0
    best_day = max(used_daily.values()) if used_daily else 0.0
    best_day_share = max(0.0, best_day) / max(profit, 1.0) if profit > 0.0 else 0.0
    return {
        "profit": profit,
        "eval_pass": eval_pass,
        "breached": breached,
        "pass_day": pass_day,
        "breach_day": breach_day,
        "pass_profit": pass_profit,
        "breach_profit": breach_profit,
        "max_dd": max_dd,
        "min_cushion": min_cushion,
        "trades": trades,
        "active_days": trades,
        "worst_day": worst_day,
        "best_day": best_day,
        "best_trade": best_day,
        "best_day_share": best_day_share,
        "best_trade_share": best_day_share,
        "consistency_share": best_day_share,
    }
