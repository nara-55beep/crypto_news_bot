# Backtest methodology

## Research protocol

1. Load and validate real one-minute proxy data.
2. Split chronologically: development through 2021, validation 2022–2023, test 2024 onward.
3. Form signals only from completed bars and enter the next minute.
4. Apply adverse entry/exit ticks, official commission, stop-first ambiguous bars, gap-worse stops and integer quantity.
5. Select signal parameters and risk policy from development plus validation only.
6. Start a fresh Lucid account at every eligible historical session and process 20-, 30- and 45-session windows.
7. Stop a path immediately on pass or MLL breach; otherwise label it unfinished at the horizon.
8. Include no-trade sessions. Require full windows so dataset-end censoring does not change rates.
9. Repeat execution stresses and deterministic missed-trade scenarios.
10. Bootstrap daily blocks/rolling-window outcomes with a fixed seed for reproducibility.

## Account path

The account simulator processes exits before unrelated same-time entries, allows an entry-minute exit only after entry, reserves all open positions' stop risk, enforces the aggregate micro cap, blocks new entries after a soft DLL, updates the trailing MLL at session end and treats equality at the floor as breach.

## Statistics

- Pass, breach and unfinished use all eligible starting windows as denominator.
- Wilson 95% confidence intervals describe binomial pass-rate uncertainty; overlapping windows mean the interval is descriptive and likely too narrow, so a block/bootstrap range is also reported.
- Pass-time percentiles are explicitly conditional on passing. Restricted mean duration assigns the full horizon to non-passes.
- Profit factor, expectancy, win rate and payoff use net trade P&L after modeled costs.
- Cost consumption is commission + decomposed spread + slippage divided by positive gross P&L.
- Parameter sensitivity changes one predeclared dimension around the selected setting. Seven signal dimensions are each shown at the chosen setting and two neighboring values; the final test period is not used to retune the chosen value.
- Annual 2022–2026 forward slices and largest winning-trade/day/month shares expose regime and concentration dependence instead of hiding it in one aggregate.
- Monte Carlo uses session/block resampling and keeps path-dependent MLL logic. It is supplementary; it cannot add information absent from the source history.

## Validation gate

The `VALIDATED` label requires positive net expectancy, chronological test evidence, at least 200 test trades and 100 rolling starts, pass rate above breach rate, survival under spread/slippage stress, broad parameter stability, no single-period domination, tested rule math, independent review and visible limitations. The current artifact also carries version identifiers for every candidate so results from materially different selectors are not silently pooled.

The current candidate is capped at `EXPERIMENTAL — PROXY EVIDENCE` because the source is not CME execution data and prior work has repeatedly inspected the test interval. A positive point estimate cannot override those defects.
