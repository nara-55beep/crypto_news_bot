"""
verify_wallets.py — LIVE health check for copy-trade wallets. The #1 rule: a wallet must
be funded + active + edged RIGHT NOW, not just historically (stale leaderboard stats are
how you get burned copying a wallet that already withdrew to $0).

For each address, query Hyperliquid live and require ALL of:
  1. funded now      — clearinghouseState accountValue >= MIN_AV
  2. active now       — last userFill within MAX_AGE_H hours
  3. copyable cadence — 1..MAX_FILLS_24H fills/day (not dead, not 2s HFT churn)
  4. edged now        — portfolio month PnL > 0 AND allTime PnL > 0 (week>0 preferred)
  5. position health  — open positions not deeply underwater; liquidation price far away
  6. crypto perp      — main traded market is a crypto perp (not spot / TradFi perp)

Usage:
  venv/Scripts/python.exe verify_wallets.py            # verify wallets currently in copy_trader.py
  venv/Scripts/python.exe verify_wallets.py _picked.json   # verify a candidate file
Writes _verified.json (survivors, ranked) and prints a KEEP/DROP table with reasons.
"""
from __future__ import annotations
import sys, os, re, json, time, urllib.request
from collections import Counter

MIN_AV = 30_000
MAX_AGE_H = 72
MAX_FILLS_24H = 40
MIN_LIQ_DIST = 0.08      # liquidation price must be >8% from mark (leveraged perps run ~10x)
HERE = os.path.dirname(os.path.abspath(__file__))


def _post(typ, user, retries=3):
    body = json.dumps({"type": typ, "user": user}).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=body,
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=20).read())
        except Exception:
            time.sleep(1.0 + i)
    return None


def _window_pnl(portfolio, name):
    for w, data in portfolio or []:
        if w == name:
            ph = data.get("pnlHistory", [])
            return float(ph[-1][1]) if ph else 0.0
    return 0.0


def verify(addr):
    """Return dict with live metrics + verdict ('KEEP' or 'DROP: reason')."""
    ch = _post("clearinghouseState", addr)
    if ch is None:
        return {"addr": addr, "verdict": "DROP: api_fail"}
    av = float(ch["marginSummary"]["accountValue"])
    positions = ch.get("assetPositions", [])
    # position health
    worst_liq_dist = 9.9
    pos_upnl = 0.0
    pos_coins = []
    for ap in positions:
        p = ap["position"]
        pos_coins.append(p["coin"])
        pos_upnl += float(p.get("unrealizedPnl") or 0)
        liq = p.get("liquidationPx")
        szi = abs(float(p.get("szi") or 0))
        pos_val = abs(float(p.get("positionValue") or 0))
        mark = (pos_val / szi) if szi else 0.0     # current mark, not entry
        if liq and mark:
            d = abs(float(liq) - mark) / mark        # distance from MARK to liquidation
            worst_liq_dist = min(worst_liq_dist, d)

    fills = _post("userFills", addr) or []
    now_ms = time.time() * 1000
    last_age_h = (now_ms - max((float(f.get("time", 0)) for f in fills), default=0)) / 3.6e6
    f24 = sum(1 for f in fills if (now_ms - float(f.get("time", 0))) < 86.4e6)
    top_coins = [c for c, _ in Counter(f.get("coin") for f in fills[:200]).most_common(3)]
    top = top_coins[0] if top_coins else ""

    pf = _post("portfolio", addr)
    pnl_week = _window_pnl(pf, "week")
    pnl_month = _window_pnl(pf, "month")
    pnl_all = _window_pnl(pf, "allTime")

    m = {"addr": addr, "av": round(av), "f24": f24, "last_age_h": round(last_age_h, 1),
         "coins": top_coins, "pnl_week": round(pnl_week), "pnl_month": round(pnl_month),
         "pnl_all": round(pnl_all), "pos_upnl": round(pos_upnl),
         "liq_dist": round(worst_liq_dist, 3) if positions else None}

    # ---- verdict ----
    reasons = []
    if av < MIN_AV:                         reasons.append(f"unfunded(${av:,.0f})")
    if last_age_h > MAX_AGE_H:              reasons.append(f"stale({last_age_h:.0f}h)")
    if f24 == 0 and last_age_h > MAX_AGE_H: reasons.append("inactive")
    if f24 > MAX_FILLS_24H:                 reasons.append(f"HFT({f24}/d)")
    if pnl_month <= 0:                      reasons.append("month_pnl<=0")
    if pnl_all <= 0:                        reasons.append("alltime_pnl<=0")
    if positions and pos_upnl < -0.15 * av: reasons.append("pos_underwater")
    if positions and worst_liq_dist < MIN_LIQ_DIST: reasons.append(f"liq_near({worst_liq_dist:.0%})")
    if top and (top.startswith("@") or top.startswith("xyz:") or "/" in top):
        reasons.append(f"non_crypto({top})")
    m["verdict"] = "KEEP" if not reasons else "DROP: " + ",".join(reasons)
    return m


def wallets_from_copytrader():
    txt = open(os.path.join(HERE, "copy_trader.py"), encoding="utf-8").read()
    block = txt[txt.index("WALLETS = ["):]
    pairs = re.findall(r'"name":\s*"([^"]+)",\s*"addr":\s*"([^"]+)"', block)
    return [{"name": n, "addr": a} for n, a in pairs]


def main():
    if len(sys.argv) > 1 and sys.argv[1].endswith(".json"):
        data = json.load(open(sys.argv[1]))
        wallets = [{"name": d.get("name", f"W{i}"), "addr": d["addr"]}
                   for i, d in enumerate(data)]
    else:
        wallets = wallets_from_copytrader()
    print(f"Verifying {len(wallets)} wallets LIVE on Hyperliquid ...\n")
    print(f"{'name':<9}{'KEEP/DROP':<26}{'av':>9}{'f/24h':>6}{'age_h':>6}"
          f"{'wk$':>9}{'mo$':>10}{'all$':>11}  coins")
    survivors = []
    for w in wallets:
        m = verify(w["addr"])
        m["name"] = w["name"]
        keep = m["verdict"] == "KEEP"
        coins = "/".join(m.get("coins", [])[:2])
        print(f"{w['name']:<9}{m['verdict']:<26}{m.get('av',0):>9,}{m.get('f24',0):>6}"
              f"{m.get('last_age_h',0):>6.0f}{m.get('pnl_week',0):>9,}{m.get('pnl_month',0):>10,}"
              f"{m.get('pnl_all',0):>11,}  {coins}")
        if keep:
            survivors.append(m)
        time.sleep(0.25)
    survivors.sort(key=lambda x: x["pnl_month"], reverse=True)
    json.dump(survivors, open(os.path.join(HERE, "_verified.json"), "w"), indent=1)
    print(f"\n{len([w for w in wallets])} checked -> {len(survivors)} KEEP, "
          f"{len(wallets)-len(survivors)} DROP.  Wrote _verified.json")


if __name__ == "__main__":
    main()
