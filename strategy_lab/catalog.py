"""The strategy registry.

Records come from three places:

1. **Curated families** — named, publicly documented strategies expanded across
   variation axes that genuinely change behaviour (indicator, period, direction,
   confirmation, timeframe).  A variation only exists if it changes what the
   strategy *does*, not merely what it is called.
2. **Academic anomalies** — one record per published cross-sectional predictor,
   cited to its original paper.  Almost all need accounting, analyst or options
   data this project does not carry, so they are honestly ``requires-data``.
3. **Catalog-only families** — options structures, execution algorithms and
   alternative-data strategies, marked ``unsupported`` or ``requires-data`` with
   the exact reason, because pretending otherwise would be the whole problem.

Everything is built once at import and validated; a malformed record raises.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Iterable

from .schema import (
    StrategyParameter,
    StrategySource,
    TradingStrategy,
    validate,
)

DATA_DIR = Path(__file__).resolve().parent / "catalog_data"
RESEARCH_DATE = "2026-08-18"

OHLCV = ("daily OHLCV",)
P = StrategyParameter


def _src(title: str, url: str = "", author: str = "", year: int | None = None,
         kind: str = "primary") -> StrategySource:
    return StrategySource(title=title, url=url, author=author, year=year, kind=kind)  # type: ignore[arg-type]


# Shared risk parameters offered by most executable rule engines.
RISK_PARAMS = (
    P("atr_stop_multiple", "ATR stop multiple (0 = none)", "float", 0.0, 0.0, 20.0),
    P("take_profit_multiple", "Take profit (multiples of stop)", "float", 0.0, 0.0, 20.0),
    P("max_bars_held", "Maximum bars held (0 = none)", "int", 0, 0, 500),
)


def _strategy(**kwargs: Any) -> TradingStrategy:
    kwargs.setdefault("sources", ())
    for key in ("aliases", "timeframes", "data_requirements", "entry_rules", "exit_rules",
                "stop_rules", "position_sizing_rules", "risk_rules", "no_trade_rules",
                "indicator_requirements", "external_data_requirements", "limitations",
                "tags", "instrument_types", "supported_markets", "market_regime", "sources"):
        if key in kwargs and isinstance(kwargs[key], list):
            kwargs[key] = tuple(kwargs[key])
    if "parameters" in kwargs and isinstance(kwargs["parameters"], list):
        kwargs["parameters"] = tuple(kwargs["parameters"])
    return TradingStrategy(**kwargs)


# =========================================================================
# 1. Curated, executable families
# =========================================================================
MA_KINDS = {
    "sma": ("Simple", "arithmetic mean of closes"),
    "ema": ("Exponential", "exponentially weighted mean, more responsive to recent closes"),
    "wma": ("Weighted", "linearly weighted mean"),
    "hma": ("Hull", "Hull average, which reduces lag at the cost of overshoot"),
    "dema": ("Double exponential", "double-smoothed exponential average"),
    "tema": ("Triple exponential", "triple-smoothed exponential average"),
}

CROSSOVER_PAIRS = [
    (5, 20, "very short"), (9, 21, "very short"), (10, 30, "short"),
    (12, 26, "short"), (20, 50, "medium"), (21, 55, "medium"),
    (34, 89, "medium"), (50, 100, "long"), (50, 200, "long"), (100, 200, "very long"),
]


def _crossover_family() -> Iterable[TradingStrategy]:
    golden = _src("Golden cross / death cross", author="Widely documented market lore")
    for kind, (label, blurb) in MA_KINDS.items():
        for fast, slow, horizon in CROSSOVER_PAIRS:
            for allow_short in (False, True):
                direction = "long-short" if allow_short else "long"
                suffix = "ls" if allow_short else "long"
                name = f"{label} MA crossover {fast}/{slow}"
                is_golden = kind == "sma" and (fast, slow) == (50, 200)
                yield _strategy(
                    id=f"trend.ma-crossover.{kind}-{fast}-{slow}-{suffix}",
                    canonical_name=name,
                    display_name=f"{name} ({direction})",
                    aliases=(["Golden cross", "Death cross"] if is_golden else []) +
                            [f"{kind.upper()} {fast}/{slow} cross"],
                    category="Trend following",
                    subcategory="Moving-average crossover",
                    parent_id="trend.ma-crossover",
                    variation_of=f"{kind} {fast}/{slow}",
                    description=(
                        f"Holds a position while the {fast}-period {label.lower()} moving average "
                        f"sits above the {slow}-period one, using the {blurb}. "
                        + ("Reverses short when the relationship inverts." if allow_short
                           else "Moves to cash when the relationship inverts.")
                    ),
                    thesis=(
                        "Prices trend at intermediate horizons, so a faster average crossing a "
                        "slower one is a cheap, lagging proxy for a change in trend direction."
                    ),
                    direction=direction,
                    timeframes=["1d"],
                    holding_period=f"{horizon} term, weeks to months",
                    data_requirements=list(OHLCV),
                    indicator_requirements=[f"{kind.upper()}({fast})", f"{kind.upper()}({slow})"],
                    entry_rules=[
                        f"Go long on the next open after a close where {kind.upper()}({fast}) > {kind.upper()}({slow}).",
                    ] + ([f"Go short on the next open after a close where {kind.upper()}({fast}) < {kind.upper()}({slow})."]
                         if allow_short else []),
                    exit_rules=[
                        f"Exit long on the next open after {kind.upper()}({fast}) crosses back below {kind.upper()}({slow}).",
                    ] + ([f"Exit short on the next open after {kind.upper()}({fast}) crosses back above {kind.upper()}({slow})."]
                         if allow_short else ["Hold cash while the fast average is below the slow average."]),
                    stop_rules=["Optional ATR stop, disabled by default."],
                    position_sizing_rules=["Fixed fraction of equity, set on the run form."],
                    evidence_level="community" if not is_golden else "historical",
                    implementation_status="executable",
                    rule_id="trend.ma_crossover",
                    parameters=[
                        P("fast", "Fast length", "int", fast, 2, 400),
                        P("slow", "Slow length", "int", slow, 3, 500),
                        P("ma_kind", "Average type", "choice", kind, choices=tuple(MA_KINDS)),
                        P("allow_short", "Allow short side", "bool", allow_short),
                        *RISK_PARAMS,
                    ],
                    default_parameters={"fast": fast, "slow": slow, "ma_kind": kind,
                                        "allow_short": allow_short},
                    sources=[golden],
                    origin_year=1978 if is_golden else None,
                    origin_notes=("Popularised by technical analysts through the 20th century; "
                                  "the 50/200-day form is the best known."),
                    tags=["trend", "moving average", "crossover", kind],
                    complexity="low",
                    trade_frequency="low" if slow >= 100 else "medium",
                    market_regime=["trending"],
                    limitations=["Whipsaws badly in range-bound markets.",
                                 "Lags every turn by construction."],
                )


def _price_vs_ma_family() -> Iterable[TradingStrategy]:
    faber = _src("A Quantitative Approach to Tactical Asset Allocation",
                 "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461",
                 "Meb Faber", 2007)
    for kind in ("sma", "ema"):
        for length in (10, 20, 50, 100, 150, 200):
            for allow_short in (False, True):
                suffix = "ls" if allow_short else "long"
                famous = kind == "sma" and length == 200
                yield _strategy(
                    id=f"trend.price-vs-ma.{kind}-{length}-{suffix}",
                    canonical_name=f"Price versus {kind.upper()}({length})",
                    display_name=f"Price above {kind.upper()}({length})"
                                 + (" (long-short)" if allow_short else ""),
                    aliases=["200-day moving average rule"] if famous else [],
                    category="Trend following",
                    subcategory="Single moving-average filter",
                    parent_id="trend.price-vs-ma",
                    description=(
                        f"Holds the market while the close is above its {length}-period "
                        f"{kind.upper()}, and steps aside (or reverses) when it is below."
                    ),
                    thesis=("A single long average separates the periods when an index compounds "
                            "from the periods when it suffers its deepest drawdowns."),
                    direction="long-short" if allow_short else "long",
                    timeframes=["1d"],
                    holding_period="Weeks to months",
                    data_requirements=list(OHLCV),
                    indicator_requirements=[f"{kind.upper()}({length})"],
                    entry_rules=[f"Go long on the next open after a close above {kind.upper()}({length})."],
                    exit_rules=[f"Exit on the next open after a close below {kind.upper()}({length})."],
                    evidence_level="academic" if famous else "community",
                    implementation_status="executable",
                    rule_id="trend.price_vs_ma",
                    parameters=[
                        P("length", "Average length", "int", length, 2, 400),
                        P("ma_kind", "Average type", "choice", kind, choices=("sma", "ema", "wma", "hma")),
                        P("allow_short", "Allow short side", "bool", allow_short),
                        *RISK_PARAMS,
                    ],
                    default_parameters={"length": length, "ma_kind": kind, "allow_short": allow_short},
                    sources=[faber] if famous else [],
                    origin_year=2007 if famous else None,
                    tags=["trend", "timing", "filter"],
                    complexity="low",
                    trade_frequency="low",
                    market_regime=["trending", "bear"],
                    limitations=["Produces frequent false signals when price oscillates around the average."],
                )


def _oscillator_reversion_family() -> Iterable[TradingStrategy]:
    specs = [
        ("reversion.rsi", "rsi", "RSI", "reversion.rsi", "RSI",
         [(2, 10, 90), (2, 5, 95), (3, 15, 85), (14, 30, 70), (14, 20, 80), (7, 25, 75), (21, 35, 65)],
         _src("New Concepts in Technical Trading Systems", author="J. Welles Wilder Jr.", year=1978),
         "oversold", "overbought"),
        ("reversion.stochastic", "stochastic", "Stochastic", "reversion.stochastic", "Stochastic %K",
         [(14, 20, 80), (14, 10, 90), (5, 20, 80), (21, 25, 75)],
         _src("Stochastic oscillator", author="George Lane", year=1957), "oversold", "overbought"),
        ("reversion.williams-r", "williams-r", "Williams %R", "reversion.williams_r", "Williams %R",
         [(14, -80, -20), (14, -90, -10), (28, -80, -20)],
         _src("How I Made One Million Dollars Last Year Trading Commodities",
              author="Larry Williams", year=1973), "oversold", "overbought"),
        ("reversion.cci", "cci", "CCI", "reversion.cci", "CCI",
         [(20, -100, 100), (20, -200, 200), (14, -100, 100), (50, -100, 100)],
         _src("Commodity Channel Index", author="Donald Lambert", year=1980), "oversold", "overbought"),
        ("reversion.mfi", "mfi", "Money Flow Index", "reversion.mfi", "MFI",
         [(14, 20, 80), (14, 10, 90), (7, 20, 80)],
         _src("Money Flow Index", author="Gene Quong and Avrum Soudack"), "oversold", "overbought"),
    ]
    for base_id, slug, label, rule_id, indicator, combos, source, low_name, high_name in specs:
        for length, low, high in combos:
            for allow_short in (False, True):
                suffix = "ls" if allow_short else "long"
                yield _strategy(
                    id=f"{base_id}.{slug}-{length}-{abs(int(low))}-{abs(int(high))}-{suffix}",
                    canonical_name=f"{label}({length}) reversion {low}/{high}",
                    display_name=f"{label}({length}) {low}/{high} reversion"
                                 + (" (long-short)" if allow_short else ""),
                    aliases=[f"{label} {low_name}/{high_name} bounce"],
                    category="Mean reversion",
                    subcategory=f"{label} oscillator",
                    parent_id=base_id,
                    description=(
                        f"Buys after {indicator} falls below {low} and exits when the oscillator "
                        f"returns to its midpoint"
                        + (f", mirroring the logic above {high} on the short side." if allow_short else ".")
                    ),
                    thesis=("Short-horizon price moves overshoot and partially retrace, so a "
                            "bounded oscillator at an extreme marks a statistically stretched price."),
                    direction="long-short" if allow_short else "long",
                    timeframes=["1d"],
                    holding_period="Days",
                    data_requirements=list(OHLCV),
                    indicator_requirements=[f"{indicator}({length})"],
                    entry_rules=[f"Go long on the next open after {indicator}({length}) closes below {low}."]
                                + ([f"Go short on the next open after {indicator}({length}) closes above {high}."]
                                   if allow_short else []),
                    exit_rules=[f"Exit when {indicator}({length}) crosses back through its midpoint."],
                    stop_rules=["Optional ATR stop, disabled by default."],
                    evidence_level="community",
                    implementation_status="executable",
                    rule_id=rule_id,
                    parameters=[
                        P("length", "Lookback", "int", length, 2, 200),
                        P("oversold", f"{low_name.title()} level", "float", low, -300.0, 100.0),
                        P("overbought", f"{high_name.title()} level", "float", high, -100.0, 300.0),
                        P("allow_short", "Allow short side", "bool", allow_short),
                        *RISK_PARAMS,
                    ],
                    default_parameters={"length": length, "oversold": low, "overbought": high,
                                        "allow_short": allow_short},
                    sources=[source],
                    origin_creator=source.author,
                    origin_year=source.year,
                    tags=["mean reversion", "oscillator", label.lower()],
                    complexity="low",
                    trade_frequency="high" if length <= 5 else "medium",
                    market_regime=["range-bound"],
                    limitations=["Oscillators stay pinned at an extreme through strong trends.",
                                 "Short-period variants trade often, so costs dominate."],
                )


def _band_reversion_family() -> Iterable[TradingStrategy]:
    boll = _src("Bollinger on Bollinger Bands", author="John Bollinger", year=2001)
    for length in (10, 20, 50):
        for dev in (1.5, 2.0, 2.5, 3.0):
            for allow_short in (False, True):
                suffix = "ls" if allow_short else "long"
                yield _strategy(
                    id=f"reversion.bollinger.{length}-{str(dev).replace('.', 'p')}-{suffix}",
                    canonical_name=f"Bollinger Band reversion {length}/{dev}",
                    display_name=f"Bollinger {length}, {dev}σ reversion"
                                 + (" (long-short)" if allow_short else ""),
                    aliases=["Bollinger bounce", "Band fade"],
                    category="Mean reversion",
                    subcategory="Volatility bands",
                    parent_id="reversion.bollinger",
                    description=(f"Buys a close below the lower Bollinger Band ({length}-period, "
                                 f"{dev} standard deviations) and exits at the middle band."),
                    thesis=("Bands normalise deviation by recent volatility, so a tag of the outer "
                            "band is a volatility-adjusted stretch rather than a raw price move."),
                    direction="long-short" if allow_short else "long",
                    timeframes=["1d"],
                    holding_period="Days to weeks",
                    data_requirements=list(OHLCV),
                    indicator_requirements=[f"Bollinger({length}, {dev})"],
                    entry_rules=[f"Go long on the next open after a close below the lower band."],
                    exit_rules=["Exit when the close returns to the middle band (the moving average)."],
                    evidence_level="community",
                    implementation_status="executable",
                    rule_id="reversion.bollinger",
                    parameters=[
                        P("length", "Band length", "int", length, 5, 200),
                        P("deviations", "Standard deviations", "float", dev, 0.5, 5.0),
                        P("allow_short", "Allow short side", "bool", allow_short),
                        *RISK_PARAMS,
                    ],
                    default_parameters={"length": length, "deviations": dev, "allow_short": allow_short},
                    sources=[boll], origin_creator="John Bollinger", origin_year=1980,
                    tags=["mean reversion", "bollinger", "volatility"],
                    complexity="low", trade_frequency="medium", market_regime=["range-bound"],
                    limitations=["Bands widen in a trend, so the strategy can buy repeatedly into a decline."],
                )


def _breakout_family() -> Iterable[TradingStrategy]:
    turtle = _src("The Original Turtle Trading Rules",
                  "https://www.tradingblox.com/originalturtles/", author="Richard Dennis / William Eckhardt",
                  year=1983)
    donchian = _src("Donchian channel", author="Richard Donchian", year=1960)
    for entry, exit_len in [(20, 10), (55, 20), (10, 5), (40, 20), (100, 50)]:
        for allow_short in (False, True):
            suffix = "ls" if allow_short else "long"
            classic = (entry, exit_len) in {(20, 10), (55, 20)}
            yield _strategy(
                id=f"breakout.donchian.{entry}-{exit_len}-{suffix}",
                canonical_name=f"Donchian channel breakout {entry}/{exit_len}",
                display_name=f"Donchian {entry}/{exit_len} breakout"
                             + (" (long-short)" if allow_short else ""),
                aliases=(["Turtle system 1" if entry == 20 else "Turtle system 2"] if classic else [])
                        + ["Price channel breakout"],
                category="Breakout",
                subcategory="Price channel",
                parent_id="breakout.donchian",
                description=(f"Enters on a close beyond the highest high of the prior {entry} bars and "
                             f"exits on a close beyond the opposite extreme of the prior {exit_len} bars."),
                thesis=("A move outside a multi-week range is evidence that new information has "
                        "arrived, and such moves have historically continued more often than chance."),
                direction="long-short" if allow_short else "long",
                timeframes=["1d"],
                holding_period="Weeks to months",
                data_requirements=list(OHLCV),
                indicator_requirements=[f"Donchian({entry})", f"Donchian({exit_len})"],
                entry_rules=[f"Go long on the next open after a close above the prior {entry}-bar high."],
                exit_rules=[f"Exit long on the next open after a close below the prior {exit_len}-bar low."],
                stop_rules=["The channel exit is the stop; an ATR stop can be added."],
                evidence_level="historical" if classic else "community",
                implementation_status="executable",
                rule_id="trend.donchian_breakout",
                parameters=[
                    P("entry_length", "Entry channel", "int", entry, 2, 400),
                    P("exit_length", "Exit channel", "int", exit_len, 2, 400),
                    P("allow_short", "Allow short side", "bool", allow_short),
                    *RISK_PARAMS,
                ],
                default_parameters={"entry_length": entry, "exit_length": exit_len,
                                    "allow_short": allow_short},
                sources=[turtle, donchian] if classic else [donchian],
                origin_creator="Richard Donchian" if not classic else "Richard Dennis / William Eckhardt",
                origin_year=1960 if not classic else 1983,
                origin_notes="The Turtle experiment used this channel logic on futures in the 1980s.",
                tags=["breakout", "turtle", "donchian", "channel"],
                complexity="low", trade_frequency="low", market_regime=["trending"],
                limitations=["Long flat periods; most breakouts fail and the profit comes from few large trends."],
            )


def _simple_executable_family() -> Iterable[TradingStrategy]:
    """One-off named strategies that do not need a whole variation grid."""
    common = dict(timeframes=["1d"], data_requirements=list(OHLCV),
                  implementation_status="executable")
    yield _strategy(
        id="benchmark.buy-and-hold.base", canonical_name="Buy and hold",
        display_name="Buy and hold (benchmark)", aliases=["Passive", "Long only index"],
        category="Long-term investment", subcategory="Passive",
        description="Buys on the first bar and holds to the end of the test window.",
        thesis="Equity indices have risen over long horizons; this is the benchmark every "
               "active strategy has to beat after costs.",
        direction="long", holding_period="Years",
        entry_rules=["Buy at the first available open."],
        exit_rules=["Hold until the end of the test window."],
        evidence_level="academic", rule_id="benchmark.buy_and_hold",
        parameters=[], default_parameters={},
        sources=[_src("Common Risk Factors in the Returns on Stocks and Bonds",
                      author="Fama and French", year=1993)],
        tags=["benchmark", "passive"], complexity="low", trade_frequency="very-low",
        origin_year=1900, **common)
    yield _strategy(
        id="trend.macd.classic-12-26-9-long", canonical_name="MACD signal-line crossover",
        display_name="MACD 12/26/9 signal cross", aliases=["Moving average convergence divergence"],
        category="Trend following", subcategory="MACD",
        description="Holds long while the MACD line is above its 9-period signal line.",
        thesis="The difference between two exponential averages captures trend acceleration "
               "earlier than either average alone.",
        direction="long", holding_period="Weeks",
        indicator_requirements=["MACD(12,26,9)"],
        entry_rules=["Go long on the next open after the MACD line closes above the signal line."],
        exit_rules=["Exit on the next open after the MACD line closes below the signal line."],
        evidence_level="community", rule_id="trend.macd",
        parameters=[P("fast", "Fast EMA", "int", 12, 2, 100), P("slow", "Slow EMA", "int", 26, 3, 200),
                    P("signal", "Signal EMA", "int", 9, 2, 100),
                    P("trigger", "Trigger", "choice", "signal", choices=("signal", "zero")),
                    P("allow_short", "Allow short side", "bool", False), *RISK_PARAMS],
        default_parameters={"fast": 12, "slow": 26, "signal": 9, "trigger": "signal", "allow_short": False},
        sources=[_src("Technical analysis of MACD", author="Gerald Appel", year=1979)],
        origin_creator="Gerald Appel", origin_year=1979,
        tags=["trend", "macd"], complexity="low", trade_frequency="medium", **common)


CANDLESTICK_PATTERNS = [
    ("bullish-engulfing", "Engulfing", "Bullish/bearish engulfing", 1750),
    ("hammer", "Hammer", "Hammer / hanging man", 1750),
    ("shooting-star", "Shooting star", "Shooting star / inverted hammer", 1750),
    ("doji", "Doji", "Doji indecision candle", 1750),
    ("morning-star", "Morning star", "Morning / evening star", 1750),
    ("harami", "Harami", "Harami (inside body)", 1750),
    ("piercing", "Piercing line", "Piercing line / dark-cloud cover", 1750),
    ("three-soldiers", "Three white soldiers", "Three white soldiers / three black crows", 1750),
    ("pin-bar", "Pin bar", "Pin bar rejection", 1990),
    ("outside-bar", "Outside bar", "Outside / engulfing range bar", 1990),
    ("marubozu", "Marubozu", "Marubozu full-body candle", 1750),
]


def _candlestick_family() -> Iterable[TradingStrategy]:
    nison = _src("Japanese Candlestick Charting Techniques", author="Steve Nison", year=1991)
    homma = _src("Candlestick charting, attributed to Japanese rice traders",
                 author="Munehisa Homma (attributed)", year=1755, kind="secondary")
    for key, label, full, year in CANDLESTICK_PATTERNS:
        for context in (20, 50):
            for allow_short in (False, True):
                suffix = "ls" if allow_short else "long"
                yield _strategy(
                    id=f"price-action.candlestick.{key}-ctx{context}-{suffix}",
                    canonical_name=f"{full} ({context}-bar context)",
                    display_name=f"{label}, {context}-bar trend context"
                                 + (" (long-short)" if allow_short else ""),
                    aliases=[label, full],
                    category="Price action",
                    subcategory="Candlestick pattern",
                    parent_id=f"price-action.candlestick.{key}",
                    description=(f"Takes the {label.lower()} pattern only when price is on the correct "
                                 f"side of its {context}-bar average, so the pattern is read in context "
                                 "rather than in isolation."),
                    thesis=("A candlestick encodes the intrabar fight between buyers and sellers; the "
                            "classical claim is that specific shapes after a directional move mark "
                            "exhaustion."),
                    direction="long-short" if allow_short else "long",
                    timeframes=["1d"],
                    holding_period="Days",
                    data_requirements=list(OHLCV),
                    indicator_requirements=[f"SMA({context})", "candle geometry"],
                    entry_rules=[
                        f"Detect the {label.lower()} geometry on the completed bar.",
                        f"Require the close to be below its {context}-bar SMA for the bullish case.",
                        "Enter at the next bar's open.",
                    ],
                    exit_rules=["Exit after the maximum holding period, by default 5 bars.",
                                "Optional ATR stop and target."],
                    evidence_level="historical",
                    implementation_status="executable",
                    rule_id="price-action.candlestick",
                    systematic_interpretation=True,
                    parameters=[
                        P("pattern", "Pattern", "choice", key,
                          choices=tuple(k for k, *_ in CANDLESTICK_PATTERNS)),
                        P("context_length", "Trend context length", "int", context, 5, 200),
                        P("max_bars_held", "Bars held", "int", 5, 1, 100),
                        P("allow_short", "Allow short side", "bool", allow_short),
                        P("atr_stop_multiple", "ATR stop multiple", "float", 0.0, 0.0, 20.0),
                        P("take_profit_multiple", "Take profit multiple", "float", 0.0, 0.0, 20.0),
                    ],
                    default_parameters={"pattern": key, "context_length": context,
                                        "max_bars_held": 5, "allow_short": allow_short},
                    sources=[nison, homma],
                    origin_year=year,
                    origin_notes="Candlestick charting is attributed to Japanese rice merchants and "
                                 "was introduced to Western readers by Steve Nison in 1991.",
                    tags=["price action", "candlestick", key],
                    complexity="low", trade_frequency="medium",
                    limitations=[
                        "Classical candlestick trading is discretionary; this is a fixed geometric "
                        "interpretation and will not match any particular author's reading.",
                        "Pattern frequency varies enormously between instruments and timeframes.",
                    ],
                )


def _generic_grid(base_id: str, rule_id: str, category: str, subcategory: str,
                  canonical: str, description: str, thesis: str, combos: list[dict[str, Any]],
                  parameters: list[StrategyParameter], *, evidence: str = "community",
                  tags: list[str] | None = None, sources: list[StrategySource] | None = None,
                  origin_creator: str = "", origin_year: int | None = None,
                  indicator: str = "", frequency: str = "medium",
                  holding: str = "Days to weeks", regime: list[str] | None = None,
                  limitations: list[str] | None = None) -> Iterable[TradingStrategy]:
    for combo in combos:
        allow_short = bool(combo.get("allow_short", False))
        slug = combo.pop("_slug")
        label = combo.pop("_label")
        suffix = "ls" if allow_short else "long"
        yield _strategy(
            id=f"{base_id}.{slug}-{suffix}",
            canonical_name=f"{canonical} ({label})",
            display_name=f"{canonical} {label}" + (" (long-short)" if allow_short else ""),
            category=category, subcategory=subcategory, parent_id=base_id,
            description=description.format(label=label, **combo),
            thesis=thesis,
            direction="long-short" if allow_short else "long",
            timeframes=["1d"], holding_period=holding,
            data_requirements=list(OHLCV),
            indicator_requirements=[indicator] if indicator else [],
            entry_rules=[f"Evaluate the rule on the completed bar ({label}); enter at the next open."],
            exit_rules=["Exit at the next open when the condition no longer holds, or on the "
                        "optional ATR stop / holding-period limit."],
            evidence_level=evidence, implementation_status="executable", rule_id=rule_id,
            parameters=list(parameters) + list(RISK_PARAMS),
            default_parameters=dict(combo),
            sources=sources or [], origin_creator=origin_creator, origin_year=origin_year,
            tags=tags or [], complexity="low", trade_frequency=frequency,
            market_regime=regime or [], limitations=limitations or [],
        )


def _curated() -> list[TradingStrategy]:
    out: list[TradingStrategy] = []
    out += list(_crossover_family())
    out += list(_price_vs_ma_family())
    out += list(_oscillator_reversion_family())
    out += list(_band_reversion_family())
    out += list(_breakout_family())
    out += list(_candlestick_family())
    out += list(_simple_executable_family())

    out += list(_generic_grid(
        "trend.supertrend", "trend.supertrend", "Trend following", "SuperTrend",
        "SuperTrend", "Follows the SuperTrend direction flip using an ATR band ({label}), reversing only on a confirmed flip.",
        "An ATR-scaled trailing band flips direction only after a move large relative to "
        "recent volatility, filtering noise that a raw price cross would trade.",
        [{"_slug": f"{l}-{str(m).replace('.', 'p')}", "_label": f"{l}/{m}", "length": l,
          "multiplier": m, "allow_short": s}
         for l in (7, 10, 14) for m in (2.0, 3.0, 4.0) for s in (False, True)],
        [P("length", "ATR length", "int", 10, 2, 100),
         P("multiplier", "ATR multiple", "float", 3.0, 0.5, 10.0),
         P("allow_short", "Allow short side", "bool", False)],
        tags=["trend", "supertrend", "atr"], indicator="SuperTrend", regime=["trending"],
        holding="Weeks", limitations=["Flips repeatedly when volatility contracts."]))

    out += list(_generic_grid(
        "trend.adx-filtered", "trend.adx_filtered_ma", "Trend following", "ADX filter",
        "ADX-filtered directional trend",
        "Trades the +DI versus -DI direction, but only while ADX confirms trend strength ({label}).",
        "ADX measures trend strength without direction, so it is used to suppress "
        "directional signals during ranges.",
        [{"_slug": f"{l}-{int(t)}", "_label": f"{l}, ADX>{int(t)}", "adx_length": l,
          "adx_threshold": t, "allow_short": s}
         for l in (10, 14, 20) for t in (20.0, 25.0, 30.0) for s in (False, True)],
        [P("adx_length", "ADX length", "int", 14, 2, 100),
         P("adx_threshold", "ADX threshold", "float", 25.0, 5.0, 60.0),
         P("allow_short", "Allow short side", "bool", False)],
        sources=[_src("New Concepts in Technical Trading Systems",
                      author="J. Welles Wilder Jr.", year=1978)],
        origin_creator="J. Welles Wilder Jr.", origin_year=1978,
        tags=["trend", "adx", "filter"], indicator="ADX", regime=["trending"], holding="Weeks"))

    out += list(_generic_grid(
        "reversion.zscore", "reversion.zscore", "Mean reversion", "Z-score",
        "Rolling z-score reversion",
        "Buys when the close sits {label} below its rolling mean in standard-deviation units, exiting near the mean.",
        "A price expressed in standard deviations from its own recent mean is a "
        "scale-free measure of how stretched it is.",
        [{"_slug": f"{l}-{str(e).replace('.', 'p')}", "_label": f"{l}-bar, {e}σ", "length": l,
          "entry_z": e, "exit_z": 0.5, "allow_short": s}
         for l in (10, 20, 50) for e in (1.5, 2.0, 2.5, 3.0) for s in (False, True)],
        [P("length", "Lookback", "int", 20, 5, 250), P("entry_z", "Entry z", "float", 2.0, 0.5, 6.0),
         P("exit_z", "Exit z", "float", 0.5, 0.0, 3.0),
         P("allow_short", "Allow short side", "bool", False)],
        tags=["mean reversion", "zscore", "statistical"], indicator="rolling z-score",
        regime=["range-bound"], holding="Days"))

    out += list(_generic_grid(
        "breakout.price-channel", "breakout.price_channel", "Breakout", "Price channel",
        "N-bar high breakout",
        "Enters on a close beyond the prior {label} high or low, holding until the opposite condition appears.",
        "Range expansion beyond a recent extreme is the simplest observable definition "
        "of a breakout.",
        [{"_slug": f"{l}", "_label": f"{l}-bar", "length": l, "allow_short": s}
         for l in (5, 10, 20, 50, 100, 252) for s in (False, True)],
        [P("length", "Channel length", "int", 20, 2, 400),
         P("allow_short", "Allow short side", "bool", False)],
        tags=["breakout", "channel"], indicator="rolling high/low", regime=["trending"],
        holding="Days to weeks"))

    out += list(_generic_grid(
        "breakout.narrow-range", "breakout.narrow_range", "Breakout", "Volatility contraction",
        "Narrow range breakout",
        "Arms on the narrowest range of the last {label} bars, then trades the break of that bar in either direction.",
        "Volatility clusters, so an unusually quiet bar is often followed by an expansion.",
        [{"_slug": f"nr{n}", "_label": f"{n} bars", "count": n, "allow_short": s,
          "max_bars_held": 5} for n in (4, 7, 10) for s in (False, True)],
        [P("count", "Range lookback", "int", 7, 2, 50),
         P("max_bars_held", "Bars held", "int", 5, 1, 50),
         P("allow_short", "Allow short side", "bool", False)],
        sources=[_src("Street Smarts: High Probability Short-Term Trading Strategies",
                      author="Linda Raschke and Laurence Connors", year=1995)],
        origin_creator="Linda Raschke and Laurence Connors", origin_year=1995,
        tags=["breakout", "nr7", "volatility"], indicator="bar range",
        frequency="medium", holding="Days"))

    out += list(_generic_grid(
        "breakout.gap", "breakout.gap", "Gap", "Opening gap",
        "Overnight gap reaction",
        "Reacts to an overnight opening gap of at least {label}, either continuing with it or fading it.",
        "An overnight gap prices information released while the market was closed; the "
        "open question is whether the move continues or retraces.",
        [{"_slug": f"{'fade' if fade else 'go'}-{str(g).replace('.', 'p')}",
          "_label": f"{g}% ({'fade' if fade else 'continuation'})", "gap_pct": g, "fade": fade,
          "allow_short": s, "max_bars_held": 3}
         for g in (1.0, 2.0, 3.0, 5.0) for fade in (False, True) for s in (False, True)],
        [P("gap_pct", "Gap size %", "float", 2.0, 0.1, 30.0),
         P("fade", "Fade the gap", "bool", False),
         P("max_bars_held", "Bars held", "int", 3, 1, 30),
         P("allow_short", "Allow short side", "bool", False)],
        tags=["gap", "event"], indicator="overnight gap", frequency="medium", holding="Days",
        limitations=["Daily bars cannot distinguish an intraday gap fill from a close-to-close move."]))

    out += list(_generic_grid(
        "momentum.time-series", "momentum.time_series", "Momentum", "Time-series momentum",
        "Absolute (time-series) momentum",
        "Holds long while the trailing {label} return is positive, and moves to cash or short when it turns negative.",
        "Assets that have risen over the past 3-12 months have tended to keep rising over "
        "the following weeks, an effect documented across markets and eras.",
        [{"_slug": f"{l}-skip{sk}", "_label": f"{l}-bar (skip {sk})", "lookback": l, "skip": sk,
          "allow_short": s}
         for l in (21, 63, 126, 252) for sk in (0, 21) for s in (False, True)],
        [P("lookback", "Lookback bars", "int", 252, 5, 750),
         P("skip", "Skip most recent bars", "int", 0, 0, 60),
         P("allow_short", "Allow short side", "bool", False)],
        evidence="academic",
        sources=[_src("Time Series Momentum", "https://doi.org/10.1016/j.jfineco.2011.11.003",
                      "Moskowitz, Ooi and Pedersen", 2012),
                 _src("Returns to Buying Winners and Selling Losers",
                      author="Jegadeesh and Titman", year=1993)],
        origin_creator="Jegadeesh and Titman", origin_year=1993,
        tags=["momentum", "trend", "academic"], indicator="trailing return",
        frequency="low", holding="Months", regime=["trending"]))

    out += list(_generic_grid(
        "reversion.short-term-reversal", "reversion.short_term_reversal", "Mean reversion",
        "Short-term reversal", "Short-term reversal",
        "Fades a {label} move over the prior window, buying weakness and selling strength.",
        "Very short-horizon returns reverse on average, usually attributed to liquidity "
        "provision and price pressure rather than to a forecast of fundamentals.",
        [{"_slug": f"{l}-{int(t)}", "_label": f"{l}-bar, {int(t)}%", "lookback": l,
          "threshold_pct": t, "allow_short": s, "max_bars_held": l}
         for l in (1, 3, 5, 10) for t in (3.0, 5.0, 10.0) for s in (False, True)],
        [P("lookback", "Lookback bars", "int", 5, 1, 60),
         P("threshold_pct", "Move threshold %", "float", 5.0, 0.1, 50.0),
         P("max_bars_held", "Bars held", "int", 5, 1, 60),
         P("allow_short", "Allow short side", "bool", False)],
        evidence="academic",
        sources=[_src("Does the Stock Market Overreact?", author="De Bondt and Thaler", year=1985),
                 _src("Contrarian Investment, Extrapolation, and Risk",
                      author="Lakonishok, Shleifer and Vishny", year=1994)],
        origin_creator="De Bondt and Thaler", origin_year=1985,
        tags=["mean reversion", "reversal", "academic"], indicator="trailing return",
        frequency="high", holding="Days", regime=["range-bound"]))

    out += list(_generic_grid(
        "volume.relative-volume", "volume.relative_volume_breakout", "Volume", "Relative volume",
        "Volume-confirmed breakout",
        "Takes a channel breakout only when relative volume on the signal bar exceeds {label}.",
        "A breakout on heavy participation is more likely to reflect real repositioning "
        "than one on a quiet tape.",
        [{"_slug": f"{str(m).replace('.', 'p')}-{b}", "_label": f"{m}x, {b}-bar", "multiple": m,
          "breakout_length": b, "allow_short": s, "max_bars_held": 10}
         for m in (1.5, 2.0, 3.0) for b in (10, 20, 50) for s in (False, True)],
        [P("multiple", "Relative volume multiple", "float", 2.0, 1.0, 20.0),
         P("breakout_length", "Breakout channel", "int", 20, 2, 200),
         P("length", "Volume average length", "int", 20, 2, 200),
         P("max_bars_held", "Bars held", "int", 10, 1, 100),
         P("allow_short", "Allow short side", "bool", False)],
        tags=["volume", "breakout"], indicator="relative volume", holding="Days to weeks"))

    out += list(_generic_grid(
        "seasonal.turn-of-month", "seasonal.turn_of_month", "Calendar and seasonal",
        "Turn of the month", "Turn-of-the-month effect",
        "Holds the market only around the month boundary ({label}) and stays in cash otherwise.",
        "Month-end flows from salary investment, index rebalancing and reporting have been "
        "associated with a concentration of equity returns around the turn of the month.",
        [{"_slug": f"{b}-{a}", "_label": f"{b} before / {a} after", "days_before": b, "days_after": a}
         for b in (1, 3, 5) for a in (1, 3, 5)],
        [P("days_before", "Days before month end", "int", 3, 0, 10),
         P("days_after", "Days after month start", "int", 3, 0, 10)],
        evidence="academic",
        sources=[_src("The Turn-of-the-Month Effect in Equity Markets",
                      author="Ariel", year=1987)],
        origin_creator="Robert Ariel", origin_year=1987,
        tags=["seasonal", "calendar"], indicator="calendar", frequency="medium",
        holding="Days", limitations=["Calendar effects are prone to data mining and have weakened since publication."]))

    out += list(_generic_grid(
        "seasonal.month-window", "seasonal.month_window", "Calendar and seasonal",
        "Seasonal month window", "Seasonal month window",
        "Holds the market only during the months {label}, and sits in cash the rest of the year.",
        "The 'sell in May' or Halloween pattern claims equity returns concentrate in the "
        "November-April half of the year.",
        [{"_slug": slug, "_label": lbl, "months": months} for slug, lbl, months in [
            ("halloween", "Nov-Apr (Halloween)", "11,12,1,2,3,4"),
            ("summer", "May-Oct", "5,6,7,8,9,10"),
            ("q4", "Oct-Dec", "10,11,12"),
            ("january", "January only", "1"),
            ("santa", "December only", "12"),
        ]],
        [P("months", "Months (comma separated)", "choice", "11,12,1,2,3,4",
           choices=("11,12,1,2,3,4", "5,6,7,8,9,10", "10,11,12", "1", "12"))],
        evidence="academic",
        sources=[_src("The Halloween Indicator, 'Sell in May and Go Away'",
                      author="Bouman and Jacobsen", year=2002)],
        origin_creator="Bouman and Jacobsen", origin_year=2002,
        tags=["seasonal", "calendar"], indicator="calendar", frequency="very-low",
        holding="Months"))

    out += list(_generic_grid(
        "seasonal.day-of-week", "seasonal.day_of_week", "Calendar and seasonal",
        "Day of week", "Day-of-week effect",
        "Holds the market only on {label} and stays in cash on every other weekday.",
        "Early studies reported systematically different mean returns by weekday, most "
        "famously a negative Monday.",
        [{"_slug": name.lower(), "_label": name, "weekday": i, "short": False}
         for i, name in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])],
        [P("weekday", "Weekday (0=Mon)", "int", 0, 0, 4), P("short", "Trade short", "bool", False)],
        evidence="academic",
        sources=[_src("Day of the Week Effects and Asset Returns", author="Gibbons and Hess", year=1981),
                 _src("Stock Returns and the Weekend Effect", author="French", year=1980)],
        origin_creator="Kenneth French", origin_year=1980,
        tags=["seasonal", "calendar"], indicator="calendar", frequency="very-high", holding="1 day",
        limitations=["The weekend effect has largely disappeared in post-publication samples."]))

    out += list(_generic_grid(
        "allocation.trend-filtered", "allocation.trend_filtered", "Portfolio and allocation",
        "Trend-filtered allocation", "Month-end trend filter",
        "Checks a {label} trend filter at each month end and holds that decision for the whole following month.",
        "Rebalancing monthly rather than daily reduces turnover and whipsaw while keeping "
        "most of the drawdown protection of a long moving-average filter.",
        [{"_slug": f"{k}-{l}", "_label": f"{k.upper()}({l})", "length": l, "ma_kind": k,
          "month_end_only": True} for k in ("sma", "ema") for l in (100, 150, 200, 250)],
        [P("length", "Filter length", "int", 200, 20, 400),
         P("ma_kind", "Average type", "choice", "sma", choices=("sma", "ema")),
         P("month_end_only", "Rebalance monthly", "bool", True)],
        evidence="academic",
        sources=[_src("A Quantitative Approach to Tactical Asset Allocation",
                      "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461", "Meb Faber", 2007)],
        origin_creator="Meb Faber", origin_year=2007,
        tags=["allocation", "timing", "tactical"], indicator="moving average",
        frequency="very-low", holding="Months"))

    out += list(_generic_grid(
        "combo.confirmed-trend", "combo.confirmed_trend", "Multi-signal", "Confirmed trend",
        "Trend with oscillator and volume confirmation",
        "Requires the trend, the oscillator and the volume filter to agree ({label}) before any position is taken.",
        "Combining one indicator from each family (trend, momentum, participation) is the "
        "standard retail recipe for reducing single-indicator false signals.",
        [{"_slug": f"{f}-{sl}-{int(r)}", "_label": f"EMA {f}/{sl}, RSI>{int(r)}", "fast": f,
          "slow": sl, "rsi_level": r, "allow_short": s}
         for f, sl in [(10, 30), (20, 50), (50, 200)] for r in (50.0, 55.0) for s in (False, True)],
        [P("fast", "Fast EMA", "int", 20, 2, 200), P("slow", "Slow EMA", "int", 50, 3, 400),
         P("rsi_level", "RSI level", "float", 50.0, 10.0, 90.0),
         P("min_relative_volume", "Minimum relative volume", "float", 1.0, 0.1, 10.0),
         P("allow_short", "Allow short side", "bool", False)],
        tags=["multi-signal", "confirmation"], indicator="EMA + RSI + relative volume",
        holding="Weeks", regime=["trending"]))

    for rid, base, cname, desc, thesis, combos, params, tags, indicator in [
        ("trend.parabolic_sar", "trend.parabolic-sar", "Parabolic SAR",
         "Goes long while price is above the parabolic stop-and-reverse dot ({label}), and reverses below it.",
         "A stop that accelerates with the trend converts a trend-following system into an "
         "always-in reversal system.",
         [{"_slug": f"{str(s).replace('.', 'p')}", "_label": f"step {s}", "step": s,
           "maximum": 0.2, "allow_short": sh} for s in (0.01, 0.02, 0.04) for sh in (False, True)],
         [P("step", "Acceleration step", "float", 0.02, 0.001, 0.5),
          P("maximum", "Maximum acceleration", "float", 0.2, 0.01, 1.0),
          P("allow_short", "Allow short side", "bool", False)],
         ["trend", "sar"], "Parabolic SAR"),
        ("trend.ichimoku", "trend.ichimoku", "Ichimoku cloud",
         "Goes long while the close is above both cloud spans with settings {label}, and flat or short below them.",
         "The cloud combines several midpoints of past ranges into one visual trend and "
         "support/resistance system.",
         [{"_slug": f"{c}-{b}-{sp}", "_label": f"{c}/{b}/{sp}", "conversion": c, "base": b,
           "span": sp, "require_tk_cross": True, "allow_short": sh}
          for c, b, sp in [(9, 26, 52), (7, 22, 44), (10, 30, 60)] for sh in (False, True)],
         [P("conversion", "Conversion line", "int", 9, 2, 60),
          P("base", "Base line", "int", 26, 3, 120),
          P("span", "Leading span B", "int", 52, 5, 240),
          P("require_tk_cross", "Require Tenkan/Kijun agreement", "bool", True),
          P("allow_short", "Allow short side", "bool", False)],
         ["trend", "ichimoku"], "Ichimoku"),
        ("trend.linreg_slope", "trend.linreg-slope", "Linear regression slope",
         "Goes long while the {label} least-squares regression slope is positive, and flat or short below zero.",
         "Fitting a least-squares line to recent closes gives a smooth, differentiable "
         "trend estimate with less lag than a moving average.",
         [{"_slug": f"{l}", "_label": f"{l}-bar", "length": l, "threshold": 0.0, "allow_short": sh}
          for l in (10, 20, 50, 100) for sh in (False, True)],
         [P("length", "Regression length", "int", 20, 3, 300),
          P("threshold", "Slope threshold (% of price)", "float", 0.0, 0.0, 5.0),
          P("allow_short", "Allow short side", "bool", False)],
         ["trend", "regression"], "linear regression slope"),
        ("volume.obv_trend", "volume.obv-trend", "On-balance volume trend",
         "Goes long while on-balance volume is above its {label} moving average, and flat below it.",
         "On-balance volume accumulates signed volume, so its own trend is a crude proxy "
         "for whether volume is arriving on up days or down days.",
         [{"_slug": f"{l}", "_label": f"{l}-bar", "length": l, "allow_short": sh}
          for l in (10, 20, 50) for sh in (False, True)],
         [P("length", "OBV average length", "int", 20, 2, 200),
          P("allow_short", "Allow short side", "bool", False)],
         ["volume", "obv"], "OBV"),
        ("volume.chaikin_money_flow", "volume.chaikin-money-flow", "Chaikin money flow",
         "Goes long while Chaikin money flow is above {label}, and short or flat below the mirrored level.",
         "Chaikin money flow weights volume by where the close sits inside the bar's range, "
         "as a proxy for accumulation or distribution.",
         [{"_slug": f"{l}-{str(t).replace('.', 'p')}", "_label": f"{l}-bar, ±{t}", "length": l,
           "threshold": t, "allow_short": sh}
          for l in (20, 50) for t in (0.05, 0.1) for sh in (False, True)],
         [P("length", "CMF length", "int", 20, 2, 200),
          P("threshold", "Threshold", "float", 0.05, 0.0, 1.0),
          P("allow_short", "Allow short side", "bool", False)],
         ["volume", "money flow"], "Chaikin money flow"),
        ("volatility.atr_expansion", "volatility.atr-expansion", "ATR expansion",
         "Trades in the direction of the trend once short-term ATR expands past long-term ATR by {label}.",
         "A short ATR rising above a long ATR marks the start of a volatility expansion, "
         "which often accompanies the start of a directional move.",
         [{"_slug": f"{str(r).replace('.', 'p')}", "_label": f"{r}x", "ratio": r, "allow_short": sh,
           "max_bars_held": 10} for r in (1.2, 1.5, 2.0) for sh in (False, True)],
         [P("ratio", "Fast/slow ATR ratio", "float", 1.2, 1.0, 5.0),
          P("max_bars_held", "Bars held", "int", 10, 1, 100),
          P("allow_short", "Allow short side", "bool", False)],
         ["volatility", "atr"], "ATR"),
        ("price-action.structure", "price-action.structure", "Market structure",
         "Goes long while swing highs and swing lows are both rising over {label}, and short when both fall.",
         "Defining a trend as a sequence of rising swing highs and lows turns a chart-reading "
         "concept into a testable rule.",
         [{"_slug": f"{l}", "_label": f"{l}-bar swings", "length": l, "allow_short": sh}
          for l in (5, 10, 20) for sh in (False, True)],
         [P("length", "Swing length", "int", 10, 2, 100),
          P("allow_short", "Allow short side", "bool", False)],
         ["price action", "structure"], "swing highs/lows"),
        ("breakout.squeeze", "breakout.squeeze", "Squeeze breakout",
         "Trades the release of a Bollinger-inside-Keltner volatility squeeze ({label}) in the direction of momentum.",
         "When Bollinger Bands contract inside Keltner Channels, realised volatility is "
         "unusually low; the release of that squeeze often precedes an expansion.",
         [{"_slug": f"{l}-{str(m).replace('.', 'p')}", "_label": f"{l}-bar, {m}x", "length": l,
           "multiplier": m, "allow_short": sh, "max_bars_held": 10}
          for l in (20, 50) for m in (1.5, 2.0) for sh in (False, True)],
         [P("length", "Length", "int", 20, 5, 200),
          P("deviations", "Bollinger deviations", "float", 2.0, 0.5, 5.0),
          P("multiplier", "Keltner multiple", "float", 1.5, 0.5, 5.0),
          P("max_bars_held", "Bars held", "int", 10, 1, 100),
          P("allow_short", "Allow short side", "bool", False)],
         ["breakout", "squeeze", "volatility"], "Bollinger + Keltner"),
        ("reversion.consecutive_bars", "reversion.consecutive-bars", "Consecutive down bars",
         "Buys after {label} consecutive lower closes and exits on the reverse streak or a holding limit.",
         "A run of consecutive down closes is the simplest possible oversold definition and "
         "needs no indicator at all.",
         [{"_slug": f"{n}", "_label": f"{n}", "count": n, "allow_short": sh, "max_bars_held": 5}
          for n in (2, 3, 4, 5) for sh in (False, True)],
         [P("count", "Consecutive bars", "int", 3, 2, 20),
          P("max_bars_held", "Bars held", "int", 5, 1, 50),
          P("allow_short", "Allow short side", "bool", False)],
         ["mean reversion", "price action"], "consecutive closes"),
        ("reversion.ma_distance", "reversion.ma-distance", "Stretch from moving average",
         "Fades price once it stretches {label} away from its moving average, exiting back at the average.",
         "Distance from a moving average, expressed as a percentage, is a direct measure of "
         "how far price has run from its recent equilibrium.",
         [{"_slug": f"{l}-{int(s)}", "_label": f"{s}% from MA({l})", "length": l,
           "stretch_pct": s, "allow_short": sh}
          for l in (20, 50, 200) for s in (3.0, 5.0, 10.0) for sh in (False, True)],
         [P("length", "MA length", "int", 50, 5, 400),
          P("stretch_pct", "Stretch %", "float", 5.0, 0.5, 50.0),
          P("ma_kind", "Average type", "choice", "sma", choices=("sma", "ema")),
          P("allow_short", "Allow short side", "bool", False)],
         ["mean reversion", "stretch"], "moving average distance"),
        ("reversion.vwap_band", "reversion.vwap-band", "VWAP band reversion",
         "Fades price once it deviates {label} from rolling VWAP, exiting back at the VWAP line.",
         "VWAP is the benchmark many institutions are measured against, which is the usual "
         "argument for prices being drawn back toward it.",
         [{"_slug": f"{l}-{int(b)}", "_label": f"{b}% over {l} bars", "length": l, "band_pct": b,
           "allow_short": sh} for l in (20, 50) for b in (1.0, 2.0, 3.0) for sh in (False, True)],
         [P("length", "VWAP length", "int", 20, 2, 200),
          P("band_pct", "Band %", "float", 2.0, 0.1, 20.0),
          P("allow_short", "Allow short side", "bool", False)],
         ["mean reversion", "vwap"], "rolling VWAP"),
        ("momentum.rate_of_change", "momentum.rate-of-change", "Rate of change",
         "Goes long while the {label} rate of change is positive, and flat or short while it is negative.",
         "Rate of change is momentum in its rawest form: the percentage change over a fixed "
         "lookback, with no smoothing.",
         [{"_slug": f"{l}", "_label": f"{l}-bar", "length": l, "threshold": 0.0, "allow_short": sh}
          for l in (5, 12, 21, 63, 126, 252) for sh in (False, True)],
         [P("length", "Lookback", "int", 12, 1, 500),
          P("threshold", "Threshold %", "float", 0.0, 0.0, 50.0),
          P("allow_short", "Allow short side", "bool", False)],
         ["momentum", "roc"], "rate of change"),
        ("momentum.rsi_trend", "momentum.rsi-trend", "RSI trend regime",
         "Uses RSI holding above or below {label} as a trend regime filter rather than an overbought or oversold signal.",
         "Constance Brown's observation that RSI holds above 40-50 in bull phases turns the "
         "same indicator into a trend filter instead of a fade signal.",
         [{"_slug": f"{l}-{int(v)}", "_label": f"{v} (RSI {l})", "length": l, "level": v,
           "allow_short": sh} for l in (14, 21) for v in (50.0, 55.0, 60.0) for sh in (False, True)],
         [P("length", "RSI length", "int", 14, 2, 100),
          P("level", "Regime level", "float", 50.0, 10.0, 90.0),
          P("allow_short", "Allow short side", "bool", False)],
         ["momentum", "rsi", "regime"], "RSI"),
        ("momentum.volatility_scaled", "momentum.volatility-scaled", "Volatility-scaled momentum",
         "Scales a {label} momentum score by realised volatility so the signal is comparable across regimes.",
         "Dividing momentum by volatility equalises risk across regimes so the signal is not "
         "dominated by whichever period happened to be most volatile.",
         [{"_slug": f"{l}", "_label": f"{l}-bar", "lookback": l, "threshold": 0.0, "allow_short": sh}
          for l in (63, 126, 252) for sh in (False, True)],
         [P("lookback", "Momentum lookback", "int", 126, 5, 750),
          P("vol_length", "Volatility length", "int", 20, 5, 250),
          P("threshold", "Threshold", "float", 0.0, 0.0, 10.0),
          P("allow_short", "Allow short side", "bool", False)],
         ["momentum", "volatility", "risk-adjusted"], "return / volatility"),
        ("volatility.regime_filter", "volatility.regime-filter", "Low-volatility regime filter",
         "Holds the market only when realised volatility sits in the lower {label} of its own history and the long trend is up.",
         "Equity drawdowns cluster in high-volatility regimes, so conditioning exposure on a "
         "volatility percentile is a simple defensive overlay.",
         [{"_slug": f"{int(q*100)}", "_label": f"{int(q*100)}th percentile", "max_percentile": q}
          for q in (0.3, 0.5, 0.7)],
         [P("max_percentile", "Maximum volatility percentile", "float", 0.5, 0.05, 1.0),
          P("length", "Volatility length", "int", 20, 5, 250),
          P("trend_length", "Trend length", "int", 100, 10, 400)],
         ["volatility", "regime", "defensive"], "realised volatility percentile"),
        ("breakout.inside_bar", "breakout.inside-bar", "Inside bar breakout",
         "Trades the break of the bar preceding an inside bar, holding for {label} before timing out.",
         "An inside bar is a one-bar contraction; breaking the prior bar's range resolves it.",
         [{"_slug": f"hold{h}", "_label": f"{h}-bar hold", "max_bars_held": h, "allow_short": sh}
          for h in (3, 5, 10) for sh in (False, True)],
         [P("max_bars_held", "Bars held", "int", 5, 1, 50),
          P("allow_short", "Allow short side", "bool", False)],
         ["breakout", "price action", "inside bar"], "bar range"),
        ("breakout.prior_period_high", "breakout.prior-period-high", "New high breakout",
         "Buys when the close sets a new {label} high, on the premise that there is no trapped overhead supply.",
         "Buying at new highs is the classic Darvas/O'Neil idea: an instrument with no "
         "overhead supply faces no trapped sellers.",
         [{"_slug": f"{l}", "_label": f"{l}-bar", "length": l, "allow_short": sh}
          for l in (20, 50, 100, 252) for sh in (False, True)],
         [P("length", "Lookback", "int", 252, 5, 750),
          P("allow_short", "Allow short side", "bool", False)],
         ["breakout", "new high", "darvas"], "rolling high"),
        ("volume.accumulation_trend", "volume.accumulation-trend", "Accumulation/distribution trend",
         "Goes long while the accumulation/distribution line is above its {label} average, and flat below it.",
         "The accumulation/distribution line weights volume by intrabar close location, and "
         "its own trend is used as a participation confirmation.",
         [{"_slug": f"{l}", "_label": f"{l}-bar", "length": l, "allow_short": sh}
          for l in (20, 50) for sh in (False, True)],
         [P("length", "Average length", "int", 20, 2, 200),
          P("allow_short", "Allow short side", "bool", False)],
         ["volume", "accumulation"], "A/D line"),
        ("trend.triple_ma", "trend.triple-ma", "Triple moving average",
         "Requires all three moving averages stacked in trend order ({label}) before taking a position.",
         "Requiring a full stack of three averages is a stricter trend definition than a "
         "single crossover and trades less often.",
         [{"_slug": f"{a}-{b}-{c}", "_label": f"{a}/{b}/{c}", "fast": a, "mid": b, "slow": c,
           "allow_short": sh} for a, b, c in [(5, 20, 50), (10, 30, 100), (20, 50, 200), (4, 9, 18)]
          for sh in (False, True)],
         [P("fast", "Fast", "int", 5, 2, 100), P("mid", "Middle", "int", 20, 3, 200),
          P("slow", "Slow", "int", 50, 5, 400),
          P("ma_kind", "Average type", "choice", "ema", choices=tuple(MA_KINDS)),
          P("allow_short", "Allow short side", "bool", False)],
         ["trend", "moving average"], "three moving averages"),
        ("trend.ma_ribbon", "trend.ma-ribbon", "Moving average ribbon",
         "Requires every line of the moving-average ribbon ({label}) to be fanned in order before taking a position.",
         "A ribbon of evenly spaced averages fans out in a persistent trend and tangles in a "
         "range, giving a visual filter a numeric definition.",
         [{"_slug": f"{b}-{s}-{n}", "_label": f"base {b}, step {s}, {n} lines", "base": b,
           "step": s, "count": n, "allow_short": sh}
          for b, s, n in [(10, 10, 6), (20, 20, 5), (5, 5, 8)] for sh in (False, True)],
         [P("base", "Base length", "int", 10, 2, 100), P("step", "Step", "int", 10, 1, 100),
          P("count", "Line count", "int", 6, 2, 12),
          P("ma_kind", "Average type", "choice", "ema", choices=tuple(MA_KINDS)),
          P("allow_short", "Allow short side", "bool", False)],
         ["trend", "ribbon"], "moving average ribbon"),
    ]:
        out += list(_generic_grid(
            base, rid, _category_for(base), _subcategory_for(base), cname, desc, thesis,
            combos, params, tags=tags, indicator=indicator))
    return out


CATEGORY_BY_PREFIX = {
    "trend": ("Trend following", "Trend system"),
    "reversion": ("Mean reversion", "Reversion system"),
    "breakout": ("Breakout", "Breakout system"),
    "momentum": ("Momentum", "Momentum system"),
    "volume": ("Volume", "Volume system"),
    "volatility": ("Volatility", "Volatility system"),
    "price-action": ("Price action", "Structure"),
    "seasonal": ("Calendar and seasonal", "Calendar"),
    "allocation": ("Portfolio and allocation", "Tactical allocation"),
    "combo": ("Multi-signal", "Confirmation stack"),
    "benchmark": ("Long-term investment", "Passive"),
}


def _category_for(base_id: str) -> str:
    return CATEGORY_BY_PREFIX.get(base_id.split(".")[0], ("Other", "Other"))[0]


def _subcategory_for(base_id: str) -> str:
    return CATEGORY_BY_PREFIX.get(base_id.split(".")[0], ("Other", "Other"))[1]


# =========================================================================
# 2. Academic cross-sectional anomalies (cited, mostly data-blocked here)
# =========================================================================
ECONOMIC_LABELS = {
    "accruals": ("Accruals", "Accounting accruals separate reported earnings from cash flow; "
                             "the accrual component has historically predicted lower future returns."),
    "valuation": ("Valuation", "Ranks firms on a price multiple, buying the cheap side of the "
                               "cross-section and selling the expensive side."),
    "profitability": ("Profitability", "Sorts firms on a measure of operating profitability, "
                                       "which has predicted higher subsequent returns."),
    "profitability alt": ("Profitability (alternative)", "An alternative construction of firm profitability."),
    "investment": ("Investment", "Firms that grow assets aggressively have tended to underperform."),
    "investment alt": ("Investment (alternative)", "An alternative construction of the investment effect."),
    "investment growth": ("Investment growth", "Change in the rate of corporate investment."),
    "momentum": ("Momentum", "Ranks firms on trailing return, buying past winners."),
    "long term reversal": ("Long-term reversal", "Multi-year losers have tended to outperform multi-year winners."),
    "short-term reversal": ("Short-term reversal", "One-month losers have tended to bounce."),
    "liquidity": ("Liquidity", "Sorts on a proxy for trading liquidity or price impact."),
    "volatility": ("Volatility", "Sorts on realised or idiosyncratic volatility; the low-volatility "
                                 "side has historically earned higher risk-adjusted returns."),
    "risk": ("Risk", "Sorts on an estimated risk exposure."),
    "market risk": ("Market risk", "Sorts on estimated market beta."),
    "default risk": ("Default risk", "Sorts on a measure of distress or default probability."),
    "cash flow risk": ("Cash-flow risk", "Sorts on cash-flow based risk exposure."),
    "leverage": ("Leverage", "Sorts on balance-sheet leverage."),
    "external financing": ("External financing", "Firms raising external capital have tended to underperform."),
    "composite accounting": ("Composite accounting", "Combines several accounting inputs into one score."),
    "asset composition": ("Asset composition", "Sorts on the composition of the balance sheet."),
    "sales growth": ("Sales growth", "Sorts on revenue growth."),
    "earnings growth": ("Earnings growth", "Sorts on the growth rate of earnings."),
    "earnings forecast": ("Earnings forecast", "Uses sell-side earnings forecasts."),
    "earnings event": ("Earnings event", "Conditions on the earnings announcement itself."),
    "recommendation": ("Analyst recommendation", "Uses published analyst recommendations."),
    "R&D": ("Research and development", "Sorts on R&D intensity or its capitalised value."),
    "payout indicator": ("Payout", "Conditions on dividends or buybacks."),
    "ownership": ("Ownership", "Sorts on the ownership structure of the firm."),
    "informed trading": ("Informed trading", "Proxies for trading by better-informed participants."),
    "short sale constraints": ("Short-sale constraints", "Proxies for how hard a stock is to short."),
    "lead lag": ("Lead-lag", "Uses the returns of economically related firms as a predictor."),
    "volume": ("Volume", "Sorts on trading volume or its trend."),
    "turnover": ("Turnover", "Sorts on share turnover."),
    "size": ("Size", "Sorts on market capitalisation."),
    "optionrisk": ("Option-implied risk", "Uses information from the options market."),
    "info proxy": ("Information proxy", "A proxy for the information environment of the firm."),
    "other": ("Other", "A published cross-sectional return predictor."),
}

DATA_LABEL = {
    "Accounting": "company fundamentals (balance sheet, income statement, cash flow)",
    "Analyst": "sell-side analyst estimates and recommendations",
    "Options": "equity options chains and implied volatility",
    "13F": "institutional 13F holdings filings",
    "Event": "corporate event dates and outcomes",
    "Trading": "cross-sectional trading and microstructure data",
    "Other": "additional external data",
    "Price": "daily OHLCV",
}

JOURNALS = {
    "JF": "Journal of Finance", "JFE": "Journal of Financial Economics",
    "RFS": "Review of Financial Studies", "AR": "The Accounting Review",
    "JAR": "Journal of Accounting Research", "JAE": "Journal of Accounting and Economics",
    "JFQA": "Journal of Financial and Quantitative Analysis", "MS": "Management Science",
    "RAS": "Review of Accounting Studies", "JPM": "Journal of Portfolio Management",
    "FAJ": "Financial Analysts Journal", "JB": "Journal of Business",
}


def _slugify(text: str) -> str:
    """Lower-kebab slug: the schema rejects anything else, on purpose."""
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in str(text))
    return "-".join(part for part in cleaned.split("-") if part) or "other"


def _humanise(acronym: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(acronym):
        if ch.isupper() and i and not acronym[i - 1].isupper():
            out.append(" ")
        out.append(ch)
    return "".join(out).replace("_", " ").strip()


def _academic() -> list[TradingStrategy]:
    path = DATA_DIR / "academic_signals.json"
    if not path.exists():
        return []
    records = json.loads(path.read_text(encoding="utf-8"))
    index_source = _src(
        "Open Source Cross-Sectional Asset Pricing (signal index used to enumerate the literature)",
        "https://www.openassetpricing.com/", "Andrew Y. Chen and Tom Zimmermann", 2022, kind="index")
    out: list[TradingStrategy] = []
    for row in records:
        acronym = row["acronym"]
        economic = row.get("economic") or "other"
        label, blurb = ECONOMIC_LABELS.get(economic, ECONOMIC_LABELS["other"])
        data_kind = row.get("data") or "Other"
        needs = DATA_LABEL.get(data_kind, "additional external data")
        authors = row.get("authors") or "See original paper"
        year = row.get("year")
        journal = JOURNALS.get(row.get("journal", ""), row.get("journal", ""))
        replicated = bool(row.get("replicated"))
        slug = _slugify(acronym)
        price_only = data_kind == "Price"
        out.append(_strategy(
            id=f"academic.{_slugify(economic)}.{slug}",
            canonical_name=f"{_humanise(acronym)} ({label.lower()} anomaly)",
            display_name=_humanise(acronym),
            aliases=[acronym],
            category="Academic anomaly",
            subcategory=label,
            description=(
                f"{blurb} Published by {authors}"
                + (f" in {year}" if year else "")
                + (f", {journal}" if journal else "")
                + ". Portfolios are formed by ranking the cross-section on this characteristic "
                  "and holding the extreme groups."
            ),
            thesis=("Cross-sectional predictors document a spread in average returns between "
                    "firms ranked high and low on a characteristic; whether that spread is "
                    "compensation for risk or a mispricing remains contested."),
            direction="long-short",
            timeframes=["1mo"],
            holding_period="Months, rebalanced monthly",
            data_requirements=["monthly cross-sectional returns", needs],
            # Even a price-only anomaly needs the cross-section: the signal is a RANK
            # against every other stock, which single-symbol OHLCV cannot provide.
            external_data_requirements=(
                ["a survivorship-free US equity universe with monthly cross-sectional returns"]
                if price_only else
                [needs, "a survivorship-free US equity universe with monthly cross-sectional returns"]
            ),
            entry_rules=["Rank the investable universe on the characteristic each month.",
                         "Buy the top decile and sell the bottom decile."],
            exit_rules=["Rebalance monthly; positions are replaced at each formation date."],
            evidence_level="academic" if replicated else "experimental",
            implementation_status="requires-data",
            unsupported_reason="",
            limitations=[
                "This project holds single-symbol OHLCV, not a survivorship-free cross-section, "
                "so no cross-sectional rank can be computed here.",
                ("Replicated with a t-statistic above 2 in the referenced replication study."
                 if replicated else
                 "Did NOT clear the usual significance hurdle in later replication work; "
                 "treat as a negative or fragile result."),
            ],
            sources=[
                _src(f"{authors} ({year}){', ' + journal if journal else ''}",
                     author=authors, year=year),
                index_source,
            ],
            origin_creator=authors, origin_year=year,
            tags=["academic", "anomaly", "cross-sectional", economic],
            complexity="high", trade_frequency="low",
            instrument_types=["equity"], supported_markets=["US equities"],
        ))
    return out


# =========================================================================
# 3. Catalog-only families (honestly not runnable here)
# =========================================================================
def _catalog_only() -> list[TradingStrategy]:
    out: list[TradingStrategy] = []

    options = [
        ("covered-call", "Covered call", "Long stock plus a short out-of-the-money call."),
        ("protective-put", "Protective put", "Long stock plus a long put as insurance."),
        ("collar", "Collar", "Long stock, long put, short call to cap both tails."),
        ("cash-secured-put", "Cash-secured put", "Short put fully collateralised with cash."),
        ("bull-call-spread", "Bull call spread", "Long lower-strike call, short higher-strike call."),
        ("bear-put-spread", "Bear put spread", "Long higher-strike put, short lower-strike put."),
        ("calendar-spread", "Calendar spread", "Short near-dated, long far-dated option at one strike."),
        ("diagonal-spread", "Diagonal spread", "Calendar spread with different strikes."),
        ("iron-condor", "Iron condor", "Short strangle defined by two long wings."),
        ("iron-butterfly", "Iron butterfly", "Short straddle defined by two long wings."),
        ("butterfly", "Butterfly spread", "Long two wings, short two body options at one expiry."),
        ("straddle", "Long straddle", "Long call and put at the same strike and expiry."),
        ("strangle", "Long strangle", "Long out-of-the-money call and put."),
        ("ratio-spread", "Ratio spread", "Unequal long and short option quantities."),
        ("backspread", "Backspread", "Short near-the-money, long more far options."),
        ("synthetic-stock", "Synthetic long stock", "Long call plus short put at one strike."),
        ("conversion", "Conversion", "Long stock, long put, short call at one strike."),
        ("reversal", "Reversal", "Short stock, short put, long call at one strike."),
        ("box-spread", "Box spread", "Bull call spread plus bear put spread; a financing trade."),
        ("wheel", "The wheel", "Cash-secured puts, then covered calls after assignment."),
        ("volatility-crush", "Earnings volatility crush", "Short option premium into an earnings implied-volatility collapse."),
        ("delta-hedge", "Delta hedging", "Neutralise directional exposure of an option book."),
        ("gamma-scalping", "Gamma scalping", "Re-hedge a long-gamma book against realised moves."),
        ("dispersion", "Dispersion trade", "Short index volatility against long single-name volatility."),
        ("skew-trade", "Skew trade", "Trade the relative pricing of out-of-the-money puts and calls."),
        ("term-structure", "Volatility term structure", "Trade the slope of implied volatility across expiries."),
    ]
    for slug, name, blurb in options:
        out.append(_strategy(
            id=f"options.structure.{slug}", canonical_name=name, display_name=name,
            category="Options", subcategory="Option structure",
            description=f"{blurb} Catalogued for completeness; this project carries no options data.",
            thesis="Option structures reshape the payoff of an underlying view into a defined "
                   "risk profile, trading directional exposure for volatility and time exposure.",
            direction="long-short", timeframes=["1d"], holding_period="Days to months",
            data_requirements=["options chains", "implied volatility surface", "underlying OHLCV"],
            external_data_requirements=["option chain snapshots with strikes, expiries, greeks and "
                                        "bid/ask", "an options-aware fill and assignment simulator"],
            entry_rules=["Defined by the chosen strikes and expiries; not runnable without a chain."],
            exit_rules=["Defined by expiry, assignment or an early-close rule."],
            evidence_level="institutional", implementation_status="unsupported",
            unsupported_reason=("The paper engine simulates single-leg equity positions only. "
                                "There is no options chain, no greeks, no assignment model and no "
                                "multi-leg order support, so any result would be fabricated."),
            limitations=["Multi-leg option payoffs cannot be approximated with equity OHLCV."],
            tags=["options", "derivatives"], instrument_types=["equity option"],
            complexity="high", trade_frequency="medium",
            sources=[_src("Options as a Strategic Investment", author="Lawrence G. McMillan", year=1980),
                     _src("Characteristics and Risks of Standardized Options (OCC)",
                          "https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document",
                          author="Options Clearing Corporation")],
        ))

    execution = [
        ("twap", "TWAP", "Slice an order into equal pieces spaced evenly over a time window."),
        ("vwap", "VWAP", "Slice an order in proportion to a forecast intraday volume profile."),
        ("pov", "Percentage of volume", "Participate at a fixed share of live traded volume."),
        ("implementation-shortfall", "Implementation shortfall", "Balance market impact against the "
                                                                 "cost of delay versus the arrival price."),
        ("arrival-price", "Arrival price", "Benchmark execution to the price when the order arrived."),
        ("liquidity-seeking", "Liquidity seeking", "Opportunistically take displayed and hidden liquidity."),
        ("iceberg", "Iceberg order", "Display only part of the order size at a time."),
        ("close-auction", "Closing auction", "Route the order into the closing cross."),
        ("open-auction", "Opening auction", "Route the order into the opening cross."),
        ("dark-aggregation", "Dark aggregation", "Seek midpoint fills across non-displayed venues."),
        ("smart-order-routing", "Smart order routing", "Split a child order across venues by expected fill quality."),
    ]
    for slug, name, blurb in execution:
        out.append(_strategy(
            id=f"execution.algorithm.{slug}", canonical_name=name, display_name=name,
            category="Institutional execution", subcategory="Execution algorithm",
            description=f"{blurb} This is an execution method, not a source of alpha: it decides "
                        "how to fill a decision that has already been made.",
            thesis="Given a decision to trade a quantity, the execution problem is to minimise "
                   "the total of market impact, spread and the opportunity cost of delay.",
            direction="long-short", timeframes=["intraday"], holding_period="Minutes to one session",
            data_requirements=["intraday volume profile", "live order book"],
            external_data_requirements=["tick-level trades and quotes", "venue-level liquidity data"],
            entry_rules=["Not applicable: the parent order is an input, not a signal."],
            exit_rules=["The schedule completes when the parent quantity is filled."],
            evidence_level="institutional", implementation_status="unsupported",
            unsupported_reason=("Execution algorithms need tick data and a venue model. The engine "
                                "here fills at the next bar's open, so an execution schedule would "
                                "have nothing to optimise against."),
            limitations=["Classified as execution, not alpha; it should never be ranked on return."],
            tags=["execution", "institutional", "microstructure"],
            complexity="high", trade_frequency="very-high",
            sources=[_src("Optimal Execution of Portfolio Transactions",
                          author="Almgren and Chriss", year=2000),
                     _src("Implementation Shortfall", author="André Perold", year=1988)],
        ))

    external = [
        ("event.earnings-drift", "Post-earnings-announcement drift", "Event driven", "Earnings",
         "Buys positive earnings surprises and holds through the subsequent drift.",
         ["earnings announcement dates", "consensus estimates", "reported actuals"],
         [_src("Post-Earnings-Announcement Drift", author="Ball and Brown", year=1968),
          _src("Evidence on the Possible Underweighting of Earnings-Related Information",
               author="Bernard and Thomas", year=1989)], 1968),
        ("event.merger-arbitrage", "Merger arbitrage", "Event driven", "Corporate action",
         "Buys the target and hedges the acquirer after a deal is announced, earning the spread "
         "if the deal closes.",
         ["announced deal terms", "deal completion outcomes", "borrow availability"],
         [_src("Characteristics of Risk and Return in Risk Arbitrage",
               author="Mitchell and Pulvino", year=2001)], 2001),
        ("event.index-addition", "Index addition", "Event driven", "Index",
         "Buys ahead of an index inclusion and exits after the effective date.",
         ["index reconstitution announcements and effective dates"],
         [_src("Do Demand Curves for Stocks Slope Down?", author="Shleifer", year=1986)], 1986),
        ("event.insider-buying", "Insider buying", "Event driven", "Insider",
         "Follows open-market purchases disclosed by corporate insiders.",
         ["Form 4 insider transaction filings"],
         [_src("Estimating the Value of Insider Information", author="Jeng, Metrick and Zeckhauser",
               year=2003)], 2003),
        ("event.dividend-capture", "Dividend capture", "Event driven", "Dividend",
         "Holds across the ex-dividend date to collect the distribution.",
         ["dividend declaration, ex and pay dates", "withholding tax treatment"],
         [_src("The Ex-Dividend Day Behavior of Stock Prices",
               author="Elton and Gruber", year=1970)], 1970),
        ("statarb.pairs-distance", "Pairs trading (distance method)", "Statistical arbitrage", "Pairs",
         "Forms pairs by minimum historical price distance and trades divergence back to the mean.",
         ["a multi-symbol price panel", "a survivorship-free universe"],
         [_src("Pairs Trading: Performance of a Relative-Value Arbitrage Rule",
               author="Gatev, Goetzmann and Rouwenhorst", year=2006)], 2006),
        ("statarb.cointegration", "Cointegration pairs", "Statistical arbitrage", "Pairs",
         "Selects pairs by a cointegration test and trades the stationary spread.",
         ["a multi-symbol price panel", "cointegration test infrastructure"],
         [_src("Co-integration and Error Correction", author="Engle and Granger", year=1987)], 1987),
        ("statarb.residual-reversion", "Residual mean reversion", "Statistical arbitrage", "Market neutral",
         "Removes factor exposure, then fades the idiosyncratic residual.",
         ["a multi-symbol price panel", "a factor risk model"],
         [_src("Statistical Arbitrage in the US Equities Market",
               author="Avellaneda and Lee", year=2010)], 2010),
        ("sentiment.news", "News sentiment", "Sentiment and alternative data", "News",
         "Scores news text and trades the direction of the sentiment change.",
         ["timestamped news with point-in-time delivery", "a sentiment model"],
         [_src("More Than Words: Quantifying Language to Measure Firms' Fundamentals",
               author="Tetlock, Saar-Tsechansky and Macskassy", year=2008)], 2008),
        ("sentiment.social", "Social-media sentiment", "Sentiment and alternative data", "Social",
         "Trades aggregated retail sentiment from social platforms.",
         ["licensed social-media firehose with timestamps"],
         [_src("Twitter mood predicts the stock market",
               "https://arxiv.org/abs/1010.3003", "Bollen, Mao and Zeng", 2011)], 2011),
        ("sentiment.search-trends", "Search-trend signals", "Sentiment and alternative data", "Web",
         "Uses changes in search interest as an attention proxy.",
         ["point-in-time search-volume index"],
         [_src("In Search of Attention", author="Da, Engelberg and Gao", year=2011)], 2011),
        ("sentiment.short-interest", "Short interest", "Sentiment and alternative data", "Positioning",
         "Sorts on reported short interest as a positioning and constraint proxy.",
         ["semi-monthly exchange short-interest reports"],
         [_src("Short Interest, Institutional Ownership, and Stock Returns",
               author="Asquith, Pathak and Ritter", year=2005)], 2005),
        ("sentiment.put-call", "Put-call ratio", "Sentiment and alternative data", "Options flow",
         "Uses the ratio of put to call volume as a contrarian sentiment gauge.",
         ["exchange option volume by class"],
         [_src("The Information in Option Volume for Future Stock Prices",
               author="Pan and Poteshman", year=2006)], 2006),
    ]
    for slug, name, category, sub, blurb, needs, sources, year in external:
        out.append(_strategy(
            id=f"{slug.split('.')[0]}.{sub.lower().replace(' ', '-')}.{slug.split('.')[1]}",
            canonical_name=name, display_name=name, category=category, subcategory=sub,
            description=f"{blurb} The rule is well documented; the data it needs is not present here.",
            thesis="A documented, economically motivated predictor whose inputs are external to "
                   "price history.",
            direction="long-short", timeframes=["1d", "1mo"], holding_period="Days to months",
            data_requirements=["daily OHLCV"] + list(needs),
            external_data_requirements=list(needs),
            entry_rules=["Defined by the cited source; requires the external dataset to evaluate."],
            exit_rules=["Defined by the cited source's holding period."],
            evidence_level="academic", implementation_status="requires-data",
            limitations=["Cannot be run here without the listed data; no approximation is offered "
                         "because a price-only proxy would not be the same strategy."],
            sources=list(sources), origin_year=year,
            tags=[category.lower(), sub.lower()], complexity="high", trade_frequency="medium",
        ))

    ml = [
        ("linear-regression", "Linear regression forecast", "Fits a linear model of next-period return on lagged features."),
        ("logistic-regression", "Logistic direction classifier", "Classifies the sign of the next return."),
        ("decision-tree", "Decision tree", "A single interpretable tree over engineered features."),
        ("random-forest", "Random forest", "Bagged trees over price and volume features."),
        ("gradient-boosting", "Gradient boosting", "Boosted trees, the workhorse of tabular return prediction."),
        ("xgboost", "XGBoost model", "Regularised gradient boosting."),
        ("lightgbm", "LightGBM model", "Histogram-based gradient boosting."),
        ("svm", "Support vector machine", "Margin classifier over standardised features."),
        ("knn", "k-nearest neighbours", "Matches the current state to similar historical states."),
        ("clustering", "Clustering regimes", "Unsupervised grouping of market states."),
        ("hmm", "Hidden Markov regime model", "Infers latent market regimes and conditions exposure."),
        ("bayesian", "Bayesian shrinkage model", "Posterior-mean forecasts with explicit priors."),
        ("mlp", "Feed-forward neural network", "A dense network over engineered features."),
        ("lstm", "LSTM sequence model", "Recurrent network over price sequences."),
        ("gru", "GRU sequence model", "Gated recurrent network over price sequences."),
        ("tcn", "Temporal convolutional network", "Dilated causal convolutions over sequences."),
        ("transformer", "Transformer sequence model", "Attention over a window of past bars."),
        ("gnn", "Graph neural network", "Models cross-asset relationships as a graph."),
        ("autoencoder", "Autoencoder factors", "Learns a low-dimensional representation of returns."),
        ("anomaly-detection", "Anomaly detection", "Flags statistically unusual market states."),
        ("reinforcement-learning", "Reinforcement learning agent", "Learns a position policy from reward."),
        ("contextual-bandit", "Contextual bandit", "Balances exploration against exploitation of signals."),
        ("ensemble", "Model ensemble", "Blends several base models."),
        ("meta-labeling", "Meta-labelling", "A second model decides whether to act on a primary signal."),
        ("triple-barrier", "Triple-barrier labelling", "Labels outcomes by profit, loss and time barriers."),
        ("nlp-event", "NLP event extraction", "Extracts structured events from filings and news."),
    ]
    lopez = _src("Advances in Financial Machine Learning", author="Marcos López de Prado", year=2018)
    gu = _src("Empirical Asset Pricing via Machine Learning",
              "https://doi.org/10.1093/rfs/hhaa009", "Gu, Kelly and Xiu", 2020)
    for slug, name, blurb in ml:
        out.append(_strategy(
            id=f"machine-learning.model.{slug}", canonical_name=name, display_name=name,
            category="Machine learning and AI", subcategory="Model class",
            description=f"{blurb} Catalogued as a model class; it is not shipped as a fitted, "
                        "runnable strategy because an unvalidated fitted model is indistinguishable "
                        "from an overfitted one.",
            thesis="A learned function may capture non-linear structure that fixed rules miss, at "
                   "the cost of far greater overfitting and leakage risk.",
            direction="long-short", timeframes=["1d"], holding_period="Days to months",
            data_requirements=["daily OHLCV", "engineered features with explicit availability times"],
            external_data_requirements=[
                "a training/validation/test split fixed before any evaluation",
                "point-in-time features with a stated availability lag",
                "a documented retraining schedule and leakage audit",
            ],
            entry_rules=["Act on the model's out-of-sample prediction only, never on in-sample fit."],
            exit_rules=["Defined by the labelling scheme, for example a triple-barrier horizon."],
            evidence_level="experimental", implementation_status="requires-data",
            limitations=[
                "Shipping a pre-fitted model without its full training provenance would be a "
                "leakage claim this project cannot support.",
                "Every ML entry must declare feature availability time, training window, validation "
                "window, test window, retraining cadence and transaction costs before it can be run.",
            ],
            sources=[lopez, gu], origin_year=2018,
            tags=["machine learning", "ai", slug], complexity="high", trade_frequency="medium",
        ))

    risk = [
        ("fixed-percent-stop", "Fixed percentage stop", "Exit at a fixed percentage adverse move."),
        ("atr-stop", "ATR stop", "Exit at a multiple of average true range."),
        ("trailing-stop", "Trailing stop", "Ratchet the stop behind the best price reached."),
        ("chandelier-exit", "Chandelier exit", "Trail from the highest high by an ATR multiple."),
        ("break-even-stop", "Break-even stop", "Move the stop to entry once a threshold gain is reached."),
        ("time-stop", "Time stop", "Exit after a fixed number of bars regardless of price."),
        ("volatility-target", "Volatility targeting", "Scale exposure to hold realised volatility constant."),
        ("fixed-fractional", "Fixed fractional sizing", "Risk a constant fraction of equity per trade."),
        ("kelly", "Kelly criterion", "Size by the growth-optimal fraction implied by edge and odds."),
        ("fractional-kelly", "Fractional Kelly", "A scaled-down Kelly fraction to reduce variance."),
        ("inverse-volatility", "Inverse-volatility sizing", "Weight inversely to recent volatility."),
        ("risk-parity", "Risk parity", "Equalise risk contribution across holdings."),
        ("equal-risk", "Equal-risk sizing", "Size each position to an identical stop-loss amount."),
        ("daily-loss-limit", "Daily loss limit", "Stop trading for the session after a loss threshold."),
        ("drawdown-stop", "Portfolio drawdown stop", "Cut exposure after an account-level drawdown."),
        ("position-cap", "Maximum position cap", "Hard cap on any single position's weight."),
        ("correlation-cap", "Correlation cap", "Limit combined exposure to correlated holdings."),
        ("sector-cap", "Sector exposure cap", "Limit exposure to any one sector."),
    ]
    for slug, name, blurb in risk:
        out.append(_strategy(
            id=f"risk.method.{slug}", canonical_name=name, display_name=name,
            category="Risk and exit methods", subcategory="Reusable overlay",
            description=f"{blurb} This is a reusable risk overlay rather than a standalone strategy; "
                        "several of these are exposed directly as parameters on executable entries.",
            thesis="Exit and sizing rules usually change a strategy's risk-adjusted result more "
                   "than its entry rule does.",
            direction="long-short", timeframes=["1d"], holding_period="Depends on host strategy",
            data_requirements=["daily OHLCV"],
            entry_rules=["Not applicable: an overlay has no entry of its own."],
            exit_rules=[blurb],
            evidence_level="community", implementation_status="research-only",
            limitations=["Ranking an overlay on return alone is meaningless without a host strategy."],
            tags=["risk", "exit", "sizing"], complexity="low", trade_frequency="medium",
            sources=[_src("Portfolio Management Formulas", author="Ralph Vince", year=1990),
                     _src("A New Interpretation of Information Rate", author="J. L. Kelly Jr.", year=1956)],
        ))

    fundamental = [
        ("magic-formula", "Magic Formula", "Ranks on earnings yield and return on capital combined.",
         _src("The Little Book That Beats the Market", author="Joel Greenblatt", year=2005), 2005),
        ("piotroski-f", "Piotroski F-score", "Nine binary accounting tests of financial strength.",
         _src("Value Investing: The Use of Historical Financial Statement Information",
              author="Joseph Piotroski", year=2000), 2000),
        ("altman-z", "Altman Z-score", "A bankruptcy-risk score used as a quality screen.",
         _src("Financial Ratios, Discriminant Analysis and the Prediction of Corporate Bankruptcy",
              author="Edward Altman", year=1968), 1968),
        ("can-slim", "CAN SLIM", "Growth screen combining earnings acceleration and relative strength.",
         _src("How to Make Money in Stocks", author="William J. O'Neil", year=1988), 1988),
        ("dogs-of-the-dow", "Dogs of the Dow", "Buys the highest-yielding Dow components annually.",
         _src("Beating the Dow", author="Michael O'Higgins", year=1991), 1991),
        ("net-net", "Net-net working capital", "Buys below net current asset value.",
         _src("Security Analysis", author="Benjamin Graham and David Dodd", year=1934), 1934),
        ("quality-minus-junk", "Quality minus junk", "Long high-quality, short low-quality firms.",
         _src("Quality Minus Junk", author="Asness, Frazzini and Pedersen", year=2019), 2019),
        ("betting-against-beta", "Betting against beta", "Long low-beta, short high-beta, leverage adjusted.",
         _src("Betting Against Beta", author="Frazzini and Pedersen", year=2014), 2014),
        ("deep-value", "Deep value", "Buys the cheapest decile on a valuation multiple.",
         _src("The Cross-Section of Expected Stock Returns", author="Fama and French", year=1992), 1992),
        ("dividend-growth", "Dividend growth", "Buys firms with a long record of raising dividends.",
         _src("Surprise! Higher Dividends = Higher Earnings Growth",
              author="Arnott and Asness", year=2003), 2003),
        ("shareholder-yield", "Shareholder yield", "Combines dividends, buybacks and debt paydown.",
         _src("Shareholder Yield: A Better Approach to Dividend Investing",
              author="Meb Faber", year=2013), 2013),
        ("garp", "Growth at a reasonable price", "Balances growth against valuation.",
         _src("One Up On Wall Street", author="Peter Lynch", year=1989), 1989),
        ("accrual-anomaly", "Accrual anomaly", "Shorts high-accrual firms, buys low-accrual firms.",
         _src("Do Stock Prices Fully Reflect Information in Accruals and Cash Flows?",
              author="Richard Sloan", year=1996), 1996),
        ("gross-profitability", "Gross profitability", "Gross profits scaled by total assets.",
         _src("The Other Side of Value: The Gross Profitability Premium",
              author="Robert Novy-Marx", year=2013), 2013),
    ]
    for slug, name, blurb, source, year in fundamental:
        out.append(_strategy(
            id=f"fundamental.screen.{slug}", canonical_name=name, display_name=name,
            category="Fundamental", subcategory="Screen",
            description=f"{blurb} The ranking rule is public and precise; the inputs are company "
                        "financials this project does not carry.",
            thesis="Firm characteristics drawn from financial statements have been associated with "
                   "differences in long-horizon average returns.",
            direction="long", timeframes=["1mo", "1y"], holding_period="Months to years",
            data_requirements=["annual and quarterly financial statements", "a point-in-time universe"],
            external_data_requirements=[
                "point-in-time fundamentals with report dates (not restated figures)",
                "a survivorship-free universe including delisted companies",
            ],
            entry_rules=["Rank the universe on the published formula at each rebalance date."],
            exit_rules=["Replace holdings at the next scheduled rebalance."],
            evidence_level="academic", implementation_status="requires-data",
            limitations=["Using restated fundamentals instead of point-in-time data would create "
                         "look-ahead bias and inflate every result."],
            sources=[source], origin_creator=source.author, origin_year=year,
            tags=["fundamental", "value", "screen"], complexity="high", trade_frequency="very-low",
        ))

    arbitrage = [
        ("index-arbitrage", "Index arbitrage", "futures and cash index quotes with sub-second timestamps"),
        ("etf-arbitrage", "ETF creation/redemption arbitrage", "ETF NAV, basket composition and creation-unit access"),
        ("adr-arbitrage", "ADR arbitrage", "synchronised foreign-listing quotes and FX"),
        ("dual-listed", "Dual-listed arbitrage", "synchronised quotes on both listings"),
        ("closed-end-fund", "Closed-end fund arbitrage", "fund NAV and market price history"),
        ("convertible-arbitrage", "Convertible arbitrage", "convertible bond terms and borrow"),
        ("capital-structure", "Capital-structure arbitrage", "bond, CDS and equity quotes for one issuer"),
        ("cross-exchange", "Cross-exchange arbitrage", "consolidated quotes across venues with latency data"),
    ]
    for slug, name, needs in arbitrage:
        out.append(_strategy(
            id=f"arbitrage.relative-value.{slug}", canonical_name=name, display_name=name,
            category="Arbitrage", subcategory="Relative value",
            description=f"{name} exploits a price difference between instruments that are linked by "
                        "an enforceable relationship.",
            thesis="Two claims on the same economics must converge; the trade earns the spread "
                   "between them, subject to financing and execution risk.",
            direction="market-neutral", timeframes=["intraday"], holding_period="Seconds to weeks",
            data_requirements=[needs],
            external_data_requirements=[needs, "co-located execution with realistic latency"],
            entry_rules=["Defined by the observed basis exceeding financing and execution costs."],
            exit_rules=["Convergence, expiry or a hard risk limit."],
            evidence_level="institutional", implementation_status="unsupported",
            unsupported_reason=("This class is latency- and access-sensitive. Simulating it on daily "
                                "bars would produce a profitable-looking result that no ordinary "
                                "participant could capture, which would be misleading."),
            limitations=["Most of this opportunity set is unavailable without institutional access."],
            tags=["arbitrage", "market neutral", "institutional"],
            complexity="high", trade_frequency="very-high",
            sources=[_src("Limits of Arbitrage", author="Shleifer and Vishny", year=1997)],
        ))
    return out


# =========================================================================
# Registry
# =========================================================================
class Catalog:
    def __init__(self, strategies: list[TradingStrategy]) -> None:
        self.strategies = strategies
        self.by_id: dict[str, TradingStrategy] = {}
        for item in strategies:
            if item.id in self.by_id:
                raise ValueError(f"duplicate strategy id: {item.id}")
            validate(item)
            self.by_id[item.id] = item
        self._search: dict[str, str] = {s.id: s.search_text() for s in strategies}
        self._alias: dict[str, str] = {}
        for item in strategies:
            for alias in item.aliases:
                self._alias.setdefault(alias.strip().lower(), item.id)

    def __len__(self) -> int:
        return len(self.strategies)

    def get(self, strategy_id: str) -> TradingStrategy:
        try:
            return self.by_id[strategy_id]
        except KeyError as exc:
            raise KeyError(f"unknown strategy: {strategy_id}") from exc

    def resolve_alias(self, name: str) -> str | None:
        key = name.strip().lower()
        if key in self.by_id:
            return key
        return self._alias.get(key)

    def executable(self) -> list[TradingStrategy]:
        return [s for s in self.strategies if s.is_executable]

    def search_index(self, strategy_id: str) -> str:
        return self._search[strategy_id]

    def stats(self) -> dict[str, Any]:
        def tally(attr: str) -> dict[str, int]:
            counts: dict[str, int] = {}
            for item in self.strategies:
                counts[getattr(item, attr)] = counts.get(getattr(item, attr), 0) + 1
            return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

        timeframes: dict[str, int] = {}
        for item in self.strategies:
            for frame in item.timeframes:
                timeframes[frame] = timeframes.get(frame, 0) + 1
        canonical = len({s.parent_id or s.id for s in self.strategies})
        years = [s.origin_year for s in self.strategies if s.origin_year]
        return {
            "total": len(self.strategies),
            "canonical_families": canonical,
            "variations": len(self.strategies) - canonical,
            "executable": sum(1 for s in self.strategies if s.implementation_status == "executable"),
            "requires_data": sum(1 for s in self.strategies if s.implementation_status == "requires-data"),
            "research_only": sum(1 for s in self.strategies if s.implementation_status == "research-only"),
            "unsupported": sum(1 for s in self.strategies if s.implementation_status == "unsupported"),
            "by_category": tally("category"),
            "by_status": tally("implementation_status"),
            "by_evidence": tally("evidence_level"),
            "by_direction": tally("direction"),
            "by_complexity": tally("complexity"),
            "by_timeframe": dict(sorted(timeframes.items(), key=lambda kv: -kv[1])),
            "oldest_year": min(years) if years else None,
            "newest_year": max(years) if years else None,
            "research_date": RESEARCH_DATE,
            "rule_engines": 0,
        }


@functools.lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    from .rules import RULES
    strategies = _curated() + _academic() + _catalog_only()
    catalog = Catalog(strategies)
    missing = {s.rule_id for s in catalog.executable()} - set(RULES)
    if missing:
        raise ValueError(f"executable strategies reference unknown rule ids: {sorted(missing)}")
    return catalog
