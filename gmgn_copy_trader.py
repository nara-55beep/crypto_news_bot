"""Paper tracker plus opt-in live copy execution for one GMGN wallet.

Live trades are routed through GMGN's official CLI. The CLI reads signing
credentials from the process environment; this module never accepts, stores,
logs, or returns a private key.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import math
import os
import re
import shutil
import sys
import time
from typing import Any

import aiohttp

ADDRESS = "0x3ad83204e37fb98cd83ac523dfb65f7f99bb716d"
CHAIN = "robinhood"
NATIVE_TOKEN = "0x0000000000000000000000000000000000000000"
PAGE_URL = f"https://gmgn.ai/{CHAIN}/address/{ADDRESS}"
FEED_URL = os.getenv("GMGN_TRADE_FEED_URL", PAGE_URL)
START_BALANCE = 50.0
POLL_SECONDS = 10.0
MAX_BACKOFF_SECONDS = 300.0
MAX_EVENTS = 100
STATE_PATH = os.path.join(os.path.dirname(__file__), "data", "trenchflippermoney_state.json")
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(max(value, minimum), maximum)


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _timestamp_seconds(value: Any) -> float:
    stamp = _number(value) or time.time()
    return stamp / 1000.0 if stamp > 10_000_000_000 else stamp


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
    """Extract copyable records from GMGN CLI JSON or a compatible feed."""
    records = []
    for item in _walk(payload):
        side = str(item.get("side", item.get("event_type",
                                           item.get("type", item.get("action", ""))))).lower()
        if side not in ("buy", "sell"):
            continue

        token_value = item.get("token") or item.get("symbol") or item.get("token_symbol") or item.get("coin")
        token_address = item.get("token_address") or item.get("address")
        if isinstance(token_value, dict):
            token_address = token_value.get("address") or token_value.get("token_address") or token_address
            token = token_value.get("symbol") or token_address
        else:
            token = token_value
            if isinstance(token, str) and EVM_ADDRESS_RE.fullmatch(token):
                token_address = token_address or token

        quote = item.get("quote_token") if isinstance(item.get("quote_token"), dict) else {}
        quote_address = (item.get("quote_address") or quote.get("token_address")
                         or quote.get("address"))
        quote_amount = _number(item.get("quote_amount") or item.get("amount_quote"))
        quote_decimals = _integer(item.get("quote_decimals") or quote.get("decimals"))
        price = _number(item.get("price_usd") or item.get("price") or item.get("token_price"))
        qty = _number(item.get("quantity") or item.get("qty") or item.get("amount_token") or item.get("token_amount"))
        usd = _number(item.get("cost_usd") or item.get("usd") or item.get("amount_usd") or item.get("value") or item.get("volume"))
        if token and (price or qty or usd):
            records.append({
                "id": _trade_id(item),
                "side": side,
                "token": str(token),
                "token_address": str(token_address) if token_address else None,
                "quote_address": str(quote_address) if quote_address else None,
                "quote_amount": quote_amount,
                "quote_decimals": quote_decimals,
                "price": price,
                "qty": qty,
                "usd": usd,
                "ts": _timestamp_seconds(item.get("timestamp") or item.get("time")),
            })
    return records


class GMGNCopyTrader:
    name = "trenchflippermoney"

    def __init__(self, *, state_path: str = STATE_PATH,
                 live_enabled: bool | None = None,
                 wallet_address: str | None = None):
        self.state_path = state_path
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

        self.live_enabled = (_env_flag("GMGN_COPY_LIVE_ENABLED")
                             if live_enabled is None else bool(live_enabled))
        self._wallet_address_locked = wallet_address is not None or bool(
            os.getenv("GMGN_COPY_WALLET_ADDRESS", "").strip()
        )
        self.live_wallet_address = (wallet_address if wallet_address is not None
                                    else os.getenv("GMGN_COPY_WALLET_ADDRESS", "")).strip()
        self.live_budget_usd = _env_float("GMGN_COPY_LIVE_BUDGET_USD", 10.0, 0.01, 10.0)
        self.live_slippage = _env_float("GMGN_COPY_MAX_SLIPPAGE_PERCENT", 10.0, 0.1, 100.0)
        self.live_positions: dict[str, dict[str, Any]] = {}
        self.live_events: list[dict] = []
        self.live_error = ""
        self._live_seen: set[str] = set()
        self._live_lock = asyncio.Lock()
        self._load()

    async def run(self):
        self.running = True
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": "Mozilla/5.0 copy-trading-monitor"}
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
                            trade_ids = {trade["id"] for trade in trades}
                            self._seen.update(trade_ids)
                            self._live_seen.update(trade_ids)
                            self._baselined = True
                            self._save()
                            await asyncio.sleep(POLL_SECONDS)
                            continue
                        for trade in sorted(trades, key=lambda item: item.get("ts", 0)):
                            if trade["id"] in self._seen:
                                continue
                            self._apply_once(trade)
                            await self._claim_and_copy_live(trade)
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

    def _cli_prefix(self) -> list[str]:
        cli = shutil.which("gmgn-cli")
        if cli:
            return [cli]
        cli = "gmgn-cli.cmd" if sys.platform == "win32" else "gmgn-cli"
        if shutil.which(cli):
            return [cli]
        npx = shutil.which("npx.cmd" if sys.platform == "win32" else "npx")
        if npx is None:
            raise RuntimeError("gmgn-cli is not installed; run npm install -g gmgn-cli")
        return [npx, "--yes", "gmgn-cli"]

    def _redact(self, text: str) -> str:
        for name in ("GMGN_API_KEY", "GMGN_PRIVATE_KEY"):
            secret = os.getenv(name, "")
            if secret:
                text = text.replace(secret, "[redacted]")
        return text[:1000]

    async def _run_cli(self, args: list[str], timeout: float = 30.0) -> Any:
        proc = await asyncio.create_subprocess_exec(
            *(self._cli_prefix() + args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("gmgn-cli command timed out")
        if proc.returncode:
            detail = self._redact(stderr.decode(errors="replace").strip())
            raise RuntimeError(detail or f"gmgn-cli exited with code {proc.returncode}")
        try:
            return json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("gmgn-cli returned invalid JSON") from exc

    async def _cli_payload(self) -> Any:
        """Use GMGN's official authenticated read CLI (credentials stay local)."""
        return await self._run_cli([
            "portfolio", "activity", "--chain", CHAIN, "--wallet", ADDRESS,
            "--limit", "100", "--type", "buy", "--type", "sell", "--raw",
        ], timeout=15.0)

    def _live_config_problem(self, *, require_enabled: bool = True) -> str:
        if require_enabled and not self.live_enabled:
            return "live mode is disabled"
        if not EVM_ADDRESS_RE.fullmatch(self.live_wallet_address):
            return "set a valid GMGN_COPY_WALLET_ADDRESS"
        if not _env_flag("GMGN_ALLOW_AUTOMATED_TRADES"):
            return "GMGN_ALLOW_AUTOMATED_TRADES=1 is required"
        return ""

    def _live_allocated_usd(self) -> float:
        return sum(float(position.get("cost_usd", 0)) for position in self.live_positions.values())

    def _live_available_usd(self) -> float:
        return max(0.0, self.live_budget_usd - self._live_allocated_usd())

    def _live_event(self, kind: str, token: str, note: str, trade_id: str,
                    *, tx_hash: str = ""):
        self.live_events.insert(0, {
            "id": f"live:{trade_id}:{time.time_ns()}",
            "trade_id": trade_id,
            "kind": kind,
            "token": token,
            "note": note,
            "tx_hash": tx_hash,
            "ts": int(time.time() * 1000),
        })
        self.live_events = self.live_events[:MAX_EVENTS]

    async def _claim_and_copy_live(self, trade: dict):
        """Claim an observation before submitting it, providing at-most-once execution."""
        trade_id = trade["id"]
        if trade_id in self._live_seen:
            return
        self._live_seen.add(trade_id)
        self._save()
        if not self.live_enabled:
            return
        problem = self._live_config_problem()
        if problem:
            self.live_error = problem
            self._live_event("blocked", trade.get("token", "?"), problem, trade_id)
            return
        async with self._live_lock:
            try:
                if trade.get("side") == "buy":
                    await self._copy_live_buy(trade)
                else:
                    await self._copy_live_sell(trade)
                self.live_error = ""
            except (RuntimeError, ValueError) as exc:
                self.live_error = self._redact(str(exc))
                self._live_event(
                    "error", trade.get("token", "?"),
                    f"Live {trade.get('side', 'trade')} skipped: {self.live_error}", trade_id,
                )
            finally:
                self._save()

    async def _copy_live_buy(self, trade: dict):
        token_address = str(trade.get("token_address") or "")
        quote_address = str(trade.get("quote_address") or "")
        source_usd = _number(trade.get("usd"))
        quote_amount = _number(trade.get("quote_amount"))
        quote_decimals = _integer(trade.get("quote_decimals"))
        if not EVM_ADDRESS_RE.fullmatch(token_address):
            raise ValueError("feed did not include a valid token contract address")
        if not EVM_ADDRESS_RE.fullmatch(quote_address):
            raise ValueError("feed did not include a valid quote-token address")
        if source_usd is None or source_usd <= 0 or quote_amount is None or quote_amount <= 0:
            raise ValueError("feed did not include positive USD and quote amounts")
        if quote_decimals is None or not 0 <= quote_decimals <= 36:
            raise ValueError("feed did not include valid quote-token decimals")
        available = self._live_available_usd()
        copy_usd = min(source_usd, available)
        if copy_usd <= 0:
            self._live_event(
                "skipped", trade["token"],
                f"Skipped buy: ${available:.2f} live budget available",
                trade["id"],
            )
            return

        raw_amount = int(
            Decimal(str(quote_amount)) * Decimal(str(copy_usd))
            / Decimal(str(source_usd)) * (Decimal(10) ** quote_decimals)
        )
        if raw_amount <= 0:
            raise ValueError("calculated swap amount is zero")
        result = await self._execute_swap(quote_address, token_address, raw_amount)
        output_amount = self._filled_amount(result, "output")
        if output_amount is None or output_amount <= 0:
            raise RuntimeError("swap confirmed without a filled output amount")

        key = token_address.lower()
        position = self.live_positions.get(key)
        if position:
            position["raw_qty"] = int(position["raw_qty"]) + output_amount
            position["cost_usd"] = float(position["cost_usd"]) + copy_usd
            position["source_qty"] = float(position.get("source_qty", 0)) + float(
                trade.get("qty") or 0
            )
        else:
            self.live_positions[key] = {
                "token": trade["token"],
                "token_address": token_address,
                "quote_address": quote_address,
                "raw_qty": output_amount,
                "cost_usd": copy_usd,
                "source_qty": float(trade.get("qty") or 0),
            }
        tx_hash = str(result.get("hash") or result.get("tx_hash") or "")
        self._live_event(
            "buy", trade["token"], f"LIVE BUY {trade['token']} - ${copy_usd:.2f}",
            trade["id"], tx_hash=tx_hash,
        )

    async def _copy_live_sell(self, trade: dict):
        token_address = str(trade.get("token_address") or "").lower()
        position = self.live_positions.get(token_address)
        if not position:
            self._live_event(
                "skipped", trade.get("token", "?"),
                "Skipped sell: no live position opened by this bot", trade["id"],
            )
            return
        raw_position = _integer(position.get("raw_qty"))
        if raw_position is None or raw_position <= 0:
            raise ValueError("tracked live position has no sellable amount")
        source_position = _number(position.get("source_qty"))
        source_sell = _number(trade.get("qty"))
        fraction = 1.0
        if source_position and source_position > 0 and source_sell and source_sell > 0:
            fraction = min(1.0, source_sell / source_position)
        raw_amount = min(raw_position, max(1, int(raw_position * fraction)))
        result = await self._execute_swap(
            position["token_address"], position["quote_address"], raw_amount
        )
        tx_hash = str(result.get("hash") or result.get("tx_hash") or "")
        token = str(position.get("token") or trade.get("token") or token_address)
        remaining_raw = raw_position - raw_amount
        if remaining_raw <= 0:
            self.live_positions.pop(token_address, None)
        else:
            position["raw_qty"] = remaining_raw
            position["cost_usd"] = float(position.get("cost_usd", 0)) * (1.0 - fraction)
            if source_position:
                position["source_qty"] = max(0.0, source_position - (source_sell or 0))
        self._live_event(
            "sell", token, f"LIVE SELL {token} - {fraction * 100:.1f}% of tracked position",
            trade["id"], tx_hash=tx_hash,
        )

    async def _execute_swap(self, input_token: str, output_token: str,
                            raw_amount: int) -> dict:
        payload = await self._run_cli([
            "swap", "--chain", CHAIN, "--from", self.live_wallet_address,
            "--input-token", input_token, "--output-token", output_token,
            "--amount", str(raw_amount), "--slippage", f"{self.live_slippage:g}",
            "--anti-mev", "--yes", "--raw",
        ], timeout=60.0)
        result = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(result, dict):
            raise RuntimeError("GMGN returned an invalid swap response")
        # Inspect the initial response, then poll at most three times.
        for attempt in range(4):
            status = self._order_status(result)
            if status in {"confirmed", "successful"}:
                return result
            if status in {"failed", "expired"}:
                detail = result.get("error_status") or result.get("error_code") or status
                raise RuntimeError(f"GMGN swap {detail}")
            order_id = result.get("order_id")
            if not order_id or attempt == 3:
                break
            await asyncio.sleep(5)
            payload = await self._run_cli([
                "order", "get", "--chain", CHAIN,
                "--order-id", str(order_id), "--raw",
            ])
            result = payload.get("data", payload) if isinstance(payload, dict) else {}
        raise RuntimeError(f"GMGN swap did not confirm (status: {self._order_status(result) or 'unknown'})")

    @staticmethod
    def _order_status(result: dict) -> str:
        confirmation = result.get("confirmation")
        if isinstance(confirmation, dict) and confirmation.get("state"):
            return str(confirmation["state"]).lower()
        if _integer(result.get("state")) == 30:
            return "confirmed"
        return str(result.get("status") or "").lower()

    @staticmethod
    def _filled_amount(result: dict, direction: str) -> int | None:
        value = result.get(f"filled_{direction}_amount")
        report = result.get("report")
        if value in (None, "") and isinstance(report, dict):
            value = report.get(f"{direction}_amount")
        return _integer(value)

    async def liquidate_live(self, confirmation: str) -> dict:
        """Sell only positions recorded as bought by this bot."""
        if confirmation != "CLOSE LIVE POSITIONS":
            return {"ok": False, "error": "confirmation phrase did not match"}
        problem = self._live_config_problem(require_enabled=False)
        if problem:
            return {"ok": False, "error": problem}
        results = []
        async with self._live_lock:
            for key, position in list(self.live_positions.items()):
                try:
                    result = await self._execute_swap(
                        position["token_address"], position["quote_address"],
                        int(position["raw_qty"]),
                    )
                    tx_hash = str(result.get("hash") or result.get("tx_hash") or "")
                    token = str(position.get("token") or key)
                    self.live_positions.pop(key, None)
                    self._live_event(
                        "sell", token, f"EMERGENCY LIVE SELL {token}",
                        f"manual:{time.time_ns()}", tx_hash=tx_hash,
                    )
                    results.append({"token": token, "ok": True, "tx_hash": tx_hash})
                except (RuntimeError, ValueError) as exc:
                    error = self._redact(str(exc))
                    results.append({"token": position.get("token", key), "ok": False,
                                    "error": error})
            self._save()
        return {"ok": all(item["ok"] for item in results), "results": results,
                "state": self.snapshot()}

    async def set_live_wallet(self, wallet_address: str) -> dict:
        """Persist a public Phantom EVM address selected on the local dashboard."""
        wallet_address = wallet_address.strip()
        if not EVM_ADDRESS_RE.fullmatch(wallet_address):
            return {"ok": False, "error": "Phantom did not return a valid EVM address"}
        if self._wallet_address_locked and self.live_wallet_address.lower() != wallet_address.lower():
            return {"ok": False, "error": "wallet address is locked by GMGN_COPY_WALLET_ADDRESS"}
        self.live_wallet_address = wallet_address
        self.live_error = ""
        self._save()
        return {"ok": True, "state": self.snapshot()}

    def _apply_once(self, trade: dict):
        trade_id = trade["id"]
        if trade_id in self._seen:
            return
        self._seen.add(trade_id)
        if len(self._seen) > 2000:
            self._seen = set(list(self._seen)[-1000:])
        token, side = trade["token"], trade["side"]
        source_ts = int(_timestamp_seconds(trade.get("ts")) * 1000)
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
            self._event("buy", token, f"BUY {token} - ${spend:.2f} @ {price:g}",
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
            self._event("sell", token, f"SELL {token} - {closed:g} @ {price:g} - P&L {pnl:+.2f}",
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
        """Reset only the paper ledger; never forget or touch real positions."""
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
            with open(self.state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            self._seen = set(state.get("seen", []))
            self._baselined = bool(state.get("baselined", False))
            self.balance = float(state.get("balance", START_BALANCE))
            self.positions = state.get("positions", {})
            self.history = state.get("history", [])[:100]
            self.events = state.get("events", [])[:MAX_EVENTS]
            self._live_seen = set(state.get("live_seen", self._seen))
            self.live_positions = state.get("live_positions", {})
            self.live_events = state.get("live_events", [])[:MAX_EVENTS]
            self.live_error = str(state.get("live_error", ""))
            if not self.live_wallet_address:
                saved_wallet = str(state.get("live_wallet_address", ""))
                if EVM_ADDRESS_RE.fullmatch(saved_wallet):
                    self.live_wallet_address = saved_wallet
        except FileNotFoundError:
            return
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._seen.clear()
            self._live_seen.clear()
            self._baselined = False

    def _save(self):
        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        state = {
            "seen": list(self._seen)[-2000:],
            "baselined": self._baselined,
            "balance": self.balance,
            "positions": self.positions,
            "history": self.history,
            "events": self.events,
            "live_seen": list(self._live_seen)[-2000:],
            "live_positions": self.live_positions,
            "live_events": self.live_events,
            "live_error": self.live_error,
            "live_wallet_address": self.live_wallet_address,
        }
        temp_path = self.state_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temp_path, self.state_path)

    def snapshot(self) -> dict:
        invested = sum(p["qty"] * p["entry"] for p in self.positions.values())
        pnl = self.balance + invested - START_BALANCE
        live_problem = self._live_config_problem() if self.live_enabled else "live mode is disabled"
        live_status = live_problem or "armed - real-money orders enabled"
        return {
            "name": self.name,
            "address": ADDRESS,
            "chain": CHAIN,
            "feed_url": FEED_URL,
            "enabled": self.enabled,
            "running": self.running,
            "online": self.online,
            "error": self.error,
            "start_balance": START_BALANCE,
            "balance": self.balance,
            "equity": self.balance + invested,
            "pnl": pnl,
            "positions": [{"token": token, **pos} for token, pos in self.positions.items()],
            "history": self.history,
            "events": self.events,
            "live": {
                "enabled": self.live_enabled,
                "ready": self.live_enabled and not live_problem,
                "status": live_status,
                "wallet_address": self.live_wallet_address,
                "budget_usd": self.live_budget_usd,
                "allocated_usd": self._live_allocated_usd(),
                "available_usd": self._live_available_usd(),
                "slippage_percent": self.live_slippage,
                "error": self.live_error,
                "positions": list(self.live_positions.values()),
                "events": self.live_events,
            },
        }
