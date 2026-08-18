"""Structured definition and validation for every catalogued strategy.

A strategy record is data, not code.  The executable part is a separate small
callable looked up by ``rule_id``; this keeps the catalog serialisable, testable
and cheap to filter, and stops several hundred definitions from turning into
several hundred bespoke components.

Validation is deliberately strict.  A malformed record is rejected at import
time rather than rendering as a broken card, and vague prose is rejected outright
because the brief requires deterministic rules.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Direction = Literal["long", "short", "long-short", "market-neutral"]
EvidenceLevel = Literal["academic", "institutional", "historical", "community", "experimental"]
ImplementationStatus = Literal["executable", "requires-data", "research-only", "unsupported"]

ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+$")

# Prose that hides an undefined decision.  An executable strategy may not use
# these; a research-only record may, because it is explicitly not runnable.
VAGUE_PHRASES = (
    "looks bullish", "looks bearish", "strong momentum", "weak market",
    "clean setup", "important level", "reasonable risk", "good volume",
    "obvious trend", "tight stop", "high-quality setup", "near resistance",
    "near support", "if it feels", "use discretion",
)


class StrategyValidationError(ValueError):
    """Raised when a catalog record cannot be trusted to render or run."""


@dataclass(frozen=True)
class StrategySource:
    title: str
    url: str = ""
    author: str = ""
    year: int | None = None
    kind: Literal["primary", "secondary", "index"] = "primary"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyParameter:
    name: str
    label: str
    kind: Literal["int", "float", "bool", "choice"]
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    def validate_value(self, value: Any) -> Any:
        if self.kind == "bool":
            return bool(value)
        if self.kind == "choice":
            text = str(value)
            if self.choices and text not in self.choices:
                raise StrategyValidationError(
                    f"{self.name}: {text!r} is not one of {list(self.choices)}"
                )
            return text
        number = int(value) if self.kind == "int" else float(value)
        if self.minimum is not None and number < self.minimum:
            raise StrategyValidationError(f"{self.name}: {number} is below {self.minimum}")
        if self.maximum is not None and number > self.maximum:
            raise StrategyValidationError(f"{self.name}: {number} is above {self.maximum}")
        return number

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["choices"] = list(self.choices)
        return data


@dataclass(frozen=True)
class TradingStrategy:
    id: str
    canonical_name: str
    display_name: str
    category: str
    subcategory: str
    description: str
    thesis: str
    direction: Direction
    timeframes: tuple[str, ...]
    holding_period: str
    data_requirements: tuple[str, ...]
    entry_rules: tuple[str, ...]
    exit_rules: tuple[str, ...]
    evidence_level: EvidenceLevel
    implementation_status: ImplementationStatus
    aliases: tuple[str, ...] = ()
    parent_id: str | None = None
    variation_of: str | None = None
    origin_creator: str = ""
    origin_year: int | None = None
    origin_notes: str = ""
    instrument_types: tuple[str, ...] = ("equity",)
    supported_markets: tuple[str, ...] = ("US equities",)
    indicator_requirements: tuple[str, ...] = ()
    external_data_requirements: tuple[str, ...] = ()
    stop_rules: tuple[str, ...] = ()
    position_sizing_rules: tuple[str, ...] = ()
    risk_rules: tuple[str, ...] = ()
    no_trade_rules: tuple[str, ...] = ()
    parameters: tuple[StrategyParameter, ...] = ()
    default_parameters: dict[str, Any] = field(default_factory=dict)
    sources: tuple[StrategySource, ...] = ()
    unsupported_reason: str = ""
    limitations: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    rule_id: str = ""
    systematic_interpretation: bool = False
    complexity: Literal["low", "medium", "high"] = "medium"
    trade_frequency: Literal["very-low", "low", "medium", "high", "very-high"] = "medium"
    market_regime: tuple[str, ...] = ()
    version: str = "1.0.0"

    # ---------------------------------------------------------------- helpers
    @property
    def is_executable(self) -> bool:
        return self.implementation_status == "executable"

    def search_text(self) -> str:
        return " ".join([
            self.id, self.canonical_name, self.display_name, self.category,
            self.subcategory, self.description, self.origin_creator,
            " ".join(self.aliases), " ".join(self.tags),
            " ".join(self.indicator_requirements),
        ]).lower()

    def summary_dict(self) -> dict[str, Any]:
        """The compact shape the list view needs; keeps payloads small."""
        return {
            "id": self.id,
            "name": self.display_name,
            "aliases": list(self.aliases),
            "category": self.category,
            "subcategory": self.subcategory,
            "direction": self.direction,
            "timeframes": list(self.timeframes),
            "holding_period": self.holding_period,
            "evidence_level": self.evidence_level,
            "implementation_status": self.implementation_status,
            "data_requirements": list(self.data_requirements),
            "external_data_requirements": list(self.external_data_requirements),
            "complexity": self.complexity,
            "trade_frequency": self.trade_frequency,
            "origin_year": self.origin_year,
            "tags": list(self.tags),
            "systematic_interpretation": self.systematic_interpretation,
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, tuple):
                data[key] = list(value)
        data["parameters"] = [p.to_dict() for p in self.parameters]
        data["sources"] = [s.to_dict() for s in self.sources]
        data["is_executable"] = self.is_executable
        return data


REQUIRED_TEXT = ("canonical_name", "display_name", "category", "subcategory",
                 "description", "thesis", "holding_period")


def validate(strategy: TradingStrategy) -> None:
    """Raise ``StrategyValidationError`` if a record cannot be trusted."""
    if not ID_PATTERN.match(strategy.id):
        raise StrategyValidationError(
            f"{strategy.id!r} must be dotted lower-kebab, e.g. 'trend.ma-crossover.sma-50-200'"
        )
    for name in REQUIRED_TEXT:
        if not str(getattr(strategy, name)).strip():
            raise StrategyValidationError(f"{strategy.id}: {name} is empty")
    if len(strategy.description.split()) < 6:
        raise StrategyValidationError(f"{strategy.id}: description is too short to be useful")
    if not strategy.timeframes:
        raise StrategyValidationError(f"{strategy.id}: at least one timeframe is required")
    if not strategy.data_requirements:
        raise StrategyValidationError(f"{strategy.id}: data requirements must be declared")

    if strategy.implementation_status == "executable":
        if not strategy.rule_id:
            raise StrategyValidationError(f"{strategy.id}: executable strategies need a rule_id")
        if not strategy.entry_rules or not strategy.exit_rules:
            raise StrategyValidationError(
                f"{strategy.id}: executable strategies need explicit entry and exit rules"
            )
        blob = " ".join(strategy.entry_rules + strategy.exit_rules + strategy.stop_rules).lower()
        for phrase in VAGUE_PHRASES:
            if phrase in blob:
                raise StrategyValidationError(
                    f"{strategy.id}: executable rules must be measurable, found {phrase!r}"
                )
    if strategy.implementation_status == "unsupported" and not strategy.unsupported_reason:
        raise StrategyValidationError(f"{strategy.id}: unsupported strategies must say why")
    if strategy.implementation_status == "requires-data" and not strategy.external_data_requirements:
        raise StrategyValidationError(
            f"{strategy.id}: requires-data strategies must list the missing data"
        )

    names = {p.name for p in strategy.parameters}
    unknown = set(strategy.default_parameters) - names
    if unknown:
        raise StrategyValidationError(
            f"{strategy.id}: default_parameters has unknown keys {sorted(unknown)}"
        )
    for parameter in strategy.parameters:
        parameter.validate_value(strategy.default_parameters.get(parameter.name, parameter.default))


def resolve_parameters(
    strategy: TradingStrategy, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Merge user overrides onto defaults, validating every value."""
    values: dict[str, Any] = {}
    supplied = overrides or {}
    unknown = set(supplied) - {p.name for p in strategy.parameters}
    if unknown:
        raise StrategyValidationError(f"unknown parameter(s): {sorted(unknown)}")
    for parameter in strategy.parameters:
        raw = supplied.get(
            parameter.name, strategy.default_parameters.get(parameter.name, parameter.default)
        )
        values[parameter.name] = parameter.validate_value(raw)
    return values
