# Reference Ladder improvement report

Exact one-minute research completed on 2026-08-20 with the repository's causal,
adverse-first Reference Ladder engine.

## Data

- Source: official Binance BTCUSDT spot monthly/daily one-minute archives, extended
  with the public REST endpoint.
- Coverage: 2018-01-01 00:00 UTC through 2026-08-19 23:14 UTC.
- Bars: 4,532,210.
- Validation: no duplicate or invalid OHLC bars; 31 discontinuities totaling 8,065
  missing minutes were recorded rather than filled.

## Algorithm change

The original one-minute Bollinger/RSI signal, fixed dollar ladder, growing BTC
sizes, and 100x leverage were rejected after losing the entire test account. The
replacement uses:

- a signal only after a completed four-hour bar first enters an oversold state
  (lower 20/2 Bollinger touch and RSI-14 below 30);
- the prior completed UTC day's close above a rising 200-day EMA as a causal
  long-only regime filter;
- a first real entry 2% below the reference and three more entries at 1% spacing;
- equity-relative notional of 20%, 15%, 10%, and 5% at the four levels;
- 5x leverage, an 8% maximum account loss per cycle, and a 14-day duration cap;
- a full-cycle exit at the reference price; and
- the original conservative $10 round-turn spread, 0.04% commission per side,
  size-dependent slippage, maintenance margin, liquidation, and financing model.

## Exact full-history result

| Metric | Result |
| --- | ---: |
| Final balance | $134,044.48 |
| Total return | +34.0445% |
| CAGR | +3.4525% |
| Profit factor | 1.8740 |
| Win rate | 89.5349% |
| Sharpe | 0.4995 |
| Max equity drawdown | 16.1999% ($17,217.78) |
| Entered cycles | 86 |
| Liquidations | 0 |
| Execution costs | $4,459.88 |
| Funding costs | $2,946.61 |

## Chronological segment analysis

Each segment starts with a fresh $100,000 account while retaining indicator warmup
from earlier data. The recent segment was inspected during final exposure selection,
so it is additional temporal validation rather than a pristine holdout.

| Segment | Return | Profit factor | Max equity DD | Cycles | Liquidations |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2018-2022 development | +13.3836% | 1.6567 | 16.1999% | 44 | 0 |
| 2023-2024 validation | +7.3067% | 1.4763 | 9.9055% | 29 | 0 |
| 2025-present recent validation | +9.6224% | 7.5752 | 7.1573% | 13 | 0 |

The recent segment has only 13 entered cycles, so its very high profit factor is a
small-sample result and must not be treated as a stable estimate.

## Stress periods

| Period | Return | Entered cycles | Liquidations | Observation |
| --- | ---: | ---: | ---: | --- |
| March 2020 crash | -8.1332% | 1 | 0 | Cycle loss cap contained the failure |
| May 2021 crash | +5.3833% | 6 | 0 | All entered cycles recovered |
| May-July 2022 (LUNA/3AC) | 0.0000% | 0 | 0 | Daily trend regime stayed flat |
| November 2022 (FTX) | 0.0000% | 0 | 0 | Daily trend regime stayed flat |
| August 2024 carry unwind | -7.6880% | 2 | 0 | One cycle hit the bounded loss cap |

## Robustness and failed trials

- Doubling spread, commission, slippage, and funding still returned +23.1988%
  with profit factor 1.5682 and no liquidation.
- Equity-notional fractions of 20%, 25%, 30%, and 35% all remained profitable;
  20% was selected for lower exposure and better holdout behavior.
- An RSI threshold of 32 remained profitable at +10.7694%.
- An RSI threshold of 28 lost 12.7256%. This sensitivity is retained as a warning,
  not removed from the record.
- Merely repairing the old signal's position sizing was insufficient: the safer
  fixed-distance version still lost 5.2354% after costs.

The backtest is profitable across the full history and all three chronological
segments, but it does not guarantee future profit. The modest CAGR, 16.2% historical
drawdown, small recent sample, and parameter sensitivity require continued paper
validation before any live deployment.
