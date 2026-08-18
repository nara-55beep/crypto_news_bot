# Project analysis

Recorded before any implementation, from direct inspection of the repository.

## Stack

| Layer | Finding |
| --- | --- |
| Backend | Python 3.14, `aiohttp` web server (`dashboard.py`, `main.py`) |
| Frontend | Server-rendered inline HTML + vanilla JS. No React/Vue/bundler. Pages are Python string constants served by aiohttp handlers. |
| Routing | `dashboard.py::routes()` returns a list of `web.get/post` route defs. Feature packages contribute via `*package.routes()` (see `lucid_lab_web.routes()`). |
| State | Per-bot JSON state files under `data/`, written atomically (`tmp` + `os.replace` + `fsync`). No database. |
| Data (crypto) | `market_data.py` — Binance public REST. |
| Data (stocks) | `stock_data.py` — Alpaca IEX (needs key); `yfinance` in `requirements.txt` for historical OHLCV (no key). |
| Historical cache | `research/ta_strat/cache/*.csv` — 1-minute bars, gitignored. |
| Paper engine | Per-bot bespoke engines (`pennystock_paper.py`, `lucid_pass_paper.py`, ...). `lucid_lab/engine.py` is the newest and most rigorous: `Decimal` money, conservative fills, explicit cost model. |
| Worker/queue | No external queue. `asyncio` tasks; `lucid_lab/service.py::SimulationRegistry` is the in-process cancellable job pattern. |
| Auth | None on the local dashboard. |
| Tests | `pytest` + `unittest`, in `tests/`. Run with `venv\Scripts\python.exe -m pytest`. |
| CI | No `.github/workflows` present. |
| Deploy | Local: `run.bat` / `main.py`. |
| Remote | `origin` -> `github.com/nara-55beep/crypto_news_bot`, default branch `main`. |

## Paper Trading page (the visual reference)

- Route: `web.get("/paper", _paper_page)` in `dashboard.py`.
- Markup: `PAPER_HTML` string constant, `dashboard.py:3278`.
- Served with `Cache-Control: no-cache` so a refresh always gets fresh markup.

### Design tokens (second `:root` block wins — "professional dark trading-terminal skin")

```
--bg:#05070a  --panel:#0a0d12  --panel2:#10151d  --line:#1d2633  --line2:#2b3748
--txt:#edf3fb --muted:#7c8798  --amber:#f2b84b   --green:#19c37d --red:#ff4d5f --blue:#3aa0ff
--bin:'IBM Plex Mono'  --sans:'IBM Plex Sans'
```

### Components reused by the strategy library

`#topbar` / `.ticker` / `.nav` / `.spacer` / `.sub`, `#wrap` grid, `.bot` panel, `.bhead` / `.bname`,
`.dot` (`.on/.off/.watch`), `.badge` (`.live`), `.btn` (`.on/.off/.reset`), `select`,
`.stats` / `.stat` / `.k` / `.v`, `.pos` / `.neg`, `.ph` section header, `.posrow` / `.top` / `.det`,
`.side` (`.long/.short`), `.empty`, `.feed` / `.ln` / `.lt`, `.hist`.

Responsive: `#wrap` is `repeat(3,minmax(300px,1fr))`, dropping to 2 columns at 1320px and 1 column at 900px.

## Architecture decision

Follow the `lucid_lab/` package pattern, which is the newest and cleanest feature architecture here:

```
strategy_lab/
  schema.py      dataclasses + validation
  indicators.py  pure indicator functions over pandas frames
  catalog.py     the registry (curated + generated + academic)
  engine.py      one backtest/paper engine for every strategy
  runner.py      batch runner: concurrency, isolation, cancel, cache
  service.py     framework-agnostic application service
  page.py        UI, reusing the Paper Trading design tokens verbatim
  web.py         aiohttp routes, mounted from dashboard.routes()
```

This keeps `dashboard.py` (8.8k lines) untouched apart from one route mount and one nav link.
