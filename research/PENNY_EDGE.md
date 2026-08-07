# Penny-stock edge audit

The live penny-stock desk is deliberately locked from opening new positions.
The current evidence status is `REJECTED`, not because the scanner is broken,
but because no tested rule survived the final out-of-sample and transaction-cost
checks.

## What the audit prevents

- Signal-day close data cannot fill at that same close; entries occur at the next
  available session open.
- Ambiguous daily bars assume the stop was hit before the target.
- Every trade pays a dynamic 1%-4% round-trip cost proxy. The robustness test
  doubles that cost.
- Repeated signals in one name are de-duplicated, and new entries are capacity
  capped by date.
- Strategy selection uses train data through 2022 and validation data from
  2023-2024. The period starting in 2025 is opened only for the final audit.
- A passing numerical result still cannot enable automatic trading when the
  universe contains only today's survivors.

## Result

The original breakout variants lost roughly 1.3%-3.0% per trade after costs in
the final test period. A profitable-EPS-beat event rule was the only development
winner:

| Period | Trades | Mean net/trade | Win rate | Profit factor | Mean at 2x costs |
|---|---:|---:|---:|---:|---:|
| Train, through 2022 | 59 | +3.284% | 54.2% | 1.64 | +2.007% |
| Validation, 2023-2024 | 34 | +3.799% | 61.8% | 1.76 | +2.491% |
| Final test, 2025 onward | 74 | **-1.037%** | 41.9% | 0.84 | **-2.316%** |

Optimizing against the failed test period would turn the test into more training
data and manufacture a result, so the rule stays rejected.

## The result was never significant in the first place

Reading the table as "it worked, then the regime broke" is not supported. The
original inference also had a flaw: its blocks advanced through irregular signal
events, so outcomes months apart could become adjacent observations. Version
`calendar-block-v2-2026-08-07` instead aligns every rule to the IWM market-session
calendar, forms one equal-weight basket per signal day, and resamples contiguous
20-session blocks. That preserves idle gaps, same-day clustering, and more of the
serial dependence caused by overlapping 10-session outcomes.

The White-style family-wise test covers all six searched strategies across 2,039
market sessions (575 sessions had at least one candidate signal). Calendar-block
noise reproduces a winner this large **13.4% of the time** (p=0.134), so the rule
does not clear the predeclared 5% threshold.

The uncertainty calculation now reports two distinct estimands rather than declaring
one "correct":

| Window | Signal-day basket | Trade-weighted | Block t | Smallest basket edge resolvable |
|---|---:|---:|---:|---:|
| Train + validation | +3.09% | +3.47% | +1.18 | 7.34% |
| Untouched test | +0.02% | -1.04% | +0.01 | 5.89% |

The test conclusion changes sign with same-day weighting. The trade-weighted result
answers the average-trade question; the signal-day basket gives each active date equal
weight and handles same-day clustering. Neither is a capital-weighted portfolio path
with overlapping positions, so both remain visible and the strategy fails either way:
the trade result is negative and the basket estimate is indistinguishable from zero.

## What would actually change the answer

Minimum detectable effect still improves with more independent information, but the
calendar-block estimate shows that the independence formula understated uncertainty by
about 22% over the full sample. Running more strategy variants is not a lever; it raises
the search penalty without adding data. To resolve a +2% signal-day basket edge:

- at the current **1.22 names per signal day**, you need about 872 signal days:
  roughly **59 years** at the observed opportunity rate;
- the independence calculation says at least **8 names per signal day** could make
  the current history sufficient, but this is only an optimistic floor because penny
  stocks co-move.

So breadth is the binding constraint - but it **cannot be bought by raising
`max_entries_per_day`**. Re-running the rule at caps of 3, 6, 10, 15 and 25 returns
byte-identical results: the rule peaks at 3 names on only 6 of its 137 signal days
and never produces a fourth. The cap is not binding; the rule is *signal-starved*.
`detectability_plan()` now reports this directly as `breadth_is_signal_limited`, so
the audit names the lever that is actually available instead of one that is already
maxed out.

The available levers are therefore universe size and filter width. The panel holds
695 current sub-$5 names, of which 395 produced any earnings event, and the rule's
filters reject 98.5% of the 10,964 events it saw. Widening the price ceiling or the
universe would raise event count roughly in proportion. Loosening the filters instead
is a trap worth naming: it raises `n` while diluting signal quality, and if the mean
falls as fast as the dispersion, the t-statistic does not move at all. Either change
is a new strategy that needs its own untouched test period, not a tuning knob.

The delisting calculation is now labelled an illustrative scenario, not a bound or a
survivorship correction. Under its configurable 7% annual hazard and -55% delisting
return assumptions, the mechanical ten-session drag is about -0.16 percentage points.
That calculation cannot measure the larger bias created by building the entire history
from names known to survive today. Survivor-only membership therefore continues to
block `VALIDATED` status.

## The live rule itself: measured at last

Every strategy above is one the live desk does not run. The scanner runs
`live_composite_v1`, and the exact-strategy contract correctly refuses to let an
unrelated result authorize it - which left the deployed rule with no evidence in
either direction. `research/penny_live_audit.py` closes that gap by rebuilding a
Dossier from historical bars and calling `pennystock_bot`'s own `hype_score`,
`technical_score` and `tradeability`, with the live weights, stop, target, 12%-armed
8% trail, 10-session cap and 6-name capacity. It is the live code path, so it cannot
drift from what actually trades.

Quality, catalyst and the AI review have no free point-in-time history. They sit at a
neutral constant, so what is measured is the **price/volume core** - 65% of the
composite weight, and the only part that moves the ranking.

| Split | Trades | Mean net | Win rate | PF | Bootstrap 95% |
|---|---:|---:|---:|---:|---:|
| Train, through 2022 | 9,552 | -1.270% | 37.0% | 0.70 | [-1.51, -1.04] |
| Validation, 2023-24 | 3,012 | -1.664% | 37.5% | 0.69 | [-2.12, -1.21] |
| Untouched test, 2025+ | 2,394 | **-2.459%** | 35.1% | 0.61 | [-3.03, -1.88] |

Unlike the earnings rule, this is **not** an underpowered null. 14,958 trades over
339,853 scored symbol-sessions gives a minimum detectable effect of 0.58%, and
t = -6.62 on train+validation and -4.98 on the untouched test. The rule loses, and
the sample is more than large enough to say so.

### Why it loses, which is the part that matters

| | Gross (pre-cost) | Cost | Net |
|---|---:|---:|---:|
| All 14,958 trades | **+0.006%** | 1.546% | -1.540% |

Gross expectancy is **zero**, with a 95% interval of [-0.19%, +0.21%]. This is the
distinction `cost_decomposition()` exists to draw, because the two cases demand
opposite responses:

- *costs dominate a real signal* -> the lever is execution: venue, spread, hold length;
- *gross expectancy is zero* -> there is nothing to keep, and *no* execution
  improvement can rescue it.

This is the second case. Breakeven would require a 0.006% round trip, and even then
the strategy would earn nothing.

### The ranking carries no information

`rank_information()` scores the entire cross-section - 192,678 rows, not just the six
names a day the rule would have bought - and sorts it into score deciles:

| Score | Spearman rho | Top-minus-bottom | Every decile negative? |
|---|---:|---:|:--|
| composite | +0.069 | +0.75% | yes |
| hype | -0.006 | -0.25% | yes |
| technical | +0.020 | -0.46% | yes |

Every decile of every component loses money. The composite's slight gradient is
+0.75% top-to-bottom against a 2.01% round trip, so it cannot fund a single trade;
the verdict is reported against that cost hurdle rather than by direction alone.
`hype` - RVOL and size-of-move, the scanner's heaviest weight - has a rank
correlation of essentially zero, and `technical` is mildly *inverted*, consistent
with the published finding that lottery-like daily winners underperform afterwards.

The practical conclusion is narrow and firm: within this universe, holding period and
cost structure, there is no ranking of these inputs that produces an edge, so
improving the scoring weights is not a promising direction. Changing the universe,
the holding period, or the cost structure are the only levers left, and each is a new
strategy requiring its own untouched test.

## Exact-strategy execution contract

Research approval no longer transfers between strategies. The live scanner identifies
itself as `live_composite_v1`; the policy loader and the final fill path both require the
validated policy to name that exact implementation. A future validation of
`profitable_earnings_beat`, for example, cannot unlock the unrelated composite/AI
scanner. Mismatches fail closed as `STRATEGY_MISMATCH` while candidates continue to be
logged for forward measurement.

## Run it

```powershell
.\venv\Scripts\python.exe research\penny_edge_research.py
```

Refresh the current universe, price history, and reconstructed earnings events:

```powershell
.\venv\Scripts\python.exe research\penny_edge_research.py --refresh
```

The machine-readable outputs are:

- `data/pennystock_edge_report.json` — complete candidates, split metrics, and
  limitations.
- `data/pennystock_edge_policy.json` — small fail-closed policy consumed by the
  live bot.

The audit hash covers the methodology version, full candidate family, inference
output, and data-snapshot metadata—not only the selected rule's split table. The live
loader cross-checks report/policy hashes, expires the audit after seven days, and permits
fills only for an exact strategy-ID match with `VALIDATED` status.

Method references: [White (2000), *A Reality Check for Data Snooping*](https://doi.org/10.1111/1468-0262.00152)
and [Künsch (1989), *The Jackknife and the Bootstrap for General Stationary Observations*](https://doi.org/10.1214/aos/1176347265).

## What would be required for a credible validation

Use a point-in-time universe that includes delisted securities, historical
unadjusted and adjusted OHLCV, actual delisting returns, and timestamped
point-in-time fundamentals/earnings estimates. The free Yahoo panel is useful
for rejecting strategies, but it is not sufficient to prove one because it
contains current survivors and reconstructed event data.
