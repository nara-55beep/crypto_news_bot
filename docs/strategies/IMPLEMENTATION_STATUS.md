# Implementation status

| Status | Count | Meaning |
| --- | ---: | --- |
| executable | 614 | required data, indicators and order types are all present; runs now |
| requires-data | 379 | valid and precisely defined, but needs data this project does not carry |
| research-only | 18 | documented but not faithfully reducible to a deterministic single-symbol rule |
| unsupported | 45 | the platform lacks the instrument or execution model entirely |

## Rule engines

47 parameterised engines back the 614 executable entries. One engine serves many catalog rows, so a fix lands everywhere at once.

| Rule id |
| --- |
| `allocation.trend_filtered` |
| `benchmark.buy_and_hold` |
| `breakout.gap` |
| `breakout.inside_bar` |
| `breakout.narrow_range` |
| `breakout.price_channel` |
| `breakout.prior_period_high` |
| `breakout.squeeze` |
| `breakout.volatility` |
| `combo.confirmed_trend` |
| `momentum.rate_of_change` |
| `momentum.rsi_trend` |
| `momentum.time_series` |
| `momentum.volatility_scaled` |
| `price-action.candlestick` |
| `price-action.structure` |
| `reversion.bollinger` |
| `reversion.cci` |
| `reversion.consecutive_bars` |
| `reversion.ma_distance` |
| `reversion.mfi` |
| `reversion.rsi` |
| `reversion.short_term_reversal` |
| `reversion.stochastic` |
| `reversion.vwap_band` |
| `reversion.williams_r` |
| `reversion.zscore` |
| `seasonal.day_of_week` |
| `seasonal.month_window` |
| `seasonal.turn_of_month` |
| `trend.adx_filtered_ma` |
| `trend.donchian_breakout` |
| `trend.ichimoku` |
| `trend.linreg_slope` |
| `trend.ma_crossover` |
| `trend.ma_ribbon` |
| `trend.macd` |
| `trend.parabolic_sar` |
| `trend.price_vs_ma` |
| `trend.supertrend` |
| `trend.triple_ma` |
| `volatility.atr_expansion` |
| `volatility.regime_filter` |
| `volume.accumulation_trend` |
| `volume.chaikin_money_flow` |
| `volume.obv_trend` |
| `volume.relative_volume_breakout` |