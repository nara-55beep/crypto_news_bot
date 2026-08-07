# trend_sweep_bot

A modular Binance USD-M Futures trading bot. Trade **with the 4-hour trend**, only after price
**sweeps the previous day's opposite extreme**, entering on a **5-minute consolidation breakout**,
targeting the **daily VWAP** (with an optional 2R runner). Backtesting + live/testnet trading,
CSV trade logs, full performance metrics.

## Folder structure
```
trend_sweep_bot/
├── __init__.py        package metadata + module map
├── config.py          typed Config (defaults) + config.yaml loader
├── config.yaml        editable configuration
├── requirements.txt   ccxt, PyYAML
├── indicators.py      ATR, fractal swings, 4H trend, prev-day H/L, daily VWAP, consolidation
├── strategy.py        sweep → consolidation → breakout state machine (no look-ahead)
├── risk.py            1%-risk sizing + max-trades/day + consecutive-loss circuit breaker
├── data_feed.py       CCXT OHLCV (paginated history; closed-candle "recent" for live)
├── trade_logger.py    CSV trade ledger + structured logging
├── metrics.py         net, win rate, profit factor, Sharpe, max DD, avg, monthly, equity
├── backtest.py        event-driven backtester (closed candles, slippage + fees)
├── live.py            CCXT Binance USDM live/testnet executor (exchange-side brackets)
├── run_backtest.py    CLI: fetch history + backtest + report
└── run_live.py        CLI: run live/testnet
```

## Module-by-module
- **config.py** — every tunable in one typed place; `Config.load()` overlays `config.yaml` and reads
  API keys from the environment (never hard-code secrets).
- **indicators.py** — pure, stateless functions. `classify_trend` returns `bull`/`bear`/`None` from
  4H fractal swings + an EMA filter; `prev_day_high_low` and `daily_vwap` use UTC day boundaries;
  `consolidation` validates small bodies + tight range.
- **strategy.py** — the only stateful trading logic. Detects the sweep, ages it out after
  `setup_valid_bars`, measures the consolidation on the bars **before** the current one, and emits a
  signal when the current bar **closes** through the range. Returns entry/stop/tp1/tp2.
- **risk.py** — `position_size` makes every trade risk the same % at its stop; `RiskManager` enforces
  max trades/day and halts after N consecutive losses (both reset at the UTC day boundary).
- **data_feed.py** — `history` paginates months of candles; `recent` returns one page and drops the
  still-forming last bar so the strategy only ever sees closed candles.
- **backtest.py** — rebuilds per-bar context from past data only, manages the position bar-by-bar
  (stop assumed first on a straddle bar), applies slippage + taker fees on every fill.
- **metrics.py** — all required statistics from the trades + equity curve.
- **live.py** — mirrors the backtest logic against the live exchange and places **reduce-only**
  STOP_MARKET / TAKE_PROFIT_MARKET brackets so your risk is held by the exchange itself.

## Install
```bash
pip install -r trend_sweep_bot/requirements.txt
```

## Backtest
```bash
python -m trend_sweep_bot.run_backtest --symbol BTC/USDT --days 120
```
Outputs a console report and writes `output/trades_BTCUSDT.csv` + `output/equity_BTCUSDT.csv`.

## Live / testnet
```bash
export BINANCE_API_KEY=...        # Binance Futures *testnet* keys to start
export BINANCE_API_SECRET=...
python -m trend_sweep_bot.run_live --symbol BTC/USDT
```
`testnet: true` (default) routes to the Binance Futures testnet. Switch to real money by setting
`testnet: false` in `config.yaml` — only after testnet proves out. Use API keys scoped to
**trade-only, no withdrawal**.

## Honest note on edge
This strategy is faithfully implemented, but in backtests its VWAP targets are frequently small
relative to fees + slippage, so its measured edge is weak-to-negative depending on the sample and
instrument. Treat the backtest output as the truth, forward-test on testnet, and consider the
improvement levers (`min_rr`, `require_vwap_tp`, a minimum absolute target distance) before risking
real capital.
