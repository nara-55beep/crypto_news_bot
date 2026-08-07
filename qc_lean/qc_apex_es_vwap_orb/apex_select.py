from apex_format import format_compact, format_ensemble, member_text


def select_final_candidate(apex_ready_ensembles, apex_ready_passes, robust_ensemble_passes, robust_passes, ensemble_eval_passes, eval_passes, ensembles, evaluated):
    order = [
        ("ensemble_apex_ready", "ensemble", apex_ready_ensembles),
        ("single_apex_ready", "single", apex_ready_passes),
        ("ensemble_robust", "ensemble", robust_ensemble_passes),
        ("single_robust", "single", robust_passes),
        ("ensemble_eval_stop", "ensemble", ensemble_eval_passes),
        ("single_eval_stop", "single", eval_passes),
    ]
    for kind, shape, items in order:
        if items:
            return kind, shape, items[0]
    return "none", "none", None


def emit_final_selection(algo, evaluated, ensembles, apex_ready_passes, apex_ready_ensembles, robust_passes, robust_ensemble_passes, eval_passes, ensemble_eval_passes):
    kind, shape, item = select_final_candidate(
        apex_ready_ensembles,
        apex_ready_passes,
        robust_ensemble_passes,
        robust_passes,
        ensemble_eval_passes,
        eval_passes,
        ensembles,
        evaluated,
    )
    if item is None:
        algo.debug("SEARCH_FINAL kind=none reason=no_clean_eval_stop_pass")
        algo.set_runtime_statistic("Final Kind", "none")
        algo.set_runtime_statistic("Final Status", "NO CLEAN APEX PASS")
        return

    if shape == "ensemble":
        _, members, train, hold, full, roll = item
        label = " > ".join(member_text(m["cfg"]) for m in members)
        algo.debug(format_ensemble(f"SEARCH_FINAL kind={kind}", 1, members, train, hold, full, roll))
        algo.set_runtime_statistic("Final Type", "ensemble")
    else:
        _, cfg, train, hold, full, roll = item
        label = f"{cfg['market']} {cfg['name']}"
        algo.debug(format_compact(f"SEARCH_FINAL kind={kind}", 1, cfg, train, hold, full, roll))
        algo.set_runtime_statistic("Final Type", "single")

    algo.set_runtime_statistic("Final Kind", kind)
    algo.set_runtime_statistic("Final Status", "CLEAN APEX PASS")
    algo.set_runtime_statistic("Final Strategy", label)
    algo.set_runtime_statistic("Final Eval Stop", "YES" if full.get("eval_stop_pass", full["eval_pass"]) and not full.get("eval_stop_breached", False) else "NO")
    algo.set_runtime_statistic("Final Eval P&L", f"${full.get('eval_stop_profit', full['profit']):,.0f}")
    algo.set_runtime_statistic("Final Full P&L", f"${full['profit']:,.0f}")
    algo.set_runtime_statistic("Final Trades", str(full["trades"]))
    algo.set_runtime_statistic("Final PF", f"{full['pf']:.2f}")
    algo.set_runtime_statistic("Final MaxDD", f"${full['max_dd']:,.0f}")
    algo.set_runtime_statistic("Final Rolling", f"{roll['passed']}/{roll['total']}")
