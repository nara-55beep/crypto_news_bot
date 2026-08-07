"""
onchain_radar_paper.py - paper-only on-chain early signal bot.

This bot does not need a hand-picked wallet list. It scans chain-wide event logs
for high-signal contracts:

  - big USDC/USDT/WBTC/BTCB/WETH transfers from all sender/receiver wallets
  - known exchange wallet inflows/outflows for directional BTC pressure
  - new pool creation events from major AMM factories

It then paper-trades BTCUSDT only. It is an experiment for measuring whether
on-chain flow gives useful early signals; it is not wired to real money.
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass

import aiohttp

import config


STATE_PATH = os.path.join(config.DATA_DIR, "onchain_radar_state.json")
CSV_PATH = os.path.join(config.DATA_DIR, "onchain_radar_trades.csv")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
ZERO_ADDR = "0x" + "0" * 40


DEFAULT_RPC_URLS = {
    "ethereum": "https://ethereum.publicnode.com",
    "base": "https://base-rpc.publicnode.com",
    # NOTE: Binance's own bsc-dataseed.* nodes return JSON-RPC -32005 "limit
    # exceeded" on eth_getLogs (they heavily cap log queries, and BSC USDT/USDC are
    # ultra-high-volume so the result set is huge). publicnode allows log scans.
    "bsc": "https://bsc-rpc.publicnode.com",
}

WATCH_TOKENS = {
    "ethereum": [
        {"symbol": "USDT", "address": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6, "kind": "stable"},
        {"symbol": "USDC", "address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6, "kind": "stable"},
        {"symbol": "WBTC", "address": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", "decimals": 8, "kind": "btc"},
        {"symbol": "WETH", "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "decimals": 18, "kind": "eth"},
    ],
    "base": [
        {"symbol": "USDC", "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bdA02913", "decimals": 6, "kind": "stable"},
        {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18, "kind": "eth"},
    ],
    "bsc": [
        {"symbol": "USDT", "address": "0x55d398326f99059ff775485246999027b3197955", "decimals": 18, "kind": "stable"},
        {"symbol": "USDC", "address": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "decimals": 18, "kind": "stable"},
        {"symbol": "BTCB", "address": "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c", "decimals": 18, "kind": "btc"},
    ],
}

FACTORY_WATCH = {
    "ethereum": [
        {"name": "Uniswap V2", "address": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f", "topic": PAIR_CREATED_TOPIC, "kind": "v2"},
        {"name": "Uniswap V3", "address": "0x1F98431c8aD98523631AE4a59f267346ea31F984", "topic": POOL_CREATED_TOPIC, "kind": "v3"},
    ],
    "bsc": [
        {"name": "Pancake V2", "address": "0xca143ce32fe78f1f7019d7d551a6402fc5350c73", "topic": PAIR_CREATED_TOPIC, "kind": "v2"},
    ],
}

DEFAULT_EXCHANGE_WALLETS = {
    "ethereum": {
        "0x28c6c06298d514db089934071355e5743bf21d60": "binance_hot",
        "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "binance_hot",
        "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "binance_hot",
        "0x503828976d22510aad0201ac7ec88293211d23da": "coinbase_hot",
        "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "coinbase_hot",
    },
    "bsc": {
        "0xf977814e90da44bfa03b6295a0616a897441acec": "binance_hot",
    },
}

BLOCK_STEP = {
    "ethereum": 48,
    "base": 180,
    "bsc": 60,        # BSC blocks are fast AND USDT/USDC logs are huge — keep the window small
}


def cfg(name, default):
    return getattr(config, name, default)


def norm_addr(addr):
    return (addr or "").lower()


def topic_addr(topic):
    if not topic or len(topic) < 42:
        return ""
    return "0x" + topic[-40:].lower()


def data_words(data):
    s = (data or "0x")[2:]
    return [s[i:i + 64] for i in range(0, len(s), 64) if len(s[i:i + 64]) == 64]


def word_addr(word):
    if not word or len(word) < 40:
        return ""
    return "0x" + word[-40:].lower()


def hex_int(v, default=0):
    try:
        return int(v or "0x0", 16)
    except Exception:
        return default


@dataclass
class OnChainPos:
    id: str
    side: str
    entry: float
    qty: float
    margin: float
    leverage: float
    opened_at: float
    note: str
    peak: float = 0.0
    best_margin_pct: float = 0.0

    def unrealized(self, mark):
        if not mark:
            return 0.0
        if self.side == "short":
            return self.qty * (self.entry - mark)
        return self.qty * (mark - self.entry)

    def liq_price(self):
        if self.side == "long":
            return self.entry * (1.0 - 1.0 / self.leverage)
        return self.entry * (1.0 + 1.0 / self.leverage)


class OnChainRadarPaperBot:
    def __init__(self, market):
        self.market = market
        self.enabled = bool(cfg("ONCHAIN_ENABLED", True))
        self.balance = float(cfg("ONCHAIN_START_BALANCE", 100.0))
        self.start_balance = float(cfg("ONCHAIN_START_BALANCE", 100.0))
        self.pos: OnChainPos | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.signals: list[dict] = []
        self.pending: list[dict] = []
        self.wallet_score: dict[str, float] = {}
        self.cursors: dict[str, int] = {}
        self.chain_status: dict[str, dict] = {}
        self._session: aiohttp.ClientSession | None = None
        self._ctx = {"score": 0.0, "bias": "flat", "components": []}
        self._last_trade_ts = 0.0
        self._last_save_ts = 0.0
        self._last_skip_note_ts = 0.0
        self._price_ticks: list[tuple[float, float]] = []
        self._err = ""

        self.rpc_urls = dict(DEFAULT_RPC_URLS)
        self.rpc_urls.update(getattr(config, "ONCHAIN_RPC_URLS", {}) or {})
        self.exchange_wallets = self._load_exchange_wallets()
        self._major_tokens = {
            (chain, norm_addr(tok["address"])): tok
            for chain, toks in WATCH_TOKENS.items()
            for tok in toks
        }
        self._load()

    def _load_exchange_wallets(self):
        merged = {chain: dict(rows) for chain, rows in DEFAULT_EXCHANGE_WALLETS.items()}
        extra = getattr(config, "ONCHAIN_EXCHANGE_WALLETS", {}) or {}
        for chain, rows in extra.items():
            merged.setdefault(chain, {})
            for addr, label in (rows or {}).items():
                merged[chain][norm_addr(addr)] = str(label or "exchange")
        return merged

    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "enabled": self.enabled,
                    "balance": self.balance,
                    "start_balance": self.start_balance,
                    "position": asdict(self.pos) if self.pos else None,
                    "history": self.history,
                    "log": self.log[:80],
                    "signals": self.signals[:250],
                    "pending": self.pending[:500],
                    "wallet_score": self.wallet_score,
                    "cursors": self.cursors,
                    "chain_status": self.chain_status,
                }, f)
            self._last_save_ts = time.time()
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(STATE_PATH):
                return
            with open(STATE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", self.enabled))
            self.balance = float(d.get("balance", self.balance))
            self.start_balance = float(d.get("start_balance", self.start_balance))
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self.signals = d.get("signals", []) or []
            self.pending = d.get("pending", []) or []
            self.cursors = {str(k): int(v) for k, v in (d.get("cursors", {}) or {}).items()}
            self.chain_status = d.get("chain_status", {}) or {}
            self.wallet_score = {
                norm_addr(k): float(v)
                for k, v in (d.get("wallet_score", {}) or {}).items()
                if norm_addr(k)
            }
            pd = d.get("position")
            if pd:
                self.pos = OnChainPos(**pd)
            if not self.history and self.pos is None:
                self.balance = float(cfg("ONCHAIN_START_BALANCE", 100.0))
                self.start_balance = float(cfg("ONCHAIN_START_BALANCE", 100.0))
        except Exception:
            pass

    def _btc(self):
        try:
            return self.market.price("BTCUSDT")
        except Exception:
            return None

    def _eth(self):
        try:
            return self.market.price("ETHUSDT")
        except Exception:
            return None

    def _note(self, msg, kind="info"):
        self.log.insert(0, {"t": time.time(), "msg": msg, "kind": kind})
        self.log = self.log[:80]
        print(f"[onchain] {msg}")

    def _track_price(self, px):
        if not px:
            return
        now = time.time()
        self._price_ticks.append((now, float(px)))
        cutoff = now - 1800
        self._price_ticks = [(t, p) for t, p in self._price_ticks if t >= cutoff]

    def _price_change(self, seconds):
        if len(self._price_ticks) < 2:
            return 0.0
        now = self._price_ticks[-1][0]
        old = None
        for t, p in reversed(self._price_ticks):
            if t <= now - seconds:
                old = p
                break
        if old is None:
            old = self._price_ticks[0][1]
        cur = self._price_ticks[-1][1]
        return (cur / old - 1.0) if old else 0.0

    async def _rpc(self, chain, method, params):
        url = self.rpc_urls.get(chain)
        if not url:
            raise RuntimeError(f"no RPC URL for {chain}")
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=float(cfg("ONCHAIN_RPC_TIMEOUT", 8.0)))
            self._session = aiohttp.ClientSession(timeout=timeout)
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) % 1000000,
            "method": method,
            "params": params,
        }
        async with self._session.post(url, json=payload) as r:
            if r.status != 200:
                raise RuntimeError(f"{chain} RPC HTTP {r.status}")
            data = await r.json(content_type=None)
        if data.get("error"):
            raise RuntimeError(str(data["error"])[:180])
        return data.get("result")

    async def _latest_block(self, chain):
        return hex_int(await self._rpc(chain, "eth_blockNumber", []), 0)

    @staticmethod
    def _is_limit_error(e):
        m = str(e).lower()
        return ("-32005" in m or "limit exceeded" in m or "limit" in m
                or "range" in m or "too many" in m or "exceed" in m
                or "response size" in m or "query timeout" in m)

    async def _scan_logs(self, chain, key, latest, address, topic, step):
        cursor = int(self.cursors.get(key, 0) or 0)
        if cursor <= 0:
            self.cursors[key] = latest
            return []
        if latest <= cursor:
            return []
        start = cursor + 1
        end = min(latest, cursor + step)
        # If the node says "limit exceeded" (too big a range / too many logs),
        # halve the block window and retry — so a busy chain self-heals instead of
        # erroring forever. Cursor only advances over the window we actually read.
        for _ in range(5):
            params = [{
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "address": address,
                "topics": [topic],
            }]
            try:
                logs = await self._rpc(chain, "eth_getLogs", params)
                self.cursors[key] = end
                return logs or []
            except RuntimeError as e:
                if self._is_limit_error(e) and end > start:
                    end = start + (end - start) // 2     # shrink the window and retry
                    await asyncio.sleep(0.25)
                    continue
                raise
        # even a 1-block window failed -> step past 1 block so we never get stuck
        self.cursors[key] = start
        return []

    def _usd_value(self, token, amount):
        kind = token.get("kind")
        if kind == "stable":
            return amount
        if kind == "btc":
            return amount * (self._btc() or 0.0)
        if kind == "eth":
            return amount * (self._eth() or 0.0)
        return 0.0

    def _size_boost(self, usd):
        base = float(cfg("ONCHAIN_MIN_TRANSFER_USD", 1_000_000.0))
        if usd <= base:
            return 1.0
        return min(3.0, 1.0 + math.log10(max(usd, 1.0) / base) * 0.9)

    def _wallet_mult(self, wallets):
        vals = [self.wallet_score.get(norm_addr(w), 0.0) for w in wallets if w]
        if not vals:
            return 1.0
        best = max(vals)
        worst = min(vals)
        mult = 1.0 + max(0.0, best) * 0.18 + min(0.0, worst) * 0.10
        return max(0.45, min(2.2, mult))

    def _record_signal(self, side, score, label, usd=0.0, chain="", tx="", wallets=None, kind="flow"):
        score = max(-8.0, min(8.0, float(score)))
        if abs(score) < 0.05:
            return
        now = time.time()
        px = self._btc()
        rec = {
            "t": now,
            "side": side,
            "score": round(score, 2),
            "label": label[:180],
            "usd": round(float(usd), 2),
            "chain": chain,
            "tx": tx,
            "kind": kind,
        }
        self.signals.insert(0, rec)
        self.signals = self.signals[:250]
        if side in ("long", "short") and px and abs(score) >= 0.35:
            clean_wallets = [norm_addr(w) for w in (wallets or []) if norm_addr(w) and norm_addr(w) != ZERO_ADDR]
            self.pending.append({
                "t": now,
                "side": side,
                "price": float(px),
                "score": score,
                "wallets": clean_wallets[:4],
            })
            self.pending = self.pending[-500:]
        if abs(score) >= float(cfg("ONCHAIN_LOG_SCORE", 1.5)):
            self._note(f"{label} -> {side.upper()} score {score:+.2f}", "info")

    def _handle_transfer(self, chain, token, log):
        topics = log.get("topics") or []
        if len(topics) < 3:
            return
        frm = topic_addr(topics[1])
        to = topic_addr(topics[2])
        if not frm or not to or frm == to:
            return
        raw = hex_int(log.get("data"), 0)
        amount = raw / (10 ** int(token["decimals"]))
        usd = self._usd_value(token, amount)
        min_usd = float(cfg("ONCHAIN_MIN_TRANSFER_USD", 1_000_000.0))
        if usd < min_usd:
            return

        symbol = token["symbol"]
        kind = token["kind"]
        exch = self.exchange_wallets.get(chain, {})
        from_ex = exch.get(frm)
        to_ex = exch.get(to)
        if from_ex and to_ex:
            return

        score = 0.0
        side = "long"
        label = ""
        mult = self._wallet_mult([frm, to])
        boost = self._size_boost(usd)
        usd_txt = f"${usd:,.0f}"

        if to_ex:
            if kind == "stable":
                score = (2.2 + boost) * mult
                side = "long"
                label = f"{chain} {symbol} {usd_txt} inflow to {to_ex} (buying ammo)"
            elif kind in ("btc", "eth"):
                score = -(2.0 + boost) * mult
                side = "short"
                label = f"{chain} {symbol} {usd_txt} inflow to {to_ex} (coin may be sold)"
        elif from_ex:
            if kind == "stable":
                score = -(0.8 + boost * 0.35) * mult
                side = "short"
                label = f"{chain} {symbol} {usd_txt} outflow from {from_ex} (dry powder leaving)"
            elif kind in ("btc", "eth"):
                score = (1.7 + boost * 0.7) * mult
                side = "long"
                label = f"{chain} {symbol} {usd_txt} outflow from {from_ex} (accumulation/custody)"
        else:
            huge = float(cfg("ONCHAIN_HUGE_TRANSFER_USD", 5_000_000.0))
            if usd < huge:
                return
            if kind == "stable":
                score = 0.45 * boost * mult
                side = "long"
                label = f"{chain} large {symbol} transfer {usd_txt} (unknown wallets)"
            elif kind in ("btc", "eth"):
                score = -0.35 * boost * mult
                side = "short"
                label = f"{chain} large {symbol} transfer {usd_txt} (unknown wallets)"
        if label:
            tx = log.get("transactionHash", "")
            self._record_signal(side, score, label, usd, chain, tx, [frm, to], "transfer")

    def _handle_pool(self, chain, factory, log):
        topics = log.get("topics") or []
        if len(topics) < 3:
            return
        token0 = topic_addr(topics[1])
        token1 = topic_addr(topics[2])
        major = []
        for addr in (token0, token1):
            tok = self._major_tokens.get((chain, addr))
            if tok:
                major.append(tok["symbol"])
        if not major:
            return
        words = data_words(log.get("data"))
        pool = ""
        if factory.get("kind") == "v2" and words:
            pool = word_addr(words[0])
        elif factory.get("kind") == "v3" and len(words) >= 2:
            pool = word_addr(words[1])
        score = 0.25 if len(major) == 1 else 0.1
        label = f"{chain} new {factory['name']} pool touching {','.join(major)}"
        if pool:
            label += f" ({pool[:8]}...{pool[-6:]})"
        self._record_signal("long", score, label, 0.0, chain, log.get("transactionHash", ""), [token0, token1], "new_pool")

    async def _scan_chain(self, chain):
        latest = await self._latest_block(chain)
        step = int(cfg("ONCHAIN_BLOCK_STEP", BLOCK_STEP.get(chain, 80)))
        seen = 0
        for token in WATCH_TOKENS.get(chain, []):
            addr = norm_addr(token["address"])
            key = f"{chain}:transfer:{addr}"
            logs = await self._scan_logs(chain, key, latest, addr, TRANSFER_TOPIC, step)
            seen += len(logs)
            for log in logs:
                self._handle_transfer(chain, token, log)
        for factory in FACTORY_WATCH.get(chain, []):
            addr = norm_addr(factory["address"])
            key = f"{chain}:factory:{addr}:{factory['topic']}"
            logs = await self._scan_logs(chain, key, latest, addr, factory["topic"], step)
            seen += len(logs)
            for log in logs:
                self._handle_pool(chain, factory, log)
        cursors = [v for k, v in self.cursors.items() if k.startswith(chain + ":")]
        self.chain_status[chain] = {
            "latest": latest,
            "cursor": min(cursors) if cursors else latest,
            "logs": seen,
            "error": "",
            "ts": time.time(),
        }

    async def _scan_all(self):
        chains = [c for c, url in self.rpc_urls.items() if url and (WATCH_TOKENS.get(c) or FACTORY_WATCH.get(c))]
        for chain in chains:
            try:
                await self._scan_chain(chain)
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:120]}"
                self.chain_status[chain] = {
                    **self.chain_status.get(chain, {}),
                    "error": msg,
                    "ts": time.time(),
                }
                self._err = f"{chain}: {msg}"

    def _learn_wallets(self, now, px):
        if not px:
            return
        horizon = float(cfg("ONCHAIN_LEARN_HORIZON_SEC", 1800.0))
        min_move = float(cfg("ONCHAIN_LEARN_MIN_MOVE", 0.0008))
        keep = []
        changed = False
        for item in self.pending:
            if now - float(item.get("t", 0.0)) < horizon:
                keep.append(item)
                continue
            entry = float(item.get("price") or 0.0)
            if entry <= 0:
                continue
            move = px / entry - 1.0
            expected = move if item.get("side") == "long" else -move
            weight = min(1.5, max(0.35, abs(float(item.get("score", 1.0))) / 3.0))
            if expected >= min_move:
                delta = 0.8 * weight
            elif expected <= -min_move:
                delta = -0.65 * weight
            else:
                delta = -0.08 * weight
            for wallet in item.get("wallets", []) or []:
                wallet = norm_addr(wallet)
                if not wallet or wallet == ZERO_ADDR:
                    continue
                self.wallet_score[wallet] = max(-5.0, min(5.0, self.wallet_score.get(wallet, 0.0) + delta))
                changed = True
        self.pending = keep[-500:]
        if changed and len(self.wallet_score) > 800:
            ranked = sorted(self.wallet_score.items(), key=lambda kv: abs(kv[1]), reverse=True)[:800]
            self.wallet_score = dict(ranked)

    def _active_score(self):
        now = time.time()
        window = float(cfg("ONCHAIN_SIGNAL_WINDOW_SEC", 600.0))
        total = 0.0
        comps = []
        for sig in self.signals:
            age = now - float(sig.get("t", 0.0))
            if age < 0 or age > window:
                continue
            decay = 0.25 + 0.75 * (1.0 - age / window)
            val = float(sig.get("score", 0.0)) * decay
            total += val
            comps.append((abs(val), val, sig.get("label", "")))
        bias = "long" if total >= 0.75 else ("short" if total <= -0.75 else "flat")
        comps.sort(reverse=True)
        self._ctx = {
            "score": round(total, 2),
            "bias": bias,
            "components": [{"score": round(v, 2), "label": label[:120]} for _, v, label in comps[:5]],
        }
        return total

    def equity(self):
        eq = self.balance
        if self.pos is not None:
            eq += self.pos.margin + self.pos.unrealized(self._btc() or self.pos.entry)
        return eq

    def _append_csv(self, rec):
        try:
            new = not os.path.exists(CSV_PATH)
            with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["closed_at", "side", "entry", "exit", "qty", "margin", "leverage", "pnl", "pnl_pct", "reason"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["closed_at"])),
                            rec["side"], rec["entry"], rec["exit"], rec["qty"], rec["margin"],
                            rec["leverage"], rec["pnl"], rec["pnl_pct"], rec["reason"]])
        except Exception:
            pass

    def _open(self, side, px, note):
        margin = min(round(self.balance * float(cfg("ONCHAIN_RISK_FRAC", 1.0)), 2), round(self.balance, 2))
        lev = float(cfg("ONCHAIN_LEVERAGE", 20.0))
        if margin < 1.0 or not px:
            return
        qty = margin * lev / px
        self.balance -= margin
        self.pos = OnChainPos(
            id=uuid.uuid4().hex[:6],
            side=side,
            entry=float(px),
            qty=qty,
            margin=margin,
            leverage=lev,
            opened_at=time.time(),
            peak=float(px),
            note=note,
        )
        self._last_trade_ts = time.time()
        self._note(f"OPEN {side.upper()} @ {px:,.1f} - {note} - margin ${margin:.2f} {lev:g}x", "open")
        self._save()

    def _close(self, px, reason):
        p = self.pos
        if p is None or not px:
            return
        pnl = p.unrealized(px)
        self.balance += p.margin + pnl
        rec = {
            "id": p.id,
            "side": p.side,
            "entry": round(p.entry, 2),
            "exit": round(px, 2),
            "qty": round(p.qty, 6),
            "margin": round(p.margin, 2),
            "leverage": p.leverage,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / p.margin * 100.0, 2) if p.margin else 0.0,
            "reason": reason,
            "opened_at": p.opened_at,
            "closed_at": time.time(),
        }
        self.history.insert(0, rec)
        self.history = self.history
        self.pos = None
        self._last_trade_ts = time.time()
        self._note(f"CLOSE {p.side.upper()} @ {px:,.1f} - {reason} - P&L ${pnl:+.2f} ({rec['pnl_pct']:+.2f}%)",
                   "win" if pnl >= 0 else "loss")
        self._append_csv(rec)
        self._save()

    def _manage_position(self, px, score):
        p = self.pos
        if p is None or not px:
            return
        pnl = p.unrealized(px)
        margin_pct = pnl / p.margin if p.margin else 0.0
        p.best_margin_pct = max(p.best_margin_pct, margin_pct)
        if p.side == "long":
            p.peak = max(p.peak or p.entry, px)
        else:
            p.peak = min(p.peak or p.entry, px)

        if margin_pct <= -0.98:
            self._close(px, "liquidation_backstop")
            return
        stop_margin = -float(cfg("ONCHAIN_STOP_MARGIN_PCT", 0.05))
        if margin_pct <= stop_margin:
            self._close(px, f"initial_stop ({stop_margin * 100:.1f}% margin)")
            return

        trail_arm = float(cfg("ONCHAIN_TRAIL_ARM_MARGIN_PCT", 0.08))
        trail_gap = float(cfg("ONCHAIN_TRAIL_GAP_MARGIN_PCT", 0.05))
        if p.best_margin_pct >= trail_arm:
            lock = p.best_margin_pct - trail_gap
            if margin_pct <= lock:
                self._close(px, f"trailing_lock ({lock * 100:+.1f}% margin)")
                return

        opp = float(cfg("ONCHAIN_OPPOSITE_EXIT_SCORE", 2.5))
        if p.side == "long" and score <= -opp:
            self._close(px, f"opposite_onchain_score ({score:+.2f})")
            return
        if p.side == "short" and score >= opp:
            self._close(px, f"opposite_onchain_score ({score:+.2f})")
            return

        max_hold = float(cfg("ONCHAIN_MAX_HOLD_SEC", 3600.0))
        if time.time() - p.opened_at >= max_hold:
            self._close(px, "time_stop")

    def _maybe_enter(self, px, score):
        if self.pos is not None or not self.enabled or not px:
            return
        cooldown = float(cfg("ONCHAIN_TRADE_COOLDOWN_SEC", 600.0))
        if time.time() - self._last_trade_ts < cooldown:
            return
        threshold = float(cfg("ONCHAIN_TRADE_THRESHOLD", 3.0))
        mom = self._price_change(float(cfg("ONCHAIN_CONFIRM_SECONDS", 60.0)))
        max_against = float(cfg("ONCHAIN_MAX_AGAINST_MOMENTUM", 0.0005))
        if score >= threshold:
            if mom >= -max_against or score >= threshold + 2.0:
                self._open("long", px, f"on-chain score {score:+.2f}, BTC {mom * 100:+.2f}% confirmation")
            else:
                self._skip_note(f"long score {score:+.2f} ignored: BTC confirmation {mom * 100:+.2f}%")
        elif score <= -threshold:
            if mom <= max_against or score <= -threshold - 2.0:
                self._open("short", px, f"on-chain score {score:+.2f}, BTC {mom * 100:+.2f}% confirmation")
            else:
                self._skip_note(f"short score {score:+.2f} ignored: BTC confirmation {mom * 100:+.2f}%")

    def _skip_note(self, msg):
        now = time.time()
        if now - self._last_skip_note_ts < 120:
            return
        self._last_skip_note_ts = now
        self._note(msg, "skip")

    async def manage_loop(self):
        await asyncio.sleep(9.0)
        while True:
            await asyncio.sleep(float(cfg("ONCHAIN_POLL_SECONDS", 20.0)))
            try:
                now = time.time()
                px = self._btc()
                self._track_price(px)
                score = self._active_score()
                if self.pos is not None and px:
                    self._manage_position(px, score)
                if not self.enabled:
                    continue
                await self._scan_all()
                self._learn_wallets(now, px)
                score = self._active_score()
                self._maybe_enter(px, score)
                if time.time() - self._last_save_ts > 30:
                    self._save()
            except Exception as e:
                self._err = f"{type(e).__name__}: {str(e)[:120]}"
                self._note(f"loop error: {self._err}", "error")

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - scanning on-chain logs for paper trades"
                   if self.enabled else "bot PAUSED - no new on-chain entries")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.balance = float(cfg("ONCHAIN_START_BALANCE", 100.0))
        self.start_balance = float(cfg("ONCHAIN_START_BALANCE", 100.0))
        self.pos = None
        self.history = []
        self.log = []
        self.signals = []
        self.pending = []
        self.wallet_score = {}
        self._ctx = {"score": 0.0, "bias": "flat", "components": []}
        self._last_trade_ts = 0.0
        self._note("bot reset (paper account back to $%.0f)" % self.start_balance)
        self._save()
        return {"ok": True}

    def state(self, **_):
        px = self._btc()
        score = self._active_score()
        positions = []
        if self.pos is not None:
            p = self.pos
            up = p.unrealized(px) if px else 0.0
            margin_pct = up / p.margin if p.margin else 0.0
            trail_gap = float(cfg("ONCHAIN_TRAIL_GAP_MARGIN_PCT", 0.05))
            lock = None
            if p.best_margin_pct >= float(cfg("ONCHAIN_TRAIL_ARM_MARGIN_PCT", 0.08)):
                lock = p.best_margin_pct - trail_gap
            note = p.note
            if lock is not None:
                note += f" - trailing lock {lock * 100:+.1f}% margin"
            else:
                note += f" - initial stop -{float(cfg('ONCHAIN_STOP_MARGIN_PCT', 0.05)) * 100:.1f}% margin"
            positions.append({
                "id": p.id,
                "side": p.side,
                "entry": round(p.entry, 2),
                "qty": round(p.qty, 6),
                "margin": round(p.margin, 2),
                "leverage": p.leverage,
                "liq": round(p.liq_price(), 2),
                "pnl": round(up, 2),
                "pnl_pct": round(margin_pct * 100.0, 2),
                "news": note,
            })
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        top_wallets = sorted(self.wallet_score.items(), key=lambda kv: kv[1], reverse=True)[:8]
        chains = []
        for chain, st in sorted(self.chain_status.items()):
            chains.append({
                "chain": chain,
                "latest": st.get("latest"),
                "cursor": st.get("cursor"),
                "logs": st.get("logs", 0),
                "error": st.get("error", ""),
            })
        return {
            "name": "On-chain Radar (paper)",
            "enabled": self.enabled,
            "running": True,
            "strategy": "onchain_radar",
            "strategy_label": "On-chain Radar (all-wallet flow + new-pool events)",
            "market": "perp",
            "timeframe": "event stream",
            "leverage": float(cfg("ONCHAIN_LEVERAGE", 20.0)),
            "price": round(px, 2) if px else None,
            "score": round(score, 2),
            "bias": self._ctx.get("bias", "flat"),
            "components": self._ctx.get("components", []),
            "chains": chains,
            "balance": round(self.balance, 2),
            "equity": round(self.equity(), 2),
            "start_balance": round(self.start_balance, 2),
            "total_pnl": round(self.equity() - self.start_balance, 2),
            "trades": len(self.history),
            "wins": wins,
            "positions": positions,
            "history": self.history,
            "signals": self.signals[:20],
            "learned_wallets": [{"addr": a, "score": round(v, 2)} for a, v in top_wallets if v > 0],
            "log": self.log[:25],
            "data_error": self._err,
        }
