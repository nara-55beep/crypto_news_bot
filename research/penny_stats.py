"""Dependence- and selection-aware inference for the penny-stock edge audit.

Trade outcomes are not independent observations. Several names can be opened on the
same session, and a ten-session forward return overlaps the next nine sessions' forward
returns. Inference therefore uses one equal-weight basket per signal date and resamples
contiguous blocks from a common market-session calendar. Idle sessions stay in the
calendar, so a "20-session" block always means 20 adjacent market sessions rather than
20 irregularly spaced signal events.

The family-wise test is a White-style reality check: every candidate is recentered to
the no-edge null, the best result in every bootstrap draw is retained, and the resulting
p-value includes the cost of searching the candidate family. It is intentionally called
"White-style" rather than claiming an exact reproduction of every statistic in White
(2000).

These routines can reject weak evidence; they cannot manufacture a tradeable edge.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Normal quantiles, hardcoded so the module has no scipy dependency.
Z_975 = 1.959963985
Z_80 = 0.841621234


def _dates(values) -> pd.DatetimeIndex:
    """Return unique, timezone-naive normalized dates."""
    idx = pd.DatetimeIndex(pd.to_datetime(values))
    if idx.tz is not None:
        idx = idx.tz_convert(None)
    return idx.normalize().unique().sort_values()


def _daily_panel(
    all_trades: dict[str, pd.DataFrame],
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    calendar: pd.DatetimeIndex | pd.Series | list | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str], pd.DatetimeIndex, str]:
    """Build return and activity matrices on one common session calendar.

    Returns are equal-weighted within signal date. ``active`` distinguishes a genuine
    zero return from an idle date; that distinction is required to estimate conditional
    mean return per signal-day basket without compressing time between signals.
    """
    names = sorted(all_trades)
    series: dict[str, pd.Series] = {}
    all_signal_dates = pd.DatetimeIndex([])
    start_ts = pd.Timestamp(start).normalize() if start is not None else None
    end_ts = pd.Timestamp(end).normalize() if end is not None else None

    for name in names:
        tr = all_trades[name]
        if tr is None or tr.empty:
            series[name] = pd.Series(dtype=float)
            continue
        s = tr.copy()
        s["signal_date"] = pd.to_datetime(s["signal_date"]).dt.tz_localize(None).dt.normalize()
        s["net_return"] = pd.to_numeric(s["net_return"], errors="coerce")
        s = s.dropna(subset=["signal_date", "net_return"])
        if start_ts is not None:
            s = s[s["signal_date"] >= start_ts]
        if end_ts is not None:
            s = s[s["signal_date"] <= end_ts]
        grouped = (s.groupby("signal_date")["net_return"].mean().sort_index()
                   if not s.empty else pd.Series(dtype=float))
        series[name] = grouped
        if not grouped.empty:
            all_signal_dates = all_signal_dates.union(_dates(grouped.index))

    if all_signal_dates.empty:
        return (
            np.zeros((0, len(names))),
            np.zeros((0, len(names)), dtype=bool),
            names,
            pd.DatetimeIndex([]),
            "provided market calendar" if calendar is not None else "business-day proxy",
        )

    lower = start_ts if start_ts is not None else all_signal_dates.min()
    upper = end_ts if end_ts is not None else all_signal_dates.max()
    if calendar is None:
        index = pd.bdate_range(lower, upper)
        calendar_source = "business-day proxy"
    else:
        index = _dates(calendar)
        index = index[(index >= lower) & (index <= upper)]
        calendar_source = "provided market calendar"

    # Never silently discard a signal because a benchmark calendar has a missing row.
    missing_signal_dates = all_signal_dates.difference(index)
    if len(missing_signal_dates):
        index = index.union(missing_signal_dates).sort_values()
        calendar_source += " (augmented with missing signal dates)"

    values = np.zeros((len(index), len(names)), dtype=float)
    active = np.zeros_like(values, dtype=bool)
    for col, name in enumerate(names):
        s = series[name]
        if s.empty:
            continue
        aligned = s.reindex(index)
        mask = aligned.notna().to_numpy()
        active[:, col] = mask
        values[mask, col] = aligned[mask].to_numpy(dtype=float)
    return values, active, names, index, calendar_source


def daily_matrix(
    all_trades: dict[str, pd.DataFrame],
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    calendar: pd.DatetimeIndex | pd.Series | list | None = None,
) -> tuple[np.ndarray, list[str], int]:
    """Align strategies to a common calendar; idle market sessions contribute zero."""
    values, _, names, index, _ = _daily_panel(all_trades, start, end, calendar)
    return values, names, len(index)


def _circular_block_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Circular block bootstrap indices for contiguous market-session runs."""
    if n <= 0:
        return np.empty(0, dtype=int)
    block = max(1, min(int(block), max(1, n // 2)))
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=n_blocks)
    idx = (starts[:, None] + np.arange(block)[None, :]).ravel() % n
    return idx[:n]


def _conditional_means(values: np.ndarray, active: np.ndarray) -> np.ndarray:
    denom = active.sum(axis=0)
    return np.divide(
        values.sum(axis=0),
        denom,
        out=np.full(values.shape[1], np.nan, dtype=float),
        where=denom > 0,
    )


def _bootstrap_conditional_means(
    centred: np.ndarray,
    active: np.ndarray,
    n_boot: int,
    block: int,
    seed: int,
) -> np.ndarray:
    """Resample calendar blocks while preserving each strategy's no-signal dates."""
    n_days, n_strategies = centred.shape
    draws = np.full((n_boot, n_strategies), np.nan, dtype=float)
    rng = np.random.default_rng(seed)
    for draw in range(n_boot):
        idx = _circular_block_indices(n_days, block, rng)
        draws[draw] = _conditional_means(centred[idx], active[idx])
    return draws


def reality_check(
    all_trades: dict[str, pd.DataFrame],
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    n_boot: int = 5000,
    block: int = 20,
    seed: int = 20260807,
    calendar: pd.DatetimeIndex | pd.Series | list | None = None,
) -> dict:
    """Family-wise, White-style test of the best signal-day basket mean.

    The candidate family is recentered to the no-positive-edge null. Both activity and
    outcome rows are resampled in contiguous calendar blocks, which retains same-day
    cross-sectional clustering, signal gaps, and much of the dependence created by
    overlapping holding periods.
    """
    values, active, names, index, calendar_source = _daily_panel(
        all_trades, start, end, calendar
    )
    counts = active.sum(axis=0)
    keep = counts > 0
    values, active = values[:, keep], active[:, keep]
    names = [name for name, include in zip(names, keep) if include]
    counts = counts[keep]
    union_signal_days = int(active.any(axis=1).sum()) if active.size else 0
    if not names or counts.max(initial=0) < 30 or len(index) < 30:
        return {
            "applicable": False,
            "reason": f"only {union_signal_days} union signal days in the selection window",
            "market_sessions": int(len(index)),
        }

    means = _conditional_means(values, active)
    best_i = int(np.nanargmax(means))
    observed = float(means[best_i])
    centred = np.where(active, values - means[None, :], 0.0)
    draws = _bootstrap_conditional_means(centred, active, n_boot, block, seed)
    draw_max = np.nanmax(draws, axis=1)
    selection_exceed = int(np.sum(draw_max >= observed))
    naive_exceed = int(np.sum(draws[:, best_i] >= observed))
    p_selection = (selection_exceed + 1) / (n_boot + 1)
    p_naive = (naive_exceed + 1) / (n_boot + 1)
    effective_block = max(1, min(int(block), max(1, len(index) // 2)))

    return {
        "applicable": True,
        "test_name": "White-style family-wise calendar-block bootstrap",
        "best_strategy": names[best_i],
        "strategies_searched": len(names),
        "market_sessions": int(len(index)),
        "union_signal_days": union_signal_days,
        "signal_days": int(counts[best_i]),  # compatibility: winner's active days
        "selected_signal_days": int(counts[best_i]),
        "observed_mean_net_pct": round(observed * 100, 4),
        "estimand": "equal-weight net return per active signal-day basket",
        "p_value_selection_aware": round(p_selection, 4),
        "p_value_naive_single_test": round(p_naive, 4),
        "significant_at_5pct": bool(p_selection < 0.05),
        "block_days": effective_block,
        "bootstrap_draws": n_boot,
        "calendar_source": calendar_source,
        "interpretation": (
            f"best of {len(names)} strategies; a family-wise p of {p_selection:.3f} "
            f"means calendar-block noise reproduces a result this large "
            f"{p_selection * 100:.1f}% of the time"
        ),
    }


def power(
    trades: pd.DataFrame,
    label: str = "",
    calendar: pd.DatetimeIndex | pd.Series | list | None = None,
    n_boot: int = 3000,
    block: int = 20,
    seed: int = 20260808,
) -> dict:
    """Estimate uncertainty and minimum detectable signal-day basket edge.

    A naive ``sd/sqrt(n)`` is retained for comparison, but the decision statistic uses
    the standard deviation of calendar-block bootstrap means. This handles same-session
    clustering and serial dependence from overlapping forward-return windows.
    """
    if trades is None or trades.empty:
        return {"applicable": False, "reason": "no trades"}
    values, active, _, index, calendar_source = _daily_panel(
        {label or "strategy": trades}, calendar=calendar
    )
    mask = active[:, 0]
    daily = values[mask, 0]
    n = len(daily)
    if n < 5:
        return {"applicable": False, "reason": f"only {n} signal days"}

    mean = float(daily.mean())
    sd = float(daily.std(ddof=1))
    naive_se = sd / np.sqrt(n)
    centred = np.where(active, values - mean, 0.0)
    draws = _bootstrap_conditional_means(centred, active, n_boot, block, seed)[:, 0]
    draws = draws[np.isfinite(draws)]
    bootstrap_se = float(draws.std(ddof=1)) if len(draws) > 1 else float("nan")
    if not np.isfinite(bootstrap_se) or bootstrap_se <= 0:
        bootstrap_se = naive_se
    mde = (Z_975 + Z_80) * bootstrap_se
    effective_sd = bootstrap_se * np.sqrt(n)
    needed = (
        ((Z_975 + Z_80) * effective_sd / abs(mean)) ** 2
        if mean and np.isfinite(effective_sd) else float("inf")
    )
    lower, upper = np.quantile(mean + draws, [0.025, 0.975])
    p_two_sided = (int(np.sum(np.abs(draws) >= abs(mean))) + 1) / (len(draws) + 1)
    s = trades.copy()
    trade_weighted_mean = float(pd.to_numeric(s["net_return"], errors="coerce").mean())
    effective_block = max(1, min(int(block), max(1, len(index) // 2)))

    return {
        "applicable": True,
        "label": label,
        "signal_days": int(n),
        "market_sessions": int(len(index)),
        "trades": int(len(s)),
        "mean_net_pct": round(mean * 100, 4),  # compatibility alias
        "mean_signal_day_basket_net_pct": round(mean * 100, 4),
        "trade_weighted_mean_net_pct": round(trade_weighted_mean * 100, 4),
        "daily_sd_pct": round(sd * 100, 4),
        "naive_standard_error_pct": round(naive_se * 100, 4),
        "standard_error_pct": round(bootstrap_se * 100, 4),
        "standard_error_method": "circular market-calendar block bootstrap",
        "autocorrelation_inflation_vs_iid": (
            round(bootstrap_se / naive_se, 3) if naive_se > 0 else None
        ),
        "t_stat": round(mean / bootstrap_se, 3) if bootstrap_se else None,
        "two_sided_bootstrap_p_value": round(p_two_sided, 4),
        "bootstrap_95_pct": [round(lower * 100, 4), round(upper * 100, 4)],
        "min_detectable_edge_pct": round(mde * 100, 4),
        "signal_days_needed_for_observed_edge": (
            int(np.ceil(needed)) if np.isfinite(needed) else None
        ),
        "sample_can_resolve_observed_edge": bool(abs(mean) >= mde),
        "block_days": effective_block,
        "bootstrap_draws": int(len(draws)),
        "calendar_source": calendar_source,
        "estimand": "equal-weight net return per active signal-day basket",
    }


def cost_decomposition(trades: pd.DataFrame) -> dict:
    """Split a losing result into "the signal is wrong" and "the costs are too big".

    These call for opposite responses and a net figure cannot tell them apart. If gross
    expectancy is solidly positive and costs swallow it, the lever is execution - venue,
    spread, holding period. If gross expectancy is ~zero, the ranking carries no
    predictive content and no execution improvement can rescue it, because there is
    nothing there to keep. Reporting only net return hides which case you are in.
    """
    if trades is None or trades.empty:
        return {"applicable": False, "reason": "no trades"}
    need = {"gross_return", "cost", "net_return"}
    if not need.issubset(trades.columns):
        return {"applicable": False,
                "reason": f"missing columns: {sorted(need - set(trades.columns))}"}

    gross = float(trades["gross_return"].mean())
    cost = float(trades["cost"].mean())
    net = float(trades["net_return"].mean())
    # clustered standard error on gross, so "zero" is a measured claim not an eyeball one
    s = trades.copy()
    s["signal_date"] = pd.to_datetime(s["signal_date"])
    daily = s.groupby("signal_date")["gross_return"].mean()
    se = float(daily.std(ddof=1) / np.sqrt(len(daily))) if len(daily) > 1 else float("nan")

    if np.isfinite(se) and se > 0:
        lo, hi = gross - Z_975 * se, gross + Z_975 * se
        gross_is_zero = lo < 0 < hi
    else:
        lo = hi = float("nan")
        gross_is_zero = False

    return {
        "applicable": True,
        "trades": int(len(trades)),
        "mean_gross_pct": round(gross * 100, 4),
        "mean_cost_pct": round(cost * 100, 4),
        "mean_net_pct": round(net * 100, 4),
        "gross_95_ci_pct": [round(lo * 100, 4), round(hi * 100, 4)],
        "breakeven_cost_pct": round(gross * 100, 4),
        "gross_indistinguishable_from_zero": bool(gross_is_zero),
        "diagnosis": (
            "no predictive content: gross expectancy is inside noise of zero, so cheaper "
            "execution cannot rescue this rule"
            if gross_is_zero else
            "costs dominate: gross expectancy is real, the lever is execution cost or "
            "holding period"
            if gross > cost * 0.5 else
            "gross expectancy is negative before any costs: the rule is backwards"
        ),
    }


def survivorship_sensitivity(
    mean_net_pct: float,
    annual_delisting_rate: float = 0.07,
    hold_days: int = 10,
    delisting_return_pct: float = -55.0,
) -> dict:
    """Illustrative delisting-loss scenario, not a survivorship-bias correction.

    A survivor-only universe omits more than delisting losses: it conditions the entire
    history on names surviving until today. This calculation only shows the mechanical
    effect of user-visible stress assumptions and must not be described as a bound.
    """
    p_delist = 1 - (1 - annual_delisting_rate) ** (hold_days / 252.0)
    drag = p_delist * (delisting_return_pct - mean_net_pct)
    adjusted = mean_net_pct + drag
    return {
        "scenario_not_correction": True,
        "assumed_annual_delisting_rate_pct": round(annual_delisting_rate * 100, 2),
        "assumed_delisting_return_pct": delisting_return_pct,
        "hold_days": hold_days,
        "delisting_probability_per_trade_pct": round(p_delist * 100, 4),
        "estimated_drag_pct": round(drag, 4),
        "survivorship_adjusted_mean_net_pct": round(adjusted, 4),
        "note": (
            "illustrative missing-delisting-loss scenario only; it neither estimates nor "
            "bounds the larger selection bias from conditioning on survival"
        ),
    }


def detectability_plan(
    trades: pd.DataFrame,
    target_edge_pct: float = 2.0,
    years_available: float | None = None,
    calendar: pd.DatetimeIndex | pd.Series | list | None = None,
    hold_days: int = 10,
    capacity_cap: int | None = None,
) -> dict:
    """Describe data requirements without pretending correlated names are independent."""
    if trades is None or trades.empty:
        return {"applicable": False, "reason": "no trades"}
    s = trades.copy()
    s["signal_date"] = pd.to_datetime(s["signal_date"]).dt.tz_localize(None).dt.normalize()
    daily = s.groupby("signal_date")["net_return"].mean()
    n = len(daily)
    if n < 5:
        return {"applicable": False, "reason": f"only {n} signal days"}

    uncertainty = power(
        s,
        "all available",
        calendar=calendar,
        n_boot=2000,
        block=max(10, int(hold_days) * 2),
        seed=20260809,
    )
    sd = float(daily.std(ddof=1))
    naive_se = sd / np.sqrt(n) if sd else 0.0
    robust_se = float(uncertainty.get("standard_error_pct") or 0.0) / 100.0
    # Do not let a noisy bootstrap claim more power than the independence assumption.
    inflation = max(1.0, robust_se / naive_se) if naive_se > 0 else 1.0
    effective_sd = sd * inflation
    per_day = len(s) / n
    target = target_edge_pct / 100.0
    k = Z_975 + Z_80

    days_needed = int(np.ceil((k * effective_sd / target) ** 2)) if target else 0
    if years_available is None:
        span_days = max(1, (daily.index.max() - daily.index.min()).days)
        years_available = span_days / 365.25
    signal_days_per_year = n / years_available if years_available > 0 else 0.0
    years_needed = days_needed / signal_days_per_year if signal_days_per_year else float("inf")
    # Independence gives the most optimistic breadth estimate. Correlated names require
    # more; reporting this as a lower bound prevents it becoming a promise.
    breadth_floor = ((k * effective_sd / (target * np.sqrt(n))) ** 2 * per_day
                     if target else float("inf"))

    counts = s.groupby("signal_date").size()
    observed_max = int(counts.max())
    if capacity_cap is not None and capacity_cap > 0:
        capacity_cap = int(capacity_cap)
        at_capacity = int((counts >= capacity_cap).sum())
        capacity_basis = "configured cap"
    else:
        capacity_cap = observed_max
        at_capacity = int((counts >= observed_max).sum())
        capacity_basis = "observed maximum proxy; configured cap was unavailable"
    scarce = at_capacity <= max(1, int(0.1 * n))

    return {
        "applicable": True,
        "target_edge_pct": target_edge_pct,
        "target_estimand": "equal-weight net return per active signal-day basket",
        "current_signal_days": int(n),
        "current_names_per_signal_day": round(per_day, 2),
        "current_daily_sd_pct": round(sd * 100, 4),
        "uncertainty_inflation_vs_iid": round(inflation, 3),
        "signal_days_needed_at_current_breadth": days_needed,
        "years_of_history_needed_at_current_breadth": round(years_needed, 1),
        "names_per_day_needed_to_use_current_history": int(np.ceil(breadth_floor)),
        "breadth_number_is_optimistic_floor": True,
        "configured_capacity_cap": capacity_cap,
        "capacity_assessment_basis": capacity_basis,
        "max_names_on_any_day": observed_max,
        "days_at_capacity": at_capacity,
        "days_at_that_maximum": int((counts >= observed_max).sum()),
        "breadth_is_signal_limited": bool(scarce),
        "binding_lever": (
            "universe size or filter width - the configured per-day cap is not binding"
            if scarce else "the configured per-day capacity cap"
        ),
        "note": (
            "the breadth figure assumes independent names and is only an optimistic "
            "lower bound; correlated penny stocks can require materially more breadth"
        ),
    }


def summarise(
    all_trades: dict[str, pd.DataFrame],
    winner: str,
    train_end: pd.Timestamp,
    validation_end: pd.Timestamp,
    winner_hold_days: int = 10,
    calendar: pd.DatetimeIndex | pd.Series | list | None = None,
    winner_capacity_cap: int | None = None,
) -> dict:
    """Full inference block for the audit report."""
    block_days = max(10, int(winner_hold_days) * 2)
    sel = reality_check(
        all_trades,
        end=validation_end,
        calendar=calendar,
        block=block_days,
    )
    win_trades = all_trades.get(winner)
    selection_trades = (
        win_trades[pd.to_datetime(win_trades["signal_date"]) <= validation_end]
        if win_trades is not None and not win_trades.empty else win_trades
    )
    test_trades = (
        win_trades[pd.to_datetime(win_trades["signal_date"]) > validation_end]
        if win_trades is not None and not win_trades.empty else win_trades
    )
    out = {
        "selection_test": sel,
        "power_selection_window": power(
            selection_trades,
            "train+validation",
            calendar=calendar,
            block=block_days,
        ),
        "power_test_window": power(
            test_trades,
            "untouched test",
            calendar=calendar,
            block=block_days,
            seed=20260810,
        ),
    }
    tw = out["power_test_window"]
    if tw.get("applicable"):
        out["survivorship_scenario"] = survivorship_sensitivity(
            tw["trade_weighted_mean_net_pct"], hold_days=winner_hold_days
        )
    if win_trades is not None and not win_trades.empty:
        out["detectability_plan"] = detectability_plan(
            win_trades,
            calendar=calendar,
            hold_days=winner_hold_days,
            capacity_cap=winner_capacity_cap,
        )
    verdicts = []
    if sel.get("applicable") and not sel.get("significant_at_5pct"):
        verdicts.append(
            f"no candidate clears the family-wise search penalty across "
            f"{sel['strategies_searched']} strategies "
            f"(calendar-block p={sel['p_value_selection_aware']:.3f})"
        )
    for key in ("power_selection_window", "power_test_window"):
        blk = out.get(key) or {}
        if blk.get("applicable") and not blk.get("sample_can_resolve_observed_edge"):
            verdicts.append(
                f"{blk['label']}: sample resolves no signal-day basket edge smaller "
                f"than {blk['min_detectable_edge_pct']:.2f}%; its "
                f"{blk['mean_signal_day_basket_net_pct']:+.2f}% estimate is noise-sized"
            )
    if tw.get("applicable"):
        basket = float(tw["mean_signal_day_basket_net_pct"])
        trade = float(tw["trade_weighted_mean_net_pct"])
        if basket * trade <= 0 and basket != trade:
            verdicts.append(
                "untouched-test conclusion changes with same-day weighting "
                f"(trade-weighted {trade:+.2f}% vs signal-day basket {basket:+.2f}%); "
                "neither substitutes for a capital-weighted overlapping portfolio path"
            )
    out["verdicts"] = verdicts
    return out
