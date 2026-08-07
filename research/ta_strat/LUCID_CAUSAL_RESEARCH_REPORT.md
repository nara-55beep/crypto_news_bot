# Lucid causal strategy research — 2026-07-31

## Verdict

No tested strategy demonstrates that a LucidPro evaluation will pass in an average
of two weeks. The best genuinely causal signal is an NQ/ES opening-gap reversal
pair, but it passes too few rolling starts to support that claim.

Do not buy an evaluation based on these results.

## Continued 20-30 session research

After the target was relaxed from 12 sessions to 20-30 sessions, the search was
expanded again. The added causal families were:

- low reward/risk opening, gap, and ORB variants designed for a high hit rate;
- jointly risk-managed ES/NQ opening portfolios;
- completed-block VWAP deviation continuation and mean reversion;
- daily session drift, permanent directional bias, gap direction, and
  prior-session direction;
- fixed 30/60/120/240/389-minute opening-signal holding horizons;
- consecutive-close reversal and continuation with up to three sequential trades;
- the published Concretum/Zarattini same-clock 14-day noise-area momentum rule;
- NQ noise-area/opening-drift blends and a third ES diversification sleeve.

The strongest frozen candidate selected on 2016-2023 was:

1. NQ observes the completed 09:30 minute and follows its direction from the
   09:31 open until the 15:59 minute open.
2. ES observes the completed first five minutes and follows their direction from
   the 09:35 open until the 13:30 minute open.
3. Each uses a protective stop 0.50 times the prior completed RTH range.
4. The 25K policy requests $500 NQ risk and $200 ES risk, then applies integer
   micros, the aggregate 20-micro cap, commission, and reserved MLL room.

Its exact rolling-start result was:

| Period | Pass by 20 | Fail by 20 | Pass by 30 | Fail by 30 | 30-day timeouts |
|---|---:|---:|---:|---:|---:|
| Train | 36.3% | 0.0% | 37.7% | 0.0% | 62.3% |
| Validation | 40.4% | 0.0% | 40.6% | 0.0% | 59.4% |
| Holdout | 36.4% | 0.0% | 39.1% | 0.0% | 60.9% |

In the holdout, the 30-day median among winners was five sessions and the mean
among winners was 6.69 sessions. Those numbers exclude 372 of 611 starts that did
not pass and therefore are not an average time to pass. Capping every non-pass at
30 sessions gives a restricted all-start mean of 20.88 sessions; it still does not
mean those accounts passed.

The canonical 14-day noise-area variant survived the chronological test at PF 1.44,
but only 34.2% of rolling starts passed by day 30. Adding it to opening drift reached
40.8% in one final-period policy, while the development-ranked leader reached 37.8%.
The three-sleeve ES/NQ portfolio reached 38.6%. None satisfied an all-start or
guarantee standard.

Reproduce the frozen result with:

```powershell
python lucid_locked_candidate.py
```

## What was tested

All new research uses completed information and next-bar execution. The search
covered:

- causal repairs of VWAP, Turtle Soup, NR7, opening-range, prior-range, and
  trend-pullback rules;
- fixed-time morning drive, gap continuation/fill, opening reversal, and closing
  momentum;
- ES/NQ relative value and cross-market half-hour seasonality;
- volatility jumps;
- walk-forward logistic and histogram-gradient-boosting classifiers;
- weekly failed-auction and prior-day sweep/retest rules;
- individual-market half-hour seasonality;
- overnight-gap forecasts;
- one-, three-, and five-day continuation/reversal;
- 30-minute moving-average continuation/reversal;
- signal-confluence and portfolio/risk-policy variants.

The rejected families either had PF below 1 in development, failed chronological
holdout, or had too little frequency to improve the evaluation result.

## Surviving signal

The quality-oriented pair is:

1. Compute the gap from the prior completed RTH close to today's 09:30 New York open.
2. Require an absolute gap of at least 0.20%.
3. Wait for the first 30 minutes to complete.
4. Require the 09:30–10:00 move to turn against the gap.
5. Enter at the 10:00 one-minute open, fading the gap, with one adverse tick.
6. NQ protective stop: the completed 09:59 ATR scaled by `sqrt(30)`.
7. ES protective stop: one tick beyond the completed first-30-minute extreme.
8. The protective stop is the only resting intrabar order. A 2R profit exit requires
   a completed one-minute close beyond the target and executes at the next open.
9. Pay one adverse exit tick and $1.00 round-turn commission per micro.
10. Use integer micros, aggregate 20-micro cap, and flatten by 15:59 New York.

The protective-stop/close-target design removes favorable high/low ordering. If the
stop trades during a minute, it wins priority because the profit condition cannot be
known until that minute closes.

At $500 planned risk on the proxy data:

| Period | Trades | PF | Net | Average/trade | Max drawdown |
|---|---:|---:|---:|---:|---:|
| 2016–2021 train | 815 | 1.14 | +$25,231 | +$31.0 | -$11,436 |
| 2022–2023 validation | 353 | 1.20 | +$15,234 | +$43.2 | -$5,637 |
| 2024–2026 holdout | 427 | 1.26 | +$21,608 | +$50.6 | -$6,613 |

These are strategy statistics, not a promise of evaluation success.

## LucidPro 25K rolling-window result

The development-selected fast policy uses up to $900 planned risk while reserving
room to the prior-EOD MLL and respecting the 20-micro cap.

| Period | Pass by day 12 | Fail by day 12 | Pass by day 30 |
|---|---:|---:|---:|
| Train | 32.5% | 0.1% | 37.7% |
| Validation | 37.0% | 0.0% | 37.6% |
| Holdout | 33.9% | 0.0% | 35.2% |

The holdout median of three days is conditional on the minority that passed. It is
not an average across all starts.

A fixed 252-session holdout cohort makes the censoring explicit:

| Planned risk | Pass by 12 | Pass by 30 | Pass by 252 | Conditional mean | Restricted all-start mean |
|---|---:|---:|---:|---:|---:|
| $300 | 20.1% | 44.5% | 52.7% | 18.7 days | 129.1 days |
| $400 | 29.0% | 41.6% | 42.2% | 10.2 days | 150.0 days |
| $700 | 39.8% | 41.1% | 41.1% | 4.2 days | 150.1 days |
| $900 | 38.8% | 39.8% | 39.8% | 3.9 days | 153.1 days |

The restricted mean counts every non-pass as 252 days. It is still optimistic,
because those accounts had not passed at the end of the horizon. High planned risk
creates many accounts that remain above the loss threshold but lack enough MLL room
to take another micro; calling only their successful peers a “3.9-day average” would
repeat the original statistics error.

The higher-frequency NQ-15-minute/ES-30-minute alternative did not improve the
result. Its best holdout 12-day pass rate was 33.4%.

## Data provenance

The files called `es_1m_10y.csv`, `nq_1m_10y.csv`, and `cl_1m_10y.csv` are not CME
trade data:

- ES is Dukascopy `USA500IDXUSD`;
- NQ is Dukascopy `USATECHIDXUSD`;
- CL is Dukascopy `LIGHTCMDUSD`;
- bars are midpoint-quote OHLCV proxies.

The available April–June 2026 Yahoo continuous E-mini files provide a useful recent
signal comparison:

- NQ: 16 actual-futures signals, 16 proxy signals, all dates and directions matched;
- ES: 20 actual-futures signals, 19 proxy signals, all 19 common directions matched.

This supports signal transfer over a small recent sample. It does not validate ten
years of CME fills, contract rolls, spread, slippage, or MLL excursions.

## Reproduction

Run from `research/ta_strat`:

```powershell
python lucid_barclose_gap_search.py
python lucid_exact_portfolio.py
python lucid_proxy_signal_audit.py
python -m pytest -q test_lucid_causal_rebuild.py test_lucid_portfolio_policy.py test_lucid_barclose_execution.py
```

Key result files from this run:

- `barclose_gap3.out`
- `exact_portfolio4.out`
- `barclose_audit3.out`
- `gap_es.out`, `gap_nq.out`, `gap_cl.out`
- `pair.out`, `ml_hgb.out`, `auction_es.out`, `auction_nq.out`
- `retest_es.out`, `retest_nq.out`, `retest_cl.out`
- `slot_es.out`, `slot_nq.out`, `slot_cl.out`, `slot_pair.out`
- `overnight2.out`, `daily_es.out`, `daily_nq_r500.out`, `daily_cl.out`, `ma.out`

## What is required before this can become a tradable claim

1. Replace the CFD proxies with at least ten years of front-contract CME ES/NQ trade
   and quote data with explicit roll mapping.
2. Re-run the same frozen rules without another parameter search.
3. Model bid/ask execution and stop-market slippage from tick or quote data.
4. Keep a final untouched period and then run a forward paper shadow using the same
   bar builder and order semantics.
5. Reject it unless the all-start pass-time statistic—not only successful accounts—
   satisfies the requested threshold.
