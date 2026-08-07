"""
================================================================================
 copy_trader.py  —  HYPERLIQUID COPY-TRADING (PAPER)
================================================================================
Mirrors a fixed set of real Hyperliquid wallets into SEPARATE paper accounts so
you can measure — risk-free — whether copying each trader is actually profitable
once you account for follow-lag.

It does NOT place real orders. It holds ONE live WebSocket to Hyperliquid and the
exchange PUSHES every change the instant it happens (no polling) — we mirror it
into a paper book immediately:

  • webData2 (per wallet) -> the wallet's positions AND its resting TP/SL orders,
                             pushed the moment anything changes (sub-second)
  • allMids               -> live mark prices streamed continuously (to value the
                             paper positions)

How the mirror works (proportional copy):
  Each paper account starts at $START_BALANCE. We size each paper position to the
  SAME portfolio weight the leader runs — weight = positionValue / accountValue,
  signed by direction, capped at MAX_WEIGHT so one wallet's leverage can't blow
  the paper book. When the leader opens / adds / trims / flips / closes, the paper
  book does the same at the current mark, realising PnL on every reduction.

What you get per wallet: realised PnL ($ and %), win/loss record + win rate, open
positions with unrealised PnL, the leader's live TP/SL, and a timestamped event
feed ("LONG BTC opened @ 63,309", "BTC closed +$420 (+1.2%)", ...).

Honest caveat baked into the design: the fast wallets (2-second scalpers, HFT)
churn many trades between our polls, so the paper book only captures what's on
the book at sample time — which is EXACTLY the limit of real copy-trading. If a
wallet is too fast to copy, this will show it by capturing very little of its PnL.
================================================================================
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import aiohttp

import config

WS_URL = "wss://api.hyperliquid.xyz/ws"
API_INFO = "https://api.hyperliquid.xyz/info"
STATE_PATH = os.path.join(config.DATA_DIR, "copy_trade_state.json")

START_BALANCE = 100.0        # paper starting capital per wallet
MAX_WEIGHT = 3.0             # cap |positionValue/accountValue| we will mirror (risk bound)
SAVE_SECONDS = 15            # how often we persist state to disk
MAX_EVENTS = 120             # per-wallet event-feed length kept
MAX_CLOSED = 500             # per-wallet closed-trade history kept

# The wallets we copy. Names are ours so you can tell them apart on the site.
# These 20 were pulled from Hyperliquid's LIVE leaderboard (38k traders) and VERIFIED right
# before adding (selected 2026-06-10): funded $100k–$5M, net-profitable, traded within the last
# 3 days, main market is a CRYPTO PERP, and at a COPYABLE pace (<= ~40 fills/day — HFT churners
# and market-makers were screened out). Ranked by monthly P&L. Re-run _pick_traders.py to refresh.
WALLETS = [
    {"name": "Falcon", "addr": "0xfce053a5e461683454bf37ad66d20344c0e3f4c0",
     "blurb": "BTC/ETH · funded $270k · +96%/mo · swing (multi-day holds)"},
    {"name": "Condor", "addr": "0x6bb971430554e3af58fbd469bce46ab2359a2d23",
     "blurb": "PURR · funded $663k · +51%/mo · swing (multi-day holds)"},
    {"name": "Panther", "addr": "0x54dbc1fbf6b1cd59807db61109b1d9eb91fd1a04",
     "blurb": "HYPE · funded $1.5M · +103%/mo · swing (multi-day holds)"},
    {"name": "Bison", "addr": "0x9c16bc8f1104e4d2f72267eb981fa12de7cc4a6f",
     "blurb": "SOL/BTC · funded $815k · +68%/mo · swing (multi-day holds)"},
    {"name": "Otter", "addr": "0xac26cf5f3c46b5e102048c65b977d2551b72a9c7",
     "blurb": "WLD/ZEC · funded $2.7M · +39%/mo · ~1/day (hours-long holds)"},
    {"name": "Lynx", "addr": "0x90882e7c28ddf0ac1177033a310aeed8eff25e90",
     "blurb": "HYPE/ETH · funded $440k · +78%/mo · ~7 trades/day"},
    {"name": "Heron", "addr": "0x512e1d1d3d17eea0a87ab9de07a2316466ea6a75",
     "blurb": "BTC/PUMP · funded $240k · +41%/mo · swing (multi-day holds)"},
    {"name": "Jaguar", "addr": "0xf62edeee17968d4c55d1c74936d2110333342f30",
     "blurb": "BTC · funded $485k · +74%/mo · ~2/day (hours-long holds)"},
    {"name": "Stag", "addr": "0xaa8253fb249498d0b6c31106539aaea0c76b9556",
     "blurb": "ZEC · funded $162k · +41%/mo · ~34 trades/day"},
    {"name": "Lion", "addr": "0xf10b1ba419f347fc129e4e79275fa022d72208de",
     "blurb": "HYPE · funded $343k · +111%/mo · ~24 trades/day"},
    {"name": "Gecko", "addr": "0x0423b4b2acb4f219ae4a270abd5dde2360e455bb",
     "blurb": "HYPE/SOL · funded $116k · +58%/mo · ~5/day (hours-long holds)"},
    {"name": "Crane", "addr": "0xbe4e91ae63090eeb4165dfcfc35db3a4eb76e75e",
     "blurb": "BTC/#2651 · funded $370k · +59%/mo · swing (multi-day holds)"},
    {"name": "Moose", "addr": "0x67f2d1e12bcd20f07e14f716bf5453e91cf42318",
     "blurb": "HYPE/#1030 · funded $117k · +25%/mo · ~1/day (hours-long holds)"},
    {"name": "Fox", "addr": "0x2057d4f2b6ea957b63371980775b0a5505a438f0",
     "blurb": "HYPE · funded $177k · +31%/mo · swing (multi-day holds)"},
    {"name": "Mamba", "addr": "0xe170ed9d77792397271d564c7161351d69fe9300",
     "blurb": "ETH/SOL · funded $1.0M · +31%/mo · swing (multi-day holds)"},
    {"name": "Marlin", "addr": "0x675462411d40a169c3397ac1dc00786dc9c7d3a1",
     "blurb": "ETH/ETC · funded $306k · +128%/mo · swing (multi-day holds)"},
    {"name": "Rhino", "addr": "0xbcfbbf38789d63721e800f1f8595af816e52c417",
     "blurb": "HYPE · funded $866k · +79%/mo · swing (multi-day holds)"},
    {"name": "Hornet", "addr": "0xebcda26432c1e2a46898d9920759438f86c8fcf4",
     "blurb": "BTC · funded $253k · +44%/mo · swing (multi-day holds)"},
    {"name": "Comet", "addr": "0x47f5a6397b79deaaca5c456503e7c1a49357b942",
     "blurb": "TAO/HYPE · funded $980k · +15%/mo · ~4/day (hours-long holds)"},
    {"name": "Kodiak", "addr": "0x1add90cfe379ad9c56c36522a3a4d33e89032671",
     "blurb": "HYPE · funded $715k · +129%/mo · ~19 trades/day"},
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fmt_px(p: float) -> str:
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.3f}"
    return f"{p:.6f}"


class CopyAccount:
    """One paper book that mirrors one Hyperliquid wallet."""

    def __init__(self, name: str, addr: str, blurb: str):
        self.name = name
        self.addr = addr
        self.blurb = blurb
        self.balance = START_BALANCE          # realised equity
        # coin -> {dir, qty(coin units), entry, opened_at, realized(accumulated)}
        self.positions: dict[str, dict] = {}
        self.closed: list[dict] = []          # finished round-trips
        self.events: list[dict] = []          # newest-first feed
        self.tpsl: dict[str, dict] = {}       # coin -> {tp, sl}  (the leader's triggers)
        self.leader_positions: list = []       # the wallet's OWN live positions (for display)
        self.leader_av = 0.0                   # the wallet's account value (USD)
        self.leader_pnl: dict = {}             # window -> {pnl, pct} : the wallet's REAL PnL
        # --- delta-tracking: only copy moves we actually WITNESS ---
        self.initialized = False               # have we taken the wallet's baseline yet?
        self.leader_sz: dict[str, float] = {}  # coin -> wallet's last signed size (szi)
        self.tracking: set = set()             # coins we opened a copy for (saw the open live)
        self.wins = 0
        self.losses = 0
        self.last_ok = 0                       # ts of last successful poll
        self.online = False

    # ---- event feed -------------------------------------------------------
    def _ev(self, kind: str, coin: str, note: str):
        self.events.insert(0, {"ts": _now_ms(), "kind": kind, "coin": coin, "note": note})
        del self.events[MAX_EVENTS:]

    # ---- PnL bookkeeping --------------------------------------------------
    def _realize(self, pos: dict, qty_closed: float, mark: float) -> float:
        if pos["dir"] == "long":
            pnl = qty_closed * (mark - pos["entry"])
        else:
            pnl = qty_closed * (pos["entry"] - mark)
        self.balance += pnl
        pos["realized"] += pnl
        return pnl

    def _close_full(self, coin: str, mark: float):
        pos = self.positions.pop(coin)
        pnl = self._realize(pos, pos["qty"], mark)
        notional = pos["entry"] * pos["qty"]
        pct = (pos["realized"] / notional * 100) if notional else 0.0
        rec = {"coin": coin, "dir": pos["dir"], "entry": pos["entry"], "exit": mark,
               "pnl": pos["realized"], "pct": pct,
               "opened_at": pos["opened_at"], "closed_at": _now_ms()}
        self.closed.insert(0, rec)
        del self.closed[MAX_CLOSED:]
        if pos["realized"] >= 0:
            self.wins += 1
        else:
            self.losses += 1
        sign = "+" if pos["realized"] >= 0 else ""
        self._ev("close", coin,
                 f"{coin} {pos['dir']} closed @ {_fmt_px(mark)} → {sign}${pos['realized']:,.2f} ({sign}{pct:.1f}%)")

    # ---- mirror helpers ---------------------------------------------------
    def _open(self, coin: str, want_dir: str, weight: float, mid: float):
        qty = (abs(weight) * self.balance) / mid
        if qty <= 0:
            return
        self.positions[coin] = {"dir": want_dir, "qty": qty, "entry": mid,
                                "opened_at": _now_ms(), "realized": 0.0}
        self._ev("open", coin, f"{want_dir.upper()} {coin} opened @ {_fmt_px(mid)} (copied)")

    def _resize(self, coin: str, weight: float, mid: float):
        cur = self.positions.get(coin)
        if cur is None:
            self._open(coin, "long" if weight > 0 else "short", weight, mid)
            return
        want_qty = (abs(weight) * self.balance) / mid
        if want_qty > cur["qty"] * 1.02:              # ADD -> weighted-avg entry
            add = want_qty - cur["qty"]
            cur["entry"] = (cur["entry"] * cur["qty"] + mid * add) / want_qty
            cur["qty"] = want_qty
            self._ev("add", coin, f"added to {cur['dir']} {coin} @ {_fmt_px(mid)}")
        elif want_qty < cur["qty"] * 0.98:            # TRIM -> realise the closed part
            closed_qty = cur["qty"] - want_qty
            pnl = self._realize(cur, closed_qty, mid)
            cur["qty"] = want_qty
            sign = "+" if pnl >= 0 else ""
            self._ev("trim", coin, f"trimmed {coin} @ {_fmt_px(mid)} → {sign}${pnl:,.2f}")

    # ---- the core mirror step: copy WITNESSED moves only -----------------
    def sync(self, leader: dict, mids: dict):
        """leader: coin -> {"szi": signed size, "weight": signed capped weight}.
        We copy only the transitions we actually SEE: a flat->open, a flip, a
        resize of something we're already copying, or a close. The wallet's
        positions that already existed before we started watching are recorded as
        a baseline and NOT copied — the bot trades only when the wallet trades."""
        EPS = 1e-9

        if not self.initialized:
            # First sight: take a baseline of what the wallet already holds and
            # copy NONE of it. Also close any stale copy whose coin the wallet no
            # longer holds (e.g. it closed while we were offline).
            self.leader_sz = {c: info["szi"] for c, info in leader.items()}
            for coin in list(self.tracking):
                if coin not in leader:
                    # leader exited this coin while we were offline -> close our orphan copy
                    if coin in self.positions:
                        mid = mids.get(coin)
                        if mid:
                            self._close_full(coin, mid)
                    self.tracking.discard(coin)
                # else: leader STILL holds it -> KEEP tracking, so we close it when they do
                #       (the old code dropped tracking here, orphaning copies across restarts)
            self.initialized = True
            return

        # 1) coins the wallet currently holds -> detect open / flip / resize
        for coin, info in leader.items():
            new_szi = info["szi"]
            weight = info["weight"]
            mid = mids.get(coin)
            if mid is None or mid <= 0:
                self.leader_sz[coin] = new_szi
                continue
            prev = self.leader_sz.get(coin, 0.0)
            new_dir = "long" if new_szi > 0 else "short"

            if abs(prev) < EPS:                       # wallet just OPENED from flat
                self._open(coin, new_dir, weight, mid)
                self.tracking.add(coin)
            elif (prev > 0) != (new_szi > 0):         # wallet FLIPPED side
                if coin in self.tracking:
                    self._close_full(coin, mid)
                self._ev("flip", coin, f"wallet flipped → {new_dir.upper()} {coin}")
                self._open(coin, new_dir, weight, mid)
                self.tracking.add(coin)
            elif coin in self.tracking:               # same side, size changed
                self._resize(coin, weight, mid)
            # else: pre-existing position we never copied -> ignore its resizes
            self.leader_sz[coin] = new_szi

        # 2) coins that VANISHED from the wallet -> the wallet CLOSED them
        for coin in list(self.leader_sz.keys()):
            if coin not in leader:
                self.leader_sz.pop(coin, None)
                if coin in self.tracking:
                    mid = mids.get(coin)
                    if mid and coin in self.positions:
                        self._close_full(coin, mid)
                    self.tracking.discard(coin)

    # ---- read-only snapshot for the UI -----------------------------------
    def open_upnl(self, mids: dict) -> float:
        tot = 0.0
        for coin, pos in self.positions.items():
            mid = mids.get(coin)
            if not mid:
                continue
            if pos["dir"] == "long":
                tot += pos["qty"] * (mid - pos["entry"])
            else:
                tot += pos["qty"] * (pos["entry"] - mid)
        return tot

    def snapshot(self, mids: dict) -> dict:
        upnl = self.open_upnl(mids)
        equity = self.balance + upnl
        realized = self.balance - START_BALANCE
        n = self.wins + self.losses
        positions = []
        for coin, pos in self.positions.items():
            mid = mids.get(coin) or pos["entry"]
            if pos["dir"] == "long":
                u = pos["qty"] * (mid - pos["entry"])
            else:
                u = pos["qty"] * (pos["entry"] - mid)
            positions.append({"coin": coin, "dir": pos["dir"], "entry": pos["entry"],
                              "mark": mid, "upnl": u, "tp": self.tpsl.get(coin, {}).get("tp"),
                              "sl": self.tpsl.get(coin, {}).get("sl")})
        return {
            "name": self.name, "addr": self.addr, "blurb": self.blurb,
            "balance": self.balance, "equity": equity,
            "realized": realized, "realized_pct": realized / START_BALANCE * 100,
            "upnl": upnl, "wins": self.wins, "losses": self.losses,
            "trades": n, "win_rate": (self.wins / n * 100) if n else None,
            "online": self.online, "last_ok": self.last_ok,
            "positions": positions, "events": self.events[:60],
            "closed": self.closed[:50],
            "leader_positions": self.leader_positions, "leader_av": self.leader_av,
            "leader_pnl": self.leader_pnl, "leader_empty": self.leader_av <= 0,
        }

    # ---- persistence ------------------------------------------------------
    def to_dict(self) -> dict:
        return {"balance": self.balance, "positions": self.positions,
                "closed": self.closed, "events": self.events,
                "wins": self.wins, "losses": self.losses,
                "tracking": list(self.tracking)}

    def load(self, d: dict):
        self.balance = d.get("balance", START_BALANCE)
        self.positions = d.get("positions", {})
        self.closed = d.get("closed", [])
        self.events = d.get("events", [])
        self.wins = d.get("wins", 0)
        self.losses = d.get("losses", 0)
        # Keep copies we already hold under management; re-baseline the wallet on
        # the next live message (initialized stays False) so we don't re-open its
        # pre-existing positions.
        self.tracking = set(d.get("tracking", [])) | set(self.positions.keys())

    def reset(self):
        self.balance = START_BALANCE
        self.positions, self.closed, self.events, self.tpsl = {}, [], [], {}
        self.leader_sz, self.tracking = {}, set()
        self.initialized = False
        self.wins = self.losses = 0


class CopyTrader:
    """Holds ONE live WebSocket to Hyperliquid; the exchange pushes every wallet's
    position/TP-SL change instantly and we mirror it. No polling."""

    def __init__(self):
        self.accounts: list[CopyAccount] = [
            CopyAccount(w["name"], w["addr"], w["blurb"]) for w in WALLETS
        ]
        self.by_addr = {a.addr.lower(): a for a in self.accounts}
        self.mids: dict = {}
        self.running = False
        self.ws_connected = False
        self._load()

    def _by_addr(self, addr: str) -> CopyAccount | None:
        return self.by_addr.get((addr or "").lower())

    @staticmethod
    def _leader_state(ch: dict) -> dict:
        """coin -> {"szi": signed size, "weight": signed capped weight}.
        szi drives open/close/flip detection; weight drives our copy sizing."""
        try:
            av = float(ch["crossMarginSummary"]["accountValue"])
        except Exception:
            try:
                av = float(ch["marginSummary"]["accountValue"])
            except Exception:
                return {}
        if av <= 0:
            return {}
        out = {}
        for ap in ch.get("assetPositions", []):
            p = ap.get("position", {})
            coin = p.get("coin")
            szi = float(p.get("szi", 0) or 0)
            pv = float(p.get("positionValue", 0) or 0)
            if not coin or szi == 0 or pv == 0:
                continue
            w = (pv / av) * (1 if szi > 0 else -1)
            w = max(-MAX_WEIGHT, min(MAX_WEIGHT, w))
            out[coin] = {"szi": szi, "weight": w}
        return out

    def _leader_view(self, ch: dict, tpsl: dict) -> tuple:
        """Build the wallet's OWN live positions for display (not our mirror)."""
        try:
            av = float(ch["crossMarginSummary"]["accountValue"])
        except Exception:
            av = 0.0
        out = []
        for ap in ch.get("assetPositions", []):
            p = ap.get("position", {})
            coin = p.get("coin")
            szi = float(p.get("szi", 0) or 0)
            if not coin or szi == 0:
                continue
            slot = tpsl.get(coin, {})
            out.append({
                "coin": coin,
                "dir": "long" if szi > 0 else "short",
                "entry": float(p.get("entryPx", 0) or 0),
                "mark": self.mids.get(coin),
                "notional": float(p.get("positionValue", 0) or 0),
                "upnl": float(p.get("unrealizedPnl", 0) or 0),
                "lev": (p.get("leverage") or {}).get("value"),
                "tp": slot.get("tp"), "sl": slot.get("sl"),
            })
        return out, av

    @staticmethod
    def _tpsl_from_orders(open_orders: list) -> dict:
        tpsl: dict[str, dict] = {}
        for o in open_orders or []:
            if not o.get("isTrigger"):
                continue
            coin = o.get("coin")
            ot = (o.get("orderType") or "").lower()
            try:
                px = float(o.get("triggerPx"))
            except Exception:
                continue
            slot = tpsl.setdefault(coin, {})
            if "take profit" in ot:
                slot["tp"] = px
            elif "stop" in ot:
                slot["sl"] = px
        return tpsl

    def _on_webdata2(self, data: dict):
        """A wallet's full state was pushed — mirror it instantly."""
        acc = self._by_addr(data.get("user", ""))
        if acc is None:
            return
        ch = data.get("clearinghouseState", {}) or {}
        tpsl = self._tpsl_from_orders(data.get("openOrders"))
        leader = self._leader_state(ch)
        if self.mids:                       # need marks to value/realise
            acc.sync(leader, self.mids)
        acc.tpsl = tpsl
        acc.leader_positions, acc.leader_av = self._leader_view(ch, tpsl)
        acc.online = True
        acc.last_ok = _now_ms()

    async def _consume(self, session):
        """One WS connection: subscribe to mids + every wallet, handle pushes."""
        async with session.ws_connect(WS_URL, heartbeat=20,
                                       timeout=aiohttp.ClientTimeout(total=30)) as ws:
            self.ws_connected = True
            await ws.send_json({"method": "subscribe", "subscription": {"type": "allMids"}})
            for a in self.accounts:
                await ws.send_json({"method": "subscribe",
                                    "subscription": {"type": "webData2", "user": a.addr}})
            print(f"[copytrade] WS live — instant push for {len(self.accounts)} wallets: "
                  + ", ".join(a.name for a in self.accounts))
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    d = json.loads(msg.data)
                except Exception:
                    continue
                ch = d.get("channel")
                if ch == "allMids":
                    mids = d.get("data", {}).get("mids", {})
                    if mids:
                        self.mids = {k: float(v) for k, v in mids.items()}
                elif ch == "webData2":
                    self._on_webdata2(d.get("data", {}))

    async def run(self):
        self.running = True
        print(f"[copytrade] connecting WebSocket to mirror {len(self.accounts)} Hyperliquid wallets…")
        # ThreadedResolver: robust DNS regardless of the aiodns availability.
        conn = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=conn) as session:
            asyncio.create_task(self._save_loop())
            asyncio.create_task(self._portfolio_loop(session))   # wallet's real PnL
            backoff = 1
            while self.running:
                try:
                    await self._consume(session)
                except Exception as e:
                    print(f"[copytrade] WS error: {type(e).__name__}: {e}")
                self.ws_connected = False
                for a in self.accounts:
                    a.online = False
                if not self.running:
                    break
                await asyncio.sleep(backoff)             # reconnect with backoff
                backoff = min(backoff * 2, 15)
                # a clean message resets backoff implicitly on next successful loop
                if self.ws_connected:
                    backoff = 1

    async def _save_loop(self):
        while self.running:
            await asyncio.sleep(SAVE_SECONDS)
            self._save()

    async def _fetch_portfolio(self, session, acc: CopyAccount):
        """Pull the wallet's REAL realized PnL per window (day/week/month) so the UI
        can show it next to our paper copy's PnL for a fair comparison."""
        try:
            async with session.post(API_INFO, json={"type": "portfolio", "user": acc.addr},
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                pf = await r.json()
        except Exception:
            return
        out = {}
        for entry in pf or []:
            try:
                window, data = entry[0], entry[1]
            except Exception:
                continue
            if window not in ("day", "week", "month"):
                continue
            pnlh = data.get("pnlHistory", [])
            avh = data.get("accountValueHistory", [])
            if not pnlh:
                continue
            try:
                pnl = float(pnlh[-1][1])
                av_now = float(avh[-1][1]) if avh else 0.0
            except Exception:
                continue
            start = av_now - pnl
            pct = (pnl / start * 100.0) if start > 0 else None
            out[window] = {"pnl": pnl, "pct": pct}
        if out:
            acc.leader_pnl = out

    async def _portfolio_loop(self, session):
        while self.running:
            for a in self.accounts:
                await self._fetch_portfolio(session, a)
                await asyncio.sleep(0.3)
            await asyncio.sleep(60)

    # ---- API for the dashboard -------------------------------------------
    def snapshot(self) -> dict:
        return {"accounts": [a.snapshot(self.mids) for a in self.accounts],
                "start_balance": START_BALANCE, "ws_connected": self.ws_connected}

    def reset(self, addr: str | None = None):
        if addr:
            acc = self._by_addr(addr)
            if acc:
                acc.reset()
        else:
            for a in self.accounts:
                a.reset()
        self._save()

    # ---- persistence ------------------------------------------------------
    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            data = {a.addr: a.to_dict() for a in self.accounts}
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def _load(self):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            for a in self.accounts:
                if a.addr in data:
                    a.load(data[a.addr])
        except Exception:
            pass
