# Strategy V3 — "The Goodman System" (from Glen Goodman, *The Crypto Trader*, 2019)

A faithful mechanisation of the discretionary trend-following method in the book.
Goodman is explicit that **the exact entry matters far less than sitting tight, cutting
losses, and position sizing** (ch.10), and that **there is no holy grail of indicator
numbers** (ch.10) — so this spec fixes the *discipline* and leaves the *parameters*
deliberately round and robust, not optimised.

> The four rules (ch.3, ch.5): **TRADE THE TREND · GROW YOUR PROFITS · CUT YOUR LOSSES · CONTROL THE VOICES.**

---

## 0. What the book actually argues (so we don't distort it)

- **Day trading is a loser's game** (ch.5): 95% lose; spread + fees + HFT eat any edge.
  Trade the **daily** chart; hold weeks–months. → *This is why our 15m V2 keeps losing;
  it's the cost cliff the book describes.*
- **Trends persist** (ch.5) — the one durable edge, backed by a century of research
  (Hurst/Ooi/Pedersen etc., cited in the book). Buy what's already going up.
- **Buy after the trend starts, sell after it bends** (ch.5, ch.10): skip the exact
  bottom ("only monkeys pick bottoms") and the exact top; grab the fat middle.
- **The sell is the hard part** (ch.10) and is where money is kept through crashes.
- **Risk, not conviction, sizes the trade** (ch.11). **Psychology is the missing element**
  (ch.13–14): the rules are easy; following them is the whole game.

This maps 1:1 onto your own research: [[trend-edge-research]] (Donchian + 200d MA on
majors is the robust edge) and [[scalping-cost-cliff]] (intraday dies after costs).

---

## 1. Timeframe & universe

- **Decision timeframe: DAILY candles.** (Weekly only for macro context.) No intraday.
- **Universe:** BTC, ETH, SOL (your majors). Liquid, tight spreads (ch.4). Goodman would
  cast wider, but majors sidestep the scam/illiquidity risk and the dud-coin checklist
  (ch.8) — keep a separate watchlist for any larger alt that *bases then breaks out*.

## 2. Master regime switch — Bitcoin is the bellwether (ch.6, ch.10)

"Where Bitcoin goes, the rest follows." Compute on BTC daily:
- **`btc_200dma`** and its slope.
- **Regime = BULL** if `BTC close > 200d MA` **and** `200d MA rising`.
- **Regime = BEAR** if `BTC close < 200d MA` **and** `200d MA falling`.
- else **NEUTRAL/CHOP**.

Rules:
- **Longs enabled only in BULL.** (Bull = "shooting fish in a barrel"; bear/chop = stand
  aside or be highly selective — ch.6.)
- **Shorts: default OFF.** Only consider in confirmed BEAR, small, as a hedge (see §7).

## 3. Per-asset trend filter

Long an asset only if **its own** `close > 200d MA` (asset is in its own uptrend), in
addition to the BTC regime being BULL.

## 4. Entry — breakout from a base, don't chase (ch.7)

Goodman buys breakouts out of multi-week **bases/triangles/flags** on **expanding volume**.
Mechanised as a **Donchian breakout** (this is literally your documented robust edge):

- **Trigger:** daily close **> highest high of prior `N=55` days** (`N` round, 20–60 fine).
- **Base confirmation:** the prior ~`N` days were a *contraction* (range/ATR compressed
  vs the preceding window) — i.e. it broke out of a base, not mid-parabola.
- **Volume confirmation:** breakout-day volume **> 1.5× the 20-day average** (ch.7: no
  volume = likely false breakout).
- **Don't chase (ch.7):** only enter within **5%** of the breakout level. If price has
  already run >5% past it, **skip** and wait for the next setup. *No FOMO entries.*
- Keep it simple — these few inputs only. No indicator soup (ch.7).

> Goodman's caveat (ch.10): even a perfect entry is secondary. If you must miss some, miss
> entries, not stops.

## 5. Initial stop & position size (ch.10–11) — the core of the system

- **Stop placement:** just below the broken base / breakout level, expressed as
  `stop = entry − k·ATR` with **`k = 2.5`** (round; book uses 1.75–5× in various places).
- **Risk per trade:** **0.5–1%** of *current equity* (use 0.75%).
- **Position size = risk-parity by ATR (ch.11):**
  `qty = (equity × risk%) / (entry − stop) = (equity × risk%) / (k · ATR)`
  → calmer (low-ATR) coins automatically get **more** size, wild coins **less**. Never
  equal-dollar weight (the ch.11 stress test: equal weight lost −61% in H1-2018 because
  the wildest coins fell hardest).
- **Leverage:** low or none; cap notional. Leverage only with tiny size (ch.4, ch.13).
- **Diversify** across the 3, each a small slice (ch.11, ch.14 — small slices make cutting
  losers psychologically easy).

## 6. Exit — "trend is your friend until it bends" (ch.10, the most important chapter)

Hold and **grow the profit**; never sell just because "it's risen enough." Exit on the
**first** of these (whichever bends the trend):

1. **Chandelier ATR trailing stop:** `stop = highest_high_since_entry − 3·ATR(daily)`.
   Ratchets up only. (Book: `highest − 5×ATR`; 3× is a touch tighter for 3-coin crypto.)
2. **Structure break:** daily **close** below the most recent significant horizontal
   support / the breakout level it last held (a *close*, not a wick — ch.10).
3. **Macro override (the top-call, ch.10):** if **BTC regime flips to BEAR** (BTC closes
   below its 200d MA / fails a new high then rolls over — his Christmas-Eve-2017 call),
   exit longs across the book regardless of individual stops.

**Profit management (ch.10–11):**
- After a **sharp parabolic run**, take partial profit (sell **⅓–½**) and let the rest run.
- Once meaningfully in profit, **move the stop to breakeven** so a winner can't become a
  loser.
- The trailing stop *will* give back some of the top — that's the accepted cost of
  catching the fat middle (his BTC trade: bought ~$330, trailed out ~$8,000 from a
  $20,000 top = still +2,300%).

## 7. Cut losses — non-negotiable, enforced in code (ch.3, ch.13–14)

- **Hard stop is sacred.** It can only move **in your favour** (toward breakeven), **never
  widened.** (This single rule prevents the £12k-in-a-day disaster: he doubled→quadrupled
  a loser and overruled his stop.)
- **No averaging down. No catching falling knives. No adding to losers.** Ever (Gameplay).
- **No revenge / no chasing losses:** after a stop-out, **no re-entry on that asset for
  K=5 days** unless a *fresh, valid breakout* occurs (not regret-buying — ch.10, ch.14).
- **Cooldown after a loss; daily/streak drawdown pause** (if equity drops a set % in a
  day/run, stop trading for the day — counters the "house money"/tilt spiral, ch.14).

## 8. Shorting policy (ch.12) — mostly don't

- Default **OFF.** Shorting is much harder: max gain capped at 100%, max loss infinite;
  position *shrinks* as it works; upward corrections in downtrends are vicious (breakout
  shorts get stopped out repeatedly — ch.12).
- If enabled: **only in confirmed BTC BEAR**, via an **MA crossover** sell signal (not
  breakouts), small size, as a portfolio hedge.

## 9. Fundamentals — only as a scam/dud filter (ch.8)

Primary decisions are technical ("everything is in the price" — ch.8). For any non-major
added to the watchlist, require: credible/active team (GitHub), known/capped token supply,
a real "why a blockchain?" use case, clean white paper — and **reject** Ponzi tells
(unrealistic returns, MLM/pyramid, closed trading, opacity). Optional confirmations:
**NVT/NVM** (overvaluation flags; NVM warned before 2014/2018), **social-sentiment**
(leads price, but fades as it's arbitraged away). Ignore news & hot tips (ch.8, ch.14).

## 10. Operations & mindset (ch.4, ch.15, ch.16)

- Watchlist + **price alerts** + resting **stop orders** so you act on daily closes
  without screen-staring (few trades, full life — ch.15).
- Liquid venues, tight spreads; spread capital across exchanges; keep little on each
  (counterparty risk — ch.4).
- **Compounding mindset:** target ~20–40%/yr, not 170%/mo fantasies (ch.3 — 20%/yr is
  Buffett-tier and compounds to wealth).

---

## 11. Honest expected behaviour (faithful to the book + our robustness work)

- **This is a "few big trends pay for many small losses" system.** Expect win rate ~35–45%
  with winners several × the size of losers (positive *expectancy* via asymmetry, not via
  win %).
- It **makes its money in 1–2 big bull legs per cycle** and **bleeds small in chop/bear by
  design** — exactly when the regime switch keeps it flat/aside. Goodman did the same
  (fortune in the 2017 bull and the 2008 short; mostly sidelined otherwise).
- Therefore a backtest over a **short, choppy 6-month window will look unimpressive** — and
  that is *not* failure, it's the strategy waiting for a real trend. Judge it over **full
  market cycles (2–4+ years)**, by *survival through crashes* and *capture of big trends*,
  not by month-to-month smoothness.
- Goodman's own framing: the exact MA/Donchian numbers won't change much; **discipline,
  sitting tight, cutting losses, and position sizing are the edge.**

## 12. Parameters (round, not optimised — change with reason, not curve-fitting)

| Param | Value | Source |
|---|---|---|
| timeframe | daily | ch.5 |
| universe | BTC, ETH, SOL | ch.4 |
| regime gauge | BTC 200d MA + slope | ch.6, ch.10 |
| entry | 55-day Donchian breakout from a base | ch.7 / [[trend-edge-research]] |
| volume confirm | >1.5× 20d avg | ch.7 |
| no-chase band | ≤5% past breakout | ch.7 |
| stop | entry − 2.5×ATR(14) | ch.10–11 |
| risk/trade | 0.75% of equity | ch.10–11 |
| sizing | risk-parity by ATR | ch.11 |
| trail | highest-high − 3×ATR (chandelier) | ch.10 |
| macro exit | BTC closes below 200d MA | ch.10 |
| partial | ⅓–½ after parabolic run, then BE stop | ch.10–11 |
| shorts | OFF (bear-only, MA-cross, hedge) | ch.12 |
| re-entry lockout | 5 days after a stop | ch.14 |
| daily-loss pause | yes | ch.14 |
