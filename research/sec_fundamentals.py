"""Compact point-in-time quarterly fundamentals from SEC Company Facts.

Only standalone 10-Q quarters are retained.  Values are keyed to the accession that
originally filed them, so a later restatement or comparative column does not rewrite an
older signal.  Raw multi-megabyte Company Facts responses are parsed in memory and not
stored; the cache contains only the event fields needed by the drift audit.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import pickle
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import edgar_catalysts as edgar  # noqa: E402


CACHE_PATH = ROOT / "research" / "cache" / "sec_quarterly_fundamentals.pkl"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SCHEMA_VERSION = 1

REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")


def _standalone_quarters(concept: dict, unit: str) -> dict[str, dict]:
    """One current-quarter fact per accession, excluding cumulative Q2/Q3 values."""
    rows: dict[str, list[dict]] = {}
    for value in (concept.get("units") or {}).get(unit, []):
        if str(value.get("form") or "").upper() != "10-Q":
            continue
        if not value.get("accn") or not value.get("start") or not value.get("end"):
            continue
        start = pd.to_datetime(value["start"], errors="coerce")
        end = pd.to_datetime(value["end"], errors="coerce")
        if pd.isna(start) or pd.isna(end):
            continue
        duration = int((end - start).days)
        if not 70 <= duration <= 110:
            continue
        try:
            number = float(value["val"])
        except (KeyError, TypeError, ValueError):
            continue
        row = {
            "accn": str(value["accn"]),
            "filed": str(value.get("filed") or ""),
            "start": str(value["start"]),
            "end": str(value["end"]),
            "duration_days": duration,
            "fy": value.get("fy"),
            "fp": str(value.get("fp") or ""),
            "value": number,
        }
        rows.setdefault(row["accn"], []).append(row)

    out = {}
    for accession, values in rows.items():
        # A current 10-Q accession usually repeats the prior-year comparison.  The
        # latest period end is the number disclosed for the current event.
        values.sort(key=lambda x: (x["end"], -x["duration_days"]), reverse=True)
        out[accession] = values[0]
    return out


def _concept_by_accession(facts: dict, tags: tuple[str, ...], unit: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for tag in tags:
        concept = facts.get(tag)
        if not concept:
            continue
        for accession, value in _standalone_quarters(concept, unit).items():
            if accession not in out:
                out[accession] = dict(value, tag=tag)
    return out


def extract_events(company_facts: dict, filing_rows: list[dict]) -> list[dict]:
    gaap = (company_facts.get("facts") or {}).get("us-gaap") or {}
    revenue = _concept_by_accession(gaap, REVENUE_TAGS, "USD")
    income = _concept_by_accession(gaap, NET_INCOME_TAGS, "USD")
    eps = _concept_by_accession(gaap, EPS_TAGS, "USD/shares")
    filings = {
        str(row.get("accessionNumber") or ""): row
        for row in filing_rows
        if str(row.get("form") or "").upper() == "10-Q"
    }
    events = []
    for accession in sorted(set(revenue) & set(income)):
        rev = revenue[accession]
        net = income[accession]
        filing = filings.get(accession)
        if not filing or rev["end"] != net["end"]:
            continue
        events.append({
            "accessionNumber": accession,
            "accepted_at": str(filing.get("acceptanceDateTime") or ""),
            "filing_date": str(filing.get("filingDate") or ""),
            "period_start": rev["start"],
            "period_end": rev["end"],
            "fiscal_year": rev.get("fy"),
            "fiscal_period": rev.get("fp"),
            "revenue": rev["value"],
            "net_income": net["value"],
            "eps_diluted": (eps.get(accession) or {}).get("value"),
            "revenue_tag": rev["tag"],
            "net_income_tag": net["tag"],
        })

    # YoY comparisons use the value filed in the earlier accession, never a prior-year
    # comparative column repeated by a later filing.
    events.sort(key=lambda x: x["period_end"])
    for event in events:
        end = pd.Timestamp(event["period_end"])
        candidates = [
            prior for prior in events
            if prior is not event
            and prior.get("fiscal_period") == event.get("fiscal_period")
            and 330 <= (end - pd.Timestamp(prior["period_end"])).days <= 400
        ]
        if not candidates:
            event.update({
                "prior_revenue": None,
                "prior_net_income": None,
                "revenue_growth_pct": None,
                "net_income_growth_pct": None,
            })
            continue
        prior = max(candidates, key=lambda x: x["period_end"])
        prior_revenue = float(prior["revenue"])
        growth = ((float(event["revenue"]) / prior_revenue - 1.0) * 100.0
                  if prior_revenue > 0 else None)
        prior_income = float(prior["net_income"])
        income_growth = (
            (float(event["net_income"]) / prior_income - 1.0) * 100.0
            if prior_income > 0 else None
        )
        event.update({
            "prior_revenue": prior_revenue,
            "prior_net_income": prior_income,
            "revenue_growth_pct": growth,
            "net_income_growth_pct": income_growth,
        })
    return events


def download(symbols: list[str], workers: int = 6) -> dict:
    mapping = edgar.ticker_to_cik()
    wanted = {symbol: mapping[symbol] for symbol in symbols if symbol in mapping}
    filings_payload = edgar.load()

    def fetch(symbol: str, cik: str):
        payload = edgar._get(COMPANY_FACTS_URL.format(cik=cik))
        if not payload:
            return symbol, []
        return symbol, extract_events(payload, edgar.filing_rows(filings_payload, symbol))

    output: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(fetch, symbol, cik): symbol for symbol, cik in wanted.items()
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                _, events = future.result()
            except Exception:
                events = []
            if events:
                output[symbol] = events

    result = {
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "source": "SEC EDGAR Company Facts API",
            "requested": len(symbols),
            "mapped_to_cik": len(wanted),
            "symbols_with_quarters": len(output),
            "point_in_time_accessions": True,
            "survivorship_free": False,
        },
        "events": output,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(result, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(temporary, CACHE_PATH)
    return result


def load(symbols: list[str] | None = None, refresh: bool = False) -> dict:
    if refresh or not CACHE_PATH.exists():
        if not symbols:
            raise ValueError("symbols are required to build the SEC fundamentals cache")
        return download(symbols)
    with CACHE_PATH.open("rb") as handle:
        return pickle.load(handle)
