import itertools
import argparse
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from local_qc_apex_cache_search import build_market_spec, evaluate, load_market, parse_day
from apex_ensemble import evaluate_ensemble
from apex_score import apex_ready_reason


def make_cfg(market, risk, profile, target_mode, rr, max_contracts):
    tick = 0.01 if market == "MCL" else 0.25
    end = 14 * 60 + 25 if market == "MCL" else 15 * 60 + 55
    return {
        "market": market,
        "name": "nr7_breakout",
        "side": "both",
        "entry_start": 0,
        "entry_end": end,
        "stop_atr": 1.0,
        "rr": rr,
        "filter": "none",
        "stop_mode": "nr7",
        "or_bars": 1,
        "risk_usd": risk,
        "risk_profile": profile,
        "event_filter": "none",
        "max_contracts": max_contracts,
        "max_trades_day": 1,
        "second_trade_mode": "any",
        "daily_profit_stop": 9999.0,
        "daily_loss_stop": 1000.0,
        "loss_cooldown": 0,
        "cooldown_loss": 250.0,
        "trail_r": 1.0,
        "target_mode": target_mode,
        "profit_lock_r": 0.0,
        "sweep_atr": 0.10,
        "regime": "all",
        "price_tick": tick,
    }


def run_combo(markets, days, split, risk, profile, target_mode, rr, max_contracts):
    members = []
    for market in ["MES", "MNQ", "MCL"]:
        cfg = make_cfg(market, risk, profile, target_mode, rr, max_contracts)
        item = evaluate(markets, cfg, split, days[0], days[-1], require_train=False)
        score, cfg, train, hold, full, roll = item
        members.append({"score": score, "cfg": cfg, "train": train, "hold": hold, "full": full, "roll": roll})
    _, members, train, hold, full, roll = evaluate_ensemble(members, days, split)
    return train, hold, full, roll


def main():
    parser = argparse.ArgumentParser(description="Sweep QC-style NR7 locked portfolio settings.")
    parser.add_argument("start", nargs="?", default="2025-03-24")
    parser.add_argument("end", nargs="?", default="2026-03-23")
    parser.add_argument("--risks", default="350,400,450,500,600,700,800")
    parser.add_argument("--profiles", default="fixed,apex_guard")
    parser.add_argument("--modes", default="partial,fixed,runner")
    parser.add_argument("--rrs", default="1.5,2.0,2.5")
    parser.add_argument("--max-contracts", default="3,6")
    args = parser.parse_args()
    start = parse_day(args.start)
    end = parse_day(args.end)
    risks = [float(x) for x in args.risks.split(",") if x]
    profiles = [x for x in args.profiles.split(",") if x]
    modes = [x for x in args.modes.split(",") if x]
    rrs = [float(x) for x in args.rrs.split(",") if x]
    max_contracts_values = [int(x) for x in args.max_contracts.split(",") if x]
    es = load_market("es")
    nq = load_market("nq")
    mcl = load_market("mcl")
    markets = {
        "MES": build_market_spec(es, nq, 5.0, 4.0),
        "MNQ": build_market_spec(nq, es, 2.0, 4.0),
        "MCL": build_market_spec(mcl, [], 100.0, 2.0),
    }
    days = sorted(d for d in set().union(*(set(m["days"]) for m in markets.values())) if start <= d <= end)
    split = days[int(len(days) * 0.62)]
    rows = []
    for risk, profile, target_mode, rr, max_contracts in itertools.product(
        risks,
        profiles,
        modes,
        rrs,
        max_contracts_values,
    ):
        train, hold, full, roll = run_combo(markets, days, split, risk, profile, target_mode, rr, max_contracts)
        reason = apex_ready_reason(hold, full, roll, 60)
        rows.append((reason == "ok", full.get("eval_stop_pass", False) and not full.get("eval_stop_breached", False),
                     full["profit"], full["max_dd"], full["worst_day"], full["trades"], full["pf"],
                     roll["passed"], roll["total"], reason, risk, profile, target_mode, rr, max_contracts,
                     full.get("eval_stop_profit", 0.0), full.get("eval_stop_day"), hold["profit"], train["profit"]))
    rows.sort(key=lambda x: (x[0], x[1], x[2], x[6]), reverse=True)
    print("ok evalstop pnl dd worst trades pf roll reason risk profile mode rr maxc evalpnl evalday hold train")
    for row in rows[:40]:
        ok, evalstop, pnl, dd, worst, trades, pf, rpass, rtot, reason, risk, profile, mode, rr, maxc, evalpnl, evalday, hold, train = row
        print(f"{ok} {evalstop} {pnl:.0f} {dd:.0f} {worst:.0f} {trades} {pf:.2f} {rpass}/{rtot} {reason} "
              f"{risk:.0f} {profile} {mode} {rr:.1f} {maxc} {evalpnl:.0f} {evalday} {hold:.0f} {train:.0f}")


if __name__ == "__main__":
    main()
