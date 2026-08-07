"""
================================================================================
 diagnose.py  —  quick health check for the CCXT order book / trade tape
================================================================================
Double-click diagnose.bat to run this. It uses the SAME venv the bot uses, then
checks — per exchange — whether THIS PC can reach it and read live BTC data.
It changes nothing; it only reads. Use it whenever the order book / tape is empty.
================================================================================
"""

import sys

print("Python    :", sys.version.split()[0])
print("Running in:", sys.prefix)
print()

# 1) Is CCXT actually installed in THIS python (the venv)?
try:
    import ccxt
    print("ccxt      :", ccxt.__version__)
except Exception as e:
    print("!! ccxt is NOT installed in this python ->", e)
    print()
    print("   FIX — in this folder, run these two lines, then try run.bat again:")
    print("       venv\\Scripts\\activate.bat")
    print("       pip install ccxt")
    sys.exit()

# 2) Which exchanges are we configured to use?
try:
    import config
    exchanges = list(config.EXCHANGES)
except Exception as e:
    print("(could not read config.EXCHANGES:", e, "- using defaults)")
    exchanges = ["binance", "bybit", "okx", "coinbase", "kraken",
                 "gate", "kucoin", "bitget", "hyperliquid"]

CANDS = ["BTC/USDT", "BTC/USD", "BTC/USDC", "BTC/USDT:USDT", "BTC/USDC:USDC"]

print(f"\nTesting {len(exchanges)} exchanges from this PC "
      f"(load markets + read BTC price):\n")

ok = 0
for x in exchanges:
    try:
        ex = getattr(ccxt, x)({"enableRateLimit": True, "timeout": 15000})
        ex.load_markets()
        sym = next((s for s in CANDS if s in ex.markets), None)
        if not sym:
            print(f"  {x:12} no BTC/USDT-like market on this exchange")
            continue
        t = ex.fetch_ticker(sym)
        print(f"  {x:12} OK     {sym:16} last = {t.get('last')}")
        ok += 1
    except Exception as e:
        print(f"  {x:12} FAIL   {type(e).__name__}: {str(e)[:80]}")

print(f"\n{ok}/{len(exchanges)} exchanges reachable from this PC.")

if ok == 0:
    print("""
0 reachable. The most likely cause is ONE of:
  1) ccxt isn't in the venv  -> run the two FIX lines shown above.
  2) Your network / region / firewall blocks these exchanges.
     Many block by country; a VPN often fixes it. Try opening
     https://api.binance.com/api/v3/time in your browser — if that
     fails too, it's a network/region block, not the bot.""")
elif ok < len(exchanges):
    print("""
Some failed — that is normal. Causes are usually a regional block on that
one exchange, or it doesn't list the BTC symbol we look for. The bot simply
skips the failures and uses whatever works. You can also edit the EXCHANGES
list in config.py to drop the ones that fail for you.""")
else:
    print("\nAll good. The order book and trade tape should fill up in the app.")
