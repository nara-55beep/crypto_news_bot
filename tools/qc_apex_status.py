import hashlib
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT = ROOT / "qc_lean" / "qc_apex_es_vwap_orb"
QC_LIMIT = 32000


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def file_line(path):
    text = path.read_text(encoding="utf-8")
    status = "ok" if len(text) <= QC_LIMIT else "too_large"
    return f"{path.name}: chars={len(text)} sha={digest(path)} qc_limit={status}"


def config_count():
    sys.path.insert(0, str(PROJECT))
    from apex_ensemble import build_configs

    return len(build_configs())


def cost_flags():
    text = "\n".join(path.read_text(encoding="utf-8") for path in [PROJECT / "main.py", PROJECT / "apex_core.py", PROJECT / "apex_signals.py", PROJECT / "apex_signal_extra.py", PROJECT / "apex_ensemble.py", PROJECT / "apex_format.py", PROJECT / "apex_score.py", PROJECT / "apex_select.py", PROJECT / "apex_locked.py"])
    checks = {
        "fee_in_specs": '"fee": 2.0' in text,
        "slip_in_specs": '"slip": 1.0' in text,
        "risk_fee_slip": "risk_per_contract = stop_dist * point + slip * point + fee" in text,
        "pnl_fee": "pnl = side * (exit_px - entry) * qty * point - qty * fee" in text,
        "same_bar_trail_recheck": "trail_hit" in text and "if trail_hit" in text and "if not target_hit" not in text,
        "signal_module_split": "from apex_signals import signal_for" in text and "def signal_for" in text,
        "rth_session_vwap": "is_rth_time" in text and "self.rth_day" in text,
        "market_session_windows": '"session_start"' in text and '"MCL"' in text and "8 * 60 + 20" in text,
        "full_contract_candidates": '"ES": {"ticker": Futures.Indices.SP_500_E_MINI' in text and '"NQ": {"ticker": Futures.Indices.NASDAQ_100_E_MINI' in text and "max_contracts = 1 if market in [\"ES\", \"NQ\"] else 6" in text,
        "macro_skip": "event_filter" in text and "macro_skip" in text,
        "consistency_gate": "bad_consistency" in text,
        "path_stress": "path_order_stress" in text,
        "apex_ready_lane": "SEARCH_APEX_READY" in text and "apex_ready_reason" in text,
        "final_selection": "SEARCH_FINAL" in text and "emit_final_selection" in text and "no_clean_eval_stop_pass" in text,
        "no_fake_final": "ensemble_best_score" not in text and "single_best_score" not in text and "NO CLEAN APEX PASS" in text,
        "parser_blockers": "top_blockers" in pathlib.Path("tools/parse_qc_apex_log.py").read_text(encoding="utf-8"),
        "ensemble_seed_gate": "ensemble_seed_ok" in text and 'full["trades"] < 8' in text,
        "entry_start_windows": '"entry_start"' in text and "cfg.get(\"entry_start\", 0)" in text,
        "diverse_shortlist": "select_shortlist" in text and "shortlist_mode=diverse" in text,
        "loss_cooldown": '"loss_cooldown"' in text and "cooldown_remaining" in text,
        "loss_streak_throttle": "loss_streak" in text and "scale = min(scale, 0.35)" in text,
        "equity_recovery_throttle": "equity < 49250.0" in text and "equity < 49500.0" in text,
        "near_target_daily_stop": "effective_daily_loss_stop" in text and "52900.0" in text and "52500.0" in text,
        "dll_not_eval_breach": "DLL pauses trading" in text and "day_pnl <= -1000.0" not in text and "pnl <= -1000.0" not in text,
        "eod_intraday_breach": "active EOD threshold is enforced intraday after each closed trade" in text,
        "risk_variants": "risk_values" in text and "300.0" in text,
        "quality_two_trade_variants": "quality_two_trade_names" in text and '"first_hour_retest_q"' in text,
        "second_trade_after_win": "second_trade_mode" in text and "after_win" in text,
        "train_filter": "train_candidate_ok" in text and "train_filter=apex_sane" in text,
        "train_sample_gate": 'm["trades"] < 10' in text and "best_trade_share > 0.45" in text,
        "train_temporal_gate": "train_period_stats" in text and 'periods["best_share"] > 0.75' in text,
        "train_stability_score": "period_bonus" in text and "period_concentration" in text,
        "train_split_stability": "train_split_stats" in text and "split_bonus" in text,
        "apex_min_trade_gate": "MIN_SINGLE_TRADES = 48" in text and "MIN_ENSEMBLE_TRADES = 60" in text,
        "apex_positive_hold_gate": 'hold["profit"] <= 0.0' in text,
        "eval_stop_score_profit": "scored_profit_value" in text and "Final Eval P&L" in text,
        "robust_eval_stop_gate": "clean_eval_stop_pass" in text and 'return "eval_breach"' in text,
        "hold_quality_gate": "weak_hold_pf" in text and "hold_quality_penalty_value" in text,
        "drawdown_cushion_gate": "min_cushion" in text and "thin_cushion" in text,
        "rolling_cushion_gate": "thin_roll_cushion" in text,
        "rolling_pass_floor": "min_rolling_passes" in text and "bad_roll_avg" in text,
        "rolling_pass_distribution": "rolling_distribution_penalty" in text and "clustered_roll_pass" in text,
        "drawdown_cushion_logging": "roll_cushion" in text or "rollcush" in text,
        "pass_quality_gate": "eval_stop_cushion" in text and "thin_pass_cushion" in text,
        "pass_active_day_gate": "eval_stop_active_days" in text and "fast_lucky_days" in text,
        "pass_concentration_gate": "eval_stop_consistency_share" in text and "lucky_pass_day" in text and "lucky_pass_trade" in text,
        "replay_pass_concentration": "used_daily = {}" in text and '"best_trade_share": best_day_share' in text,
        "neighbor_eval_stop": "evaluate_neighbors" in text and "stop_on_event=True" in text and 'm["eval_pass"] and not m["breached"]' in text,
        "smooth_train_score": "best_day_share" in text and "active_days" in text,
        "balanced_prior_breakout": "balanced_prior_breakout_q" in text,
        "exhaustion_gap_fade": "exhaustion_gap_fade_q" in text,
        "volatility_compression_accept": "volatility_compression_accept_q" in text and "box_hi" in text,
        "vvg_late_reversal": "vvg_late_reversal_q" in text and "morning_ret" in text,
        "opening_impulse": "opening_impulse_momentum_q" in text and "opening_impulse_fade_q" in text,
        "direct_apex_family": "first_mom_q" in text and "first_fade_q" in text and "prior_break_eod_q" in text,
        "active_direct_filter": "ACTIVE_STRATEGY_NAMES" in text and "active_config_ok(cfg)" in text,
        "locked_fixed_ensemble": "LOCKED_ENSEMBLE" in text and "LOCKED_FINAL" in text,
        "multi_speed_trend_pullback": "multi_speed_trend_pullback_q" in text,
        "first_hour_retest": "first_hour_retest_q" in text,
        "second_entry_trend": "second_entry_trend_q" in text and "se_pullbacks" in text,
        "trail_variants": "trail_values" in text and "1.35" in text,
        "runner_target_mode": "target_mode" in text and '"runner"' in text,
        "eod_target_mode": "target_mode" in text and '"eod"' in text,
        "profit_lock_variants": "profit_lock_r" in text and "0.35" in text,
        "opening_regime_variants": "balanced_open" in text and "drive_open" in text and "vvg_drive" in text,
        "larger_shortlist": "select_shortlist(train_ranked, 600)" in text,
    }
    return " ".join(f"{k}={v}" for k, v in checks.items())


def main():
    print("QC Apex Local Status")
    print(file_line(PROJECT / "main.py"))
    print(file_line(PROJECT / "apex_ensemble.py"))
    print(file_line(PROJECT / "apex_core.py"))
    print(file_line(PROJECT / "apex_signals.py"))
    print(file_line(PROJECT / "apex_signal_extra.py"))
    print(file_line(PROJECT / "apex_format.py"))
    print(file_line(PROJECT / "apex_score.py"))
    print(file_line(PROJECT / "apex_select.py"))
    print(file_line(PROJECT / "apex_locked.py"))
    print(f"configs={config_count()}")
    print(cost_flags())
    print(f"project={PROJECT}")


if __name__ == "__main__":
    main()
