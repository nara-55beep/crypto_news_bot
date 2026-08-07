# BTC TA Strategy Study — Honest Results (Jun 2026)

Fresh data: BTC perp from Binance USDM, **1m (604,803 bars), 5m (120,960), 15m (40,320)**
covering 2025-04-24 → 2026-06-18, plus **3.5y daily** (2022-11 → 2026-06).
Test year (last 365d): **BTC −39%** ($104,833 → $63,857), maxDD −51% — a **bear market**.

All backtests: no look-ahead (signal on close → fill next open), intrabar stop/liq
ordering, taker slippage on both sides, funding drag, isolated-margin liquidation,
$100 start, compounding. Code in this folder (`run_screen.py`, `run_trend*.py`).

---

## 1. Intraday day-trading (1m / 5m / 15m) — DOES NOT WORK after costs

13 strategy families (Donchian, Supertrend, EMA/MACD cross, ADX, ORB, session-momentum,
overnight, TSMOM, RSI2, Bollinger fade, engulfing) × 3 timeframes × cost grid {0,1,2,3,5} bps.

**Every single one is unprofitable at ≥1 bps/side. The cost cliff (textbook):**

| Strategy (15m/5m) | ROI @ 0 bps | ROI @ 1 bps | ROI @ 2 bps |
|---|---|---|---|
| Bollinger fade (5m) | **+455%** | −100% | −100% |
| RSI2 (5m) | +171% | −98% | −100% |
| TSMOM (15m) | +22% | −94% | −100% |
| Donchian20 (15m) | −99% | −100% | −100% |

The 0-bps "profits" are a mirage (a market order still pays the spread), and most are
**negative out-of-sample even at 0 cost** (overfit). Donchian — the best trend pedigree —
is the *worst* intraday because breakouts whipsaw on fast bars. **Verdict: no edge.**

## 2. The one real edge: daily long/short trend-following

Best honest config: **Donchian-20 breakout, long/short, 200-day MA filter, ADX(14)>25,
2.5×ATR stop, 4×ATR trailing exit.** Risk-based sizing → **~1× effective leverage**.

| Window | Trades | Win% | ROI | MaxDD | Liquidations | Profit ($100) |
|---|---|---|---|---|---|---|
| **Last 1 year (requested)** | **5** | **67%** | **+21%** | **−4%** | **0** | **+$21 → $121** |
| Full 3.5 years | 14 | 43% | +5% | −27% | 0 | +$5 → $105 |

Last-year mechanism: 3 shorts in the downtrend won (+2%, +18%, +12%); 2 longs lost. Profit
came from **catching big multi-day moves at ~1×**, not from leverage.

Honest caveats: +5%/3.5y ≈ breakeven (only +ve in 1 of 4 calendar years; 2024 chop lost
−14%). Low statistical confidence (14 trades, survived a small ADX-threshold search).
It is **regime-dependent**: it profits in *trending* years and bleeds in *choppy* ones.

## 3. Why 10–20× leverage is the wrong question

Same best strategy, different sizing:

| Sizing | Full ROI | Last-yr ROI | MaxDD | Liquidations | Final $ |
|---|---|---|---|---|---|
| risk 8% (~1× effective) | +5% | +21% | −27% | 0 | $105 |
| **all-in 10×** | **−100%** | wiped | −100% | 4 | **$0.00** |
| **all-in 20×** | **−100%** | wiped | −100% | 5 | **$0.01** |

A daily swing needs a wide (~6–8%) stop; at 20× a 5% wiggle = liquidation, so it blows up.
**10–20× is only compatible with tight-stop fast trading — which has no edge after costs.**
High leverage multiplies an edge; with ~zero edge it just multiplies the path to zero.

## 4. Second pass — walk-forward + true holdout (the strongest anti-overfit tests)

Chasing the "must be profitable" goal, I ran rolling walk-forward (train 120d → test 45d)
and true 60/40 holdout splits on the low-frequency structural strategies (the only ones with
theoretical merit), at the user's real near-zero cost.

- **ORB looked like a winner (+108% walk-forward OOS) — then I found a look-ahead bug** in my
  own volume filter (it summed the full anchor *hour*, but breakouts fire mid-hour). After
  fixing it to use only opening-range volume: walk-forward fell to +10%, and on a **true
  untouched holdout it was −22%** (train +60% → holdout −22% = textbook overfit). Dead.
- **Session-momentum (Shen et al., peer-reviewed) is the single best-behaved signal — and it's
  a pure zero-cost mirage:** full 14mo = **+35% ROI / Sharpe 1.61 @ 0 bps → −3% @ 1 bps →
  −31% @ 2 bps.** Positive in only 1 of 5 quarters. A genuine signal smaller than the spread.

This is the cost cliff, re-proven on fresh 2025–26 data with the most rigorous methodology:
intraday signals have real predictive content at **0 cost** but **no net edge at ≥1 bps**, so
they cannot be traded profitably with market orders — and 10–20× leverage only amplifies the
post-cost loss.

## Bottom line
- **Profitable in the requested 1-yr window:** daily L/S trend, +21%, 67% win, −4% DD — but at ~1× leverage and ~5 trades/yr, not intraday 10–20×.
- **Not certifiable as reliably profitable forward** (breakeven over 3.5y, regime-dependent).
- **Intraday 10–20× day-trading on BTC is a proven loser after costs.** Don't risk real money on it.
- Real structural edges live elsewhere: maker/zero-fee market-making on Lighter, copy-trading verified wallets, low-leverage trend.
