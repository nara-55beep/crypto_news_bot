"""
================================================================================
 CONFIG  —  THIS IS THE ONLY FILE YOU NEED TO EDIT TO GET RUNNING
================================================================================
Fill in the three things marked with  >>> EDIT <<<  below:
  1. Telegram API credentials  (free, from https://my.telegram.org)
  2. Your AI API key + model    (the only paid thing)
That's it. Everything else has sane defaults. Read the comments.

Mode is PAPER by default. It will NOT touch real money. Leave it that way until
you have a real track record (see README).
================================================================================
"""

import os

# ---------------------------------------------------------------------------
# 0. MODE
# ---------------------------------------------------------------------------
# "PAPER" = simulated $100 account, real live prices, no real orders. SAFE.
# "LIVE"  = real Binance futures orders. DO NOT use until you've read README +
#           proven the system in paper. Live execution module is intentionally
#           left as a stub you must consciously wire up.
MODE = "PAPER"


# ---------------------------------------------------------------------------
# 1. TELEGRAM  (free)
# ---------------------------------------------------------------------------
# Go to https://my.telegram.org -> "API development tools" -> create an app.
# You get an api_id (int) and api_hash (string). Paste them here.
# First run will ask for your phone number + a login code (one time).
#
#   >>> SET THESE AS PRIVATE ENVIRONMENT VARIABLES <<<
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")

# Telegram bot used for outbound Lucid signal alerts. This sends alerts as real
# incoming bot messages, so Telegram can play normal notification sounds.
TELEGRAM_ALERT_BOT_TOKEN = os.getenv("TELEGRAM_ALERT_BOT_TOKEN", "")
TELEGRAM_ALERT_CHAT_ID = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")

# Lucid futures signals have two live-feed paths. The strict default below uses
# the same Dukascopy tick-derived proxy candles as the saved 36/36 backtest. The
# TradingView path is kept for explicit non-matching CME experiments only.
LUCID_USE_TRADINGVIEW_LIVE = os.getenv("LUCID_USE_TRADINGVIEW_LIVE", "1") == "1"
LUCID_TRADINGVIEW_AUTH_TOKEN = os.getenv("LUCID_TRADINGVIEW_AUTH_TOKEN", "")
LUCID_REQUIRE_REALTIME_FEED = os.getenv("LUCID_REQUIRE_REALTIME_FEED", "1") == "1"
# The 36/36 Lucid backtest was produced from Dukascopy tick-derived CFD proxy
# candles, not CME futures candles. Keep strict source matching on by default so
# the paper bot never silently trades a different feed while showing that report.
LUCID_REQUIRE_BACKTEST_SOURCE_MATCH = os.getenv("LUCID_REQUIRE_BACKTEST_SOURCE_MATCH", "1") == "1"
# Hard live-entry gate: the saved backtest can be replayed from historical/public
# Dukascopy files, but real signals should only open from the local realtime
# bridge. Keep this on unless you are deliberately running a delayed-data
# experiment.
LUCID_REQUIRE_EXACT_REALTIME_ENTRY = os.getenv("LUCID_REQUIRE_EXACT_REALTIME_ENTRY", "1") == "1"
LUCID_LIVE_SOURCE = os.getenv(
    "LUCID_LIVE_SOURCE",
    "dukascopy" if LUCID_REQUIRE_BACKTEST_SOURCE_MATCH else "tradingview",
).lower()
LUCID_DUKASCOPY_POLL_SEC = float(os.getenv("LUCID_DUKASCOPY_POLL_SEC", "30"))
LUCID_DUKASCOPY_CATCHUP_DAYS = int(os.getenv("LUCID_DUKASCOPY_CATCHUP_DAYS", "30"))
LUCID_DUKASCOPY_FEED_GRACE_SEC = int(os.getenv("LUCID_DUKASCOPY_FEED_GRACE_SEC", "120"))
LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL = int(os.getenv("LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL", "2"))
# Optional local live bridge for a future JForex/Dukascopy producer. Disabled
# unless LUCID_LIVE_SOURCE=local_bridge. Files must be 1m bars named
# lucid_live_bridge_es_1m.csv, lucid_live_bridge_nq_1m.csv, lucid_live_bridge_cl_1m.csv.
LUCID_LOCAL_BRIDGE_DIR = os.getenv(
    "LUCID_LOCAL_BRIDGE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
LUCID_LOCAL_BRIDGE_PREFIX = os.getenv("LUCID_LOCAL_BRIDGE_PREFIX", "lucid_live_bridge_")
LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = os.getenv("LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "")
LUCID_LOCAL_BRIDGE_POLL_SEC = float(os.getenv("LUCID_LOCAL_BRIDGE_POLL_SEC", "1"))
LUCID_BRIDGE_MAX_FUTURE_SEC = float(os.getenv("LUCID_BRIDGE_MAX_FUTURE_SEC", "120"))

# Channels to listen to (usernames, no t.me/). These are public; your account
# must be able to view them (join them in the Telegram app first if needed).
TELEGRAM_CHANNELS = [
    # Impersonators of newswire accounts are common — verify each @handle in your
    # own Telegram app. A handle that can't be resolved is just skipped (the
    # console says which) and won't break the others.
    "BWEnews", "trad_fin", "SynopticNewswire", "WalterBloomberg", "marketfeed",
    "CryptoRankNews", "WatcherGuru",
    "Cointelegraph", "Coindesk", "CoinGeckoNews", "theblocknewsfeed",
    "CoinMarketCap", "CryptoQuant", "BinanceAnnouncements", "coingape",
    "cryptonewspaper", "unfolded", "cryptopush", "CryptoUnfolded",
    "whale_alert", "cryptoindustry",
]

# ---------------------------------------------------------------------------
# 2. AI API  (the only paid component — works with ANY provider)
# ---------------------------------------------------------------------------
# You don't have to know your provider in advance. Almost every AI API speaks the
# "OpenAI-compatible" dialect, so one setting covers nearly everything. Pick how
# the bot talks to your AI with AI_PROVIDER:
#
#   "openai"    -> OpenAI-compatible /v1/chat/completions. USE THIS for: OpenAI,
#                  OpenRouter (one key -> Claude/GPT/Llama/etc), Groq, Together,
#                  Fireworks, DeepSeek, xAI/Grok, Mistral, AND any LOCAL model
#                  (Ollama / llama.cpp / vLLM). If your provider mentions an
#                  "OpenAI-compatible endpoint" (most do), this is it.
#   "anthropic" -> Anthropic's native /v1/messages (only if calling Claude
#                  directly and not via OpenRouter/compat).
#   "raw"       -> a hand-written HTTP call you edit yourself, for any provider
#                  whose format is neither of the above (rare).
#
# HOW each one is written is fully explained + editable in ai_client.py. You only
# ever touch ONE function there.
#
#   >>> SET UP FOR GROQ (free tier) <<<
# Switched off Gemini on 2026-08-06 because its prepaid credits were depleted
# ("Your prepayment credits are depleted") which silently killed every AI bot.
# Groq is genuinely free, OpenAI-compatible (so AI_PROVIDER stays "openai"), and
# llama-3.3-70b-versatile was verified returning valid strict JSON in ~1.5s on the
# penny-stock analysis prompt. Model list: https://console.groq.com/docs/models
AI_PROVIDER = "openai"
# SECURITY: this key was exposed in a shared transcript - ROTATE IT at
# https://console.groq.com/keys and then set it as an environment variable:
#     setx GROQ_API_KEY "your-new-key"
# No credential fallback is committed; a missing environment value fails closed.
AI_API_KEY = os.getenv("GROQ_API_KEY", "")
AI_BASE_URL = "https://api.groq.com/openai/v1"
AI_MODEL    = "llama-3.1-8b-instant"
# ^ the HIGH-FREQUENCY bots (news reactors) use this. Groq meters tokens PER MODEL
# (12k tokens/min), so keeping the chatty bots on the small model stops them starving
# the deeper analysis, which runs on its own model below.
PENNY_AI_MODEL = "llama-3.3-70b-versatile"   # deeper reasoning for company analysis
# Other free Groq options: "llama-3.1-8b-instant" (faster, weaker reasoning),
# "qwen/qwen3.6-27b". NOTE: "openai/gpt-oss-120b" was tested and returned an
# error payload instead of a completion, so avoid it for structured JSON.
#
# --- previous Gemini settings, kept so you can switch back after topping up ---
# Previous-provider example: AI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# AI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
# AI_MODEL    = "gemini-2.5-flash-lite"

# Hard timeout on the AI call. If it doesn't answer in time we DROP the signal
# rather than place a stale trade. News alpha decays in seconds.
AI_TIMEOUT_SEC = 6.0

# ---- TREE OF ALPHA live feed --------------------------------------------------
# The /treeofalpha page + the News Reactor bot read Tree of Alpha's news feed.
# Leave this EMPTY to use the free public REST feed (polled ~1s — near-instant).
# If you have a Tree of Alpha PREMIUM api key, paste it here to switch to their
# WebSocket for TRUE instant push (zero polling). Get one at news.treeofalpha.com.
TOA_API_KEY = os.getenv("TOA_API_KEY", "")

# ---- GROQ (the "GPTnews" strategy) — free, fast, OpenAI-compatible endpoint -----
# Used by the paper News+Whale bot's "GPTnews" strategy via the openai library.
# Get/rotate the key at https://console.groq.com/keys  (keep it secret).
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL  = "https://api.groq.com/openai/v1"
GROQ_MODEL     = "openai/gpt-oss-120b"
GROQ_TIMEOUT_SEC = 8.0

# ---- ANTHROPIC / CLAUDE (the "claude news haiku" strategy) — same flow as GPTnews,
#      but the model is Claude WITH adaptive thinking. Used by the REAL-money Lighter bot.
# Get/rotate the key at https://console.anthropic.com  (Billing → buy credits first; the
# $100 Claude app plan does NOT fund the API). KEEP THIS SECRET — rotate if it leaks.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Haiku 4.5 cannot do extended thinking; Sonnet 4.6 can — so to honour "Claude's thinking"
# this runs Sonnet 4.6. Switch to "claude-haiku-4-5" here if you'd rather have a plain fast
# call with no thinking (then set CLAUDE_NEWS_EFFORT to "" so we don't send thinking params).
CLAUDE_NEWS_MODEL  = "claude-sonnet-4-6"  # upgraded from Haiku for far stronger news judgment
CLAUDE_NEWS_EFFORT = ""             # left EMPTY = no extended thinking, so the decision stays FAST
                                    # (news must be quick). Set to e.g. "medium" for deeper but
                                    # slower reasoning. Costs ~3-4x more per headline than Haiku.
                                   #   Switch model to "claude-sonnet-4-6" + effort "low"/"medium"
                                   #   if you want deliberate thinking again (and ~3x the cost).
CLAUDE_TIMEOUT_SEC = 15.0           # plain fast call; no thinking to wait on

# *** ABOUT "MILLISECONDS" (read this) ***
# Telegram delivery + all the bot's own code (filter/risk/execute) is sub-ms to a
# few ms. The ONLY slow step is the AI call:
#   - hosted API (GPT/Claude/etc): ~300-3000 ms  -> NOT milliseconds, by nature.
#   - LOCAL small model via Ollama/llama.cpp:     ~50-300 ms -> the fast path.
# So if you truly want millisecond-class DECISIONS, set AI_PROVIDER="openai" and
# point AI_BASE_URL at a local model (e.g. http://localhost:11434/v1, AI_MODEL
# "llama3.1"). The keyword pre-filter (section 5) already rejects obvious noise
# in microseconds before any AI is called. See pipeline latency printout.


# ---------------------------------------------------------------------------
# 3. EXTRA NEWS SOURCES — DISABLED (Telegram-only build)
# ---------------------------------------------------------------------------
# Twitter/X + Nitter were removed per your request: there is no free real-time X
# feed worth the unreliability, and the accounts you wanted (e.g. DeItaone /
# WalterBloomberg) are mirrored to Telegram anyway. Telegram is now the sole
# source. RSS is left here only as an OPTIONAL macro add-on — empty = off.
RSS_FEEDS = []            # e.g. ["https://feeds.feedburner.com/zerohedge/feed"] to re-enable
RSS_POLL_SECONDS = 20


# ---------------------------------------------------------------------------
# 4. MARKET / SYMBOLS  (Binance public data — free, no key)
# ---------------------------------------------------------------------------
# Majors + top liquid alts. These all have deep Binance USDⓈ-M futures markets,
# so paper fills are realistic. We deliberately do NOT include tiny/illiquid alts:
# on a $100 5x account, thin order books mean brutal slippage + instant liquidation.
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT",            # majors
    "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT",
    "DOTUSDT", "MATICUSDT", "LTCUSDT", "BCHUSDT", "ATOMUSDT", "UNIUSDT",
    "TRXUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "FILUSDT",
    "INJUSDT", "SUIUSDT", "SEIUSDT", "TIAUSDT", "RUNEUSDT", "AAVEUSDT",
    "ETCUSDT", "FTMUSDT", "ALGOUSDT", "1000PEPEUSDT", "WIFUSDT", "ENAUSDT",
]

# Map a free-text asset name (what the AI returns) to a tradable symbol.
# The AI may say "Bitcoin" or "BTC" or "$BTC"; all should resolve.
ASSET_TO_SYMBOL = {
    "BTC": "BTCUSDT", "BITCOIN": "BTCUSDT",
    "ETH": "ETHUSDT", "ETHEREUM": "ETHUSDT", "ETHER": "ETHUSDT",
    "SOL": "SOLUSDT", "SOLANA": "SOLUSDT",
    "BNB": "BNBUSDT", "BINANCE COIN": "BNBUSDT",
    "XRP": "XRPUSDT", "RIPPLE": "XRPUSDT",
    "ADA": "ADAUSDT", "CARDANO": "ADAUSDT",
    "DOGE": "DOGEUSDT", "DOGECOIN": "DOGEUSDT",
    "AVAX": "AVAXUSDT", "AVALANCHE": "AVAXUSDT",
    "LINK": "LINKUSDT", "CHAINLINK": "LINKUSDT",
    "DOT": "DOTUSDT", "POLKADOT": "DOTUSDT",
    "MATIC": "MATICUSDT", "POLYGON": "MATICUSDT",
    "LTC": "LTCUSDT", "LITECOIN": "LTCUSDT",
    "BCH": "BCHUSDT", "BITCOIN CASH": "BCHUSDT",
    "ATOM": "ATOMUSDT", "COSMOS": "ATOMUSDT",
    "UNI": "UNIUSDT", "UNISWAP": "UNIUSDT",
    "TRX": "TRXUSDT", "TRON": "TRXUSDT",
    "NEAR": "NEARUSDT",
    "APT": "APTUSDT", "APTOS": "APTUSDT",
    "ARB": "ARBUSDT", "ARBITRUM": "ARBUSDT",
    "OP": "OPUSDT", "OPTIMISM": "OPUSDT",
    "FIL": "FILUSDT", "FILECOIN": "FILUSDT",
    "INJ": "INJUSDT", "INJECTIVE": "INJUSDT",
    "SUI": "SUIUSDT",
    "SEI": "SEIUSDT",
    "TIA": "TIAUSDT", "CELESTIA": "TIAUSDT",
    "RUNE": "RUNEUSDT", "THORCHAIN": "RUNEUSDT",
    "AAVE": "AAVEUSDT",
    "ETC": "ETCUSDT", "ETHEREUM CLASSIC": "ETCUSDT",
    "FTM": "FTMUSDT", "FANTOM": "FTMUSDT",
    "ALGO": "ALGOUSDT", "ALGORAND": "ALGOUSDT",
    "PEPE": "1000PEPEUSDT",
    "WIF": "WIFUSDT", "DOGWIFHAT": "WIFUSDT",
    "ENA": "ENAUSDT", "ETHENA": "ENAUSDT",
}


# ---------------------------------------------------------------------------
# 5. DECISIONS  (the AI decides — no numeric thresholds anymore)
# ---------------------------------------------------------------------------
# You asked for the AI to decide everything itself. It does: ai_analyzer.py asks
# it to return TRADE or SKIP using its own trader judgment (weighing importance,
# priced-in, conviction internally). There are NO importance/confidence cutoffs
# in the bot now. The only thing that can still stop a trade is the ACCOUNT
# SAFETY section below (daily loss cap, trades/hour, etc.) — that protects the
# $100 and is not a market opinion.

# Send EVERY message to the AI (no keyword pre-filter). The AI decides relevance.
#   - uses more of your Gemini free-tier quota (every message = 1 AI call)
#   - a fast-posting channel can hit rate limits -> you'll see [AI ERROR] 429 lines
ANALYZE_ALL = True

# Used ONLY as a fallback if you ever set ANALYZE_ALL=False again.
KEYWORD_PREFILTER = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
    "etf", "sec", "fed", "fomc", "powell", "rate", "inflation", "cpi", "ppi",
    "trump", "tariff", "war", "sanction", "hack", "exploit", "approval",
    "regulation", "ban", "liquidat", "treasury", "jobs", "nfp",
]


# ---------------------------------------------------------------------------
# 6. RISK & SIZING  (this is what keeps the $100 alive)
# ---------------------------------------------------------------------------
STARTING_BALANCE_USDT = 100.0

RISK_PCT_PER_TRADE  = 0.02     # risk 2% of equity per trade at stop-loss
MAX_LEVERAGE        = 5         # cap leverage (start LOW). Paper or not.
MAX_CONCURRENT      = 2         # max simultaneous open positions
MAX_TRADES_PER_HOUR = 6         # anti-overtrading throttle

# Stop / take are PRICE moves (not account %). With leverage L, a 1.5% price
# move against you = 1.5% * L of your margin.
STOP_LOSS_PCT   = 0.015        # 1.5% adverse price move -> exit
TAKE_PROFIT_PCT = 0.030        # 3.0% favorable price move -> exit
MAX_HOLD_MINUTES = 90          # time-stop: news alpha is short-lived

# Circuit breakers
MAX_DAILY_LOSS_PCT = 0.10      # stop trading for the day after -10% equity
COOLDOWN_AFTER_LOSSES = 2      # this many consecutive losses ->
COOLDOWN_MINUTES = 30          #   pause new entries for this long


# ---------------------------------------------------------------------------
# 6b. NEWS + WHALE BOT  (the fast, mechanical bot shown on the chart page)
# ---------------------------------------------------------------------------
# This bot ignores AI sentiment. On ANY news it starts WATCHING for a few
# seconds; the moment whale taker-flow crosses the threshold AND price is
# moving that way AND the order book agrees, it bets. It exits on a fixed
# stop-loss / take-profit (price %), with a time-stop as a safety net.
NW_ENABLED         = True      # master on/off (you can also toggle it in the UI)
NW_START_BALANCE   = 100.0     # its own separate paper balance (USDT)
NW_WATCH_SECONDS   = 30        # after news, watch this long for a signal
NW_FLOW_WINDOW     = 60        # seconds of trades used for the buy/sell %
NW_FLOW_ENTER      = 0.60      # enter when the dominant side >= 60%
NW_MIN_MOVE_PCT    = 0.0005    # price must have moved >= 0.05% since the news
NW_LEVERAGE        = 20        # leverage for its trades
NW_RISK_FRAC       = 1.0       # margin per trade = 100% of its balance
NW_STOP_LOSS_PCT   = 0.00125   # close at a 0.125% adverse price move
NW_TAKE_PROFIT_PCT = 0.0025    # close at a 0.25% favorable price move
NW_MAX_HOLD_MIN    = 45        # safety: force-close after this many minutes
NW_MAX_CONCURRENT  = 1         # how many open positions it may hold at once


# ---------------------------------------------------------------------------
# 6c. TRADES TAPE  (the "Order Book + Trades" page)
# ---------------------------------------------------------------------------
# Minimum trade size (USD) KEPT IN THE TAPE — and therefore counted in the buy/sell
# flow %. 0 = keep EVERY trade, so flow reflects ALL order flow (not just whales).
# This is what the flow-based strategies read to pick their instant direction.
WHALE_MIN_USD = 10000
# Trades below this (USD) are NOT streamed to the browser / shown in the trades panel.
# 0 = show EVERY trade (full firehose). WARNING: in a busy minute this is tens of
# thousands of rows; the browser tab will get sluggish or freeze past ~10-20k rows.
WHALE_DISPLAY_USD = 10000


# Trades at least this big (USD) get a marker drawn on the price chart
# ("whale prints"), so you can see where big money hit relative to price.
BIG_TRADE_USD = 100000


# Promotional / junk messages to IGNORE completely (case-insensitive substring match).
# Any incoming news containing one of these is dropped at the door: not shown on the
# feed, not logged, and no bot reacts to it. Add more lines as channels spam more.
NEWS_IGNORE_SUBSTRINGS = [
    "follow us on x for even faster headlines",
    "x.com/marketnews_feed",
]

# News sources to IGNORE completely (case-insensitive exact source match).
NEWS_IGNORE_SOURCES = [
    "tg:cointelegraph",
]


# ---------------------------------------------------------------------------
# 6d. ON-CHAIN RADAR PAPER BOT
# ---------------------------------------------------------------------------
# Paper-only bot that scans chain-wide Transfer logs for major tokens plus major
# AMM pool-creation events. It does NOT need a pre-picked wallet list; every
# sender/receiver in those logs is considered, and useful wallets are learned
# over time from whether their signals predicted BTC moves.
ONCHAIN_ENABLED = True
ONCHAIN_START_BALANCE = 100.0
ONCHAIN_LEVERAGE = 20
ONCHAIN_RISK_FRAC = 1.0
ONCHAIN_POLL_SECONDS = 20
ONCHAIN_RPC_TIMEOUT = 8.0

# Transfer must be at least this large to be considered. Unknown-wallet moves
# need to be even larger; exchange-wallet moves can trade at this threshold.
ONCHAIN_MIN_TRANSFER_USD = 1_000_000
ONCHAIN_HUGE_TRANSFER_USD = 5_000_000

# Trade only when the rolling on-chain score is strong enough. Risk is measured
# as percent of margin, so the stop/trailing behavior scales with leverage.
ONCHAIN_SIGNAL_WINDOW_SEC = 600
ONCHAIN_TRADE_THRESHOLD = 3.0
ONCHAIN_TRADE_COOLDOWN_SEC = 600
ONCHAIN_CONFIRM_SECONDS = 60
ONCHAIN_MAX_AGAINST_MOMENTUM = 0.0005
ONCHAIN_STOP_MARGIN_PCT = 0.05
ONCHAIN_TRAIL_ARM_MARGIN_PCT = 0.08
ONCHAIN_TRAIL_GAP_MARGIN_PCT = 0.05
ONCHAIN_OPPOSITE_EXIT_SCORE = 2.5
ONCHAIN_MAX_HOLD_SEC = 3600

# Wallet learning: after this many seconds, judge whether a wallet's signal was
# directionally right and slightly boost/cut future signals involving it.
ONCHAIN_LEARN_HORIZON_SEC = 1800
ONCHAIN_LEARN_MIN_MOVE = 0.0008

# Public RPC endpoints are enough for paper testing. If one rate-limits, replace
# it here with your own Alchemy/Infura/QuickNode/PublicNode URL.
ONCHAIN_RPC_URLS = {
    "ethereum": "https://ethereum.publicnode.com",
    "base": "https://base-rpc.publicnode.com",
    "bsc": "https://bsc-dataseed.binance.org",
}

# Optional: add more labeled exchange wallets here. The bot still scans all
# wallets either way; labels only make inflow/outflow direction more reliable.
ONCHAIN_EXCHANGE_WALLETS = {}


# Exchanges to pull the order book + trade tape from, via CCXT (one unified API).
# These are the most liquid venues. You can add ANY CCXT exchange id here
# (e.g. "huobi", "mexc", "cryptocom", "bingx") — but more exchanges = more
# requests and slower refresh, and tiny venues add noise, so keep it sensible.
EXCHANGES = ["binance", "bybit", "okx", "coinbase", "kraken",
             "gate", "kucoin", "bitget", "hyperliquid"]


# ---------------------------------------------------------------------------
# 7. STORAGE
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)   # create the data/ folder if it's missing
DB_PATH  = os.path.join(DATA_DIR, "trades.db")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "tg_session")


# ===========================================================================
# 8. STOCKS  (the /stocks page + the stock AI news bot)  — uses ALPACA
# ===========================================================================
# Stock market data is NOT free+keyless like crypto. Alpaca gives a free key and
# real-time data from the IEX exchange (a real venue). Make a free account at
# https://alpaca.markets -> "Paper / API keys" -> paste the two values below.
# (We only READ market data with these; no real orders are ever sent.)
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_DATA_URL   = "https://data.alpaca.markets"
ALPACA_FEED       = "iex"        # free tier = "iex". (Paid SIP = "sip".)

# The stocks the bot watches and can trade. Add/remove freely (keep it to a few
# dozen — free data is rate-limited). Use the exchange TICKER.
STOCK_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX",
    "AVGO", "INTC", "MU", "QCOM", "ORCL", "CRM", "ADBE", "PYPL", "UBER",
    "JPM", "BAC", "GS", "WFC", "V", "MA", "DIS", "BA", "CAT", "XOM", "CVX",
    "PFE", "JNJ", "KO", "PEP", "WMT", "COST", "MCD", "NKE", "SPY", "QQQ",
]

# Company / common names -> ticker, so the AI can map a headline to a symbol.
STOCK_NAME_TO_SYMBOL = {
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "meta": "META", "facebook": "META",
    "tesla": "TSLA", "amd": "AMD", "netflix": "NFLX", "broadcom": "AVGO",
    "intel": "INTC", "micron": "MU", "qualcomm": "QCOM", "oracle": "ORCL",
    "salesforce": "CRM", "adobe": "ADBE", "paypal": "PYPL", "uber": "UBER",
    "jpmorgan": "JPM", "jp morgan": "JPM", "bank of america": "BAC",
    "goldman": "GS", "goldman sachs": "GS", "wells fargo": "WFC",
    "visa": "V", "mastercard": "MA", "disney": "DIS", "boeing": "BA",
    "caterpillar": "CAT", "exxon": "XOM", "chevron": "CVX", "pfizer": "PFE",
    "johnson": "JNJ", "coca-cola": "KO", "coca cola": "KO", "coke": "KO",
    "pepsi": "PEP", "pepsico": "PEP", "walmart": "WMT", "costco": "COST",
    "mcdonald": "MCD", "nike": "NKE", "s&p": "SPY", "s&p 500": "SPY",
    "nasdaq": "QQQ", "nasdaq 100": "QQQ",
}

# The stock bot's own paper account + behaviour (mirrors the crypto news bot).
STOCK_ENABLED       = True
STOCK_ANALYZE_ALL   = True      # send every news message to the AI
STOCK_START_BALANCE = 1000.0    # its own separate paper balance (USD)
STOCK_RISK_FRAC     = 0.10      # margin per trade = 10% of its balance
STOCK_LEVERAGE      = 1         # 1 = cash (no leverage). Stocks are less leveraged.
STOCK_STOP_LOSS_PCT = 0.02      # close at a 2% adverse move
STOCK_TAKE_PROFIT_PCT = 0.04    # close at a 4% favorable move
STOCK_MAX_HOLD_MIN  = 240       # force-close after this many minutes
STOCK_MAX_CONCURRENT = 3        # how many open stock positions at once
STOCK_TRADES_CSV    = os.path.join(DATA_DIR, "stock_news_trades.csv")


# ============================================================
# 9) LIGHTER  —  REAL-MONEY DEX  (https://lighter.xyz)
# ------------------------------------------------------------
# This is REAL MONEY, not paper. We are doing it in safe stages.
#
# STEP 1 (now): READ-ONLY. Just SHOW your real Lighter balance + positions, to
#   confirm the connection works. This places NO orders and needs NO private key
#   — it is completely safe. Fill in EITHER of the two below (the wallet address
#   is easiest — it's public, not a secret):
LIGHTER_TRADING_ENABLED = False
LIGHTER_ENABLED = False
LIGHTER_L1_ADDRESS = os.getenv("LIGHTER_L1_ADDRESS", "")
LIGHTER_ACCOUNT_INDEX = (int(os.getenv("LIGHTER_ACCOUNT_INDEX")) if os.getenv("LIGHTER_ACCOUNT_INDEX") else None)
LIGHTER_TESTNET       = False          # True = practice testnet, False = real mainnet
LIGHTER_SIGNER_PATH = "lighter-signer-windows-amd64.dll"
#
# STEP 2 (LATER — only after Step 1 shows your real balance): placing REAL orders.
#   THAT needs the items below, generated at https://app.lighter.xyz/apikeys.
#   Leave them blank for now; Step 1 does not use them and cannot place trades.
LIGHTER_API_PRIVATE_KEY = os.getenv("LIGHTER_API_PRIVATE_KEY", "")
LIGHTER_API_KEY_INDEX = (int(os.getenv("LIGHTER_API_KEY_INDEX")) if os.getenv("LIGHTER_API_KEY_INDEX") else None)
