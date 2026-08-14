# Market data sources

## Evidence data

The local research cache contains Dukascopy one-minute proxy OHLCV histories:

| Strategy market | Proxy symbol | Local files used | Approximate coverage | Resolution |
|---|---|---|---|---|
| ES / MES | `USA500IDXUSD` | `es_1m_10y.csv`, `es_1m_3y.csv` | 2016–2026 | 1 minute |
| NQ / MNQ | `USATECHIDXUSD` | `nq_1m_10y.csv`, `nq_1m_3y.csv`, `nq_1m_rth_repair.csv` | 2016–2026 | 1 minute |
| CL / MCL candidates | `LIGHTCMDUSD` | `cl_1m_10y.csv`, `cl_1m_3y.csv` | 2016–2026 | 1 minute |

The evidence generator records the exact first/last accepted timestamp, accepted sessions, missing-session handling, file sizes and SHA-256 hashes in `research/ta_strat/results/lucid_lab_validation.json`. Raw files remain ignored and are not copied or committed.

## Chronological split

- Development: first available session through 2021-12-31.
- Validation: 2022-01-01 through 2023-12-31.
- Test: 2024-01-01 through the last accepted local session.

The final test end date is never moved to avoid a weak result. Previous research has already inspected this test period, so the report calls it chronological test/confirmatory evidence rather than a pristine untouched test.

## Validation rules

The import validator requires `timestamp/dt_utc`, open, high, low, close and volume; parseable explicitly zoned timestamps (including mixed DST offsets normalized to UTC); ascending unique rows; positive prices; nonnegative volume; low ≤ open/close ≤ high; an expected symbol; recognized resolution; and explicit timezone. It reports zero-volume rows, missing minutes, session gaps, rows outside the conservative Sunday-evening-through-Friday-close futures session, incomplete 390-minute RTH sessions, invalid or expired contracts, and contract rollover transitions. Friday-evening and Saturday rows are rejected. Row-indexed strategy logic admits only a complete 09:30–15:59 New York sequence (390 consecutive minutes). Short/early-close and damaged sessions remain no-trade calendar days where observable and never become shortened “easy” pass windows.

CSV is supported directly. Parquet is accepted when pandas has a working parquet engine; otherwise the API returns an explicit dependency error.

## Limitations

- The proxies are **not CME futures**, not contract-level tapes and not order books.
- Volume may be broker/proxy volume rather than consolidated CME volume.
- Continuous symbols can hide real expiration/roll basis and liquidity changes.
- Historical bid/ask, queue position, partial fills and rejected orders are unavailable.
- DST is handled with `America/New_York`; exchange holidays/early closes are inferred conservatively from incomplete sessions rather than asserted as full RTH.
- These limitations prevent a `VALIDATED` label even if point estimates look attractive.

Synthetic fixtures are used only in unit tests and are never included in the displayed strategy evidence.
