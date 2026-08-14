# Lucid Strategy Lab final report

## Delivered

- A dedicated responsive `/lucid-lab` page linked from the main and paper navigation.
- Central typed, source-linked rule configuration by public program, stage and size.
- Decimal account-path math, EOD/intraday trailing floors, consistency, DLL, contract caps and a position-size calculator.
- Exact causal three-sleeve strategy instructions and a generated daily operating plan.
- Immutable historical evidence, chronological rolling evaluations, explicit unfinished outcomes, stresses, block bootstrap, Monte Carlo and candidate/parameter comparisons.
- Read-only snapshot, sizing, cancellable replay and temporary upload-validation APIs.
- CSV/Parquet data quality checks and documentation for sources, assumptions, conflicts, candidates and methodology.
- 53 focused Lucid tests inside a 577-test passing repository suite, plus 21 strategy-harness tests against the historical cache.

## Quantitative verdict

The authoritative result is immutable run `db5b31c94d3cd7b7`, labeled **EXPERIMENTAL — PROXY EVIDENCE**. On 588 chronological 45-session LucidPro 25K starts, 47.28% passed, 0.00% breached in the observed sample and 52.72% remained unfinished. The Wilson interval is 43.27%–51.32%; the overlap-aware block-bootstrap range is 32.48%–61.73%. Severe execution reduced the point estimate to 37.07% passing. The artifact SHA-256 is `46cbbd0f01ce90104f8ab725c1aa39e0f0205b91055b6de1eef2bd295bc6a7e8`.

The strategy is not labeled validated because its data are Dukascopy CFD/index proxies rather than CME execution data, the final interval has been inspected during prior research, and the independent Claude review could not be obtained because the sandbox rejected Anthropic connections with `ECONNREFUSED`. Zero historical breaches does not mean zero future breach probability.

## Verification

- Focused tests: `python -B -m unittest tests.test_lucid_lab` — 53 passed.
- Full project tests in the existing project virtual environment — 577 passed.
- Existing causal/portfolio/strategy research harnesses — 21 passed against the original ignored historical cache.
- Python compilation completed for all new modules and tests.
- Direct page and API route tests cover load, refresh, sizing, invalid inputs, simulations, cancellation and upload validation.
- Desktop 1440 px and narrow responsive browser renders were inspected. A discovered metadata-overflow defect was fixed; wide tables now scroll only inside their containers.
- Existing Lucid paper bot files and existing live/paper trading gates were not modified.
- Claude research and final-review calls were attempted multiple times but returned no model output; debug logs confirmed refused network connections. This failure is preserved in the review records rather than called approval.

## Safety boundary

No part of the Lab can place an order. It cannot promise a profit or pass. Uploaded files are size-limited, validated in a temporary directory and not incorporated into the committed artifact. Raw market caches, credentials, runtime state and screenshots remain uncommitted.
