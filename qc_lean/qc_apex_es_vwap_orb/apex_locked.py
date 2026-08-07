from apex_ensemble import attach_path_stress, attach_stress, attach_eval_stop, evaluate_ensemble, path_order_stress, rolling_30d_from_daily


ACTIVE_STRATEGY_NAMES = {
    "nr7_breakout",
}
ACTIVE_MARKETS = {"MES", "MNQ", "MCL"}

LOCKED_ENSEMBLE = [
    {
        "market": "MES", "name": "nr7_breakout", "side": "both", "entry_start": 0, "entry_end": 15 * 60 + 55,
        "stop_atr": 1.0, "rr": 2.0, "filter": "none", "stop_mode": "nr7", "or_bars": 1,
        "risk_usd": 500.0, "risk_profile": "fixed", "event_filter": "none", "max_contracts": 6,
        "max_trades_day": 1, "second_trade_mode": "any", "daily_profit_stop": 9999.0, "daily_loss_stop": 1000.0,
        "loss_cooldown": 0, "cooldown_loss": 250.0, "trail_r": 1.0, "target_mode": "runner",
        "profit_lock_r": 0.0, "sweep_atr": 0.10, "regime": "all", "price_tick": 0.25,
    },
    {
        "market": "MNQ", "name": "nr7_breakout", "side": "both", "entry_start": 0, "entry_end": 15 * 60 + 55,
        "stop_atr": 1.0, "rr": 2.0, "filter": "none", "stop_mode": "nr7", "or_bars": 1,
        "risk_usd": 500.0, "risk_profile": "fixed", "event_filter": "none", "max_contracts": 6,
        "max_trades_day": 1, "second_trade_mode": "any", "daily_profit_stop": 9999.0, "daily_loss_stop": 1000.0,
        "loss_cooldown": 0, "cooldown_loss": 250.0, "trail_r": 1.0, "target_mode": "runner",
        "profit_lock_r": 0.0, "sweep_atr": 0.10, "regime": "all", "price_tick": 0.25,
    },
    {
        "market": "MCL", "name": "nr7_breakout", "side": "both", "entry_start": 0, "entry_end": 14 * 60 + 25,
        "stop_atr": 1.0, "rr": 2.0, "filter": "none", "stop_mode": "nr7", "or_bars": 1,
        "risk_usd": 500.0, "risk_profile": "fixed", "event_filter": "none", "max_contracts": 6,
        "max_trades_day": 1, "second_trade_mode": "any", "daily_profit_stop": 9999.0, "daily_loss_stop": 1000.0,
        "loss_cooldown": 0, "cooldown_loss": 250.0, "trail_r": 1.0, "target_mode": "runner",
        "profit_lock_r": 0.0, "sweep_atr": 0.10, "regime": "all", "price_tick": 0.01,
    },
]


def active_config_ok(cfg):
    name = cfg["name"]
    if cfg["market"] not in ACTIVE_MARKETS or name not in ACTIVE_STRATEGY_NAMES:
        return False
    if cfg["side"] != "both":
        return False
    return cfg["stop_mode"] == "nr7" and cfg["rr"] == 2.0


def locked_ensemble_configs():
    return [dict(cfg) for cfg in LOCKED_ENSEMBLE]


def evaluate_locked_ensemble(markets, all_days, split, run_backtest, stressed_spec, attach_neighbors, evaluate_neighbors, score_final):
    members = []
    for cfg in locked_ensemble_configs():
        if cfg["market"] not in markets:
            return None
        spec = markets[cfg["market"]]
        train = run_backtest(spec["groups"], spec["peer"], spec, cfg, end_day=split)
        hold = run_backtest(spec["groups"], spec["peer"], spec, cfg, start_day=split)
        full = run_backtest(spec["groups"], spec["peer"], spec, cfg)
        eval_stop = run_backtest(spec["groups"], spec["peer"], spec, cfg, stop_on_event=True)
        attach_eval_stop(full, eval_stop)
        stress = run_backtest(spec["groups"], spec["peer"], stressed_spec(spec), cfg)
        attach_stress(full, stress)
        attach_path_stress(full, path_order_stress(full["daily"], spec["days"]))
        attach_neighbors(full, evaluate_neighbors(spec, cfg))
        roll = rolling_30d_from_daily(full["daily"], spec["days"])
        members.append({"score": score_final(train, hold, full, roll), "cfg": cfg, "train": train, "hold": hold, "full": full, "roll": roll})
    return evaluate_ensemble(members, all_days, split)
