# Lucid Strategy Lab — project brief

Status: implemented and verified
Repository audit: 2026-08-14

## Objective

Add a dedicated `/lucid-lab` research page that compares current Lucid evaluation rules, explains one fully mechanical strategy candidate, calculates rule-aware position size, and displays honest chronological historical evidence and stresses. The Lab is research and decision support. It does not place orders, enable either legacy Lucid paper bot, or promise that an evaluation will pass.

## Existing architecture

| Concern | Existing project | Lab decision |
|---|---|---|
| Language | Python 3 plus browser JavaScript | Keep both |
| Backend | `aiohttp.web` in `dashboard.py` | Register native handlers and JSON APIs |
| Frontend | Server-rendered inline HTML/CSS/vanilla JS | Add a self-contained page module; no new framework |
| Routing | Explicit `app.add_routes` table | Add `/lucid-lab` and `/api/lucid-lab/*` |
| Homepage/navigation | `PAGE_HTML` top bar in `dashboard.py` | Add a visible Lucid Strategy Lab link |
| Shared layout | Each major page owns HTML and styles while using the same dark palette, mono typography, cards, borders and nav controls | Match those conventions without copying the whole dashboard |
| Component library | None | Do not add one |
| Charts | Lightweight Charts CDN on the main chart; several pages also use plain DOM/CSS | Use dependency-free accessible SVG/HTML charts so the Lab works offline |
| Client state | Plain JavaScript objects and `fetch` | Keep a small page-local state store |
| Validation | Hand-written Python/JS validation | Validate in both API and browser; backend remains authoritative |
| API | JSON handlers under `/api/*` | Add versioned read-only snapshot and calculator endpoints |
| Database | SQLite is used for news/journal data; paper bots use JSON state | Lab reads a tracked immutable validation artifact; no database migration |
| Authentication | Local dashboard has no application authentication | Add no auth or credential handling |
| Tests | `unittest`, including async test cases and source integration checks | Add deterministic engine, API and UI contract tests |
| Formatting/type/build | No configured formatter, linter, type checker or JavaScript build | Run compile checks, the full unittest suite and application startup smoke tests; record unavailable categories honestly |
| Deployment | Local Windows launchers run `main.py`; dashboard chooses a local port | Keep startup unchanged and make direct route refresh work |
| Responsive layout | Existing page breakpoints cluster near 1320, 1100/1050, 980/900 px; reduced-motion is honored in animated entry points | Lab uses 1120, 820 and 560 px breakpoints and honors reduced motion |
| Accessibility | Semantic buttons/links are mixed with custom controls; focus treatment varies | Use labels, tables, keyboard-operable controls, visible focus, chart summaries and reduced motion |

## Existing Lucid work

The repository already contains two disabled/blocked paper ledgers and substantial causal research in `research/ta_strat/`. The original attractive 98% pass result was invalidated because it used non-causal information and optimistic fills. The causal rebuild uses completed bars, next-minute entries, adverse entry/exit ticks, stop-first ambiguous-bar handling, gap-worse stops, integer micros, commissions, a shared contract cap, the soft daily loss stop and the EOD trailing maximum-loss limit.

The Lab does not revive the invalidated strategy or carry its P&L. It exposes the strongest surviving research candidate as **experimental / not independently validated** and keeps the old trading switches unchanged.

## Separation of concerns

- `lucid_lab/rules.py`: official-source metadata and typed rule selection.
- `lucid_lab/engine.py`: decimal account state machine, execution assumptions, sizing and data validation.
- `lucid_lab/service.py`: validation-artifact loading, request validation and reproducible run bookkeeping.
- `lucid_lab/page.py`: HTML/CSS/JavaScript only.
- `research/ta_strat/lucid_lab_validation.py`: offline historical evidence generator.
- `research/ta_strat/results/lucid_lab_validation.json`: immutable, source-stamped display artifact.

Expensive historical research remains offline. Browser interactions only select verified configurations, recalculate risk, and render precomputed evidence.

## Safety and scope

- No live-trading endpoint or credential is introduced.
- The existing Lucid strategy version and paper-bot behavior are not changed.
- Raw market files remain ignored and local.
- Synthetic data are allowed only in deterministic tests.
- A result can be labelled `VALIDATED` only if every documented gate passes. The current candidate does not pass that label because the market source is a CFD/index proxy rather than CME order-book data and the newest test period has been inspected repeatedly during prior research.
