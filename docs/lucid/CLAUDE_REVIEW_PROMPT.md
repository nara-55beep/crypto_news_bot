# Claude final review prompt

Review the complete Lucid Strategy Lab implementation in this repository as a skeptical independent auditor. Read `docs/lucid/`, `lucid_lab/`, `research/ta_strat/lucid_lab_validation.py`, the immutable JSON artifact, the dashboard route integration, and `tests/test_lucid_lab.py`.

Confirm or refute, with exact file/function references:

1. Official rules are program-, stage- and size-specific, source-linked, last-checked and fail closed when ambiguous.
2. Account math implements target, EOD/intraday trailing floors, DLL when applicable, consistency, commissions, contract caps and session flattening without crossing scopes.
3. Every signal is causal and every simulated fill is conservative; stop/target ambiguity, gaps, integer sizing and overlapping positions are handled.
4. Development, validation and test periods are chronological; the selected strategy was not tuned on the final test; no-trade and unfinished paths stay in denominators.
5. The reported intervals, bootstrap, stress tests, Monte Carlo and pass-time wording match the computation and do not exaggerate confidence.
6. The browser page cannot mutate live trading state, exposes errors/empty states, works on mobile, and never presents proxy research as guaranteed profitability.
7. Tests reach production-shaped interfaces and would fail if a critical safety property regressed.
8. No credential, private runtime data, giant raw file or unrelated bot change is included.

Return: executive verdict, confirmed material defects, non-material observations, required fixes, evidence wording that is permitted, and exact tests you would add. Do not edit unrelated bots, change existing trading gates or claim an edge that the evidence does not establish.
