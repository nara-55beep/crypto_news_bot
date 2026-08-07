# NQ Regime/Pullback Research — Not Approved for Funding

## Decision

This is the strongest causal lead found in the current search, but it does **not**
meet the requested “pass LucidPro within 20–30 sessions” standard. Do not buy or
trade an evaluation from these results.

The best development-selected policy under conservative execution passes a
25K LucidPro evaluation within 30 sessions on:

| Segment | 30-session pass | Modeled breach | Status |
|---|---:|---:|---|
| 2016–2021 training | 37.4% | 0.0% | Fail |
| 2022–2023 validation | 56.0% | 0.0% | Fail |
| 2024+ chronological audit | 57.9% | 0.0% | Fail |

The 2024+ segment is **not pristine out-of-sample data anymore**. It has been
viewed repeatedly during this research program and is reported only as a
chronological stability check.

## Frozen strategy tested

1. Build clock-aligned, completed five-minute NQ bars.
2. Causally standardize bar return and range from preceding bars only.
3. At each month boundary, fit a three-state GMM using only the prior 5,000
   valid bars. Canonicalize states by return mean as bearish, active, bullish.
4. Active-state sleeve: long; signals from 10:34 through 14:29 ET.
5. Bear-state sleeve: short; signals from 10:04 through 14:29 ET.
6. Place a resting limit after the completed signal bar, 0.15 prior-session
   ATR away from its close.
7. Require the midpoint proxy to penetrate the limit by one full 0.25-point
   MNQ tick before counting a fill.
8. Protective stop: 0.25 prior-session ATR, rounded away from entry to a legal
   0.25-point tick.
9. Exit 65 clock minutes after the signal; no shortened late-day horizon.
10. Never stack positions; no more than three accepted trades per day.
11. Size integer micros while reserving the complete stop loss, commission,
    and friction against the prior-EOD MLL. Aggregate cap: 20 MNQ micros.

The development-selected conservative policy risks $400 per active-state trade
and $400 per bear-state trade. “Risk” is a cap used to derive integer quantity,
not a promise that every trade risks exactly $400.

## Execution and Lucid assumptions

- $25K LucidPro: $1,250 target, $1,000 prior-EOD trailing MLL, MLL locks at
  +$100 after an EOD balance above +$1,100, no DLL, maximum 20 micros.
- Official MNQ commission: $0.50 per side ($1.00 round turn).
- Conservative research friction: two NQ points all-in, or $4.00 per MNQ
  round turn. This is materially harsher than commission alone.
- Resting limits require one-tick penetration; a midpoint touch is not a fill.
- Stops fill no better than the stop after gaps, with adverse exit slippage.
- Limit, stop, and exit prices are legal 0.25-point MNQ ticks.
- All positions are flat by approximately 15:35 ET, before Lucid’s cutoff.
- All observed sessions, including shortened or data-guard sessions, count
  toward 20/30/60-session evaluation windows even when no trade is allowed.

## Dependence-aware result

Rolling daily starts overlap heavily, so they are not independent trials.
For the equal-$300 conservative policy:

| Segment | Rolling 30d | Month-start 30d | Non-overlap phase median | NW(5) daily-P&L t |
|---|---:|---:|---:|---:|
| 2016–2021 | 37.0% | 43.9% | 37.8% | 1.48 |
| 2022–2023 | 52.3% | 47.8% | 50.0% | 4.13 |
| 2024+ | 57.5% | 56.7% | 57.1% | 5.01 |

The old-period weakness is not just an evaluation-path artifact. With fixed
$500 signal risk and conservative friction, 2016 has PF 0.58 and 2017 has
PF 0.79. Every calendar year from 2018 onward is positive, but introducing a
2018 date cutoff now would be hindsight.

A causal rolling shadow-performance gate was tested instead of a hindsight
date cutoff. Its best 30-session training pass rate was 33.9%, worse than the
unfiltered strategy, so it was rejected.

## Data repair and limitations

The source history is Dukascopy `USATECHIDXUSD`, not CME MNQ. It is a price
proxy, and its volume is not exchange volume. The final portable model therefore
uses price and range only.

The original cache contained whole missing UTC-hour blocks. A targeted refetch
recovered 87 full NQ sessions. The final research set contains 2,402 fully
clock-contiguous model/trading sessions and 2,564 observed evaluation sessions;
162 shortened or unresolved sessions remain zero-trade guard days. No missing
minute was synthesized.

This result cannot be described as identical to future paper/live MNQ trading
until it is reproduced on continuous, contract-aware CME MNQ data and then
forward-tested with the exact live order adapter.

## Other branches tested

- NQ/ES fast-alpha ATR breakouts and pre-open filters: positive in some
  segments but inadequate 30-session pass rates.
- Daily IBS/gap-state mean reversion: unstable across the chronological split.
- London GMM transition: failed on the downloaded ten-year London window.
- ES and CL GMM replications: weaker; adding them did not improve the
  development floor.
- Alternative GMM seeds and 2,500/10,000-bar fits: edge direction generally
  survived, but pass reliability remained inadequate.
- Rolling shadow-performance filters: reduced pass frequency and were rejected.

## Reproduce

From `research/ta_strat`:

```powershell
python -m unittest test_lucid_gmm_research.py test_lucid_portfolio_policy.py test_lucid_causal_rebuild.py
python lucid_gmm_robustness_research.py
python lucid_gmm_priceonly_policy.py
python lucid_gmm_window_audit.py
python lucid_gmm_meta_filter.py
```

Primary research inspiration:

- https://arxiv.org/pdf/2605.04004

Official rule references:

- https://support.lucidtrading.com/en/articles/12890029-lucidpro-evaluation-account
- https://support.lucidtrading.com/en/articles/12890136-lucidpro-drawdown
- https://support.lucidtrading.com/en/articles/11508978-approved-products-and-commissions
- https://support.lucidtrading.com/en/articles/11404729-allowed-trading-times
