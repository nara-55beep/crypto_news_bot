"""OKX test — confirms the FIX (raw public trades endpoint, bypassing the broken load_markets).
Run:  venv\\Scripts\\python.exe okx_diag.py   (or:  ! venv\\Scripts\\python.exe okx_diag.py)
"""
import ccxt

print("=== OLD path (ccxt load_markets — the one that was crashing) ===")
try:
    c = ccxt.okx({"enableRateLimit": True, "timeout": 12000})
    c.load_markets()
    print("  load_markets OK")
except Exception as e:
    print(f"  load_markets FAIL (expected): {type(e).__name__}: {str(e)[:120]}")

print("\n=== NEW path (raw public trades endpoint — what the bot now uses) ===")
try:
    c = ccxt.okx({"enableRateLimit": True, "timeout": 12000})
    raw = c.publicGetMarketTrades({"instId": "BTC-USDT", "limit": "5"})
    data = raw.get("data") or []
    print(f"  publicGetMarketTrades OK -> {len(data)} trades")
    for d in data[:3]:
        print(f"    {d.get('side')}  px={d.get('px')}  sz={d.get('sz')}  ts={d.get('ts')}")
    print("\n  >>> SUCCESS — OKX trades are reachable; the bot will show OKX green.")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {str(e)[:300]}")
    print("\n  >>> If this failed too, OKX itself is blocked on your network.")
print("\nDone — paste this output back.")
