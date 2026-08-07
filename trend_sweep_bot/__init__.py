"""
trend_sweep_bot — a modular Binance-Futures trend-sweep / consolidation-breakout bot.

Strategy in one line: trade WITH the 4H trend, only after price sweeps the previous day's
opposite extreme, entering on a 5-minute consolidation breakout, targeting the daily VWAP.

Modules:
  config         — typed configuration (defaults + optional config.yaml)
  indicators     — pure, stateless TA: ATR, swings, 4H trend, prev-day H/L, daily VWAP, consolidation
  strategy       — the sweep -> consolidation -> breakout state machine (no look-ahead)
  risk           — 1%-risk position sizing + daily-trade / consecutive-loss circuit breakers
  data_feed      — CCXT OHLCV fetching (paginated, closed-candle only)
  trade_logger   — CSV + structured logging of every entry/exit/SL/TP/PnL
  metrics        — net profit, win rate, profit factor, Sharpe, max DD, avg trade, monthly, equity
  backtest       — event-driven backtester (closed candles, realistic slippage)
  live           — CCXT Binance USDM live executor
"""
__version__ = "1.0.0"
