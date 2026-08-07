"""
refresh_wallets.py — one command to refresh the copy-trade roster with ONLY wallets that
are verified funded + active + edged RIGHT NOW.

Pipeline:
  1. Candidate pool = (wallets currently in copy_trader.py) + (_picked.json fresh leaderboard).
  2. verify() each LIVE (verify_wallets.verify): funded / active / copyable / edged / healthy.
  3. Keep survivors, rank by monthly PnL, take top N.
  4. Retain existing names for kept addresses; assign fresh names to new ones.
  5. Rewrite the WALLETS block in copy_trader.py (timestamped .bak backup first).

Run:  venv/Scripts/python.exe refresh_wallets.py [N]      (default N=20)
"""
from __future__ import annotations
import os, re, json, time, sys, urllib.request, shutil
from verify_wallets import verify

HERE = os.path.dirname(os.path.abspath(__file__))
CT = os.path.join(HERE, "copy_trader.py")
NAMES = ["Falcon", "Viper", "Orca", "Lynx", "Cobra", "Wolf", "Raven", "Shark", "Puma",
         "Tiger", "Jaguar", "Panther", "Eagle", "Mamba", "Kodiak", "Osprey", "Marlin",
         "Comet", "Bolt", "Hornet", "Condor", "Bison", "Otter", "Heron", "Stag",
         "Lion", "Gecko", "Crane", "Moose", "Fox", "Rhino", "Badger", "Lemur",
         "Walrus", "Ibex", "Dingo", "Caracal", "Ocelot", "Serval", "Tapir"]


def leaderboard_mr():
    """addr(lower) -> monthly ROI %, from the live board (for blurb display)."""
    try:
        rows = json.loads(urllib.request.urlopen(
            "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard", timeout=25
        ).read())["leaderboardRows"]
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            perf = {w: v for w, v in r["windowPerformances"]}
            out[r["ethAddress"].lower()] = round(float(perf["month"]["roi"]) * 100)
        except Exception:
            continue
    return out


def current_wallets():
    txt = open(CT, encoding="utf-8").read()
    block = txt[txt.index("WALLETS = ["):]
    block = block[:block.index("\n]")]
    return re.findall(r'"name":\s*"([^"]+)",\s*"addr":\s*"([^"]+)"', block)


def clean_coins(coins):
    out = [c for c in coins if not (c.startswith("@") or ":" in c or "/" in c)]
    return out[:2] or ["crypto"]


def fmt_av(av):
    return f"${av/1e6:.1f}M" if av >= 1e6 else f"${av/1e3:.0f}k"


def cadence(f24):
    if f24 == 0:
        return "swing (multi-day holds)"
    if f24 <= 6:
        return f"~{f24}/day (hours-long holds)"
    return f"~{f24} trades/day"


def build_block(rows):
    lines = ["WALLETS = ["]
    for r in rows:
        coins = "/".join(clean_coins(r["coins"]))
        mr = r.get("mr")
        roi = f"+{mr}%/mo" if mr is not None else f"+${r['pnl_month']:,}/mo"
        blurb = f"{coins} · funded {fmt_av(r['av'])} · {roi} · {cadence(r['f24'])}"
        lines.append(f'    {{"name": "{r["name"]}", "addr": "{r["addr"]}",')
        lines.append(f'     "blurb": "{blurb}"}},')
    lines.append("]")
    return "\n".join(lines)


def main():
    topn = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    cur = current_wallets()
    cur_names = {a.lower(): n for n, a in cur}
    pool = {a.lower(): a for _, a in cur}
    if os.path.exists(os.path.join(HERE, "_picked.json")):
        for d in json.load(open(os.path.join(HERE, "_picked.json"))):
            pool[d["addr"].lower()] = d["addr"]
    print(f"Candidate pool: {len(pool)} addresses "
          f"({len(cur)} current + fresh picks). Verifying LIVE ...\n")
    mr_map = leaderboard_mr()

    kept = []
    for a in pool.values():
        m = verify(a)
        if m["verdict"] == "KEEP":
            m["mr"] = mr_map.get(a.lower())
            kept.append(m)
        tag = "KEEP" if m["verdict"] == "KEEP" else m["verdict"]
        print(f"  {a[:10]}… {tag:<34} mo=${m.get('pnl_month',0):>12,} av=${m.get('av',0):>10,}")
        time.sleep(0.2)

    kept.sort(key=lambda x: x["pnl_month"], reverse=True)
    kept = kept[:topn]

    # assign names: retained wallets keep their name; NEW wallets get a genuinely fresh
    # name (never recycle a name from the prior roster — a dead wallet's name pointing to
    # a different trader would be misleading for a trust-critical feature).
    old_names = set(cur_names.values())
    for r in kept:
        nm = cur_names.get(r["addr"].lower())
        if nm:
            r["name"] = nm
    fresh = [n for n in NAMES if n not in old_names]
    fi = 0
    for r in kept:
        if "name" not in r:
            r["name"] = fresh[fi] if fi < len(fresh) else f"T{fi}"; fi += 1

    block = build_block(kept)
    json.dump(kept, open(os.path.join(HERE, "_verified.json"), "w"), indent=1, default=str)

    # rewrite copy_trader.py WALLETS block
    txt = open(CT, encoding="utf-8").read()
    new_txt = re.sub(r"WALLETS = \[.*?\n\]", block, txt, count=1, flags=re.S)
    if new_txt != txt:
        bak = CT + f".bak_{int(time.time())}"
        shutil.copy(CT, bak)
        open(CT, "w", encoding="utf-8").write(new_txt)
        print(f"\nUpdated copy_trader.py WALLETS ({len(kept)} verified wallets). Backup: {os.path.basename(bak)}")
    else:
        print("\nWARNING: WALLETS block not replaced (regex miss).")

    retained = [r["name"] for r in kept if r["addr"].lower() in cur_names]
    added = [r["name"] for r in kept if r["addr"].lower() not in cur_names]
    dropped = [n for a, n in [(a, n) for n, a in cur] if a.lower() not in {r["addr"].lower() for r in kept}]
    print(f"  retained ({len(retained)}): {', '.join(retained)}")
    print(f"  added    ({len(added)}): {', '.join(added)}")
    print(f"  dropped  ({len(dropped)}): {', '.join(dropped)}")


if __name__ == "__main__":
    main()
