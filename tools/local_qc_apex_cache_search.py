import argparse
import ast
import collections
import pathlib
import sys
from datetime import date

import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT = ROOT / "qc_lean" / "qc_apex_es_vwap_orb"
CACHE = ROOT / "research" / "ta_strat" / "cache"

sys.path.insert(0, str(PROJECT))

from apex_core import signal_for, simulate_trade, summarize
from apex_ensemble import attach_path_stress, build_configs, build_ensembles, evaluate_ensemble, path_order_stress, rolling_30d_from_daily
from apex_format import format_compact, format_ensemble
from apex_locked import active_config_ok, locked_ensemble_configs
from apex_score import apex_ready_ok, apex_ready_reason, robust_ok, robust_reason, score_final, score_train, train_candidate_ok


DEFAULT_NAMES = {
    "opening_impulse_momentum_q",
    "opening_impulse_fade_q",
    "orb_break",
    "orb_fade",
    "prior_sweep",
    "daily_breakout_go",
    "balanced_prior_breakout_q",
    "prev_day_continuation",
    "opening_drive_pullback",
    "first_hour_retest_q",
    "vwap_trend_pullback_q",
    "failed_break_peer_reversal_q",
}


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
        raise RuntimeError(f"{', '.join(sorted(missing))} not found in {path}")
    ns = {}
    if globals_dict:
        ns.update(globals_dict)
    exec(compile(ast.Module(body=assigns + funcs, type_ignores=[]), str(path), "exec"), ns)
    return {name: ns[name] for name in names}


MAIN_FUNCS = load_functions(
    PROJECT / "main.py",
    [
        "SymbolState",
        "is_rth_time",
        "prepare_rows",
        "group_rows",
        "run_backtest",
        "risk_adjusted_cfg",
        "effective_daily_loss_stop",
        "day_regime_ok",
        "macro_event_day",
        "stressed_spec",
        "attach_eval_stop",
        "attach_stress",
        "select_shortlist",
    ],
    globals_dict={"signal_for": signal_for, "simulate_trade": simulate_trade, "summarize": summarize},
    global_names=["MAJOR_EVENT_DATES"],
)


class Bar:
    def __init__(self, row):
        self.end_time = row.dt_ny.to_pydatetime()
        self.open = row.open
        self.high = row.high
        self.low = row.low
        self.close = row.close
        self.volume = row.volume


def parse_day(value):
    y, m, d = value.split("-")
    return date(int(y), int(m), int(d))


def load_market(name):
    path = CACHE / f"apex3yr_{name.lower()}.csv"
    session_start = 9 * 60 + 30
    session_end = 16 * 60
    if path.exists():
        df = pd.read_csv(path)
        df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    elif name.lower() == "mcl":
        df = pd.read_csv(CACHE / "cl_1m_3y.csv")
        df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
        df = (
            df.set_index("dt_utc")
            .resample("5min")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna()
            .reset_index()
        )
        session_start = 9 * 60
        session_end = 14 * 60 + 30
    else:
        raise FileNotFoundError(path)
    df["dt_ny"] = df["dt_utc"].dt.tz_convert("America/New_York")
    state = MAIN_FUNCS["SymbolState"](session_start, session_end)
    rows = []
    for row in df.itertuples(index=False):
        out = state.update(Bar(row))
        if MAIN_FUNCS["is_rth_time"](out["time"], session_start, session_end):
            rows.append(out)
    return MAIN_FUNCS["prepare_rows"](rows)


def build_market_spec(rows, peer_rows, point, fee):
    groups = MAIN_FUNCS["group_rows"](rows)
    return {
        "groups": groups,
        "days": sorted(groups),
        "peer": {r["time"]: r for r in peer_rows},
        "point": point,
        "tick": 0.25,
        "fee": fee,
        "slip": 1.0,
    }


def interleaved_configs(cfgs, limit):
    buckets = {}
    for cfg in cfgs:
        key = (cfg["market"], cfg["name"])
        buckets.setdefault(key, []).append(cfg)
    keys = sorted(buckets)
    selected = []
    while keys and len(selected) < limit:
        next_keys = []
        for key in keys:
            bucket = buckets[key]
            if bucket:
                selected.append(bucket.pop(0))
                if len(selected) >= limit:
                    break
            if bucket:
                next_keys.append(key)
        keys = next_keys
    return selected


def attach_light_neighbors(full):
    full["neighbor_total"] = 0
    full["neighbor_profitable"] = 0
    full["neighbor_passed"] = 0
    full["neighbor_breached"] = 0
    full["neighbor_min_profit"] = 0.0
    full["neighbor_avg_profit"] = 0.0


def train_reason(m):
    active = m.get("active_days", m["trades"])
    best_day_share = m.get("best_day_share", 0.0)
    best_trade_share = m.get("best_trade_share", 0.0)
    daily = m.get("daily", {})
    months = {}
    days = sorted(daily)
    for d, pnl in daily.items():
        if abs(pnl) <= 1e-9:
            continue
        key = (d.year, d.month) if hasattr(d, "year") else d // 22
        months[key] = months.get(key, 0.0) + pnl
    month_values = list(months.values())
    month_profit = sum(month_values)
    month_best_share = max(0.0, max(month_values)) / max(month_profit, 1.0) if month_profit > 0.0 and month_values else 0.0
    mid = max(1, len(days) // 2) if days else 0
    first_days = days[:mid]
    second_days = days[mid:]
    first = sum(daily.get(d, 0.0) for d in first_days)
    second = sum(daily.get(d, 0.0) for d in second_days)
    first_active = sum(1 for d in first_days if abs(daily.get(d, 0.0)) > 1e-9)
    second_active = sum(1 for d in second_days if abs(daily.get(d, 0.0)) > 1e-9)
    split_min = min(first, second) if days else 0.0
    split_best_share = max(0.0, max(first, second)) / max(first + second, 1.0) if first + second > 0.0 else 0.0
    if m["trades"] < 10:
        return "low_trades"
    if active < 6:
        return "low_active"
    if m["profit"] <= 100.0:
        return "low_profit"
    if len(month_values) < 3 or sum(1 for v in month_values if v > 0.0) < 2:
        return "weak_months"
    if month_best_share > 0.75:
        return "month_concentration"
    if min(month_values) <= -1200.0:
        return "bad_month"
    if min(first_active, second_active) == 0:
        return "one_half_inactive"
    if split_min <= -1200.0:
        return "bad_split"
    if m["profit"] > 0 and split_best_share > 0.95:
        return "split_concentration"
    if m["eval_pass"]:
        if best_day_share > 0.65 or best_trade_share > 0.55:
            return "lucky_train_pass"
        return "ok"
    if m["trades"] < 14:
        return "low_trades_no_pass"
    if active < 8:
        return "low_active_no_pass"
    if m["worst_day"] <= -1000.0:
        return "bad_day"
    if m["max_dd"] <= -2600.0:
        return "bad_dd"
    if m["profit"] <= -1200.0:
        return "big_loss"
    if m["pf"] < 0.85:
        return "low_pf"
    if m["profit"] > 0 and best_day_share > 0.60:
        return "day_concentration"
    if m["profit"] > 0 and best_trade_share > 0.45:
        return "trade_concentration"
    return "ok"


def evaluate(markets, cfg, split, start_day, end_day, require_train=True):
    spec = markets[cfg["market"]]
    train = MAIN_FUNCS["run_backtest"](spec["groups"], spec["peer"], spec, cfg, start_day=start_day, end_day=split)
    if require_train and not train_candidate_ok(train):
        return None
    hold = MAIN_FUNCS["run_backtest"](spec["groups"], spec["peer"], spec, cfg, start_day=split, end_day=end_day)
    full = MAIN_FUNCS["run_backtest"](spec["groups"], spec["peer"], spec, cfg, start_day=start_day, end_day=end_day)
    eval_stop = MAIN_FUNCS["run_backtest"](spec["groups"], spec["peer"], spec, cfg, start_day=start_day, end_day=end_day, stop_on_event=True)
    MAIN_FUNCS["attach_eval_stop"](full, eval_stop)
    stress = MAIN_FUNCS["run_backtest"](spec["groups"], spec["peer"], MAIN_FUNCS["stressed_spec"](spec), cfg, start_day=start_day, end_day=end_day)
    MAIN_FUNCS["attach_stress"](full, stress)
    attach_path_stress(full, path_order_stress(full["daily"], [d for d in spec["days"] if start_day <= d <= end_day]))
    attach_light_neighbors(full)
    roll = rolling_30d_from_daily(full["daily"], [d for d in spec["days"] if start_day <= d <= end_day])
    return (score_final(train, hold, full, roll), cfg, train, hold, full, roll)


def main():
    parser = argparse.ArgumentParser(description="Local cached-data preflight for qc_apex_es_vwap_orb ES/NQ candidates.")
    parser.add_argument("--start", default="2025-03-24")
    parser.add_argument("--end", default="2026-03-23")
    parser.add_argument("--markets", default="ES,NQ")
    parser.add_argument("--names", default=",".join(sorted(DEFAULT_NAMES)))
    parser.add_argument("--max-configs", type=int, default=12000)
    parser.add_argument("--shortlist", type=int, default=160)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--fallback-rejected", type=int, default=80)
    parser.add_argument("--active-qc-filter", action="store_true", help="Use main.py active_config_ok so local preflight matches the QC project filter.")
    parser.add_argument("--locked-only", action="store_true", help="Evaluate the locked QC ensemble and exit.")
    parser.add_argument(
        "--groups",
        default="apex_ready,robust,eval_pass,top,ensemble_apex_ready,ensemble_robust,ensemble_eval_pass,ensemble_top",
        help="Comma-separated output groups to print; use summary for no result rows.",
    )
    args = parser.parse_args()

    start_day = parse_day(args.start)
    end_day = parse_day(args.end)
    names = {x for x in args.names.split(",") if x}
    market_names = {x for x in args.markets.split(",") if x}
    print_groups = {x.strip() for x in args.groups.split(",") if x.strip()}

    locked_market_names = {cfg["market"] for cfg in locked_ensemble_configs()} if args.locked_only else market_names
    es = load_market("es")
    nq = load_market("nq")
    mcl = load_market("mcl") if "MCL" in locked_market_names else []
    markets = {
        "ES": build_market_spec(es, nq, 50.0, 4.0),
        "NQ": build_market_spec(nq, es, 20.0, 4.0),
        "MES": build_market_spec(es, nq, 5.0, 4.0),
        "MNQ": build_market_spec(nq, es, 2.0, 4.0),
        "MCL": build_market_spec(mcl, [], 100.0, 2.0) if mcl else None,
    }
    markets = {k: v for k, v in markets.items() if v is not None}
    day_sets = [set(markets[k]["days"]) for k in locked_market_names if k in markets]
    all_days = sorted(d for d in set().union(*day_sets) if start_day <= d <= end_day)
    split = all_days[int(len(all_days) * 0.62)]

    if args.locked_only:
        members = []
        for cfg in locked_ensemble_configs():
            item = evaluate(markets, cfg, split, start_day, end_day, require_train=False)
            if item is None:
                raise RuntimeError(f"locked member failed: {cfg['market']} {cfg['name']}")
            score, cfg, train, hold, full, roll = item
            members.append({"score": score, "cfg": cfg, "train": train, "hold": hold, "full": full, "roll": roll})
        ensemble = evaluate_ensemble(members, all_days, split)
        _, members, train, hold, full, roll = ensemble
        reason = apex_ready_reason(hold, full, roll, 60)
        print(f"LOCAL_LOCKED_SUMMARY days={len(all_days)} split={split} reason={reason}")
        print(format_ensemble(f"LOCAL_LOCKED_FINAL reason={reason}", 1, members, train, hold, full, roll))
        return

    cfgs = [cfg for cfg in build_configs() if cfg["market"] in market_names and cfg["name"] in names]
    if args.active_qc_filter:
        cfgs = [cfg for cfg in cfgs if active_config_ok(cfg)]
    cfgs = interleaved_configs(cfgs, args.max_configs)
    print(f"LOCAL_START days={len(all_days)} split={split} markets={','.join(sorted(market_names))} cfgs={len(cfgs)} names={len(names)}")

    trained = []
    rejected = []
    reason_counts = collections.Counter()
    for idx, cfg in enumerate(cfgs, 1):
        spec = markets[cfg["market"]]
        train = MAIN_FUNCS["run_backtest"](spec["groups"], spec["peer"], spec, cfg, start_day=start_day, end_day=split)
        reason = train_reason(train)
        reason_counts[reason] += 1
        if reason == "ok" and train_candidate_ok(train):
            trained.append((score_train(train), cfg, train))
        else:
            rejected.append((score_train(train), cfg, train, reason))
        if idx % 2000 == 0:
            print(f"LOCAL_PROGRESS train_tested={idx} trainq={len(trained)}")

    trained.sort(key=lambda x: x[0], reverse=True)
    shortlist = MAIN_FUNCS["select_shortlist"](trained, args.shortlist)
    using_fallback = False
    if not shortlist and args.fallback_rejected > 0:
        rejected.sort(key=lambda x: x[0], reverse=True)
        shortlist = [(score, cfg, train) for score, cfg, train, reason in rejected[:args.fallback_rejected]]
        using_fallback = True
        print("LOCAL_FALLBACK rejected_top_used=" + str(len(shortlist)))
    print("LOCAL_TRAIN_REASONS " + " ".join(f"{k}={v}" for k, v in reason_counts.most_common(12)))
    evaluated = []
    for idx, (_, cfg, _) in enumerate(shortlist, 1):
        item = evaluate(markets, cfg, split, start_day, end_day, require_train=not using_fallback)
        if item:
            evaluated.append(item)
        if idx % 40 == 0:
            print(f"LOCAL_PROGRESS eval_tested={idx} evaluated={len(evaluated)}")

    evaluated.sort(key=lambda x: x[0], reverse=True)
    robust = [x for x in evaluated if robust_ok(x[3], x[4], x[5], 48)]
    apex_ready = [x for x in evaluated if apex_ready_ok(x[3], x[4], x[5], 48)]
    eval_pass = [x for x in evaluated if x[4].get("eval_stop_pass") and not x[4].get("eval_stop_breached")]
    ensembles = build_ensembles(evaluated, all_days, split) if evaluated else []
    ensemble_eval_pass = [x for x in ensembles if x[4].get("eval_stop_pass") and not x[4].get("eval_stop_breached")]
    ensemble_robust = [x for x in ensembles if robust_ok(x[3], x[4], x[5], 60)]
    ensemble_apex_ready = [x for x in ensembles if apex_ready_ok(x[3], x[4], x[5], 60)]
    print(f"LOCAL_SUMMARY trainq={len(trained)} shortlist={len(shortlist)} evaluated={len(evaluated)} eval_pass={len(eval_pass)} robust={len(robust)} apex_ready={len(apex_ready)} ensembles={len(ensembles)} ensemble_eval_pass={len(ensemble_eval_pass)} ensemble_robust={len(ensemble_robust)} ensemble_apex_ready={len(ensemble_apex_ready)}")

    single_groups = [
        ("apex_ready", "LOCAL_APEX_READY", apex_ready),
        ("robust", "LOCAL_ROBUST", robust),
        ("eval_pass", "LOCAL_EVAL_PASS", eval_pass),
        ("top", "LOCAL_TOP", evaluated),
    ]
    for group, tag, rows in single_groups:
        if group not in print_groups:
            continue
        for rank, item in enumerate(rows[:args.top], 1):
            _, cfg, train, hold, full, roll = item
            reason = apex_ready_reason(hold, full, roll, 48) if tag == "LOCAL_APEX_READY" else robust_reason(hold, full, roll, 48)
            print(format_compact(f"{tag} reason={reason}", rank, cfg, train, hold, full, roll))

    ensemble_groups = [
        ("ensemble_apex_ready", "LOCAL_ENSEMBLE_APEX_READY", ensemble_apex_ready),
        ("ensemble_robust", "LOCAL_ENSEMBLE_ROBUST", ensemble_robust),
        ("ensemble_eval_pass", "LOCAL_ENSEMBLE_EVAL_PASS", ensemble_eval_pass),
        ("ensemble_top", "LOCAL_ENSEMBLE_TOP", ensembles),
    ]
    for group, tag, rows in ensemble_groups:
        if group not in print_groups:
            continue
        for rank, item in enumerate(rows[:args.top], 1):
            _, members, train, hold, full, roll = item
            reason = apex_ready_reason(hold, full, roll, 60) if tag == "LOCAL_ENSEMBLE_APEX_READY" else robust_reason(hold, full, roll, 60)
            print(format_ensemble(f"{tag} reason={reason}", rank, members, train, hold, full, roll))


if __name__ == "__main__":
    main()
