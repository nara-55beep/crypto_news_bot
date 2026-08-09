"""Airdrop Radar - finds tokenless protocols, scores them, and sizes a small bankroll.

Airdrop farming is the one strategy in this repo whose payoff does not scale linearly
with capital. Carry, arbitrage and market making all pay a percentage, so a $100
account earns $100-sized money. Airdrop allocation is typically logarithmic, so
crossing a protocol's minimum activity threshold captures a large share of what a much
bigger wallet receives. That is why this scanner exists for a small bankroll.

Rather than scraping "upcoming airdrop" listicles, which are unverifiable and often
paid placements, this ranks a hard, checkable signal: protocols that hold real TVL,
sit in a category that historically rewards users, and have **no token yet**. A
protocol with capital at risk and no ticker has an unresolved incentive to distribute
one. Everything is derived from one DeFiLlama call, so a full 8,000-protocol sweep
takes a couple of seconds.

Two honesty constraints are wired in rather than left to the caller:

* Scam screening is a heuristic over objective fields (audits, TVL, age, disclosure),
  never a guarantee. `risk_band` says SPECULATIVE when the evidence is thin.
* The dollar projection is a model, not a measurement. Every assumption is a named
  module constant so it can be argued with, and `assumptions()` reports them alongside
  the number. The realistic downside - most farms pay nothing - is reported as
  prominently as the upside.

This module holds no key, signs nothing, and cannot move funds. It reads public data
and tells a human where to spend their own time.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import aiohttp


NAME = "Airdrop Radar"
LLAMA_PROTOCOLS_URL = "https://api.llama.fi/protocols"
REQUEST_TIMEOUT_SEC = 25

MIN_TVL_USD = 2_000_000.0
MONTH_SECONDS = 30 * 24 * 3600

# Categories whose users are customers or counterparties rather than an ecosystem to
# bootstrap. Exchanges, wrappers and canonical bridges do not airdrop to depositors.
EXCLUDED_CATEGORIES = frozenset({
    "CEX", "Bridge", "Canonical Bridge", "RWA", "Chain", "Staking Pool",
    "Liquid Staking", "Risk Curators", "Onchain Capital Allocator",
    "Treasury Manager", "Managed Token Pools", "Basis Trading", "Anchor BTC",
    "Stablecoin Wrapper", "Restaked BTC", "Yield Aggregator",
    # Infrastructure and regulated venues: real TVL, but their users are
    # customers of a business, not an ecosystem being bootstrapped. Prediction
    # markets and restaking collateral layers are deliberately NOT excluded -
    # Polymarket and Symbiotic are among the most credible open candidates.
    "Payments", "OTC Marketplace", "RWA Lending", "CeDeFi",
})

# --- Legitimacy model -------------------------------------------------------
# Weights are deliberately blunt. Each maps to a fact anyone can re-check on
# DeFiLlama, so a disputed score can be traced to the field that produced it.
SCORE_AUDIT_FULL = 25          # two or more published audits
SCORE_AUDIT_PARTIAL = 12
SCORE_TVL_MAX = 30             # log-scaled between MIN_TVL_USD and this ceiling
TVL_SCORE_CEILING_USD = 500_000_000.0
SCORE_AGE_MATURE = 20          # listed 12+ months ago
SCORE_AGE_ESTABLISHED = 12     # 6-12 months
SCORE_AGE_YOUNG = 6            # 3-6 months
SCORE_HAS_SITE = 10
SCORE_HAS_SOCIAL = 5
SCORE_MULTICHAIN = 10
PENALTY_TVL_SPIKE = 15         # a >300% weekly jump is mercenary or wash capital
TVL_SPIKE_THRESHOLD_PCT = 300.0

BAND_STRONG = 70
BAND_MODERATE = 50
BAND_SPECULATIVE = 30

# --- Payoff model -----------------------------------------------------------
# Anchored on reported 2024-25 outcomes ($500-$5,000 for active users on major
# drops) and deliberately discounted. These are assumptions, not measurements.
BASE_ALLOCATION_FLOOR_USD = 40.0      # a small drop that clears the threshold
BASE_ALLOCATION_CEILING_USD = 1_200.0  # a major drop, active-user tier
# Allocation is logarithmic in wallet activity, not flat: a small wallet is not
# penalised proportionally, but it is still in the bottom tier. Reported
# $500-$5,000 outcomes belong to wallets funded near this reference, so a $33
# deposit must be discounted against it rather than assumed equivalent.
DEPOSIT_REFERENCE_USD = 1_000.0
DEPOSIT_FLOOR_USD = 10.0
# Most tokenless protocols never distribute anything, and many that do exclude
# minimum-size wallets. Probability is capped well below certainty at every score.
P_AIRDROP_MAX = 0.35
P_AIRDROP_MIN = 0.03
# 88% of airdropped tokens lose value within three months, so a claimed
# allocation is not realised value.
VALUE_RETENTION = 0.30
# Share of small wallets lost to Sybil filters and minimum-size cutoffs.
QUALIFICATION_RATE = 0.65


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def is_tokenless(protocol: dict) -> bool:
    """True when a protocol has no live token, which is what makes a drop possible."""
    symbol = _text(protocol.get("symbol"))
    if symbol not in ("", "-"):
        return False
    return not protocol.get("gecko_id") and not protocol.get("cmcId")


def is_eligible(protocol: dict, min_tvl_usd: float = MIN_TVL_USD) -> bool:
    if not is_tokenless(protocol):
        return False
    if _number(protocol.get("tvl")) < min_tvl_usd:
        return False
    return _text(protocol.get("category")) not in EXCLUDED_CATEGORIES


def age_months(protocol: dict, now: float | None = None) -> float:
    listed = _number(protocol.get("listedAt"))
    if listed <= 0:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, (now - listed) / MONTH_SECONDS)


def legitimacy_score(protocol: dict, now: float | None = None) -> tuple[float, list[str]]:
    """Score 0-100 from checkable fields, with the reason for each contribution."""
    score = 0.0
    signals: list[str] = []

    audits = int(_number(protocol.get("audits")))
    if audits >= 2:
        score += SCORE_AUDIT_FULL
        signals.append(f"{audits} published audits")
    elif audits == 1:
        score += SCORE_AUDIT_PARTIAL
        signals.append("1 published audit")
    else:
        signals.append("no published audit")

    tvl = _number(protocol.get("tvl"))
    if tvl > 0:
        span = math.log10(max(TVL_SCORE_CEILING_USD / MIN_TVL_USD, 10.0))
        ratio = math.log10(max(tvl / MIN_TVL_USD, 1.0)) / span
        score += SCORE_TVL_MAX * min(1.0, ratio)
        signals.append(f"${tvl:,.0f} TVL at risk")

    months = age_months(protocol, now)
    if months >= 12:
        score += SCORE_AGE_MATURE
        signals.append(f"tracked {months:.0f} months")
    elif months >= 6:
        score += SCORE_AGE_ESTABLISHED
        signals.append(f"tracked {months:.0f} months")
    elif months >= 3:
        score += SCORE_AGE_YOUNG
        signals.append(f"tracked {months:.0f} months")
    else:
        signals.append("listed under 3 months ago")

    if _text(protocol.get("url")):
        score += SCORE_HAS_SITE
    else:
        signals.append("no official site on file")
    if _text(protocol.get("twitter")):
        score += SCORE_HAS_SOCIAL

    chains = protocol.get("chains")
    if isinstance(chains, list) and len(chains) > 1:
        score += SCORE_MULTICHAIN
        signals.append(f"deployed on {len(chains)} chains")

    change_7d = _number(protocol.get("change_7d"))
    if change_7d > TVL_SPIKE_THRESHOLD_PCT:
        score -= PENALTY_TVL_SPIKE
        signals.append(f"TVL spiked {change_7d:.0f}% in 7d - likely mercenary capital")

    return max(0.0, min(100.0, score)), signals


def risk_band(score: float) -> str:
    if score >= BAND_STRONG:
        return "STRONG"
    if score >= BAND_MODERATE:
        return "MODERATE"
    if score >= BAND_SPECULATIVE:
        return "SPECULATIVE"
    return "HIGH RISK"


def airdrop_probability(score: float) -> float:
    """Modelled chance this protocol distributes anything a small wallet receives."""
    fraction = max(0.0, min(100.0, score)) / 100.0
    return P_AIRDROP_MIN + (P_AIRDROP_MAX - P_AIRDROP_MIN) * fraction


def base_allocation_usd(tvl: float) -> float:
    """Allocation a threshold-clearing wallet might see, scaled by protocol size.

    Logarithmic in TVL, mirroring how allocation tiers themselves are usually
    logarithmic: a wallet that qualifies at all captures a meaningful share.
    """
    if tvl <= 0:
        return 0.0
    span = math.log10(max(TVL_SCORE_CEILING_USD / MIN_TVL_USD, 10.0))
    ratio = min(1.0, math.log10(max(tvl / MIN_TVL_USD, 1.0)) / span)
    return (BASE_ALLOCATION_FLOOR_USD
            + (BASE_ALLOCATION_CEILING_USD - BASE_ALLOCATION_FLOOR_USD) * ratio)


def deposit_factor(deposit_usd: float) -> float:
    """Share of a reference-sized wallet's allocation that `deposit_usd` earns.

    Logarithmic, so a small wallet is not penalised proportionally - that asymmetry
    is the entire reason airdrops suit a small bankroll. It is still bounded well
    below 1.0, because the reported active-user outcomes belong to wallets funded
    near DEPOSIT_REFERENCE_USD, not to $30 ones.
    """
    if deposit_usd <= 0:
        return 0.0
    span = math.log10(DEPOSIT_REFERENCE_USD / DEPOSIT_FLOOR_USD)
    ratio = math.log10(max(deposit_usd, DEPOSIT_FLOOR_USD) / DEPOSIT_FLOOR_USD) / span
    return max(0.0, min(1.0, ratio))


def expected_value(protocol: dict, score: float, deposit_usd: float) -> dict:
    """Modelled payoff for one wallet at this deposit. Assumptions, not forecasts.

    The two haircuts describe different things and must be applied to different
    quantities exactly once each. Qualification is a *probability* - a filtered wallet
    receives nothing at all, not a smaller amount - so it belongs in the odds.
    Depreciation is a *value* effect on tokens that were actually received, so it
    belongs in the payout. An earlier version applied qualification to both, which
    left the reported odds and payout describing no coherent outcome (a 34% chance of
    something alongside a 78% chance of nothing sums to 112%). Expected value was
    unaffected, but the numbers a reader would act on were not.
    """
    tvl = _number(protocol.get("tvl"))
    gross = base_allocation_usd(tvl) * deposit_factor(deposit_usd)
    drops = airdrop_probability(score)
    p_paid = drops * QUALIFICATION_RATE
    value_if_paid = gross * VALUE_RETENTION
    return {
        "p_protocol_airdrops": drops,
        "p_paid": p_paid,
        "p_nothing": 1.0 - p_paid,
        "deposit_usd": deposit_usd,
        "deposit_factor": deposit_factor(deposit_usd),
        "gross_if_it_lands_usd": gross,
        "value_if_paid_usd": value_if_paid,
        "expected_usd": p_paid * value_if_paid,
    }


def assumptions() -> dict:
    return {
        "p_airdrop_range": [P_AIRDROP_MIN, P_AIRDROP_MAX],
        "value_retention": VALUE_RETENTION,
        "value_retention_basis": "88% of airdropped tokens lose value within 3 months",
        "qualification_rate": QUALIFICATION_RATE,
        "qualification_basis": "Sybil filters and minimum-size cutoffs; Linea "
                               "disqualified ~40% of claimants",
        "allocation_range_usd": [BASE_ALLOCATION_FLOOR_USD,
                                 BASE_ALLOCATION_CEILING_USD],
        "allocation_basis": "reported $500-$5,000 active-user outcomes, discounted",
        "caveat": ("Modelled, not measured. Unlike the funding scanner these numbers "
                   "cannot be verified against an exchange API."),
    }


def instructions(protocol: dict, bankroll_usd: float) -> list[str]:
    """Concrete steps for this protocol, sized to the bankroll."""
    name = _text(protocol.get("name")) or "this protocol"
    site = _text(protocol.get("url"))
    chains = protocol.get("chains") if isinstance(protocol.get("chains"), list) else []
    chain = _text(protocol.get("chain")) or (chains[0] if chains else "its chain")
    per_wallet = max(20.0, bankroll_usd / 3.0)
    steps = [
        f"Open {name} only via {site or 'the link on defillama.com'} - navigate there "
        f"yourself. Never use a link from a DM, reply, or ad; airdrop phishing is the "
        f"single most common way farmers lose their whole balance.",
        f"Bridge roughly ${per_wallet:,.0f} to {chain}. Keep a few dollars spare for gas.",
        f"Use the core product - deposit, swap, lend or trade, whatever {name} actually "
        f"does. Points are usually weighted to real usage, not to wallet count.",
        "Return and interact across several separate weeks. Consistency over time is "
        "what most allocation formulas reward; a single day of activity rarely counts.",
        "Use ONE wallet. 85% of 2026 airdrops run Sybil detection, and multi-wallet "
        "farming now gets the whole cluster zeroed rather than multiplying anything.",
        "Never sign a transaction you did not initiate, and never enter a seed phrase. "
        "No genuine airdrop ever asks for one.",
    ]
    if int(_number(protocol.get("audits"))) == 0:
        steps.insert(1, "No audit is published for this protocol. Treat any deposit as "
                        "money you could lose to a contract bug, independent of whether "
                        "a token ever arrives.")
    return steps


def rank(protocols: list[dict], bankroll_usd: float = 100.0,
         now: float | None = None, min_tvl_usd: float = MIN_TVL_USD,
         limit: int = 40, wallets: int = 3) -> list[dict]:
    # The bankroll is split across the farms actually worked, so each protocol is
    # scored at the deposit it would really receive - not at the full bankroll.
    deposit = bankroll_usd / max(1, wallets)
    rows = []
    for protocol in protocols:
        if not isinstance(protocol, dict) or not is_eligible(protocol, min_tvl_usd):
            continue
        score, signals = legitimacy_score(protocol, now)
        payoff = expected_value(protocol, score, deposit)
        rows.append({
            "name": _text(protocol.get("name")),
            "slug": _text(protocol.get("slug")),
            "url": _text(protocol.get("url")),
            "twitter": _text(protocol.get("twitter")),
            "category": _text(protocol.get("category")),
            "chain": _text(protocol.get("chain")),
            "chains": protocol.get("chains") if isinstance(protocol.get("chains"), list) else [],
            "tvl_usd": _number(protocol.get("tvl")),
            "age_months": age_months(protocol, now),
            "audits": int(_number(protocol.get("audits"))),
            "score": round(score, 1),
            "risk_band": risk_band(score),
            "signals": signals,
            "expected": payoff,
            "instructions": instructions(protocol, bankroll_usd),
        })
    # Most reliable first. Legitimacy leads because the modelled payoff rests on
    # assumptions while the score rests on checkable facts; ordering by dollars would
    # promote a thinly-evidenced protocol above a well-audited one for the sake of a
    # number that cannot be verified. Expected value breaks ties within a band.
    rows.sort(key=lambda row: (-row["score"], -row["expected"]["expected_usd"]))
    # Every row is priced at the same per-farm deposit, so the expected-value column
    # sums to far more than this bankroll can actually fund. Tag the rows the money
    # really covers, so the list cannot be read as an affordable shopping basket.
    for index, row in enumerate(rows):
        row["funded"] = index < wallets
    return rows[:limit]


def portfolio_view(rows: list[dict], bankroll_usd: float,
                   wallets: int = 3) -> dict:
    """What the whole plan is worth if the top `wallets` farms are actually worked."""
    chosen = rows[:max(0, wallets)]
    expected = sum(row["expected"]["expected_usd"] for row in chosen)
    upside = sum(row["expected"]["value_if_paid_usd"] for row in chosen)
    p_all_nothing = 1.0
    for row in chosen:
        p_all_nothing *= row["expected"]["p_nothing"]
    # Unlike a leveraged bet, the bankroll is deposited rather than consumed: it is
    # still yours while it farms. Airdrop value is additional to it, so the honest
    # end-state is capital + drop, less whatever the protocol risk costs.
    return {
        "farms": len(chosen),
        "bankroll_usd": bankroll_usd,
        "expected_airdrop_usd": expected,
        "expected_total_usd": bankroll_usd + expected,
        "expected_multiple": (1.0 + expected / bankroll_usd) if bankroll_usd > 0 else 0.0,
        "upside_if_all_land_usd": upside,
        "upside_total_usd": bankroll_usd + upside,
        "probability_of_nothing": p_all_nothing,
        "capital_note": ("The bankroll is deposited, not spent, and remains withdrawable. "
                         "It is still exposed to smart-contract failure, depeg and "
                         "bridge risk, which is the real way this loses money."),
        "horizon": "3-12 months; airdrop timing cannot be scheduled",
    }


async def _json(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url) as response:
        if response.status != 200:
            body = (await response.text())[:120]
            raise RuntimeError(f"HTTP {response.status}: {body}")
        return await response.json()


REFRESH_SEC = 900.0          # DeFiLlama TVL moves slowly; 15 minutes is plenty


class AirdropRadar:
    """Caches one scan for the dashboard so page loads never hit DeFiLlama directly."""

    def __init__(self, bankroll_usd: float = 100.0):
        self.bankroll_usd = bankroll_usd
        self.result: dict = {}
        self.error = ""
        self.last_good_at = 0.0
        self._universe: list[dict] = []

    def _reprice(self) -> None:
        """Rebuild the ranking from the cached universe at the current bankroll."""
        rows = rank(self._universe, bankroll_usd=self.bankroll_usd)
        self.result = {
            **self.result,
            "bankroll_usd": self.bankroll_usd,
            "universe": len(self._universe),
            "candidates": len(rows),
            "rows": rows,
            "portfolio": portfolio_view(rows, self.bankroll_usd),
            "assumptions": assumptions(),
        }

    async def refresh(self) -> None:
        started = time.time()
        try:
            self._universe = await fetch_protocols()
            self._reprice()
            self.result["generated_at"] = time.time()
            self.result["scan_seconds"] = round(time.time() - started, 2)
            self.last_good_at = time.time()
            self.error = ""
        except Exception as exc:                      # noqa: BLE001 - surfaced to UI
            self.error = f"{type(exc).__name__}: {str(exc)[:140]}"

    async def manage_loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(REFRESH_SEC)

    def set_bankroll(self, bankroll_usd: float) -> dict:
        """Re-price the cached universe; no refetch needed to change bankroll."""
        self.bankroll_usd = _number(bankroll_usd) if _number(bankroll_usd) > 0 else 100.0
        if self._universe:
            self._reprice()
        return {"ok": True, "bankroll_usd": self.bankroll_usd}

    def state(self) -> dict:
        return {
            "running": True,
            "name": NAME,
            "error": self.error,
            "last_good_at": self.last_good_at,
            "refresh_sec": REFRESH_SEC,
            **(self.result or {"rows": [], "candidates": 0, "universe": 0,
                               "bankroll_usd": self.bankroll_usd,
                               "portfolio": portfolio_view([], self.bankroll_usd),
                               "assumptions": assumptions()}),
        }


async def fetch_protocols() -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        protocols = await _json(session, LLAMA_PROTOCOLS_URL)
    if not isinstance(protocols, list):
        raise ValueError("DeFiLlama returned an unexpected payload")
    return protocols


async def scan(bankroll_usd: float = 100.0, limit: int = 40) -> dict:
    started = time.time()
    protocols = await fetch_protocols()
    rows = rank(protocols, bankroll_usd=bankroll_usd, limit=limit)
    return {
        "generated_at": time.time(),
        "scan_seconds": round(time.time() - started, 2),
        "universe": len(protocols),
        "candidates": len(rows),
        "bankroll_usd": bankroll_usd,
        "rows": rows,
        "portfolio": portfolio_view(rows, bankroll_usd),
        "assumptions": assumptions(),
        "method": ("Ranks DeFiLlama protocols that hold real TVL in a user-facing "
                   "category and have no token yet. Read-only public data."),
    }
