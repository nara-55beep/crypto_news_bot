"""Airdrops whose token has been publicly confirmed by the team, with dates.

The speculative scanner in `airdrop_scanner` ranks protocols that *might* issue a
token. This module is the opposite end: drops the team has actually announced. It
exists because "confirmed" and "still farmable" are mostly mutually exclusive, and
that tension is the single most useful thing to show someone.

Lighter is the proof. Its airdrop was confirmed, dated and delivered - 250M LIT, 25%
of supply, TGE 30 December 2025 - and the points programme closed with it. By the time
it was certain, it was over. Chasing only confirmed drops means arriving after every
snapshot; chasing only unconfirmed ones means most never pay. The narrow overlap, where
a token is confirmed but no snapshot has been taken, is the whole opportunity.

Two things this deliberately does not claim:

* A confirmed token is not a confirmed payment. Every one of these still requires the
  wallet to qualify, and confirmation draws far more competition, which dilutes the
  per-user allocation.
* This is a hand-verified snapshot, not a live feed. No public API carries airdrop
  announcements, so each entry records who confirmed it and where, and `staleness()`
  reports how old the check is rather than presenting it as current fact.
"""

from __future__ import annotations

import time

# Every entry below was read from the cited source on this date. Anything derived
# from it is only as current as this.
VERIFIED_ON = "2026-08-10"
VERIFIED_AT = 1_786_400_000.0
STALE_AFTER_DAYS = 14

OPEN = "OPEN"                # token confirmed, no snapshot taken, still farmable
PENDING = "PENDING"          # token confirmed, qualification path not yet published
DISTRIBUTED = "DISTRIBUTED"  # already paid out; the window has closed

CONFIRMED_AIRDROPS = [
    {
        "name": "Ink",
        "token": "INK",
        "status": OPEN,
        "chain": "Ink",
        "tge_window": "July-September 2026 (some sources say Q3-Q4)",
        "confirmed_by": "Ink Foundation - confirmed an incentives token and airdrop "
                        "weighted toward real usage, early onchain activity and "
                        "Kraken Pro users",
        "source_url": "https://airdrops.io/ink-chain/",
        "snapshot_taken": False,
        "why_it_matters": ("The clearest confirmed-and-still-open drop found. The TGE "
                           "window is live right now and no snapshot has been taken, "
                           "so a wallet started today can still qualify."),
        "qualify": [
            "Trade on Kraken Pro - Ink points accrue from Kraken Pro activity, and "
            "the first points distribution landed 13 April 2026.",
            "Bridge assets to the Ink network.",
            "Use the Ink DeFi apps: Nado for perpetuals, Tydro for lending, "
            "Velodrome for liquidity.",
            "Keep activity consistent across weeks. The Foundation has said allocation "
            "weights real usage, so a history of use beats one large transaction.",
        ],
        "risk": ("The TGE window may already be closing - some sources place it as "
                 "early as July 2026. Verify the snapshot has not been taken before "
                 "committing anything."),
    },
    {
        "name": "MetaMask",
        "token": "MASK",
        "status": OPEN,
        "chain": "Ethereum and L2s",
        "tge_window": "Q3-Q4 2026 (estimated, not announced)",
        "confirmed_by": "Consensys CEO Joseph Lubin confirmed a native MASK token",
        "source_url": "https://airdrops.io/metamask/",
        "snapshot_taken": False,
        "why_it_matters": ("Points are still accruing and Season 1 points carry into "
                           "Season 2, so activity now is not wasted even though the "
                           "date is unannounced."),
        "qualify": [
            "Use MetaMask Rewards and accumulate points - they carry between seasons.",
            "Earn points by swapping tokens, trading perpetuals, spending with the "
            "MetaMask Card, and referring users.",
        ],
        "risk": ("The token is confirmed but eligibility rules, distribution method "
                 "and timeline have never been officially published. Treat the "
                 "airdrop itself as expected rather than guaranteed."),
    },
    {
        "name": "Polymarket",
        "token": "not announced",
        "status": PENDING,
        "chain": "Polygon",
        "tge_window": "not announced; still pending as of August 2026",
        "confirmed_by": "CMO Matthew Modabber said the launch will include an airdrop",
        "source_url": "https://airdropalert.com/blogs/polymarket-airdrop-confirmed/",
        "snapshot_taken": False,
        "why_it_matters": ("Confirmed by an executive and not yet distributed, but no "
                           "qualification programme has been published, so there is "
                           "nothing concrete to farm yet."),
        "qualify": [
            "Trade real markets on Polymarket. No points programme has been announced, "
            "so genuine usage is the only defensible proxy.",
        ],
        "risk": "No published criteria means no way to confirm any action qualifies.",
    },
    {
        "name": "MegaETH",
        "token": "not announced",
        "status": PENDING,
        "chain": "MegaETH",
        "tge_window": "undistributed; prediction markets price ~39% by 31 Dec 2026",
        "confirmed_by": "Widely expected; timing traded on Polymarket rather than "
                        "announced by the team",
        "source_url": "https://polymarket.com/event/megaeth-airdrop-by",
        "snapshot_taken": False,
        "why_it_matters": ("Included because the market-implied odds are a real number "
                           "rather than a guess - but a prediction market is not a "
                           "team announcement."),
        "qualify": [
            "Use the MegaETH network and its applications.",
        ],
        "risk": ("Not confirmed by the team. The 39% is a crowd estimate of timing, "
                 "not evidence that an airdrop exists."),
    },
    {
        "name": "Lighter",
        "token": "LIT",
        "status": DISTRIBUTED,
        "chain": "Lighter",
        "tge_window": "30 December 2025 - already happened",
        "confirmed_by": "Lighter - 250M LIT, 25% of supply, paid directly to points "
                        "holders from Seasons 1 and 2 with no claim or vesting",
        "source_url": "https://www.coindesk.com/markets/2025/12/30/"
                      "lighter-dex-launches-lit-token-with-25-airdrop",
        "snapshot_taken": True,
        "why_it_matters": ("The cautionary case. It was confirmed, dated, generous and "
                           "delivered - and the points programme closed with it. "
                           "Certainty arrived only once the window had shut."),
        "qualify": [],
        "risk": "Closed. Points earned after the seasons ended count for nothing.",
    },
    {
        "name": "OpenSea",
        "token": "SEA",
        "status": DISTRIBUTED,
        "chain": "Ethereum",
        "tge_window": "Q1 2026 - already happened",
        "confirmed_by": "CEO Devin Finzer - 50% of supply to the community, over half "
                        "available at initial claim",
        "source_url": "https://zipmex.com/blog/top-crypto-airdrops-q1-2026/",
        "snapshot_taken": True,
        "why_it_matters": "Another confirmed drop whose window closed before certainty "
                          "was useful to a newcomer.",
        "qualify": [],
        "risk": "Closed.",
    },
]


def staleness(now: float | None = None) -> dict:
    """How old this hand-verified list is, and whether it should be re-checked."""
    now = time.time() if now is None else now
    days = max(0.0, (now - VERIFIED_AT) / 86_400.0)
    return {
        "verified_on": VERIFIED_ON,
        "days_old": round(days, 1),
        "stale": days > STALE_AFTER_DAYS,
        "note": ("Hand-verified from the cited sources, not a live feed - no public API "
                 "carries airdrop announcements. Re-check each source before acting; "
                 "a TGE window that was open at verification may have closed since."),
    }


def by_status(status: str) -> list[dict]:
    return [entry for entry in CONFIRMED_AIRDROPS if entry["status"] == status]


def farmable() -> list[dict]:
    """Confirmed tokens whose snapshot has not been taken - the only actionable set."""
    return [entry for entry in CONFIRMED_AIRDROPS
            if entry["status"] == OPEN and not entry["snapshot_taken"]]


def overview(now: float | None = None) -> dict:
    return {
        "verified": staleness(now),
        "open": by_status(OPEN),
        "pending": by_status(PENDING),
        "distributed": by_status(DISTRIBUTED),
        "farmable_count": len(farmable()),
        "lesson": (
            "Confirmed and still farmable are mostly mutually exclusive. Lighter was "
            "confirmed, dated and generous - and its points programme closed at the "
            "same moment it became certain. The only actionable window is a token that "
            "is confirmed but not yet snapshotted, which is a small set at any time."),
        "caveat": (
            "A confirmed token is not a confirmed payment. Every entry still requires "
            "the wallet to qualify, and confirmation attracts far more farmers, which "
            "dilutes what each one receives. Nothing here is a 100% chance."),
    }
