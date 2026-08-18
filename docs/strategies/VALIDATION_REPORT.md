# Validation report

Every command below was actually run on 2026-08-18. Results are copied from the
real output, including the failures and what fixed them.

## Commands

| # | Command | Result |
| --- | --- | --- |
| 1 | `venv\Scripts\python.exe -m compileall -q strategy_lab tests\test_strategy_lab.py` | pass |
| 2 | `venv\Scripts\python.exe -m pytest tests\test_strategy_lab.py -q` | **79 passed** |
| 3 | `venv\Scripts\python.exe -m pytest tests\ -q` | **759 passed, 256 subtests** |
| 4 | `node --check` on the page's inline script | pass |
| 5 | AST check that `dashboard.py` imports and mounts the routes and links the page | pass (4/4) |
| 6 | Headless Chrome render at 1500px, 820px and 390px | pass |
| 7 | Headless Chrome DOM dump after driving select → run → run-all | **0 runtime JS errors** |
| 8 | Full-catalog batch over 614 executable strategies | 614 ok, 0 failed, 9.2 s |
| 9 | Live backtest against real yfinance data (SPY 2015–2025) | pass |

There is no lint, typecheck, formatter or production-build step configured in this
repository, and no `.github/workflows`. Those rows are therefore **not applicable**
rather than passing — I did not invent them.

## Scale and performance

| Measure | Result |
| --- | --- |
| Catalog size | 1,056 strategies |
| Catalog build + validation | ~0.35 s, once, cached for the process |
| Full batch, 614 executable strategies | **9.2 s** (~15 ms each) |
| Cached rerun of the same batch | **0.02 s**, 614/614 cache hits |
| Browse endpoint, any filter | < 30 ms |
| DOM nodes in the 1,056-row list | ~25 (virtualised window), not 1,056 |

## Failures found and fixed during the build

| Found by | Failure | Fix |
| --- | --- | --- |
| Schema validator | `reversion.williams_r...` id contained an underscore | explicit clean slug per oscillator family |
| Schema validator | generated descriptions under six words | lengthened every templated description |
| Schema validator | `academic.RandD.orgcap` — `&`/mixed case in the category slug | added `_slugify` |
| Schema validator | price-only academic signals declared no missing data | they still need the cross-section; requirement added |
| `git add` | `strategy_lab/data/` silently gitignored | renamed to `catalog_data/` rather than weakening the credential-protecting ignore rule |
| Own test run | two engine tests used 5–6 bar fixtures, below the 30-bar minimum | padded the fixtures to 40 bars, keeping the specific bar under test |
| Visual inspection | default sort buried all 614 runnable entries under 326 academic ones | added a "runnable first" sort and made it the default |

## Correctness checks that specifically guard against misleading results

- **Look-ahead**: indicators and rules are re-run on a truncated frame and must
  produce byte-identical past values. Covered for the moving averages and for five
  representative rule engines.
- **Fill timing**: a signal on bar 10 is asserted to fill at bar 11's open, at bar
  11's open *price*.
- **Adverse intrabar resolution**: a bar touching both stop and target exits at the
  stop; a gap through a stop exits at the open, asserted at 80.0 rather than the
  95.0 stop.
- **Costs**: a zero-cost run and a costly run are asserted to differ, and the costly
  one to be worse.
- **Sample size**: a 1-trade run is asserted `sample_sufficient == False`; a 60-bar
  run is asserted to suppress the annualised figure.
- **Benchmark**: excess return is asserted to equal strategy minus buy-and-hold.
- **Isolation**: a strategy handed a malformed frame returns an error row and the
  batch still completes.

## Live-data sanity check

`SPY`, 2015-01-02 → 2024-12-31, 2,516 bars, golden cross (SMA 50/200, long only):

```
net             +124.60%      benchmark (buy & hold)  +238.01%
CAGR              +8.43%      excess                 -113.41%
max drawdown     -32.29%      trades                        5
Sharpe             0.670      costs                   $573.61
sample_sufficient  False  ->  "Only 5 trades. Below 30 the statistics are noise."
```

The best-known strategy in the catalog underperformed buy-and-hold by 113 points
over the decade and is flagged as an unusable sample. That is the correct output,
and it is the reason the leaderboard sorts insufficient samples last.
