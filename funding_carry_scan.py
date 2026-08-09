"""Delta-neutral funding-carry scanner (measurement only, no order code).

Cash-and-carry is the one mechanically sound edge left in liquid crypto: buy spot,
short the matching perpetual, hold delta-flat and collect funding. The trade is real,
but a raw funding screen is misleading in two specific ways, and this module exists to
correct both:

1. **Fees are paid up front, funding accrues slowly.** A perp paying the +0.01%/8h
   floor annualizes to a headline ~11%/yr, but a round trip costs ~30 bps across four
   legs. You must hold roughly ten days just to break even. `break_even_hours` states
   that cost in the unit that matters.
2. **A high rate is not a durable rate.** A name printing 160%/yr today may have been
   negative yesterday. `stability` reports the realized distribution of recent funding
   prints so a spike is never mistaken for a yield.

Every reported figure is net of modelled fees. This module contains no private
endpoint, no credential handling, and cannot place an order; it ranks opportunities so
a human can decide whether any of them justify capital.
"""

from __future__ import annotations

import asyncio
import math
import statistics
from typing import Any

import aiohttp


NAME = "Funding Carry Scanner"

BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"
BINANCE_SPOT = "https://api.binance.com/api/v3"
REQUEST_TIMEOUT_SEC = 20

# Binance VIP-0 published taker fees. Both legs are crossed on entry and again on
# exit, so a completed carry pays each of these twice.
SPOT_TAKER_FEE_RATE = 0.0010
PERP_TAKER_FEE_RATE = 0.0005

FUNDING_INTERVAL_HOURS = 8.0
PERIODS_PER_YEAR = 365 * 24 / FUNDING_INTERVAL_HOURS

# A carry leg that cannot be exited without moving the book is not a carry leg.
MIN_PERP_24H_VOLUME_USD = 20_000_000.0
FUNDING_HISTORY_LIMIT = 30          # ~10 days of 8h prints
MAX_HISTORY_LOOKUPS = 15

# The fee is a one-off, so the assumed hold dominates every net figure. A week is
# short enough that the round trip alone turns floor-rate majors negative, which
# flatters nothing but misrepresents a trade that is normally held for months.
DEFAULT_HOLD_HOURS = 24 * 30
HOLD_SENSITIVITY_DAYS = (7, 14, 30, 60, 90, 180, 365)


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def round_trip_cost_bps() -> float:
    """Total modelled cost of opening and closing both legs, in basis points."""
    return (2 * SPOT_TAKER_FEE_RATE + 2 * PERP_TAKER_FEE_RATE) * 1e4


def annualized_bps(funding_per_8h: float) -> float:
    """Headline annualized carry, before any fee amortization."""
    return funding_per_8h * PERIODS_PER_YEAR * 1e4


def break_even_hours(funding_per_8h: float,
                     cost_bps: float | None = None) -> float:
    """Hours the position must be held for accrued funding to repay the round trip.

    Returns infinity when funding is zero or points the wrong way, because no holding
    period recovers the cost in that case.
    """
    cost_bps = round_trip_cost_bps() if cost_bps is None else cost_bps
    earned_bps_per_period = funding_per_8h * 1e4
    if earned_bps_per_period <= 0:
        return math.inf
    return cost_bps / earned_bps_per_period * FUNDING_INTERVAL_HOURS


def net_annualized_bps(funding_per_8h: float, hold_hours: float,
                       cost_bps: float | None = None) -> float:
    """Annualized return net of the round trip, assuming a fixed holding period.

    The fee is a one-off, so its drag depends entirely on how long the position is
    held; a rate that looks excellent annualized can still be negative over a
    realistic hold.
    """
    cost_bps = round_trip_cost_bps() if cost_bps is None else cost_bps
    if hold_hours <= 0:
        return -cost_bps * PERIODS_PER_YEAR
    periods = hold_hours / FUNDING_INTERVAL_HOURS
    earned_bps = funding_per_8h * 1e4 * periods
    net_bps = earned_bps - cost_bps
    return net_bps * (365 * 24 / hold_hours)


def stability(history: list[float]) -> dict:
    """Realized distribution of recent funding prints.

    `positive_fraction` is the headline guard: a name that only pays two thirds of the
    time is not yielding its current rate, whatever the current rate says.
    """
    values = [_number(value) for value in history if _number(value) != 0.0]
    if not values:
        return {"samples": 0, "mean_bps": 0.0, "stdev_bps": 0.0,
                "positive_fraction": 0.0, "min_bps": 0.0, "worst_ann_bps": 0.0}
    mean = statistics.fmean(values)
    return {
        "samples": len(values),
        "mean_bps": mean * 1e4,
        "stdev_bps": (statistics.pstdev(values) * 1e4) if len(values) > 1 else 0.0,
        "positive_fraction": sum(1 for value in values if value > 0) / len(values),
        "min_bps": min(values) * 1e4,
        # What the realized mean would have paid, which is the honest expectation.
        "worst_ann_bps": annualized_bps(min(values)),
    }


def dollars_per_month(net_ann_bps: float, hedged_notional_usd: float) -> float:
    return hedged_notional_usd * net_ann_bps / 1e4 / 12.0


def hedged_notional(bankroll_usd: float, perp_margin_fraction: float = 0.5) -> float:
    """Carry notional a bankroll supports.

    Spot must be paid for in full; the short perp only posts margin. With margin at
    half the notional, a bankroll of B supports B / (1 + margin_fraction) per leg.
    """
    if bankroll_usd <= 0 or perp_margin_fraction < 0:
        return 0.0
    return bankroll_usd / (1.0 + perp_margin_fraction)


def hold_sensitivity(funding_per_8h: float,
                     hedged_notional_usd: float) -> list[dict]:
    """Net carry across candidate holding periods.

    Reported because a single assumed hold is the most misleading number a carry
    screen can print: the same rate is negative over a week and positive over a
    quarter, purely from amortizing one round trip.
    """
    rows = []
    for days in HOLD_SENSITIVITY_DAYS:
        net = net_annualized_bps(funding_per_8h, days * 24)
        rows.append({
            "days": days,
            "net_ann_bps": net,
            "dollars_per_year": hedged_notional_usd * net / 1e4,
        })
    return rows


def rank_candidates(perps: dict[str, float], volumes: dict[str, float],
                    spot_symbols: set[str], *, hold_hours: float = DEFAULT_HOLD_HOURS,
                    min_volume_usd: float = MIN_PERP_24H_VOLUME_USD) -> list[dict]:
    """Rank spot-hedgeable perps by carry that survives fees over `hold_hours`."""
    rows = []
    for symbol, funding in perps.items():
        if symbol not in spot_symbols:
            continue                      # no spot leg means no delta-neutral hedge
        volume = _number(volumes.get(symbol))
        if volume < min_volume_usd:
            continue
        funding = _number(funding)
        if funding <= 0:
            continue                      # a payer is required to be short the perp
        rows.append({
            "symbol": symbol,
            "funding_per_8h": funding,
            "headline_ann_bps": annualized_bps(funding),
            "net_ann_bps": net_annualized_bps(funding, hold_hours),
            "break_even_hours": break_even_hours(funding),
            "perp_24h_volume_usd": volume,
        })
    rows.sort(key=lambda row: -row["net_ann_bps"])
    return rows


async def _json(session: aiohttp.ClientSession, url: str,
                params: dict | None = None) -> Any:
    async with session.get(url, params=params) as response:
        if response.status != 200:
            body = (await response.text())[:120]
            raise RuntimeError(f"HTTP {response.status}: {body}")
        return await response.json()


async def fetch_universe(session: aiohttp.ClientSession) -> tuple[dict, dict, set]:
    premium, tickers, exchange_info = await asyncio.gather(
        _json(session, f"{BINANCE_FAPI}/premiumIndex"),
        _json(session, f"{BINANCE_FAPI}/ticker/24hr"),
        _json(session, f"{BINANCE_SPOT}/exchangeInfo"),
    )
    perps = {row["symbol"]: _number(row.get("lastFundingRate"))
             for row in premium if str(row.get("symbol", "")).endswith("USDT")}
    volumes = {row["symbol"]: _number(row.get("quoteVolume")) for row in tickers}
    spot = {row["symbol"] for row in exchange_info.get("symbols", [])
            if row.get("status") == "TRADING"}
    return perps, volumes, spot


async def fetch_funding_history(session: aiohttp.ClientSession,
                                symbol: str) -> list[float]:
    rows = await _json(session, f"{BINANCE_FAPI}/fundingRate",
                       {"symbol": symbol, "limit": FUNDING_HISTORY_LIMIT})
    return [_number(row.get("fundingRate")) for row in rows or []]


async def scan(bankroll_usd: float = 100.0, hold_hours: float = DEFAULT_HOLD_HOURS,
               top_n: int = MAX_HISTORY_LOOKUPS) -> dict:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        perps, volumes, spot = await fetch_universe(session)
        ranked = rank_candidates(perps, volumes, spot, hold_hours=hold_hours)
        head = ranked[:top_n]
        histories = await asyncio.gather(
            *(fetch_funding_history(session, row["symbol"]) for row in head),
            return_exceptions=True,
        )

    notional = hedged_notional(bankroll_usd)
    for row, history in zip(head, histories):
        row["stability"] = (stability(history)
                            if not isinstance(history, BaseException)
                            else stability([]))
        # Expectation built on the realized mean, not on today's single print.
        realized = row["stability"]["mean_bps"] / 1e4
        row["realized_net_ann_bps"] = net_annualized_bps(realized, hold_hours)
        row["dollars_per_month"] = dollars_per_month(
            row["realized_net_ann_bps"], notional)
    # Sensitivity is built on the best REALIZED mean, not the best current print,
    # so the headline case is not a spike that has already passed.
    best = max(head, key=lambda row: row["realized_net_ann_bps"], default=None)
    sensitivity = (hold_sensitivity(best["stability"]["mean_bps"] / 1e4, notional)
                   if best else [])
    return {
        "best_symbol": best["symbol"] if best else "",
        "hold_sensitivity": sensitivity,
        "bankroll_usd": bankroll_usd,
        "hedged_notional_usd": notional,
        "hold_hours": hold_hours,
        "round_trip_cost_bps": round_trip_cost_bps(),
        "candidates": head,
        "universe_size": len(perps),
        "note": ("Measurement only. No order is placed and no private endpoint "
                 "exists in this module."),
    }


def _format(result: dict) -> str:
    lines = [
        f"Funding carry scan  |  {result['universe_size']} perps  |  "
        f"round trip costs {result['round_trip_cost_bps']:.0f} bps",
        f"Bankroll ${result['bankroll_usd']:,.0f} supports "
        f"${result['hedged_notional_usd']:,.0f} of hedged notional "
        f"(spot paid in full, perp on 2x margin)",
        f"Net figures assume a {result['hold_hours'] / 24:.0f}-day hold.",
        "",
        f"{'symbol':<13}{'now/8h':>9}{'headline':>10}{'net':>9}"
        f"{'breakeven':>11}{'pos%':>7}{'realized':>10}{'$/mo':>9}",
        "-" * 78,
    ]
    for row in result["candidates"]:
        stats = row.get("stability", {})
        breakeven = row["break_even_hours"]
        lines.append(
            f"{row['symbol']:<13}"
            f"{row['funding_per_8h'] * 100:>8.4f}%"
            f"{row['headline_ann_bps'] / 100:>9.1f}%"
            f"{row['net_ann_bps'] / 100:>8.1f}%"
            f"{breakeven:>10.1f}h"
            f"{stats.get('positive_fraction', 0) * 100:>6.0f}%"
            f"{row.get('realized_net_ann_bps', 0) / 100:>9.1f}%"
            f"{row.get('dollars_per_month', 0):>9.2f}"
        )
    lines += [
        "",
        "headline = today's print annualized, before fees (what most screens show)",
        "net      = same rate net of the round trip over the assumed hold",
        "realized = the recent MEAN print net of fees, which is the honest expectation",
        "pos%     = share of recent prints that actually paid; low means it flips",
    ]
    if result.get("hold_sensitivity"):
        lines += [
            "",
            f"Hold sensitivity for {result['best_symbol']} at its realized mean "
            f"(one 30 bps round trip amortized):",
            f"  {'hold':>7}{'net %/yr':>12}{'$/yr':>10}",
        ]
        for row in result["hold_sensitivity"]:
            lines.append(f"  {row['days']:>6}d{row['net_ann_bps'] / 100:>11.2f}%"
                         f"{row['dollars_per_year']:>10.2f}")
    return "\n".join(lines)


def main() -> None:
    print(_format(asyncio.run(scan())))


if __name__ == "__main__":
    main()
