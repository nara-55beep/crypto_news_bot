# region imports
from AlgorithmImports import *
from datetime import timedelta
from apex_ensemble import attach_path_stress, build_configs, build_ensembles, path_order_stress, rolling_30d_from_daily
from apex_format import format_compact, format_ensemble, format_result
from apex_core import signal_for, simulate_trade, summarize
from apex_locked import ACTIVE_MARKETS, ACTIVE_STRATEGY_NAMES, active_config_ok, evaluate_locked_ensemble
from apex_score import apex_ready_ok, apex_ready_reason, format_reason_summary, robust_ok, robust_reason, score_final, score_train, train_candidate_ok
from apex_select import emit_final_selection
# endregion

MIN_SINGLE_TRADES = 48
MIN_ENSEMBLE_TRADES = 60


class ApexStrategySearch(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2025, 3, 24)
        self.set_end_date(2026, 3, 23)
        self.set_cash(50000)
        self.set_time_zone(TimeZones.NEW_YORK)

        self.rows = {}
        self.market_specs = futures_specs()
        self.states = {k: SymbolState(v["session_start"], v["session_end"]) for k, v in self.market_specs.items()}

        for key, spec in self.market_specs.items():
            self.add_continuous_future(key, spec["ticker"])

        self.debug(
            f"SEARCH_START streams={','.join(sorted(self.market_specs))} "
            f"active_markets={','.join(sorted(ACTIVE_MARKETS))} "
            f"active_names={','.join(sorted(ACTIVE_STRATEGY_NAMES))} "
            f"mode={'search' if self.search_enabled() else 'locked'} Apex 50K 2025-03-24..2026-03-23"
        )

    def search_enabled(self):
        value = str(self.get_parameter("search") or "").strip().lower()
        return value in ("1", "true", "yes", "on")

    def add_continuous_future(self, key, ticker):
        future = self.add_future(
            ticker,
            Resolution.MINUTE,
            extended_market_hours=True,
            data_mapping_mode=DataMappingMode.OPEN_INTEREST,
            data_normalization_mode=DataNormalizationMode.BACKWARDS_RATIO,
            contract_depth_offset=0,
        )
        future.set_filter(timedelta(0), timedelta(days=182))
        con = TradeBarConsolidator(timedelta(minutes=5))
        con.data_consolidated += (lambda sender, bar, k=key: self.on_5m(k, bar))
        self.subscription_manager.add_consolidator(future.symbol, con)

    def on_data(self, data: Slice):
        pass

    def on_5m(self, key, bar):
        row = self.states[key].update(bar)
        if not self.is_rth(key, row):
            return
        self.rows.setdefault(key, []).append(row)

    def is_rth(self, key, row):
        spec = self.market_specs[key]
        return is_rth_time(row["time"], spec["session_start"], spec["session_end"])

    def on_end_of_algorithm(self):
        prepared = {k: prepare_rows(self.rows.get(k, [])) for k in self.market_specs}
        available = {k: v for k, v in prepared.items() if len(v) >= 1000}
        if len(available) < 2:
            counts = " ".join(f"{k}:{len(v)}" for k, v in prepared.items())
            self.debug(f"SEARCH_ERROR not enough data subscribed={','.join(sorted(self.market_specs))} counts={counts}")
            return

        time_maps = {k: {r["time"]: r for r in rows} for k, rows in available.items()}
        markets = {}
        for key, spec in trade_specs().items():
            source = spec["source"]
            if source not in available:
                continue
            peer_key = spec.get("peer")
            groups = group_rows(available[source])
            markets[key] = {
                "groups": groups,
                "days": sorted(groups),
                "peer": time_maps.get(peer_key, {}),
                "point": spec["point"],
                "tick": spec["tick"],
                "fee": spec["fee"],
                "slip": spec["slip"],
            }

        all_days = sorted(set().union(*[{r["day"] for r in v} for v in available.values()]))
        split = all_days[int(len(all_days) * 0.62)]
        locked = evaluate_locked_ensemble(markets, all_days, split, run_backtest, stressed_spec, attach_neighbors, evaluate_neighbors, score_final)
        if locked is None:
            self.debug("LOCKED_ERROR missing locked market data")
            return

        _, locked_members, locked_train, locked_hold, locked_full, locked_roll = locked
        locked_reason = apex_ready_reason(locked_hold, locked_full, locked_roll, MIN_ENSEMBLE_TRADES)
        self.debug(format_ensemble(f"LOCKED_FINAL reason={locked_reason}", 1, locked_members, locked_train, locked_hold, locked_full, locked_roll))
        eval_clean = locked_full.get("eval_stop_pass") and not locked_full.get("eval_stop_breached", False)
        self.set_runtime_statistic("Final Type", "locked ensemble")
        self.set_runtime_statistic("Final Kind", "locked")
        self.set_runtime_statistic("Final Status", ("APEX TARGET PASSED; robustness=" + locked_reason) if eval_clean else locked_reason)
        self.set_runtime_statistic("Final Eval Stop", "YES" if eval_clean else "NO")
        self.set_runtime_statistic("Final Eval P&L", f"${locked_full.get('eval_stop_profit', locked_full['profit']):,.0f}")
        self.set_runtime_statistic("Final Full P&L", f"${locked_full['profit']:,.0f}")
        self.set_runtime_statistic("Final Trades", str(locked_full["trades"]))
        self.set_runtime_statistic("Final PF", f"{locked_full['pf']:.2f}")
        self.set_runtime_statistic("Final MaxDD", f"${locked_full['max_dd']:,.0f}")
        self.set_runtime_statistic("Final Rolling", f"{locked_roll['passed']}/{locked_roll['total']}")
        if not self.search_enabled():
            return

        cfgs = [
            cfg for cfg in build_configs()
            if cfg["market"] in markets
            and active_config_ok(cfg)
        ]

        train_ranked = []
        tested = 0
        train_qualified = 0
        for cfg in cfgs:
            spec = markets[cfg["market"]]
            m = run_backtest(spec["groups"], spec["peer"], spec, cfg, end_day=split)
            tested += 1
            if train_candidate_ok(m):
                train_qualified += 1
                train_ranked.append((score_train(m), cfg, m))

        train_ranked.sort(key=lambda x: x[0], reverse=True)
        shortlist = select_shortlist(train_ranked, 600)
        evaluated = []
        for _, cfg, train in shortlist:
            spec = markets[cfg["market"]]
            hold = run_backtest(spec["groups"], spec["peer"], spec, cfg, start_day=split)
            full = run_backtest(spec["groups"], spec["peer"], spec, cfg)
            eval_stop = run_backtest(spec["groups"], spec["peer"], spec, cfg, stop_on_event=True)
            attach_eval_stop(full, eval_stop)
            stress = run_backtest(spec["groups"], spec["peer"], stressed_spec(spec), cfg)
            attach_stress(full, stress)
            attach_path_stress(full, path_order_stress(full["daily"], spec["days"]))
            attach_neighbors(full, evaluate_neighbors(spec, cfg))
            roll = rolling_30d_from_daily(full["daily"], spec["days"])
            evaluated.append((score_final(train, hold, full, roll), cfg, train, hold, full, roll))

        evaluated.sort(key=lambda x: x[0], reverse=True)
        passes = [x for x in evaluated if x[4]["eval_pass"] and x[5]["passed"] > 0]
        eval_passes = [x for x in evaluated if x[4].get("eval_stop_pass") and not x[4].get("eval_stop_breached")]
        ensembles = build_ensembles(evaluated, all_days, split)
        ensemble_passes = [x for x in ensembles if x[3]["eval_pass"] and x[4]["passed"] > 0]
        ensemble_eval_passes = [x for x in ensembles if x[3].get("eval_stop_pass") and not x[3].get("eval_stop_breached")]
        robust_passes = [x for x in evaluated if robust_ok(x[3], x[4], x[5], MIN_SINGLE_TRADES)]
        robust_ensemble_passes = [x for x in ensembles if robust_ok(x[3], x[4], x[5], MIN_ENSEMBLE_TRADES)]
        apex_ready_passes = [x for x in evaluated if apex_ready_ok(x[3], x[4], x[5], MIN_SINGLE_TRADES)]
        apex_ready_ensembles = [x for x in ensembles if apex_ready_ok(x[3], x[4], x[5], MIN_ENSEMBLE_TRADES)]

        self.debug(
            f"SEARCH_SUMMARY streams={stream_counts(available)} days={len(all_days)} "
            f"markets={len(markets)} active_markets={','.join(sorted(ACTIVE_MARKETS))} "
            f"active_names={','.join(sorted(ACTIVE_STRATEGY_NAMES))} configs={tested} "
            f"trainq={train_qualified} shortlist={len(shortlist)} shortlist_mode=diverse train_filter=apex_sane pass={len(passes)} "
            f"eval_pass={len(eval_passes)} robust={len(robust_passes)} ensembles={len(ensembles)} "
            f"ensemble_pass={len(ensemble_passes)} ensemble_eval_pass={len(ensemble_eval_passes)} "
            f"apex_ready={len(apex_ready_passes)} ensemble_robust={len(robust_ensemble_passes)} "
            f"ensemble_apex_ready={len(apex_ready_ensembles)} split={split}"
        )
        self.debug(format_reason_summary("SEARCH_REASON", evaluated, MIN_SINGLE_TRADES))
        self.debug(format_reason_summary("SEARCH_APEX_REASON", evaluated, MIN_SINGLE_TRADES, apex_ready_reason))
        self.debug(format_reason_summary("SEARCH_ENSEMBLE_REASON", ensembles, MIN_ENSEMBLE_TRADES))
        self.debug(format_reason_summary("SEARCH_ENSEMBLE_APEX_REASON", ensembles, MIN_ENSEMBLE_TRADES, apex_ready_reason))

        for idx, item in enumerate(evaluated[:12], 1):
            _, cfg, train, hold, full, roll = item
            self.debug(format_result("SEARCH_TOP", idx, cfg, train, hold, full, roll))
            self.debug(format_compact("SEARCH_PICK", idx, cfg, train, hold, full, roll))

        for idx, item in enumerate(passes[:8], 1):
            _, cfg, train, hold, full, roll = item
            self.debug(format_result("SEARCH_PASS", idx, cfg, train, hold, full, roll))

        for idx, item in enumerate(eval_passes[:8], 1):
            _, cfg, train, hold, full, roll = item
            self.debug(format_result("SEARCH_EVAL_PASS", idx, cfg, train, hold, full, roll))

        for idx, item in enumerate(robust_passes[:8], 1):
            _, cfg, train, hold, full, roll = item
            self.debug(format_result("SEARCH_ROBUST_PASS", idx, cfg, train, hold, full, roll))

        for idx, item in enumerate(apex_ready_passes[:8], 1):
            _, cfg, train, hold, full, roll = item
            self.debug(format_result("SEARCH_APEX_READY", idx, cfg, train, hold, full, roll))

        for idx, item in enumerate(ensembles[:10], 1):
            _, members, train, hold, full, roll = item
            self.debug(format_ensemble("SEARCH_ENSEMBLE", idx, members, train, hold, full, roll))

        for idx, item in enumerate(ensemble_passes[:8], 1):
            _, members, train, hold, full, roll = item
            self.debug(format_ensemble("SEARCH_ENSEMBLE_PASS", idx, members, train, hold, full, roll))

        for idx, item in enumerate(ensemble_eval_passes[:8], 1):
            _, members, train, hold, full, roll = item
            self.debug(format_ensemble("SEARCH_ENSEMBLE_EVAL_PASS", idx, members, train, hold, full, roll))

        for idx, item in enumerate(robust_ensemble_passes[:8], 1):
            _, members, train, hold, full, roll = item
            self.debug(format_ensemble("SEARCH_ENSEMBLE_ROBUST_PASS", idx, members, train, hold, full, roll))

        for idx, item in enumerate(apex_ready_ensembles[:8], 1):
            _, members, train, hold, full, roll = item
            self.debug(format_ensemble("SEARCH_ENSEMBLE_APEX_READY", idx, members, train, hold, full, roll))

        emit_final_selection(self, evaluated, ensembles, apex_ready_passes, apex_ready_ensembles, robust_passes, robust_ensemble_passes, eval_passes, ensemble_eval_passes)


class SymbolState:
    def __init__(self, session_start=9 * 60 + 30, session_end=16 * 60):
        self.day = None
        self.rth_day = None
        self.session_start = session_start
        self.session_end = session_end
        self.ema9 = None
        self.ema20 = None
        self.atr14 = None
        self.prev_close = None
        self.cum_pv = 0.0
        self.cum_v = 0.0

    def update(self, bar):
        t = bar.end_time
        day = t.date()
        is_rth = is_rth_time(t, self.session_start, self.session_end)
        if self.day != day:
            self.day = day

        o = float(bar.open)
        h = float(bar.high)
        l = float(bar.low)
        c = float(bar.close)
        v = float(bar.volume)

        if is_rth:
            if self.rth_day != day:
                self.rth_day = day
                self.cum_pv = 0.0
                self.cum_v = 0.0

            self.ema9 = c if self.ema9 is None else self.ema9 + (2.0 / 10.0) * (c - self.ema9)
            self.ema20 = c if self.ema20 is None else self.ema20 + (2.0 / 21.0) * (c - self.ema20)

            tr = h - l if self.prev_close is None else max(h - l, abs(h - self.prev_close), abs(l - self.prev_close))
            self.atr14 = tr if self.atr14 is None else self.atr14 + (1.0 / 14.0) * (tr - self.atr14)
            self.prev_close = c

        tp = (h + l + c) / 3.0
        if is_rth:
            self.cum_pv += tp * v
            self.cum_v += v
        vwap = self.cum_pv / self.cum_v if self.cum_v > 0 else None

        return {
            "time": t,
            "day": day,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
            "ema9": self.ema9,
            "ema20": self.ema20,
            "atr14": self.atr14,
            "vwap": vwap,
        }


def is_rth_time(t, start=9 * 60 + 30, end=16 * 60):
    m = t.hour * 60 + t.minute
    return start < m <= end


def futures_specs():
    return {
        "ES": {"ticker": Futures.Indices.SP_500_E_MINI, "session_start": 9 * 60 + 30, "session_end": 16 * 60},
        "NQ": {"ticker": Futures.Indices.NASDAQ_100_E_MINI, "session_start": 9 * 60 + 30, "session_end": 16 * 60},
        "MES": {"ticker": Futures.Indices.MICRO_SP_500_E_MINI, "session_start": 9 * 60 + 30, "session_end": 16 * 60},
        "MNQ": {"ticker": Futures.Indices.MICRO_NASDAQ_100_E_MINI, "session_start": 9 * 60 + 30, "session_end": 16 * 60},
        "MYM": {"ticker": Futures.Indices.MICRO_DOW_30_E_MINI, "session_start": 9 * 60 + 30, "session_end": 16 * 60},
        "M2K": {"ticker": Futures.Indices.MICRO_RUSSELL_2000_E_MINI, "session_start": 9 * 60 + 30, "session_end": 16 * 60},
        "MCL": {"ticker": Futures.Energies.MICRO_CRUDE_OIL_WTI, "session_start": 9 * 60, "session_end": 14 * 60 + 30},
        "MGC": {"ticker": Futures.Metals.MICRO_GOLD, "session_start": 8 * 60 + 20, "session_end": 13 * 60 + 30},
    }


def trade_specs():
    return {
        "ES": {"source": "ES", "peer": "NQ", "point": 50.0, "tick": 0.25, "fee": 4.0, "slip": 1.0},
        "NQ": {"source": "NQ", "peer": "ES", "point": 20.0, "tick": 0.25, "fee": 4.0, "slip": 1.0},
        "MES": {"source": "MES", "peer": "MNQ", "point": 5.0, "tick": 0.25, "fee": 2.0, "slip": 1.0},
        "MNQ": {"source": "MNQ", "peer": "MES", "point": 2.0, "tick": 0.25, "fee": 2.0, "slip": 1.0},
        "MYM": {"source": "MYM", "peer": "MES", "point": 0.5, "tick": 1.0, "fee": 2.0, "slip": 1.0},
        "M2K": {"source": "M2K", "peer": "MES", "point": 5.0, "tick": 0.1, "fee": 2.0, "slip": 1.0},
        "MCL": {"source": "MCL", "point": 100.0, "tick": 0.01, "fee": 2.0, "slip": 1.0},
        "MGC": {"source": "MGC", "point": 10.0, "tick": 0.1, "fee": 2.0, "slip": 1.0},
    }


def stressed_spec(spec):
    out = dict(spec)
    out["fee"] = spec.get("fee", 2.0) * 2.0
    out["slip"] = spec.get("slip", 1.0) * 2.0
    return out


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


def evaluate_neighbors(spec, cfg):
    results = []
    for variant in neighbor_configs(cfg):
        results.append(run_backtest(spec["groups"], spec["peer"], spec, variant, stop_on_event=True))
    if not results:
        return {"total": 0, "profitable": 0, "passed": 0, "breached": 0, "min_profit": 0.0, "avg_profit": 0.0}
    profits = [m["profit"] for m in results]
    return {
        "total": len(results),
        "profitable": sum(1 for m in results if m["profit"] > 0),
        "passed": sum(1 for m in results if m["eval_pass"] and not m["breached"]),
        "breached": sum(1 for m in results if m["breached"]),
        "min_profit": min(profits),
        "avg_profit": sum(profits) / len(profits),
    }


def neighbor_configs(cfg):
    variants = []
    seen = set()
    candidates = [
        ("stop_atr", round(max(0.45, cfg["stop_atr"] * 0.85), 3)),
        ("stop_atr", round(min(1.35, cfg["stop_atr"] * 1.15), 3)),
        ("rr", round(max(1.2, cfg["rr"] - 0.3), 3)),
        ("rr", round(min(2.8, cfg["rr"] + 0.3), 3)),
    ]
    for key, value in candidates:
        if abs(value - cfg[key]) < 1e-9:
            continue
        variant = dict(cfg)
        variant[key] = value
        sig = (variant["stop_atr"], variant["rr"], variant.get("entry_start", 0), variant["entry_end"], variant["or_bars"])
        if sig not in seen:
            seen.add(sig)
            variants.append(variant)
    return variants


def select_shortlist(items, limit):
    if len(items) <= limit:
        return items
    selected = []
    seen = set()
    market_counts = {}
    name_counts = {}
    family_counts = {}

    def sig(cfg):
        return (
            cfg["market"], cfg["name"], cfg["side"], cfg.get("entry_start", 0), cfg["entry_end"],
            cfg["stop_atr"], cfg["rr"], cfg["filter"], cfg["or_bars"], cfg["risk_usd"], cfg["max_trades_day"],
            cfg.get("second_trade_mode", "any"), cfg.get("daily_profit_stop", 9999.0), cfg.get("daily_loss_stop", 1000.0), cfg.get("loss_cooldown", 0), cfg.get("trail_r", 1.0), cfg.get("target_mode", "fixed"), cfg.get("profit_lock_r", 0.0), cfg.get("regime", "all"),
            cfg.get("min_impulse_atr", 0.0), cfg.get("peer_mode", "none"), cfg.get("daily_mode", "none"),
        )

    def add(item):
        cfg = item[1]
        s = sig(cfg)
        if s in seen:
            return False
        seen.add(s)
        selected.append(item)
        market_counts[cfg["market"]] = market_counts.get(cfg["market"], 0) + 1
        name_counts[cfg["name"]] = name_counts.get(cfg["name"], 0) + 1
        family = (cfg["market"], cfg["name"])
        family_counts[family] = family_counts.get(family, 0) + 1
        return True

    market_cap = max(10, limit // 3)
    name_cap = max(6, limit // 8)
    family_cap = max(4, limit // 22)
    for item in items:
        cfg = item[1]
        family = (cfg["market"], cfg["name"])
        if (
            market_counts.get(cfg["market"], 0) < market_cap
            and name_counts.get(cfg["name"], 0) < name_cap
            and family_counts.get(family, 0) < family_cap
        ):
            add(item)
            if len(selected) >= limit:
                return selected

    loose_name_cap = max(name_cap * 2, limit // 5)
    loose_family_cap = max(family_cap * 2, limit // 14)
    for item in items:
        cfg = item[1]
        family = (cfg["market"], cfg["name"])
        if name_counts.get(cfg["name"], 0) < loose_name_cap and family_counts.get(family, 0) < loose_family_cap:
            add(item)
            if len(selected) >= limit:
                return selected

    for item in items:
        add(item)
        if len(selected) >= limit:
            return selected
    return selected


def attach_neighbors(full, neighbors):
    full["neighbors"] = neighbors
    full["neighbor_total"] = neighbors["total"]
    full["neighbor_profitable"] = neighbors["profitable"]
    full["neighbor_passed"] = neighbors["passed"]
    full["neighbor_breached"] = neighbors["breached"]
    full["neighbor_min_profit"] = neighbors["min_profit"]
    full["neighbor_avg_profit"] = neighbors["avg_profit"]


def risk_adjusted_cfg(cfg, equity, threshold, day_pnl, loss_streak=0):
    if cfg.get("risk_profile", "fixed") != "apex_guard":
        return cfg
    base = cfg["risk_usd"]
    cushion = max(0.0, equity - threshold)
    day_room = max(0.0, 1000.0 + day_pnl)
    scale = 1.0
    if equity < 50000.0:
        scale = min(scale, 0.75)
    if equity < 49250.0:
        scale = min(scale, 0.35)
    elif equity < 49500.0:
        scale = min(scale, 0.50)
    if cushion < 900.0:
        scale = min(scale, 0.45)
    elif cushion < 1300.0:
        scale = min(scale, 0.65)
    if day_pnl <= -250.0:
        scale = min(scale, 0.55)
    if loss_streak >= 3:
        scale = min(scale, 0.35)
    elif loss_streak >= 2:
        scale = min(scale, 0.50)
    if equity >= 52500.0:
        scale = min(scale, 0.60)
    if equity >= 52900.0:
        scale = min(scale, 0.35)
    risk = min(base * scale, cushion * 0.25, day_room * 0.45)
    out = dict(cfg)
    out["risk_usd"] = max(0.0, risk)
    return out


def stream_counts(available):
    return ",".join(f"{k}:{len(v)}" for k, v in sorted(available.items()))


def prepare_rows(rows):
    out = [dict(r) for r in rows]
    grouped = {}
    for r in out:
        grouped.setdefault(r["day"], []).append(r)

    prev_high = None
    prev_low = None
    prev_close = None
    prev_open = None
    prev_range = None
    prev_is_nr7 = False
    vol_by_idx = {}
    daily_hist = []
    for day in sorted(grouped):
        g = grouped[day]
        day_open = g[0]["open"]
        closes = [x["close"] for x in daily_hist]
        highs = [x["high"] for x in daily_hist]
        lows = [x["low"] for x in daily_hist]
        ranges = [x["range"] for x in daily_hist]
        trend5 = 0
        trend20 = 0
        if len(closes) >= 5 and prev_close is not None:
            trend5 = 1 if prev_close > closes[-5] else (-1 if prev_close < closes[-5] else 0)
        if len(closes) >= 20 and prev_close is not None:
            trend20 = 1 if prev_close > closes[-20] else (-1 if prev_close < closes[-20] else 0)
        prior20_high = max(highs[-20:]) if len(highs) >= 5 else None
        prior20_low = min(lows[-20:]) if len(lows) >= 5 else None
        avg_daily_range = sum(ranges[-20:]) / min(len(ranges), 20) if len(ranges) >= 5 else None
        prev_is_nr7 = len(ranges) >= 7 and ranges[-1] < min(ranges[-7:-1])
        for i, r in enumerate(g):
            hist = vol_by_idx.get(i, [])
            recent = hist[-20:]
            avg_vol = sum(recent) / len(recent) if len(recent) >= 5 else 0.0
            r["idx"] = i
            r["day_open"] = day_open
            r["prev_high"] = prev_high
            r["prev_low"] = prev_low
            r["prev_close"] = prev_close
            r["prev_open"] = prev_open
            r["prev_range"] = prev_range
            r["prev_is_nr7"] = prev_is_nr7
            r["gap"] = None if prev_close is None else day_open - prev_close
            r["minute"] = r["time"].hour * 60 + r["time"].minute
            r["rel_vol"] = (r["volume"] / avg_vol) if avg_vol > 0 else None
            r["trend5"] = trend5
            r["trend20"] = trend20
            r["prior20_high"] = prior20_high
            r["prior20_low"] = prior20_low
            r["avg_daily_range"] = avg_daily_range
        for i, r in enumerate(g):
            vol_by_idx.setdefault(i, []).append(r["volume"])
        prev_open = day_open
        prev_high = max(x["high"] for x in g)
        prev_low = min(x["low"] for x in g)
        prev_range = prev_high - prev_low
        prev_close = g[-1]["close"]
        daily_hist.append({"high": prev_high, "low": prev_low, "close": prev_close, "range": prev_range})
    return out


def group_rows(rows):
    grouped = {}
    for r in rows:
        grouped.setdefault(r["day"], []).append(r)
    return grouped


def run_backtest(grouped_all, peer_by_time, spec, cfg, start_day=None, end_day=None, stop_on_event=False):
    equity = 50000.0
    peak = 50000.0
    eod_peak = 50000.0
    threshold = 48000.0
    max_dd = 0.0
    worst_day = 0.0
    trades = []
    daily = {}
    eval_pass = False
    first_breach = False
    pass_day = None
    breach_day = None
    pass_profit = None
    breach_profit = None
    cooldown_remaining = 0
    loss_streak = 0
    min_cushion = 2000.0

    for day in sorted(grouped_all):
        if start_day is not None and day < start_day:
            continue
        if end_day is not None and day > end_day:
            continue
        g = grouped_all[day]
        if len(g) <= cfg["or_bars"] + 2:
            daily[day] = 0.0
            continue
        if cooldown_remaining > 0:
            daily[day] = 0.0
            cooldown_remaining -= 1
            continue

        or_part = g[:cfg["or_bars"]]
        or_hi = max(x["high"] for x in or_part)
        or_lo = min(x["low"] for x in or_part)
        or_range = max(or_hi - or_lo, spec["tick"])
        or_atr = or_part[-1]["atr14"] or or_range
        if not day_regime_ok(g[cfg["or_bars"]], cfg, or_range, or_atr):
            daily[day] = 0.0
            continue

        state = {"breakout": 0, "used_gap": False}
        day_pnl = 0.0
        day_loss_stop = effective_daily_loss_stop(cfg, equity)
        trades_day = 0
        i = cfg["or_bars"]
        while i < len(g) - 1:
            row = g[i]
            if row["minute"] > cfg["entry_end"]:
                break
            if day_pnl >= cfg["daily_profit_stop"] or day_pnl <= -day_loss_stop or trades_day >= cfg["max_trades_day"]:
                break
            if cfg.get("second_trade_mode", "any") == "after_win" and trades_day > 0 and day_pnl <= 0.0:
                break

            side = signal_for(g, i, peer_by_time.get(row["time"]), cfg, state, or_hi, or_lo, or_range, or_atr)
            if row["minute"] < cfg.get("entry_start", 0):
                side = 0
            if side:
                trade_cfg = risk_adjusted_cfg(cfg, equity, threshold, day_pnl, loss_streak)
                trade, exit_i = simulate_trade(g, i + 1, i, side, spec, trade_cfg, or_hi, or_lo)
                if trade:
                    trades_day += 1
                    trades.append(trade)
                    equity += trade["pnl"]
                    day_pnl += trade["pnl"]
                    min_cushion = min(min_cushion, equity - threshold)
                    peak = max(peak, equity)
                    max_dd = min(max_dd, equity - peak)

                    # The active EOD threshold is enforced intraday after each closed trade.
                    if (not first_breach) and equity <= threshold:
                        first_breach = True
                        breach_day = day
                        breach_profit = equity - 50000.0
                        if stop_on_event:
                            daily[day] = day_pnl
                            return summarize(equity, trades, max_dd, min(worst_day, day_pnl), first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion)
                    if (not first_breach) and (not eval_pass) and equity >= 53000.0:
                        eval_pass = True
                        pass_day = day
                        pass_profit = equity - 50000.0
                        if stop_on_event:
                            daily[day] = day_pnl
                            return summarize(equity, trades, max_dd, min(worst_day, day_pnl), first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion)
                    if first_breach and stop_on_event:
                        daily[day] = day_pnl
                        return summarize(equity, trades, max_dd, min(worst_day, day_pnl), first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion)
                    i = exit_i + 1
                    continue
            i += 1

        worst_day = min(worst_day, day_pnl)
        daily[day] = day_pnl
        if cfg.get("loss_cooldown", 0) > 0 and day_pnl <= -cfg.get("cooldown_loss", 250.0):
            cooldown_remaining = max(cooldown_remaining, cfg["loss_cooldown"])
        if day_pnl < 0.0:
            loss_streak += 1
        elif day_pnl > 0.0:
            loss_streak = 0
        min_cushion = min(min_cushion, equity - threshold)
        # Apex DLL pauses trading; the EOD trailing threshold is the eval breach.
        if (not first_breach) and equity <= threshold:
            first_breach = True
            breach_day = day
            breach_profit = equity - 50000.0
        if (not first_breach) and (not eval_pass) and equity >= 53000.0:
            eval_pass = True
            pass_day = day
            pass_profit = equity - 50000.0
            if stop_on_event:
                break
        eod_peak = max(eod_peak, equity)
        threshold = max(threshold, eod_peak - 2000.0)
        if first_breach and stop_on_event:
            break

    return summarize(equity, trades, max_dd, worst_day, first_breach, eval_pass, pass_day, breach_day, pass_profit, breach_profit, daily, min_cushion)


def effective_daily_loss_stop(cfg, start_equity):
    base = cfg["daily_loss_stop"]
    if start_equity >= 52900.0:
        return min(base, 180.0)
    if start_equity >= 52500.0:
        return min(base, 350.0)
    return base


def day_regime_ok(row, cfg, or_range, or_atr):
    if cfg.get("event_filter") == "macro_skip" and macro_event_day(row["day"]):
        return False
    regime = cfg.get("regime", "all")
    if regime == "all":
        return True
    gap = row.get("gap")
    atr = row["atr14"] or or_atr
    if gap is None or atr <= 0:
        return False
    gap_abs = abs(gap)
    if regime == "small_gap":
        return gap_abs <= 0.45 * atr and or_range <= 1.25 * atr
    if regime == "large_gap":
        return gap_abs >= 0.45 * atr or or_range >= 1.15 * atr
    if regime == "balanced_open":
        return gap_abs <= 0.35 * atr and 0.35 * atr <= or_range <= 0.95 * atr
    if regime == "drive_open":
        return gap_abs <= 0.75 * atr and 0.55 * atr <= or_range <= 1.35 * atr and abs(row["close"] - row["day_open"]) >= 0.25 * atr
    if regime == "vvg_drive":
        return gap_abs <= 0.85 * atr and 0.50 * atr <= or_range <= 1.40 * atr and abs(row["close"] - row["day_open"]) >= 0.25 * atr and (row.get("rel_vol") or 0.0) >= 1.15
    return True


MAJOR_EVENT_DATES = {
    (2025, 4, 4), (2025, 4, 10),
    (2025, 5, 2), (2025, 5, 7), (2025, 5, 13),
    (2025, 6, 6), (2025, 6, 11), (2025, 6, 18),
    (2025, 7, 3), (2025, 7, 15), (2025, 7, 30),
    (2025, 8, 1), (2025, 8, 12),
    (2025, 9, 5), (2025, 9, 11), (2025, 9, 17),
    (2025, 10, 3), (2025, 10, 15), (2025, 10, 29),
    (2025, 11, 7), (2025, 11, 13),
    (2025, 12, 5), (2025, 12, 10), (2025, 12, 18),
    (2026, 1, 9), (2026, 1, 13), (2026, 1, 28),
    (2026, 2, 11), (2026, 2, 13),
    (2026, 3, 6), (2026, 3, 11), (2026, 3, 18),
}


def macro_event_day(day):
    if hasattr(day, "year") and hasattr(day, "month") and hasattr(day, "day"):
        return (day.year, day.month, day.day) in MAJOR_EVENT_DATES
    return day in MAJOR_EVENT_DATES
