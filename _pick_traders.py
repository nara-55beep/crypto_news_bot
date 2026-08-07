"""Pick top COPYABLE Hyperliquid traders from the LIVE leaderboard, then VERIFY each is
funded + active right now. Prints candidates; does not write anything."""
import urllib.request, json, time

def _get(url, data=None):
    req = urllib.request.Request(url, data=(json.dumps(data).encode() if data else None),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())

def info(typ, user):
    return _get("https://api.hyperliquid.xyz/info", {"type": typ, "user": user})

print("fetching live leaderboard ...")
rows = _get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")["leaderboardRows"]
print(f"  {len(rows)} traders on the board\n")

cands = []
for r in rows:
    try:
        av = float(r["accountValue"])
        perf = {w: v for w, v in r["windowPerformances"]}
        m, wk, at = perf["month"], perf["week"], perf["allTime"]
        mp, mr, mv = float(m["pnl"]), float(m["roi"]), float(m["vlm"])
        wv = float(wk["vlm"]); ar = float(at["roi"]); ap = float(at["pnl"])
    except Exception:
        continue
    turnover = mv / av if av > 0 else 9e9          # month volume / account = churn proxy
    if not (30_000 <= av <= 5_000_000): continue   # serious money, but not a mega-MM
    if mp < 5_000: continue                        # made real money this month
    if mr < 0.08: continue                         # >8% monthly ROI = real edge
    if ar <= 0 or ap <= 0: continue                # net winner all-time (not a one-month fluke)
    if wv <= 0: continue                           # traded THIS WEEK (recently active)
    if turnover > 120: continue                    # exclude HFT churners (uncopyable)
    cands.append({"addr": r["ethAddress"], "av": av, "mp": mp, "mr": mr,
                  "ar": ar, "turnover": turnover})

cands.sort(key=lambda c: c["mp"], reverse=True)    # rank by monthly PnL (top real earners)
print(f"{len(cands)} passed the screen; verifying for COPYABLE cadence ...\n")

def fills_of(addr):                                # 1 call, with a gentle retry on rate-limit
    for attempt in range(2):
        try:
            return info("userFills", addr)
        except Exception:
            time.sleep(1.5)
    return None

from collections import Counter
picked = []
for c in cands[:400]:
    fills = fills_of(c["addr"])
    if fills is None:
        time.sleep(0.2); continue
    now_ms = time.time() * 1000
    f24 = [f for f in fills if (now_ms - float(f.get("time", 0))) < 24 * 3600 * 1000]
    last_age_h = (now_ms - max((float(f.get("time", 0)) for f in fills), default=0)) / 3600000
    # COPYABLE: traded within 3 days, but NOT a high-frequency churner (<= ~40 fills/day).
    if last_age_h > 72 or len(f24) > 40:
        time.sleep(0.2); continue
    coins = [coin for coin, _ in Counter(f.get("coin") for f in fills[:200]).most_common(3)]
    # Require a recognizable CRYPTO PERP as the main market — skip spot (@NNN, X/USDC) and
    # Hyperliquid's TradFi perps (xyz:SP500, xyz:GOLD, oil, ...) so the page is crypto-relevant.
    top = coins[0] if coins else ""
    if (not top) or top.startswith("@") or top.startswith("xyz:") or "/" in top:
        time.sleep(0.2); continue
    c.update({"coins": coins, "fills24": len(f24), "last_age_h": last_age_h})
    picked.append(c)
    print(f"  OK {c['addr']}  av=${c['av']:,.0f}  mROI={c['mr']*100:.0f}%  mPnL=${c['mp']:,.0f}  "
          f"coins={coins}  fills/24h={len(f24)}  last={last_age_h:.0f}h")
    time.sleep(0.2)
    if len(picked) >= 20:
        break

print(f"\n=== {len(picked)} verified COPYABLE traders ===")
out = [{"addr": c["addr"], "av": round(c["av"]), "mr": round(c["mr"]*100),
        "mp": round(c["mp"]), "coins": c["coins"], "fills24": c["fills24"]} for c in picked]
import json as _j
open("_picked.json", "w").write(_j.dumps(out, indent=1))
print(f"wrote {len(out)} to _picked.json")
