# Strategy candidates

The repository research tested more than 100 parameterized configurations per ES/NQ/CL market plus later predictive families. Candidate selection uses development and validation periods; the chronological 2024+ period is reported afterward.

Families researched include:

1. VWAP rejection, cross-back, momentum and mean reversion.
2. Opening-range breakout and failed opening-range breakout.
3. Fixed-time opening drive and opening gap continuation/fill.
4. Prior-day high/low and prior-range breakout.
5. Confirmed Turtle Soup.
6. NR7 volatility expansion.
7. 80/20 reversal.
8. Trend/EMA pullback.
9. Intraday/session drift and slot seasonality.
10. GMM regime, cross-market, meta-filter and other ML candidates.
11. A three-sleeve portfolio combining NQ opening drive, ES gap fill and lower-risk NQ prior-range breakout.

## Rejection principles

- The original five-strategy basket is rejected because its 98% result was created by causal and fill defects.
- ML/regime candidates that fail chronological stability after costs are rejected.
- A family with a narrow profitable parameter spike is rejected even if its best cell is positive.
- High planned risk that raises pass rate by raising breach rate is not preferred.
- Conditional “median days among successful windows” is never presented as the expected duration for all starts.
- No martingale, averaging recovery, grid, news gamble, cross-account hedge, HFT or simulated-fill exploit is eligible.

The current leaderboard and reasons are generated into the validation artifact and summarized in `SELECTED_STRATEGY.md` after the historical run.
