from datetime import date

import pandas as pd

import lucid_causal_rebuild as L
import lucid_portfolio_policy as S


def trade(
    *,
    market="nq",
    strategy="nq_morning_test",
    day=date(2026, 1, 2),
    entry="2026-01-02 14:46:00+00:00",
    exit="2026-01-02 14:46:00+00:00",
    risk=50.0,
    gross=100.0,
):
    return L.Trade(
        market=market,
        strategy=strategy,
        day=day,
        entry_ts=pd.Timestamp(entry),
        exit_ts=pd.Timestamp(exit),
        side=1,
        entry=100.0,
        stop=99.0,
        target=102.0,
        exit=102.0,
        reason="target",
        risk_per_micro=risk,
        gross_per_micro=gross,
    )


def test_same_minute_execution_is_realized_not_left_open():
    d = date(2026, 1, 2)
    policy = S.Policy(2_000.0, 0.0)
    outcome, used = S.simulate_window({d: [trade()]}, [d], policy)
    assert outcome == "pass"
    assert used == 1


def test_aggregate_micro_cap_counts_open_positions():
    policy = S.Policy(2_000.0, 0.0)
    first = S.Position(trade(risk=20.0), 30)
    second = trade(market="es", risk=20.0)
    qty = S._entry_qty(second, policy, 0.0, -2_000.0, [first])
    assert qty == 10


def test_mll_room_is_reserved_across_open_positions():
    policy = S.Policy(2_000.0, 0.0)
    first = S.Position(trade(risk=49.0), 30)  # $1,500 including commission
    second = trade(market="es", risk=49.0)
    qty = S._entry_qty(second, policy, 0.0, -2_000.0, [first])
    assert qty == 9


def test_zero_trade_sessions_count_toward_horizon():
    d1, d2 = date(2026, 1, 2), date(2026, 1, 5)
    policy = S.Policy(100.0, 0.0)
    result = S.evaluate([], [d1, d2], policy, 2)
    assert result["starts"] == 1
    assert result["undecided"] == 1
    assert result["passes"] == 0
    assert result["mean_pass_days"] is None
    assert result["restricted_mean_days"] == 2


def test_25k_rules_use_20_micro_cap_and_1250_target():
    d = date(2026, 1, 2)
    candidate = trade(risk=49.0, gross=70.0)
    policy = S.Policy(2_000.0, 0.0)
    qty = S._entry_qty(candidate, policy, 0.0, -1_000.0, [], S.RULES_25K)
    assert qty == 19  # MLL reserve is tighter than the nominal 20-micro cap.
    outcome_25k, _ = S.simulate_window(
        {d: [candidate]}, [d], policy, S.RULES_25K
    )
    outcome_50k, _ = S.simulate_window(
        {d: [candidate]}, [d], policy, S.RULES_50K
    )
    assert outcome_25k == "pass"
    assert outcome_50k == "undecided"
