# Execution assumptions

The historical source is one-minute OHLCV from Dukascopy CFD/index proxies. It has neither CME queue position nor historical bid/ask. Therefore fills are conservative deterministic bar assumptions—not broker receipts.

## Normal model

- A signal is formed only from a completed bar.
- Entry occurs at the following one-minute open.
- One adverse tick is charged at entry and one at exit.
- Those two adverse ticks are decomposed for reporting as one round-trip tick of spread and one round-trip tick of slippage. This is a model classification, not an observed book.
- Stop/target evaluation begins at the entry minute.
- If stop and target are both inside the same one-minute bar, stop wins.
- A gap through a stop fills at the worse of stop or bar open, plus the modeled adverse exit tick.
- Target fills are not improved beyond the target and pay the exit tick.
- Orders that cannot fit one whole micro after commission and stop risk are rejected.
- The aggregate evaluation cap is enforced using ten micros per mini.
- All positions close by the last 15:59 RTH bar. This is earlier than Lucid's 16:45 ET cutoff.
- MES, MNQ and MCL commission is $0.50 per side per contract, $1.00 round turn.

## Categories

| Input | Value | Category |
|---|---:|---|
| MES/MNQ/MCL commission | $0.50 per side | Officially verified |
| Tick size / point value | MES 0.25/$5; MNQ 0.25/$2; MCL 0.01/$100 | Contract specification; user-visible model constant |
| Base spread | 1 tick round trip | Conservative assumption because no bid/ask history |
| Base slippage | 1 tick round trip | Conservative assumption |
| Signal latency | next one-minute open | Conservative assumption / data-resolution constraint |
| Limit fill | not used by selected strategy | Fail closed |
| Partial fill | unsupported by one-minute OHLC; full quantity is capped and treated as marketable | Limitation; stresses include missed trades |
| Stop gaps | worse of stop or next bar open | Market-derived conservative rule |
| Rollover | continuous proxy series; no CME roll receipt | Known data limitation |

## Presets

Normal is the historical base. Other presets add costs relative to normal:

| Preset | Additional spread | Additional slippage | Other change |
|---|---:|---:|---|
| spread +50% | 0.5 tick RT | 0 | — |
| spread doubled | 1 tick RT | 0 | — |
| slippage +50% | 0 | 0.5 tick RT | — |
| slippage doubled | 0 | 1 tick RT | — |
| volatile open | 1 tick RT | 2 ticks RT | no entries in first 15 minutes beyond selected fixed-time rules |
| low liquidity | 1 tick RT | 1 tick RT | deterministic 10% missed entries |
| delayed stop | 0 | 0 | +1 tick on stop exits |
| gap event | 0 | 0 | historical gaps plus 4 additional ticks on stop exits |
| missed trades | 0 | 0 | deterministic 10% of signals skipped using fixed seed |
| combined severe | 1 tick RT | 2 ticks RT | +4 stop ticks, one-minute conceptual delay, 20% missed |

The UI labels all spread/slippage inputs as estimated/configurable. It never describes them as Lucid rules.
