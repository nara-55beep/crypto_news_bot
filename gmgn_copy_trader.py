"""Paper-only copy trader for a GMGN wallet feed.

GMGN does not provide a stable unauthenticated browser endpoint, so the feed URL
is configurable with ``GMGN_TRADE_FEED_URL``. The default is the requested
wallet page and the parser also accepts JSON responses from a proxy/API.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from typing import Any

import aiohttp

ADDRESS = "0x3ad83204e37fb98cd83ac523dfb65f7f99bb716d"
PAGE_URL = f"https://gmgn.ai/robinhood/address/{ADDRESS}"
FEED_URL = os.getenv("GMGN_TRADE_FEED_URL", PAGE_URL)
START_BALANCE = 50.0
POLL_SECONDS = 10.0
MAX_BACKOFF_SECONDS = 300.0
MAX_EVENTS = 100
STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "trenchflippermoney_state.json")


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _trade_id(trade: dict) -> str:
    for key in ("id", "tx_hash", "txHash", "hash", "signature", "transactionHash"):
        if trade.get(key):
            return str(trade[key])
    return "|".join(str(trade.get(k, "")) for k in ("timestamp", "token", "side", "price", "amount"))


def _extract_trades(payload: Any) -> list[dict]:
    """Extract trade-shaped records from either JSON or embedded page data."""
    records = []
    for item in _walk(payload):
        side = str(item.get("side", item.get("event_type",
                                           item.get("type", item.get("action", ""))))).lower()
        if side not in ("buy", "sell"):
            continue
        token = item.get("token") or item.get("symbol") or item.get("token_symbol") or item.get("coin")
        if isinstance(token, dict):
            token = token.get("symbol") or token.get("address")
        price = _number(item.get("price_usd") or item.get("price") or item.get("token_price"))
        qty = _number(item.get("quantity") or item.get("qty") or item.get("amount_token") or item.get("token_amount"))
        usd = _number(item.get("cost_usd") or item.get("usd") or item.get("amount_usd") or item.get("value") or item.get("volume"))
        if token and (price or qty or usd):
            records.append({"id": _trade_id(item), "side": side, "token": str(token),
                            "price": price, "qty": qty, "usd": usd,
                            "ts": _number(item.get("timestamp") or item.get("time")) or time.time()})
    return records


class GMGNCopyTrader:
    name = "trenchflippermoney"

    def __init__(self):
        self.enabled = True
        self.running = False
        self.online = False
        self.error = ""
        self.balance = START_BALANCE
        self.positions: dict[str, dict[str, float]] = {}
        self.history: list[dict] = []
        self.events: list[dict] = []
        self._seen: set[str] = set()
        self._baselined = False
        self._task: asyncio.Task | None = None
        self._backoff_seconds = POLL_SECONDS
        self._load()

    async def run(self):
        self.running = True
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": "Mozilla/5.0 paper-trading-monitor"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            while self.running:
                if self.enabled:
                    try:
                        if FEED_URL == PAGE_URL:
                            payload = await self._cli_payload()
                        else:
                            async with session.get(FEED_URL) as response:
                                if response.status >= 400:
                                    raise RuntimeError(f"feed returned HTTP {response.status}")
                                text = await response.text()
                                try:
                                    payload = json.loads(text)
                                except json.JSONDecodeError:
                                    payload = text
                        trades = _extract_trades(payload)
                        self.online, self.error = True, ""
                        if not self._baselined:
                            self._seen.update(trade["id"] for trade in trades)
                            self._baselined = True
                            self._save()
                            await asyncio.sleep(POLL_SECONDS)
                            continue
                        for trade in sorted(trades, key=lambda item: item.get("ts", 0)):
                            self._apply_once(trade)
                        self._save()
                    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
                        self.online = False
                        self.error = f"{type(exc).__name__}: {exc}"
                        if "429" in str(exc) or "RATE_LIMIT" in str(exc):
                            self._backoff_seconds = min(
                                max(self._backoff_seconds * 2, 30.0),
                                MAX_BACKOFF_SECONDS,
                            )
                        else:
                            self._backoff_seconds = POLL_SECONDS
                else:
                    self._backoff_seconds = POLL_SECONDS
                await asyncio.sleep(self._backoff_seconds)
                if self.online:
                    self._backoff_seconds = POLL_SECONDS
        self.running = False

    async def _cli_payload(self) -> Any:
        """Use GMGN's official authenticated read CLI (credentials stay local)."""
        cli = shutil.which("gmgn-cli")
        if cli is None:
            cli = "gmgn-cli.cmd" if sys.platform == "win32" else "gmgn-cli"
            if shutil.which(cli) is None:
                npx = shutil.which("npx.cmd" if sys.platform == "win32" else "npx")
                if npx is None:
                    raise RuntimeError("gmgn-cli is not installed; run npm install -g gmgn-cli")
                command = [npx, "--yes", "gmgn-cli"]
            else:
                command = [cli]
        else:
            command = [cli]
        command += ["portfolio", "activity", "--chain", "robinhood", "--wallet",
                    ADDRESS, "--limit", "100", "--raw"]
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("gmgn-cli query timed out")
        if proc.returncode:
            detail = stderr.decode(errors="replace").strip()
            raise RuntimeError(detail or f"gmgn-cli exited with code {proc.returncode}")
        try:
            return json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("gmgn-cli returned invalid JSON") from exc

    def _apply_once(self, trade: dict):
        trade_id = trade["id"]
        if trade_id in self._seen:
            return
        self._seen.add(trade_id)
        if len(self._seen) > 2000:
            self._seen = set(list(self._seen)[-1000:])
        token, side = trade["token"], trade["side"]
        source_ts = int(float(trade.get("ts", time.time())) * 1000)
        observed_ts = int(time.time() * 1000)
        price, qty, usd = trade["price"], trade["qty"], trade["usd"]
        if price is None and qty and usd:
            price = usd / qty
        if price is None or price <= 0:
            return
        if qty is None and usd:
            qty = usd / price
        if qty is None or qty <= 0:
            return
        if side == "buy":
            spend = min(self.balance, qty * price)
            qty = spend / price
            if qty <= 0:
                return
            self.balance -= spend
            pos = self.positions.get(token)
            if pos:
                total = pos["qty"] + qty
                pos["entry"] = (pos["entry"] * pos["qty"] + price * qty) / total
                pos["qty"] = total
            else:
                self.positions[token] = {"qty": qty, "entry": price}
            self._event("buy", token, f"BUY {token} · ${spend:.2f} @ {price:g}",
                        source_ts, observed_ts, trade_id)
        else:
            pos = self.positions.get(token)
            if not pos:
                return
            closed = min(qty, pos["qty"])
            proceeds = closed * price
            pnl = closed * (price - pos["entry"])
            self.balance += proceeds
            pos["qty"] -= closed
            if pos["qty"] <= 1e-12:
                self.positions.pop(token)
            self.history.insert(0, {"token": token, "side": "sell", "price": price,
                                    "pnl": pnl, "ts": source_ts,
                                    "observed_ts": observed_ts, "trade_id": trade_id})
            self.history = self.history[:100]
            self._event("sell", token, f"SELL {token} · {closed:g} @ {price:g} · P&L {pnl:+.2f}",
                        source_ts, observed_ts, trade_id)

    def _event(self, kind: str, token: str, note: str, source_ts: int,
               observed_ts: int, trade_id: str):
        self.events.insert(0, {"id": trade_id, "kind": kind, "token": token,
                               "note": note, "ts": source_ts,
                               "observed_ts": observed_ts})
        self.events = self.events[:MAX_EVENTS]

    def toggle(self, enabled: bool | None = None) -> dict:
        self.enabled = not self.enabled if enabled is None else bool(enabled)
        return self.snapshot()

    def reset(self) -> dict:
        self.balance = START_BALANCE
        self.positions.clear()
        self.history.clear()
        self.events.clear()
        self._seen.clear()
        self._baselined = False
        self.error = ""
        self._save()
        return self.snapshot()

    def _load(self):
        try:
            with open(STATE_PATH, encoding="utf-8") as handle:
                state = json.load(handle)
            self._seen = set(state.get("seen", []))
            self._baselined = bool(state.get("baselined", False))
            self.balance = float(state.get("balance", START_BALANCE))
            self.positions = state.get("positions", {})
            self.history = state.get("history", [])[:100]
            self.events = state.get("events", [])[:MAX_EVENTS]
        except FileNotFoundError:
            return
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._seen.clear()
            self._baselined = False

    def _save(self):
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        state = {"seen": list(self._seen)[-2000:], "baselined": self._baselined,
                 "balance": self.balance, "positions": self.positions,
                 "history": self.history, "events": self.events}
        temp_path = STATE_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temp_path, STATE_PATH)

    def snapshot(self) -> dict:
        invested = sum(p["qty"] * p["entry"] for p in self.positions.values())
        pnl = self.balance + invested - START_BALANCE
        return {"name": self.name, "address": ADDRESS, "feed_url": FEED_URL,
                "enabled": self.enabled, "running": self.running, "online": self.online,
                "error": self.error, "start_balance": START_BALANCE, "balance": self.balance,
                "equity": self.balance + invested, "pnl": pnl,
                "positions": [{"token": token, **pos} for token, pos in self.positions.items()],
                "history": self.history, "events": self.events}
