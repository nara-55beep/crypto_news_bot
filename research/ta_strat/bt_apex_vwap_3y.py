"""
bt_apex_vwap_3y.py — run the EXACT "Apex ES VWAP ORB (paper)" rule set over 3 YEARS on every market
we have data for (ES, NQ, CL). Reuses the real backtest engine in research/apex_vwap_orb_bot.py
(OR break + VWAP + EMA9/20 + peer confirm -> pullback-rejection entry, 2.5R target, +1R trail, flat
by close, $50k Apex account, $2k EOD trailing). Settings match the live paper bot: 1 full contract,
$1,000 risk, max 2 trades/day, $1,000 daily-loss stop, entry window to 10:30 ET.
ES peer = NQ, NQ peer = ES, CL has no index peer (require_peer off).
"""
from __future__ import annotations
import os, sys
import pandas as pd
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from apex_vwap_orb_bot import prepare, backtest_one, _row_by_time, MarketSpec, Params, format_metrics

NY = ZoneInfo("America/New_York")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

SPECS = {
    "ES": MarketSpec("ES=F", "E-mini S&P 500", 50.0, tick=0.25),
    "NQ": MarketSpec("NQ=F", "E-mini Nasdaq-100", 20.0, tick=0.25),
    "CL": MarketSpec("CL=F", "Crude Oil", 1000.0, tick=0.01),
}
PEER = {"ES": "NQ", "NQ": "ES", "CL": None}


def load_rth(name: str, rule: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(CACHE, f"{name.lower()}_1m_3y.csv"))
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    df = df.set_index("dt_utc").sort_index()
    r = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"]).reset_index()
    r["dt_ny"] = r["dt_utc"].dt.tz_convert(NY)
    return prepare(r)


def params_for(mkt: str, interval: str) -> Params:
    # exact paper-bot config: 1 full contract, $1k risk, to-10:30 entries, 2.5R, 1.8 ATR stop cap
    return Params(interval=interval, risk_usd=1_000.0, max_daily_loss=1_000.0, max_contracts=1,
                  max_trades_day=2, or_minutes=15, trade_end="10:30", flat_time="15:55",
                  min_stop_atr=0.35, max_stop_atr=1.8, pullback_tol_atr=0.15,
                  rr1=1.0, rr_final=2.5, trail_r=1.0, require_peer=(PEER[mkt] is not None))


def rolling_pass(data, peer, spec, p, peer_map):
    """% of rolling 30-calendar-day windows that pass the Apex $50k eval (+$3k, no $2k breach)."""
    days = sorted(data["day"].unique())
    last = days[-1]; total = passed = 0
    for s in days[::3]:
        e = (pd.Timestamp(s) + pd.Timedelta(days=30)).date()
        if e > last:
            break
        _, m = backtest_one(data, peer, spec, p, date_start=s, date_end=e, peer_map=peer_map)
        total += 1; passed += int(m["passed_50k_eval"])
    return passed, total


def main():
    data = {tf: {m: load_rth(m, tf) for m in SPECS} for tf in ("5min", "15min")}
    print("Apex ES VWAP ORB — 3-year backtest, all markets (1 contract, $1k risk, paper-bot rules)\n")
    for interval, tfname in [("5min", "5m"), ("15min", "15m")]:
        print(f"================= {tfname} =================")
        d0 = data[interval]["ES"]
        print(f"data: {d0['dt_ny'].iloc[0].date()} -> {d0['dt_ny'].iloc[-1].date()}  (RTH 9:30-16:00 ET)")
        for mkt in ("ES", "NQ", "CL"):
            spec = SPECS[mkt]
            p = params_for(mkt, interval)
            df = data[interval][mkt]
            peer_key = PEER[mkt]
            peer = data[interval][peer_key] if peer_key else df
            peer_map = _row_by_time(peer) if peer_key else {}
            _, m = backtest_one(df, peer, spec, p, peer_map=peer_map)
            pw, tw = rolling_pass(df, peer, spec, p, peer_map)
            pk = peer_key or "none"
            print(format_metrics(f"{mkt} (peer {pk})", m))
            print(f"{'':<24} 1-month Apex windows passed: {pw}/{tw} "
                  f"({(pw/tw*100 if tw else 0):.0f}%)   breached$2k-DD ever: {'YES' if m['breached'] else 'no'}")
        print()


if __name__ == "__main__":
    main()
