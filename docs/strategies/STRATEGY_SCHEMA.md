# Strategy schema

Defined in `strategy_lab/schema.py`. A record is data; the executable behaviour
lives in a rule engine referenced by `rule_id`. That split is what lets the catalog
hold a thousand entries without a thousand components.

## Fields

| Field | Type | Notes |
| --- | --- | --- |
| `id` | str | `category.family.variation`, lower-kebab, dot-separated. Enforced by regex. |
| `canonical_name` / `display_name` | str | one canonical name; the display name adds the variation. |
| `aliases` | tuple[str] | alternative names; resolved to the same record by search. |
| `category` / `subcategory` | str | taxonomy position. |
| `parent_id` / `variation_of` | str | links a variation to its family. |
| `description` / `thesis` | str | what it does; why it might work. Original wording. |
| `direction` | enum | `long`, `short`, `long-short`, `market-neutral`. |
| `timeframes` / `holding_period` | tuple / str | |
| `data_requirements` | tuple[str] | everything the strategy needs. |
| `external_data_requirements` | tuple[str] | the subset this project does **not** have. |
| `indicator_requirements` | tuple[str] | |
| `entry_rules` / `exit_rules` / `stop_rules` | tuple[str] | must be measurable when executable. |
| `position_sizing_rules` / `risk_rules` / `no_trade_rules` | tuple[str] | |
| `parameters` / `default_parameters` | tuple / dict | typed, bounded, validated. |
| `sources` | tuple[StrategySource] | title, url, author, year, primary/secondary/index. |
| `evidence_level` | enum | `academic`, `institutional`, `historical`, `community`, `experimental`. |
| `implementation_status` | enum | `executable`, `requires-data`, `research-only`, `unsupported`. |
| `unsupported_reason` | str | required when unsupported. |
| `systematic_interpretation` | bool | true when a discretionary original was reduced to fixed rules. |
| `limitations` | tuple[str] | shown on the detail panel. |
| `complexity` / `trade_frequency` / `market_regime` | enum / tuple | filter facets. |
| `rule_id` | str | the engine that runs it; required when executable. |
| `version` | str | |

## Validation rules

Enforced at import; a malformed record raises rather than rendering broken.

1. `id` must match `^[a-z0-9-]+(\.[a-z0-9-]+)+$`.
2. Required text fields must be non-empty; the description must exceed five words.
3. At least one timeframe and one data requirement.
4. **Executable** records must carry a `rule_id`, entry rules and exit rules.
5. **Executable** rules may not contain vague prose. The validator rejects
   "strong momentum", "clean setup", "near resistance", "tight stop", "looks
   bullish" and similar outright.
6. **Unsupported** records must state `unsupported_reason`.
7. **requires-data** records must list `external_data_requirements`.
8. Every default parameter must exist and satisfy its own bounds.

## Systematic interpretation

Where an original is discretionary — candlestick reading is the clearest case — the
catalog ships a fixed geometric approximation, flags `systematic_interpretation`, and
says on the detail panel that it will not match any particular author's reading. The
alternative, silently presenting an interpretation as the original, is the thing worth
avoiding.
