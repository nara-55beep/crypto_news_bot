# Crypto Trading Bot — rule-based research tool (BTC / ETH / SOL)

A clean, **fully backtestable** crypto trading bot. It is **not** an "AI predicts the
market" toy. It uses transparent technical-analysis rules (trend filter + momentum +
volatility + volume + breakout/pullback entry), strict risk management, and runs in
three modes: **backtest → paper → live**.

> ⚠️ **Read this first.** This is a research tool. It does **not** predict the market and
> is **not** guaranteed to be profitable. Past/backtest results do **not** predict future
> results. Live trading is **OFF by default**. Do not risk money you can't afford to lose.
> Test extensively in backtest and paper first.

---

## What it does

- **Markets:** BTC/USDT, ETH/USDT, SOL/USDT (major assets only). Binance first; the
  exchange layer is abstracted so OKX/Bybit can be added later.
- **Strategy (rule-based, configurable):**
  1. **Higher-timeframe trend filter** (1H by default): EMA50/EMA200 — long only in
     bullish trend, short only in bearish trend.
  2. **Momentum:** RSI band + MACD histogram (or ROC).
  3. **Volatility filter:** ATR/price must be inside a sane band.
  4. **Entry:** `pullback` (near EMA50/VWAP) **or** `breakout` (prior N-bar high/low) —
     configurable.
  5. **Volume confirmation:** volume above its rolling average.
  6. **Funding filter (optional):** skip longs when funding is very positive, etc.
- **Risk management:** fixed % risk per trade, ATR stop-loss, R/R take-profit, optional
  trailing stop, max open positions, max leverage, **daily loss limit (pauses the day)**,
  **cooldown after a loss**, and hard rules: **no martingale, no averaging down**.
- **Backtester:** fees + slippage, and reports total return, win rate, profit factor, max
  drawdown, Sharpe, Sortino, # trades, average/best/worst trade, max consecutive losses.
  Results saved to `results/`.
- **Dashboard:** status, mode, symbols, latest signal, open positions, balance, daily &
  total P&L, trade history, editable settings, Start/Stop and **Emergency Stop**.

---

## Project structure

```
crypto-trading-bot/
  backend/
    main.py            FastAPI app (serves API + dashboard)
    cli.py             command-line backtests
    config.py          ALL parameters (env-overridable, many editable live)
    exchange/          base.py + binance.py (ccxt), make_exchange() factory
    data/              fetcher.py (OHLCV -> pandas, disk cache)
    strategy/          indicators.py + signal.py (the rules)
    risk/              manager.py (sizing, stops, guardrails)
    backtest/          engine.py (backtest + metrics)
    paper/             engine.py (live data, simulated fills, threaded loop)
    live/              engine.py (real orders — OFF + armed-gated)
    database/          db.py (SQLite: trades/signals/equity/logs)
    api/               controller.py + routes.py
    utils/             logger.py
  frontend/            index.html + app.js + styles.css (vanilla dashboard)
  requirements.txt
  .env.example
  README.md
```

---

## Setup (run locally first)

```bash
cd crypto-trading-bot
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # (Windows: copy .env.example .env) then edit if you like
```

### 1) Backtest (no server needed)
```bash
python -m backend.cli backtest BTC/USDT ETH/USDT SOL/USDT --days 180
# results/ gets a JSON file per run
```

### 2) Run the dashboard (paper mode)
```bash
uvicorn backend.main:app --port 8100
# open http://127.0.0.1:8100
```
The bot boots **stopped** and in **paper** mode. Press **Start**. It pulls live Binance
data, evaluates the rules on each closed bar, and simulates fills. Nothing is real.

### 3) Live trading (advanced, optional, real money)
Live is gated behind **four** locks:
1. set `BOT_LIVE_TRADING=true` in `.env`,
2. set `BOT_BINANCE_API_KEY` and `BOT_BINANCE_API_SECRET` (from environment — never
   hardcoded),
3. in the dashboard, switch the mode selector to **Live**,
4. click **Arm live trading** and confirm.

Only then can a real order be placed. Use a sub-account, small size, and understand the
code first. Live mode is an **MVP** — before real size you should add exchange-side
stop/TP orders and full position reconciliation. **You trade at your own risk.**

---

## Configuration

Every parameter lives in `backend/config.py` and can be overridden by a `BOT_*` env var
(see `.env.example`). Many can also be edited **live** from the dashboard (they apply to
new trades). Examples: `entry_mode`, `ema_fast/slow`, RSI bands, `atr_stop_mult`, `rr`,
`risk_per_trade`, `max_daily_loss`, `cooldown_minutes`, `fee_rate`, `slippage`.

Defaults match the requested starter strategy: HTF=1H, entry=15m, pullback entry, RSI
45–65 (long) / 35–55 (short), volume>20MA, 1.5×ATR stop, 2R target, 0.5% risk/trade.

---

## API (for embedding in your website)

`GET /api/status`, `POST /api/start|stop|emergency_stop`, `POST /api/mode`,
`GET/POST /api/settings`, `GET /api/trades|signals|equity|logs`, `POST /api/backtest`,
`POST /api/live/arm|disarm`, and a `WS /api/ws` that streams status every 2s. CORS is open
so your existing site can call it or embed the dashboard in an `<iframe>`.

---

## Disclaimer

This software is for **education and research**. It is provided "as is", without warranty.
Nothing here is financial advice. Trading crypto (especially with leverage) can lose you
**all** your money. The authors are not responsible for any losses. **Test before risking
real funds.**
