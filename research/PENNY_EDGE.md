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

## The catalyst requirement, tested instead of assumed

The desk now refuses to call anything a setup without a dated catalyst. That is the
right instinct - the price/volume core alone has zero gross expectancy, so an event
is the only plausible source of one - but as shipped it was marked `COLLECTING` and
would have needed years of forward signals before anyone could tell.

`research/edgar_catalysts.py` makes it testable today. The refreshed schema retains
the SEC's exact `acceptanceDateTime`, accession, primary document and 8-K item codes;
the decision clock no longer pretends a date-only field says whether a filing arrived
before or after the market close. All 695 panel symbols mapped to a CIK, yielding
**70,278 8-K events** and **24,105 offering filings** across ten years.

On train+validation the catalyst filter looked like a real discovery - gross return
before costs, so the comparison is not contaminated by the cost model:

| Cell | Rows | Gross |
|---|---:|---:|
| no 8-K within 30 days | 109,489 | +0.167% |
| 8-K today or yesterday | 10,946 | +0.694% |
| hot tape + 8-K within 1 day | 4,342 | +0.655% |
| hot tape + 8-K 1d + no offering in 90d | 2,983 | **+0.912%** |

A five-fold improvement over the no-catalyst baseline, with a confidence interval
that excluded zero. The offering filter pointed the same way: names with an offering
filed inside 30 days returned +0.134% against +0.316% for those without.

### It does not survive the untouched period

The rule was pre-registered from that development evidence - top hype quintile, 8-K
within one day, no offering in 90 days, threshold taken from the development window -
and run **once** on 2025 onward:

| | Setups | Gross | 95% CI |
|---|---:|---:|---|
| Untouched test | 2,783 over 390 signal days | **-0.115%** | [-0.935%, +0.705%] |

Zero, and negative at every assumed cost from 0.35% to 2.00%. The development result
was a search artifact: roughly a dozen cells were examined, so one interval excluding
zero is what chance produces. This is the same lesson `penny_stats.py` was built to
enforce, arriving one level up - it is not enough to correct for searching over
*strategies* if the *features* are searched uncorrected.

### One real finding did come out of it

The research cost model is not neutral. It floors at 1.00% by construction
(`max(0.01, cost)` in `estimated_round_trip_cost`), while the live scanner's own
trusted quotes on sub-$5 names measure 0.08%-0.75%, mean **0.335%**. The model
therefore charges about **4.6x** the observed spread.

That does not rescue anything here - the test-period gross is negative before any
cost at all - but it matters for future work. Any conclusion of the form "the signal
is real but smaller than costs" drawn from this harness is partly a modelling
assumption, and should be re-derived against measured spreads before being believed.

## Item-aware 8-K drift: useful safety data, still no edge

The date-only test above mixed fundamentally different events. Version
`sec-item-drift-v1-2026-08-08` fixes that: it separates earnings (2.02), agreements
and asset sales (1.01/2.01), Regulation FD/other material disclosures, and adverse
items. Entry is causal: exact SEC acceptance, then the first fully observable reaction
close, then the following session's open. Six rules and two holding periods were
declared before inspecting their split results, with 0.50% base and 1.00% stress costs.

| Predeclared rule | 2025+ trades | Mean net | Profit factor |
|---|---:|---:|---:|
| earnings drift, 5 sessions | 27 | **-3.639%** | 0.34 |
| earnings drift, 10 sessions | 27 | **-7.321%** | 0.14 |
| agreement drift, 3 sessions | 5 | -2.860% | 0.73 |
| agreement drift, 5 sessions | 5 | -6.183% | 0.46 |
| FD/other drift, 3 sessions | 22 | +0.627% | 1.17 |
| strict material drift, 5 sessions | 5 | -4.442% | 0.45 |

The small positive FD/other cell is not evidence of an edge: its 95% interval is
[-3.20%, +4.77%], it has only 21 signal days, and the six-rule family-wise test gives
p=0.393. The development winner, five-session earnings drift, reversed from +6.41%
in train and +1.08% in validation to -3.64% in 2025+. Also, 2025+ had already been
viewed by the broader catalyst study, so this is conservative evidence for rejection,
not a new pristine holdout.

The operational conclusion is still valuable. Item codes can identify and hard-block
bankruptcy (1.03), covenant/default (2.04), impairment (2.06), delisting (3.01),
unregistered equity sales (3.02), and non-reliance/restatement risk (4.02). But a code
does not reveal whether an earnings release or agreement is economically good. The live
bot therefore uses SEC items for immediate discovery and adverse-event rejection, not
as unearned bullish score points.

## Point-in-time quarterly fundamentals: promising numbers, no sample

`research/sec_fundamentals.py` next reconstructs standalone 10-Q quarters from SEC
Company Facts. Each value stays tied to its original accession, so a comparative column
in next year's filing cannot rewrite an old signal. The audit covers 5,523 eligible
quarterly events from 425 issuers and checks profitable revenue growth, faster growth,
profit turnarounds and profit acceleration.

The apparent 2025+ results are positive, but each rule has only **1-6 trades**. The
selected profitable-growth rule has 4 train trades at -4.275%, 3 validation trades at
-0.914%, and 6 later trades at +1.318%; its bootstrap interval is
[-10.51%, +18.18%]. Other positive-looking variants reuse almost exactly those same
few events. This is far below the predeclared 40/25/40 trade gates and cannot support
an accuracy or profitability claim. Tuning thresholds around those outcomes would be
test-set overfitting, so the audit stops here and reports `REJECTED`.

## Continuous live discovery

The scanner now polls the newest SEC 8-K feed page on every cache cycle and intersects
it with the complete current Yahoo penny-stock eligibility query before allocating
analysis slots. This catches a filing before its issuer has to become a top mover, while
excluding large caps, funds and warrants. The current-universe result is cached for 15
minutes and the SEC page for five; a measured cold scan fell from about 101 seconds for
ten stale filing pages to 7.5 seconds for the continuously polled current page.

Regular mover, volume and short-interest passes still run independently and are
interleaved with the SEC candidates. The paper service continues around the clock using
its market-state cadence; a hot setup increases scan frequency, but closed-market scans
remain slower because there is no executable opportunity to miss.

## Exact-strategy execution contract

Research approval no longer transfers between strategies. The live scanner identifies
itself as `live_sec_news_align_marketwide_v5`; the policy loader and the final fill path both require the
validated policy to name that exact implementation. A future validation of
`profitable_earnings_beat`, for example, cannot unlock the unrelated composite/AI
scanner. Mismatches fail closed as `STRATEGY_MISMATCH` while candidates continue to be
logged for forward measurement.

## Run it

```powershell
.\venv\Scripts\python.exe research\penny_edge_research.py
.\venv\Scripts\python.exe research\penny_event_drift.py
.\venv\Scripts\python.exe research\penny_fundamental_drift.py
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
- `data/pennystock_event_drift_report.json` - item-aware 8-K audit.
- `data/pennystock_fundamental_drift_report.json` - point-in-time 10-Q audit.

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

## Corrected marginal item-code audit

An initial item-code report claimed v3 was "backwards." That report did not support the
claim: it included observations when the stocks were not penny stocks, let material and
adverse groups overlap, omitted live adverse code 3.02, counted repeated same-day
filings separately, paired a trade-weighted mean with a day-weighted interval, treated
gross return as net in the search test, and called already-viewed 2025+ data untouched.
It also treated the Submissions API's `Z` timestamp as Eastern. A direct SEC-file check
shows the raw header's 07:07:44 Eastern acceptance represented as 11:07:44Z in the API.

`item-code-marginal-v2-2026-08-08` corrects those errors. It uses the same UTC-to-New
York event clock as the causal event audit, collapses filings to one symbol/reaction-day,
makes the groups mutually exclusive with the adverse veto first, applies the historical
penny price and live share-volume floors, and uses a ten-session market-calendar block
bootstrap. It reports equal-weight signal-day baskets and a 0.50% cost separately.

### All eligible item events

| Window | Material events | Gross basket | Gross 95% CI | Net after 0.50% |
|---|---:|---:|---|---:|
| Development through 2024 | 3,717 | +1.812% | [+0.797%, +2.942%] | +1.312% |
| Post-2024, already viewed | 4,226 | -0.301% | [-1.686%, +1.101%] | -0.801% |

The development effect disappears and changes sign. The later gross interval includes
zero, so these results do **not** prove the category is inverted; they show that its
earlier positive drift was unstable and cannot be used as a standalone edge.

### Reaction-confirmed proxy

The closer proxy also requires a 3%-30% reaction, elevated volume, a strong close,
bounded volatility, no recent parabolic move and no offering inside 90 days. It still
cannot reconstruct historical headlines, fundamentals, quotes or v3's two separated
observations, so it is not labelled an exact live-rule backtest.

| Window | Material events | Gross basket | Gross 95% CI | Net after 0.50% |
|---|---:|---:|---|---:|
| Development through 2024 | 198 | +1.298% | [-0.575%, +3.182%] | +0.798% |
| Post-2024, already viewed | 210 | -0.043% | [-1.772%, +2.010%] | -0.543% |

Neither window resolves a non-zero gross effect, and the exploratory search across 16
individual codes fails its family-wise test (p=0.467). The defensible conclusion is
`NO_STANDALONE_ITEM_CODE_EDGE`: use item codes for discovery and adverse-event safety,
not for directional prediction. This matches the deployed implementation—v3 gives no
bullish score merely because a filing carries a "material" code.

## Exit structure: useful hypothesis, failed deployment gate

The first exit audit made an unsafe inference. It treated 5,187 broadly eligible 8-K
events as though they were v3 entries, simulated ten trading sessions even though the
bot uses ten calendar days, and quoted a family-wise test of the best rule versus zero
as evidence that target removal beat the current exit. It also used maximum path price
to argue that a capped exit cannot win; a target plainly can win when price reaches the
target and subsequently reverses.

Version 2 fixes those errors. It uses the first session on or after the ten-calendar-day
deadline, applies conservative same-bar stop/trail handling, measures paired differences
against the current stop + 2.5R target + trail, and runs the family-wise test on those
differences. It reports both the broad sample and a reaction-confirmed proxy with price,
reaction, volume, close-location, liquidity, volatility, dilution and three-entry daily
capacity filters.

### Direct result for removing only the fixed target

| Scope/window | Events | Signal days | Paired basket effect | 95% CI |
|---|---:|---:|---:|---|
| Broad 8-K, development | 5,187 | 1,520 | +0.164% | [-0.056%, +0.454%] |
| Broad 8-K, post-2024 reused | 6,017 | 389 | +0.022% | [-0.080%, +0.127%] |
| Reaction proxy, development | 59 | 57 | -0.157% | [-0.765%, +0.536%] |
| Reaction proxy, post-2024 reused | 53 | 47 | -0.168% | [-0.593%, 0.000%] |

The claimed target-removal improvement no longer clears zero after matching the live
calendar horizon and handling daily-bar ambiguity conservatively. More importantly, its
sign reverses in the closer reaction-confirmed proxy. Removing both target and trail is
also negative in that proxy: -1.544% in development and -0.383% post-2024, with both
intervals crossing zero. The proxy is sparse enough that the six-slot limit causes no
additional skips, so it provides no evidence about portfolio-capacity gains.

Because a daily bar cannot reveal whether its low occurred before or after a new high
tightened the trail, the audit also repeats target removal while deferring a newly raised
trail until the next bar. Deployment requires agreement under both conventions; neither
reaction-proxy interval clears zero under the deferred convention either. This prevents
an arbitrary same-bar assumption from deciding the production default.

`PENNY_FIXED_TARGET` therefore defaults **on** again. It can be set to `0` for an
explicit paper experiment, but not as a claimed improvement. The exit result is
`REJECTED_FOR_DEPLOYMENT` until an exact point-in-time v3 sample, executable quotes,
intraday paths and a delisted-inclusive universe exist. The post-2024 window has already
been examined and is sensitivity evidence, not a clean holdout.

### A hypothesis that failed, recorded so it is not retried

The reverse idea - that dilution and distress features predict *harm* even though nothing
predicts gains - was tested and falsified. Every flag (recent offering, adverse 8-K item,
lottery run-up, sub-$1, unstable range) associated with *higher* mean excess return, the
harm score rose monotonically from +0.94% to +17.7%, and family-wise p=0.999. Those flags
select for volatility, and in a right-skewed tape volatility raises the mean without
improving the median. Selecting on variance is not selecting on edge.

## Feasibility is a planning constraint, not an edge verdict

The exit scope error exposed a useful design question: before collecting a new rule, can
its event rate and variance plausibly resolve an economically relevant effect? The first
feasibility implementation answered that question too strongly. It called the
reaction-confirmed 8-K proxy “the desk's own entries,” ignored serial dependence from
overlapping returns, described total required history as additional time, and translated
“longer than a chosen five-year horizon” into “unprovable.”

Version 2 uses equal-weight signal-day baskets and the same circular market-calendar
block bootstrap as the other audits. It distinguishes total history from additional
history and reports `INFEASIBLE_WITHIN_HORIZON`, not an impossibility claim. Its current
2% effect / 95% confidence / 80% power calculation is:

| Stream | Events | Signal days | History | Block-adjusted MDE | Additional years for 2% |
|---|---:|---:|---:|---:|---:|
| Broad 8-K proxy | 11,204 | 1,909 | 9.5y | 1.08% | 0.0 |
| Reaction-confirmed 8-K proxy | 112 | 104 | 8.5y | 3.52% | 17.8 |
| Exact prospective v4 | 0 completed | 0 | — | not assessable | not assessable |

The reaction-confirmed stream is still only a closer historical proxy. It lacks
point-in-time headlines, fundamentals, AI vetoes, two-scan persistence, executable
quotes, regime state and delisted names. Therefore its 17.8-year estimate cannot be
promoted to a claim about the exact v4 rule. It says that this proxy is impractical to
evaluate within five more years if its signal rate and dependence-adjusted dispersion
remain stationary.

`research/penny_feasibility.py` now exposes those assumptions and is also connected to
the paper bot's prospective `forward_validation` output. The prospective tracker still
cannot authorize trades; the reproducible edge policy remains `REJECTED`. Until exact v4
outcomes accumulate over enough calendar history, its own rate and power are correctly
reported as `not assessable` rather than guessed from a proxy.

Feasibility passing means only that a specified effect size can be measured with the
chosen confidence and power. It says nothing about whether the effect exists, whether it
is positive, or whether it survives execution costs.

## Live implementation integrity correction: v4

The next code audit found that the v3 identifier overstated what the implementation
required. `has_dated_catalyst()` accepted either a fresh Yahoo headline or a material
SEC event, so a potentially promotional press release could qualify without EDGAR
corroboration. The confirmation state also counted observations when the displayed
spread was only an ADV proxy, and a provider quote rejected as internally implausible
could still reach the paper-fill path because the raw `spread_reliable` flag remained
true.

`live_sec_news_align_v4` fixes that contract:

- a candidate requires a non-adverse material 8-K and a headline timestamp aligned
  within 24 hours; the AI must still confirm direction and can only veto;
- confirmation hits require an unlocked, internally plausible regular-session bid/ask
  with a last-trade freshness proxy of at most five minutes;
- an ADV spread proxy can rank a research candidate but can never confirm or fill it;
- every prospective record now retains the SEC accession/items, headline snapshot, AI
  model/verdict, bid/ask, quote age, market state, and exact decision timestamp.

The strategy and signal-engine identifiers changed, so older prospective observations
cannot silently mix with v4. This is an evidence-integrity and false-positive reduction,
not proof of profitability. The reproducible policy remains `REJECTED`, and v4 starts a
new forward sample at zero.

## Market-wide discovery correction: v5

The old full scan did not scan the full market: three Yahoo sorts supplied at most 60
names for deep scoring. Calling the resulting top-20 board a market scan overstated its
coverage. `live_sec_news_align_marketwide_v5` now requests a delayed consolidated
snapshot for every active, tradable, non-OTC U.S. equity in Alpaca's master asset list,
then identifies the full $0.10-$5 price population before selecting expensive dossiers.

The stages remain separate and visible: assets requested, snapshots returned, priced
assets, penny-price matches, deep dossiers, and leaderboard rows. SEC candidates,
movers, volume leaders and tight books are interleaved so one ranking cannot own every
deep-analysis slot. Real-time confirmation still requires a fresh execution-feed book;
the delayed universe snapshot can discover a name but can never confirm or fill it.

OTC is explicitly excluded because Alpaca restricts OTC market data to broker partners.
This is broader discovery, not proof of edge. It changes the sampled population, so the
signal engine moves to 7 and forward evidence restarts rather than pooling v4 and v5.
