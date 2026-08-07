# Lucid Local Live Bridge

This bridge is optional and disabled by default. It exists so a future live
Dukascopy/JForex producer can feed the verified Lucid strategy with realtime 1m
bars while preserving strict source matching.

The default live source is still the public Dukascopy `.bi5` historical tick-file
poller. Do not switch to the bridge until the validator says `READY`.

## Enable

Set these environment variables before starting the website:

```powershell
$env:LUCID_LIVE_SOURCE = "local_bridge"
$env:LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = "dukascopy_tick_proxy"
```
The website also defaults to a hard live-entry guard:

```powershell
$env:LUCID_REQUIRE_EXACT_REALTIME_ENTRY = "1"
```

With this on, the Lucid bots will not open new positions from public Dukascopy
polling, TradingView, Yahoo, or any other non-bridge feed. Historical replay can
still prove the 36/36 strategy, but live paper entries require the local bridge
to be ready.

Optional:

```powershell
$env:LUCID_LOCAL_BRIDGE_DIR = "C:\Users\<you>\OneDrive\Desktop\crypto_news_bot\data"
$env:LUCID_LOCAL_BRIDGE_PREFIX = "lucid_live_bridge_"
$env:LUCID_LOCAL_BRIDGE_POLL_SEC = "1"
```

## Required Files

The producer must write three CSV files:

```text
data/lucid_live_bridge_es_1m.csv
data/lucid_live_bridge_nq_1m.csv
data/lucid_live_bridge_cl_1m.csv
```

These map to the exact backtest proxy instruments:

```text
es -> Dukascopy USA500IDXUSD
nq -> Dukascopy USATECHIDXUSD
cl -> Dukascopy LIGHTCMDUSD
```

## CSV Schema

Each file must contain 1-minute OHLCV bars with UTC timestamps.

Required columns:

```text
dt_utc,open,high,low,close,volume
```

Example:

```csv
dt_utc,open,high,low,close,volume
2026-07-07T13:00:00Z,7540.25,7541.00,7539.75,7540.50,12.0
2026-07-07T13:01:00Z,7540.50,7542.00,7540.25,7541.75,14.0
```

Rules:

- `dt_utc` must be the bar open time, not close time.
- Timestamps must be UTC.
- Bars must be complete 1-minute bars.
- Duplicate timestamps are allowed, but the last row wins.
- Prices must use the same scale as the historical Dukascopy cache.
- The producer should write atomically: write `*.tmp`, then replace the final CSV.
- OHLC rows are rejected/blocked if prices are non-finite, non-positive, high
  does not contain open/close, low does not contain open/close, high is below
  low, volume is negative, or the timestamp is too far in the future.
- `LUCID_BRIDGE_MAX_FUTURE_SEC` defaults to `120`. Increase it only for
  synthetic smoke tests, not for real live trading.

## Validate

Run:

```powershell
python tools\validate_lucid_bridge.py
```

The bridge is usable only when the tool prints:

```text
READY
```

If it prints `NOT READY`, the website should stay on default `dukascopy` mode or
the Lucid bots will block the bridge before strategy logic runs.

Smoke-test the bridge plumbing without touching production files:

```powershell
python tools\smoke_lucid_bridge.py
```

This uses a temporary directory and synthetic bars. It does not prove market data
quality, but it proves the receiver/loader/source/history/freshness gates can
all pass when fresh rows exist.

Audit the JForex producer source:

```powershell
python tools\verify_lucid_jforex_bridge.py
```

This does not compile or run JForex. It checks that the included producer is a
non-trading strategy that only subscribes to `USA500IDXUSD`, `USATECHIDXUSD`,
and `LIGHTCMDUSD`, then forwards ticks to the local receiver.

## Safety Gates

The bridge is still blocked unless all are true:

- `LUCID_LOCAL_BRIDGE_SOURCE_FAMILY=dukascopy_tick_proxy`
- all three CSV files exist
- CSV schema parses cleanly
- enough historical context exists for Turtle/NR7 components
- completed bars are fresh enough for the component being scanned

The strategy engine is unchanged. The bridge only supplies 1m bars.

## Local HTTP Receiver

`tools\lucid_bridge_receiver.py` is a localhost writer for those CSV files. It
does not fetch market data by itself; it receives exact live ticks/bars from a
separate producer such as Dukascopy/JForex.

Start it:

```powershell
$env:LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = "dukascopy_tick_proxy"
python tools\lucid_bridge_receiver.py
```

Receiver checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
Invoke-RestMethod http://127.0.0.1:8765/ready
```

`/health` only means the local receiver is alive. `/ready` uses the same source,
file, history, and freshness gates as the Lucid bot. If `/ready` returns HTTP
`503` or `ready:false`, the website must not be switched to local bridge mode.
If any component is stale, `/ready` is also false even though the bot may still
scan other fresh components; this prevents mistaking a partial feed for a fully
realtime bridge.

Optional safety token:

```powershell
$env:LUCID_BRIDGE_TOKEN = "choose-a-secret"
```

Then the producer sends either completed 1m bars:

```powershell
$body = @{
  market = "es"
  dt_utc = "2026-07-07T13:00:00Z"
  open = 7540.25
  high = 7541.00
  low = 7539.75
  close = 7540.50
  volume = 12
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8765/bar -Method POST -Body $body -ContentType "application/json"
```

Or live ticks, which the receiver rolls into 1m OHLCV bars:

```powershell
$body = @{
  market = "es"
  dt_utc = "2026-07-07T13:00:12Z"
  price = 7540.50
  volume = 1
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8765/tick -Method POST -Body $body -ContentType "application/json"
```

Market mapping:

```text
es -> USA500IDXUSD
nq -> USATECHIDXUSD
cl -> LIGHTCMDUSD
```

Only use this for the Lucid bot if the producer is truly sending those exact
Dukascopy instruments. Sending CME/TradingView/Yahoo prices through this bridge
would make the source label dishonest and invalidate the 36/36 backtest claim.

## JForex Producer

`jforex\LucidBridgeStrategy.java` is the matching producer scaffold for
Dukascopy/JForex. It subscribes to the exact three backtest proxy instruments
and posts each tick to `http://127.0.0.1:8765/tick`.

Run order:

```powershell
$env:LUCID_BRIDGE_TOKEN = "choose-a-secret"
$env:LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = "dukascopy_tick_proxy"
python tools\lucid_bridge_receiver.py
```

Then start `LucidBridgeStrategy.java` inside JForex/JForex SDK with the same
token as JVM property if you enabled one:

```text
-Dlucid.bridge.token=choose-a-secret
```

Keep the website on `LUCID_LIVE_SOURCE=dukascopy` until
`python tools\validate_lucid_bridge.py` prints `READY`. After that, restart the
website with:

```powershell
$env:LUCID_LIVE_SOURCE = "local_bridge"
$env:LUCID_LOCAL_BRIDGE_SOURCE_FAMILY = "dukascopy_tick_proxy"
```
