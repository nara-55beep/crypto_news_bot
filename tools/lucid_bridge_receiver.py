"""
Local HTTP receiver for Lucid exact-source bridge data.

This is intentionally small and broker-neutral. A Dukascopy/JForex producer can
POST exact USA500IDXUSD/USATECHIDXUSD/LIGHTCMDUSD ticks or completed 1m bars to
localhost, and this receiver writes the bridge CSV files consumed by
LUCID_LIVE_SOURCE=local_bridge.

Endpoints:
  GET  /health
  GET  /ready
  POST /bar   {"market":"es|nq|cl","dt_utc":"2026-07-07T13:00:00Z",
               "open":1,"high":1,"low":1,"close":1,"volume":1}
  POST /tick  {"market":"es|nq|cl","dt_utc":"2026-07-07T13:00:12Z",
               "price":1,"volume":1}

Optional auth:
  set LUCID_BRIDGE_TOKEN and send header X-Lucid-Bridge-Token.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
import lucid_pass_paper as lucid


MARKETS = {"es", "nq", "cl"}
COLUMNS = ["dt_utc", "open", "high", "low", "close", "volume"]


def _market_path(market: str) -> pathlib.Path:
    return pathlib.Path(lucid._local_bridge_path(market))


def _utc_minute(value) -> pd.Timestamp:
    if value is None or value == "":
        raise ValueError("missing dt_utc")
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError("invalid dt_utc")
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.floor("min")


def _max_future_seconds() -> float:
    return float(os.getenv("LUCID_BRIDGE_MAX_FUTURE_SEC", "120"))


def _reject_far_future(dt: pd.Timestamp) -> None:
    max_future = _max_future_seconds()
    if max_future < 0:
        return
    now = pd.Timestamp.now(tz="UTC")
    if (dt - now).total_seconds() > max_future:
        raise ValueError(f"dt_utc is more than {max_future:.0f}s in the future")


def _finite_float(value, name: str) -> float:
    val = float(value)
    if not math.isfinite(val):
        raise ValueError(f"{name} must be finite")
    return val


def _price(value, name: str) -> float:
    val = _finite_float(value, name)
    if val <= 0:
        raise ValueError(f"{name} must be positive")
    return val


def _volume(value) -> float:
    val = _finite_float(value if value is not None else 0.0, "volume")
    if val < 0:
        raise ValueError("volume must be non-negative")
    return val


def _bar_from_payload(payload: dict) -> dict:
    dt = _utc_minute(payload.get("dt_utc") or payload.get("ts") or payload.get("time"))
    _reject_far_future(dt)
    vals = {}
    for name in ("open", "high", "low", "close"):
        if name not in payload:
            raise ValueError(f"missing {name}")
        vals[name] = _price(payload[name], name)
    if vals["high"] < max(vals["open"], vals["close"]) or vals["low"] > min(vals["open"], vals["close"]):
        raise ValueError("invalid OHLC: high/low must contain open and close")
    if vals["high"] < vals["low"]:
        raise ValueError("invalid OHLC: high is below low")
    volume = _volume(payload.get("volume", 0.0))
    return {
        "dt_utc": dt.isoformat(),
        "open": vals["open"],
        "high": vals["high"],
        "low": vals["low"],
        "close": vals["close"],
        "volume": volume,
    }


def _tick_price(payload: dict) -> float:
    if "price" in payload:
        return _price(payload["price"], "price")
    if "bid" in payload and "ask" in payload:
        bid = _price(payload["bid"], "bid")
        ask = _price(payload["ask"], "ask")
        if ask < bid:
            raise ValueError("ask must be greater than or equal to bid")
        return (bid + ask) / 2.0
    raise ValueError("tick needs price or bid+ask")


class BridgeStore:
    # Ticks must be handled in O(1). The old path re-parsed and re-wrote the whole
    # CSV on EVERY tick (~23 ticks/s ceiling), so JForex's 1s POST timeout dropped
    # ticks on fast markets - NQ lost up to 74% of its volume, which moved the
    # volume-weighted VWAP bands and changed trades vs the backtest. Now: bars live
    # in a plain dict (O(1) per tick) and disk writes happen on a timer.
    FLUSH_SEC = float(os.getenv("LUCID_BRIDGE_FLUSH_SEC", "1.0"))

    def __init__(self):
        self.lock = threading.RLock()
        # market -> {minute_epoch_int: [open, high, low, close, volume]}
        self.bars: dict[str, dict[int, list]] = {}
        self.dirty: dict[str, bool] = {m: False for m in MARKETS}
        for m in MARKETS:
            self.bars[m] = self._dict_from_frame(self._load(m))
        self._stop = threading.Event()
        self._writer = threading.Thread(target=self._flush_loop, name="bridge-flush", daemon=True)
        self._writer.start()

    @staticmethod
    def _dict_from_frame(df: pd.DataFrame) -> dict[int, list]:
        out: dict[int, list] = {}
        if df is None or df.empty:
            return out
        # UNIT-SAFE epoch seconds. pandas >=3 returns datetime64[us]; assuming ns here
        # silently mapped 2026 timestamps into 1970 and corrupted the bridge CSVs.
        dt = pd.to_datetime(df["dt_utc"], utc=True)
        ts = (dt - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds().astype("int64")
        for t, o, h, l, c, v in zip(ts, df["open"], df["high"], df["low"], df["close"], df["volume"]):
            out[int(t)] = [float(o), float(h), float(l), float(c), float(v)]
        return out

    def _frame(self, market: str) -> pd.DataFrame:
        """Build a DataFrame from the in-memory bars (only for disk writes / status)."""
        d = self.bars.get(market) or {}
        if not d:
            return pd.DataFrame(columns=COLUMNS)
        keys = sorted(d)
        max_rows = int(os.getenv("LUCID_BRIDGE_MAX_ROWS", "20000"))
        keys = keys[-max_rows:]
        rows = [d[k] for k in keys]
        df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
        df.insert(0, "dt_utc", pd.to_datetime(pd.Series(keys), unit="s", utc=True))
        return df

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.FLUSH_SEC)
            for market in list(MARKETS):
                try:
                    with self.lock:
                        if not self.dirty.get(market):
                            continue
                        self.dirty[market] = False
                    self._flush(market)
                except Exception:
                    pass

    def _load(self, market: str) -> pd.DataFrame:
        # Try the live CSV, then the rolling backup. A truncated CSV (e.g. power
        # loss) must not silently reset to empty, or the next _flush() overwrites
        # the whole tick history with a blank file.
        path = _market_path(market)
        for candidate in (path, path.with_suffix(path.suffix + ".bak")):
            if not candidate.exists():
                continue
            try:
                df = pd.read_csv(candidate)
                out = lucid._duka_normalize_frame(df)[COLUMNS]
                if not out.empty:
                    return out
            except Exception:
                try:
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    shutil.copyfile(candidate, f"{candidate}.corrupt-{stamp}")
                except Exception:
                    pass
                print(f"WARNING: bridge CSV {candidate.name} unreadable; preserved copy, trying backup")
        return pd.DataFrame(columns=COLUMNS)

    def _flush(self, market: str) -> None:
        path = _market_path(market)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            df = self._frame(market)
        tmp = path.with_suffix(path.suffix + ".tmp")
        # fsync so a power cut cannot leave a renamed-but-empty CSV
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            df.to_csv(f, index=False)
            f.flush()
            os.fsync(f.fileno())
        try:
            if path.exists() and path.stat().st_size > 0:
                shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        except Exception:
            pass
        os.replace(tmp, path)

    def put_bar(self, market: str, payload: dict) -> dict:
        market = str(market or "").lower()
        if market not in MARKETS:
            raise ValueError("market must be es, nq, or cl")
        row = _bar_from_payload(payload)
        key = int(pd.Timestamp(row["dt_utc"]).timestamp())
        with self.lock:
            self.bars[market][key] = [row["open"], row["high"], row["low"],
                                      row["close"], row["volume"]]
            self.dirty[market] = True
            return self.status(market)

    def put_tick(self, market: str, payload: dict) -> dict:
        market = str(market or "").lower()
        if market not in MARKETS:
            raise ValueError("market must be es, nq, or cl")
        dt = _utc_minute(payload.get("dt_utc") or payload.get("ts") or payload.get("time"))
        _reject_far_future(dt)
        px = _tick_price(payload)
        volume = _volume(payload.get("volume", 0.0))
        key = int(dt.timestamp())
        # O(1): touch only the current minute's bar. No DataFrame work, no disk IO.
        with self.lock:
            bar = self.bars[market].get(key)
            if bar is None:
                self.bars[market][key] = [px, px, px, px, volume]
            else:
                if px > bar[1]:
                    bar[1] = px          # high
                if px < bar[2]:
                    bar[2] = px          # low
                bar[3] = px              # close
                bar[4] += volume         # cumulative tick volume
            self.dirty[market] = True
        return {"ok": True, "market": market, "minute": dt.isoformat()}

    def status(self, market: str | None = None, include_ready: bool = False) -> dict:
        with self.lock:
            markets = sorted([market] if market else MARKETS)
            out = {}
            for m in markets:
                d = self.bars.get(m) or {}
                rows = len(d)
                latest = str(pd.Timestamp(max(d), unit="s", tz="UTC")) if d else None
                out[m] = {"path": str(_market_path(m)), "rows": rows, "latest_dt_utc": latest}
            missing = [m for m in sorted(MARKETS) if not _market_path(m).exists()]
            family = str(getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "") or "").strip()
            payload = {
                "ok": not missing,
                "alive": True,
                "source_family": family or "unverified_source",
                "expected_source_family": lucid.BACKTEST_FEED_FAMILY,
                "missing_files": missing,
                "markets": out,
            }
        if include_ready:
            ready, report = self.ready_report()
            payload["ok"] = ready
            payload["ready"] = ready
            payload.update(report)
        return payload

    def ready_report(self) -> tuple[bool, dict]:
        problems: list[str] = []
        family = str(getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "") or "").strip()
        if family != lucid.BACKTEST_FEED_FAMILY:
            problems.append(f"set LUCID_LOCAL_BRIDGE_SOURCE_FAMILY={lucid.BACKTEST_FEED_FAMILY}")
        try:
            frames, source_status = lucid._load_local_bridge_component_data_all()
            source_block = lucid._lucid_source_block_reason(source_status, require_match=True)
            bridge_block = lucid._lucid_local_bridge_block_reason(source_status)
            history_block = lucid._lucid_history_block_reason(frames)
            freshness_block = lucid._lucid_exact_source_freshness_block(frames)
            details = lucid._lucid_exact_source_freshness_details(frames)
            freshness_warning = lucid._lucid_exact_source_freshness_warning(details)
            for item in (source_block, bridge_block, history_block, freshness_block):
                if item:
                    problems.append(item)
            exact_realtime_ready, exact_realtime_status = lucid._lucid_exact_realtime_state(
                source_status,
                freshness_block or freshness_warning,
            )
            if freshness_warning:
                problems.append(freshness_warning)
        except Exception as e:
            source_status = "Local Lucid live bridge error"
            details = []
            exact_realtime_ready = False
            exact_realtime_status = f"{type(e).__name__}: {str(e)[:160]}"
            problems.append(f"bridge_load_error={exact_realtime_status}")
        ready = not problems and exact_realtime_ready
        return ready, {
            "source_status": source_status,
            "exact_realtime_ready": exact_realtime_ready,
            "exact_realtime_status": exact_realtime_status,
            "problems": problems,
            "feed_details": details,
        }


STORE = BridgeStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "LucidBridgeReceiver/1.0"

    def _auth_ok(self) -> bool:
        token = os.getenv("LUCID_BRIDGE_TOKEN", "").strip()
        if not token:
            return True
        return self.headers.get("X-Lucid-Bridge-Token", "") == token

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _payload(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/health", "/"}:
            self._send(200, STORE.status())
            return
        if path == "/ready":
            status = STORE.status(include_ready=True)
            self._send(200 if status.get("ready") else 503, status)
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            self._send(401, {"ok": False, "error": "bad token"})
            return
        path = urlparse(self.path).path
        try:
            payload = self._payload()
            market = str(payload.get("market") or "").lower()
            if path == "/bar":
                self._send(200, STORE.put_bar(market, payload))
            elif path == "/tick":
                self._send(200, STORE.put_tick(market, payload))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send(400, {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})

    def log_message(self, fmt: str, *args) -> None:
        if os.getenv("LUCID_BRIDGE_QUIET", "0") != "1":
            super().log_message(fmt, *args)


def main() -> int:
    host = os.getenv("LUCID_BRIDGE_HOST", "127.0.0.1")
    port = int(os.getenv("LUCID_BRIDGE_PORT", "8765"))
    print(f"Lucid bridge receiver listening on http://{host}:{port}")
    print(f"Writing CSV files under {os.getenv('LUCID_LOCAL_BRIDGE_DIR', str(ROOT / 'data'))}")
    print(f"Set LUCID_LIVE_SOURCE=local_bridge and LUCID_LOCAL_BRIDGE_SOURCE_FAMILY={lucid.BACKTEST_FEED_FAMILY}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
