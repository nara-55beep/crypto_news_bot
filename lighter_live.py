"""
lighter_live.py  -  REAL-MONEY Lighter tool
============================================
SAFE commands (no orders, no money moves):
   check      - prove the signer + API key work (signed auth token).
   balance    - show real spot + perps USDC (read-only).
   positions  - show perps collateral + any open positions.

REAL-MONEY commands (place actual orders; guarded by a hard cap + typed YES):
   buy  <usd> - open a tiny LONG  on BTC perps, sized to <usd> notional.
   sell <usd> - open a tiny SHORT on BTC perps, sized to <usd> notional.
   close      - close your BTC perps position (reduce-only).
   menu       - friendly interactive menu (used by lighter_live_trade.bat).

Real orders ONLY run when, in config.py:
   LIGHTER_TRADING_ENABLED = True
   LIGHTER_MAX_ORDER_USD   = <your hard cap, e.g. 5>
...and you type YES at the prompt. Everything stays MANUAL.
"""

import sys
import os
import time
from collections import deque

try:
    import config
except Exception as e:
    print("Could not import config.py:", e)
    sys.exit(1)

# ---- settings (safe defaults so a missing field never crashes) ----
ADDRESS      = (getattr(config, "LIGHTER_L1_ADDRESS", "") or "").strip()
ACCOUNT_IDX  = getattr(config, "LIGHTER_ACCOUNT_INDEX", None)
API_PRIV     = (getattr(config, "LIGHTER_API_PRIVATE_KEY", "") or "").strip()
API_KEY_IDX  = getattr(config, "LIGHTER_API_KEY_INDEX", 3)
SIGNER_PATH  = (getattr(config, "LIGHTER_SIGNER_PATH", "") or "").strip()
TESTNET      = bool(getattr(config, "LIGHTER_TESTNET", False))
CHAIN_ID     = getattr(config, "LIGHTER_CHAIN_ID", None)

TRADING_ENABLED = bool(getattr(config, "LIGHTER_TRADING_ENABLED", False))
try:
    MAX_ORDER_USD = float(getattr(config, "LIGHTER_MAX_ORDER_USD", 2.0))
except Exception:
    MAX_ORDER_USD = 2.0

LINE = "-" * 60

# Lighter REQUIRES a price even on market orders (it's the avg_execution_price /
# worst-case fill). Passing None is what throws the decimal.ConversionSyntax error.
# We pass the mark nudged by this slippage so a market order crosses and fills,
# while capping the worst fill. 2% is negligible on a small BTC order in a deep book.
MARKET_SLIPPAGE = float(getattr(config, "LIGHTER_MARKET_SLIPPAGE", 0.02))

# --- Official Lighter SDK (elliottech/lighter-python, pip: lighter-sdk) ---------
# REAL order placement now goes through Lighter's own SDK, which ships a signer
# binary that MATCHES the protocol it speaks. ccxt's Lighter layer was calling a
# separately-downloaded .dll with mismatched arguments (the "client is not
# created for apiKeyIndex: 192 ..." garbage). The SDK avoids that entirely.
# Reads (balance / positions / price) still use ccxt because they already work.
BASE_URL = (getattr(config, "LIGHTER_BASE_URL", "") or "").strip() or (
    "https://testnet.zklighter.elliot.ai" if TESTNET else "https://mainnet.zklighter.elliot.ai")
MARGIN_MODE = int(getattr(config, "LIGHTER_MARGIN_MODE", 0))   # 0 = cross, 1 = isolated


def _marketable_price(side, ref_px):
    """Worst-case price for a Lighter market order: pay up to +slip on a buy,
    accept down to -slip on a sell. Returns None if ref_px is unusable."""
    try:
        ref_px = float(ref_px)
    except Exception:
        return None
    if ref_px <= 0:
        return None
    s = (side or "").lower()
    return ref_px * (1 + MARKET_SLIPPAGE) if s == "buy" else ref_px * (1 - MARKET_SLIPPAGE)


def _need(cond, msg):
    if not cond:
        print("  [missing] " + msg)
    return bool(cond)


def _make_exchange():
    """Build a ccxt.lighter client with the wallet address credential."""
    import ccxt
    if not getattr(ccxt, "lighter", None):
        print("Your ccxt is too old for Lighter. In the project folder run install.bat,")
        print("or:  venv\\Scripts\\python.exe -m pip install --upgrade ccxt")
        return None, None
    ex = ccxt.lighter({
        "walletAddress": ADDRESS,
        "enableRateLimit": True,
        "timeout": 20000,
    })
    if TESTNET:
        ex.set_sandbox_mode(True)
        print("  mode: TESTNET (practice)")
    else:
        print("  mode: MAINNET (real money)")
    return ex, ccxt


def _signed_exchange(verbose=True):
    """Return a ccxt.lighter ready to SIGN (signer loaded), or None on failure."""
    ok = True
    ok &= _need(ADDRESS, "LIGHTER_L1_ADDRESS")
    ok &= _need(ACCOUNT_IDX is not None, "LIGHTER_ACCOUNT_INDEX")
    ok &= _need(API_PRIV, "LIGHTER_API_PRIVATE_KEY")
    ok &= _need(SIGNER_PATH, "LIGHTER_SIGNER_PATH")
    if not ok:
        print("  Fill the missing field(s) in config.py first.")
        return None

    signer_path = os.path.abspath(SIGNER_PATH)
    if not os.path.isfile(signer_path):
        print(f"  signer file not found at: {signer_path}")
        return None

    ex, ccxt = _make_exchange()
    if ex is None:
        return None
    try:
        ex.load_markets()
        if verbose:
            print("  markets: loaded OK")
    except Exception as e:
        print("  markets: FAILED ->", type(e).__name__, str(e)[:160])
        print("  (can't reach Lighter; check your connection)")
        return None

    chain_id = CHAIN_ID if CHAIN_ID is not None else ex.options.get("chainId")
    ai, ki = str(ACCOUNT_IDX), str(API_KEY_IDX)
    try:
        ex.options["libraryPath"] = signer_path
        ex.init_auth_object(ai, ki)
        ex.options["auths"][ai][ki]["lighterPrivateKey"] = API_PRIV
        ex.load_account(chain_id, API_PRIV, ki, ai)
        if verbose:
            print("  signer: loaded OK")
    except Exception as e:
        print("  signer: FAILED ->", type(e).__name__, str(e)[:200])
        return None
    return ex


def _btc_perp_symbol(ex):
    """Find the BTC perpetual market symbol on Lighter."""
    for m in ex.markets.values():
        try:
            if m.get("base") == "BTC" and (m.get("swap") or m.get("contract")) \
               and m.get("quote") in ("USDC", "USD", "USDT"):
                return m["symbol"]
        except Exception:
            pass
    for s in ("BTC/USDC:USDC", "BTC/USD:USD", "BTC/USDT:USDT", "BTC/USDC"):
        if s in ex.markets:
            return s
    return None


# ============================================================
# SAFE commands
# ============================================================
def cmd_check():
    print(LINE)
    print("STAGE 1 SIGNER CHECK  (no order is placed, no money moves)")
    print(LINE)
    ex = _signed_exchange(verbose=True)
    if ex is None:
        return
    ai, ki = str(ACCOUNT_IDX), str(API_KEY_IDX)
    try:
        token = ex.create_auth({"account_index": int(ai), "api_key_index": int(ki)})
        shown = (token[:14] + "...") if isinstance(token, str) and len(token) > 14 else token
        print(f"  auth token: created OK  ({shown})")
    except Exception as e:
        print("  auth token: FAILED ->", type(e).__name__, str(e)[:200])
        return
    print(LINE)
    print("SUCCESS: signer + API key work.")


def cmd_balance():
    print(LINE)
    print("READ-ONLY BALANCE")
    print(LINE)
    if not _need(ADDRESS or ACCOUNT_IDX is not None, "set LIGHTER_L1_ADDRESS in config.py"):
        return
    ex, ccxt = _make_exchange()
    if ex is None:
        return
    try:
        ex.load_markets()
    except Exception as e:
        print("  could not reach Lighter:", type(e).__name__, str(e)[:160])
        return
    acct = ACCOUNT_IDX
    for kind, typ in (("SPOT", "spot"), ("PERPS margin", "swap")):
        try:
            params = {"type": typ}
            if acct is not None:
                params["accountIndex"] = int(acct)
            bal = ex.fetch_balance(params)
            usdc = (bal.get("USDC") or {})
            print(f"  {kind:<13}: total USDC = {usdc.get('total')}   (available = {usdc.get('free')})")
        except Exception as e:
            print(f"  {kind:<13}: error {type(e).__name__}: {str(e)[:120]}")
    print(LINE)
    print("To TRADE perps you need USDC in the PERPS pocket (use Lighter's Spot<->Perps toggle).")


def cmd_positions():
    print(LINE)
    print("PERPS BALANCE + OPEN POSITIONS")
    print(LINE)
    ex = _signed_exchange(verbose=False)
    if ex is None:
        return
    acct = ACCOUNT_IDX
    try:
        bal = ex.fetch_balance({"type": "swap", "accountIndex": int(acct)} if acct is not None else {"type": "swap"})
        usdc = (bal.get("USDC") or {})
        print(f"  perps collateral USDC: total = {usdc.get('total')}   available = {usdc.get('free')}")
    except Exception as e:
        print("  balance error:", type(e).__name__, str(e)[:140])
    try:
        poss = ex.fetch_positions()
        live = [p for p in poss if (p.get("contracts") or 0)]
        if not live:
            print("  open positions: none")
        else:
            for p in live:
                print(f"  position: {p.get('symbol')}  side={p.get('side')}  size={p.get('contracts')}  "
                      f"entry={p.get('entryPrice')}  uPnL={p.get('unrealizedPnl')}")
    except Exception as e:
        print("  positions error:", type(e).__name__, str(e)[:140])


# ============================================================
# REAL-MONEY commands
# ============================================================
def _place(side, usd_str):
    print(LINE)
    print(f"REAL-MONEY {side.upper()} ORDER  (BTC perps)")
    print(LINE)

    if not TRADING_ENABLED:
        print("  BLOCKED: LIGHTER_TRADING_ENABLED is False.")
        print("  To place real orders, add this line to config.py section 9:")
        print("      LIGHTER_TRADING_ENABLED = True")
        return
    try:
        usd = float(usd_str)
    except Exception:
        print(f"  '{usd_str}' is not a number.")
        return
    if usd <= 0:
        print("  amount must be greater than 0.")
        return

    ex = _signed_exchange(verbose=False)
    if ex is None:
        return
    sym = _btc_perp_symbol(ex)
    if not sym:
        print("  could not find the BTC perps market on Lighter.")
        return

    try:
        t = ex.fetch_ticker(sym)
        price = t.get("last") or t.get("close") or t.get("ask") or t.get("bid")
        price = float(price)
    except Exception as e:
        print("  could not read BTC price:", type(e).__name__, str(e)[:140])
        return

    amount = usd / price
    try:
        amount = float(ex.amount_to_precision(sym, amount))
    except Exception:
        pass

    mkt = ex.market(sym)
    limits = (mkt.get("limits") or {}).get("amount") or {}
    min_amt = limits.get("min")
    if (not amount) or amount <= 0 or (min_amt and amount < float(min_amt)):
        need_notional = (float(min_amt) * price) if min_amt else None
        print(f"  TOO SMALL: ${usd:.2f} -> {amount} BTC, but the market minimum is "
              f"{min_amt} BTC" + (f" (~${need_notional:.2f})." if need_notional else "."))
        if need_notional:
            print(f"  Use at least ~${need_notional:.2f} (and raise LIGHTER_MAX_ORDER_USD to match).")
        return

    notional = amount * price
    print(f"  market: {sym}")
    print(f"  side  : {side.upper()}")
    print(f"  size  : {amount} BTC   (~${notional:.2f} at BTC=${price:,.0f})")
    print("  NOTE  : margin used depends on the leverage set for BTC on Lighter.")
    print()
    print("  This places a REAL order with REAL money.")
    try:
        confirm = input("  Type  YES  (capitals) to place it, anything else to cancel: ").strip()
    except EOFError:
        confirm = ""
    if confirm != "YES":
        print("  cancelled. nothing was placed.")
        return

    try:
        order = ex.create_order(sym, "market", side, amount, _marketable_price(side, price))
        print("  ORDER SENT.")
        print(f"    id     : {order.get('id')}")
        print(f"    status : {order.get('status')}")
        print(f"    filled : {order.get('filled')}  avg: {order.get('average')}")
    except Exception as e:
        msg = str(e)
        print("  ORDER FAILED ->", type(e).__name__, msg[:240])
        low = msg.lower()
        if "publickey" in low or "update the sdk" in low or "asset" in low:
            print("  -> the 'signer lags the API' case. Re-download the latest .dll and retry.")
        elif "margin" in low or "insufficient" in low or "balance" in low or "collateral" in low:
            print("  -> not enough USDC in the PERPS pocket. Deposit + move funds to perps.")
        elif "nonce" in low:
            print("  -> a nonce issue; just run it again.")
        return
    print()
    cmd_positions()


def cmd_close():
    print(LINE)
    print("CLOSE BTC PERPS POSITION  (reduce-only)")
    print(LINE)
    ex = _signed_exchange(verbose=False)
    if ex is None:
        return
    sym = _btc_perp_symbol(ex)
    if not sym:
        print("  could not find the BTC perps market.")
        return
    try:
        poss = ex.fetch_positions([sym])
    except Exception:
        try:
            poss = ex.fetch_positions()
        except Exception as e:
            print("  could not read positions:", type(e).__name__, str(e)[:140])
            return
    pos = None
    for p in poss:
        if p.get("symbol") == sym and (p.get("contracts") or 0):
            pos = p
            break
    if not pos:
        print("  no open BTC position to close.")
        return
    size = abs(float(pos.get("contracts")))
    close_side = "sell" if pos.get("side") == "long" else "buy"
    print(f"  position: {pos.get('side')}  size={size} BTC  -> closing with a {close_side.upper()}")
    try:
        confirm = input("  Type  YES  to close it: ").strip()
    except EOFError:
        confirm = ""
    if confirm != "YES":
        print("  cancelled.")
        return
    try:
        try:
            tk = ex.fetch_ticker(sym)
            ref = tk.get("last") or tk.get("close") or tk.get("ask") or tk.get("bid")
        except Exception:
            ref = pos.get("markPrice") or pos.get("entryPrice")
        order = ex.create_order(sym, "market", close_side, size,
                                _marketable_price(close_side, ref), {"reduceOnly": True})
        print("  CLOSE SENT. id:", order.get("id"), " status:", order.get("status"))
    except Exception as e:
        print("  CLOSE FAILED ->", type(e).__name__, str(e)[:200])
        return
    print()
    cmd_positions()


def cmd_menu():
    print()
    print("Lighter REAL-MONEY menu")
    print(f"  trading enabled: {TRADING_ENABLED}    per-order cap: ${MAX_ORDER_USD:.2f}")
    print(LINE)
    print("  1 = check balance + positions   (safe)")
    print("  2 = place tiny BUY  (LONG)      [REAL MONEY]")
    print("  3 = place tiny SELL (SHORT)     [REAL MONEY]")
    print("  4 = CLOSE BTC position          [REAL MONEY]")
    print("  0 = quit")
    print(LINE)
    try:
        choice = input("  choose 0-4: ").strip()
    except EOFError:
        choice = "0"
    if choice == "1":
        cmd_positions()
    elif choice == "2":
        usd = input("  USD amount for the LONG: ").strip()
        _place("buy", usd)
    elif choice == "3":
        usd = input("  USD amount for the SHORT: ").strip()
        _place("sell", usd)
    elif choice == "4":
        cmd_close()
    else:
        print("  bye.")


class LighterLive:
    """Reusable real-money Lighter access for the dashboard. No dollar cap.
    Builds and signs ONE ccxt instance lazily and reuses it (nonce-safe when
    the caller serializes calls)."""

    def __init__(self):
        self.address = ADDRESS
        self.account_idx = ACCOUNT_IDX
        self.api_priv = API_PRIV
        self.api_key_idx = API_KEY_IDX
        self.signer_path = os.path.abspath(SIGNER_PATH) if SIGNER_PATH else ""
        self.testnet = TESTNET
        self.chain_id = CHAIN_ID
        self.trading_enabled = TRADING_ENABLED
        self._ex = None
        self._symbol = None
        # --- reused official-SDK plumbing (so a buy doesn't rebuild everything) ---
        self._loop = None          # one background event loop for all SDK calls
        self._sc = None            # the reused SignerClient (created once)
        self._order_lock = None    # serialises orders on the loop (nonce safety)
        self._sdk_lev = {}         # market_index -> leverage we last set on Lighter
        # ---- Lighter's OWN recent-trades tape (so flow matches the Lighter screen) ----
        self._ltrades = deque(maxlen=8000)   # (ts_ms, usd, is_buy) — Lighter's taker flow
        self._ltrade_max_id = 0              # highest trade_id seen (monotonic de-dupe cursor)
        self._ltrade_err = ""

    def configured(self):
        return bool(self.address and self.account_idx is not None and self.api_priv
                    and self.signer_path and os.path.isfile(self.signer_path))

    def why_not_configured(self):
        miss = []
        if not self.address: miss.append("LIGHTER_L1_ADDRESS")
        if self.account_idx is None: miss.append("LIGHTER_ACCOUNT_INDEX")
        if not self.api_priv: miss.append("LIGHTER_API_PRIVATE_KEY")
        if not self.signer_path: miss.append("LIGHTER_SIGNER_PATH")
        elif not os.path.isfile(self.signer_path): miss.append("signer .dll not found at " + self.signer_path)
        return miss

    def _ensure(self):
        if self._ex is not None:
            return self._ex
        import ccxt
        if not getattr(ccxt, "lighter", None):
            raise RuntimeError("ccxt too old for Lighter; run install.bat")
        ex = ccxt.lighter({"walletAddress": self.address, "enableRateLimit": True, "timeout": 20000})
        if self.testnet:
            ex.set_sandbox_mode(True)
        ex.load_markets()
        chain = self.chain_id if self.chain_id is not None else ex.options.get("chainId")
        ai = str(self.account_idx)
        try:
            ki_int = int(self.api_key_idx)
        except Exception:
            ki_int = -1
        if not (4 <= ki_int <= 254):
            raise RuntimeError(
                f"LIGHTER_API_KEY_INDEX is {self.api_key_idx}, but Lighter API keys must use an "
                "index between 4 and 254. Create an API key at a valid index on "
                "app.lighter.xyz/apikeys and set LIGHTER_API_KEY_INDEX in config.py to match.")
        ki = str(ki_int)
        ex.options["libraryPath"] = self.signer_path
        # CRITICAL: make EVERY signed call (createOrder, close, ...) use OUR account + api-key
        # index. Without this, ccxt's createOrder defaults the api-key index to 254 and signs
        # under it, but the signer/key are loaded under our configured index — so the signer
        # comes back None and signing dies with 'NoneType has no attribute SignCreateOrder'.
        ex.options["apiKeyIndex"] = ki_int
        ex.options["accountIndex"] = int(self.account_idx)
        ex.init_auth_object(ai, ki)
        ex.options["auths"][ai][ki]["lighterPrivateKey"] = self.api_priv
        ex.load_account(chain, self.api_priv, ki, ai)
        # confirm the signer (.dll) actually bound to our account/key — fail clearly if not
        try:
            signer = (ex.options.get("auths", {}).get(ai, {}).get(ki, {}) or {}).get("signer")
        except Exception:
            signer = None
        if signer is None:
            raise RuntimeError(
                "Lighter signer did not load. Check LIGHTER_SIGNER_PATH points to the correct "
                "signer .dll for your machine, and that LIGHTER_API_KEY_INDEX / "
                "LIGHTER_ACCOUNT_INDEX match the API key you created on Lighter.")
        self._ex = ex
        self._symbol = _btc_perp_symbol(ex)
        return ex

    def state(self):
        if not self.configured():
            return {"ok": False, "configured": False, "missing": self.why_not_configured()}
        try:
            ex = self._ensure()
        except Exception as e:
            return {"ok": False, "configured": True, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        out = {"ok": True, "configured": True, "symbol": self._symbol,
               "trading_enabled": self.trading_enabled, "testnet": self.testnet}
        try:
            mkt = ex.market(self._symbol)
            mx = ((mkt.get("limits") or {}).get("leverage") or {}).get("max")
            out["max_leverage"] = int(mx) if mx else None
        except Exception:
            out["max_leverage"] = None
        try:
            t = ex.fetch_ticker(self._symbol)
            out["price"] = t.get("last") or t.get("close") or t.get("ask")
            if out["price"]:                     # cache it so a MARKET order can skip the slow
                self._last_px = float(out["price"])   # get_best_price round-trip (see _sdk_order)
                self._last_px_ts = time.time()
        except Exception:
            out["price"] = None
        try:
            bal = ex.fetch_balance({"type": "swap", "accountIndex": int(self.account_idx)})
            u = bal.get("USDC") or {}
            out["balance"] = {"total": u.get("total"), "free": u.get("free")}
        except Exception as e:
            out["balance"] = None
            out["balance_error"] = str(e)[:160]
        try:
            def _f(*vals):                       # first value that parses as a float
                for v in vals:
                    if v is not None and v != "":
                        try:
                            return float(v)
                        except Exception:
                            pass
                return None
            poss = ex.fetch_positions()
            live = []
            for p in poss:
                info = p.get("info") or {}
                # SIZE: ccxt 'contracts', else Lighter's raw 'position'
                size = p.get("contracts")
                if not size:
                    size = _f(info.get("position"))
                if not size:
                    continue
                size = abs(size)
                # SIDE: ccxt side, else from Lighter's signed 'position' / 'sign'
                side = p.get("side")
                if side not in ("long", "short"):
                    sgn = _f(info.get("position"), info.get("sign"))
                    side = "long" if (sgn is None or sgn >= 0) else "short"
                # These are LIGHTER'S OWN numbers (raw info) — the source of truth. ccxt's
                # unified entryPrice/unrealizedPnl are often empty for Lighter, which is why
                # the panel used to show the bot's wrong estimate instead.
                live.append({
                    "symbol": p.get("symbol"), "side": side, "size": size,
                    "entry": _f(info.get("avg_entry_price"), p.get("entryPrice")),
                    "pnl": _f(info.get("unrealized_pnl"), p.get("unrealizedPnl")),
                    "realized": _f(info.get("realized_pnl")),
                    "notional": _f(info.get("position_value"), p.get("notional")),
                    "liq": _f(info.get("liquidation_price"), p.get("liquidationPrice")),
                    "leverage": p.get("leverage"),
                })
            out["positions"] = live
        except Exception as e:
            out["positions"] = []
            out["positions_error"] = str(e)[:160]
        self._warm()          # pre-build the SDK signer in the background (fast first buy)
        return out

    # ----- official Lighter SDK order placement (matched signer) ---------------
    # The SDK is async. To avoid rebuilding the signing client (and re-fetching the
    # nonce from the server) on every order, we run ONE background event loop and
    # keep ONE SignerClient alive on it. Blocking order()/close() just hand a
    # coroutine to that loop. This is what makes a buy fast instead of ~8s.
    def _sdk_loop(self):
        import asyncio, threading
        loop = self._loop
        if loop is not None and not loop.is_closed():
            return loop
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True, name="lighter-sdk").start()
        self._loop = loop
        return loop

    def _sdk_submit(self, coro, timeout=30):
        """Run an SDK coroutine on the background loop and wait for the result."""
        import asyncio
        loop = self._sdk_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    def _warm(self):
        """Fire-and-forget: build the signing client + cache the market on the
        background loop so the first buy isn't a cold start. No-ops once warm."""
        if not self.trading_enabled or self._sc is not None:
            return
        import asyncio
        async def _go():
            try:
                c = await self._client()
                await self._sdk_btc_market(c)
            except Exception:
                pass
        try:
            asyncio.run_coroutine_threadsafe(_go(), self._sdk_loop())
        except Exception:
            pass

    async def _client(self):
        """The reused SignerClient. Built once (key checked once) and kept alive on
        the background loop, so its aiohttp session + nonce state persist."""
        c = self._sc
        if c is not None:
            return c
        import lighter
        c = lighter.SignerClient(
            url=BASE_URL,
            account_index=int(self.account_idx),
            api_private_keys={int(self.api_key_idx): self.api_priv},
        )
        try:
            cerr = c.check_client()
        except Exception:
            cerr = None
        if cerr:
            try:
                await c.close()
            except Exception:
                pass
            raise RuntimeError(f"Lighter rejected API key index {self.api_key_idx}: {str(cerr)[:140]}")
        self._sc = c
        return c

    async def _sdk_market(self, client, symbol="BTC"):
        """Find + cache ANY Lighter perp market by symbol: index, size/price decimals, min size.
        Uses Lighter's OWN order_book_details, so the market_index is authoritative (never a
        guess) — that's what makes trading an arbitrary market safe."""
        sym = (symbol or "BTC").upper().split("/")[0].split(":")[0]
        cache = getattr(self, "_sdk_mkts", None)
        if cache is None:
            cache = {}
            self._sdk_mkts = cache
        if sym in cache:
            return cache[sym]
        details = await client.order_api.order_book_details()
        rows = getattr(details, "order_book_details", None) or []
        pick = None
        for r in rows:
            if (getattr(r, "symbol", "") or "").upper() == sym:
                pick = r
                break
        if pick is None:
            return None
        m = {
            "market_index": int(getattr(pick, "market_id")),
            "size_decimals": int(getattr(pick, "size_decimals")),
            "price_decimals": int(getattr(pick, "price_decimals")),
            "min_base_amount": getattr(pick, "min_base_amount", None),
            "symbol": getattr(pick, "symbol", sym),
        }
        cache[sym] = m
        return m

    async def _sdk_btc_market(self, client):
        """Back-compat shim — BTC market lookup (the default trading market)."""
        return await self._sdk_market(client, "BTC")

    async def _sdk_order(self, side, price_usd=None, notional_usd=None, leverage=None,
                         reduce_only=False, close_btc=None, limit_price=None, post_only=False,
                         market="BTC", ioc=False, slippage=None):
        """Place a REAL order through Lighter's own SDK. Market (IOC) by default;
        pass limit_price for a GTT limit order, post_only=True for a guaranteed-MAKER limit
        (rejected if it would cross). Reuses one SignerClient and only touches leverage when
        it actually changes, so repeat orders are fast."""
        import asyncio
        import time as _t
        _t0 = _t.perf_counter(); ts = {}; used_gbp = False; lev_fired = False
        try:
            client = await self._client()
        except Exception as e:
            return {"ok": False, "error": str(e)[:180]}
        ts["client"] = _t.perf_counter()
        if self._order_lock is None:
            self._order_lock = asyncio.Lock()
        async with self._order_lock:          # one order at a time -> nonce-safe
            ts["lock"] = _t.perf_counter()
            try:
                m = await self._sdk_market(client, market)
                ts["market"] = _t.perf_counter()
                if not m:
                    return {"ok": False, "error": f"market '{market}' not found on Lighter"}
                mkt, szd, pxd = m["market_index"], m["size_decimals"], m["price_decimals"]
                is_ask = (side == "sell")
                # reference price (USD): caller's mark, the limit price, or Lighter's book
                ref = 0.0
                if limit_price:
                    ref = float(limit_price)
                elif price_usd and float(price_usd) > 0:
                    ref = float(price_usd)
                elif (str(market).upper().startswith("BTC") and getattr(self, "_last_px", 0)
                      and (_t.time() - getattr(self, "_last_px_ts", 0)) < 15.0):
                    # FAST PATH (BTC opens AND closes): use Lighter's just-polled BTC price instead
                    # of an inline get_best_price order-book REST call. The 2% slippage cap makes a
                    # ~1s-old price safe to cross with — removes a network round-trip. Only for BTC
                    # (that's the market state() polls); other markets read their own best price.
                    ref = float(self._last_px)
                else:
                    used_gbp = True
                    try:
                        best_int = await client.get_best_price(mkt, is_ask)
                        ref = (best_int or 0) / (10 ** pxd)
                    except Exception as e:
                        return {"ok": False, "error": f"price read failed: {str(e)[:140]}"}
                ts["price"] = _t.perf_counter()
                if ref <= 0:
                    return {"ok": False, "error": "no usable price"}
                btc_qty = float(close_btc) if close_btc is not None else (float(notional_usd) / ref)
                base_amount = int(round(btc_qty * (10 ** szd)))
                min_base = 0
                try:
                    if m.get("min_base_amount") is not None:
                        min_base = int(round(float(m["min_base_amount"]) * (10 ** szd)))
                except Exception:
                    min_base = 0
                floor = max(1, min_base)
                if base_amount < floor:
                    approx = (floor / (10 ** szd)) * ref
                    return {"ok": False, "error": f"size below Lighter minimum (~${approx:.2f}); increase it"}
                # leverage: only send the (slow) leverage tx when it actually changes
                lev_note = None
                if leverage is not None and close_btc is None and self._sdk_lev.get(mkt) != int(leverage):
                    lev_fired = True
                    try:
                        _, _, lerr = await client.update_leverage(
                            mkt, MARGIN_MODE, int(leverage), api_key_index=int(self.api_key_idx))
                        if lerr:
                            lev_note = f"leverage not set ({str(lerr)[:50]})"
                        else:
                            self._sdk_lev[mkt] = int(leverage)
                    except Exception as e:
                        lev_note = f"leverage not set ({str(e)[:50]})"
                ts["lev"] = _t.perf_counter()
                self._coi = (getattr(self, "_coi", 0) + 1) % 1000
                coi = (int(_t.time() * 1000) * 1000 + self._coi) % (2 ** 31 - 1)
                if limit_price:
                    price_int = int(round(float(limit_price) * (10 ** pxd)))
                    # POST_ONLY → guaranteed maker (the order is rejected if it would cross the
                    # book and take). That's the whole point of the maker strategy: add liquidity,
                    # earn the half-spread, never pay it.
                    # POST_ONLY = maker-only; IOC = fill instantly at this price-or-better and
                    # cancel the rest (no resting order); else GTT (rests until filled/expiry).
                    tif = (client.ORDER_TIME_IN_FORCE_POST_ONLY if post_only
                           else client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL if ioc
                           else client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME)
                    # A resting order (GTT / post-only) needs the 28-day expiry; an IOC order is
                    # immediate and must carry expiry 0 (Lighter rejects a future expiry on IOC).
                    expiry = 0 if ioc else client.DEFAULT_28_DAY_ORDER_EXPIRY
                    tx, resp, err = await client.create_order(
                        market_index=mkt, client_order_index=coi, base_amount=base_amount,
                        price=price_int, is_ask=is_ask, order_type=client.ORDER_TYPE_LIMIT,
                        time_in_force=tif,
                        reduce_only=bool(reduce_only),
                        order_expiry=expiry,
                        api_key_index=int(self.api_key_idx))
                else:
                    # slippage cap: how far past the best price we'll accept a fill. Default is the
                    # global 2%; pass a small value (e.g. 0.0003) for a TOP-OF-BOOK marketable order
                    # that fills at ~the best price or not at all (no walking the book).
                    slip = MARKET_SLIPPAGE if slippage is None else float(slippage)
                    price_int = int(round(ref * (10 ** pxd)))
                    worst = int(price_int * (1 + slip)) if not is_ask else int(price_int * (1 - slip))
                    if worst <= 0:
                        return {"ok": False, "error": "computed bad price"}
                    tx, resp, err = await client.create_market_order(
                        market_index=mkt, client_order_index=coi, base_amount=base_amount,
                        avg_execution_price=worst, is_ask=is_ask, reduce_only=bool(reduce_only),
                        api_key_index=int(self.api_key_idx))
                ts["send"] = _t.perf_counter()
                # ---- TIMING BREAKDOWN (prints to your terminal) — this is how we find the lag ----
                def _ms(a, b):
                    return (ts[b] - ts[a]) * 1000 if (a in ts and b in ts) else -1.0
                try:
                    print(f"[lighter-timing] {market} {'limit' if limit_price else 'market'} {side}: "
                          f"connect={(ts['client']-_t0)*1000:.0f}ms  lock={_ms('client','lock'):.0f}ms  "
                          f"market_lookup={_ms('lock','market'):.0f}ms  "
                          f"price={_ms('market','price'):.0f}ms(orderbook_fetch={'YES' if used_gbp else 'no'})  "
                          f"leverage_tx={_ms('price','lev'):.0f}ms(fired={'YES' if lev_fired else 'no'})  "
                          f"SEND_TX={_ms('lev','send'):.0f}ms  >>> TOTAL={(ts['send']-_t0)*1000:.0f}ms")
                except Exception:
                    pass
                if err is not None:
                    return {"ok": False, "error": f"Lighter: {str(err)[:200]}", "lev_note": lev_note}
                return {"ok": True, "id": coi, "side": side, "amount": round(btc_qty, 8),
                        "base_amount": base_amount, "average": ref,
                        "notional": round(btc_qty * ref, 2),
                        "type": "limit" if limit_price else "market", "lev_note": lev_note,
                        "ms": round((ts['send']-_t0)*1000)}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    def order(self, side, usd=None, margin=None, leverage=None,
              order_type="market", price=None, reduce_only=False, post_only=False, market="BTC",
              ioc=False, slippage=None):
        side = (side or "").lower()
        if side not in ("buy", "sell"):
            return {"ok": False, "error": "side must be buy or sell"}
        if not self.trading_enabled:
            return {"ok": False, "error": "real trading is OFF. Set LIGHTER_TRADING_ENABLED = True in config.py"}
        order_type = (order_type or "market").lower()
        # size: either a direct USD notional, or margin x leverage (like the paper panel)
        lev = None
        try:
            if margin is not None and leverage is not None:
                lev = float(leverage)
                notional = float(margin) * lev
            elif usd is not None:
                notional = float(usd)
            else:
                return {"ok": False, "error": "no size given"}
        except Exception:
            return {"ok": False, "error": "size is not a number"}
        if notional <= 0:
            return {"ok": False, "error": "size must be greater than 0"}
        try:
            limit_price = float(price) if price not in (None, "", 0, "0") else None
        except Exception:
            limit_price = None
        if order_type == "limit" and not limit_price:
            return {"ok": False, "error": "a limit order needs a price"}
        # No ccxt here: the SDK reads Lighter's own book for the price and signs/sends,
        # all on the one reused client. That's the difference between fast and ~8s.
        # MARKET orders read LIGHTER'S OWN best price inside _sdk_order (get_best_price) and
        # cross it with the slippage buffer — that is what actually fills. Do NOT feed an
        # outside (Binance) price here as the cap: it's a different book and the order won't
        # fill. price_usd stays None for market; for a LIMIT order the price IS the limit price.
        return self._sdk_submit(self._sdk_order(
            side, price_usd=None, notional_usd=notional,
            leverage=(int(lev) if lev else None), reduce_only=reduce_only,
            limit_price=(limit_price if order_type == "limit" else None),
            post_only=bool(post_only), market=market, ioc=bool(ioc), slippage=slippage))

    def order_best_ioc(self, side, usd=None, margin=None, leverage=None,
                       cap_price=None, market="BTC", attempts=8, delay_ms=60):
        """REAL order: repeatedly read the top 100 Lighter book levels and send a
        single IOC limit at the current BEST allowed level, never worse than cap_price.

        For a buy, allowed means ask <= cap_price. For a sell, allowed means bid >=
        cap_price. This avoids walking the book up to a loose cap; each attempt only
        takes the current top level. If it disappears, retry from a fresh snapshot.
        """
        side = (side or "").lower()
        if side not in ("buy", "sell"):
            return {"ok": False, "error": "side must be buy or sell"}
        if not self.trading_enabled:
            return {"ok": False, "error": "real trading is OFF. Set LIGHTER_TRADING_ENABLED = True in config.py"}
        try:
            cap = float(cap_price)
        except Exception:
            return {"ok": False, "error": "cap_price is not a number"}
        if cap <= 0:
            return {"ok": False, "error": "cap_price must be positive"}
        try:
            if margin is not None and leverage is not None:
                lev = float(leverage)
                notional = float(margin) * lev
            elif usd is not None:
                lev = None
                notional = float(usd)
            else:
                return {"ok": False, "error": "no size given"}
        except Exception:
            return {"ok": False, "error": "size is not a number"}
        if notional <= 0:
            return {"ok": False, "error": "size must be greater than 0"}
        try:
            tries = max(1, min(int(attempts), 25))
        except Exception:
            tries = 8
        try:
            delay = max(0, min(int(delay_ms), 250))
        except Exception:
            delay = 60
        return self._sdk_submit(self._sdk_order_best_ioc(
            side, notional_usd=notional, leverage=(int(lev) if lev else None),
            cap_price=cap, market=market, attempts=tries, delay_ms=delay))

    async def _sdk_order_best_ioc(self, side, notional_usd, leverage=None,
                                  cap_price=None, market="BTC", attempts=8, delay_ms=60):
        import asyncio
        import time as _t
        _t0 = _t.perf_counter()
        try:
            client = await self._client()
        except Exception as e:
            return {"ok": False, "error": str(e)[:180]}
        if self._order_lock is None:
            self._order_lock = asyncio.Lock()
        async with self._order_lock:
            try:
                m = await self._sdk_market(client, market)
                if not m:
                    return {"ok": False, "error": f"market '{market}' not found on Lighter"}
                mkt, szd, pxd = m["market_index"], m["size_decimals"], m["price_decimals"]
                is_ask = (side == "sell")
                cap = float(cap_price)
                lev_note = None
                if leverage is not None and self._sdk_lev.get(mkt) != int(leverage):
                    try:
                        _, _, lerr = await client.update_leverage(
                            mkt, MARGIN_MODE, int(leverage), api_key_index=int(self.api_key_idx))
                        if lerr:
                            lev_note = f"leverage not set ({str(lerr)[:50]})"
                        else:
                            self._sdk_lev[mkt] = int(leverage)
                    except Exception as e:
                        lev_note = f"leverage not set ({str(e)[:50]})"
                last = "no allowed top-of-book price"
                for attempt in range(1, int(attempts) + 1):
                    ob = await client.order_api.order_book_orders(mkt, 100)
                    levels = (ob.bids if is_ask else ob.asks) or []
                    best = None
                    for lvl in levels:
                        try:
                            px = float(lvl.price)
                        except Exception:
                            continue
                        if (not is_ask and px <= cap) or (is_ask and px >= cap):
                            best = px
                            break
                    if best is None:
                        if delay_ms and attempt < int(attempts):
                            await asyncio.sleep(float(delay_ms) / 1000.0)
                        continue
                    btc_qty = float(notional_usd) / best
                    base_amount = int(round(btc_qty * (10 ** szd)))
                    min_base = 0
                    try:
                        if m.get("min_base_amount") is not None:
                            min_base = int(round(float(m["min_base_amount"]) * (10 ** szd)))
                    except Exception:
                        min_base = 0
                    floor = max(1, min_base)
                    if base_amount < floor:
                        approx = (floor / (10 ** szd)) * best
                        return {"ok": False, "error": f"size below Lighter minimum (~${approx:.2f}); increase it"}
                    self._coi = (getattr(self, "_coi", 0) + 1) % 1000
                    coi = (int(_t.time() * 1000) * 1000 + self._coi) % (2 ** 31 - 1)
                    _, _, err = await client.create_order(
                        market_index=mkt, client_order_index=coi, base_amount=base_amount,
                        price=int(round(best * (10 ** pxd))), is_ask=is_ask,
                        order_type=client.ORDER_TYPE_LIMIT,
                        time_in_force=client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                        reduce_only=False, order_expiry=0,
                        api_key_index=int(self.api_key_idx))
                    if err is None:
                        try:
                            print(f"[lighter-timing] {market} best-ioc {side}: "
                                  f"attempt={attempt}/{attempts} cap={cap:,.2f} best={best:,.2f} "
                                  f">>> TOTAL={(_t.perf_counter()-_t0)*1000:.0f}ms")
                        except Exception:
                            pass
                        return {"ok": True, "id": coi, "side": side, "amount": round(btc_qty, 8),
                                "average": best, "notional": round(btc_qty * best, 2),
                                "type": "best_ioc", "attempts": attempt, "cap_price": cap,
                                "lev_note": lev_note}
                    last = str(err)[:180]
                    if delay_ms and attempt < int(attempts):
                        await asyncio.sleep(float(delay_ms) / 1000.0)
                return {"ok": False, "error": last}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    def cancel_all(self):
        """Cancel every resting order for this account (immediate). Used by the maker
        strategy to pull / reprice its post-only quotes. Safe no-op if nothing rests."""
        if not self.trading_enabled:
            return {"ok": False, "error": "real trading is OFF"}
        return self._sdk_submit(self._sdk_cancel_all())

    async def _sdk_cancel_all(self):
        try:
            client = await self._client()
        except Exception as e:
            return {"ok": False, "error": str(e)[:180]}
        if self._order_lock is None:
            import asyncio
            self._order_lock = asyncio.Lock()
        async with self._order_lock:
            try:
                # IMMEDIATE cancel: the time argument MUST be nil/0 (a timestamp is only for the
                # SCHEDULED variant) — Lighter rejects it otherwise ("CancelAllTime should be nil").
                _, _, err = await client.cancel_all_orders(
                    client.CANCEL_ALL_TIF_IMMEDIATE, 0,
                    api_key_index=int(self.api_key_idx))
                if err is not None:
                    return {"ok": False, "error": f"Lighter: {str(err)[:180]}"}
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}"}

    def set_leverage(self, leverage, market="BTC"):
        """Set the account's perp leverage for `market` NOW (and cache it) so a later order
        doesn't have to send the (slow) leverage transaction inline. Call this when the user
        changes the leverage slider — it makes the actual order fast."""
        if not self.trading_enabled:
            return {"ok": False, "error": "real trading is OFF"}
        try:
            lev = int(float(leverage))
        except Exception:
            return {"ok": False, "error": "leverage is not a number"}
        if lev < 1:
            return {"ok": False, "error": "leverage must be >= 1"}
        return self._sdk_submit(self._sdk_set_leverage(lev, market))

    async def _sdk_set_leverage(self, leverage, market="BTC"):
        try:
            client = await self._client()
            m = await self._sdk_market(client, market)
            if not m:
                return {"ok": False, "error": "BTC perps market not found on Lighter"}
            mkt = m["market_index"]
            if self._sdk_lev.get(mkt) == int(leverage):
                return {"ok": True, "leverage": leverage, "cached": True}
            if self._order_lock is None:
                import asyncio
                self._order_lock = asyncio.Lock()
            async with self._order_lock:
                _, _, lerr = await client.update_leverage(
                    mkt, MARGIN_MODE, int(leverage), api_key_index=int(self.api_key_idx))
            if lerr is not None:
                return {"ok": False, "error": f"Lighter: {str(lerr)[:160]}"}
            self._sdk_lev[mkt] = int(leverage)
            return {"ok": True, "leverage": leverage}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}

    def close(self, symbol=None, size=None, side=None, market="BTC", slippage=None):
        # FAST PATH: if the caller (the panel) already knows the open position's size + side,
        # close it STRAIGHT through the SDK. The slow ~10s on close was the CCXT fetch_positions
        # read below; the SDK reduce-only order itself is fast. reduce_only guarantees this can
        # only CLOSE (never flip), so a slightly-stale size is safe.
        try:
            if size and side in ("long", "short"):
                close_side = "sell" if side == "long" else "buy"
                res = self._sdk_submit(self._sdk_order(close_side, price_usd=None,
                                                       reduce_only=True, close_btc=abs(float(size)),
                                                       market=market, slippage=slippage))
                if isinstance(res, dict) and res.get("ok"):
                    res["closed"] = abs(float(size))
                return res
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        # FALLBACK (no size passed, e.g. a direct API call): read the position, then close.
        try:
            ex = self._ensure()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        sym = symbol or self._symbol
        try:
            poss = ex.fetch_positions([sym])
        except Exception:
            try:
                poss = ex.fetch_positions()
            except Exception as e:
                return {"ok": False, "error": f"positions read failed: {str(e)[:160]}"}
        pos = None
        for p in poss:
            if p.get("symbol") == sym and (p.get("contracts") or 0):
                pos = p
                break
        if not pos:
            return {"ok": False, "error": "no open position to close"}
        size = abs(float(pos.get("contracts")))
        close_side = "sell" if pos.get("side") == "long" else "buy"
        # SDK prices the close off Lighter's own book; just route the reduce-only order
        res = self._sdk_submit(self._sdk_order(close_side, price_usd=None,
                                               reduce_only=True, close_btc=size, slippage=slippage))
        if isinstance(res, dict) and res.get("ok"):
            res["closed"] = size
        return res

    # ----- Lighter's OWN trade tape (so the bot's flow matches the Lighter screen) -----
    async def _sdk_recent_trades(self, limit=100):
        client = await self._client()
        m = await self._sdk_btc_market(client)
        if not m:
            return {"ok": False, "error": "no BTC market"}
        r = await client.order_api.recent_trades(market_id=m["market_index"], limit=int(limit))
        rows = []
        for t in (getattr(r, "trades", None) or []):
            try:
                # is_maker_ask=True ⇒ the resting order was an ASK ⇒ the TAKER lifted it ⇒ BUY.
                rows.append((int(t.trade_id), int(t.timestamp), float(t.usd_amount),
                             bool(t.is_maker_ask), float(t.price), float(t.size)))
            except Exception:
                pass
        return {"ok": True, "rows": rows}

    def poll_trades(self):
        """Blocking: pull Lighter's recent trades and append the NEW ones to the tape.
        Run OFF the event loop (it makes a network call). No-op if not configured."""
        if not self.configured():
            return
        res = self._sdk_submit(self._sdk_recent_trades(100), timeout=15)
        if not (isinstance(res, dict) and res.get("ok")):
            self._ltrade_err = (res or {}).get("error", "?") if isinstance(res, dict) else "?"
            return
        self._ltrade_err = ""
        mx = self._ltrade_max_id
        for tid, ts, usd, is_buy, price, size in sorted(res["rows"]):   # ascending trade_id
            if tid <= mx:
                continue
            self._ltrades.append((ts, usd, is_buy))
            if tid > self._ltrade_max_id:
                self._ltrade_max_id = tid

    def recent_trades_rows(self, limit=100):
        """Blocking: Lighter's recent trades as (tid, ts, usd, is_buy, price, size) rows, for
        feeding the cross-exchange 'all exchanges' tape. Returns None on error/not-configured.
        Run OFF the event loop (network call)."""
        if not self.configured():
            return None
        res = self._sdk_submit(self._sdk_recent_trades(int(limit)), timeout=15)
        if isinstance(res, dict) and res.get("ok"):
            return res.get("rows") or []
        return None

    def flow(self, window_sec=60):
        """Lighter's OWN taker buy/sell USD over the last window_sec — the SAME buy/sell
        the Lighter screen shows. Returns (buy_usd, sell_usd). In-memory, fast, no network."""
        cut = (time.time() - float(window_sec)) * 1000.0
        buy = sell = 0.0
        for ts, usd, is_buy in self._ltrades:
            if ts >= cut:
                if is_buy:
                    buy += usd
                else:
                    sell += usd
        return buy, sell


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    print()
    print("Lighter REAL-MONEY tool")
    if cmd == "check":
        cmd_check()
    elif cmd == "balance":
        cmd_balance()
    elif cmd == "positions":
        cmd_positions()
    elif cmd == "buy":
        _place("buy", arg if arg is not None else "0")
    elif cmd == "sell":
        _place("sell", arg if arg is not None else "0")
    elif cmd == "close":
        cmd_close()
    elif cmd == "menu":
        cmd_menu()
    else:
        print(f"unknown command '{cmd}'. use: check | balance | positions | buy <usd> | sell <usd> | close | menu")
    print()


if __name__ == "__main__":
    main()
