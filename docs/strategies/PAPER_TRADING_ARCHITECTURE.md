# Paper trading architecture

One engine (`strategy_lab/engine.py`) runs every executable strategy. No strategy
has its own result generator, so a bug in the fill model shows up in all 614 at once
rather than hiding in one.

## The contract

```
bar i          strategy sees bars 0..i, emits target position in {-1, 0, +1}
bar i+1 open   the engine fills that target
```

A signal derived from bar `i`'s close therefore cannot be filled at that close. This
is checked by a test that asserts the fill index and price.

## Execution assumptions

| Item | Model | Classification |
| --- | --- | --- |
| Fill reference | next bar's open | conservative assumption |
| Spread | half of `spread_bps` crossed per side, 5 bps default | user-configurable |
| Slippage | `slippage_bps` on top, adverse, 2 bps default | user-configurable |
| Commission | `max(min_fee, per_share × shares)`, $0.005/share and $1 min | user-configurable |
| Short borrow | `short_borrow_bps_annual` accrued over the holding period | conservative assumption |
| Stop and target on one bar | the stop wins | conservative assumption |
| Gap through a stop | fills at the open, not the stop | conservative assumption |
| Splits and dividends | handled by the provider's adjusted series | market-derived |
| Position sizing | fixed fraction, volatility target, or fixed shares | user-configurable |

Nothing defaults to zero cost, midpoint fills, guaranteed limit fills, unlimited
liquidity, or a favourable ordering of a bar's high and low.

## Metrics

Net return, CAGR, max drawdown, Sharpe, Sortino, Calmar, win rate, profit factor,
expectancy, trade count, average holding period, commission, spread and slippage
cost, exposure, turnover, and a buy-and-hold benchmark for the same symbol and window
with the excess stated explicitly.

## Sample-size discipline

- Under **30 trades** the result is flagged and `sample_sufficient` is false.
- Under **126 bars** annualised figures are suppressed entirely rather than
  extrapolated from a short window.
- The leaderboard sorts insufficient-sample rows last and labels them, and offers
  explicit "sufficient" / "insufficient" filters.

## Batch runner

`strategy_lab/runner.py`. Chunks the executable set across a bounded thread pool so
the event loop stays responsive; a raising strategy is caught and recorded on its own
row, so one failure never stops the batch. Cancellation is checked between strategies
and inside long replays. Identical `(strategy, parameters, data fingerprint, config)`
work is served from cache, and the run id is a hash of exactly those four things.

## What this is not

There is no order routing. No broker credential is read, no order is ever placed, and
the page states this. Intraday, options, cross-sectional and tick-level strategies are
catalogued but not runnable, and each says exactly what it would need.
