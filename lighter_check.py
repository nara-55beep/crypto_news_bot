"""
================================================================================
 lighter_check.py  —  READ-ONLY check of your real Lighter account
================================================================================
Double-click lighter_check.bat to run this. It connects to Lighter and prints
your REAL balance and open positions, to confirm the connection works before we
put it on the website.

Iт IS SAFE: it only READS. It places no orders, needs no private key, and cannot
move or withdraw any funds. All it needs is your account index OR wallet address
(both set in config.py, section 9).
================================================================================
"""

import sys

print("Python:", sys.version.split()[0])

try:
    import ccxt
    print("ccxt  :", ccxt.__version__)
except Exception as e:
    print("!! ccxt is not installed in this python ->", e)
    print("   Run install.bat first.")
    sys.exit()

import config

acct = getattr(config, "LIGHTER_ACCOUNT_INDEX", None)
addr = (getattr(config, "LIGHTER_L1_ADDRESS", "") or "").strip()
testnet = bool(getattr(config, "LIGHTER_TESTNET", False))

if acct is None and not addr:
    print("""
Nothing to check yet. Open config.py (section 9) and fill in ONE of:
    LIGHTER_L1_ADDRESS    = "0xYourWalletAddress"     (easiest — it's public)
  or
    LIGHTER_ACCOUNT_INDEX = 12345                      (your numeric account index)
then run this again.""")
    sys.exit()

# Build a read-only client. No private key is set, so it physically cannot trade.
# ccxt resolves your account index from the wallet address (set as a credential).
client_cfg = {"options": {"defaultType": "swap"}}
if acct is None and addr:
    client_cfg["walletAddress"] = addr      # ccxt looks up your account index from this
ex = ccxt.lighter(client_cfg)
if testnet:
    try:
        ex.set_sandbox_mode(True)
        print("network: TESTNET")
    except Exception:
        print("(could not switch to testnet; using mainnet)")
else:
    print("network: MAINNET (real account)")

if acct is not None:
    ex.options["accountIndex"] = acct
    who = f"account index {acct}"
else:
    who = f"wallet {addr}"

print(f"\nLooking up your Lighter account by {who} …\n")

try:
    ex.load_markets()
except Exception:
    pass  # not strictly needed for balance

# ---- account(s) + balance (both pockets: spot and perps) ----
def _f(x):
    try:
        return f"{float(x):.6f}"
    except Exception:
        return "—" if x in (None, "") else str(x)

# 1) find every account index tied to this wallet
indices = []
if acct is not None:
    indices = [acct]
elif addr:
    try:
        r = ex.publicGetAccountsByL1Address({"l1_address": addr})
        for s in (r.get("sub_accounts") or r.get("accounts") or []):
            i = s.get("account_index", s.get("index"))
            if i is not None and i not in indices:
                indices.append(i)
    except Exception as e:
        print(f"(could not list sub-accounts: {type(e).__name__}: {str(e)[:90]})")

# 2) for each account, read BOTH the spot USDC and the perps collateral
def _usdc(params):
    try:
        b = ex.fetch_balance(params)
        t = b.get("total", {}) or {}
        return t.get("USDC", t.get("USD", t.get("USDT")))
    except Exception as e:
        return f"err:{type(e).__name__}"

if not indices:
    print("No account indices found for this wallet — send me this whole window.")
else:
    print(f"YOUR LIGHTER ACCOUNT(S) — found {len(indices)}:\n")
    for i in indices:
        spot = _usdc({"account_index": i, "type": "spot"})
        perps = _usdc({"account_index": i, "type": "swap"})
        print(f"   account_index {i}")
        print(f"      spot USDC           : {_f(spot)}")
        print(f"      perps USDC (margin) : {_f(perps)}")
    print("\nYour 0.006736 is your SPOT USDC. 'perps USDC' is what you can open leveraged")
    print("trades with. Note the account_index whose spot shows 0.006736 — that's yours.")

print("""
--------------------------------------------------------------------
If your real balance showed up above, the connection works and we can
safely put it on the website next.
If you saw an error, copy this whole window and send it over.
--------------------------------------------------------------------""")