"""
lucid_pass_paper.py - Lucid 50K monthly pass basket paper bot.

This is the Lucid 50K five-strategy futures basket implemented as a live paper bot:

  * ES 3m VWAP fade at 2.5 sigma
  * NQ 3m VWAP fade at 2.5 sigma
  * CL 5m VWAP fade at 2.5 sigma
  * NQ 30m Turtle Soup, 10-session lookback
  * CL 30m NR7 breakout

Account model: $50k start, +$3k pass target, $2k max-loss guard, $1.2k daily
loss guard, and $200 planned risk per trade. Paper sizing uses the same exact
R-unit sizing as the backtest; displayed micro quantities are virtual.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import lzma
import math
import os
import random
import socket
import string
import struct
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import aiohttp
import numpy as np
import pandas as pd
import requests

import config


NY = ZoneInfo("America/New_York")
TBILISI = ZoneInfo("Asia/Tbilisi")

NAME = "Lucid 50K Monthly Pass Basket (paper)"
STRATEGY_VERSION = "lucid_5basket_r200_realtime_guard_v18"
START_BALANCE = 50_000.0
TARGET_BALANCE = 53_000.0
MAX_DRAWDOWN = 2_000.0
LOCK_PEAK = 9_999_999.0
FLOOR_LOCK = START_BALANCE - MAX_DRAWDOWN
DAILY_LOSS_LIMIT = 1_200.0
RISK_USD = 200.0
MAX_MICROS = None

POLL_SEC = 2.0
# The winning backtest was built from cached 1m data fetched for UTC hours
# 13:00-20:59. Keep the live bot on that same clock, then group by NY date.
BACKTEST_SESSION_START_UTC = 13 * 60
BACKTEST_SESSION_END_UTC = 21 * 60
COMMISSION_RT = 0.50
SLIP_TICKS = 0.0

VWAP_K = 2.5
VWAP_MIN_BARS = 15
VWAP_WARN_SIGMA = 0.20
TURTLE_LOOKBACK = 10
TURTLE_RECENCY = 4
TURTLE_BUF_TICKS = 8
EIGHTY_BUF_TICKS = 10
NR7_WARN_TICKS = 8
WARNING_DEDUP_SECONDS = 10 * 60
ENTRY_BAR_LAG_GRACE_SEC = 45
_GLOBAL_WARNING_ALERTS: dict[str, float] = {}

# symbol -> (micro $/point, tick size, micro label)
MARKETS = {
    "ES=F": (5.0, 0.25, "MES"),
    "NQ=F": (2.0, 0.25, "MNQ"),
    "CL=F": (100.0, 0.01, "MCL"),
}

COMPONENTS = {
    "ES_VWAP3": {
        "symbol": "ES=F",
        "label": "MES",
        "name": "ES 3m VWAP Fade 2.5s",
        "kind": "vwap",
        "interval": "1m",
        "range": "8d",
        "resample": "3min",
        "bar_sec": 3 * 60,
    },
    "NQ_VWAP3": {
        "symbol": "NQ=F",
        "label": "MNQ",
        "name": "NQ 3m VWAP Fade 2.5s",
        "kind": "vwap",
        "interval": "1m",
        "range": "8d",
        "resample": "3min",
        "bar_sec": 3 * 60,
    },
    "CL_VWAP5": {
        "symbol": "CL=F",
        "label": "MCL",
        "name": "CL 5m VWAP Fade 2.5s",
        "kind": "vwap",
        "interval": "5m",
        "tv_interval": "1m",
        "range": "60d",
        "resample": "5min",
        "bar_sec": 5 * 60,
    },
    "NQ_TURTLE30": {
        "symbol": "NQ=F",
        "label": "MNQ",
        "name": "NQ 30m Turtle Soup 10",
        "kind": "turtle",
        "interval": "30m",
        "tv_interval": "1m",
        "range": "60d",
        "resample": "30min",
        "bar_sec": 30 * 60,
    },
    "CL_NR7_30": {
        "symbol": "CL=F",
        "label": "MCL",
        "name": "CL 30m NR7 Breakout",
        "kind": "nr7",
        "interval": "30m",
        "tv_interval": "1m",
        "range": "60d",
        "resample": "30min",
        "bar_sec": 30 * 60,
    },
}

TV_WS_URL = "wss://data.tradingview.com/socket.io/websocket?from=chart%2F&date=2026_07_02-00_00"
TV_STALE_SECONDS = 20
BACKTEST_FEED_FAMILY = "dukascopy_tick_proxy"
BACKTEST_SOURCE_SYMBOLS = {
    "ES=F": "USA500IDXUSD",
    "NQ=F": "USATECHIDXUSD",
    "CL=F": "LIGHTCMDUSD",
}
LIVE_TRADINGVIEW_FEED_FAMILY = "tradingview_cme_continuous"
DUKASCOPY_FEED_STATUS = "Dukascopy exact-source polling"
LOCAL_BRIDGE_FEED_STATUS = "Local Lucid live bridge"
DUKA_INSTRUMENTS = {
    "ES=F": ("es", "USA500IDXUSD"),
    "NQ=F": ("nq", "USATECHIDXUSD"),
    "CL=F": ("cl", "LIGHTCMDUSD"),
}
DUKA_HOURS = range(13, 21)
DUKA_DIV = 1000.0
DUKA_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research", "ta_strat", "cache")
DUKA_LIVE_CACHE_PREFIX = "lucid_dukascopy_live_"
_DUKA_LOCK = threading.RLock()
_DUKA_RAW_CACHE: dict[str, pd.DataFrame] = {}
_DUKA_FETCHED_HOURS: set[tuple[str, int, int, int, int]] = set()
_DUKA_CONFIRMED_EMPTY_HOURS: set[tuple[str, int, int, int, int]] = set()
_DUKA_EMPTY_HOUR_MISSES: dict[tuple[str, int, int, int, int], int] = {}
_DUKA_EMPTY_HOURS_LOADED = False
_DUKA_LOCAL = threading.local()
_LOCAL_BRIDGE_SEED_CACHE: dict[str, pd.DataFrame] = {}
TV_SYMBOLS = {
    "ES=F": "CME_MINI:ES1!",
    "NQ=F": "CME_MINI:NQ1!",
    "CL=F": "NYMEX:CL1!",
}
TV_COMPONENT_BARS = {
    "ES_VWAP3": 3200,
    "NQ_VWAP3": 3200,
    "CL_VWAP5": 5000,
    "NQ_TURTLE30": 40000,
    "CL_NR7_30": 40000,
}
TV_USE_QUOTES_FOR_STRATEGY_CANDLES = False
MIN_PRIOR_SESSIONS = {
    "NQ_TURTLE30": TURTLE_LOOKBACK + TURTLE_RECENCY,
    "CL_NR7_30": 7,
}


def _lucid_strategy_fingerprint() -> str:
    payload = {
        "version": STRATEGY_VERSION,
        "components": COMPONENTS,
        "markets": MARKETS,
        "session_utc": [BACKTEST_SESSION_START_UTC, BACKTEST_SESSION_END_UTC],
        "account": {
            "start": START_BALANCE,
            "target": TARGET_BALANCE,
            "max_drawdown": MAX_DRAWDOWN,
            "floor_lock": FLOOR_LOCK,
            "daily_loss": DAILY_LOSS_LIMIT,
            "risk_usd": RISK_USD,
            "max_micros": MAX_MICROS,
            "commission_rt": COMMISSION_RT,
            "slip_ticks": SLIP_TICKS,
        },
        "params": {
            "vwap_k": VWAP_K,
            "vwap_min_bars": VWAP_MIN_BARS,
            "vwap_warn_sigma": VWAP_WARN_SIGMA,
            "turtle_lookback": TURTLE_LOOKBACK,
            "turtle_recency": TURTLE_RECENCY,
            "turtle_buf_ticks": TURTLE_BUF_TICKS,
            "eighty_buf_ticks": EIGHTY_BUF_TICKS,
            "nr7_warn_ticks": NR7_WARN_TICKS,
            "entry_bar_lag_grace_sec": ENTRY_BAR_LAG_GRACE_SEC,
            "min_prior_sessions": MIN_PRIOR_SESSIONS,
        },
        "backtest_feed_family": BACKTEST_FEED_FAMILY,
        "backtest_source_symbols": BACKTEST_SOURCE_SYMBOLS,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


STRATEGY_FINGERPRINT = _lucid_strategy_fingerprint()


@dataclass
class LucidPos:
    id: str
    key: str
    symbol: str
    label: str
    strat: str
    side: str
    qty: float
    qty0: float
    entry: float
    stop: float
    stop0: float
    tp1: float
    target: float
    r_points: float
    micro_pv: float
    tick: float
    risk_usd: float
    cost_usd: float
    opened_at: float
    opened_bar: int
    best: float
    last_managed_bar: int = 0
    last_close: float = 0.0
    last_day: str = ""
    partial_done: bool = False
    realized: float = 0.0
    note: str = ""


def _minute(ts: pd.Timestamp) -> int:
    return int(ts.hour) * 60 + int(ts.minute)


def _epoch_seconds(values) -> np.ndarray:
    dt = pd.to_datetime(values, utc=True)
    raw = np.asarray(dt.astype("int64"), dtype=np.int64)
    max_abs = int(np.nanmax(np.abs(raw))) if raw.size else 0
    if max_abs > 10**17:      # nanoseconds
        raw = raw // 1_000_000_000
    elif max_abs > 10**14:    # microseconds
        raw = raw // 1_000_000
    elif max_abs > 10**11:    # milliseconds
        raw = raw // 1_000
    return raw.astype(np.int64, copy=False)


def _frame_ts_values(d: pd.DataFrame) -> np.ndarray:
    if "_ts" in d.columns:
        return d["_ts"].to_numpy(dtype=np.int64, copy=False)
    return _epoch_seconds(d["dt_utc"])


def _row_ts(row: pd.Series) -> int:
    try:
        return int(row["_ts"])
    except Exception:
        return int(pd.Timestamp(row["dt_utc"]).timestamp())


def _utc_minute(ts: pd.Timestamp) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return int(t.hour) * 60 + int(t.minute)


def _in_backtest_session_utc(ts: pd.Timestamp) -> bool:
    m = _utc_minute(ts)
    return BACKTEST_SESSION_START_UTC <= m < BACKTEST_SESSION_END_UTC


def _drop_incomplete_tail(d: pd.DataFrame, bar_sec: int, now: pd.Timestamp | None = None) -> pd.DataFrame:
    if d.empty:
        return d
    cur = now if now is not None else pd.Timestamp.now(tz="UTC")
    complete = d["dt_utc"] + pd.Timedelta(seconds=bar_sec) <= cur
    if not bool(complete.all()):
        return d[complete].copy()
    return d


def _component_flat_utc_min(key: str) -> int:
    bar_sec = int(COMPONENTS[key]["bar_sec"])
    return BACKTEST_SESSION_END_UTC - max(1, bar_sec // 60)


def _before_component_flat_utc(key: str, ts: pd.Timestamp) -> bool:
    return _utc_minute(ts) < _component_flat_utc_min(key)


def _entry_clock_ok(cur: pd.Series | None, key: str, now_utc: pd.Timestamp) -> bool:
    if now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize("UTC")
    else:
        now_utc = now_utc.tz_convert("UTC")
    now_ny = now_utc.tz_convert(NY)
    if now_ny.weekday() >= 5:
        return False
    if cur is None:
        return _in_backtest_session_utc(now_utc)
    try:
        bar_day = pd.Timestamp(cur["dt_ny"]).date()
        bar_dt_utc = pd.Timestamp(cur["dt_utc"])
        if bar_dt_utc.tzinfo is None:
            bar_dt_utc = bar_dt_utc.tz_localize("UTC")
        else:
            bar_dt_utc = bar_dt_utc.tz_convert("UTC")
    except Exception:
        return False
    if bar_day != now_ny.date():
        return False
    if not _in_backtest_session_utc(bar_dt_utc):
        return False
    if _in_backtest_session_utc(now_utc):
        return True
    if not key:
        return False
    bar_sec = int(COMPONENTS[key]["bar_sec"])
    age = (now_utc - bar_dt_utc).total_seconds()
    return 0 <= age <= bar_sec + ENTRY_BAR_LAG_GRACE_SEC


def _fetch_yahoo(symbol: str, interval: str, rng: str) -> pd.DataFrame:
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": rng, "interval": interval, "includePrePost": "false"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    r.raise_for_status()
    raw = r.json()["chart"]["result"][0]
    ts = raw.get("timestamp") or []
    q = raw["indicators"]["quote"][0]
    vol = q.get("volume") or [0] * len(ts)
    df = pd.DataFrame({
        "dt_utc": pd.to_datetime(ts, unit="s", utc=True),
        "open": q.get("open"),
        "high": q.get("high"),
        "low": q.get("low"),
        "close": q.get("close"),
        "volume": vol,
    }).dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df["dt_ny"] = df["dt_utc"].dt.tz_convert(NY)
    return df.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def _prepare(df: pd.DataFrame, bar_sec: int, drop_incomplete: bool = True) -> pd.DataFrame:
    d = df.copy()
    if d.empty:
        if "day" not in d.columns:
            d["day"] = pd.Series(dtype=object)
        if "_ts" not in d.columns:
            d["_ts"] = pd.Series(dtype="int64")
        return d
    d["dt_utc"] = pd.to_datetime(d["dt_utc"], utc=True)
    if "dt_ny" not in d.columns:
        d["dt_ny"] = d["dt_utc"].dt.tz_convert(NY)
    else:
        d["dt_ny"] = pd.to_datetime(d["dt_ny"], utc=True).dt.tz_convert(NY)
    utc_mins = d["dt_utc"].map(_utc_minute)
    d = d[(utc_mins >= BACKTEST_SESSION_START_UTC) & (utc_mins < BACKTEST_SESSION_END_UTC)].copy()
    d["day"] = d["dt_ny"].dt.date
    d = d.reset_index(drop=True)
    if drop_incomplete:
        d = _drop_incomplete_tail(d, bar_sec)
    d["_ts"] = _epoch_seconds(d["dt_utc"])
    return d


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df
    base = df.set_index("dt_utc")[["open", "high", "low", "close", "volume"]].sort_index()
    out = base.resample(rule, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"])
    out["dt_utc"] = out.index
    out = out.reset_index(drop=True)
    out["dt_ny"] = out["dt_utc"].dt.tz_convert(NY)
    return out


def _load_component_data(key: str) -> pd.DataFrame:
    c = COMPONENTS[key]
    raw = _fetch_yahoo(c["symbol"], c["interval"], c["range"])
    if c.get("resample"):
        raw = _resample(raw, c["resample"])
    return _prepare(raw, int(c["bar_sec"]))


async def _load_all_component_data() -> dict[str, pd.DataFrame]:
    loop = asyncio.get_running_loop()
    keys = list(COMPONENTS)
    frames = await asyncio.gather(*[
        loop.run_in_executor(None, _load_component_data, key) for key in keys
    ])
    return {k: f for k, f in zip(keys, frames)}


def _duka_session() -> requests.Session:
    if not hasattr(_DUKA_LOCAL, "session"):
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0"})
        _DUKA_LOCAL.session = sess
    return _DUKA_LOCAL.session


def _duka_hour_key(inst: str, hour: pd.Timestamp) -> tuple[str, int, int, int, int]:
    hour = pd.Timestamp(hour)
    if hour.tzinfo is None:
        hour = hour.tz_localize("UTC")
    else:
        hour = hour.tz_convert("UTC")
    return (inst, int(hour.year), int(hour.month) - 1, int(hour.day), int(hour.hour))


def _duka_ticks(inst: str, hour: pd.Timestamp) -> list[tuple[int, float, float, float]] | None:
    inst, y, m0, d, h = _duka_hour_key(inst, hour)
    url = f"https://datafeed.dukascopy.com/datafeed/{inst}/{y}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5"
    for attempt in range(4):
        try:
            r = _duka_session().get(url, timeout=18)
            status = int(getattr(r, "status_code", 200) or 0)
            if status in (404, 410):
                return []
            if status == 204:
                return []
            if status and status != 200:
                time.sleep(0.45 * (attempt + 1))
                continue
            if not r.content:
                if attempt < 2:
                    time.sleep(0.15)
                    continue
                return []
            raw = lzma.decompress(r.content, format=lzma.FORMAT_ALONE)
            base = int(pd.Timestamp(year=y, month=m0 + 1, day=d, hour=h, tz="UTC").timestamp() * 1000)
            out = []
            for i in range(0, len(raw), 20):
                ms, ask, bid, av, bv = struct.unpack(">IIIff", raw[i:i + 20])
                out.append((base + ms, ask, bid, av + bv))
            return out
        except lzma.LZMAError:
            time.sleep(0.35 * (attempt + 1))
        except Exception:
            time.sleep(0.45 * (attempt + 1))
    return None


def _duka_rows_to_1m(rows: list[tuple[int, float, float, float]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"])
    df = pd.DataFrame(rows, columns=["ms", "ask", "bid", "vol"]).drop_duplicates("ms").sort_values("ms")
    df["price"] = (df["ask"] + df["bid"]) / 2.0 / DUKA_DIV
    df["dt_utc"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    bars = df.set_index("dt_utc").resample("1min").agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("vol", "sum"),
    ).dropna(subset=["open", "high", "low", "close"]).reset_index()
    bars["dt_ny"] = bars["dt_utc"].dt.tz_convert(NY)
    return bars[["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"]]


def _duka_load_seed(market: str) -> pd.DataFrame:
    path = os.path.join(DUKA_CACHE_DIR, f"{market}_1m_3y.csv")
    if not os.path.exists(path):
        return pd.DataFrame(columns=["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"])
    df = pd.read_csv(path)
    df["dt_utc"] = pd.to_datetime(df["dt_utc"], utc=True)
    if "dt_ny" not in df.columns:
        df["dt_ny"] = df["dt_utc"].dt.tz_convert(NY)
    else:
        df["dt_ny"] = pd.to_datetime(df["dt_ny"], utc=True).dt.tz_convert(NY)
    return df[["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"]].drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def _duka_live_cache_path(market: str) -> str:
    return os.path.join(config.DATA_DIR, f"{DUKA_LIVE_CACHE_PREFIX}{market}_1m.csv")


def _duka_empty_hours_path() -> str:
    return os.path.join(config.DATA_DIR, f"{DUKA_LIVE_CACHE_PREFIX}confirmed_empty_hours.json")


def _duka_hour_key_str(hk: tuple[str, int, int, int, int]) -> str:
    return "|".join(str(x) for x in hk)


def _duka_hour_key_from_str(s: str) -> tuple[str, int, int, int, int] | None:
    try:
        inst, y, m0, d, h = str(s).split("|", 4)
        return (inst, int(y), int(m0), int(d), int(h))
    except Exception:
        return None


def _duka_load_confirmed_empty_hours() -> set[tuple[str, int, int, int, int]]:
    path = _duka_empty_hours_path()
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        out = set()
        for item in raw if isinstance(raw, list) else []:
            hk = _duka_hour_key_from_str(str(item))
            if hk is not None:
                out.add(hk)
        return out
    except Exception:
        return set()


def _duka_save_confirmed_empty_hours() -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        path = _duka_empty_hours_path()
        tmp = path + ".tmp"
        rows = sorted(_duka_hour_key_str(hk) for hk in _DUKA_CONFIRMED_EMPTY_HOURS)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows[-5000:], f)
        os.replace(tmp, path)
    except Exception:
        pass


def _duka_ensure_empty_hours_loaded() -> None:
    global _DUKA_EMPTY_HOURS_LOADED
    if _DUKA_EMPTY_HOURS_LOADED:
        return
    confirmed = _duka_load_confirmed_empty_hours()
    _DUKA_CONFIRMED_EMPTY_HOURS.update(confirmed)
    _DUKA_FETCHED_HOURS.update(confirmed)
    _DUKA_EMPTY_HOURS_LOADED = True


def _duka_empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"])


def _duka_normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _duka_empty_frame()
    out = df.copy()
    out["dt_utc"] = pd.to_datetime(out["dt_utc"], utc=True)
    if "dt_ny" not in out.columns:
        out["dt_ny"] = out["dt_utc"].dt.tz_convert(NY)
    else:
        out["dt_ny"] = pd.to_datetime(out["dt_ny"], utc=True).dt.tz_convert(NY)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["dt_utc", "open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    return out[["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"]].drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def _duka_load_live_cache(market: str) -> pd.DataFrame:
    path = _duka_live_cache_path(market)
    if not os.path.exists(path):
        return _duka_empty_frame()
    try:
        return _duka_normalize_frame(pd.read_csv(path))
    except Exception:
        return _duka_empty_frame()


def _duka_save_live_cache(market: str, df: pd.DataFrame, seed_last_ts: pd.Timestamp | None = None) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        out = _duka_normalize_frame(df)
        if seed_last_ts is not None and not out.empty:
            seed_last_ts = pd.Timestamp(seed_last_ts)
            if seed_last_ts.tzinfo is None:
                seed_last_ts = seed_last_ts.tz_localize("UTC")
            else:
                seed_last_ts = seed_last_ts.tz_convert("UTC")
            out = out[out["dt_utc"] > seed_last_ts].reset_index(drop=True)
        path = _duka_live_cache_path(market)
        tmp = path + ".tmp"
        out.to_csv(tmp, index=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _duka_load_seed_with_live_cache(market: str) -> pd.DataFrame:
    seed = _duka_load_seed(market)
    live_cache = _duka_load_live_cache(market)
    if live_cache.empty:
        return seed
    return _duka_normalize_frame(pd.concat([seed, live_cache], ignore_index=True))


def _duka_hours_to_fetch(last_ts: pd.Timestamp | None, now_utc: pd.Timestamp) -> list[pd.Timestamp]:
    now_utc = pd.Timestamp(now_utc).tz_convert("UTC")
    catchup_days = int(getattr(config, "LUCID_DUKASCOPY_CATCHUP_DAYS", 30))
    start = now_utc - pd.Timedelta(days=catchup_days)
    if last_ts is not None:
        last_ts = pd.Timestamp(last_ts)
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        else:
            last_ts = last_ts.tz_convert("UTC")
        start = max(start, last_ts.floor("h") - pd.Timedelta(hours=1))
    end = now_utc.floor("h")
    hours = []
    for hour in pd.date_range(start.floor("h"), end, freq="h", tz="UTC"):
        if hour.weekday() < 5 and int(hour.hour) in DUKA_HOURS:
            hours.append(hour)
    return hours


def _duka_missing_session_hours(base: pd.DataFrame, now_utc: pd.Timestamp) -> list[pd.Timestamp]:
    if base is None or base.empty:
        return []
    now_utc = pd.Timestamp(now_utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize("UTC")
    else:
        now_utc = now_utc.tz_convert("UTC")
    catchup_days = int(getattr(config, "LUCID_DUKASCOPY_CATCHUP_DAYS", 30))
    start = max(
        pd.Timestamp(base.iloc[0]["dt_utc"]).tz_convert("UTC").floor("h"),
        (now_utc - pd.Timedelta(days=catchup_days)).floor("h"),
    )
    end = now_utc.floor("h")
    if end < start:
        return []
    present = set()
    dt = pd.to_datetime(base["dt_utc"], utc=True)
    for hour in dt.dt.floor("h").drop_duplicates():
        h = pd.Timestamp(hour).tz_convert("UTC")
        if h.weekday() < 5 and int(h.hour) in DUKA_HOURS:
            present.add(h)
    missing = []
    for hour in pd.date_range(start, end, freq="h", tz="UTC"):
        if hour.weekday() < 5 and int(hour.hour) in DUKA_HOURS and hour not in present:
            missing.append(hour)
    return missing


def _duka_update_hours(last_ts: pd.Timestamp | None, now_utc: pd.Timestamp,
                       base: pd.DataFrame, inst: str | None = None) -> list[pd.Timestamp]:
    recent_hours = sorted(set(_duka_hours_to_fetch(last_ts, now_utc)))
    recent_set = set(recent_hours)
    missing_hours = [h for h in _duka_missing_session_hours(base, now_utc) if h not in recent_set]
    if inst:
        missing_hours = [
            h for h in missing_hours
            if _duka_hour_key(inst, h) not in _DUKA_FETCHED_HOURS
        ]
    repair_limit = max(0, int(getattr(config, "LUCID_DUKASCOPY_MISSING_REPAIR_HOURS_PER_POLL", 2)))
    if repair_limit:
        missing_hours = missing_hours[:repair_limit]
    else:
        missing_hours = []
    return sorted(recent_set | set(missing_hours))


def _duka_update_market(symbol: str, now_utc: pd.Timestamp) -> pd.DataFrame:
    market, inst = DUKA_INSTRUMENTS[symbol]
    with _DUKA_LOCK:
        _duka_ensure_empty_hours_loaded()
        if market not in _DUKA_RAW_CACHE:
            _DUKA_RAW_CACHE[market] = _duka_load_seed_with_live_cache(market)
        base = _DUKA_RAW_CACHE[market]
        last_ts = pd.Timestamp(base.iloc[-1]["dt_utc"]) if not base.empty else None
    hours = _duka_update_hours(last_ts, now_utc, base, inst)
    if not hours:
        return base.copy()

    now_floor = pd.Timestamp(now_utc).tz_convert("UTC").floor("h")
    jobs = []
    with _DUKA_LOCK:
        for hour in hours:
            hk = _duka_hour_key(inst, hour)
            is_recent = hour >= now_floor - pd.Timedelta(hours=2)
            if is_recent or hk not in _DUKA_FETCHED_HOURS:
                jobs.append(hour)
    if not jobs:
        with _DUKA_LOCK:
            return _DUKA_RAW_CACHE[market].copy()

    rows_by_hour: list[tuple[pd.Timestamp, list[tuple[int, float, float, float]]]] = []
    max_workers = min(6, max(1, len(jobs)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for hour, rows in zip(jobs, ex.map(lambda h: _duka_ticks(inst, h), jobs)):
            rows_by_hour.append((hour, rows))

    new_frames = []
    persist_empty_hours = False
    with _DUKA_LOCK:
        for hour, rows in rows_by_hour:
            hk = _duka_hour_key(inst, hour)
            if rows is None:
                continue
            if rows:
                new_frames.append(_duka_rows_to_1m(rows))
                _DUKA_EMPTY_HOUR_MISSES.pop(hk, None)
                _DUKA_CONFIRMED_EMPTY_HOURS.discard(hk)
                _DUKA_FETCHED_HOURS.add(hk)
            elif hour < now_floor - pd.Timedelta(hours=2):
                misses = _DUKA_EMPTY_HOUR_MISSES.get(hk, 0) + 1
                _DUKA_EMPTY_HOUR_MISSES[hk] = misses
                if misses >= 3:
                    _DUKA_FETCHED_HOURS.add(hk)
                    if hour < now_floor - pd.Timedelta(days=1):
                        _DUKA_CONFIRMED_EMPTY_HOURS.add(hk)
                        persist_empty_hours = True
        if new_frames:
            merged = pd.concat([_DUKA_RAW_CACHE[market], *new_frames], ignore_index=True)
            merged = merged.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)
            _DUKA_RAW_CACHE[market] = _duka_normalize_frame(merged)
            seed = _duka_load_seed(market)
            seed_last = pd.Timestamp(seed.iloc[-1]["dt_utc"]) if not seed.empty else None
            _duka_save_live_cache(market, _DUKA_RAW_CACHE[market], seed_last)
        if persist_empty_hours:
            _duka_save_confirmed_empty_hours()
        return _DUKA_RAW_CACHE[market].copy()


def _load_dukascopy_component_data_all() -> tuple[dict[str, pd.DataFrame], str]:
    now_utc = pd.Timestamp.now(tz="UTC")
    raw_by_symbol = {
        symbol: _duka_update_market(symbol, now_utc)
        for symbol in DUKA_INSTRUMENTS
    }
    raw_by_key = {
        key: raw_by_symbol[COMPONENTS[key]["symbol"]]
        for key in COMPONENTS
    }
    frames = _build_component_frames_from_raw(raw_by_key, drop_incomplete=True)
    latest = []
    for key, d in frames.items():
        if not d.empty:
            close_ts = pd.Timestamp(d.iloc[-1]["dt_utc"]) + pd.Timedelta(seconds=int(COMPONENTS[key]["bar_sec"]))
            latest.append(f"{key} {close_ts.strftime('%H:%M')}")
    suffix = "; ".join(latest[:3])
    status = DUKASCOPY_FEED_STATUS + (f" ({suffix})" if suffix else " (waiting for candles)")
    return frames, status


def _local_bridge_path(market: str) -> str:
    directory = str(getattr(config, "LUCID_LOCAL_BRIDGE_DIR", config.DATA_DIR) or config.DATA_DIR)
    prefix = str(getattr(config, "LUCID_LOCAL_BRIDGE_PREFIX", "lucid_live_bridge_") or "lucid_live_bridge_")
    return os.path.join(directory, f"{prefix}{market}_1m.csv")


def _local_bridge_missing_markets() -> list[str]:
    return [
        market for market in ("es", "nq", "cl")
        if not os.path.exists(_local_bridge_path(market))
    ]


def _local_bridge_invalid_markets() -> list[str]:
    invalid: list[str] = []
    raw_max_future = getattr(config, "LUCID_BRIDGE_MAX_FUTURE_SEC", 120)
    max_future = float(120 if raw_max_future is None else raw_max_future)
    now_utc = pd.Timestamp.now(tz="UTC")
    required = ["dt_utc", "open", "high", "low", "close", "volume"]
    for market in ("es", "nq", "cl"):
        path = _local_bridge_path(market)
        if not os.path.exists(path):
            continue
        try:
            raw = pd.read_csv(path)
            if raw.empty or any(col not in raw.columns for col in required):
                invalid.append(market)
                continue
            dt = pd.to_datetime(raw["dt_utc"], utc=True, errors="coerce")
            nums = {
                col: pd.to_numeric(raw[col], errors="coerce")
                for col in ("open", "high", "low", "close", "volume")
            }
            num_df = pd.DataFrame(nums)
            bad = dt.isna()
            for col in ("open", "high", "low", "close", "volume"):
                bad = bad | num_df[col].isna() | ~np.isfinite(num_df[col])
            for col in ("open", "high", "low", "close"):
                bad = bad | (num_df[col] <= 0)
            bad = (
                bad
                | (num_df["volume"] < 0)
                | (num_df["high"] < num_df[["open", "close"]].max(axis=1))
                | (num_df["low"] > num_df[["open", "close"]].min(axis=1))
                | (num_df["high"] < num_df["low"])
            )
            if max_future >= 0:
                bad = bad | ((dt - now_utc).dt.total_seconds() > max_future)
            if bool(bad.any()):
                invalid.append(market)
        except Exception:
            invalid.append(market)
    return invalid


def _local_bridge_seed(market: str) -> pd.DataFrame:
    with _DUKA_LOCK:
        if market not in _LOCAL_BRIDGE_SEED_CACHE:
            _LOCAL_BRIDGE_SEED_CACHE[market] = _duka_load_seed(market)
        return _LOCAL_BRIDGE_SEED_CACHE[market].copy()


def _load_local_bridge_market(market: str, invalid: set[str] | None = None) -> pd.DataFrame:
    seed = _local_bridge_seed(market)
    if invalid and market in invalid:
        return seed
    path = _local_bridge_path(market)
    if not os.path.exists(path):
        return seed
    bridge = _duka_normalize_frame(pd.read_csv(path))
    if bridge.empty:
        return seed
    merged = pd.concat([seed, bridge], ignore_index=True)
    merged["dt_utc"] = pd.to_datetime(merged["dt_utc"], utc=True)
    merged = merged.drop_duplicates("dt_utc", keep="last").sort_values("dt_utc").reset_index(drop=True)
    return _duka_normalize_frame(merged)


def _load_local_bridge_component_data_all() -> tuple[dict[str, pd.DataFrame], str]:
    missing = _local_bridge_missing_markets()
    invalid = _local_bridge_invalid_markets()
    invalid_set = set(invalid)
    raw_by_symbol = {
        symbol: _load_local_bridge_market(market, invalid_set)
        for symbol, (market, _inst) in DUKA_INSTRUMENTS.items()
    }
    raw_by_key = {
        key: raw_by_symbol[COMPONENTS[key]["symbol"]]
        for key in COMPONENTS
    }
    frames = _build_component_frames_from_raw(raw_by_key, drop_incomplete=True)
    latest = []
    for key, d in frames.items():
        if not d.empty:
            close_ts = pd.Timestamp(d.iloc[-1]["dt_utc"]) + pd.Timedelta(seconds=int(COMPONENTS[key]["bar_sec"]))
            latest.append(f"{key} {close_ts.strftime('%H:%M')}")
    suffix = "; ".join(latest[:3]) or "waiting for candles"
    family = str(getattr(config, "LUCID_LOCAL_BRIDGE_SOURCE_FAMILY", "") or "unverified_source").strip()
    parts = [family, suffix]
    if missing:
        parts.append("missing_files=" + ",".join(missing))
    if invalid:
        parts.append("invalid_files=" + ",".join(invalid))
    return frames, f"{LOCAL_BRIDGE_FEED_STATUS} ({'; '.join(parts)})"


def _lucid_local_bridge_block_reason(source_status: str) -> str:
    low = str(source_status or "").lower()
    if LOCAL_BRIDGE_FEED_STATUS.lower() not in low:
        return ""
    marker = "missing_files="
    if marker not in low:
        marker = "invalid_files="
        if marker not in low:
            return ""
        invalid = low.split(marker, 1)[1].split(";", 1)[0].split(")", 1)[0].strip()
        return (
            "Local Lucid live bridge is not ready; invalid producer CSV data for "
            f"{invalid or 'es,nq,cl'}."
        )
    missing = low.split(marker, 1)[1].split(";", 1)[0].split(")", 1)[0].strip()
    return (
        "Local Lucid live bridge is not ready; missing producer CSV files for "
        f"{missing or 'es,nq,cl'}."
    )


def _lucid_exact_source_freshness_block(frames: dict[str, pd.DataFrame], now_utc: pd.Timestamp | None = None) -> str:
    details = _lucid_exact_source_freshness_details(frames, now_utc)
    stale = [
        f"{d['key']} latest closed {d['latest_closed_utc']}, lag {d['lag_sec']}s"
        for d in details
        if d.get("stale")
    ]
    active = [d for d in details if d.get("session_active")]
    if stale and active and len(stale) >= len(active):
        return (
            "Exact Dukascopy feed is stale during entry session. "
            "This exact backtest source is the public hourly Dukascopy tick-file feed; "
            "current-hour files may not be published yet: " + "; ".join(stale[:3])
        )
    return ""


def _lucid_exact_source_freshness_warning(details: list[dict]) -> str:
    stale = [
        f"{d['key']} latest closed {d['latest_closed_utc']}, lag {d['lag_sec']}s"
        for d in details
        if d.get("stale")
    ]
    if stale:
        return (
            "Some exact Dukascopy components are stale; stale components are blocked "
            "but fresh components still scan: " + "; ".join(stale[:3])
        )
    return ""


def _lucid_exact_source_freshness_details(frames: dict[str, pd.DataFrame],
                                          now_utc: pd.Timestamp | None = None) -> list[dict]:
    now_utc = pd.Timestamp.now(tz="UTC") if now_utc is None else pd.Timestamp(now_utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize("UTC")
    else:
        now_utc = now_utc.tz_convert("UTC")
    grace = int(getattr(config, "LUCID_DUKASCOPY_FEED_GRACE_SEC", 120))
    in_session = _in_backtest_session_utc(now_utc)
    details = []
    for key, c in COMPONENTS.items():
        d = frames.get(key)
        if d is None or d.empty:
            details.append({
                "key": key,
                "name": c["name"],
                "latest_closed_utc": None,
                "latest_closed_tbilisi": None,
                "lag_sec": None,
                "bar_sec": int(c["bar_sec"]),
                "session_active": bool(in_session),
                "stale": bool(in_session),
                "state": "stale" if in_session else "outside_session",
                "reason": "no candles",
            })
            continue
        bar_sec = int(c["bar_sec"])
        close_ts = pd.Timestamp(d.iloc[-1]["dt_utc"])
        if close_ts.tzinfo is None:
            close_ts = close_ts.tz_localize("UTC")
        else:
            close_ts = close_ts.tz_convert("UTC")
        close_ts = close_ts + pd.Timedelta(seconds=bar_sec)
        lag = (now_utc - close_ts).total_seconds()
        stale = bool(in_session and lag > bar_sec + grace)
        details.append({
            "key": key,
            "name": c["name"],
            "latest_closed_utc": close_ts.strftime("%H:%M UTC"),
            "latest_closed_tbilisi": close_ts.tz_convert(TBILISI).strftime("%a %H:%M"),
            "lag_sec": int(lag),
            "bar_sec": bar_sec,
            "session_active": bool(in_session),
            "stale": stale,
            "state": "outside_session" if not in_session else ("stale" if stale else "fresh"),
            "reason": "stale" if stale else "",
        })
    return details


def _lucid_session_text(now_utc: pd.Timestamp | None = None) -> str:
    now_utc = pd.Timestamp.now(tz="UTC") if now_utc is None else pd.Timestamp(now_utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize("UTC")
    else:
        now_utc = now_utc.tz_convert("UTC")
    start = now_utc.replace(
        hour=BACKTEST_SESSION_START_UTC // 60,
        minute=BACKTEST_SESSION_START_UTC % 60,
        second=0,
        microsecond=0,
    )
    end = now_utc.replace(
        hour=BACKTEST_SESSION_END_UTC // 60,
        minute=BACKTEST_SESSION_END_UTC % 60,
        second=0,
        microsecond=0,
    )
    if end <= start:
        end += pd.Timedelta(days=1)
    return f"{start.astimezone(TBILISI).strftime('%a %H:%M')}-{end.astimezone(TBILISI).strftime('%H:%M')} Tbilisi"


def _tv_frame(payload: dict) -> str:
    s = json.dumps(payload, separators=(",", ":"))
    return f"~m~{len(s)}~m~{s}"


def _tv_messages(raw: str) -> list[dict]:
    out = []
    i = 0
    while True:
        j = raw.find("~m~", i)
        if j < 0:
            break
        k = raw.find("~m~", j + 3)
        if k < 0:
            break
        try:
            n = int(raw[j + 3:k])
        except ValueError:
            i = k + 3
            continue
        s = raw[k + 3:k + 3 + n]
        try:
            out.append(json.loads(s))
        except Exception:
            pass
        i = k + 3 + n
    return out


def _tv_session(prefix: str) -> str:
    return prefix + "_" + "".join(random.choice(string.ascii_lowercase) for _ in range(12))


def _tv_interval(c: dict) -> str:
    raw = str(c.get("tv_interval") or c.get("interval", "1m")).lower().replace("min", "m")
    if raw.endswith("m"):
        return raw[:-1] or "1"
    return raw


def _tv_symbol(symbol: str) -> str:
    return TV_SYMBOLS.get(symbol, symbol)


def _tv_resolve_expr(tv_symbol: str) -> str:
    return "=" + json.dumps({
        "symbol": tv_symbol,
        "adjustment": "splits",
        "session": "extended",
    }, separators=(",", ":"))


def _tv_rows_to_frame(rows: dict[int, list]) -> pd.DataFrame:
    recs = []
    for idx in sorted(rows):
        v = rows[idx]
        if len(v) < 5:
            continue
        try:
            recs.append({
                "dt_utc": pd.to_datetime(float(v[0]), unit="s", utc=True),
                "open": float(v[1]),
                "high": float(v[2]),
                "low": float(v[3]),
                "close": float(v[4]),
                "volume": float(v[5]) if len(v) > 5 and v[5] is not None else 0.0,
            })
        except (TypeError, ValueError, OverflowError):
            continue
    if not recs:
        return pd.DataFrame(columns=["dt_utc", "open", "high", "low", "close", "volume", "dt_ny"])
    df = pd.DataFrame(recs)
    df["dt_ny"] = df["dt_utc"].dt.tz_convert(NY)
    return df.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def _tv_apply_quote(df: pd.DataFrame, interval_min: int, ts: float, px: float, volume=None) -> pd.DataFrame:
    if df.empty:
        bar_ts = int(ts // (interval_min * 60) * (interval_min * 60))
        out = pd.DataFrame([{
            "dt_utc": pd.to_datetime(bar_ts, unit="s", utc=True),
            "open": px, "high": px, "low": px, "close": px,
            "volume": float(volume or 0.0),
        }])
        out["dt_ny"] = out["dt_utc"].dt.tz_convert(NY)
        return out
    out = df.copy()
    bar_ts = int(ts // (interval_min * 60) * (interval_min * 60))
    bar_dt = pd.to_datetime(bar_ts, unit="s", utc=True)
    mask = out["dt_utc"] == bar_dt
    if mask.any():
        i = out.index[mask][-1]
        out.loc[i, "high"] = max(float(out.loc[i, "high"]), px)
        out.loc[i, "low"] = min(float(out.loc[i, "low"]), px)
        out.loc[i, "close"] = px
        if volume is not None:
            out.loc[i, "volume"] = max(float(out.loc[i, "volume"] or 0.0), float(volume or 0.0))
    elif bar_dt > out.iloc[-1]["dt_utc"]:
        row = {
            "dt_utc": bar_dt,
            "open": px, "high": px, "low": px, "close": px,
            "volume": float(volume or 0.0),
            "dt_ny": bar_dt.tz_convert(NY),
        }
        out = pd.concat([out, pd.DataFrame([row])], ignore_index=True)
    return out.drop_duplicates("dt_utc").sort_values("dt_utc").reset_index(drop=True)


def _build_component_frames_from_raw(raw_by_key: dict[str, pd.DataFrame],
                                     drop_incomplete: bool) -> dict[str, pd.DataFrame]:
    frames = {}
    for key, raw in raw_by_key.items():
        c = COMPONENTS[key]
        d = raw
        if c.get("resample"):
            d = _resample(d, c["resample"])
        frames[key] = _prepare(d, int(c["bar_sec"]), drop_incomplete=drop_incomplete)
    return frames


def _clone_component_frames(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    return {key: frame.copy(deep=True) for key, frame in frames.items()}


async def _stream_tradingview_key(key: str, queue: asyncio.Queue):
    headers = {"Origin": "https://www.tradingview.com", "User-Agent": "Mozilla/5.0"}
    auth = getattr(config, "LUCID_TRADINGVIEW_AUTH_TOKEN", "unauthorized_user_token")
    c = COMPONENTS[key]
    tv_sym = _tv_symbol(c["symbol"])
    interval_min = int(_tv_interval(c))
    while True:
        chart_session = _tv_session("cs")
        quote_session = _tv_session("qs")
        rows: dict[int, list] = {}
        raw_df = pd.DataFrame()
        last_quote_time = 0.0
        last_quote_volume = None
        last_data_time = time.time()
        stream_mode = "connecting"
        history_ready = False
        try:
            connector = aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver(),
                family=socket.AF_INET,
                ttl_dns_cache=300,
            )
            async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
                async with session.ws_connect(
                    TV_WS_URL, heartbeat=20, timeout=aiohttp.ClientTimeout(total=30)
                ) as ws:
                    async def send(m: str, p: list):
                        await ws.send_str(_tv_frame({"m": m, "p": p}))

                    await send("set_auth_token", [auth])
                    await send("chart_create_session", [chart_session, ""])
                    await send("quote_create_session", [quote_session])
                    await send("quote_set_fields", [
                        quote_session, "lp", "lp_time", "bid", "ask", "volume", "rtc",
                    ])
                    await send("resolve_symbol", [chart_session, "symbol_1", _tv_resolve_expr(tv_sym)])
                    await send("create_series", [
                        chart_session, "s1", "s1", "symbol_1",
                        _tv_interval(c), TV_COMPONENT_BARS.get(key, 1200),
                    ])
                    await send("quote_add_symbols", [quote_session, tv_sym])

                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=TV_STALE_SECONDS)
                        except asyncio.TimeoutError:
                            raise RuntimeError(f"TradingView websocket stale for {TV_STALE_SECONDS}s")
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                raise RuntimeError("TradingView websocket closed")
                            continue
                        raw = msg.data
                        if raw.startswith("~m~4~m~~h~") or "~h~" in raw:
                            try:
                                await ws.send_str(raw)
                            except Exception:
                                pass
                        changed = False
                        for packet in _tv_messages(raw):
                            m = packet.get("m")
                            p = packet.get("p") or []
                            if m == "timescale_update" and len(p) >= 2:
                                data = (p[1] or {}).get("s1") or {}
                                for row in data.get("s") or []:
                                    try:
                                        rows[int(row["i"])] = row["v"]
                                    except Exception:
                                        continue
                                if rows:
                                    raw_df = _tv_rows_to_frame(rows)
                                    if len(rows) >= 20:
                                        history_ready = True
                                    changed = True
                            elif m == "series_completed" and len(p) >= 3:
                                stream_mode = str(p[2])
                            elif m == "qsd" and len(p) >= 2:
                                q = p[1] or {}
                                vals = q.get("v") or {}
                                if q.get("n") != tv_sym:
                                    continue
                                if "lp_time" in vals:
                                    last_quote_time = float(vals["lp_time"])
                                if "volume" in vals:
                                    try:
                                        last_quote_volume = float(vals["volume"])
                                    except Exception:
                                        pass
                                px = vals.get("lp")
                                if px is None:
                                    bid, ask = vals.get("bid"), vals.get("ask")
                                    if bid is not None and ask is not None:
                                        px = (float(bid) + float(ask)) / 2.0
                                # Quotes are useful for knowing the websocket is alive, but
                                # strategy candles must come from TradingView chart-series
                                # OHLC updates. Building OHLC from quote ticks can miss highs
                                # or lows and drift away from the completed-candle backtest.
                                if (
                                    TV_USE_QUOTES_FOR_STRATEGY_CANDLES
                                    and px is not None
                                    and history_ready
                                ):
                                    ts = last_quote_time or time.time()
                                    raw_df = _tv_apply_quote(
                                        raw_df, interval_min, ts, float(px), last_quote_volume
                                    )
                                    changed = True
                        if changed and history_ready and not raw_df.empty:
                            last_data_time = time.time()
                            status = "TradingView websocket"
                            mode = stream_mode.lower()
                            if "delayed" in mode:
                                status += f" ({stream_mode})"
                            elif stream_mode == "connecting":
                                status += " (feed_mode_unknown)"
                            else:
                                status += f" ({stream_mode})"
                            await queue.put((key, raw_df, status))
                        elif history_ready and time.time() - last_data_time > TV_STALE_SECONDS:
                            raise RuntimeError(f"TradingView data stale for {TV_STALE_SECONDS}s")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await queue.put((key, None, f"{key} reconnecting: {type(e).__name__}"))
            await asyncio.sleep(2)


async def _stream_tradingview_component_data():
    queue: asyncio.Queue = asyncio.Queue()
    tasks = [asyncio.create_task(_stream_tradingview_key(key, queue)) for key in COMPONENTS]
    raw_by_key: dict[str, pd.DataFrame] = {}
    statuses: dict[str, str] = {}
    try:
        while True:
            key, raw_df, status = await queue.get()
            statuses[key] = status
            if raw_df is not None and not raw_df.empty:
                raw_by_key[key] = raw_df
            if all(k in raw_by_key for k in COMPONENTS):
                source_status = _combine_tradingview_statuses(statuses)
                yield _build_component_frames_from_raw(raw_by_key, drop_incomplete=True), source_status
    finally:
        for task in tasks:
            task.cancel()


def _combine_tradingview_statuses(statuses: dict[str, str]) -> str:
    def detail(status: str) -> str:
        text = str(status or "").strip()
        prefix = "TradingView websocket"
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            if text.startswith("(") and text.endswith(")"):
                text = text[1:-1].strip()
            return text or "realtime_streaming"
        return text

    parts = sorted({detail(s) for s in statuses.values() if detail(s)})
    source_status = "TradingView websocket"
    delayed = [p for p in parts if "delayed" in p.lower()]
    unauthorized = [
        p for p in parts
        if (
            "permission" in p.lower()
            or "denied" in p.lower()
            or "unauthorized" in p.lower()
            or "not_entitled" in p.lower()
        )
    ]
    stale = [p for p in parts if "reconnecting" in p.lower() or "stale" in p.lower()]
    unknown = [
        p for p in parts
        if ("feed_mode_unknown" in p.lower() or "connecting" in p.lower())
        and p not in stale
    ]
    unconfirmed = [
        p for p in parts
        if p not in delayed
        and p not in unauthorized
        and p not in stale
        and p not in unknown
        and not ("streaming" in p.lower() or "realtime" in p.lower())
    ]
    details = delayed or unauthorized or unknown or stale or unconfirmed
    if details:
        source_status += " (" + ", ".join(details[:2]) + ")"
    elif parts and all("streaming" in p.lower() or "realtime" in p.lower() for p in parts):
        source_status += " (realtime_streaming)"
    if stale and details is not stale:
        source_status += " | " + ", ".join(stale[:2])
    return source_status


def _lucid_feed_block_reason(source_status: str) -> str:
    low = (source_status or "").lower()
    if "delayed" in low:
        return "Realtime CME feed required; current TradingView feed is delayed."
    if "reconnecting" in low or "stale" in low:
        return "Realtime CME feed required; TradingView feed is reconnecting/stale."
    if "feed_mode_unknown" in low or "connecting" in low:
        return "Realtime CME feed required; waiting for TradingView to confirm realtime mode."
    if "permission" in low or "denied" in low or "unauthorized" in low or "not_entitled" in low:
        return "Realtime CME feed required; TradingView feed is not authorized."
    if "tradingview websocket" in low and "streaming" in low:
        return ""
    if "tradingview websocket" in low and "realtime" not in low:
        return "Realtime CME feed required; TradingView feed mode is not confirmed realtime."
    return ""


def _lucid_fallback_block_reason(require_realtime: bool) -> str:
    return "Realtime CME feed required; Yahoo fallback polling is not allowed." if require_realtime else ""


def _lucid_source_block_reason(source_status: str, require_match: bool | None = None) -> str:
    if require_match is None:
        require_match = bool(getattr(config, "LUCID_REQUIRE_BACKTEST_SOURCE_MATCH", True))
    if not require_match:
        return ""
    low = (source_status or "").lower()
    if LOCAL_BRIDGE_FEED_STATUS.lower() in low:
        family = _local_bridge_status_family(source_status)
        if family == BACKTEST_FEED_FAMILY:
            return ""
        return (
            "Exact 36/36 backtest source required; local bridge must declare "
            f"LUCID_LOCAL_BRIDGE_SOURCE_FAMILY={BACKTEST_FEED_FAMILY} and write "
            "Dukascopy USA500IDXUSD/USATECHIDXUSD/LIGHTCMDUSD 1m bars."
        )
    if low.startswith(DUKASCOPY_FEED_STATUS.lower() + " ("):
        return ""
    if "tradingview websocket" in low or "yahoo" in low or "cme" in low or "nymex" in low:
        return (
            "Exact 36/36 backtest source required; saved report used Dukascopy "
            "tick-derived proxy candles (USA500IDXUSD/USATECHIDXUSD/LIGHTCMDUSD), "
            "but the live feed is TradingView CME/NYMEX futures."
        )
    return ""


def _local_bridge_status_family(source_status: str) -> str:
    text = str(source_status or "").strip()
    prefix = LOCAL_BRIDGE_FEED_STATUS + " ("
    if not text.startswith(prefix):
        return ""
    rest = text[len(prefix):]
    return rest.split(";", 1)[0].split(")", 1)[0].strip()


def _lucid_source_verified(source_status: str, require_match: bool | None = None) -> bool:
    if require_match is None:
        require_match = bool(getattr(config, "LUCID_REQUIRE_BACKTEST_SOURCE_MATCH", True))
    if not require_match:
        return True
    low = (source_status or "").lower()
    if LOCAL_BRIDGE_FEED_STATUS.lower() in low:
        if "missing_files=" in low or "invalid_files=" in low or "error" in low:
            return False
        return _local_bridge_status_family(source_status) == BACKTEST_FEED_FAMILY
    return low.startswith(DUKASCOPY_FEED_STATUS.lower() + " (")


def _lucid_exact_realtime_state(source_status: str, data_error: str = "") -> tuple[bool, str]:
    text = str(source_status or "")
    err = str(data_error or "").strip()
    low = text.lower()
    if LOCAL_BRIDGE_FEED_STATUS.lower() in low:
        if not _lucid_source_verified(text, True):
            reason = _lucid_source_block_reason(text, True) or _lucid_local_bridge_block_reason(text)
            return False, reason or "Local exact-source bridge is not ready."
        bridge_block = _lucid_local_bridge_block_reason(text)
        if bridge_block:
            return False, bridge_block
        if err:
            return False, err[:180]
        return True, "Local Dukascopy/JForex bridge is ready."
    if _lucid_source_verified(text, True):
        return False, "Exact source matches backtest, but this is public Dukascopy polling, not the realtime local bridge."
    return False, _lucid_source_block_reason(text, True) or "Exact realtime source is not verified."


def _lucid_history_block_reason(frames: dict[str, pd.DataFrame]) -> str:
    missing = []
    for key, need in MIN_PRIOR_SESSIONS.items():
        d = frames.get(key)
        if d is None or d.empty:
            missing.append(f"{COMPONENTS[key]['name']} has no candles")
            continue
        latest_day = d.iloc[-1]["day"]
        prior = len(_daily(d[d["day"] < latest_day]))
        if prior < need:
            missing.append(f"{COMPONENTS[key]['name']} needs {need} prior sessions, has {prior}")
    if missing:
        return "Insufficient Lucid history from live feed: " + "; ".join(missing[:3])
    return ""


def _apply_feed_block(bot, reason: str, live_feed_status: str | None = None,
                      status: str = "blocked - futures feed not realtime"):
    try:
        bot._force_close_expired_positions_without_new_bars()
    except Exception:
        pass
    bot.realtime_entry_ready = False
    bot.realtime_entry_status = reason
    bot.data_error = reason
    bot.status = status
    if live_feed_status is not None:
        bot.live_feed_status = live_feed_status
    if not bot.pos:
        bot.prices = {}
    bot.setups = {}
    bot._save()


async def manage_shared_loop(bots):
    await asyncio.sleep(0.5)
    live_source = str(getattr(config, "LUCID_LIVE_SOURCE", "") or "").lower()
    if not live_source:
        live_source = "tradingview" if getattr(config, "LUCID_USE_TRADINGVIEW_LIVE", True) else "yahoo"
    if live_source == "dukascopy":
        while True:
            try:
                loop = asyncio.get_running_loop()
                shared_df, source_status = await loop.run_in_executor(None, _load_dukascopy_component_data_all)
                feed_details = _lucid_exact_source_freshness_details(shared_df)
                freshness_block = _lucid_exact_source_freshness_block(shared_df)
                freshness_warning = _lucid_exact_source_freshness_warning(feed_details)
                for bot in bots:
                    bot.live_feed_status = source_status
                    bot.feed_details = feed_details
                    bot.realtime_entry_ready = False
                    bot.realtime_entry_status = (
                        "Exact source matches backtest, but this is public Dukascopy polling, "
                        "not the realtime local bridge."
                    )
                    source_block = _lucid_source_block_reason(source_status)
                    if source_block:
                        _apply_feed_block(bot, source_block, status="blocked - live source differs from backtest")
                        continue
                    history_block = _lucid_history_block_reason(shared_df)
                    if history_block:
                        _apply_feed_block(bot, history_block, status="blocked - Lucid history not ready")
                        continue
                    bot._df = _clone_component_frames(shared_df)
                    bot.data_error = freshness_block or freshness_warning
                    bot._tick()
                    if freshness_block and not bot.pos:
                        bot.status = "blocked - exact source feed stale"
                    elif bot._requires_exact_realtime_entry() and not bot.realtime_entry_ready and not bot.pos and bot._real_entry_window_ok():
                        bot.status = "blocked - exact realtime bridge not ready"
                    bot._save()
            except Exception as e:
                err = f"Dukascopy exact-source feed error: {type(e).__name__}: {str(e)[:100]}"
                for bot in bots:
                    bot.realtime_entry_ready = False
                    bot.realtime_entry_status = err
                    bot.data_error = err
                    bot.status = "data error"
                    bot.live_feed_status = "Dukascopy exact-source polling error"
                    bot.feed_details = []
                    bot._save()
            await asyncio.sleep(float(getattr(config, "LUCID_DUKASCOPY_POLL_SEC", 30.0)))
    elif live_source in {"local_bridge", "bridge", "localcsv"}:
        while True:
            try:
                loop = asyncio.get_running_loop()
                shared_df, source_status = await loop.run_in_executor(None, _load_local_bridge_component_data_all)
                feed_details = _lucid_exact_source_freshness_details(shared_df)
                freshness_block = _lucid_exact_source_freshness_block(shared_df)
                freshness_warning = _lucid_exact_source_freshness_warning(feed_details)
                for bot in bots:
                    bot.live_feed_status = source_status
                    bot.feed_details = feed_details
                    bridge_block = _lucid_local_bridge_block_reason(source_status)
                    if bridge_block:
                        _apply_feed_block(bot, bridge_block, status="blocked - local bridge not ready")
                        continue
                    source_block = _lucid_source_block_reason(source_status)
                    if source_block:
                        _apply_feed_block(bot, source_block, status="blocked - live source differs from backtest")
                        continue
                    history_block = _lucid_history_block_reason(shared_df)
                    if history_block:
                        _apply_feed_block(bot, history_block, status="blocked - Lucid history not ready")
                        continue
                    bot._df = _clone_component_frames(shared_df)
                    bot.data_error = freshness_block or freshness_warning
                    entry_ready, entry_status = _lucid_exact_realtime_state(source_status, bot.data_error)
                    bot.realtime_entry_ready = entry_ready
                    bot.realtime_entry_status = entry_status
                    bot._tick()
                    if freshness_block and not bot.pos:
                        bot.status = "blocked - exact source feed stale"
                    elif bot._requires_exact_realtime_entry() and not bot.realtime_entry_ready and not bot.pos and bot._real_entry_window_ok():
                        bot.status = "blocked - exact realtime bridge not ready"
                    bot._save()
            except Exception as e:
                err = f"Local Lucid live bridge error: {type(e).__name__}: {str(e)[:100]}"
                for bot in bots:
                    bot.realtime_entry_ready = False
                    bot.realtime_entry_status = err
                    bot.data_error = err
                    bot.status = "data error"
                    bot.live_feed_status = "Local Lucid live bridge error"
                    bot.feed_details = []
                    bot._save()
            await asyncio.sleep(float(getattr(config, "LUCID_LOCAL_BRIDGE_POLL_SEC", 1.0)))
    elif getattr(config, "LUCID_USE_TRADINGVIEW_LIVE", True):
        while True:
            try:
                async for shared_df, source_status in _stream_tradingview_component_data():
                    for bot in bots:
                        bot.live_feed_status = source_status
                        bot.feed_details = []
                        block_reason = (
                            _lucid_feed_block_reason(source_status)
                            if getattr(config, "LUCID_REQUIRE_REALTIME_FEED", True)
                            else ""
                        )
                        if block_reason:
                            _apply_feed_block(bot, block_reason)
                            continue
                        source_block = _lucid_source_block_reason(source_status)
                        if source_block:
                            _apply_feed_block(bot, source_block, status="blocked - live source differs from backtest")
                            continue
                        history_block = _lucid_history_block_reason(shared_df)
                        if history_block:
                            _apply_feed_block(bot, history_block, status="blocked - Lucid history not ready")
                            continue
                        bot._df = _clone_component_frames(shared_df)
                        bot.data_error = ""
                        bot._tick()
                        bot._save()
            except Exception as e:
                err = f"TradingView live feed error: {type(e).__name__}: {str(e)[:100]}"
                for bot in bots:
                    bot.data_error = err
                    bot.live_feed_status = "reconnecting TradingView websocket"
                    bot.feed_details = []
                    bot._save()
                await asyncio.sleep(3)
    else:
        while True:
            try:
                shared_df = await _load_all_component_data()
                for bot in bots:
                    bot.feed_details = []
                    fallback_block = _lucid_fallback_block_reason(
                        getattr(config, "LUCID_REQUIRE_REALTIME_FEED", True)
                    )
                    if fallback_block:
                        _apply_feed_block(bot, fallback_block, "Yahoo fallback polling (blocked)")
                        continue
                    source_block = _lucid_source_block_reason("Yahoo fallback polling")
                    if source_block:
                        _apply_feed_block(
                            bot,
                            source_block,
                            "Yahoo fallback polling (blocked - source mismatch)",
                            status="blocked - live source differs from backtest",
                        )
                        continue
                    history_block = _lucid_history_block_reason(shared_df)
                    if history_block:
                        _apply_feed_block(bot, history_block, status="blocked - Lucid history not ready")
                        continue
                    bot._df = _clone_component_frames(shared_df)
                    bot.data_error = ""
                    bot.live_feed_status = "Yahoo fallback polling"
                    bot._tick()
                    bot._save()
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:120]}"
                for bot in bots:
                    bot.data_error = err
                    bot.status = "data error"
                    bot.feed_details = []
                    bot._save()
            await asyncio.sleep(POLL_SEC)




def _daily(d: pd.DataFrame) -> pd.DataFrame:
    if d.empty:
        return pd.DataFrame(columns=["day", "open", "high", "low", "close", "range"])
    g = d.groupby("day")
    out = pd.DataFrame({
        "day": g["day"].first(),
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
    }).reset_index(drop=True)
    out["range"] = out["high"] - out["low"]
    return out


class LucidPassPaperBot:
    NAME = NAME

    def __init__(self):
        self.enabled = True
        self.balance = START_BALANCE
        self.peak = START_BALANCE
        self.locked = False
        self.floor = START_BALANCE - MAX_DRAWDOWN
        self.day_key = ""
        self.day_pnl = 0.0
        self.passed = False
        self.failed = False
        self.daily_stopped_day = ""
        self.pos: dict[str, LucidPos] = {}
        self.fired_keys: set[str] = set()
        self.warning_keys: set[str] = set()
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.prices: dict[str, float] = {}
        self.setups: dict[str, dict] = {}
        self.status = "starting..."
        self.data_error = ""
        self.telegram_enabled = True
        self._telegram_client = None
        self._telegram_target = "me"
        self._telegram_bot_token = ""
        self._telegram_chat_id = ""
        self._last_alert_error = ""
        self._alert_queue = None
        self._alert_worker_task = None
        self._df: dict[str, pd.DataFrame] = {}
        self._last_bar_ts: dict[str, int] = {}
        self._last_bar_sig: dict[str, tuple] = {}
        self._primed_keys: set[str] = set()
        self.live_feed_status = "starting..."
        self.feed_details: list[dict] = []
        self.realtime_entry_ready = False
        self.realtime_entry_status = "Exact realtime source is not verified."
        self._enforce_live_open_guard = True
        self._load()

    def _path(self) -> str:
        return os.path.join(config.DATA_DIR, "lucid_pass_state.json")

    def _save(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            path = self._path()
            tmp = path + ".tmp"
            data = {
                "enabled": self.enabled,
                "strategy_version": STRATEGY_VERSION,
                "strategy_fingerprint": STRATEGY_FINGERPRINT,
                "balance": self.balance,
                "peak": self.peak,
                "locked": self.locked,
                "floor": self.floor,
                "day_key": self.day_key,
                "day_pnl": self.day_pnl,
                "passed": self.passed,
                "failed": self.failed,
                "daily_stopped_day": self.daily_stopped_day,
                "telegram_enabled": self.telegram_enabled,
                "positions": {k: asdict(p) for k, p in self.pos.items()},
                "fired_keys": sorted(self.fired_keys)[-120:],
                "warning_keys": sorted(self.warning_keys)[-160:],
                "history": self.history[:180],
                "log": self.log[:120],
                "setups": self.setups,
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception:
            pass

    def _load(self):
        try:
            if not os.path.exists(self._path()):
                return
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            if d.get("strategy_version") != STRATEGY_VERSION:
                self.telegram_enabled = bool(d.get("telegram_enabled", True))
                return
            saved_fp = str(d.get("strategy_fingerprint") or "").strip()
            if saved_fp and saved_fp != STRATEGY_FINGERPRINT:
                self.telegram_enabled = bool(d.get("telegram_enabled", True))
                return
            self.enabled = bool(d.get("enabled", True))
            self.balance = float(d.get("balance", START_BALANCE))
            self.peak = float(d.get("peak", self.balance))
            self.locked = bool(d.get("locked", False))
            self.floor = float(d.get("floor", START_BALANCE - MAX_DRAWDOWN))
            self.day_key = str(d.get("day_key", ""))
            self.day_pnl = float(d.get("day_pnl", 0.0))
            self.passed = bool(d.get("passed", False))
            self.failed = bool(d.get("failed", False))
            self.daily_stopped_day = str(d.get("daily_stopped_day", ""))
            self.telegram_enabled = bool(d.get("telegram_enabled", True))
            self.pos = {k: LucidPos(**p) for k, p in (d.get("positions") or {}).items()}
            self.fired_keys = set(d.get("fired_keys") or [])
            self.warning_keys = set(d.get("warning_keys") or [])
            self.history = d.get("history", []) or []
            self.log = d.get("log", []) or []
            self.setups = d.get("setups", {}) or {}
        except Exception:
            pass

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"t": time.time(), "kind": kind, "msg": msg})
        self.log = self.log[:120]
        print(f"[lucid-pass] {msg}")

    def set_telegram_client(self, client, target: str = "me"):
        self._telegram_client = client
        self._telegram_target = target or "me"

    def set_telegram_bot(self, token: str, chat_id):
        self._telegram_bot_token = str(token or "").strip()
        self._telegram_chat_id = str(chat_id or "").strip()

    def set_notifications(self, on):
        self.telegram_enabled = bool(on)
        self._note("Telegram signals ON" if self.telegram_enabled else "Telegram signals OFF")
        self._save()
        return {"ok": True, "telegram_enabled": self.telegram_enabled}

    def _telegram_ready(self) -> bool:
        if self._telegram_bot_token and self._telegram_chat_id:
            return True
        try:
            return bool(self._telegram_client and self._telegram_client.is_connected())
        except Exception:
            return False

    def _alert(self, text: str):
        if not self.telegram_enabled:
            return
        has_bot = bool(self._telegram_bot_token and self._telegram_chat_id)
        has_client = self._telegram_client is not None
        if not has_bot and not has_client:
            return
        try:
            loop = asyncio.get_running_loop()
            if self._alert_queue is None:
                self._alert_queue = asyncio.Queue()
            if self._alert_worker_task is None or self._alert_worker_task.done():
                self._alert_worker_task = loop.create_task(self._alert_worker())
            self._alert_queue.put_nowait(text)
        except RuntimeError:
            pass

    async def _alert_worker(self):
        while True:
            text = await self._alert_queue.get()
            try:
                if self._telegram_bot_token and self._telegram_chat_id:
                    url = f"https://api.telegram.org/bot{self._telegram_bot_token}/sendMessage"
                    payload = {
                        "chat_id": self._telegram_chat_id,
                        "text": text,
                        "disable_notification": False,
                    }
                    loop = asyncio.get_running_loop()
                    resp = await loop.run_in_executor(
                        None,
                        lambda: requests.post(url, json=payload, timeout=10),
                    )
                    resp.raise_for_status()
                elif self._telegram_client is not None:
                    if not self._telegram_client.is_connected():
                        continue
                    await self._telegram_client.send_message(self._telegram_target, text)
                self._last_alert_error = ""
                await asyncio.sleep(0.25)
            except Exception as e:
                self._last_alert_error = f"{type(e).__name__}: {str(e)[:100]}"
                self._note(f"telegram signal failed: {self._last_alert_error}", "loss")
            finally:
                self._alert_queue.task_done()

    def _fmt_qty(self, qty: float) -> str:
        return f"{qty:.6f}".rstrip("0").rstrip(".")

    def _open_alert_text(self, p: LucidPos) -> str:
        side = p.side.upper()
        approx_risk = abs(p.entry - p.stop) * p.micro_pv * p.qty
        return "\n".join([
            f"OPEN {p.label} {side} x{self._fmt_qty(p.qty)}",
            f"RISK ~${approx_risk:.0f}",
            f"ENTRY {p.entry:.2f}",
            f"SL {p.stop:.2f}",
            f"TP1 {p.tp1:.2f}",
            f"TP2 {p.target:.2f}",
            f"BOT {self.NAME}",
        ])

    def _partial_alert_text(self, p: LucidPos, qty: float, exit_px: float, pnl: float) -> str:
        return "\n".join([
            f"TP1 HIT {p.label} x{self._fmt_qty(qty)}",
            f"CLOSE {self._fmt_qty(qty)} @ {exit_px:.2f}",
            f"KEEP x{self._fmt_qty(p.qty)}",
            f"MOVE SL -> BE {p.entry:.2f}",
            f"PNL ${pnl:+.2f}",
        ])

    def _close_alert_text(self, p: LucidPos, exit_px: float, reason: str, total: float) -> str:
        return "\n".join([
            f"CLOSE {p.label} x{self._fmt_qty(p.qty)}",
            f"EXIT {exit_px:.2f}",
            f"WHY {reason}",
            f"PNL ${total:+.2f}",
            f"BOT {self.NAME}",
        ])

    def _warning_alert_text(self, key: str, side: str, entry: float, stop: float,
                            target: float, note: str) -> str:
        c = COMPONENTS[key]
        qty, approx_risk = self._size_for_levels(c["symbol"], entry, stop)
        return "\n".join([
            f"WARN {c['label']} {side.upper()} x{self._fmt_qty(qty)} soon",
            "WAIT FOR OPEN",
            f"RISK ~${approx_risk:.0f}",
            f"ENTRY ~{entry:.2f}",
            f"SL {stop:.2f}",
            f"TP {target:.2f}",
            f"WHY {note}",
            f"BOT {self.NAME}",
        ])

    def _size_for_levels(self, symbol: str, entry: float, stop: float) -> tuple[float, float]:
        pv, _, _ = MARKETS[symbol]
        r_points = abs(float(entry) - float(stop))
        if r_points <= 0:
            return 1, 0.0
        qty = RISK_USD / max(r_points * pv, 1e-9)
        approx_risk = r_points * pv * qty
        return qty, approx_risk

    def _warn_signal(self, key: str, side: str, entry: float, stop: float,
                     target: float, note: str):
        if not self.enabled or self.failed or key in self.pos:
            return
        if self._stops_after_target() and (self.passed or self.equity() >= TARGET_BALANCE):
            return
        if self._uses_daily_loss_guard() and self.day_pnl <= -DAILY_LOSS_LIMIT:
            return
        if not self._exact_realtime_entry_ok():
            return
        fired_key = f"{self.day_key}:{key}"
        if fired_key in self.fired_keys:
            return
        _, tick, _ = MARKETS[COMPONENTS[key]["symbol"]]
        level_key = int(round(float(entry) / max(tick, 1e-9)))
        warn_key = f"{self.day_key}:{key}:{side}:{level_key}"
        if warn_key in self.warning_keys:
            return
        now = time.time()
        for old_key, old_ts in list(_GLOBAL_WARNING_ALERTS.items()):
            if now - old_ts > WARNING_DEDUP_SECONDS:
                _GLOBAL_WARNING_ALERTS.pop(old_key, None)
        if warn_key in _GLOBAL_WARNING_ALERTS:
            return
        self.warning_keys.add(warn_key)
        _GLOBAL_WARNING_ALERTS[warn_key] = now
        c = COMPONENTS[key]
        self._note(
            f"WARNING possible {c['name']} {side.upper()} {c['label']} near {entry:.2f} - wait for OPEN signal",
            "info",
        )
        self._alert(self._warning_alert_text(key, side, entry, stop, target, note))

    def _stops_after_target(self) -> bool:
        return True

    def _uses_daily_loss_guard(self) -> bool:
        return True

    def _next_session_open(self, now_ny: datetime) -> datetime:
        now_utc = now_ny.astimezone(ZoneInfo("UTC"))
        day0 = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(8):
            day = day0 + timedelta(days=offset)
            open_utc = day.replace(
                hour=BACKTEST_SESSION_START_UTC // 60,
                minute=BACKTEST_SESSION_START_UTC % 60,
            )
            end_utc = day.replace(
                hour=BACKTEST_SESSION_END_UTC // 60,
                minute=BACKTEST_SESSION_END_UTC % 60,
            )
            if now_utc >= end_utc:
                continue
            if open_utc.astimezone(NY).weekday() >= 5:
                continue
            return open_utc.astimezone(NY)
        return (day0 + timedelta(days=8)).replace(
            hour=BACKTEST_SESSION_START_UTC // 60,
            minute=BACKTEST_SESSION_START_UTC % 60,
        ).astimezone(NY)

    def _next_session_close(self, session_open: datetime) -> datetime:
        open_utc = session_open.astimezone(ZoneInfo("UTC"))
        return open_utc.replace(
            hour=BACKTEST_SESSION_END_UTC // 60,
            minute=BACKTEST_SESSION_END_UTC % 60,
            second=0,
            microsecond=0,
        ).astimezone(NY)

    def _component_flat_time(self, session_open: datetime, interval_min: int) -> datetime:
        open_utc = session_open.astimezone(ZoneInfo("UTC"))
        return open_utc.replace(
            hour=(BACKTEST_SESSION_END_UTC - interval_min) // 60,
            minute=(BACKTEST_SESSION_END_UTC - interval_min) % 60,
            second=0,
            microsecond=0,
        ).astimezone(NY)

    def _next_component_check(self, session_open: datetime, now_ny: datetime,
                              interval_min: int, min_bars: int) -> datetime:
        first = session_open + timedelta(minutes=interval_min * min_bars)
        session_end = self._next_session_close(session_open)
        if now_ny < first:
            cand = first
        else:
            elapsed_min = max(0.0, (now_ny - session_open).total_seconds() / 60.0)
            n = max(min_bars, int(math.ceil(elapsed_min / interval_min)))
            cand = session_open + timedelta(minutes=n * interval_min)
            if cand <= now_ny:
                cand += timedelta(minutes=interval_min)
        if cand >= session_end:
            next_open = self._next_session_open(self._next_session_close(session_open) + timedelta(minutes=1))
            return next_open + timedelta(minutes=interval_min * min_bars)
        return cand

    def _next_component_alert(self, session_open: datetime, now_ny: datetime,
                              interval_min: int, min_bars: int) -> datetime:
        first_start = session_open + timedelta(minutes=interval_min * min_bars)
        first_alert = first_start + timedelta(minutes=interval_min)
        session_end = self._next_session_close(session_open)
        if now_ny < first_alert:
            return first_alert
        elapsed_min = max(0.0, (now_ny - first_alert).total_seconds() / 60.0)
        n = int(math.floor(elapsed_min / interval_min)) + 1
        alert = first_alert + timedelta(minutes=n * interval_min)
        if alert > session_end:
            next_open = self._next_session_open(session_end + timedelta(minutes=1))
            return next_open + timedelta(minutes=interval_min * (min_bars + 1))
        return alert

    def _next_signal_window(self) -> str:
        if not self.enabled:
            return "Bot is paused. No new entry signals until enabled."
        if self.failed:
            return "Bot is stopped because the drawdown floor was breached."
        if self._stops_after_target() and (self.passed or self.equity() >= TARGET_BALANCE):
            return "Target already passed. This bot will not send new entries until reset."
        if self._uses_daily_loss_guard() and self.day_pnl <= -DAILY_LOSS_LIMIT:
            return "Daily loss guard is active. Next possible entries are tomorrow's NY session."
        data_error_l = str(self.data_error or "").lower()
        if "exact dukascopy" in data_error_l and "stale" in data_error_l:
            if "some exact dukascopy components are stale" in data_error_l:
                return (
                    "Some exact-source candles are stale. Stale components are blocked; "
                    "only fresh components can fire. "
                    f"Strategy candle session is {_lucid_session_text()}, "
                    "and completed-bar checks for stale components are theoretical only."
                )
            return (
                "Exact-source candles are stale, so no Lucid signal will fire until "
                "the public hourly Dukascopy tick-file feed publishes the needed hour. "
                f"Strategy candle session is {_lucid_session_text()}, "
                "but any completed-bar times shown while stale are theoretical only."
            )

        now_ny = datetime.now(NY)
        session_open = self._next_session_open(now_ny)
        session_end = self._next_session_close(session_open)
        specs = [
            ("ES/NQ 3m VWAP", 3, VWAP_MIN_BARS),
            ("CL 5m VWAP", 5, VWAP_MIN_BARS),
            ("NQ/CL 30m Turtle/NR7", 30, 0),
        ]
        checks = []
        for label, interval_min, min_bars in specs:
            dt = self._next_component_alert(session_open, now_ny, interval_min, min_bars)
            checks.append(f"{label} {dt.astimezone(TBILISI).strftime('%a %H:%M')}")
        start_txt = session_open.astimezone(TBILISI).strftime("%a %H:%M")
        end_txt = session_end.astimezone(TBILISI).strftime("%H:%M")
        flat_txt = ", ".join(
            f"{label} {self._component_flat_time(session_open, interval).astimezone(TBILISI).strftime('%H:%M')}"
            for label, interval, _ in specs
        )
        return (
            "Next completed-bar alerts: " + "; ".join(checks) +
            f" Tbilisi. Backtest candle session {start_txt}-{end_txt}; forced flat by component: {flat_txt}."
        )

    async def manage_loop(self):
        await manage_shared_loop([self])

    def _roll_day(self, day_key: str):
        if day_key == self.day_key:
            return
        if self.day_key:
            self.peak = max(self.peak, self.balance)
        self.day_key = day_key
        self.day_pnl = 0.0
        self.daily_stopped_day = ""
        self._prune_fired(day_key)

    def _prune_fired(self, day_key: str):
        keep = {k for k in self.fired_keys if k.startswith(day_key + ":")}
        old = sorted(self.fired_keys - keep)[-40:]
        self.fired_keys = keep | set(old)
        wkeep = {k for k in self.warning_keys if k.startswith(day_key + ":")}
        wold = sorted(self.warning_keys - wkeep)[-60:]
        self.warning_keys = wkeep | set(wold)

    def _bar_signature(self, cur: pd.Series) -> tuple:
        return (
            _row_ts(cur),
            round(float(cur["high"]), 8),
            round(float(cur["low"]), 8),
            round(float(cur["close"]), 8),
            round(float(cur.get("volume", 0.0) or 0.0), 4),
        )

    def _new_entry_events(self, latest_day) -> list[tuple[pd.Timestamp, int, str, int]]:
        order = {key: i for i, key in enumerate(COMPONENTS)}
        events: list[tuple[pd.Timestamp, int, str, int]] = []
        for key, d in self._df.items():
            if d.empty:
                continue
            ts_values = _frame_ts_values(d)
            last_ts = self._last_bar_ts.get(key)
            indices: list[int] = []
            if last_ts is None:
                if key in self.pos:
                    p = self.pos[key]
                    last_managed = int(getattr(p, "last_managed_bar", 0) or (p.opened_bar - 1))
                    indices = [int(i) for i in np.flatnonzero(ts_values > last_managed)]
                else:
                    indices = [len(d) - 1]
            else:
                indices = [int(i) for i in np.flatnonzero(ts_values > last_ts)]
                if not indices:
                    cur = d.iloc[-1]
                    ts = int(ts_values[-1])
                    if ts == last_ts and self._bar_signature(cur) != self._last_bar_sig.get(key):
                        indices.append(len(d) - 1)
            for i in indices:
                cur = d.iloc[i]
                if cur["day"] == latest_day:
                    events.append((pd.Timestamp(cur["dt_utc"]), order.get(key, 999), key, int(i)))
        events.sort(key=lambda x: (x[0], x[1]))
        return events

    def _tick(self):
        latest_day = None
        for d in self._df.values():
            if not d.empty:
                day = d.iloc[-1]["day"]
                latest_day = max(latest_day, day) if latest_day else day
        if latest_day is None:
            self._force_close_expired_positions_without_new_bars()
            self.status = "waiting for ES/NQ/CL candles..."
            return

        for key, d in self._df.items():
            if key in self.pos and not d.empty:
                p = self.pos.get(key)
                latest_bar_day = str(d.iloc[-1].get("day", ""))
                if (
                    not p
                    or not p.last_day
                    or (latest_bar_day == str(p.last_day) and latest_bar_day == str(latest_day))
                ):
                    continue
                self.prices[key] = float(d.iloc[-1]["close"])
                self._manage_pending(key, d, through_ts=d.iloc[-1]["dt_utc"])

        self._force_close_expired_positions_without_new_bars()
        self._roll_day(str(latest_day))

        entry_events = self._new_entry_events(latest_day)
        for key, d in self._df.items():
            if d.empty:
                self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "waiting for candles"}

        for _, _, key, idx in entry_events:
            d = self._df.get(key)
            if d is None or d.empty or idx >= len(d):
                continue
            cur = d.iloc[idx]
            self.prices[key] = float(cur["close"])
            if key in self.pos:
                self._manage_pending(key, d, through_ts=cur["dt_utc"])
                if key not in self.pos:
                    continue
            if cur["day"] != latest_day:
                continue
            bar_ts = _row_ts(cur)
            bar_sig = self._bar_signature(cur)
            if self._last_bar_sig.get(key) == bar_sig:
                continue
            self._last_bar_sig[key] = bar_sig
            self._last_bar_ts[key] = bar_ts

            if key not in self.pos and self._can_open_key(key, cur):
                today = d[(d["day"] == latest_day) & (d["dt_utc"] <= cur["dt_utc"])].reset_index(drop=True)
                daily_before = _daily(d[d["day"] < latest_day])
                sig = self._signal(key, today, daily_before)
                if sig:
                    fired_key = f"{self.day_key}:{key}"
                    if sig.get("spent"):
                        self.fired_keys.add(fired_key)
                    elif fired_key not in self.fired_keys:
                        self._open(sig, cur)
                        if key in self.pos:
                            self._manage_pending(key, d, through_ts=cur["dt_utc"])
                elif key not in self.setups:
                    self.setups[key] = {
                        "mkt": COMPONENTS[key]["label"],
                        "status": COMPONENTS[key]["name"] + " scanning",
                    }
        self._force_close_expired_positions_without_new_bars()
        self._set_status()

    def _can_open(self, cur: pd.Series) -> bool:
        return self._can_open_key("", cur)

    def _real_entry_window_ok(self, cur: pd.Series | None = None, key: str = "") -> bool:
        return _entry_clock_ok(cur, key, pd.Timestamp.now(tz="UTC"))

    def _fresh_entry_bar_ok(self, key: str, cur: pd.Series, now_utc: pd.Timestamp | None = None) -> bool:
        if not key:
            return True
        try:
            bar_start = pd.Timestamp(cur["dt_utc"])
            if bar_start.tzinfo is None:
                bar_start = bar_start.tz_localize("UTC")
            now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
            if now_utc.tzinfo is None:
                now_utc = now_utc.tz_localize("UTC")
            else:
                now_utc = now_utc.tz_convert("UTC")
            age = (now_utc - bar_start).total_seconds()
            bar_sec = int(COMPONENTS[key]["bar_sec"])
        except Exception:
            return False
        min_age = bar_sec
        max_age = bar_sec + ENTRY_BAR_LAG_GRACE_SEC
        return min_age <= age <= max_age

    def _open_guard_now_utc(self) -> pd.Timestamp:
        return pd.Timestamp.now(tz="UTC")

    def _enforce_open_wall_clock(self) -> bool:
        return bool(getattr(self, "_enforce_live_open_guard", True))

    def _requires_exact_realtime_entry(self) -> bool:
        return bool(getattr(config, "LUCID_REQUIRE_EXACT_REALTIME_ENTRY", True)) and self._enforce_open_wall_clock()

    def _exact_realtime_entry_ok(self) -> bool:
        if not self._requires_exact_realtime_entry():
            return True
        return bool(getattr(self, "realtime_entry_ready", False))

    def _live_open_guard_ok(self, sig: dict, cur: pd.Series) -> bool:
        if not self._enforce_open_wall_clock():
            return True
        key = str(sig.get("key") or "")
        return self._exact_realtime_entry_ok() and self._entry_guard_ok(key, cur)

    def _entry_guard_ok(self, key: str, cur: pd.Series) -> bool:
        now_utc = self._open_guard_now_utc()
        return _entry_clock_ok(cur, key, now_utc) and self._fresh_entry_bar_ok(key, cur, now_utc)

    def _force_close_expired_positions_without_new_bars(self):
        if not self._enforce_open_wall_clock() or not self.pos:
            return
        now_utc = self._open_guard_now_utc()
        if now_utc.tzinfo is None:
            now_utc = now_utc.tz_localize("UTC")
        else:
            now_utc = now_utc.tz_convert("UTC")
        for key, p in list(self.pos.items()):
            try:
                opened = pd.Timestamp(p.opened_bar, unit="s", tz="UTC")
                flat_min = _component_flat_utc_min(key)
                bar_sec = int(COMPONENTS[key]["bar_sec"])
                deadline = (
                    opened.floor("D")
                    + pd.Timedelta(minutes=flat_min)
                    + pd.Timedelta(seconds=bar_sec + ENTRY_BAR_LAG_GRACE_SEC)
                )
            except Exception:
                continue
            if now_utc >= deadline:
                self._note(
                    f"FORCE EOD {p.label} {p.strat} - no fresh candle after forced-flat deadline; "
                    f"closing at last known close {p.last_close or p.entry:.2f}",
                    "loss",
                )
                if p.last_day and self.day_key and str(self.day_key) != str(p.last_day):
                    current_day = self.day_key
                    current_day_pnl = self.day_pnl
                    current_daily_stop = self.daily_stopped_day
                    self.day_key = str(p.last_day)
                    try:
                        self._close(key, p.last_close or p.entry, "eod")
                    finally:
                        self.day_key = current_day
                        self.day_pnl = current_day_pnl
                        self.daily_stopped_day = current_daily_stop
                else:
                    self._close(key, p.last_close or p.entry, "eod")

    def _can_open_key(self, key: str, cur: pd.Series) -> bool:
        if not self.enabled or self.failed:
            return False
        eq = self.equity()
        if eq >= TARGET_BALANCE:
            if not self.passed:
                self.passed = True
                self._note("pass target reached - no new trades until reset", "win")
            return False
        if eq <= self.floor:
            if not self.failed:
                self.failed = True
                self._note("drawdown floor breached - bot stopped", "loss")
            return False
        if self.day_pnl <= -DAILY_LOSS_LIMIT:
            if self.daily_stopped_day != self.day_key:
                self.daily_stopped_day = self.day_key
                self._note("daily loss guard hit - no more entries today", "loss")
            return False
        if not self._exact_realtime_entry_ok():
            if key:
                self.setups[key] = {
                    "mkt": COMPONENTS[key]["label"],
                    "status": "blocked - exact realtime bridge not ready",
                }
            return False
        if not self._entry_guard_ok(key, cur):
            return False
        return True

    def _signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame) -> dict | None:
        if today.empty:
            return None
        kind = COMPONENTS[key]["kind"]
        last_i = len(today) - 1
        if kind == "vwap":
            return self._vwap_signal(key, today, last_i)
        if kind == "turtle":
            return self._turtle_signal(key, today, daily_before, last_i)
        if kind == "eighty":
            return self._eighty_signal(key, today, daily_before, last_i)
        if kind == "nr7":
            return self._nr7_signal(key, today, daily_before, last_i)
        return None

    def _vwap_signal(self, key: str, today: pd.DataFrame, last_i: int) -> dict | None:
        if last_i < VWAP_MIN_BARS:
            self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "building VWAP sample"}
            return None
        tp = (today["high"].astype(float) + today["low"].astype(float) + today["close"].astype(float)) / 3.0
        vol = today["volume"].astype(float).where(today["volume"].astype(float) > 0, 1.0)
        cum_v = 0.0
        cum_pv = 0.0
        cum_p2v = 0.0
        current_levels = None
        for i in range(last_i + 1):
            v = float(vol.iloc[i])
            t = float(tp.iloc[i])
            cum_v += v
            cum_pv += t * v
            cum_p2v += t * t * v
            if cum_v <= 0 or i < VWAP_MIN_BARS:
                continue
            vwap = float(cum_pv / cum_v)
            var = max(float(cum_p2v / cum_v) - vwap * vwap, 0.0)
            sig = math.sqrt(var)
            if sig <= 0:
                continue
            up = vwap + VWAP_K * sig
            dn = vwap - VWAP_K * sig
            row = today.iloc[i]
            h, l = float(row["high"]), float(row["low"])
            current_levels = (vwap, sig, up, dn)
            if h >= up:
                entry = up
                stop = vwap + (VWAP_K + 1.0) * sig
                return self._mk_sig(
                    key, -1, entry, stop, vwap,
                    f"fade +{VWAP_K:g} sigma back to VWAP {vwap:.2f}",
                    spent=(i < last_i),
                )
            if l <= dn:
                entry = dn
                stop = vwap - (VWAP_K + 1.0) * sig
                return self._mk_sig(
                    key, 1, entry, stop, vwap,
                    f"fade -{VWAP_K:g} sigma back to VWAP {vwap:.2f}",
                    spent=(i < last_i),
                )
        if current_levels is None:
            self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "building VWAP sample"}
            return None
        vwap, sig, up, dn = current_levels
        self.setups[key] = {
            "mkt": COMPONENTS[key]["label"],
            "status": f"VWAP {vwap:.2f}, bands {dn:.2f}/{up:.2f}",
        }
        cur = today.iloc[last_i]
        h, l = float(cur["high"]), float(cur["low"])
        warn_gap = sig * VWAP_WARN_SIGMA
        if 0 <= up - h <= warn_gap:
            self._warn_signal(
                key, "short", up, vwap + (VWAP_K + 1.0) * sig, vwap,
                f"price is within {VWAP_WARN_SIGMA:g} sigma of the upper VWAP fade band",
            )
        elif 0 <= l - dn <= warn_gap:
            self._warn_signal(
                key, "long", dn, vwap - (VWAP_K + 1.0) * sig, vwap,
                f"price is within {VWAP_WARN_SIGMA:g} sigma of the lower VWAP fade band",
            )
        return None

    def _turtle_signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame, last_i: int) -> dict | None:
        if len(daily_before) < TURTLE_LOOKBACK + TURTLE_RECENCY:
            self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "waiting for 14 prior sessions"}
            return None
        _, tick, _ = MARKETS[COMPONENTS[key]["symbol"]]
        look = daily_before.tail(TURTLE_LOOKBACK).reset_index(drop=True)
        lows = look["low"].astype(float).to_numpy()
        highs = look["high"].astype(float).to_numpy()
        prior_low = float(lows.min())
        prior_high = float(highs.max())
        # Preserve the historical backtest helper's session-index age calculation.
        session_index = len(daily_before)
        lo_age = session_index - 1 - int(np.argmin(lows))
        hi_age = session_index - 1 - int(np.argmax(highs))
        self.setups[key] = {
            "mkt": COMPONENTS[key]["label"],
            "status": f"Turtle levels {prior_low:.2f}/{prior_high:.2f}",
        }
        for i in range(last_i + 1):
            row = today.iloc[i]
            d = 0
            if float(row["low"]) < prior_low and lo_age >= TURTLE_RECENCY:
                d = 1
                entry = prior_low + TURTLE_BUF_TICKS * tick
                stop = float(today.iloc[:i + 1]["low"].min()) - tick
            elif float(row["high"]) > prior_high and hi_age >= TURTLE_RECENCY:
                d = -1
                entry = prior_high - TURTLE_BUF_TICKS * tick
                stop = float(today.iloc[:i + 1]["high"].max()) + tick
            else:
                continue
            r_points = abs(entry - stop)
            if r_points <= 0:
                continue
            target = entry + d * 2.0 * r_points
            fill_i = None
            for j in range(i, last_i + 1):
                bar = today.iloc[j]
                if (d > 0 and float(bar["high"]) >= entry) or (d < 0 and float(bar["low"]) <= entry):
                    fill_i = j
                    break
            if fill_i is None:
                self._warn_signal(
                    key, "long" if d > 0 else "short", entry, stop, target,
                    "false break happened; waiting for the retrace entry to fill",
                )
                return None
            return self._mk_sig(
                key, d, entry, stop, target,
                "false break of prior 10-session extreme",
                spent=(fill_i < last_i),
            )
        return None

    def _eighty_signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame, last_i: int) -> dict | None:
        if daily_before.empty:
            self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "waiting for prior session"}
            return None
        _, tick, _ = MARKETS[COMPONENTS[key]["symbol"]]
        y = daily_before.iloc[-1]
        rng = float(y["high"] - y["low"])
        if rng <= 0:
            self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "waiting for prior range"}
            return None
        opened_top = float(y["open"]) >= float(y["high"]) - 0.2 * rng
        closed_bot = float(y["close"]) <= float(y["low"]) + 0.2 * rng
        opened_bot = float(y["open"]) <= float(y["low"]) + 0.2 * rng
        closed_top = float(y["close"]) >= float(y["high"]) - 0.2 * rng
        if opened_top and closed_bot:
            d = 1
            level = float(y["low"])
        elif opened_bot and closed_top:
            d = -1
            level = float(y["high"])
        else:
            self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "no 80-20 prior session"}
            return None

        trig = level - d * EIGHTY_BUF_TICKS * tick
        self.setups[key] = {
            "mkt": COMPONENTS[key]["label"],
            "status": f"80-20 armed level {level:.2f}, trigger {trig:.2f}",
        }
        pushed = False
        fill_i = None
        for i in range(last_i + 1):
            row = today.iloc[i]
            if not pushed and (
                (d > 0 and float(row["low"]) <= trig)
                or (d < 0 and float(row["high"]) >= trig)
            ):
                pushed = True
            if pushed and (
                (d > 0 and float(row["high"]) >= level)
                or (d < 0 and float(row["low"]) <= level)
            ):
                fill_i = i
                break
        stop_base = today.iloc[:last_i + 1]
        stop = (
            float(stop_base["low"].min()) - tick
            if d > 0
            else float(stop_base["high"].max()) + tick
        )
        r_points = abs(level - stop)
        if r_points <= 0:
            return None
        target = level + d * 2.0 * r_points
        if fill_i is None:
            if not pushed:
                self._warn_signal(
                    key, "long" if d > 0 else "short", level, stop, target,
                    "80-20 prior session armed; waiting for push beyond trigger",
                )
            else:
                self._warn_signal(
                    key, "long" if d > 0 else "short", level, stop, target,
                    "80-20 trigger pushed; waiting for snapback entry",
                )
            return None
        fill_stop_base = today.iloc[:fill_i + 1]
        stop = (
            float(fill_stop_base["low"].min()) - tick
            if d > 0
            else float(fill_stop_base["high"].max()) + tick
        )
        r_points = abs(level - stop)
        if r_points <= 0:
            return None
        target = level + d * 2.0 * r_points
        return self._mk_sig(
            key, d, level, stop, target,
            "prior session 80-20 reversal",
            spent=(fill_i < last_i),
        )

    def _nr7_signal(self, key: str, today: pd.DataFrame, daily_before: pd.DataFrame, last_i: int) -> dict | None:
        if len(daily_before) < 7:
            self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "waiting for NR7 history"}
            return None
        _, tick, _ = MARKETS[COMPONENTS[key]["symbol"]]
        last = daily_before.iloc[-1]
        prior6 = daily_before.iloc[-7:-1]
        if float(last["range"]) >= float(prior6["range"].min()):
            self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": "no NR7 setup today"}
            return None
        hi = float(last["high"])
        lo = float(last["low"])
        self.setups[key] = {"mkt": COMPONENTS[key]["label"], "status": f"NR7 armed {lo:.2f}/{hi:.2f}"}
        for i in range(last_i + 1):
            row = today.iloc[i]
            if float(row["high"]) >= hi + tick:
                entry = hi + tick
                stop = lo - tick
                r_points = abs(entry - stop)
                target = entry + 2.0 * r_points
                return self._mk_sig(key, 1, entry, stop, target, "NR7 day high breakout", spent=(i < last_i))
            if float(row["low"]) <= lo - tick:
                entry = lo - tick
                stop = hi + tick
                r_points = abs(entry - stop)
                target = entry - 2.0 * r_points
                return self._mk_sig(key, -1, entry, stop, target, "NR7 day low breakout", spent=(i < last_i))
        cur = today.iloc[last_i]
        cur_hi = float(cur["high"])
        cur_lo = float(cur["low"])
        warn_gap = NR7_WARN_TICKS * tick
        long_entry = hi + tick
        short_entry = lo - tick
        if 0 <= long_entry - cur_hi <= warn_gap:
            stop = lo - tick
            target = long_entry + 2.0 * abs(long_entry - stop)
            self._warn_signal(key, "long", long_entry, stop, target, "NR7 high breakout is close")
        elif 0 <= cur_lo - short_entry <= warn_gap:
            stop = hi + tick
            target = short_entry - 2.0 * abs(short_entry - stop)
            self._warn_signal(key, "short", short_entry, stop, target, "NR7 low breakout is close")
        return None

    def _mk_sig(self, key: str, d: int, entry: float, stop: float, target: float,
                note: str, spent: bool = False) -> dict:
        c = COMPONENTS[key]
        return {
            "key": key,
            "symbol": c["symbol"],
            "label": c["label"],
            "strat": c["name"],
            "side": "long" if d > 0 else "short",
            "entry": float(entry),
            "stop": float(stop),
            "target": float(target),
            "note": note,
            "spent": spent,
        }

    def _open(self, sig: dict, cur: pd.Series):
        if not self._live_open_guard_ok(sig, cur):
            key = str(sig.get("key") or "")
            self._note(
                f"REJECT stale/out-of-session open for {sig.get('strat', key)} "
                f"bar {pd.Timestamp(cur.get('dt_utc'))}",
                "loss",
            )
            return
        side = 1 if sig["side"] == "long" else -1
        pv, tick, _ = MARKETS[sig["symbol"]]
        raw_entry = float(sig["entry"])
        entry = raw_entry + side * tick * SLIP_TICKS
        stop = float(sig["stop"])
        target = float(sig["target"])
        r_points = abs(entry - stop)
        if r_points <= 0:
            return
        qty = RISK_USD / max(r_points * pv, 1e-9)
        tp1 = entry + side * r_points
        cost_usd = self._trade_cost_usd(r_points, tick)
        p = LucidPos(
            id=uuid.uuid4().hex[:6],
            key=sig["key"],
            symbol=sig["symbol"],
            label=sig["label"],
            strat=sig["strat"],
            side=sig["side"],
            qty=qty,
            qty0=qty,
            entry=entry,
            stop=stop,
            stop0=stop,
            tp1=tp1,
            target=target,
            r_points=r_points,
            micro_pv=pv,
            tick=tick,
            risk_usd=RISK_USD,
            cost_usd=cost_usd,
            opened_at=time.time(),
            opened_bar=_row_ts(cur),
            best=entry,
            last_managed_bar=_row_ts(cur) - 1,
            last_close=entry,
            last_day=str(cur.get("day", "")),
            realized=0.0,
            note=sig["note"],
        )
        self.pos[p.key] = p
        self.fired_keys.add(f"{self.day_key}:{p.key}")
        self._note(
            f"OPEN {p.strat} {p.side.upper()} {p.label} @ {entry:.2f} "
            f"stop {stop:.2f} tp1 {tp1:.2f} target {target:.2f} ({self._fmt_qty(qty)} virtual micros)",
            "open",
        )
        self._alert(self._open_alert_text(p))

    def _manage_pending(self, key: str, d: pd.DataFrame, through_ts=None):
        p = self.pos.get(key)
        if p is None or d.empty:
            return
        last_managed = int(getattr(p, "last_managed_bar", 0) or (p.opened_bar - 1))
        through_bar = None
        if through_ts is not None:
            through_bar = int(pd.Timestamp(through_ts).timestamp())
        ts_values = _frame_ts_values(d)
        mask = (ts_values >= int(p.opened_bar)) & (ts_values > last_managed)
        if through_bar is not None:
            mask = mask & (ts_values <= through_bar)
        rows = d.iloc[np.flatnonzero(mask)]
        for _, row in rows.iterrows():
            if key not in self.pos:
                break
            self._manage(key, row)

    def _manage(self, key: str, bar: pd.Series):
        p = self.pos.get(key)
        if p is None:
            return
        p.last_managed_bar = _row_ts(bar)
        side = 1 if p.side == "long" else -1
        hi, lo, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        bar_day = str(bar.get("day", ""))
        if p.last_day and bar_day and bar_day != p.last_day:
            self._close(key, p.last_close or p.entry, "eod")
            return
        if bar_day:
            p.last_day = bar_day
        p.last_close = close
        utc_minute = _utc_minute(bar["dt_utc"])
        if side > 0:
            p.best = max(p.best, hi)
            stopped = lo <= p.stop
            tp1_hit = hi >= p.tp1
            target_hit = hi >= p.target
        else:
            p.best = min(p.best, lo)
            stopped = hi >= p.stop
            tp1_hit = lo <= p.tp1
            target_hit = lo <= p.target

        if not p.partial_done:
            if stopped:
                self._close(key, p.stop, "stop")
                return
            if target_hit:
                self._close(key, p.target, "target")
                return
            if tp1_hit:
                half = p.qty0 * 0.5
                half = min(half, p.qty)
                if half > 0:
                    self._partial(key, half, p.tp1)
                if key in self.pos:
                    p.partial_done = True
                    p.stop = p.entry
                    self._note(f"+1R {p.label} {p.strat} - runner stop to breakeven", "info")
                    if utc_minute >= _component_flat_utc_min(key):
                        self._close(key, close, "eod")
                return
        else:
            if stopped:
                self._close(key, p.stop, "be" if abs(p.stop - p.entry) < 1e-9 else "stop")
                return
            if target_hit:
                self._close(key, p.target, "target")
                return

        if utc_minute >= _component_flat_utc_min(key):
            self._close(key, close, "eod")

    def _exit_price(self, p: LucidPos, raw_exit: float) -> float:
        side = 1 if p.side == "long" else -1
        return float(raw_exit) - side * p.tick * SLIP_TICKS

    def _trade_cost_usd(self, r_points: float, tick: float) -> float:
        if r_points <= 0:
            return 0.0
        return RISK_USD * ((2.0 * tick / r_points) + 0.05)

    def _pnl(self, side: int, entry: float, exit_px: float, qty: float, pv: float) -> float:
        return side * (exit_px - entry) * qty * pv

    def _partial(self, key: str, qty: float, raw_exit: float):
        p = self.pos[key]
        side = 1 if p.side == "long" else -1
        exit_px = self._exit_price(p, raw_exit)
        pnl = self._pnl(side, p.entry, exit_px, qty, p.micro_pv)
        self._book(pnl)
        p.qty = p.qty - qty
        p.realized += pnl
        self._note(
            f"TP1 {p.strat} {p.label} @ {exit_px:.2f} - partial P&L ${pnl:+.2f}",
            "win" if pnl >= 0 else "loss",
        )
        self._alert(self._partial_alert_text(p, qty, exit_px, pnl))

    def _close(self, key: str, raw_exit: float, reason: str):
        p = self.pos.get(key)
        if p is None:
            return
        side = 1 if p.side == "long" else -1
        exit_px = self._exit_price(p, raw_exit)
        pnl = self._pnl(side, p.entry, exit_px, p.qty, p.micro_pv)
        gross_total = p.realized + pnl
        total = gross_total - p.cost_usd
        self._book(pnl - p.cost_usd)
        self.history.insert(0, {
            "mkt": p.label + "." + p.strat,
            "side": p.side,
            "entry": round(p.entry, 2),
            "exit": round(exit_px, 2),
            "qty": p.qty0,
            "pnl": round(total, 2),
            "gross_exit_pnl": round(gross_total, 2),
            "cost": round(p.cost_usd, 2),
            "rr": round(total / max(p.risk_usd, 1e-9), 2),
            "reason": reason,
            "closed_at": time.time(),
        })
        self._note(
            f"CLOSE {p.strat} {p.label} @ {exit_px:.2f} - {reason} - trade P&L ${total:+.2f}",
            "win" if total >= 0 else "loss",
        )
        self._alert(self._close_alert_text(p, exit_px, reason, total))
        self.pos.pop(key, None)

    def _book(self, pnl: float):
        self.balance += pnl
        self.day_pnl += pnl
        if self.balance > self.peak:
            self.peak = self.balance
        if self.balance >= TARGET_BALANCE and not self.passed:
            self.passed = True
            self._note("pass target reached - no new trades until reset", "win")
        if self.balance <= self.floor and not self.failed:
            self.failed = True
            self._note("drawdown floor breached - bot stopped", "loss")

    def _set_status(self):
        if not self.enabled:
            self.status = "paused"
        elif self.failed:
            self.status = "stopped - drawdown floor breached"
        elif self.pos:
            self.status = "in trade: " + ", ".join(p.label + " " + p.strat for p in self.pos.values())
        elif self.passed or self.equity() >= TARGET_BALANCE:
            self.status = "Lucid target passed (+$3,000) - waiting for reset"
            self.passed = True
        elif self.day_pnl <= -DAILY_LOSS_LIMIT:
            self.status = "daily loss guard active - no new trades today"
        elif not self._real_entry_window_ok():
            self.status = "outside real entry window - waiting for next NY session"
        else:
            self.status = "live - scanning Lucid 5-strategy ES/NQ/CL basket"

    def equity(self) -> float:
        eq = self.balance
        for key, p in self.pos.items():
            px = self.prices.get(key, p.entry)
            side = 1 if p.side == "long" else -1
            eq += self._pnl(side, p.entry, px, p.qty, p.micro_pv)
        return eq

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._note("bot ENABLED - Lucid 5-strategy basket" if self.enabled else "bot PAUSED")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self):
        self.enabled = True
        self.balance = START_BALANCE
        self.peak = START_BALANCE
        self.locked = False
        self.floor = START_BALANCE - MAX_DRAWDOWN
        self.day_key = ""
        self.day_pnl = 0.0
        self.passed = False
        self.failed = False
        self.daily_stopped_day = ""
        self.pos = {}
        self.prices = {}
        self.fired_keys = set()
        self.warning_keys = set()
        self.history = []
        self.log = []
        self.setups = {}
        self._last_bar_ts = {}
        self._last_bar_sig = {}
        self._primed_keys = set()
        self.feed_details = []
        self._note("bot reset and enabled (Lucid paper account back to $50,000)")
        self._save()
        return {"ok": True, "enabled": True}

    def state(self):
        eq = self.equity()
        wins = sum(1 for h in self.history if h.get("pnl", 0) > 0)
        live_status = str(self.live_feed_status or "")
        source_match_required = bool(getattr(config, "LUCID_REQUIRE_BACKTEST_SOURCE_MATCH", True))
        source_verified = _lucid_source_verified(live_status, source_match_required)
        exact_realtime_ready, exact_realtime_status = _lucid_exact_realtime_state(live_status, self.data_error)
        live_feed_family = (
            BACKTEST_FEED_FAMILY
            if source_verified
            else
            LIVE_TRADINGVIEW_FEED_FAMILY
            if "TradingView websocket" in live_status
            else (live_status or "unknown")
        )
        positions = []
        for p in self.pos.values():
            px = self.prices.get(p.key, p.entry)
            side = 1 if p.side == "long" else -1
            up = self._pnl(side, p.entry, px, p.qty, p.micro_pv)
            positions.append({
                "mkt": p.label + "." + p.strat,
                "side": p.side,
                "entry": round(p.entry, 2),
                "qty": p.qty,
                "stop": round(p.stop, 2),
                "tp1": round(p.tp1, 2),
                "tp2": round(p.target, 2),
                "pnl": round(up + p.realized, 2),
                "pnl_R": round((up + p.realized) / max(p.risk_usd, 1e-9), 2),
                "news": p.note + (" - runner at breakeven" if p.partial_done else ""),
            })
        setups = []
        for key, c in COMPONENTS.items():
            snap = self.setups.get(key, {})
            setups.append({
                "mkt": c["label"],
                "name": c["name"],
                "status": snap.get("status", "scanning"),
                "price": round(self.prices[key], 2) if key in self.prices else None,
            })
        return {
            "running": True,
            "enabled": self.enabled,
            "name": self.NAME,
            "strategy_version": STRATEGY_VERSION,
            "strategy_fingerprint": STRATEGY_FINGERPRINT,
            "status": self.status,
            "symbols": "ES + NQ + CL micros",
            "timeframe": "ES/NQ 3m, CL 5m, NQ/CL 30m",
            "balance": round(self.balance, 2),
            "equity": round(eq, 2),
            "start_balance": START_BALANCE,
            "total_pnl": round(eq - START_BALANCE, 2),
            "total_pnl_pct": round((eq / START_BALANCE - 1.0) * 100.0, 2),
            "apex_target": TARGET_BALANCE,
            "target_left": round(max(0.0, TARGET_BALANCE - eq), 2),
            "floor": round(self.floor, 2),
            "drawdown_room": round(eq - self.floor, 2),
            "trail_locked": False,
            "phase": "target passed" if self.passed else ("floor breached" if self.failed else "Lucid eval mode"),
            "risk_per_trade": RISK_USD,
            "day_pnl": round(self.day_pnl, 2),
            "daily_loss_limit": DAILY_LOSS_LIMIT,
            "max_micros": MAX_MICROS,
            "trades": len(self.history),
            "wins": wins,
            "positions": positions,
            "setups": setups,
            "history": self.history[:80],
            "log": self.log[:35],
            "data_error": self.data_error,
            "passed": self.passed,
            "failed": self.failed,
            "telegram_enabled": self.telegram_enabled,
            "telegram_ready": self._telegram_ready(),
            "telegram_target": (
                "Telegram bot chat"
                if self._telegram_bot_token and self._telegram_chat_id
                else ("Saved Messages" if self._telegram_target == "me" else self._telegram_target)
            ),
            "telegram_error": self._last_alert_error,
            "next_signal_window": self._next_signal_window(),
            "live_feed_status": self.live_feed_status,
            "feed_details": getattr(self, "feed_details", []),
            "backtest_feed_family": BACKTEST_FEED_FAMILY,
            "backtest_source_symbols": BACKTEST_SOURCE_SYMBOLS,
            "live_feed_family": live_feed_family,
            "source_match_required": source_match_required,
            "source_match": source_verified if source_match_required else not bool(_lucid_source_block_reason(live_status)),
            "exact_realtime_ready": exact_realtime_ready,
            "exact_realtime_status": exact_realtime_status,
            "realtime_entry_required": self._requires_exact_realtime_entry(),
            "realtime_entry_ready": self._exact_realtime_entry_ok(),
            "realtime_entry_status": getattr(self, "realtime_entry_status", exact_realtime_status),
            "backtest_note": (
                "Lucid 50K Monthly Pass Basket test: ES 3m VWAP2.5 + NQ 3m VWAP2.5 + "
                "CL 5m VWAP2.5 + NQ 30m TurtleSoup10 + CL 30m NR7; $200 risk, "
                "$1,200 daily stop, exact R-unit paper sizing, stops after +$3,000. The saved "
                "36/36 report belongs to Dukascopy tick-derived proxy candles "
                "(USA500IDXUSD/USATECHIDXUSD/LIGHTCMDUSD); strict source matching blocks "
                "non-matching CME/Yahoo live feeds."
            ),
        }
