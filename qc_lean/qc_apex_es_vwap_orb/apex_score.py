def score_ensemble(train, hold, full, roll):
    eval_stop_pass = clean_eval_stop_pass(full)
    pass_bonus = 30000.0 if eval_stop_pass else (15000.0 if full["eval_pass"] else 0.0)
    hold_bonus = 9000.0 if hold["eval_pass"] else 0.0
    roll_rate = roll["pass_rate"]
    consistency_share = full.get("consistency_share", full.get("best_day_share", 0.0))
    breach_penalty = (5500.0 if eval_stop_pass else 11000.0) if full["breached"] else 0.0
    eval_breach_penalty = 22000.0 if full.get("eval_stop_breached", False) else 0.0
    eval_profit_bonus = min(max(full.get("eval_stop_profit", 0.0), 0.0), 3500.0) * (1.1 if eval_stop_pass else 0.0)
    rolling_breach_penalty = 26000.0 * roll["breach_rate"]
    quarter_penalty = max(0, 3 - roll["q_pos"]) * 5200.0 + max(0.0, -roll["q_min"]) * 0.7
    month_penalty = max(0, min(5, roll["m_total"]) - roll["m_pos"]) * 1800.0 + max(0.0, -1500.0 - roll["m_min"]) * 1.6
    daily_loss_penalty = max(0.0, -1000.0 - full["worst_day"]) * 8.0
    low_trade_penalty = max(0, 20 - full["trades"]) * 240.0 + max(0, 6 - hold["trades"]) * 420.0
    concentration = win_concentration_penalty(full)
    hold_loss_penalty = max(0.0, -hold["profit"]) * 3.0
    hold_quality_penalty = hold_quality_penalty_value(hold, 1.05, -1900.0, -900.0, 3600.0)
    active_penalty = max(0, 14 - full.get("active_days", full["trades"])) * 500.0
    consistency_penalty = max(0.0, consistency_share - 0.30) * 14000.0 + max(0.0, consistency_share - 0.40) * 18000.0
    stress_penalty = max(0.0, -full.get("stress_profit", 0.0)) * 4.0 + (9000.0 if full.get("stress_breached", False) else 0.0)
    path_penalty = path_penalty_value(full, 6500.0, 2.8)
    neighbor_penalty = neighbor_penalty_value(full)
    wf_penalty = walk_forward_penalty_value(roll, 2100.0, 1.4)
    roll_distribution_penalty = rolling_distribution_penalty(roll, 2, 0.75, 3200.0)
    cushion_penalty = cushion_penalty_value(full, roll, 350.0, 10.0)
    pass_quality_penalty = pass_quality_penalty_value(full, 350.0, 9.0, 6)
    score_profit = scored_profit_value(full)
    return (
        pass_bonus
        + hold_bonus
        + 42000.0 * roll_rate
        + score_profit
        + eval_profit_bonus
        + 1.5 * hold["profit"]
        + 160.0 * min(full["pf"], 6.0)
        + 1.2 * full["max_dd"]
        + 7.0 * min(full["trades"], 90)
        - breach_penalty
        - eval_breach_penalty
        - rolling_breach_penalty
        - quarter_penalty
        - month_penalty
        - daily_loss_penalty
        - low_trade_penalty
        - concentration
        - hold_loss_penalty
        - hold_quality_penalty
        - active_penalty
        - consistency_penalty
        - stress_penalty
        - path_penalty
        - neighbor_penalty
        - wf_penalty
        - roll_distribution_penalty
        - cushion_penalty
        - pass_quality_penalty
    )


def score_train(m):
    pass_bonus = 8000.0 if m["eval_pass"] else 0.0
    active = m.get("active_days", m["trades"])
    progress = min(max(m["profit"], 0.0), 3000.0)
    periods = train_period_stats(m.get("daily", {}))
    split = train_split_stats(m.get("daily", {}))
    concentration = max(0.0, m.get("best_day_share", 0.0) - 0.45) * 7000.0
    daily_loss = max(0.0, -750.0 - m["worst_day"]) * 4.0
    dd_loss = max(0.0, -1800.0 - m["max_dd"]) * 1.6
    sparse = max(0, 8 - active) * 180.0
    split_bonus = 450.0 if split["first"] > 0.0 and split["second"] > 0.0 else 0.0
    split_loss = max(0.0, -650.0 - split["min"]) * 1.2
    split_sparse = max(0, 2 - split["min_active"]) * 350.0
    if periods["active"]:
        period_bonus = 60.0 * min(periods["active"], 8) + 180.0 * min(periods["pos"], 6)
        period_concentration = max(0.0, periods["best_share"] - 0.45) * 5200.0
        period_loss = max(0.0, -650.0 - periods["min"]) * 1.4
        period_sparse = max(0, 4 - periods["active"]) * 450.0 + max(0, 3 - periods["pos"]) * 700.0
    else:
        period_bonus = 0.0
        period_concentration = 0.0
        period_loss = 0.0
        period_sparse = 0.0
    return pass_bonus + 0.75 * m["profit"] + 0.85 * progress + 140.0 * min(m["pf"], 5.0) + 12.0 * min(m["trades"], 80) + 70.0 * min(active, 35) + period_bonus + split_bonus + 0.9 * m["max_dd"] + 1.0 * m["worst_day"] - concentration - daily_loss - dd_loss - sparse - split_loss - split_sparse - period_concentration - period_loss - period_sparse


def train_candidate_ok(m):
    active = m.get("active_days", m["trades"])
    best_day_share = m.get("best_day_share", 0.0)
    best_trade_share = m.get("best_trade_share", 0.0)
    periods = train_period_stats(m.get("daily", {}))
    split = train_split_stats(m.get("daily", {}))
    if m["trades"] < 10:
        return False
    if active < 6:
        return False
    if m["profit"] <= 100.0:
        return False
    if periods["active"] < 3 or periods["pos"] < 2:
        return False
    if periods["best_share"] > 0.75:
        return False
    if periods["min"] <= -1200.0:
        return False
    if split["min_active"] == 0:
        return False
    if split["min"] <= -1200.0:
        return False
    if m["profit"] > 0 and split["best_share"] > 0.95:
        return False
    if m["eval_pass"]:
        if best_day_share > 0.65 or best_trade_share > 0.55:
            return False
        return True
    if m["trades"] < 14:
        return False
    if active < 8:
        return False
    if m["worst_day"] <= -1000.0:
        return False
    if m["max_dd"] <= -2600.0:
        return False
    if m["profit"] <= -1200.0:
        return False
    if m["pf"] < 0.85:
        return False
    if m["profit"] > 0 and best_day_share > 0.60:
        return False
    if m["profit"] > 0 and best_trade_share > 0.45:
        return False
    return True


def train_split_stats(daily):
    days = sorted(daily)
    if not days:
        return {"first": 0.0, "second": 0.0, "min": 0.0, "min_active": 0, "best_share": 0.0}
    mid = max(1, len(days) // 2)
    first_days = days[:mid]
    second_days = days[mid:]
    first = sum(daily.get(d, 0.0) for d in first_days)
    second = sum(daily.get(d, 0.0) for d in second_days)
    first_active = sum(1 for d in first_days if abs(daily.get(d, 0.0)) > 1e-9)
    second_active = sum(1 for d in second_days if abs(daily.get(d, 0.0)) > 1e-9)
    profit = first + second
    best = max(first, second)
    return {
        "first": first,
        "second": second,
        "min": min(first, second),
        "min_active": min(first_active, second_active),
        "best_share": max(0.0, best) / max(profit, 1.0) if profit > 0.0 else 0.0,
    }


def train_period_stats(daily):
    buckets = {}
    for day, pnl in daily.items():
        if abs(pnl) <= 1e-9:
            continue
        if hasattr(day, "year") and hasattr(day, "month"):
            key = (day.year, day.month)
        elif isinstance(day, int):
            key = day // 22
        else:
            key = day
        buckets[key] = buckets.get(key, 0.0) + pnl
    values = list(buckets.values())
    profit = sum(values)
    best = max(values) if values else 0.0
    return {
        "active": len(values),
        "pos": sum(1 for v in values if v > 0.0),
        "min": min(values) if values else 0.0,
        "best_share": max(0.0, best) / max(profit, 1.0) if profit > 0.0 else 0.0,
    }


def score_final(train, hold, full, roll):
    eval_stop_pass = clean_eval_stop_pass(full)
    pass_bonus = 26000.0 if eval_stop_pass else (12000.0 if full["eval_pass"] else 0.0)
    hold_bonus = 8000.0 if hold["eval_pass"] else 0.0
    roll_rate = roll["pass_rate"]
    consistency_share = full.get("consistency_share", full.get("best_day_share", 0.0))
    best_day = max(full["daily"].values()) if full["daily"] else 0.0
    concentration = max(0.0, best_day / max(full["profit"], 1.0) - 0.55) * 7000.0 if full["profit"] > 0 else 0.0
    breach_penalty = (4000.0 if eval_stop_pass else 8000.0) if full["breached"] else 0.0
    eval_breach_penalty = 18000.0 if full.get("eval_stop_breached", False) else 0.0
    eval_profit_bonus = min(max(full.get("eval_stop_profit", 0.0), 0.0), 3500.0) * (1.0 if eval_stop_pass else 0.0)
    rolling_breach_penalty = 22000.0 * roll["breach_rate"]
    quarter_penalty = max(0, 3 - roll["q_pos"]) * 4200.0 + max(0.0, -roll["q_min"]) * 0.55
    month_penalty = max(0, min(5, roll["m_total"]) - roll["m_pos"]) * 1500.0 + max(0.0, -1500.0 - roll["m_min"]) * 1.3
    low_trade_penalty = max(0, 18 - full["trades"]) * 220.0 + max(0, 5 - hold["trades"]) * 350.0
    daily_loss_penalty = max(0.0, -950.0 - full["worst_day"]) * 5.0
    hold_loss_penalty = max(0.0, -hold["profit"]) * 3.0
    hold_quality_penalty = hold_quality_penalty_value(hold, 1.05, -1900.0, -900.0, 3000.0)
    active_penalty = max(0, 12 - full.get("active_days", full["trades"])) * 480.0
    lucky_trade_penalty = max(0.0, full.get("best_trade_share", 0.0) - 0.32) * 9500.0
    consistency_penalty = max(0.0, consistency_share - 0.30) * 12000.0 + max(0.0, consistency_share - 0.40) * 16000.0
    stress_penalty = max(0.0, -full.get("stress_profit", 0.0)) * 4.0 + (8000.0 if full.get("stress_breached", False) else 0.0)
    path_penalty = path_penalty_value(full, 5500.0, 2.4)
    neighbor_penalty = neighbor_penalty_value(full)
    wf_penalty = walk_forward_penalty_value(roll, 1800.0, 1.2)
    roll_distribution_penalty = rolling_distribution_penalty(roll, 2, 0.80, 2600.0)
    cushion_penalty = cushion_penalty_value(full, roll, 300.0, 8.0)
    pass_quality_penalty = pass_quality_penalty_value(full, 300.0, 7.0, 5)
    score_profit = scored_profit_value(full)
    return pass_bonus + hold_bonus + 36000.0 * roll_rate + score_profit + eval_profit_bonus + 1.5 * hold["profit"] + 140.0 * min(full["pf"], 6.0) + 1.2 * full["max_dd"] + 8.0 * min(full["trades"], 90) - breach_penalty - eval_breach_penalty - rolling_breach_penalty - quarter_penalty - month_penalty - low_trade_penalty - daily_loss_penalty - concentration - hold_loss_penalty - hold_quality_penalty - active_penalty - lucky_trade_penalty - consistency_penalty - stress_penalty - path_penalty - neighbor_penalty - wf_penalty - roll_distribution_penalty - cushion_penalty - pass_quality_penalty


def scored_profit_value(full):
    if clean_eval_stop_pass(full):
        return min(max(full.get("eval_stop_profit", 0.0), 0.0), 3600.0)
    return full["profit"]


def clean_eval_stop_pass(full):
    return full.get("eval_stop_pass", full["eval_pass"]) and not full.get("eval_stop_breached", False)


def win_concentration_penalty(m):
    if m["profit"] <= 0 or not m["daily"]:
        return 0.0
    best_day = max(m["daily"].values())
    share = max(0.0, best_day) / max(m["profit"], 1.0)
    return max(0.0, share - 0.55) * 8000.0


def neighbor_penalty_value(full):
    total = full.get("neighbor_total", 0)
    if total <= 0:
        return 0.0
    required = max(2, total // 2)
    weak = max(0, required - full.get("neighbor_profitable", 0)) * 2800.0
    breach = full.get("neighbor_breached", 0) * 1800.0
    loss = max(0.0, -full.get("neighbor_min_profit", 0.0)) * 1.2
    return weak + breach + loss


def hold_quality_penalty_value(hold, pf_floor, dd_floor, day_floor, pf_weight):
    pf_penalty = max(0.0, pf_floor - hold.get("pf", pf_floor)) * pf_weight
    dd_penalty = max(0.0, dd_floor - hold.get("max_dd", 0.0)) * 1.2
    day_penalty = max(0.0, day_floor - hold.get("worst_day", 0.0)) * 2.0
    return pf_penalty + dd_penalty + day_penalty


def cushion_penalty_value(full, roll, floor, weight):
    full_penalty = max(0.0, floor - full.get("min_cushion", 2000.0)) * weight
    roll_penalty = max(0.0, floor * 0.50 - roll.get("min_cushion", 2000.0)) * (weight * 0.6)
    return full_penalty + roll_penalty


def pass_quality_penalty_value(full, cushion_floor, cushion_weight, min_trades):
    if not full.get("eval_stop_pass", full.get("eval_pass", False)):
        return 0.0
    cushion_penalty = max(0.0, cushion_floor - full.get("eval_stop_cushion", 2000.0)) * cushion_weight
    trade_penalty = max(0, min_trades - full.get("eval_stop_trades", full.get("trades", 0))) * 500.0
    day_penalty = max(0, min_eval_active_days(min_trades) - full.get("eval_stop_active_days", full.get("active_days", 0))) * 650.0
    pass_day_penalty = max(0.0, full.get("eval_stop_consistency_share", 0.0) - 0.45) * 9000.0
    pass_trade_penalty = max(0.0, full.get("eval_stop_best_trade_share", 0.0) - 0.38) * 11000.0
    return cushion_penalty + trade_penalty + day_penalty + pass_day_penalty + pass_trade_penalty


def min_eval_active_days(min_trades):
    return max(4, min_trades // 12)


def path_penalty_value(full, breach_weight, loss_weight):
    if "path_profit" not in full:
        return 0.0
    breach = breach_weight if full.get("path_breached", False) else 0.0
    loss = max(0.0, -full.get("path_profit", 0.0)) * loss_weight
    dd = max(0.0, -1900.0 - full.get("path_max_dd", 0.0)) * 1.4
    return breach + loss + dd


def walk_forward_penalty_value(roll, pos_weight, loss_weight):
    total = roll.get("wf_total", 0)
    if total <= 0:
        return 0.0
    need = max(3, total - 2)
    weak = max(0, need - roll.get("wf_pos", 0)) * pos_weight
    breach = roll.get("wf_breached", 0) * pos_weight
    loss = max(0.0, -1600.0 - roll.get("wf_min", 0.0)) * loss_weight
    return weak + breach + loss


def rolling_distribution_penalty(roll, min_buckets, max_share, weight):
    if roll.get("passed", 0) <= 0:
        return 0.0
    bucket_penalty = max(0, min_buckets - roll.get("pass_bucket_pos", 0)) * weight
    cluster_penalty = max(0.0, roll.get("pass_bucket_max_share", 0.0) - max_share) * weight
    return bucket_penalty + cluster_penalty


def min_rolling_passes(roll, floor, rate):
    total = roll.get("total", 0)
    return max(floor, int(total * rate + 0.999999))


def robust_ok(hold, full, roll, min_trades):
    return robust_reason(hold, full, roll, min_trades) == "ok"


def apex_ready_ok(hold, full, roll, min_trades):
    return apex_ready_reason(hold, full, roll, min_trades) == "ok"


def apex_ready_reason(hold, full, roll, min_trades):
    if full.get("eval_stop_breached", False):
        return "eval_breach"
    if not full.get("eval_stop_pass", full.get("eval_pass", False)):
        return "no_target"
    if full.get("eval_stop_cushion", 2000.0) < 250.0:
        return "thin_pass_cushion"
    if full.get("eval_stop_trades", full.get("trades", 0)) < max(5, min_trades // 10):
        return "fast_lucky_pass"
    if full.get("eval_stop_active_days", full.get("active_days", 0)) < min_eval_active_days(min_trades):
        return "fast_lucky_days"
    if full.get("eval_stop_consistency_share", full.get("consistency_share", 0.0)) > 0.50:
        return "lucky_pass_day"
    if full.get("eval_stop_best_trade_share", full.get("best_trade_share", 0.0)) > 0.42:
        return "lucky_pass_trade"
    if full["trades"] < min_trades:
        return "low_trades"
    if full.get("active_days", full["trades"]) < max(12, min_trades // 2):
        return "low_active_days"
    if hold["trades"] < max(6, min_trades // 4):
        return "low_hold_trades"
    if hold["profit"] <= 0.0:
        return "bad_hold"
    if hold.get("pf", 1.05) < 1.05:
        return "weak_hold_pf"
    if hold.get("worst_day", 0.0) <= -900.0:
        return "bad_hold_day"
    if hold.get("max_dd", 0.0) <= -1900.0:
        return "bad_hold_dd"
    if full.get("min_cushion", 2000.0) < 250.0:
        return "thin_cushion"
    if roll.get("min_cushion", 2000.0) < 150.0:
        return "thin_roll_cushion"
    if full["pf"] < 1.15:
        return "low_pf"
    if full["worst_day"] <= -1000.0:
        return "bad_day"
    if full.get("consistency_share", full.get("best_day_share", 0.0)) > 0.40:
        return "bad_consistency"
    if full.get("best_trade_share", 0.0) > 0.35:
        return "lucky_trade"
    if full.get("stress_breached", False):
        return "stress_breach"
    if full.get("stress_profit", 1.0) <= -500.0:
        return "stress_loss"
    if full.get("path_breached", False) and full.get("path_profit", 0.0) <= -1800.0:
        return "path_breach"
    if full.get("path_profit", 0.0) <= -2200.0:
        return "path_loss"
    if full.get("neighbor_total", 0) > 0:
        if full.get("neighbor_profitable", 0) < max(2, full["neighbor_total"] // 2):
            return "param_fragile"
        if full.get("neighbor_breached", 0) > full["neighbor_total"] // 2:
            return "param_breach"
    if roll["passed"] < min_rolling_passes(roll, 3, 0.02):
        return "no_roll_pass"
    if roll.get("total", 0) >= 80 and roll.get("pass_bucket_pos", 0) < 2:
        return "clustered_roll_pass"
    if roll.get("pass_bucket_max_share", 0.0) > 0.85:
        return "clustered_roll_pass"
    if roll.get("avg_profit", 0.0) <= -100.0:
        return "bad_roll_avg"
    if roll["breach_rate"] > 0.30:
        return "rolling_breach"
    if roll.get("m_total", 0) >= 6 and roll.get("m_pos", 0) < 4:
        return "bad_months"
    if roll.get("wf_total", 0) >= 4 and roll.get("wf_pos", 0) < 3:
        return "bad_walkforward"
    if roll.get("wf_breached", 0) > 1:
        return "walkforward_breach"
    return "ok"


def robust_reason(hold, full, roll, min_trades):
    if full.get("eval_stop_breached", False):
        return "eval_breach"
    if not clean_eval_stop_pass(full):
        return "no_target"
    if full["profit"] <= 0:
        return "neg_full"
    if hold["profit"] <= 0:
        return "neg_hold"
    if full["breached"]:
        return "full_breach"
    if hold["breached"]:
        return "hold_breach"
    if full["trades"] < min_trades:
        return "low_trades"
    if full.get("active_days", full["trades"]) < max(12, min_trades // 2):
        return "low_active_days"
    if hold["trades"] < max(6, min_trades // 4):
        return "low_hold_trades"
    if hold.get("pf", 1.10) < 1.10:
        return "weak_hold_pf"
    if hold.get("worst_day", 0.0) <= -850.0:
        return "bad_hold_day"
    if hold.get("max_dd", 0.0) <= -1800.0:
        return "bad_hold_dd"
    if full.get("min_cushion", 2000.0) < 350.0:
        return "thin_cushion"
    if roll.get("min_cushion", 2000.0) < 250.0:
        return "thin_roll_cushion"
    if full["pf"] < 1.20:
        return "low_pf"
    if full["worst_day"] <= -1000.0:
        return "bad_day"
    if full.get("consistency_share", full.get("best_day_share", 0.0)) > 0.40:
        return "bad_consistency"
    if full.get("best_day_share", 0.0) > 0.45:
        return "lucky_day"
    if full.get("best_trade_share", 0.0) > 0.35:
        return "lucky_trade"
    if full.get("stress_breached", False):
        return "stress_breach"
    if full.get("stress_profit", 1.0) <= 0:
        return "stress_loss"
    if full.get("stress_pf", 1.2) < 1.05:
        return "stress_low_pf"
    if full.get("path_breached", False) and full.get("path_profit", 0.0) <= -1800.0:
        return "path_breach"
    if full.get("path_profit", 0.0) <= -2200.0:
        return "path_loss"
    if full.get("neighbor_total", 0) > 0:
        if full.get("neighbor_profitable", 0) < max(2, full["neighbor_total"] // 2):
            return "param_fragile"
        if full.get("neighbor_breached", 0) > full["neighbor_total"] // 2:
            return "param_breach"
        if full.get("neighbor_min_profit", 0.0) <= -2000.0:
            return "param_tail_loss"
    if roll["passed"] < min_rolling_passes(roll, 4, 0.025):
        return "no_roll_pass"
    if roll.get("total", 0) >= 80 and roll.get("pass_bucket_pos", 0) < 2:
        return "clustered_roll_pass"
    if roll.get("pass_bucket_max_share", 0.0) > 0.80:
        return "clustered_roll_pass"
    if roll["avg_profit"] <= 0:
        return "neg_roll_avg"
    if roll["breach_rate"] > 0.25:
        return "rolling_breach"
    if roll["q_pos"] < 3:
        return "bad_quarters"
    if roll["q_min"] <= -1500.0:
        return "bad_quarter_loss"
    if roll.get("m_total", 0) >= 6 and roll.get("m_pos", 0) < 4:
        return "bad_months"
    if roll.get("m_min", 0.0) <= -1600.0:
        return "bad_month_loss"
    if roll.get("wf_total", 0) >= 4 and roll.get("wf_pos", 0) < 3:
        return "bad_walkforward"
    if roll.get("wf_breached", 0) > 1:
        return "walkforward_breach"
    if roll.get("wf_min", 0.0) <= -1900.0:
        return "walkforward_tail_loss"
    return "ok"


def format_reason_summary(tag, items, min_trades, reason_func=None):
    reason_func = robust_reason if reason_func is None else reason_func
    counts = {}
    for item in items:
        reason = reason_func(item[3], item[4], item[5], min_trades)
        counts[reason] = counts.get(reason, 0) + 1
    parts = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return f"{tag} " + " ".join(f"{k}={v}" for k, v in parts)
