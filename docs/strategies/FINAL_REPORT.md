# Final report

## What was added

A Strategy Library at **`/strategies`**, linked from the Paper Trading page by an
**“All Strategies →”** button next to the existing “Lucid Strategy Lab →” link.

## Catalog

| Measure | Count |
| --- | ---: |
| Total strategy definitions | **1,056** |
| Canonical families | 498 |
| Variations | 558 |
| Executable now | **614** |
| Requires additional data | 379 |
| Research-only | 18 |
| Unsupported | 45 |
| Rule engines backing the executable set | 47 |
| Year span | 1750 → 2019 |

### By category

| Category | Count |
| --- | ---: |
| Academic anomaly | 326 |
| Trend following | 215 |
| Mean reversion | 152 |
| Breakout | 50 |
| Price action | 50 |
| Momentum | 46 |
| Volume | 36 |
| Options | 26 |
| Machine learning and AI | 26 |
| Calendar and seasonal | 19 |
| Risk and exit methods | 18 |
| Gap | 16 |
| Fundamental | 14 |
| Multi-signal | 12 |
| Institutional execution | 11 |
| Volatility | 9 |
| Portfolio and allocation | 8 |
| Arbitrage | 8 |
| Event driven | 5 |
| Sentiment and alternative data | 5 |
| Statistical arbitrage | 3 |
| Long-term investment | 1 |

### Oldest concepts found

- **Engulfing, 20-bar trend context** (1750) — Price action
- **Engulfing, 20-bar trend context (long-short)** (1750) — Price action
- **Engulfing, 50-bar trend context** (1750) — Price action
- **Engulfing, 50-bar trend context (long-short)** (1750) — Price action
- **Hammer, 20-bar trend context** (1750) — Price action

### Newest concepts found

- **Quality minus junk** (2019) — Fundamental
- **Linear regression forecast** (2018) — Machine learning and AI
- **Logistic direction classifier** (2018) — Machine learning and AI
- **Decision tree** (2018) — Machine learning and AI
- **Random forest** (2018) — Machine learning and AI

## What cannot run, and exactly why

| Category | Not runnable |
| --- | ---: |
| Academic anomaly | 326 |
| Options | 26 |
| Machine learning and AI | 26 |
| Risk and exit methods | 18 |
| Fundamental | 14 |
| Institutional execution | 11 |
| Arbitrage | 8 |
| Event driven | 5 |
| Sentiment and alternative data | 5 |
| Statistical arbitrage | 3 |

The reasons, in order of how many entries they block:

1. **No cross-section.** An anomaly ranks a stock against every other stock each
   month. This project loads one symbol at a time, so even a price-only anomaly
   cannot be computed. Blocks the 326 academic entries.
2. **No fundamentals.** No point-in-time financial statements, so value, quality,
   profitability, accrual and F-score style screens cannot be evaluated without
   look-ahead from restated figures. Blocks the 14 fundamental screens.
3. **No options chain.** No strikes, expiries, greeks, assignment model or multi-leg
   orders. Blocks all 26 option structures.
4. **No tick data or venue model.** Blocks the 11 execution algorithms and the 8
   arbitrage entries; both are also latency-sensitive, so a daily-bar simulation
   would produce a profitable-looking result nobody could capture.
5. **No external datasets.** News, social, search, short interest, insider filings
   and analyst estimates are absent. Blocks the event-driven and sentiment entries.
6. **No fitted models shipped.** The 26 ML classes are catalogued as model classes,
   not as pre-fitted strategies, because shipping a fitted model without its full
   training provenance would be a leakage claim that cannot be supported.

Nothing was marked executable that cannot actually run: the test suite asserts that
options, execution and arbitrage entries are never `executable` and always carry a
reason.

## Honest limits of the results

- Results come from **one symbol at a time**, so they carry single-symbol selection
  risk. There is no universe backtest.
- Prices are **split- and dividend-adjusted**, which makes long windows comparable
  but differs from the prices that traded on the day. This is stated on every run.
- The catalog is a **research tool, not a recommendation**. No strategy in it is
  claimed to be profitable; the leaderboard reports what happened on the data you
  chose, with costs, and flags samples too small to mean anything.
- 322 of the 614 executable strategies produced **fewer than 30 trades** on a
  10-year SPY window and are therefore explicitly not rankable on that data.
