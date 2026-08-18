# Data requirements

What this project has, and what each blocked family would need.

## Available

| Source | Coverage | Used for |
| --- | --- | --- |
| yfinance daily OHLCV | US listed equities and ETFs, split/dividend adjusted | every executable strategy |
| Alpaca IEX (`stock_data.py`) | live quotes, needs a key | the existing live pages, not the backtester |

## Not available, and what it blocks

| Missing data | Strategies blocked |
| --- | ---: |
| a survivorship-free US equity universe with monthly cross-sectional returns | 326 |
| company fundamentals (balance sheet, income statement, cash flow) | 195 |
| option chain snapshots with strikes, expiries, greeks and bid/ask | 26 |
| an options-aware fill and assignment simulator | 26 |
| a training/validation/test split fixed before any evaluation | 26 |
| point-in-time features with a stated availability lag | 26 |
| a documented retraining schedule and leakage audit | 26 |
| sell-side analyst estimates and recommendations | 21 |
| cross-sectional trading and microstructure data | 20 |
| point-in-time fundamentals with report dates (not restated figures) | 14 |
| a survivorship-free universe including delisted companies | 14 |
| additional external data | 12 |
| tick-level trades and quotes | 11 |
| venue-level liquidity data | 11 |
| equity options chains and implied volatility | 9 |
| institutional 13F holdings filings | 8 |
| corporate event dates and outcomes | 8 |
| co-located execution with realistic latency | 8 |
| a multi-symbol price panel | 3 |
| earnings announcement dates | 1 |
| consensus estimates | 1 |
| reported actuals | 1 |
| announced deal terms | 1 |
| deal completion outcomes | 1 |
| borrow availability | 1 |
| index reconstitution announcements and effective dates | 1 |
| Form 4 insider transaction filings | 1 |
| dividend declaration, ex and pay dates | 1 |
| withholding tax treatment | 1 |
| a survivorship-free universe | 1 |

## Why blocked strategies are not approximated

A price-only proxy for an accounting or options signal is a different strategy with the
same name. Those records stay `requires-data` and name the exact missing input rather
than shipping a lookalike whose results would be quietly meaningless.

The single largest block is the cross-section: an anomaly ranks a stock against every
other stock each month. This project loads one symbol at a time, so no cross-sectional
rank can be computed even when the anomaly only needs price.