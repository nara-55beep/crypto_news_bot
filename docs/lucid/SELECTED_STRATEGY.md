# Selected strategy

## Status and scope

**EXPERIMENTAL — PROXY EVIDENCE.** The selected configuration is research for the LucidPro 25K evaluation. It is not validated for live execution, does not apply to a funded account, and does not imply a likely pass or profit.

LucidPro 25K was selected because its published $1,250 target and $1,000 maximum-loss limit produce the most favorable target-to-drawdown ratio (1.25) among the public Pro sizes checked on 2026-08-14. The other public programs and sizes remain available in the page for transparent rule comparison, but switching the selector does not transfer the evidence to that account.

## Exact strategy

The portfolio admits at most three trades per New York session and uses three independent sleeves:

1. **MNQ opening drive.** At the completed 09:44–09:45 bar, require a directional move of at least 25% of the prior regular-session range and a close in the outer 20% of the opening range. Enter on the next minute. Stop at the session open and target 2R.
2. **MES opening-gap fill.** Require an opening gap of at least 10% of the prior regular-session range, a completed opening drive back toward the prior close, and a close in the outer 20% of the opening range. Enter on the next minute. Stop one tick beyond the opening-range extreme and target 1.5R.
3. **MNQ prior-range breakout.** Require a completed 15-minute close beyond the session open by at least 25% of the prior regular-session range. Enter on the next minute. Stop one tick beyond the signal-bar extreme and target 2R.

All observations used to form a signal are completed before entry. If a stop and target are both touched inside the same one-minute bar, the simulator takes the stop first. A gap through a stop exits at the worse opening price. All positions are flat by 16:00 New York time in the research policy.

## Risk policy

- Opening-drive and gap-fill risk budget: **$400** each.
- Prior-range breakout risk budget: **$100**.
- At least **$100** of the published maximum-loss room is reserved.
- No new trade after two losses in a session.
- No new trade after $600 of realized session profit.
- Integer micro contracts only, subject to the published account contract cap.
- New positions are blocked if open stop risk plus proposed risk would consume the remaining loss room.
- No entry during the two minutes before or after a configured high-impact USD event; this is a stricter research control, not a statement that every Lucid evaluation program imposes it.

## Why this candidate

The selection did not choose the largest point estimate. It favored the combined portfolio because it had more test trades, broader opportunity coverage, a positive after-cost expectancy, lower dependence on a single sleeve, and materially better stressed completion rates than the standalone prior-range breakout. The MES gap-fill sleeve has a stronger standalone point estimate but only 128 test trades; the portfolio has 429 accepted test trades.

The former five-strategy basket is retained in the candidate table as **INVALIDATED**. Its attractive 98% result used future information and optimistic fills and must not be used as evidence.

## Sensitivity reality

The artifact varies seven signal parameters one at a time at two neighboring settings as well as the frozen selected setting. Forty-five-session pass rates span 41.67%–55.95% across those alternatives; the chosen configuration remains 47.28%. Several alternatives look better on this already-inspected test interval—for example the 1.5R NQ drive target reports 55.95%—but they are **not** adopted, because choosing them now would be test-set tuning. The table on `/lucid-lab` exposes all 21 rows, trade counts, expectancy, pass, breach and unfinished rates.

Annual forward slices are also heterogeneous: 65.02% in 2022, 55.56% in 2023, 35.61% in 2024, 60.00% in 2025 and 46.32% in the partial 2026 slice. This is evidence of regime dependence, not a stable guaranteed pass rate. The selected version remains frozen until genuinely new point-in-time data can judge it.

## What would change the status

Promotion requires point-in-time CME futures data, bid/ask or defensible execution observations, a frozen rule version, a new untouched forward period, verified current Lucid rules, independent reproduction, and a result that remains useful after realistic costs and uncertainty. Until then, the only accurate label is experimental proxy research.
