"""
rank_wallets.py — score & TIER the verified copy-trade roster so you mirror the BEST
wallets, not all 20 equally. Pulls live Hyperliquid data per wallet and scores on:
  * return     — monthly ROI %
  * consistency— how many of {week, month, allTime} PnL windows are positive (0..3)
  * risk       — max drawdown of the all-time PnL curve, as % of account value (lower better)
  * cadence    — copyable activity (a few to ~25 fills/day is ideal; 0=slow swing; >30 penalized)
Tiers: A = copy at full size, B = copy at half size, C = watchlist only.

Run: venv/Scripts/python.exe rank_wallets.py
"""
from __future__ import annotations
import os, re, json, time, math, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CT = os.path.join(HERE, "copy_trader.py")


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


def roster():
    txt = open(CT, encoding="utf-8").read()
    block = txt[txt.index("WALLETS = ["):]
    block = block[:block.index("\n]")]
    out = []
    for m in re.finditer(r'"name":\s*"([^"]+)",\s*"addr":\s*"([^"]+)",\s*"blurb":\s*"([^"]+)"', block):
        out.append({"name": m.group(1), "addr": m.group(2), "blurb": m.group(3)})
    return out


def pnl_retracement(pf, window="allTime"):
    """Largest give-back of the cumulative PnL curve as a fraction of its PEAK gain.
    Withdrawal-invariant (both terms in PnL$), so taking profits out doesn't distort it.
    0 = never gave back; ->1 = round-tripped its gains."""
    for w, data in pf or []:
        if w == window:
            ph = [float(p[1]) for p in data.get("pnlHistory", [])]
            if len(ph) < 2:
                return 0.0
            peak = ph[0]; mdd = 0.0
            for v in ph:
                peak = max(peak, v)
                mdd = max(mdd, peak - v)
            return mdd / peak if peak > 0 else 1.0
    return 0.0


def win(pf, window):
    for w, data in pf or []:
        if w == window:
            ph = data.get("pnlHistory", [])
            return float(ph[-1][1]) if ph else 0.0
    return 0.0


def cadence_score(f24):
    if f24 == 0:    return 0.6          # pure swing — copyable but slower to mirror
    if f24 <= 25:   return 1.0          # ideal day-trade/swing cadence
    if f24 <= 30:   return 0.7
    return 0.3                          # borderline fast


def main():
    wallets = roster()
    print(f"Ranking {len(wallets)} verified wallets (pulling live data)...\n")
    rows = []
    for w in wallets:
        ch = _post("clearinghouseState", w["addr"])
        pf = _post("portfolio", w["addr"])
        fills = _post("userFills", w["addr"]) or []
        if ch is None or pf is None:
            time.sleep(0.3); continue
        av = float(ch["marginSummary"]["accountValue"])
        now_ms = time.time() * 1000
        f24 = sum(1 for f in fills if (now_ms - float(f.get("time", 0))) < 86.4e6)
        pw, pm, pa = win(pf, "week"), win(pf, "month"), win(pf, "allTime")
        mr = (pm / av * 100) if av else 0          # monthly ROI proxy on current equity
        dd_ratio = pnl_retracement(pf, "allTime")  # peak-gain give-back (withdrawal-invariant)
        consistency = sum(1 for x in (pw, pm, pa) if x > 0)
        # ---- composite score (transparent, bounded) ----
        s_ret = min(mr / 100, 2.0)                 # cap monthly ROI contribution at 200%
        s_con = consistency / 3                    # 0..1
        s_risk = 1 - min(dd_ratio, 1.0)            # 1 = never gave back peak gains, 0 = round-tripped
        s_cad = cadence_score(f24)
        s_size = min(math.log10(max(av, 1e4)) / 7, 1)   # establishment, small weight
        score = 100 * (0.34*s_ret/2 + 0.26*s_con + 0.22*s_risk + 0.12*s_cad + 0.06*s_size)
        rows.append({**w, "av": av, "f24": f24, "mr": mr, "dd_ratio": dd_ratio,
                     "consistency": consistency, "pw": pw, "score": round(score, 1)})
        time.sleep(0.25)

    rows.sort(key=lambda x: x["score"], reverse=True)
    print(f"{'rank':>4} {'name':<8}{'score':>6} {'mROI%':>6}{'consist':>8}{'giveback':>9}"
          f"{'f/24h':>6}{'wkPnL$':>11}  coins")
    for i, r in enumerate(rows, 1):
        coins = r["blurb"].split(" · ")[0]
        print(f"{i:>4} {r['name']:<8}{r['score']:>6}{r['mr']:>6.0f}{r['consistency']:>6}/3"
              f"{r['dd_ratio']:>8.0%}{r['f24']:>6}{r['pw']:>11,.0f}  {coins}")

    # tiers by score percentile
    n = len(rows)
    tierA = rows[:max(1, n//3)]
    tierB = rows[n//3:2*n//3]
    print(f"\nTIER A — copy at full size ({len(tierA)}): {', '.join(r['name'] for r in tierA)}")
    print(f"TIER B — copy at half size ({len(tierB)}): {', '.join(r['name'] for r in tierB)}")
    print(f"TIER C — watchlist only ({n-len(tierA)-len(tierB)}): "
          f"{', '.join(r['name'] for r in rows[2*n//3:])}")
    json.dump(rows, open(os.path.join(HERE, "_ranked.json"), "w"), indent=1, default=str)
    print("\nwrote _ranked.json")


if __name__ == "__main__":
    main()
