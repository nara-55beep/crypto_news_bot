# Reference Ladder baseline report

Run on 2026-08-19 with the repository's causal Reference Ladder engine.

## Data

- Source: official Binance BTCUSDT spot monthly/daily one-minute archives, extended
  with the public REST endpoint.
- Coverage: 2018-01-01 00:00 UTC through 2026-08-19 19:35 UTC.
- Bars: 4,531,991.
- Validation: no duplicate or invalid OHLC bars; 31 discontinuities totaling 8,065
  missing minutes were recorded rather than silently filled.

## Baseline

Defaults: $100,000 starting capital, 100x margin setting, auto base size of 0.05 BTC
per $1,000 equity, level multipliers 1/2/4/8, first trigger $800 adverse to the
reference, then $500 between levels, $10 round-turn spread, 0.04% commission per
side, size-dependent slippage, and 0.01% financing per eight hours. No stop loss.

| Metric | Result |
| --- | ---: |
| Final balance | $0.00 |
| Total return | -100.00% |
| Entered cycles | 7 |
| Win rate | 85.71% |
| Profit factor | 0.7295 |
| Max equity drawdown | 100.00% ($380,700.62 peak-to-trough) |
| Liquidations | 1 |
| Worst cycle floating loss | -$369,466.57 |
| Worst cycle duration | 19.067 hours |

The high win rate is not evidence of safety. Six recovered cycles were unable to
offset one liquidation, and the strategy lost the entire account.

## Named stress periods

| Period | Recovered | Deepest floating loss | Liquidations | Return |
| --- | --- | ---: | ---: | ---: |
| March 2020 crash | No | -$99,148.63 | 1 | -99.9870% |
| May 2021 crash | No | -$103,074.13 | 8 | -100.0000% |
| May-July 2022 (LUNA/3AC) | No | -$75,921.92 | 6 | -99.9921% |
| November 2022 (FTX) | No | -$96,671.95 | 1 | -94.0750% |
| August 2024 carry unwind | No | -$117,813.96 | 4 | -98.7202% |

## Parameter and improvement conclusions

| Variant | Return | Max equity DD | Liquidations | Final balance |
| --- | ---: | ---: | ---: | ---: |
| Baseline growing sizes | -100.0000% | 100.0000% | 1 | $0.00 |
| Regime filter | -99.8669% | 99.9845% | 3 | $133.07 |
| ATR-scaled distances | -100.0000% | 100.0000% | 2 | $0.00 |
| Flat level sizing | -95.4079% | 99.2592% | 1 | $4,592.13 |
| Shrinking level sizing | -99.4834% | 99.7993% | 1 | $516.60 |

Every tested fixed trigger from $500 through $1,500 lost at least 99.96%. All twelve
trigger/step heatmap cells lost at least 99.01%. Changing starting capital from
$10,000 through $1,000,000 did not remove liquidation because auto sizing scales
with equity. The chronological 70/30 walk-forward selected the least-bad $500
training trigger, but its out-of-sample segment still lost 69.75% and liquidated.

These results reject the supplied defaults and the tested improvements as a viable
deployment configuration. They are retained as research outputs, not promoted to
live trading.
