# Validation report

Generated 2026-08-14 from immutable run `9dd7658a37e8dcf6` with seed `20260814`.

## Evidence

The source is real Dukascopy one-minute index/CFD proxy history mapped to ES and NQ research sleeves. The chronological test interval contains 632 sessions from 2024-01-02 through 2026-07-30 and 847 raw signals. The selected, sized portfolio admitted 429 test trades. Raw files remain outside Git because of their size; names, hashes, byte counts, coverage and rejection rules are in the artifact and `DATA_SOURCES.md`.

This is not CME market-by-order data. It cannot establish actual queue position, fill probability, exchange latency or realized futures slippage. The test interval has also been inspected in earlier research, so it is confirmatory rather than pristine holdout evidence.

## Chronological results

For 588 eligible 45-session starts on LucidPro 25K under the normal conservative execution model:

| Outcome | Count | Rate |
|---|---:|---:|
| Passed within 45 sessions | 278 | 47.28% |
| Maximum-loss breach | 0 | 0.00% |
| Unfinished | 310 | 52.72% |

The descriptive Wilson 95% interval for pass rate is **43.27%–51.32%**. Because rolling starts overlap, the block-bootstrap range is wider: **32.48%–61.73%**. Median completion time is **12 sessions conditional on passing**; it is not the expected duration for every start. The restricted mean, assigning 45 sessions to non-passes, is **30.57 sessions**.

The development/validation/test 45-session pass rates are 44.90%, 57.75% and 47.28%. Those differences are visible rather than averaged away.

## Trade economics

| Metric | Test result |
|---|---:|
| Accepted trades | 429 |
| Net expectancy | $44.29/trade |
| Profit factor | 1.3814 |
| Win rate | 46.15% |
| Average win / loss | $347.60 / -$215.69 |
| Commission | $1,161.00 |
| Modeled spread | $1,092.75 |
| Modeled slippage | $1,092.75 |
| Costs / positive gross P&L | 14.97% |

Costs use the officially published $0.50 per side for the micro instruments plus one adverse spread tick and one adverse slippage tick per round trip in the normal preset. The severe preset further worsens execution and is not a claim about a precise live cost distribution.

## Stress and Monte Carlo

The severe combined execution scenario produced a 37.07% pass rate, 0.00% observed breach rate and 62.93% unfinished rate across the same 588 starts. Zero observed breaches is not proof of zero breach risk.

The supplementary 5,000-path, 5-session block-resampled simulation produced 47.54% passes, 0.04% breaches and 52.42% unfinished paths at 45 sessions. Median terminal profit was $139.64 and the fifth percentile was -$991.48. Resampling reflects variation present in this history; it cannot correct proxy-data or execution-model error.

## Decision

The gate returns **EXPERIMENTAL_PROXY**, not `VALIDATED`. Positive after-cost historical behavior and stress survival support continued paper research, while proxy execution, repeated inspection of the test interval, overlapping evaluation windows and rule-change risk prevent a live-edge claim. The web page must always display pass, breach and unfinished together and must retain this limitation language.

## Reproduction

From the repository root, with the historical cache available:

```powershell
python -B research\ta_strat\lucid_lab_validation.py `
  --cache-dir research\ta_strat\cache `
  --output research\ta_strat\results\lucid_lab_validation.json
python -B -m unittest discover -s tests -p "test_lucid_lab.py"
```

The generated artifact is `research/ta_strat/results/lucid_lab_validation.json`. Its run id is derived from research version, seed, input file hashes and core configuration.
