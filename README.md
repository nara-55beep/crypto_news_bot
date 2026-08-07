# Crypto News Trading Bot — Paper Mode

Autonomous news-driven paper-trading bot for **BTC / ETH / SOL only**. Listens to
Telegram + RSS, sends each headline through an AI analyzer that reasons in causal
chains, gates it through risk rules, and simulates trades on a virtual **$100**
account using **real live Binance prices**. No real money is touched.

---

## Private configuration (never commit credentials)

Create your machine-local configuration from the committed safe template:

```powershell
Copy-Item config.example.py config.py
```

`config.py`, `.env*`, runtime databases, Telegram sessions, logs, account state,
and downloaded market data are intentionally ignored by Git. Put credentials in
system/deployment environment variables named in `.env.example`; never paste real
keys into `config.example.py`, frontend JavaScript, documentation, or screenshots.

1. **Telegram** — get a free `api_id` + `api_hash` at https://my.telegram.org
   and set `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` privately. Join the channels
   in your Telegram app so your account can read them.
2. **AI key** — set `GROQ_API_KEY` (or the provider variable used by your local
   configuration) privately. Hosted OpenAI-compatible services or a local model
   such as Ollama/llama.cpp can be used.

Everything else (Binance market data, RSS, paper trading) is free and needs no key.

Before deploying, configure the same variables in the host's secret manager. A
private key placed in browser JavaScript is public to every visitor even when the
GitHub repository itself is private.

## Run

```bash
pip install -r requirements.txt
python main.py          # first Telegram run prompts for phone + login code, once
```

You'll see live prices, then a stream of `[SKIP]` / `[OPEN]` / `[CLOSE]` /
`[status]` lines. Every news item (traded or not) is logged to `data/trades.db`
(SQLite) — query it later to learn what you skipped and how it moved.

---

## Read this — honest expectations

- **Twitter/X is not in here, on purpose.** Free real-time X access no longer
  exists (Nitter is dormant; X's free API is ~1 request / 15 min). `bwenews` is
  already covered via Telegram, and ZeroHedge via free RSS. To add the others
  you'd pay for an X data API (TwitterAPI.io, Sorsa, etc.) and write a small
  poller that calls `on_news(...)` — the bot is built to accept new sources
  trivially.
- **You will not win the latency race against firms.** By the time news hits
  BWEnews/DeItaone, firms with direct feeds + colocation already traded it. Your
  realistic latency is hundreds of ms to seconds (Telegram hop + AI call), not
  the sub-1s in the original spec. A hosted frontier-model call alone is often
  0.5–3s. **Treat this as a system to measure whether you have an edge, not to
  beat HFT.**
- **Most retail news bots lose money.** Run it in paper for weeks. If the
  equity curve in `data/trades.db` isn't convincingly positive across many
  trades and different market regimes, you don't have an edge yet — and going
  live would just lose money faster. The whole point of paper mode is to find
  this out at zero risk.

## Latency (if you want to chase it later)

The hosted AI call is the bottleneck. The professional pattern is **three tiers**:
a tiny fine-tuned classifier (~25ms, local) as a fast gate → a local quantized
~7B model (~300ms) for structured analysis → a frontier API only for async
logging/learning. The `KEYWORD_PREFILTER` in `config.py` is a stand-in for that
fast tier today. Running the model **locally** (set `AI_BASE_URL` to Ollama/llama.cpp)
removes network round-trip and per-call cost.

## Tuning

All knobs are in `config.py` section 5–6. Start **strict** (high `MIN_IMPORTANCE`/
`MIN_CONFIDENCE`, low `MAX_LEVERAGE`). Loosen only if paper results justify it.
`RISK_PCT_PER_TRADE` controls how fast the account can bleed — leave it small.

---

## Not built yet (the original spec's bigger pieces)

These are deliberately out of this MVP; add them once paper trading shows promise:

- **Backtester** — replay historical headlines + minute price data through
  `pipeline.handle_news` with a mocked market/broker. The code is already
  structured for this (the broker and market are injected), so a backtest harness
  is mostly a historical data loader + a clock.
- **Learning / memory (RAG)** — embed each `news` row, store vectors, and at
  inference retrieve the top-k most similar past events + their outcomes to feed
  the AI as context. `data/trades.db` already stores the raw material.
- **Live execution** — see below.

## Going live (gated on purpose)

`MODE = "LIVE"` **raises and refuses to run** — there is no live broker wired, by
design, so you can't flip a flag and accidentally trade real money. To go live you
must consciously write a `LiveBroker` with the **same method names** as
`PaperBroker` (`open_position`, `mark_to_market`, `equity`, ...) that calls the
real Binance USDⓈ-M Futures API (signed REST + user-data websocket), then swap it
in `main.py`. Do that **only** after a real paper track record, and start with the
smallest size the exchange allows. Trading derivatives with leverage can lose more
than your deposit. This is not financial advice.
