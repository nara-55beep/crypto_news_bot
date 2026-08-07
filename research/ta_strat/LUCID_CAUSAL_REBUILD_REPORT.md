# Lucid causal rebuild — final research decision

Date: 2026-07-31

Decision: **NO-GO. Do not enable the old basket and do not fund a Lucid evaluation from
these results.** A modest NQ momentum effect survived causal testing, but no tested
configuration came close to a reliable pass in 12 sessions or one month.

## Rules modeled

The simulator uses the current LucidPro 50K evaluation terms:

- $3,000 target, $2,000 MLL, $1,200 soft DLL, maximum 40 micros
- EOD trailing MLL; it locks at $50,100 after an EOD balance above $52,100
- positions flat at 15:59 New York time, before the 16:45 cutoff
- $0.50 commission per side per MES/MNQ/MCL contract ($1.00 round turn)

Official references:

- https://support.lucidtrading.com/en/articles/12890029-lucidpro-evaluation-account
- https://support.lucidtrading.com/en/articles/12890136-lucidpro-drawdown
- https://support.lucidtrading.com/en/articles/12890122-lucidpro-daily-loss-limit
- https://support.lucidtrading.com/en/articles/11404729-allowed-trading-times
- https://support.lucidtrading.com/en/articles/11508978-approved-products-and-commissions

## Execution invariants

- Signals use completed, New-York-clock-aligned bars.
- Entries occur at the next one-minute open plus one adverse tick.
- Stops and targets are evaluated from the entry minute; stop wins an ambiguous candle.
- A stop gapped through fills at the worse available open, never the skipped level.
- Exits pay another adverse tick.
- Sizing is integer micros, includes stop slippage and commission, and is capped at 40.
- Pass timing counts every cached session, including sessions without a trade.
- Only full rolling windows are included in a 12- or 30-session pass-rate denominator.

The focused invariant suite is in `test_lucid_causal_rebuild.py`.

## Original basket reproduction

Independent reruns reproduce the invalidation:

- Original: 7,839 trades, PF 2.54, +$650,808, 98% pass, median 12 days.
- Causal VWAP: PF 1.02, +$13,769, -$86,648 max drawdown, 42% pass.
- Turtle: 581/906 entries have sweep and recovery in the same minute; those carry
  +$98,264. The later-minute subset is -$4,318.
- NR7: 233/378 entries opened beyond the threshold. Gap-aware P&L is -$4,175.

## Search design

`lucid_causal_rebuild.py` tested 102 configurations per market across ES, NQ, and CL:
causal VWAP rejection/crossback/momentum, opening-range breakout/fade, confirmed
Turtle, gap-safe NR7, confirmed 80/20 reversal, trend pullback, and prior-range
momentum.

Splits:

- training: start through 2021
- validation: 2022–2023
- chronological test: 2024–2026-07-30

The repaired three-year seed is merged over the older ten-year aggregate. ES/CL/NQ
session logic uses New York time, not a fixed UTC window.

## Best fixed finalist

NQ prior-range momentum:

- 15-minute completed close beyond session open ± 0.25 × prior-session range
- enter next one-minute open
- signal-bar stop
- 2R target

At $300 planned risk:

| Split | Trades | PF | Net | Average/trade | Max DD |
|---|---:|---:|---:|---:|---:|
| Train | 1,190 | 1.13 | +$22,549 | +$18.9 | -$8,477 |
| Validation | 433 | 1.20 | +$11,921 | +$27.5 | -$2,841 |
| Test | 514 | 1.22 | +$14,599 | +$28.4 | -$3,393 |

This is a possible research lead, not a Lucid-ready system. It lost in 2017 and 2020
and was approximately flat in 2023.

Chronological-test rolling outcomes:

| Planned risk | Pass in 12 sessions | Fail in 12 | Pass in 30 | Fail in 30 | Conditional median pass |
|---:|---:|---:|---:|---:|---:|
| $300 | 0.0% | 1.3% | 7.4% | 10.5% | 25 days |
| $400 | 4.1% | 6.7% | 21.8% | 38.5% | 19 days |
| $500 | 11.0% | 18.6% | 32.6% | 38.5% | 15 days |
| $600 | 18.4% | 29.4% | 38.5% | 51.7% | 13 days |

The apparent speed at $600 is selection among winners: more windows fail than pass.

## Expanded evidence-guided search

The follow-up research did not merely tune the original basket. It implemented and
tested additional model families in `lucid_predictive_research.py` and
`lucid_ml_walkforward.py`:

- the published rest-of-day to last-half-hour futures momentum effect;
- timely, clock-limited opening-range breakouts with optional clock-volume confirmation;
- NQ prior-range momentum with completed ES confirmation;
- fixed-time opening drive, gap continuation, gap fill, and prior-day continuation;
- 576 frozen-year logistic models using only information available at each fixed entry
  time and training only on the prior three calendar years.

All orders still use a completed signal bar and the following one-minute open. The
published close-momentum effect, timely ORB variants, CL morning models, and fixed-time
machine-learning models failed the chronological stability gate after costs.

Two genuinely causal morning signals did survive:

| Development-selected signal | Train PF | Validation PF | Chronological-test PF |
|---|---:|---:|---:|
| NQ 15-minute opening drive | 1.45 | 1.53 | 1.12 |
| ES 15-minute gap fill | 1.26 | 1.42 | 1.66 |
| NQ prior-range momentum | 1.13 | 1.20 | 1.22 |

The first two are sparse; increasing risk makes the account reach the target faster
only by increasing the breach rate. Combining them with the more frequent NQ prior-range
signal did not create a reliable evaluation strategy.

Research references:

- Baltussen, Da, Lammers, and Martens, *Hedging demand and market intraday momentum*,
  Journal of Financial Economics 142 (2021):
  https://www3.nd.edu/~zda/intramom.pdf
- Chen and Tsai, *An Effective and Automatic Approach for Timely Opening Range
  Breakout*, IEEE Access (2019):
  https://doi.org/10.1109/access.2019.2899177
- A recent fixed-time MNQ walk-forward comparison likewise found no statistically
  significant out-of-sample predictive edge:
  https://arxiv.org/abs/2605.17724

## Portfolio-level Lucid simulation

`lucid_portfolio_policy.py` adds the constraints that a per-trade simulation misses:

- concurrent positions share one aggregate 40-micro cap;
- exits release cap before unrelated entries at the same timestamp;
- entry-minute stops/targets are realized after entry, not left as phantom positions;
- sizing reserves MLL room for every open position's all-in stop risk;
- the $1,200 soft DLL blocks further entries for that session;
- the EOD MLL trails and locks according to the current 50K rules;
- all no-trade sessions remain in the denominator.

Thirty-six predeclared risk policies were ranked on training plus validation only.
The best development-selected policy risks up to $800 on either sparse morning signal
and $200 on the NQ frequency sleeve. Its rolling-window result is:

| Split | Pass by 12 sessions | Fail by 12 | Pass by 30 | Fail by 30 | Median among 30-day passes |
|---|---:|---:|---:|---:|---:|
| Train | 16.2% | 0.0% | 35.5% | 0.0% | 13.0 |
| Validation | 24.9% | 0.0% | 45.3% | 0.0% | 12.5 |
| Chronological test | 24.3% | 1.6% | 38.3% | 3.1% | 10.0 |

The low breach rate is not a hidden success: the MLL reserve correctly reduces or
stops new sizing near the floor, leaving most windows undecided. In the chronological
test, 75.7% do not pass within 12 sessions and 61.7% do not pass within 30 sessions.
Reporting only the 10-day median among successful windows would recreate the original
conditional-median error.

## Walk-forward and stress results

Parameters selected from each prior three-year window and traded in the next year
produced PF 1.21 at $300, but the evaluation result remained unusable:

| Planned risk | Pass in 12 | Fail in 12 | Pass in 30 | Fail in 30 | Conditional median pass |
|---:|---:|---:|---:|---:|---:|
| $300 | 0.0% | 1.0% | 4.3% | 9.5% | 26 days |
| $400 | 1.7% | 5.3% | 17.5% | 24.2% | 21.5 days |
| $500 | 4.7% | 11.9% | 29.2% | 33.6% | 19 days |
| $600 | 10.7% | 22.9% | 34.8% | 45.6% | 16 days |

One extra adverse tick at both entry and exit reduced the fixed finalist's training PF
from 1.13 to 1.07 at $300. It did not create a hidden pass edge.

## Paper/live parity

No candidate passed the deployment gate, so none was integrated or enabled. This avoids
creating another paper chart whose attractive result is not supported by the research.

For a future candidate, deterministic paper replay can share this engine and match its
signals and simulated fill rules. It still cannot be guaranteed identical to real Lucid
execution: the ten-year history and live bridge use Dukascopy CFD/index proxies, not CME
futures order-book data, and real slippage is not deterministic.

## Final decision

No tested strategy passes a LucidPro 50K evaluation in an average two weeks with a
defensible success rate. No new strategy has been integrated into either paper bot.
The old invalidated bots remain disabled. The causal morning signals are retained as
research leads only; presenting them as an evaluation-ready strategy would be
statistically false.
