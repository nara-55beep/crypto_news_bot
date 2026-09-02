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
POLL_SECONDS = 1.0
MAX_EVENTS = 100


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
        self._task: asyncio.Task | None = None

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
                        for trade in reversed(trades):
                            self._apply_once(trade)
                    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
                        self.online = False
                        self.error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(POLL_SECONDS)
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
            self._event("buy", token, f"BUY {token} · ${spend:.2f} @ {price:g}")
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
                                    "pnl": pnl, "ts": int(time.time() * 1000)})
            self.history = self.history[:100]
            self._event("sell", token, f"SELL {token} · {closed:g} @ {price:g} · P&L {pnl:+.2f}")

    def _event(self, kind: str, token: str, note: str):
        self.events.insert(0, {"id": f"{time.time_ns()}", "kind": kind, "token": token,
                               "note": note, "ts": int(time.time() * 1000)})
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
        self.error = ""
        return self.snapshot()

    def snapshot(self) -> dict:
        invested = sum(p["qty"] * p["entry"] for p in self.positions.values())
        pnl = self.balance + invested - START_BALANCE
        return {"name": self.name, "address": ADDRESS, "feed_url": FEED_URL,
                "enabled": self.enabled, "running": self.running, "online": self.online,
                "error": self.error, "start_balance": START_BALANCE, "balance": self.balance,
                "equity": self.balance + invested, "pnl": pnl,
                "positions": [{"token": token, **pos} for token, pos in self.positions.items()],
                "history": self.history, "events": self.events}
