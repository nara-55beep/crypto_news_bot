import argparse
import calendar
import pathlib
import sys
from datetime import date


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from local_qc_apex_cache_search import build_market_spec, evaluate, load_market
from apex_ensemble import evaluate_ensemble
from apex_locked import locked_ensemble_configs
from apex_score import apex_ready_reason


def month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def next_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1


def fmt_money(value):
    return f"{value:,.0f}"


def month_rows(markets, first_day, last_day):
    y, m = first_day.year, first_day.month
    while (y, m) <= (last_day.year, last_day.month):
        start = max(date(y, m, 1), first_day)
        end = min(month_end(y, m), last_day)
        day_set = set()
        for market in markets.values():
            day_set |= set(market["days"])
        days = sorted(d for d in day_set if start <= d <= end)
        if days:
            yield y, m, start, end, days
        y, m = next_month(y, m)


def evaluate_locked_month(markets, days):
    split = days[len(days) // 2]
    members = []
    for cfg in locked_ensemble_configs():
        item = evaluate(markets, cfg, split, days[0], days[-1], require_train=False)
        if item is None:
            raise RuntimeError(f"locked member failed: {cfg['market']} {cfg['name']}")
        score, cfg, train, hold, full, roll = item
        members.append({"score": score, "cfg": cfg, "train": train, "hold": hold, "full": full, "roll": roll})
    return evaluate_ensemble(members, days, split)


def main():
    parser = argparse.ArgumentParser(description="Month-by-month report for the locked qc_apex_es_vwap_orb strategy.")
    parser.add_argument("--start", default=None, help="Optional YYYY-MM-DD start date.")
    parser.add_argument("--end", default=None, help="Optional YYYY-MM-DD end date.")
    args = parser.parse_args()

    es = load_market("es")
    nq = load_market("nq")
    mcl = load_market("mcl")
    markets = {
        "ES": build_market_spec(es, nq, 50.0, 4.0),
        "NQ": build_market_spec(nq, es, 20.0, 4.0),
        "MES": build_market_spec(es, nq, 5.0, 4.0),
        "MNQ": build_market_spec(nq, es, 2.0, 4.0),
        "MCL": build_market_spec(mcl, [], 100.0, 2.0),
    }
    all_days = sorted(set().union(*(set(market["days"]) for market in markets.values())))
    first_day = all_days[0]
    last_day = all_days[-1]
    if args.start:
        y, m, d = (int(x) for x in args.start.split("-"))
        first_day = max(first_day, date(y, m, d))
    if args.end:
        y, m, d = (int(x) for x in args.end.split("-"))
        last_day = min(last_day, date(y, m, d))

    print(f"MONTHLY_SOURCE cache=apex3yr_es/apex3yr_nq/cl_1m_3y start={first_day} end={last_day} reset_capital=50000")
    print("month,days,pnl,final,trades,win_rate,pf,max_dd,worst_day,apex_pass,pass_day,pass_pnl,breached,breach_day,reason")

    total = 0.0
    wins = 0
    losses = 0
    pass_count = 0
    breach_count = 0
    rows = 0
    worst_month = None
    best_month = None
    for y, m, start, end, days in month_rows(markets, first_day, last_day):
        _, members, train, hold, full, roll = evaluate_locked_month(markets, days)
        reason = apex_ready_reason(hold, full, roll, 60)
        month = f"{y:04d}-{m:02d}"
        pnl = full["profit"]
        total += pnl
        rows += 1
        wins += 1 if pnl > 0 else 0
        losses += 1 if pnl < 0 else 0
        pass_count += 1 if full["eval_pass"] else 0
        breach_count += 1 if full["breached"] else 0
        if best_month is None or pnl > best_month[1]:
            best_month = (month, pnl)
        if worst_month is None or pnl < worst_month[1]:
            worst_month = (month, pnl)
        print(
            f"{month},{len(days)},{fmt_money(pnl)},{fmt_money(full['final'])},{full['trades']},"
            f"{full['win_rate']:.1f},{full['pf']:.2f},{fmt_money(full['max_dd'])},{fmt_money(full['worst_day'])},"
            f"{full['eval_pass']},{full['pass_day']},{full['pass_profit'] if full['pass_profit'] is not None else ''},"
            f"{full['breached']},{full['breach_day']},{reason}"
        )

    print(
        "MONTHLY_TOTAL "
        f"months={rows} total_pnl={fmt_money(total)} avg_month={fmt_money(total / max(rows, 1))} "
        f"positive_months={wins} negative_months={losses} apex_pass_months={pass_count} breach_months={breach_count} "
        f"best={best_month[0]}:{fmt_money(best_month[1])} worst={worst_month[0]}:{fmt_money(worst_month[1])}"
    )


if __name__ == "__main__":
    main()
