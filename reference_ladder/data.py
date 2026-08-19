"""Official Binance BTCUSDT one-minute archive loader.

Monthly ZIPs supply the long history efficiently; daily ZIPs and the public REST
endpoint extend it through the present. Downloaded bars live under ``data/`` and
are therefore runtime cache, never source-controlled research output.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pandas as pd
import requests


ARCHIVE = "https://data.binance.vision/data/spot"
REST = "https://api.binance.com/api/v3/klines"
DEFAULT_START = "2018-01-01"


class DataCoverageError(RuntimeError):
    pass


class BinanceMinuteLoader:
    def __init__(self, cache_dir: str | Path = "data/reference_ladder") -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "crypto-news-bot-reference-ladder/1.0"})

    @staticmethod
    def _parse_archive(content: bytes) -> pd.DataFrame:
        with ZipFile(BytesIO(content)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not names:
                raise DataCoverageError("Binance archive contains no CSV")
            raw = pd.read_csv(archive.open(names[0]), header=None, low_memory=False)
        if len(raw) and str(raw.iloc[0, 0]).lower() == "open_time":
            raw = raw.iloc[1:].reset_index(drop=True)
        if raw.shape[1] < 6:
            raise DataCoverageError("Binance archive has fewer than six kline columns")
        raw = raw.iloc[:, :6]
        raw.columns = ["open_time", "open", "high", "low", "close", "volume"]
        timestamps = pd.to_numeric(raw["open_time"], errors="coerce")
        finite = timestamps.dropna()
        if finite.empty:
            raise DataCoverageError("Binance archive has no valid timestamps")
        unit = "us" if float(finite.iloc[0]) >= 100_000_000_000_000 else "ms"
        frame = pd.DataFrame(index=pd.to_datetime(timestamps, unit=unit, utc=True))
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(raw[column], errors="coerce").to_numpy()
        return frame.dropna(subset=["open", "high", "low", "close"])

    def _get(self, url: str, *, missing_ok: bool = False) -> bytes | None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=90)
                if response.status_code == 404 and missing_ok:
                    return None
                response.raise_for_status()
                return response.content
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise DataCoverageError(f"download failed for {url}: {last_error}")

    def _cached_archive(self, cache_name: str, url: str, *, refresh: bool,
                        missing_ok: bool = False) -> pd.DataFrame | None:
        path = self.cache_dir / f"{cache_name}.csv.gz"
        if path.exists() and not refresh:
            frame = pd.read_csv(path, index_col=0)
            frame.index = pd.to_datetime(frame.index, utc=True, format="mixed")
            return frame
        payload = self._get(url, missing_ok=missing_ok)
        if payload is None:
            return None
        frame = self._parse_archive(payload)
        frame.to_csv(path, compression="gzip")
        return frame

    def _month(self, month: pd.Timestamp, refresh: bool) -> pd.DataFrame | None:
        stamp = month.strftime("%Y-%m")
        url = f"{ARCHIVE}/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{stamp}.zip"
        return self._cached_archive(f"BTCUSDT-1m-{stamp}", url, refresh=refresh, missing_ok=True)

    def _day(self, day: date, refresh: bool) -> pd.DataFrame | None:
        stamp = day.isoformat()
        url = f"{ARCHIVE}/daily/klines/BTCUSDT/1m/BTCUSDT-1m-{stamp}.zip"
        return self._cached_archive(f"BTCUSDT-1m-{stamp}", url, refresh=refresh, missing_ok=True)

    @staticmethod
    def _month_starts(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
        first = start.to_period("M").to_timestamp()
        last = end.to_period("M").to_timestamp()
        return list(pd.date_range(first, last, freq="MS"))

    def _rest(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        rows: list = []
        while cursor < end_ms:
            response = self.session.get(
                REST,
                params={
                    "symbol": "BTCUSDT", "interval": "1m", "startTime": cursor,
                    "endTime": end_ms, "limit": 1000,
                },
                timeout=30,
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + 60_000
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1000:
                break
            time.sleep(0.05)
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        raw = pd.DataFrame(rows)
        frame = pd.DataFrame(index=pd.to_datetime(raw.iloc[:, 0], unit="ms", utc=True))
        for position, column in enumerate(("open", "high", "low", "close", "volume"), start=1):
            frame[column] = pd.to_numeric(raw.iloc[:, position], errors="coerce").to_numpy()
        return frame.dropna(subset=["open", "high", "low", "close"])

    def load(self, start: str = DEFAULT_START, end: str = "", *,
             refresh: bool = False) -> pd.DataFrame:
        start_ts = pd.Timestamp(start or DEFAULT_START)
        start_ts = start_ts.tz_localize("UTC") if start_ts.tzinfo is None else start_ts.tz_convert("UTC")
        now = pd.Timestamp(datetime.now(timezone.utc)).floor("min")
        if end:
            parsed_end = pd.Timestamp(end)
            if parsed_end.tzinfo is None:
                parsed_end = parsed_end.tz_localize("UTC")
            else:
                parsed_end = parsed_end.tz_convert("UTC")
            # A date-only end is inclusive through that UTC day.
            if len(str(end).strip()) <= 10:
                parsed_end += pd.Timedelta(days=1)
            end_ts = min(parsed_end, now)
        else:
            end_ts = now
        if start_ts >= end_ts:
            raise ValueError("start must precede end")

        current_month = now.tz_localize(None).to_period("M").to_timestamp().tz_localize("UTC")
        frames: list[pd.DataFrame] = []
        missing: list[str] = []
        for month_naive in self._month_starts(start_ts.tz_localize(None), end_ts.tz_localize(None)):
            month = month_naive.tz_localize("UTC")
            if month < current_month:
                frame = self._month(month_naive, refresh)
                if frame is not None:
                    frames.append(frame)
                    continue
                # A not-yet-published month is reconstructed from daily archives.
                next_month = month + pd.DateOffset(months=1)
                day = month.date()
                while day < min(next_month.date(), end_ts.date()):
                    daily = self._day(day, refresh)
                    if daily is None:
                        missing.append(day.isoformat())
                    else:
                        frames.append(daily)
                    day += timedelta(days=1)
            else:
                day = max(month.date(), start_ts.date())
                yesterday = min(end_ts.date(), now.date())
                while day < yesterday:
                    daily = self._day(day, refresh)
                    if daily is None:
                        missing.append(day.isoformat())
                    else:
                        frames.append(daily)
                    day += timedelta(days=1)

        rest_start = max(start_ts, now.floor("D"))
        if frames:
            latest = max(frame.index.max() for frame in frames)
            rest_start = max(rest_start, latest + pd.Timedelta(minutes=1))
        if rest_start < end_ts:
            try:
                recent = self._rest(rest_start, end_ts)
                if len(recent):
                    frames.append(recent)
            except Exception as exc:
                missing.append(f"REST {rest_start.isoformat()}: {type(exc).__name__}: {exc}")

        if not frames:
            raise DataCoverageError("no BTCUSDT one-minute bars could be loaded")
        frame = pd.concat(frames).sort_index()
        duplicates = int(frame.index.duplicated(keep="last").sum())
        frame = frame[~frame.index.duplicated(keep="last")]
        frame = frame[(frame.index >= start_ts) & (frame.index < end_ts)]
        if len(frame) < 200:
            raise DataCoverageError(f"only {len(frame)} BTCUSDT one-minute bars were loaded")
        valid = (
            (frame["low"] <= frame["high"])
            & frame["open"].between(frame["low"], frame["high"])
            & frame["close"].between(frame["low"], frame["high"])
            & (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
        )
        invalid = int((~valid).sum())
        frame = frame[valid]
        gaps = frame.index.to_series().diff().dropna()
        gap_events = int((gaps > pd.Timedelta(minutes=1)).sum())
        gap_minutes = gaps / pd.Timedelta(minutes=1)
        missing_minutes = int((gap_minutes[gap_minutes > 1.0] - 1.0).sum())
        quality = {
            "duplicates_removed": duplicates,
            "invalid_bars_removed": invalid,
            "gap_events": gap_events,
            "missing_minutes": missing_minutes,
            "missing_archives": missing,
            "coverage_start": frame.index[0].isoformat(),
            "coverage_end": frame.index[-1].isoformat(),
        }
        frame.attrs["data_source"] = "official Binance BTCUSDT spot monthly/daily archives plus public REST"
        frame.attrs["data_quality"] = quality
        frame.attrs["reference_ladder_normalized"] = True
        return frame
