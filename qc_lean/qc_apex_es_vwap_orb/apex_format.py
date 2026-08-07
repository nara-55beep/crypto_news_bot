def cfg_underlying(cfg):
    groups = {
        "ES": "ES",
        "MES": "ES",
        "NQ": "NQ",
        "MNQ": "NQ",
        "YM": "YM",
        "MYM": "YM",
        "RTY": "RTY",
        "M2K": "RTY",
        "CL": "CL",
        "MCL": "CL",
        "GC": "GC",
        "MGC": "GC",
    }
    return groups.get(cfg["market"], cfg["market"])


def fmt_money(x):
    return f"{x:.0f}"


def fmt_optional(x):
    return "None" if x is None else fmt_money(x)


def member_text(cfg):
    direct = (
        f":mi{cfg.get('min_impulse_atr', 0.0)}:pm{cfg.get('peer_mode', 'none')}:dm{cfg.get('daily_mode', 'none')}"
        if "min_impulse_atr" in cfg or "peer_mode" in cfg or "daily_mode" in cfg
        else ""
    )
    return (
        f"{cfg['market']}:{cfg['name']}:{cfg['side']}:st{cfg.get('entry_start', 0)}:e{cfg['entry_end']}:"
        f"s{cfg['stop_atr']}:r{cfg['rr']}:f{cfg['filter']}:"
        f"or{cfg['or_bars'] * 5}:risk{fmt_money(cfg['risk_usd'])}:"
        f"rp{cfg.get('risk_profile', 'fixed')}:"
        f"ev{cfg.get('event_filter', 'none')}:"
        f"m2{cfg.get('second_trade_mode', 'any')}:dp{fmt_money(cfg.get('daily_profit_stop', 9999))}:dl{fmt_money(cfg.get('daily_loss_stop', 1000))}:cd{cfg.get('loss_cooldown', 0)}:tr{cfg.get('trail_r', 1.0)}:tm{cfg.get('target_mode', 'fixed')}:pl{cfg.get('profit_lock_r', 0.0)}:reg{cfg.get('regime', 'all')}{direct}"
    )


def cfg_text(cfg):
    start = cfg.get("entry_start", 0)
    start_h = start // 60
    start_m = start % 60
    end_h = cfg["entry_end"] // 60
    end_m = cfg["entry_end"] % 60
    direct = (
        f" mi={cfg.get('min_impulse_atr', 0.0)} peer={cfg.get('peer_mode', 'none')} daily={cfg.get('daily_mode', 'none')}"
        if "min_impulse_atr" in cfg or "peer_mode" in cfg or "daily_mode" in cfg
        else ""
    )
    return (
        f"{cfg['market']} {cfg['name']} side={cfg['side']} start={start_h:02d}:{start_m:02d} end={end_h:02d}:{end_m:02d} "
        f"stop={cfg['stop_mode']}:{cfg['stop_atr']} rr={cfg['rr']} filter={cfg['filter']} "
        f"or={cfg['or_bars'] * 5}m risk={cfg['risk_usd']} maxc={cfg['max_contracts']} "
        f"rp={cfg.get('risk_profile', 'fixed')} ev={cfg.get('event_filter', 'none')} maxt={cfg['max_trades_day']} dp={fmt_money(cfg.get('daily_profit_stop', 9999))} "
        f"m2={cfg.get('second_trade_mode', 'any')} dl={fmt_money(cfg.get('daily_loss_stop', 1000))} cd={cfg.get('loss_cooldown', 0)} tr={cfg.get('trail_r', 1.0)} tm={cfg.get('target_mode', 'fixed')} pl={cfg.get('profit_lock_r', 0.0)} regime={cfg.get('regime', 'all')}{direct}"
    )


def metric_text(prefix, m):
    stress = ""
    if "stress_profit" in m:
        stress = (
            f" {prefix}_stress={fmt_money(m['stress_profit'])}"
            f" {prefix}_stresspf={m.get('stress_pf', 0.0):.2f}"
            f" {prefix}_stressbr={m.get('stress_breached', False)}"
        )
    eval_stop = ""
    if "eval_stop_pass" in m:
        eval_stop = (
            f" {prefix}_evalstop={m.get('eval_stop_pass', False)}"
            f" {prefix}_evalpnl={fmt_money(m.get('eval_stop_profit', 0.0))}"
            f" {prefix}_evalcush={fmt_money(m.get('eval_stop_cushion', 2000.0))}"
            f" {prefix}_evaltrades={m.get('eval_stop_trades', 0)}"
            f" {prefix}_evaldays={m.get('eval_stop_active_days', m.get('eval_stop_trades', 0))}"
            f" {prefix}_evalcons={m.get('eval_stop_consistency_share', 0.0)*100:.0f}%"
            f" {prefix}_evalbtrade={m.get('eval_stop_best_trade_share', 0.0)*100:.0f}%"
            f" {prefix}_evalday={m.get('eval_stop_day')}"
        )
    neighbor = ""
    if m.get("neighbor_total", 0):
        neighbor = (
            f" {prefix}_nbr={m.get('neighbor_profitable', 0)}/{m.get('neighbor_total', 0)}"
            f" {prefix}_nbrpass={m.get('neighbor_passed', 0)}"
            f" {prefix}_nbrmin={fmt_money(m.get('neighbor_min_profit', 0.0))}"
        )
    path = ""
    if "path_profit" in m:
        path = (
            f" {prefix}_path={fmt_money(m.get('path_profit', 0.0))}"
            f" {prefix}_pathdd={fmt_money(m.get('path_max_dd', 0.0))}"
            f" {prefix}_pathbr={m.get('path_breached', False)}"
        )
    return (
        f"{prefix}_pass={m['eval_pass']} {prefix}_profit={fmt_money(m['profit'])} "
        f"{prefix}_trades={m['trades']} {prefix}_days={m.get('active_days', m['trades'])} {prefix}_wr={m['win_rate']:.1f} "
        f"{prefix}_pf={m['pf']:.2f} {prefix}_dd={fmt_money(m['max_dd'])} "
        f"{prefix}_cushion={fmt_money(m.get('min_cushion', 2000.0))} {prefix}_worst={fmt_money(m['worst_day'])} {prefix}_bestday={fmt_money(m.get('best_day', 0.0))} "
        f"{prefix}_cons={m.get('consistency_share', m.get('best_day_share', 0.0))*100:.0f}% {prefix}_bday={m.get('best_day_share', 0.0)*100:.0f}% "
        f"{prefix}_btrade={m.get('best_trade_share', 0.0)*100:.0f}% {prefix}_passday={m['pass_day']} "
        f"{prefix}_passp={fmt_optional(m['pass_profit'])} {prefix}_breachday={m['breach_day']} "
        f"{prefix}_breachp={fmt_optional(m['breach_profit'])}{stress}{eval_stop}{path}{neighbor}"
    )


def format_result(tag, idx, cfg, train, hold, full, roll):
    return (
        f"{tag} rank={idx} cfg='{cfg_text(cfg)}' "
        f"{metric_text('train', train)} | {metric_text('hold', hold)} | {metric_text('full', full)} | "
        f"roll={roll['passed']}/{roll['total']} roll_passrate={roll['pass_rate']*100:.1f} "
        f"roll_breach={roll['breached']} roll_breachrate={roll['breach_rate']*100:.1f} "
        f"roll_avg={fmt_money(roll['avg_profit'])} roll_pnl={fmt_money(roll['min_profit'])}..{fmt_money(roll['max_profit'])} "
        f"roll_wdd={fmt_money(roll['worst_dd'])} roll_cushion={fmt_money(roll.get('min_cushion', 2000.0))} rb={roll.get('pass_bucket_pos', 0)} rcl={roll.get('pass_bucket_max_share', 0.0)*100:.0f}% q={roll['q_pos']}/4 qmin={fmt_money(roll['q_min'])} "
        f"m={roll.get('m_pos', 0)}/{roll.get('m_total', 0)} mmin={fmt_money(roll.get('m_min', 0.0))} "
        f"wf={roll.get('wf_pos', 0)}/{roll.get('wf_total', 0)} wfmin={fmt_money(roll.get('wf_min', 0.0))} wfbr={roll.get('wf_breached', 0)}"
    )


def format_compact(tag, idx, cfg, train, hold, full, roll):
    direct = (
        f"mi={cfg.get('min_impulse_atr', 0.0)} peer={cfg.get('peer_mode', 'none')} daily={cfg.get('daily_mode', 'none')} "
        if "min_impulse_atr" in cfg or "peer_mode" in cfg or "daily_mode" in cfg
        else ""
    )
    return (
        f"{tag} rank={idx} market={cfg['market']} strat={cfg['name']} side={cfg['side']} "
        f"start={cfg.get('entry_start', 0)} end={cfg['entry_end']} stop={cfg['stop_mode']}:{cfg['stop_atr']} rr={cfg['rr']} "
        f"filter={cfg['filter']} or={cfg['or_bars'] * 5} risk={cfg['risk_usd']} "
        f"rp={cfg.get('risk_profile', 'fixed')} ev={cfg.get('event_filter', 'none')} maxc={cfg['max_contracts']} maxt={cfg['max_trades_day']} "
        f"m2={cfg.get('second_trade_mode', 'any')} dp={fmt_money(cfg.get('daily_profit_stop', 9999))} dl={fmt_money(cfg.get('daily_loss_stop', 1000))} cd={cfg.get('loss_cooldown', 0)} tr={cfg.get('trail_r', 1.0)} tm={cfg.get('target_mode', 'fixed')} pl={cfg.get('profit_lock_r', 0.0)} regime={cfg.get('regime', 'all')} {direct}"
        f"full_pass={full['eval_pass']} full_pnl={fmt_money(full['profit'])} "
        f"full_trades={full['trades']} full_days={full.get('active_days', full['trades'])} "
        f"full_pf={full['pf']:.2f} full_dd={fmt_money(full['max_dd'])} cushion={fmt_money(full.get('min_cushion', 2000.0))} "
        f"bestday={fmt_money(full.get('best_day', 0.0))} cons={full.get('consistency_share', full.get('best_day_share', 0.0))*100:.0f}% "
        f"bday={full.get('best_day_share', 0.0)*100:.0f}% btrade={full.get('best_trade_share', 0.0)*100:.0f}% "
        f"stress={fmt_money(full.get('stress_profit', 0.0))} stresspf={full.get('stress_pf', 0.0):.2f} stressbr={full.get('stress_breached', False)} "
        f"evalstop={full.get('eval_stop_pass', False)} evalpnl={fmt_money(full.get('eval_stop_profit', 0.0))} evalcush={fmt_money(full.get('eval_stop_cushion', 2000.0))} evaltrades={full.get('eval_stop_trades', 0)} evaldays={full.get('eval_stop_active_days', full.get('eval_stop_trades', 0))} evalcons={full.get('eval_stop_consistency_share', 0.0)*100:.0f}% evalbtrade={full.get('eval_stop_best_trade_share', 0.0)*100:.0f}% evalday={full.get('eval_stop_day')} "
        f"path={fmt_money(full.get('path_profit', 0.0))} pathdd={fmt_money(full.get('path_max_dd', 0.0))} pathbr={full.get('path_breached', False)} "
        f"nbr={full.get('neighbor_profitable', 0)}/{full.get('neighbor_total', 0)} nbrpass={full.get('neighbor_passed', 0)} nbrmin={fmt_money(full.get('neighbor_min_profit', 0.0))} "
        f"passday={full['pass_day']} passp={fmt_optional(full['pass_profit'])} "
        f"breachday={full['breach_day']} breachp={fmt_optional(full['breach_profit'])} "
        f"roll={roll['passed']}/{roll['total']} rollpr={roll['pass_rate']*100:.1f} "
        f"rollbr={roll['breach_rate']*100:.1f} rollavg={fmt_money(roll['avg_profit'])} "
        f"rollwdd={fmt_money(roll['worst_dd'])} rollcush={fmt_money(roll.get('min_cushion', 2000.0))} rb={roll.get('pass_bucket_pos', 0)} rcl={roll.get('pass_bucket_max_share', 0.0)*100:.0f}% q={roll['q_pos']}/4 qmin={fmt_money(roll['q_min'])} "
        f"m={roll.get('m_pos', 0)}/{roll.get('m_total', 0)} mmin={fmt_money(roll.get('m_min', 0.0))} "
        f"wf={roll.get('wf_pos', 0)}/{roll.get('wf_total', 0)} wfmin={fmt_money(roll.get('wf_min', 0.0))} wfbr={roll.get('wf_breached', 0)}"
    )


def format_ensemble(tag, idx, members, train, hold, full, roll):
    member_cfgs = " > ".join(member_text(m["cfg"]) for m in members)
    return (
        f"{tag} rank={idx} members='{member_cfgs}' "
        f"{metric_text('train', train)} | {metric_text('hold', hold)} | {metric_text('full', full)} | "
        f"roll={roll['passed']}/{roll['total']} roll_passrate={roll['pass_rate']*100:.1f} "
        f"roll_breach={roll['breached']} roll_breachrate={roll['breach_rate']*100:.1f} "
        f"roll_avg={fmt_money(roll['avg_profit'])} roll_pnl={fmt_money(roll['min_profit'])}..{fmt_money(roll['max_profit'])} "
        f"roll_wdd={fmt_money(roll['worst_dd'])} roll_cushion={fmt_money(roll.get('min_cushion', 2000.0))} rb={roll.get('pass_bucket_pos', 0)} rcl={roll.get('pass_bucket_max_share', 0.0)*100:.0f}% q={roll['q_pos']}/4 qmin={fmt_money(roll['q_min'])} "
        f"m={roll.get('m_pos', 0)}/{roll.get('m_total', 0)} mmin={fmt_money(roll.get('m_min', 0.0))} "
        f"wf={roll.get('wf_pos', 0)}/{roll.get('wf_total', 0)} wfmin={fmt_money(roll.get('wf_min', 0.0))} wfbr={roll.get('wf_breached', 0)}"
    )
