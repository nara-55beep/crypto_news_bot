"""Central, source-traceable Lucid rule configuration.

The values here are deliberately separate from strategy and UI code.  Rules are
selected by program, stage and account size; a funded rule can never silently be
used for an evaluation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar


RULES_LAST_CHECKED = "2026-08-14"
Confidence = Literal["verified", "ambiguous", "estimated", "configurable"]
T = TypeVar("T")


@dataclass(frozen=True)
class RuleSource:
    title: str
    url: str
    displayed_date: str
    retrieved_at: str = RULES_LAST_CHECKED

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class VerifiedRule(Generic[T]):
    value: T
    unit: str
    program: str
    stage: str
    source: RuleSource
    confidence: Confidence = "verified"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: Any = self.value
        if isinstance(value, Decimal):
            value = str(value)
        return {
            "value": value,
            "unit": self.unit,
            "program": self.program,
            "stage": self.stage,
            "source": self.source.to_dict(),
            "confidence": self.confidence,
            "notes": self.notes,
        }


SOURCES = {
    "pro_eval": RuleSource(
        "LucidPro Evaluation Account",
        "https://support.lucidtrading.com/en/articles/12890029-lucidpro-evaluation-account",
        "2025-11-20",
    ),
    "pro_drawdown": RuleSource(
        "LucidPro Drawdown",
        "https://support.lucidtrading.com/en/articles/12890136-lucidpro-drawdown",
        "2025-11-20",
    ),
    "pro_dll": RuleSource(
        "LucidPro Daily Loss Limit",
        "https://support.lucidtrading.com/en/articles/12890122-lucidpro-daily-loss-limit",
        "updated over 2 weeks before access",
    ),
    "flex_eval": RuleSource(
        "LucidFlex Evaluation Account",
        "https://support.lucidtrading.com/en/articles/12945790-lucidflex-evaluation-account",
        "2026-04-15",
    ),
    "flex_consistency": RuleSource(
        "LucidFlex Consistency Percentage",
        "https://support.lucidtrading.com/en/articles/12945805-lucidflex-consistency-percentage",
        "2026-06-29",
    ),
    "flex_drawdown": RuleSource(
        "LucidFlex Drawdown",
        "https://support.lucidtrading.com/en/articles/12945815-lucidflex-drawdown",
        "2025-11-26",
    ),
    "black_eval": RuleSource(
        "LucidBlack Evaluation Account",
        "https://support.lucidtrading.com/en/articles/13424894-lucidblack-evaluation-account",
        "2026-01-18",
    ),
    "black_consistency": RuleSource(
        "LucidBlack Consistency Percentage",
        "https://support.lucidtrading.com/en/articles/13424900-lucidblack-consistency-percentage",
        "2026-06-29",
    ),
    "black_drawdown": RuleSource(
        "LucidBlack Drawdown",
        "https://support.lucidtrading.com/en/articles/13424906-lucidblack-drawdown",
        "2026-01-18",
    ),
    "daily_eval": RuleSource(
        "LucidDaily Evaluation",
        "https://support.lucidtrading.com/en/articles/15996664-luciddaily-evaluation",
        "updated over 2 weeks before access",
    ),
    "daily_custom": RuleSource(
        "LucidDaily Customization",
        "https://support.lucidtrading.com/en/articles/16033858-luciddaily-customization",
        "updated week of access",
    ),
    "daily_consistency": RuleSource(
        "LucidDaily Consistency",
        "https://support.lucidtrading.com/en/articles/15998336-luciddaily-consistency",
        "updated week of access",
    ),
    "daily_drawdown": RuleSource(
        "LucidDaily Drawdown",
        "https://support.lucidtrading.com/en/articles/15998425-luciddaily-drawdown",
        "updated week of access",
    ),
    "daily_dll": RuleSource(
        "LucidDaily Daily Loss Limit",
        "https://support.lucidtrading.com/en/articles/16085900-luciddaily-daily-loss-limit",
        "updated day of access",
    ),
    "products": RuleSource(
        "Approved Products and Commissions",
        "https://support.lucidtrading.com/en/articles/11508978-approved-products-and-commissions",
        "2026-02-09",
    ),
    "times": RuleSource(
        "Allowed Trading Times",
        "https://support.lucidtrading.com/en/articles/11404729-allowed-trading-times",
        "updated over 2 weeks before access",
    ),
    "activities": RuleSource(
        "Other Activities",
        "https://support.lucidtrading.com/en/articles/11404728-other-activities",
        "updated 2026-08-13 relative to access",
    ),
    "microscalping": RuleSource(
        "Prohibited: Microscalping",
        "https://support.lucidtrading.com/en/articles/11404742-prohibited-microscalping",
        "2025-11-20",
    ),
    "hedging": RuleSource(
        "Prohibited: Hedging",
        "https://support.lucidtrading.com/en/articles/11404734-prohibited-hedging",
        "2026-03-03",
    ),
    "hft": RuleSource(
        "Prohibited: High Frequency Trading",
        "https://support.lucidtrading.com/en/articles/11404736-prohibited-high-frequency-trading",
        "2025-06-13",
    ),
}


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    exchange: str
    kind: Literal["micro", "mini"]
    tick_size: Decimal
    point_value: Decimal
    commission_per_side: Decimal
    cap_units: int
    source: RuleSource = SOURCES["products"]

    @property
    def tick_value(self) -> Decimal:
        return self.tick_size * self.point_value

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "kind": self.kind,
            "tick_size": str(self.tick_size),
            "point_value": str(self.point_value),
            "tick_value": str(self.tick_value),
            "commission_per_side": str(self.commission_per_side),
            "cap_units": self.cap_units,
            "source": self.source.to_dict(),
        }


INSTRUMENTS: dict[str, Instrument] = {
    "MES": Instrument("MES", "Micro E-mini S&P 500", "CME", "micro", Decimal("0.25"), Decimal("5"), Decimal("0.50"), 1),
    "MNQ": Instrument("MNQ", "Micro E-mini Nasdaq-100", "CME", "micro", Decimal("0.25"), Decimal("2"), Decimal("0.50"), 1),
    "MCL": Instrument("MCL", "Micro Crude Oil", "NYMEX", "micro", Decimal("0.01"), Decimal("100"), Decimal("0.50"), 1),
    "ES": Instrument("ES", "E-mini S&P 500", "CME", "mini", Decimal("0.25"), Decimal("50"), Decimal("1.75"), 10),
    "NQ": Instrument("NQ", "E-mini Nasdaq-100", "CME", "mini", Decimal("0.25"), Decimal("20"), Decimal("1.75"), 10),
    "CL": Instrument("CL", "Crude Oil Futures", "NYMEX", "mini", Decimal("0.01"), Decimal("1000"), Decimal("2.00"), 10),
}


@dataclass(frozen=True)
class AccountRules:
    program_id: str
    program_label: str
    stage: Literal["evaluation", "funded", "live"]
    account_size: int
    starting_balance: Decimal
    profit_target: Decimal | None
    max_loss: Decimal
    drawdown_type: Literal["eod", "intraday"]
    daily_loss_limit: Decimal | None
    consistency_limit_pct: Decimal | None
    minimum_trading_days: int
    max_minis: int
    max_micros: int
    trail_trigger: Decimal
    locked_floor: Decimal
    forced_close_ny: str
    overnight_allowed: bool
    weekend_allowed: bool
    news_rule: str
    evidence_compatible: bool
    source_keys: tuple[str, ...]
    conflicts: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "starting_balance", "profit_target", "max_loss",
            "daily_loss_limit", "consistency_limit_pct", "trail_trigger",
            "locked_floor",
        ):
            value = data[key]
            data[key] = None if value is None else str(value)
        data["sources"] = [SOURCES[key].to_dict() for key in self.source_keys]
        data["target_to_drawdown"] = (
            None if self.profit_target is None
            else str((self.profit_target / self.max_loss).quantize(Decimal("0.01")))
        )
        return data


_SIZES = {
    25_000: (Decimal("1250"), Decimal("1000"), None, 2, 20),
    50_000: (Decimal("3000"), Decimal("2000"), Decimal("1200"), 4, 40),
    100_000: (Decimal("6000"), Decimal("3000"), Decimal("1800"), 6, 60),
    150_000: (Decimal("9000"), Decimal("4500"), Decimal("2700"), 10, 100),
}


def _base_values(size: int) -> tuple[Decimal, Decimal, Decimal | None, int, int]:
    try:
        return _SIZES[int(size)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unsupported Lucid account size: {size}") from exc


def get_account_rules(
    program: str,
    stage: str,
    size: int,
    *,
    daily_drawdown: str = "eod",
    daily_loss_enabled: bool = True,
) -> AccountRules:
    """Return one exact program/stage configuration or raise ``ValueError``."""
    program = str(program).strip().lower()
    stage = str(stage).strip().lower()
    size = int(size)
    if stage != "evaluation":
        raise ValueError("the Lab pass simulator currently supports evaluation stage only")
    target, max_loss, pro_dll, max_minis, max_micros = _base_values(size)
    start = Decimal(size)
    trail_trigger = start + max_loss + Decimal("100")
    locked_floor = start + Decimal("100")
    conflicts: list[str] = []

    if program == "lucidpro":
        label, consistency, dll, drawdown = "LucidPro", None, pro_dll, "eod"
        source_keys = ("pro_eval", "pro_drawdown", "pro_dll", "times", "products")
        news = "Allowed; strategy still filters scheduled high-impact releases for execution risk."
    elif program == "lucidflex":
        label, consistency, dll, drawdown = "LucidFlex", Decimal("50"), None, "eod"
        source_keys = ("flex_eval", "flex_consistency", "flex_drawdown", "times", "products")
        news = "Allowed; strategy still filters scheduled high-impact releases for execution risk."
    elif program == "lucidblack":
        if size == 150_000:
            raise ValueError("LucidBlack evaluation has no verified 150K option")
        label, consistency, dll, drawdown = "LucidBlack", Decimal("60"), None, "eod"
        source_keys = ("black_eval", "black_consistency", "black_drawdown", "products")
        news = "Official general article does not name Black; Lab blocks high-impact windows."
        conflicts.append("The official cutoff/news articles do not explicitly name LucidBlack.")
    elif program == "luciddaily":
        if daily_drawdown not in {"eod", "intraday"}:
            raise ValueError("LucidDaily drawdown must be 'eod' or 'intraday'")
        label, consistency = "LucidDaily", Decimal("50")
        dll = pro_dll if daily_loss_enabled else None
        drawdown = daily_drawdown
        source_keys = (
            "daily_eval", "daily_custom", "daily_consistency",
            "daily_drawdown", "daily_dll", "products",
        )
        news = "USD high-impact red-folder window blocked from 1 minute before through 1 minute after."
        conflicts.append("The general cutoff page does not explicitly name LucidDaily.")
    else:
        raise ValueError(f"unsupported public evaluation program: {program}")

    return AccountRules(
        program_id=program,
        program_label=label,
        stage="evaluation",
        account_size=size,
        starting_balance=start,
        profit_target=target,
        max_loss=max_loss,
        drawdown_type=drawdown,  # type: ignore[arg-type]
        daily_loss_limit=dll,
        consistency_limit_pct=consistency,
        minimum_trading_days=1,
        max_minis=max_minis,
        max_micros=max_micros,
        trail_trigger=trail_trigger,
        locked_floor=locked_floor,
        forced_close_ny="16:45",
        overnight_allowed=False,
        weekend_allowed=False,
        news_rule=news,
        evidence_compatible=(program == "lucidpro" and drawdown == "eod"),
        source_keys=source_keys,
        conflicts=tuple(conflicts),
    )


def public_evaluation_options() -> list[dict[str, Any]]:
    programs = (
        ("lucidpro", "LucidPro", (25_000, 50_000, 100_000, 150_000)),
        ("lucidflex", "LucidFlex", (25_000, 50_000, 100_000, 150_000)),
        ("lucidblack", "LucidBlack", (25_000, 50_000, 100_000)),
        ("luciddaily", "LucidDaily", (25_000, 50_000, 100_000, 150_000)),
    )
    return [
        {"id": program, "label": label, "stages": ["evaluation"], "sizes": list(sizes)}
        for program, label, sizes in programs
    ]


def official_sources() -> list[dict[str, str]]:
    seen: set[str] = set()
    result = []
    for source in SOURCES.values():
        if source.url not in seen:
            result.append(source.to_dict())
            seen.add(source.url)
    return result
