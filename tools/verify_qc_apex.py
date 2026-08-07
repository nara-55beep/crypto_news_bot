import ast
from datetime import date, datetime
import math
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT = ROOT / "qc_lean" / "qc_apex_es_vwap_orb"
MAX_QC_FILE_CHARS = 32000
EXPECTED_CONFIGS = 1254112


def load_function(path, name, globals_dict=None, global_names=None):
    return load_functions(path, [name], globals_dict, global_names)[name]


def load_functions(path, names, globals_dict=None, global_names=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = set(names)
    wanted_globals = set(global_names or [])
    assigns = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in wanted_globals for t in node.targets)
    ]
    funcs = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted]
    found = {node.name for node in funcs}
    missing = wanted - found
    if missing:
        raise AssertionError(f"{', '.join(sorted(missing))} not found in {path}")
    ns = {"math": math}
    if globals_dict:
        ns.update(globals_dict)
    exec(compile(ast.Module(body=assigns + funcs, type_ignores=[]), str(path), "exec"), ns)
    return {name: ns[name] for name in names}


def row(day, minute, open_, high, low, close, atr=5.0):
    return {
        "day": day,
        "minute": minute,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "atr14": atr,
    }


def signal_row(day, idx, minute, open_, high, low, close):
    r = row(day, minute, open_, high, low, close)
    r.update({
        "idx": idx,
        "vwap": 100.0,
        "ema9": 101.0,
        "ema20": 100.0,
        "rel_vol": None,
        "trend5": 0,
        "trend20": 0,
        "prior20_high": None,
        "prior20_low": None,
        "avg_daily_range": None,
        "day_open": 100.0,
        "prev_open": 95.0,
        "prev_high": 101.0,
        "prev_low": 90.0,
        "prev_close": 100.0,
        "prev_range": 11.0,
        "gap": 0.0,
    })
    return r


def approx(actual, expected, label, tolerance=1e-9):
    if abs(actual - expected) > tolerance:
        raise AssertionError(f"{label}: got {actual}, expected {expected}")


def verify_trade_costs():
    simulate_trade = load_function(PROJECT / "apex_core.py", "simulate_trade")
    spec = {"tick": 0.25, "point": 5.0, "fee": 2.0, "slip": 1.0}
    cfg = {
        "stop_mode": "atr",
        "stop_atr": 1.0,
        "rr": 2.0,
        "max_contracts": 6,
        "risk_usd": 220.0,
        "trail_r": 1.0,
    }
    day = object()

    stopped_rows = [
        row(day, 575, 100.0, 101.0, 99.0, 100.0),
        row(day, 580, 100.0, 101.0, 95.25, 96.0),
    ]
    trade, _ = simulate_trade(stopped_rows, 1, 0, 1, spec, cfg, 101.0, 99.0)
    approx(trade["entry"], 100.25, "long entry includes adverse slip")
    approx(trade["exit"], 95.0, "long stop exit includes adverse slip")
    approx(trade["qty"], 6, "qty capped after fee/slip-aware risk sizing")
    approx(trade["pnl"], -169.5, "long stopped pnl includes fee and slippage")

    target_rows = [
        row(day, 575, 100.0, 101.0, 99.0, 100.0),
        row(day, 580, 100.0, 111.0, 99.5, 110.0),
    ]
    trade, _ = simulate_trade(target_rows, 1, 0, 1, spec, cfg, 101.0, 99.0)
    approx(trade["exit"], 105.75, "ambiguous same-bar target/trail uses conservative trail exit")
    approx(trade["pnl"], 153.0, "ambiguous target/trail pnl includes fee and slippage")
    assert trade["reason"] == "trail"

    clean_target_rows = [
        row(day, 575, 100.0, 101.0, 99.0, 100.0),
        row(day, 580, 100.0, 111.0, 106.5, 110.0),
    ]
    trade, _ = simulate_trade(clean_target_rows, 1, 0, 1, spec, cfg, 101.0, 99.0)
    approx(trade["exit"], 110.0, "clean target exit includes adverse slip")
    approx(trade["pnl"], 280.5, "clean target pnl includes fee and slippage")
    assert trade["reason"] == "target"

    same_bar_trail_cfg = dict(cfg, rr=3.0)
    same_bar_trail_rows = [
        row(day, 575, 100.0, 101.0, 99.0, 100.0, atr=1.0),
        row(day, 580, 100.0, 102.0, 100.0, 102.0, atr=1.0),
        row(day, 585, 102.0, 104.0, 101.5, 103.5, atr=1.0),
    ]
    trade, exit_i = simulate_trade(same_bar_trail_rows, 1, 0, 1, spec, same_bar_trail_cfg, 101.0, 99.0)
    assert exit_i == 1
    assert trade["reason"] == "trail"
    approx(trade["exit"], 100.75, "same-bar trail recheck exits when new trail is crossed")
    approx(trade["pnl"], 3.0, "same-bar trail pnl includes fee and slippage")

    runner_cfg = dict(cfg)
    runner_cfg["target_mode"] = "runner"
    runner_rows = [
        row(day, 575, 100.0, 101.0, 99.0, 100.0, atr=1.0),
        row(day, 580, 100.0, 105.0, 100.0, 104.5, atr=1.0),
        row(day, 585, 104.5, 104.8, 103.5, 103.7, atr=1.0),
    ]
    trade, _ = simulate_trade(runner_rows, 1, 0, 1, spec, runner_cfg, 101.0, 99.0)
    approx(trade["exit"], 103.75, "runner ignores fixed target and exits by trailing stop")
    approx(trade["pnl"], 93.0, "runner pnl includes fee and slippage")
    assert trade["reason"] == "trail"

    eod_cfg = dict(cfg, target_mode="eod")
    eod_rows = [
        row(day, 575, 100.0, 101.0, 99.0, 100.0, atr=1.0),
        row(day, 580, 100.0, 111.0, 100.0, 110.0, atr=1.0),
        row(day, 585, 110.0, 113.0, 109.0, 112.0, atr=1.0),
    ]
    trade, _ = simulate_trade(eod_rows, 1, 0, 1, spec, eod_cfg, 101.0, 99.0)
    approx(trade["exit"], 111.75, "eod target mode holds until session exit")
    approx(trade["pnl"], 333.0, "eod target mode pnl includes fee and slippage")
    assert trade["reason"] == "eod"

    lock_cfg = dict(runner_cfg, trail_r=1.35, profit_lock_r=0.35)
    lock_rows = [
        row(day, 575, 100.0, 101.0, 99.0, 100.0, atr=4.0),
        row(day, 580, 100.0, 104.5, 100.0, 104.0, atr=4.0),
        row(day, 585, 104.0, 104.2, 101.5, 101.8, atr=4.0),
    ]
    trade, _ = simulate_trade(lock_rows, 1, 0, 1, spec, lock_cfg, 101.0, 99.0)
    approx(trade["exit"], 101.4, "profit-lock runner exits above breakeven before trailing catches up")
    approx(trade["pnl"], 22.5, "profit-lock runner pnl includes fee and slippage")
    assert trade["reason"] == "trail"


def verify_robust_gate():
    sys.path.insert(0, str(PROJECT))
    from apex_score import apex_ready_ok, apex_ready_reason, clean_eval_stop_pass, robust_ok, robust_reason, score_train, scored_profit_value, train_period_stats, train_split_stats

    hold = {"breached": False, "trades": 6, "profit": 600.0, "pf": 1.25, "worst_day": -300.0, "max_dd": -650.0}
    full = {
        "eval_pass": True,
        "profit": 3500.0,
        "breached": False,
        "trades": 20,
        "active_days": 14,
        "pf": 1.5,
        "worst_day": -500.0,
        "min_cushion": 1200.0,
        "best_day": 875.0,
        "best_day_share": 0.25,
        "consistency_share": 0.25,
        "best_trade_share": 0.20,
        "eval_stop_pass": True,
        "eval_stop_breached": False,
        "eval_stop_profit": 3010.0,
        "eval_stop_cushion": 900.0,
        "eval_stop_trades": 8,
        "eval_stop_active_days": 6,
        "eval_stop_best_day": 760.0,
        "eval_stop_best_trade": 610.0,
        "eval_stop_consistency_share": 0.25,
        "eval_stop_best_trade_share": 0.20,
        "stress_profit": 800.0,
        "stress_breached": False,
        "stress_pf": 1.2,
        "path_profit": 100.0,
        "path_breached": False,
        "path_max_dd": -500.0,
        "neighbor_total": 4,
        "neighbor_profitable": 3,
        "neighbor_breached": 0,
        "neighbor_min_profit": -100.0,
    }
    roll = {
        "passed": 4,
        "total": 120,
        "pass_rate": 0.04,
        "avg_profit": 200.0,
        "breach_rate": 0.2,
        "pass_bucket_pos": 2,
        "pass_bucket_max_share": 0.50,
        "q_pos": 3,
        "q_min": -500.0,
        "m_total": 8,
        "m_pos": 5,
        "m_min": -500.0,
        "min_cushion": 800.0,
        "wf_total": 5,
        "wf_pos": 4,
        "wf_min": -500.0,
        "wf_breached": 0,
    }
    assert robust_ok(hold, full, roll, 18), "baseline should be robust"
    assert apex_ready_ok(hold, full, roll, 18), "baseline should be apex ready"
    assert clean_eval_stop_pass(full), "baseline should have a clean eval-stop pass"
    assert scored_profit_value(dict(full, profit=12000.0)) == 3010.0, "score should use eval-stop profit after clean pass"
    assert scored_profit_value(dict(full, eval_stop_pass=False, profit=2200.0)) == 2200.0, "score should use full profit without clean eval stop"

    full_breach = dict(full)
    full_breach["breached"] = True
    assert robust_reason(hold, full_breach, roll, 18) == "full_breach"
    assert apex_ready_reason(hold, full_breach, roll, 18) == "ok"

    eval_breach = dict(full)
    eval_breach["eval_stop_pass"] = False
    eval_breach["eval_stop_breached"] = True
    assert robust_reason(hold, eval_breach, roll, 18) == "eval_breach"
    assert apex_ready_reason(hold, eval_breach, roll, 18) == "eval_breach"

    touched_only = dict(full)
    touched_only["eval_stop_pass"] = False
    touched_only["eval_stop_breached"] = False
    assert robust_reason(hold, touched_only, roll, 18) == "no_target"

    thin_pass = dict(full)
    thin_pass["eval_stop_cushion"] = 120.0
    assert apex_ready_reason(hold, thin_pass, roll, 18) == "thin_pass_cushion"

    fast_pass = dict(full)
    fast_pass["eval_stop_trades"] = 1
    assert apex_ready_reason(hold, fast_pass, roll, 18) == "fast_lucky_pass"

    fast_days = dict(full)
    fast_days["eval_stop_active_days"] = 1
    assert apex_ready_reason(hold, fast_days, roll, 18) == "fast_lucky_days"

    lucky_pass_day = dict(full)
    lucky_pass_day["eval_stop_consistency_share"] = 0.60
    assert apex_ready_reason(hold, lucky_pass_day, roll, 18) == "lucky_pass_day"

    lucky_pass_trade = dict(full)
    lucky_pass_trade["eval_stop_best_trade_share"] = 0.48
    assert apex_ready_reason(hold, lucky_pass_trade, roll, 18) == "lucky_pass_trade"

    rolling_bad = dict(roll)
    rolling_bad["breach_rate"] = 0.5
    assert robust_reason(hold, full, rolling_bad, 18) == "rolling_breach"

    thin_roll_pass = dict(roll)
    thin_roll_pass["passed"] = 2
    thin_roll_pass["pass_rate"] = 2 / thin_roll_pass["total"]
    assert apex_ready_reason(hold, full, thin_roll_pass, 18) == "no_roll_pass"

    bad_roll_avg = dict(roll)
    bad_roll_avg["avg_profit"] = -150.0
    assert apex_ready_reason(hold, full, bad_roll_avg, 18) == "bad_roll_avg"

    clustered_roll = dict(roll)
    clustered_roll["pass_bucket_pos"] = 1
    clustered_roll["pass_bucket_max_share"] = 1.0
    assert apex_ready_reason(hold, full, clustered_roll, 18) == "clustered_roll_pass"

    thin = dict(full)
    thin["min_cushion"] = 200.0
    assert robust_reason(hold, thin, roll, 18) == "thin_cushion"
    assert apex_ready_reason(hold, thin, roll, 18) == "thin_cushion"

    thin_roll = dict(roll)
    thin_roll["min_cushion"] = 100.0
    assert robust_reason(hold, full, thin_roll, 18) == "thin_roll_cushion"
    assert apex_ready_reason(hold, full, thin_roll, 18) == "thin_roll_cushion"

    weak_hold = dict(hold)
    weak_hold["pf"] = 1.01
    assert robust_reason(weak_hold, full, roll, 18) == "weak_hold_pf"
    assert apex_ready_reason(weak_hold, full, roll, 18) == "weak_hold_pf"

    bad_hold_dd = dict(hold)
    bad_hold_dd["max_dd"] = -1900.0
    assert robust_reason(bad_hold_dd, full, roll, 18) == "bad_hold_dd"

    lucky = dict(full)
    lucky["best_trade_share"] = 0.6
    assert robust_reason(hold, lucky, roll, 18) == "lucky_trade"

    inconsistent = dict(full)
    inconsistent["best_day_share"] = 0.41
    inconsistent["consistency_share"] = 0.41
    assert robust_reason(hold, inconsistent, roll, 18) == "bad_consistency"

    bad_months = dict(roll)
    bad_months["m_pos"] = 2
    assert robust_reason(hold, full, bad_months, 18) == "bad_months"

    bad_wf = dict(roll)
    bad_wf["wf_pos"] = 2
    assert robust_reason(hold, full, bad_wf, 18) == "bad_walkforward"

    stress_bad = dict(full)
    stress_bad["stress_profit"] = -100.0
    assert robust_reason(hold, stress_bad, roll, 18) == "stress_loss"

    path_bad = dict(full)
    path_bad["path_breached"] = True
    path_bad["path_profit"] = -1900.0
    path_bad["path_max_dd"] = -2050.0
    assert robust_reason(hold, path_bad, roll, 18) == "path_breach"

    fragile = dict(full)
    fragile["neighbor_profitable"] = 1
    assert robust_reason(hold, fragile, roll, 18) == "param_fragile"

    smooth_train = {
        "eval_pass": False,
        "profit": 1200.0,
        "pf": 1.4,
        "trades": 24,
        "active_days": 16,
        "max_dd": -700.0,
        "worst_day": -260.0,
        "best_day_share": 0.25,
    }
    burst_train = dict(smooth_train)
    burst_train["active_days"] = 4
    burst_train["best_day_share"] = 0.75
    assert score_train(smooth_train) > score_train(burst_train), "train score should prefer smoother active-day profit"

    stable_train = dict(smooth_train)
    stable_train["daily"] = {
        date(2025, 4, 1): 250.0,
        date(2025, 5, 1): 280.0,
        date(2025, 6, 2): 310.0,
        date(2025, 7, 1): 360.0,
    }
    concentrated_train = dict(smooth_train)
    concentrated_train["daily"] = {
        date(2025, 4, 1): 1050.0,
        date(2025, 5, 1): 50.0,
        date(2025, 6, 2): 50.0,
        date(2025, 7, 1): 50.0,
    }
    assert train_period_stats(stable_train["daily"])["pos"] == 4
    assert train_split_stats(stable_train["daily"])["min"] > 0.0
    assert score_train(stable_train) > score_train(concentrated_train), "train score should prefer multi-period profit"

    one_half_train = dict(smooth_train)
    one_half_train["daily"] = {
        date(2025, 4, 1): 0.0,
        date(2025, 5, 1): 0.0,
        date(2025, 6, 2): 600.0,
        date(2025, 7, 1): 600.0,
    }
    assert score_train(stable_train) > score_train(one_half_train), "train score should penalize one-half training dependence"


def verify_stress_helpers():
    sys.path.insert(0, str(PROJECT))
    from apex_score import train_candidate_ok, train_period_stats, train_split_stats

    neighbor_calls = []

    def fake_run_backtest(groups, peer, spec, cfg, start_day=None, end_day=None, stop_on_event=False):
        idx = len(neighbor_calls)
        neighbor_calls.append(stop_on_event)
        outcomes = [
            {"profit": 3010.0, "eval_pass": True, "breached": False},
            {"profit": 3500.0, "eval_pass": True, "breached": True},
            {"profit": -1200.0, "eval_pass": False, "breached": True},
            {"profit": 200.0, "eval_pass": False, "breached": False},
        ]
        return outcomes[idx % len(outcomes)]

    funcs = load_functions(
        PROJECT / "main.py",
        ["attach_eval_stop", "attach_neighbors", "attach_stress", "evaluate_neighbors", "neighbor_configs", "select_shortlist", "stressed_spec"],
        globals_dict={"run_backtest": fake_run_backtest},
    )
    spec = {"point": 5.0, "tick": 0.25, "fee": 2.0, "slip": 1.0}
    stressed = funcs["stressed_spec"](spec)
    approx(stressed["fee"], 4.0, "stress doubles fee")
    approx(stressed["slip"], 2.0, "stress doubles slip")
    full = {}
    stress = {"profit": 123.0, "breached": False, "eval_pass": False, "pf": 1.1}
    funcs["attach_stress"](full, stress)
    approx(full["stress_profit"], 123.0, "stress profit attached")

    eval_stop = {"profit": 3010.0, "breached": False, "eval_pass": True, "pass_day": "D1", "breach_day": None, "min_cushion": 700.0, "trades": 9, "active_days": 5, "best_day": 900.0, "best_trade": 650.0, "consistency_share": 0.30, "best_trade_share": 0.22}
    funcs["attach_eval_stop"](full, eval_stop)
    assert full["eval_stop_pass"], "eval stop pass attached"
    approx(full["eval_stop_profit"], 3010.0, "eval stop profit attached")
    approx(full["eval_stop_cushion"], 700.0, "eval stop cushion attached")
    assert full["eval_stop_trades"] == 9
    assert full["eval_stop_active_days"] == 5
    approx(full["eval_stop_best_day"], 900.0, "eval stop best day attached")
    approx(full["eval_stop_best_trade"], 650.0, "eval stop best trade attached")
    approx(full["eval_stop_consistency_share"], 0.30, "eval stop consistency attached")
    approx(full["eval_stop_best_trade_share"], 0.22, "eval stop best trade share attached")
    assert full["eval_stop_day"] == "D1"

    cfg = {"stop_atr": 1.0, "rr": 1.5, "entry_end": 630, "or_bars": 3}
    neighbors = funcs["neighbor_configs"](cfg)
    assert len(neighbors) == 4, "four parameter neighbors expected"
    funcs["attach_neighbors"](full, {"total": 4, "profitable": 3, "passed": 1, "breached": 0, "min_profit": -50.0, "avg_profit": 300.0})
    assert full["neighbor_total"] == 4
    assert full["neighbor_profitable"] == 3

    neighbor_summary = funcs["evaluate_neighbors"]({"groups": {}, "peer": {}}, cfg)
    assert len(neighbor_calls) == 4
    assert all(neighbor_calls), "neighbor variants should use stop-on-event Apex evaluation"
    assert neighbor_summary["total"] == 4
    assert neighbor_summary["profitable"] == 3
    assert neighbor_summary["passed"] == 1, "breached neighbor pass should not count as clean pass"
    assert neighbor_summary["breached"] == 2
    approx(neighbor_summary["min_profit"], -1200.0, "neighbor min uses stop-on-event path")

    from apex_ensemble import attach_path_stress, diverse_seeds, ensemble_seed_ok, path_order_stress, replay_apex_window

    daily = {1: -800.0, 2: 600.0, 3: -700.0, 4: 500.0, 5: -650.0}
    path = path_order_stress(daily, [1, 2, 3, 4, 5])
    assert path["breached"], "clustered losses should breach Apex drawdown"
    assert path["clustered_losses"] == 3
    attach_path_stress(full, path)
    assert full["path_breached"], "path stress breach attached"

    pass_replay = replay_apex_window({1: 1500.0, 2: 1700.0, 3: -900.0}, [1, 2, 3], 1, 3)
    assert pass_replay["eval_pass"]
    assert pass_replay["trades"] == 2
    assert pass_replay["active_days"] == 2
    approx(pass_replay["best_day"], 1700.0, "eval replay best day should stop at pass")
    approx(pass_replay["best_trade"], 1700.0, "eval replay best trade uses daily replay unit")
    approx(pass_replay["consistency_share"], 1700.0 / 3200.0, "eval replay concentration uses pass-time profit")
    approx(pass_replay["best_trade_share"], 1700.0 / 3200.0, "eval replay best trade share attached")

    seed_cfg = {
        "market": "MES", "name": "orb_break", "side": "both", "entry_start": 0, "entry_end": 690,
        "stop_atr": 1.0, "rr": 1.5, "filter": "ema_peer", "or_bars": 3,
    }
    seed_hold = {"profit": 100.0}
    good_full = {"trades": 12, "active_days": 9, "profit": 700.0, "best_day_share": 0.25, "best_trade_share": 0.20}
    sparse_full = dict(good_full)
    sparse_full["trades"] = 3
    lucky_full = dict(good_full)
    lucky_full["best_trade_share"] = 0.70
    assert ensemble_seed_ok(seed_hold, good_full)
    assert not ensemble_seed_ok(seed_hold, sparse_full)
    assert not ensemble_seed_ok(seed_hold, lucky_full)
    seed_items = [
        (3000.0, seed_cfg, {}, seed_hold, sparse_full, {}),
        (2500.0, dict(seed_cfg, name="gap_fade"), {}, seed_hold, lucky_full, {}),
        (1000.0, dict(seed_cfg, name="quality_orb_continuation"), {}, seed_hold, good_full, {}),
    ]
    seeds = diverse_seeds(seed_items, 5)
    assert len(seeds) == 1
    assert seeds[0]["cfg"]["name"] == "quality_orb_continuation"

    dominant = []
    for i in range(40):
        dominant.append((1000 - i, {
            "market": "MES", "name": "orb_break", "side": "both", "entry_start": 0, "entry_end": 630 + i,
            "stop_atr": 1.0 + 0.001 * i, "rr": 1.5, "filter": "ema_peer", "or_bars": 3, "max_trades_day": 1,
            "risk_usd": 220.0, "second_trade_mode": "any", "daily_profit_stop": 9999.0, "daily_loss_stop": 1000.0, "regime": "all",
        }, {}))
    for i in range(12):
        dominant.append((800 - i, {
            "market": "MCL", "name": "gap_fade", "side": "both", "entry_start": 0, "entry_end": 690 + i,
            "stop_atr": 1.0 + 0.001 * i, "rr": 1.5, "filter": "ema", "or_bars": 3, "max_trades_day": 1,
            "risk_usd": 160.0, "second_trade_mode": "any", "daily_profit_stop": 9999.0, "daily_loss_stop": 1000.0, "regime": "large_gap",
        }, {}))
    shortlist = funcs["select_shortlist"](dominant, 20)
    assert len(shortlist) == 20
    assert any(item[1]["market"] == "MCL" for item in shortlist), "diverse shortlist should preserve lower-ranked markets"

    good_train = {
        "eval_pass": False,
        "trades": 16,
        "active_days": 10,
        "worst_day": -300.0,
        "max_dd": -700.0,
        "profit": 600.0,
        "pf": 1.1,
        "best_day_share": 0.35,
        "best_trade_share": 0.20,
        "daily": {
            date(2025, 4, 1): 120.0,
            date(2025, 4, 8): -80.0,
            date(2025, 5, 2): 210.0,
            date(2025, 6, 4): 170.0,
            date(2025, 7, 7): 180.0,
        },
    }
    assert train_candidate_ok(good_train), "reasonable train candidate should pass"
    low_trades = dict(good_train)
    low_trades["trades"] = 9
    assert not train_candidate_ok(low_trades), "low trade train candidate should fail"
    sparse_days = dict(good_train)
    sparse_days["active_days"] = 5
    assert not train_candidate_ok(sparse_days), "sparse active-day train candidate should fail"
    lucky_trade = dict(good_train)
    lucky_trade["best_trade_share"] = 0.50
    assert not train_candidate_ok(lucky_trade), "one-trade train candidate should fail"
    one_period = dict(good_train)
    one_period["daily"] = {
        date(2025, 4, 1): 500.0,
        date(2025, 4, 8): 100.0,
    }
    assert not train_candidate_ok(one_period), "one-period train candidate should fail"
    period_stats = train_period_stats(good_train["daily"])
    assert period_stats["active"] == 4
    assert period_stats["pos"] == 4
    split_stats = train_split_stats(good_train["daily"])
    assert split_stats["min_active"] == 2
    bad_daily = dict(good_train)
    bad_daily["worst_day"] = -1100.0
    assert not train_candidate_ok(bad_daily), "pre-target daily breach should fail"
    bad_split = dict(good_train)
    bad_split["daily"] = {
        date(2025, 4, 1): -1300.0,
        date(2025, 5, 2): 100.0,
        date(2025, 6, 4): 1000.0,
        date(2025, 7, 7): 1000.0,
    }
    assert not train_candidate_ok(bad_split), "bad train split should fail"
    target_hit = dict(bad_daily)
    target_hit["eval_pass"] = True
    assert train_candidate_ok(target_hit), "training target hit should survive full-period blemishes"


def verify_final_selection():
    sys.path.insert(0, str(PROJECT))
    from apex_select import select_final_candidate

    assert select_final_candidate(["ae"], ["as"], ["re"], ["rs"], ["ee"], ["es"], ["be"], ["bs"]) == ("ensemble_apex_ready", "ensemble", "ae")
    assert select_final_candidate([], ["as"], ["re"], ["rs"], ["ee"], ["es"], ["be"], ["bs"]) == ("single_apex_ready", "single", "as")
    assert select_final_candidate([], [], ["re"], ["rs"], ["ee"], ["es"], ["be"], ["bs"]) == ("ensemble_robust", "ensemble", "re")
    assert select_final_candidate([], [], [], [], ["ee"], ["es"], ["be"], ["bs"]) == ("ensemble_eval_stop", "ensemble", "ee")
    assert select_final_candidate([], [], [], [], [], ["es"], ["be"], ["bs"]) == ("single_eval_stop", "single", "es")
    assert select_final_candidate([], [], [], [], [], [], ["be"], ["bs"]) == ("none", "none", None)
    assert select_final_candidate([], [], [], [], [], [], [], []) == ("none", "none", None)


def verify_loss_cooldown():
    def fake_signal(g, i, peer, cfg, state, or_hi, or_lo, or_range, or_atr):
        return 1

    def fake_trade(g, entry_i, sig_i, side, spec, cfg, or_hi, or_lo):
        row = g[entry_i]
        return {"day": row["day"], "pnl": row["pnl"]}, entry_i

    def fake_summary(equity, trades, max_dd, worst_day, first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion=2000.0):
        return {"equity": equity, "trades": trades, "daily": daily, "worst_day": worst_day, "min_cushion": min_cushion}

    funcs = load_functions(
        PROJECT / "main.py",
        ["effective_daily_loss_stop", "run_backtest"],
        globals_dict={
            "day_regime_ok": lambda row, cfg, or_range, or_atr: True,
            "risk_adjusted_cfg": lambda cfg, equity, threshold, day_pnl, loss_streak=0: cfg,
            "signal_for": fake_signal,
            "simulate_trade": fake_trade,
            "summarize": fake_summary,
        },
    )
    grouped = {}
    for day, pnl in [(1, -300.0), (2, 300.0), (3, 300.0)]:
        grouped[day] = [
            {"day": day, "time": ("t", day, 0), "minute": 575, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": day, "time": ("t", day, 1), "minute": 580, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": day, "time": ("t", day, 2), "minute": 585, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": pnl},
            {"day": day, "time": ("t", day, 3), "minute": 590, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
        ]
    cfg = {
        "or_bars": 1,
        "entry_end": 600,
        "max_trades_day": 1,
        "daily_profit_stop": 9999.0,
        "daily_loss_stop": 1000.0,
        "loss_cooldown": 1,
        "cooldown_loss": 250.0,
        "risk_profile": "fixed",
    }
    result = funcs["run_backtest"](grouped, {}, {"tick": 0.25}, cfg)
    assert result["daily"][1] == -300.0
    assert result["daily"][2] == 0.0
    assert result["daily"][3] == 300.0
    assert result["min_cushion"] == 1700.0
    assert len(result["trades"]) == 2


def verify_daily_loss_not_eval_breach():
    def fake_signal(g, i, peer, cfg, state, or_hi, or_lo, or_range, or_atr):
        return 1

    def fake_trade(g, entry_i, sig_i, side, spec, cfg, or_hi, or_lo):
        row = g[entry_i]
        return {"day": row["day"], "pnl": row["pnl"]}, entry_i

    def fake_summary(equity, trades, max_dd, worst_day, first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion=2000.0):
        return {
            "equity": equity,
            "trades": trades,
            "daily": daily,
            "breached": first_breach,
            "eval_pass": eval_pass,
            "pass_day": pass_day,
            "breach_day": breach_day,
            "pass_profit": pass_profit,
            "breach_profit": breach_profit,
        }

    funcs = load_functions(
        PROJECT / "main.py",
        ["effective_daily_loss_stop", "run_backtest"],
        globals_dict={
            "day_regime_ok": lambda row, cfg, or_range, or_atr: True,
            "risk_adjusted_cfg": lambda cfg, equity, threshold, day_pnl, loss_streak=0: cfg,
            "signal_for": fake_signal,
            "simulate_trade": fake_trade,
            "summarize": fake_summary,
        },
    )
    grouped = {}
    for day, pnl in [(1, -1100.0), (2, 4200.0)]:
        grouped[day] = [
            {"day": day, "time": ("t", day, 0), "minute": 575, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": day, "time": ("t", day, 1), "minute": 580, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": day, "time": ("t", day, 2), "minute": 585, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": pnl},
            {"day": day, "time": ("t", day, 3), "minute": 590, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
        ]
    cfg = {
        "or_bars": 1,
        "entry_end": 600,
        "max_trades_day": 1,
        "daily_profit_stop": 9999.0,
        "daily_loss_stop": 1000.0,
        "loss_cooldown": 0,
        "risk_profile": "fixed",
    }
    result = funcs["run_backtest"](grouped, {}, {"tick": 0.25}, cfg)
    assert result["daily"][1] == -1100.0
    assert not result["breached"], "daily loss hit should pause the session, not fail the eval"
    assert result["eval_pass"], "candidate should still pass after later recovery above target"
    assert result["breach_day"] is None

    sys.path.insert(0, str(PROJECT))
    from apex_ensemble import replay_apex_window

    replay = replay_apex_window({1: -1100.0, 2: 4200.0}, [1, 2], 1, 2)
    assert not replay["breached"], "daily replay should not treat DLL hit as eval breach"
    assert replay["eval_pass"], "daily replay should allow recovery to Apex target"
    assert replay["breach_day"] is None


def verify_eod_threshold_intraday_breach():
    def fake_signal(g, i, peer, cfg, state, or_hi, or_lo, or_range, or_atr):
        return 1

    def fake_trade(g, entry_i, sig_i, side, spec, cfg, or_hi, or_lo):
        row = g[entry_i]
        return {"day": row["day"], "pnl": row["pnl"]}, entry_i

    def fake_summary(equity, trades, max_dd, worst_day, first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion=2000.0):
        return {
            "equity": equity,
            "trades": trades,
            "daily": daily,
            "breached": first_breach,
            "eval_pass": eval_pass,
            "pass_day": pass_day,
            "breach_day": breach_day,
            "breach_profit": breach_profit,
            "min_cushion": min_cushion,
        }

    funcs = load_functions(
        PROJECT / "main.py",
        ["effective_daily_loss_stop", "run_backtest"],
        globals_dict={
            "day_regime_ok": lambda row, cfg, or_range, or_atr: True,
            "risk_adjusted_cfg": lambda cfg, equity, threshold, day_pnl, loss_streak=0: cfg,
            "signal_for": fake_signal,
            "simulate_trade": fake_trade,
            "summarize": fake_summary,
        },
    )

    def bars(day, pnls):
        rows = [
            {"day": day, "time": ("t", day, 0), "minute": 575, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": day, "time": ("t", day, 1), "minute": 580, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
        ]
        minute = 585
        for pnl in pnls:
            rows.append({"day": day, "time": ("t", day, minute), "minute": minute, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": pnl})
            minute += 5
            rows.append({"day": day, "time": ("t", day, minute), "minute": minute, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0})
            minute += 5
        return rows

    grouped = {
        1: bars(1, [2000.0]),
        2: bars(2, [-1950.0]),
        3: bars(3, [-100.0, 400.0]),
    }
    cfg = {
        "or_bars": 1,
        "entry_end": 620,
        "max_trades_day": 2,
        "daily_profit_stop": 9999.0,
        "daily_loss_stop": 1000.0,
        "loss_cooldown": 0,
        "risk_profile": "fixed",
    }
    result = funcs["run_backtest"](grouped, {}, {"tick": 0.25}, cfg)
    assert result["daily"][1] == 2000.0
    assert result["daily"][2] == -1950.0
    assert result["breached"], "touching active EOD threshold intraday should fail even if the day recovers"
    assert result["breach_day"] == 3
    assert not result["eval_pass"]
    assert result["min_cushion"] == -50.0


def verify_near_target_daily_stop():
    def fake_signal(g, i, peer, cfg, state, or_hi, or_lo, or_range, or_atr):
        return 1

    def fake_trade(g, entry_i, sig_i, side, spec, cfg, or_hi, or_lo):
        row = g[entry_i]
        return {"day": row["day"], "pnl": row["pnl"]}, entry_i

    def fake_summary(equity, trades, max_dd, worst_day, first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion=2000.0):
        return {"equity": equity, "trades": trades, "daily": daily, "eval_pass": eval_pass}

    funcs = load_functions(
        PROJECT / "main.py",
        ["effective_daily_loss_stop", "run_backtest"],
        globals_dict={
            "day_regime_ok": lambda row, cfg, or_range, or_atr: True,
            "risk_adjusted_cfg": lambda cfg, equity, threshold, day_pnl, loss_streak=0: cfg,
            "signal_for": fake_signal,
            "simulate_trade": fake_trade,
            "summarize": fake_summary,
        },
    )
    assert funcs["effective_daily_loss_stop"]({"daily_loss_stop": 1000.0}, 52499.0) == 1000.0
    assert funcs["effective_daily_loss_stop"]({"daily_loss_stop": 1000.0}, 52500.0) == 350.0
    assert funcs["effective_daily_loss_stop"]({"daily_loss_stop": 1000.0}, 52900.0) == 180.0

    grouped = {
        1: [
            {"day": 1, "time": ("t", 1, 0), "minute": 575, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": 1, "time": ("t", 1, 1), "minute": 580, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": 1, "time": ("t", 1, 2), "minute": 585, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 2600.0},
            {"day": 1, "time": ("t", 1, 3), "minute": 590, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
        ],
        2: [
            {"day": 2, "time": ("t", 2, 0), "minute": 575, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": 2, "time": ("t", 2, 1), "minute": 580, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": -400.0},
            {"day": 2, "time": ("t", 2, 2), "minute": 585, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": -400.0},
            {"day": 2, "time": ("t", 2, 3), "minute": 590, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
        ],
    }
    cfg = {
        "or_bars": 1,
        "entry_end": 600,
        "max_trades_day": 3,
        "daily_profit_stop": 9999.0,
        "daily_loss_stop": 1000.0,
        "loss_cooldown": 0,
        "risk_profile": "fixed",
    }
    result = funcs["run_backtest"](grouped, {}, {"tick": 0.25}, cfg)
    assert result["daily"][1] == 2600.0
    assert result["daily"][2] == -400.0
    assert len(result["trades"]) == 2


def verify_second_trade_mode():
    def fake_signal(g, i, peer, cfg, state, or_hi, or_lo, or_range, or_atr):
        return 1

    def fake_trade(g, entry_i, sig_i, side, spec, cfg, or_hi, or_lo):
        row = g[entry_i]
        return {"day": row["day"], "pnl": row["pnl"]}, entry_i

    def fake_summary(equity, trades, max_dd, worst_day, first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion=2000.0):
        return {"equity": equity, "trades": trades, "daily": daily}

    funcs = load_functions(
        PROJECT / "main.py",
        ["effective_daily_loss_stop", "run_backtest"],
        globals_dict={
            "day_regime_ok": lambda row, cfg, or_range, or_atr: True,
            "risk_adjusted_cfg": lambda cfg, equity, threshold, day_pnl, loss_streak=0: cfg,
            "signal_for": fake_signal,
            "simulate_trade": fake_trade,
            "summarize": fake_summary,
        },
    )
    grouped = {
        1: [
            {"day": 1, "time": ("t", 1, 0), "minute": 575, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": 1, "time": ("t", 1, 1), "minute": 580, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
            {"day": 1, "time": ("t", 1, 2), "minute": 585, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": -100.0},
            {"day": 1, "time": ("t", 1, 3), "minute": 590, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 300.0},
            {"day": 1, "time": ("t", 1, 4), "minute": 595, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr14": 2.0, "pnl": 0.0},
        ]
    }
    base = {
        "or_bars": 1,
        "entry_end": 600,
        "max_trades_day": 2,
        "daily_profit_stop": 9999.0,
        "daily_loss_stop": 1000.0,
        "loss_cooldown": 0,
        "risk_profile": "fixed",
    }
    any_result = funcs["run_backtest"](grouped, {}, {"tick": 0.25}, dict(base, second_trade_mode="any"))
    after_win = funcs["run_backtest"](grouped, {}, {"tick": 0.25}, dict(base, second_trade_mode="after_win"))
    assert len(any_result["trades"]) == 2
    assert len(after_win["trades"]) == 1


def verify_risk_profile():
    risk_adjusted_cfg = load_function(PROJECT / "main.py", "risk_adjusted_cfg")
    cfg = {"risk_usd": 220.0, "risk_profile": "apex_guard"}
    normal = risk_adjusted_cfg(cfg, 51000.0, 49000.0, 0.0)
    approx(normal["risk_usd"], 220.0, "normal cushion keeps base risk")
    tight = risk_adjusted_cfg(cfg, 50050.0, 49500.0, 0.0)
    approx(tight["risk_usd"], 99.0, "tight cushion cuts risk")
    damaged = risk_adjusted_cfg(cfg, 49400.0, 48000.0, 0.0)
    approx(damaged["risk_usd"], 110.0, "damaged equity halves risk")
    deeply_damaged = risk_adjusted_cfg(cfg, 49200.0, 48000.0, 0.0)
    approx(deeply_damaged["risk_usd"], 77.0, "deeply damaged equity cuts risk further")
    near_target = risk_adjusted_cfg(cfg, 52950.0, 51000.0, 0.0)
    approx(near_target["risk_usd"], 77.0, "near target reduces risk")
    two_loss_streak = risk_adjusted_cfg(cfg, 51000.0, 49000.0, 0.0, 2)
    approx(two_loss_streak["risk_usd"], 110.0, "two-day loss streak halves risk")
    three_loss_streak = risk_adjusted_cfg(cfg, 51000.0, 49000.0, 0.0, 3)
    approx(three_loss_streak["risk_usd"], 77.0, "three-day loss streak cuts risk further")


def verify_macro_filter():
    ns = load_functions(PROJECT / "main.py", ["day_regime_ok", "macro_event_day"], global_names=["MAJOR_EVENT_DATES"])
    row = {"day": date(2025, 6, 18), "gap": 0.0, "atr14": 5.0}
    assert ns["macro_event_day"](date(2025, 6, 18)), "FOMC date should be macro event"
    assert not ns["day_regime_ok"](row, {"regime": "all", "event_filter": "macro_skip"}, 1.0, 5.0)
    row["day"] = date(2025, 6, 19)
    assert ns["day_regime_ok"](row, {"regime": "all", "event_filter": "macro_skip"}, 1.0, 5.0)
    row.update({"gap": 2.0, "atr14": 10.0, "day_open": 100.0, "close": 104.0})
    assert ns["day_regime_ok"](row, {"regime": "balanced_open", "event_filter": "none"}, 6.0, 10.0)
    assert not ns["day_regime_ok"](row, {"regime": "balanced_open", "event_filter": "none"}, 11.0, 10.0)
    assert ns["day_regime_ok"](row, {"regime": "drive_open", "event_filter": "none"}, 8.0, 10.0)
    row["rel_vol"] = 1.20
    assert ns["day_regime_ok"](row, {"regime": "vvg_drive", "event_filter": "none"}, 8.0, 10.0)
    row["rel_vol"] = 1.05
    assert not ns["day_regime_ok"](row, {"regime": "vvg_drive", "event_filter": "none"}, 8.0, 10.0)
    row["close"] = 101.0
    assert not ns["day_regime_ok"](row, {"regime": "drive_open", "event_filter": "none"}, 8.0, 10.0)


def verify_new_signals():
    sys.path.insert(0, str(PROJECT))
    from apex_signal_extra import EXTRA_SIGNAL_NAMES, extra_signal_for

    ns = load_functions(
        PROJECT / "apex_signals.py",
        ["daily_trend_side", "indicators_ok", "peer_confirms", "peer_diverges", "rel_vol_ok", "rejection", "signal_for", "trend_side"],
        globals_dict={"EXTRA_SIGNAL_NAMES": EXTRA_SIGNAL_NAMES, "extra_signal_for": extra_signal_for},
    )
    signal_for = ns["signal_for"]
    day = object()
    cfg = {
        "side": "both",
        "filter": "none",
        "name": "gap_go",
        "sweep_atr": 0.10,
        "or_bars": 3,
    }
    g = [
        signal_row(day, 0, 575, 100.0, 101.0, 99.0, 100.5),
        signal_row(day, 1, 580, 100.5, 101.5, 100.0, 101.0),
        signal_row(day, 2, 585, 101.0, 102.0, 100.5, 101.5),
        signal_row(day, 3, 590, 101.5, 103.0, 101.0, 102.8),
    ]
    for r in g:
        r["day_open"] = 102.0
        r["prev_close"] = 100.0
    assert signal_for(g, 3, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    cfg["name"] = "prev_day_continuation"
    assert signal_for(g, 3, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    impulse = [
        signal_row(day, 0, 575, 100.0, 100.8, 99.8, 100.4),
        signal_row(day, 1, 580, 100.4, 101.4, 100.2, 101.0),
        signal_row(day, 2, 585, 101.0, 102.0, 100.8, 101.8),
        signal_row(day, 3, 590, 101.8, 102.5, 101.2, 102.2),
    ]
    for r in impulse:
        r["day_open"] = 100.0
        r["vwap"] = 101.0
        r["ema9"] = 102.0
        r["ema20"] = 101.5
        r["rel_vol"] = 1.2
        r["trend5"] = 1
        r["trend20"] = 1
    cfg["name"] = "opening_impulse_momentum_q"
    assert signal_for(impulse, 3, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.8, 2.2, 5.0) == 1

    fade_open = [
        signal_row(day, 0, 575, 100.0, 101.0, 99.0, 100.8),
        signal_row(day, 1, 580, 100.8, 102.5, 100.5, 102.0),
        signal_row(day, 2, 585, 102.0, 103.0, 101.2, 102.4),
        signal_row(day, 3, 590, 102.4, 102.6, 101.0, 101.4),
        signal_row(day, 4, 595, 101.4, 101.8, 100.2, 100.5),
    ]
    for r in fade_open:
        r["day_open"] = 100.0
        r["vwap"] = 101.0
        r["ema9"] = 100.8
        r["ema20"] = 101.0
        r["rel_vol"] = 1.0
    cfg["name"] = "opening_impulse_fade_q"
    assert signal_for(fade_open, 4, None, cfg, {"breakout": 0, "used_gap": False}, 103.0, 99.0, 4.0, 5.0) == -1

    direct = [
        signal_row(day, 0, 575, 100.0, 100.8, 99.8, 100.3),
        signal_row(day, 1, 580, 100.3, 101.1, 100.0, 100.8),
        signal_row(day, 2, 585, 100.8, 101.8, 100.6, 101.4),
        signal_row(day, 3, 590, 101.4, 102.8, 101.2, 102.4),
    ]
    for r in direct:
        r["day_open"] = 100.0
        r["vwap"] = 101.0
        r["ema9"] = 102.0
        r["ema20"] = 101.5
    cfg.update({"or_bars": 3, "min_impulse_atr": 0.10, "peer_mode": "none", "daily_mode": "none"})
    cfg["name"] = "first_mom_q"
    assert signal_for(direct, 3, None, cfg, {"breakout": 0, "used_gap": False}, 101.8, 99.8, 2.0, 5.0) == 1
    cfg["name"] = "first_fade_q"
    assert signal_for(direct, 3, None, cfg, {"breakout": 0, "used_gap": False}, 101.8, 99.8, 2.0, 5.0) == -1
    cfg["name"] = "prior_break_eod_q"
    direct[2]["close"] = 100.8
    state = {"breakout": 0, "used_gap": False}
    assert signal_for(direct, 3, None, cfg, state, 101.8, 99.8, 2.0, 5.0) == 1
    assert state["used_prior_break"]

    cfg["name"] = "inside_day_break"
    g[2]["close"] = 100.8
    assert signal_for(g, 3, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    cfg["name"] = "late_day_momentum"
    late = []
    for idx in range(32):
        close = 100.0 + min(idx, 11) * 0.3
        r = signal_row(day, idx, 575 + idx * 5, close - 0.1, close + 0.2, close - 0.2, close)
        r["day_open"] = 100.0
        r["vwap"] = 101.0
        r["ema9"] = 103.0
        r["ema20"] = 102.0
        late.append(r)
    late[-1]["minute"] = 14 * 60 + 30
    late[-1]["close"] = 104.0
    assert signal_for(late, len(late) - 1, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    cfg["name"] = "volume_orb_break"
    g[2]["close"] = 101.5
    g[3]["rel_vol"] = 1.5
    assert signal_for(g, 3, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    cfg["name"] = "daily_trend_orb"
    for r in g:
        r["trend5"] = 1
        r["trend20"] = 1
    assert signal_for(g, 3, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    peer = signal_row(day, 3, 590, 101.5, 103.0, 101.0, 102.8)
    peer["day_open"] = 101.0
    peer["vwap"] = 101.5
    peer["ema9"] = 102.2
    peer["ema20"] = 101.8
    cfg["name"] = "peer_confirmed_break"
    assert signal_for(g, 3, peer, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    fade = [
        signal_row(day, 0, 575, 100.0, 101.0, 99.0, 100.5),
        signal_row(day, 1, 580, 100.5, 101.5, 100.0, 101.0),
        signal_row(day, 2, 585, 101.0, 101.5, 100.5, 101.0),
        signal_row(day, 3, 590, 102.5, 103.2, 100.5, 101.0),
    ]
    bearish_peer = signal_row(day, 3, 590, 100.5, 101.0, 99.0, 99.5)
    bearish_peer["vwap"] = 100.2
    bearish_peer["ema9"] = 99.8
    bearish_peer["ema20"] = 100.0
    cfg["name"] = "peer_divergence_fade"
    assert signal_for(fade, 3, bearish_peer, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == -1

    cfg["name"] = "quality_orb_continuation"
    g[3]["rel_vol"] = 1.2
    g[3]["trend5"] = 1
    g[3]["trend20"] = 1
    assert signal_for(g, 3, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    pull = []
    for idx in range(8):
        r = signal_row(day, idx, 575 + idx * 5, 101.0, 103.0, 100.8, 102.0)
        r["vwap"] = 100.5
        r["ema9"] = 101.5
        r["ema20"] = 101.0
        r["rel_vol"] = 1.0
        pull.append(r)
    pull[5]["high"] = 104.0
    pull[-1].update({"open": 101.0, "high": 102.5, "low": 100.8, "close": 102.0})
    cfg["name"] = "vwap_trend_pullback_q"
    assert signal_for(pull, 7, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    trend_pull = []
    for idx in range(9):
        r = signal_row(day, idx, 575 + idx * 5, 102.0, 103.0, 101.5, 102.5)
        r["vwap"] = 101.5
        r["ema9"] = 102.2
        r["ema20"] = 102.0
        r["rel_vol"] = 1.0
        r["trend5"] = 1
        r["trend20"] = 1
        r["gap"] = 0.0
        trend_pull.append(r)
    trend_pull[5]["high"] = 104.2
    trend_pull[-1].update({"open": 102.0, "high": 103.2, "low": 101.8, "close": 102.8})
    cfg["name"] = "multi_speed_trend_pullback_q"
    assert signal_for(trend_pull, 8, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    first_hour = []
    for idx in range(15):
        close = 100.0 + min(idx, 11) * 0.1
        r = signal_row(day, idx, 575 + idx * 5, close - 0.1, close + 0.4, close - 0.4, close)
        r["vwap"] = 100.0
        r["ema9"] = 100.6
        r["ema20"] = 100.4
        r["rel_vol"] = 1.0
        r["trend5"] = 1
        r["trend20"] = 1
        first_hour.append(r)
    first_hour[12].update({"open": 101.0, "high": 103.0, "low": 100.9, "close": 102.4})
    first_hour[13].update({"open": 102.2, "high": 102.5, "low": 100.8, "close": 101.5})
    first_hour[14].update({"open": 101.2, "high": 102.6, "low": 101.0, "close": 102.2})
    cfg["name"] = "first_hour_retest_q"
    assert signal_for(first_hour, 14, None, cfg, {"breakout": 0, "used_gap": False}, 101.2, 99.4, 1.8, 3.0) == 1

    trap = [
        signal_row(day, 0, 575, 100.0, 101.0, 99.0, 100.5),
        signal_row(day, 1, 580, 100.5, 101.5, 100.0, 101.0),
        signal_row(day, 2, 585, 101.0, 102.0, 100.5, 101.5),
        signal_row(day, 3, 590, 102.0, 103.0, 100.4, 101.0),
        signal_row(day, 4, 595, 100.8, 101.0, 98.7, 99.2),
    ]
    for r in trap:
        r["rel_vol"] = 1.0
    cfg["name"] = "failed_break_peer_reversal_q"
    state = {"breakout": 0, "used_gap": False}
    assert signal_for(trap, 3, None, cfg, state, 102.0, 99.0, 3.0, 5.0) == 0
    assert signal_for(trap, 4, None, cfg, state, 102.0, 99.0, 3.0, 5.0) == -1

    squeeze = []
    for idx in range(6):
        r = signal_row(day, idx, 575 + idx * 5, 100.0, 101.0, 99.0, 100.5)
        r["prev_range"] = 6.0
        r["avg_daily_range"] = 10.0
        r["rel_vol"] = 1.2
        squeeze.append(r)
    squeeze[-1].update({"open": 101.0, "high": 103.0, "low": 100.8, "close": 102.5})
    cfg["name"] = "compression_expansion_q"
    assert signal_for(squeeze, 5, None, cfg, {"breakout": 0, "used_gap": False}, 102.0, 99.0, 3.0, 5.0) == 1

    balanced = []
    for idx in range(6):
        r = signal_row(day, idx, 575 + idx * 5, 100.0, 101.0, 99.0, 100.5)
        r["prev_high"] = 102.0
        r["prev_low"] = 96.0
        r["prev_range"] = 6.0
        r["avg_daily_range"] = 10.0
        r["rel_vol"] = 1.1
        r["gap"] = 0.0
        balanced.append(r)
    balanced[-2]["close"] = 101.8
    balanced[-1].update({"open": 101.8, "high": 103.4, "low": 101.4, "close": 103.0})
    cfg["name"] = "balanced_prior_breakout_q"
    assert signal_for(balanced, 5, None, cfg, {"breakout": 0, "used_gap": False}, 101.0, 99.0, 2.0, 5.0) == 1

    exhaustion = []
    for idx in range(9):
        r = signal_row(day, idx, 575 + idx * 5, 105.0, 106.0, 103.5, 104.5)
        r["day_open"] = 105.0
        r["prev_close"] = 100.0
        r["vwap"] = 104.8
        r["ema9"] = 104.0
        r["ema20"] = 104.3
        r["rel_vol"] = 1.2
        exhaustion.append(r)
    exhaustion[4]["high"] = 107.0
    exhaustion[-1].update({"open": 104.2, "high": 104.6, "low": 101.8, "close": 102.2})
    cfg["name"] = "exhaustion_gap_fade_q"
    assert signal_for(exhaustion, 8, None, cfg, {"breakout": 0, "used_gap": False}, 105.5, 103.0, 2.5, 5.0) == -1


def verify_relative_volume():
    prepare_rows = load_function(PROJECT / "main.py", "prepare_rows")
    rows = []
    for day in range(7):
        for idx in range(2):
            rows.append({
                "day": day,
                "time": type("T", (), {"hour": 9, "minute": 35 + idx * 5})(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 100.0 if day < 6 else (200.0 if idx == 1 else 100.0),
            })
    out = prepare_rows(rows)
    last = [r for r in out if r["day"] == 6 and r["idx"] == 1][0]
    approx(last["rel_vol"], 2.0, "relative volume uses prior days only")


def verify_daily_context():
    prepare_rows = load_function(PROJECT / "main.py", "prepare_rows")
    rows = []
    for day in range(22):
        close = 100.0 + day
        rows.append({
            "day": day,
            "time": type("T", (), {"hour": 9, "minute": 35})(),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 100.0,
        })
    out = prepare_rows(rows)
    last = out[-1]
    assert last["trend5"] == 1
    assert last["trend20"] == 1
    approx(last["prior20_high"], 121.0, "prior 20 high excludes current day")
    approx(last["avg_daily_range"], 2.0, "prior daily range average")


def verify_rth_session_vwap():
    class StubFutures:
        class Indices:
            SP_500_E_MINI = "ES"
            NASDAQ_100_E_MINI = "NQ"
            MICRO_SP_500_E_MINI = "MES"
            MICRO_NASDAQ_100_E_MINI = "MNQ"
            MICRO_DOW_30_E_MINI = "MYM"
            MICRO_RUSSELL_2000_E_MINI = "M2K"

        class Energies:
            MICRO_CRUDE_OIL_WTI = "MCL"

        class Metals:
            MICRO_GOLD = "MGC"

    ns = load_functions(
        PROJECT / "main.py",
        ["SymbolState", "futures_specs", "is_rth_time", "trade_specs"],
        globals_dict={"Futures": StubFutures},
    )
    state = ns["SymbolState"]()

    def bar(ts, px, vol):
        return type("Bar", (), {
            "end_time": ts,
            "open": px,
            "high": px,
            "low": px,
            "close": px,
            "volume": vol,
        })()

    pre = state.update(bar(datetime(2025, 4, 1, 8, 0), 200.0, 10000.0))
    assert pre["vwap"] is None, "premarket should not initialize RTH VWAP"
    first = state.update(bar(datetime(2025, 4, 1, 9, 35), 100.0, 100.0))
    approx(first["vwap"], 100.0, "first RTH VWAP excludes premarket volume")
    second = state.update(bar(datetime(2025, 4, 1, 9, 40), 110.0, 100.0))
    approx(second["vwap"], 105.0, "RTH VWAP accumulates only RTH bars")
    next_day = state.update(bar(datetime(2025, 4, 2, 9, 35), 120.0, 100.0))
    approx(next_day["vwap"], 120.0, "RTH VWAP resets on new RTH session")
    assert ns["is_rth_time"](datetime(2025, 4, 1, 9, 5), 9 * 60, 14 * 60 + 30), "MCL session starts before equity-index RTH"
    assert not ns["is_rth_time"](datetime(2025, 4, 1, 15, 0), 9 * 60, 14 * 60 + 30), "MCL session ends before equity-index close"
    specs = ns["futures_specs"]()
    assert specs["ES"]["ticker"] == "ES"
    assert specs["NQ"]["ticker"] == "NQ"
    assert specs["MCL"]["session_start"] == 9 * 60
    assert specs["MGC"]["session_start"] == 8 * 60 + 20
    trades = ns["trade_specs"]()
    assert trades["ES"]["point"] == 50.0
    assert trades["NQ"]["point"] == 20.0
    assert trades["ES"]["fee"] == 4.0
    oil = ns["SymbolState"](specs["MCL"]["session_start"], specs["MCL"]["session_end"])
    oil_pre = oil.update(bar(datetime(2025, 4, 1, 8, 55), 200.0, 1000.0))
    assert oil_pre["vwap"] is None
    oil_first = oil.update(bar(datetime(2025, 4, 1, 9, 5), 90.0, 100.0))
    approx(oil_first["vwap"], 90.0, "MCL VWAP uses MCL session start")


def verify_project_shape():
    main_chars = len((PROJECT / "main.py").read_text(encoding="utf-8"))
    helper_chars = len((PROJECT / "apex_ensemble.py").read_text(encoding="utf-8"))
    core_chars = len((PROJECT / "apex_core.py").read_text(encoding="utf-8"))
    signals_chars = len((PROJECT / "apex_signals.py").read_text(encoding="utf-8"))
    extra_chars = len((PROJECT / "apex_signal_extra.py").read_text(encoding="utf-8"))
    format_chars = len((PROJECT / "apex_format.py").read_text(encoding="utf-8"))
    score_chars = len((PROJECT / "apex_score.py").read_text(encoding="utf-8"))
    select_chars = len((PROJECT / "apex_select.py").read_text(encoding="utf-8"))
    locked_chars = len((PROJECT / "apex_locked.py").read_text(encoding="utf-8"))
    if main_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"main.py exceeds QuantConnect file limit: {main_chars}")
    if helper_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"apex_ensemble.py exceeds QuantConnect file limit: {helper_chars}")
    if core_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"apex_core.py exceeds QuantConnect file limit: {core_chars}")
    if signals_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"apex_signals.py exceeds QuantConnect file limit: {signals_chars}")
    if extra_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"apex_signal_extra.py exceeds QuantConnect file limit: {extra_chars}")
    if format_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"apex_format.py exceeds QuantConnect file limit: {format_chars}")
    if score_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"apex_score.py exceeds QuantConnect file limit: {score_chars}")
    if select_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"apex_select.py exceeds QuantConnect file limit: {select_chars}")
    if locked_chars > MAX_QC_FILE_CHARS:
        raise AssertionError(f"apex_locked.py exceeds QuantConnect file limit: {locked_chars}")

    sys.path.insert(0, str(PROJECT))
    from apex_ensemble import build_configs

    configs = build_configs()
    if len(configs) != EXPECTED_CONFIGS:
        raise AssertionError(f"config count changed: {len(configs)} != {EXPECTED_CONFIGS}")
    if not all("entry_start" in cfg for cfg in configs):
        raise AssertionError("all configs should include entry_start")
    if not all("loss_cooldown" in cfg for cfg in configs):
        raise AssertionError("all configs should include loss_cooldown")
    if not all("second_trade_mode" in cfg for cfg in configs):
        raise AssertionError("all configs should include second_trade_mode")
    if not all("target_mode" in cfg for cfg in configs):
        raise AssertionError("all configs should include target_mode")
    if not all("profit_lock_r" in cfg for cfg in configs):
        raise AssertionError("all configs should include profit_lock_r")
    if not any(cfg["loss_cooldown"] == 1 for cfg in configs):
        raise AssertionError("cooldown variants missing")
    risk_values = set(cfg["risk_usd"] for cfg in configs)
    for risk in [160.0, 180.0, 220.0, 240.0, 300.0]:
        if risk not in risk_values:
            raise AssertionError(f"risk variant missing: {risk}")
    trail_values = set(cfg["trail_r"] for cfg in configs)
    for trail in [0.75, 1.0, 1.35]:
        if trail not in trail_values:
            raise AssertionError(f"trail variant missing: {trail}")
    if not any(cfg["entry_start"] == 9 * 60 + 50 for cfg in configs):
        raise AssertionError("delayed open-noise variants missing")
    if not any(cfg["entry_start"] == 10 * 60 for cfg in configs):
        raise AssertionError("quality delayed-entry variants missing")
    if not any(cfg["name"] == "balanced_prior_breakout_q" for cfg in configs):
        raise AssertionError("balanced prior breakout configs missing")
    if not any(cfg["name"] == "exhaustion_gap_fade_q" and cfg["loss_cooldown"] == 1 for cfg in configs):
        raise AssertionError("exhaustion gap fade cooldown configs missing")
    if not any(cfg["name"] == "multi_speed_trend_pullback_q" for cfg in configs):
        raise AssertionError("multi-speed trend pullback configs missing")
    if not any(cfg["name"] == "first_hour_retest_q" for cfg in configs):
        raise AssertionError("first-hour retest configs missing")
    if not any(cfg["name"] == "multi_speed_trend_pullback_q" and cfg["max_trades_day"] == 2 for cfg in configs):
        raise AssertionError("multi-speed second-trade configs missing")
    if not any(cfg["name"] == "first_hour_retest_q" and cfg["max_trades_day"] == 2 for cfg in configs):
        raise AssertionError("first-hour retest second-trade configs missing")
    if not any(cfg["name"] == "multi_speed_trend_pullback_q" and cfg["max_trades_day"] == 2 and cfg["second_trade_mode"] == "after_win" for cfg in configs):
        raise AssertionError("multi-speed after-win second-trade configs missing")
    if not any(cfg["name"] == "first_hour_retest_q" and cfg["max_trades_day"] == 2 and cfg["second_trade_mode"] == "after_win" for cfg in configs):
        raise AssertionError("first-hour retest after-win second-trade configs missing")
    if not any(cfg["name"] == "first_hour_retest_q" and cfg["target_mode"] == "runner" for cfg in configs):
        raise AssertionError("first-hour retest runner configs missing")
    if not any(cfg["name"] == "vwap_trend_pullback_q" and cfg["target_mode"] == "runner" for cfg in configs):
        raise AssertionError("vwap trend runner configs missing")
    if not any(cfg["name"] == "first_hour_retest_q" and cfg["target_mode"] == "runner" and cfg["profit_lock_r"] == 0.35 for cfg in configs):
        raise AssertionError("profit-lock runner configs missing")
    if any(cfg["target_mode"] == "fixed" and cfg["profit_lock_r"] != 0.0 for cfg in configs):
        raise AssertionError("fixed-target configs should not use profit lock")
    if not any(cfg["name"] == "first_hour_retest_q" and cfg["regime"] == "balanced_open" for cfg in configs):
        raise AssertionError("balanced-open first-hour configs missing")
    if not any(cfg["name"] == "failed_break_peer_reversal_q" and cfg["regime"] == "drive_open" for cfg in configs):
        raise AssertionError("drive-open failed-break configs missing")
    if not any(cfg["name"] == "first_hour_retest_q" and cfg["regime"] == "vvg_drive" for cfg in configs):
        raise AssertionError("VVG drive first-hour configs missing")
    if not any(cfg["name"] == "opening_impulse_momentum_q" and cfg["stop_atr"] == 0.40 for cfg in configs):
        raise AssertionError("opening impulse momentum tight-stop configs missing")
    if not any(cfg["name"] == "opening_impulse_fade_q" and cfg["target_mode"] == "runner" for cfg in configs):
        raise AssertionError("opening impulse fade runner configs missing")
    for direct_name in ["first_mom_q", "first_fade_q", "two_mom_q", "two_fade_q", "prior_break_eod_q"]:
        if not any(cfg["name"] == direct_name for cfg in configs):
            raise AssertionError(f"{direct_name} configs missing")
    if not any(cfg["name"] == "first_mom_q" and cfg["target_mode"] == "eod" for cfg in configs):
        raise AssertionError("first momentum EOD target-mode configs missing")
    if not any(cfg["name"] == "prior_break_eod_q" and cfg["target_mode"] == "eod" for cfg in configs):
        raise AssertionError("prior-break EOD target-mode configs missing")
    main_text = (PROJECT / "main.py").read_text(encoding="utf-8")
    locked_text = (PROJECT / "apex_locked.py").read_text(encoding="utf-8")
    if "ACTIVE_STRATEGY_NAMES" not in locked_text or "ACTIVE_MARKETS" not in locked_text or "active_config_ok" not in locked_text:
        raise AssertionError("active direct strategy filter missing from main.py")
    if "nr7_breakout" not in locked_text:
        raise AssertionError("nr7_breakout missing from active strategy filter")
    if "active_config_ok(cfg)" not in main_text:
        raise AssertionError("main.py should restrict QC search to the active strategy family")
    if "LOCKED_ENSEMBLE" not in locked_text or "evaluate_locked_ensemble" not in locked_text:
        raise AssertionError("locked ensemble helper missing")
    if "LOCKED_FINAL" not in main_text:
        raise AssertionError("main.py should emit locked fixed-ensemble result")
    if "APEX TARGET PASSED" not in main_text:
        raise AssertionError("main.py should report eval-target pass distinctly")
    if not any(cfg["market"] == "ES" and cfg["max_contracts"] == 1 for cfg in configs):
        raise AssertionError("full ES one-contract configs missing")
    if not any(cfg["market"] == "NQ" and cfg["max_contracts"] == 1 for cfg in configs):
        raise AssertionError("full NQ one-contract configs missing")
    if any(cfg["market"] in ["ES", "NQ"] and cfg["max_contracts"] != 1 for cfg in configs):
        raise AssertionError("full ES/NQ configs should stay capped at one contract")


def main():
    verify_trade_costs()
    verify_robust_gate()
    verify_stress_helpers()
    verify_final_selection()
    verify_loss_cooldown()
    verify_daily_loss_not_eval_breach()
    verify_eod_threshold_intraday_breach()
    verify_near_target_daily_stop()
    verify_risk_profile()
    verify_macro_filter()
    verify_new_signals()
    verify_relative_volume()
    verify_daily_context()
    verify_rth_session_vwap()
    verify_second_trade_mode()
    verify_project_shape()
    print("verify_qc_apex: ok")


if __name__ == "__main__":
    main()
