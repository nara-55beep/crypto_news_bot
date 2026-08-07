"""
Live ICT visual scanner for the dashboard.

This does not trade. It turns the broad ICT confluence model into chart overlays:
killzones, liquidity pools, sweeps, MSS, FVG/OB zones, and entry/stop/target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import math
import os
import time


NY = ZoneInfo("America/New_York")
STATE_PATH = os.path.join("data", "ict_lab_state.json")


def _round(x, n=2):
    try:
        return round(float(x), n)
    except Exception:
        return None


def _ny(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), timezone.utc).astimezone(NY)


def _mins(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _killzone(ts: int) -> str | None:
    d = _ny(ts)
    if d.weekday() >= 5:
        return None
    m = _mins(d)
    if 2 * 60 <= m < 5 * 60:
        return "London"
    if 9 * 60 + 30 <= m < 11 * 60 + 30:
        return "NY AM"
    if 13 * 60 + 30 <= m < 15 * 60 + 30:
        return "NY PM"
    return None


def _tr(cur: dict, prev_close: float) -> float:
    return max(
        cur["high"] - cur["low"],
        abs(cur["high"] - prev_close),
        abs(cur["low"] - prev_close),
    )


def _atr(candles: list[dict], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    vals: list[float] = []
    for i in range(1, len(candles)):
        vals.append(_tr(candles[i], candles[i - 1]["close"]))
        if len(vals) >= period:
            out[i] = sum(vals[-period:]) / period
    return out


def _swings(candles: list[dict], side: str, w: int = 2) -> list[int]:
    out: list[int] = []
    if len(candles) < w * 2 + 1:
        return out
    key = "high" if side == "high" else "low"
    for i in range(w, len(candles) - w):
        v = candles[i][key]
        if side == "high":
            ok = all(v > candles[i - j][key] for j in range(1, w + 1)) and all(
                v > candles[i + j][key] for j in range(1, w + 1)
            )
        else:
            ok = all(v < candles[i - j][key] for j in range(1, w + 1)) and all(
                v < candles[i + j][key] for j in range(1, w + 1)
            )
        if ok:
            out.append(i)
    return out


@dataclass
class Pool:
    name: str
    price: float
    side: int       # 1 = buy-side liquidity above price, -1 = sell-side liquidity below price
    kind: str
    priority: int = 1
    time: int | None = None

    def as_level(self) -> dict:
        return {
            "name": self.name,
            "price": _round(self.price, 2),
            "side": self.side,
            "kind": self.kind,
            "priority": self.priority,
            "color": "#d8a84a" if self.side > 0 else "#4fb7ff",
        }


class ICTLab:
    def __init__(self, path: str = STATE_PATH):
        self.path = path
        self.enabled = False
        self.log: list[dict] = []
        self._last_sig = ""
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.enabled = bool(d.get("enabled", False))
            self.log = list(d.get("log", []))[:80]
        except Exception:
            self.enabled = False
            self.log = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"enabled": self.enabled, "log": self.log[:80]}, f)
        except Exception:
            pass

    def _note(self, msg: str, kind: str = "info"):
        self.log.insert(0, {"t": time.time(), "msg": msg, "kind": kind})
        self.log = self.log[:80]

    def set_enabled(self, enabled: bool) -> dict:
        self.enabled = bool(enabled)
        self._note("ICT visual bot enabled" if self.enabled else "ICT visual bot paused")
        self._save()
        return {"ok": True, "enabled": self.enabled}

    def reset(self) -> dict:
        self.log = []
        self._last_sig = ""
        self._note("ICT visual bot reset")
        self._save()
        return {"ok": True}

    def state(self, raw_candles: list[dict], price: float | None = None) -> dict:
        candles = self._normalize(raw_candles)
        if not candles:
            return {
                "ok": False,
                "enabled": self.enabled,
                "phase": "OFF" if not self.enabled else "SCAN",
                "error": "no candles",
                "candles": [],
                "levels": [],
                "zones": [],
                "markers": [],
                "log": self.log[:40],
            }

        analysis = self._analyse(candles)
        if not self.enabled:
            analysis["phase"] = "OFF"
            analysis["event"] = "paused - context levels still drawn"
            analysis["setup"] = None

        sig = self._signature(analysis)
        if self.enabled and sig and sig != self._last_sig:
            self._last_sig = sig
            self._note(analysis.get("event") or analysis.get("phase", "SCAN"), analysis.get("kind", "info"))
            self._save()

        analysis.update({
            "ok": True,
            "enabled": self.enabled,
            "price": _round(price or candles[-1]["close"], 2),
            "candles": candles,
            "log": self.log[:40],
            "now": time.time(),
        })
        return analysis

    def _normalize(self, raw: list[dict]) -> list[dict]:
        out: list[dict] = []
        for c in raw or []:
            try:
                t = int(c.get("time") or c.get("t"))
                out.append({
                    "time": t,
                    "open": float(c.get("open", c.get("o"))),
                    "high": float(c.get("high", c.get("h"))),
                    "low": float(c.get("low", c.get("l"))),
                    "close": float(c.get("close", c.get("c"))),
                })
            except Exception:
                continue
        out.sort(key=lambda x: x["time"])
        dedup: list[dict] = []
        for c in out:
            if dedup and dedup[-1]["time"] == c["time"]:
                dedup[-1] = c
            else:
                dedup.append(c)
        return dedup[-1500:]

    def _signature(self, analysis: dict) -> str:
        setup = analysis.get("setup") or {}
        return "|".join([
            str(analysis.get("phase")),
            str(setup.get("side")),
            str(setup.get("sweep_time")),
            str(setup.get("mss_time")),
            str(setup.get("entry")),
            str(setup.get("exit_reason")),
        ])

    def _analyse(self, candles: list[dict]) -> dict:
        atr = _atr(candles)
        highs = _swings(candles, "high")
        lows = _swings(candles, "low")
        levels, pools, day_mid = self._context_levels(candles, highs, lows)
        zones = self._killzone_zones(candles)
        markers: list[dict] = []
        concepts = [
            {"name": "Killzones", "state": _killzone(candles[-1]["time"]) or "waiting"},
            {"name": "PDH / PDL", "state": "drawn" if any(p.kind in ("pdh", "pdl") for p in pools) else "needs more data"},
            {"name": "Opening range", "state": "drawn" if any(p.kind in ("orh", "orl") for p in pools) else "waiting"},
            {"name": "Asia / premarket range", "state": "drawn" if any(p.kind.startswith("asia") for p in pools) else "waiting"},
            {"name": "Equal highs / lows", "state": "drawn" if any(p.kind in ("eqh", "eql") for p in pools) else "scanning"},
            {"name": "Sweep", "state": "scanning"},
            {"name": "MSS + displacement", "state": "waiting"},
            {"name": "FVG / OB", "state": "waiting"},
            {"name": "Premium / discount", "state": "checking"},
            {"name": "Entry / stop / target", "state": "waiting"},
        ]
        current_kz = _killzone(candles[-1]["time"])
        base = {
            "phase": "SCAN",
            "bias": None,
            "killzone": current_kz,
            "levels": [p.as_level() for p in levels],
            "zones": zones,
            "markers": markers,
            "setup": None,
            "event": "scanning liquidity pools" if current_kz else "outside killzone - drawing context",
            "kind": "info",
            "concepts": concepts,
        }

        sweep = self._latest_sweep(candles, pools, atr)
        if not sweep:
            return base

        side = sweep["side"]
        bias = "LONG" if side == "long" else "SHORT"
        concepts[5]["state"] = f"{sweep['pool'].name} swept"
        markers.append({
            "time": candles[sweep["idx"]]["time"],
            "position": "belowBar" if side == "long" else "aboveBar",
            "shape": "arrowUp" if side == "long" else "arrowDown",
            "color": "#19c37d" if side == "long" else "#ff4d5f",
            "text": "sweep",
        })
        setup = {
            "side": side,
            "pool": sweep["pool"].name,
            "pool_price": _round(sweep["pool"].price, 2),
            "raid": _round(sweep["raid"], 2),
            "sweep_time": candles[sweep["idx"]]["time"],
            "score": 2 + sweep["pool"].priority,
            "rr": None,
            "tradable": False,
        }
        base.update({
            "phase": "SWEPT",
            "bias": bias,
            "setup": setup,
            "event": f"{bias} idea: swept {sweep['pool'].name}",
            "kind": "open",
        })

        mss = self._find_mss(candles, highs, lows, atr, sweep)
        if not mss:
            return base
        concepts[6]["state"] = "confirmed"
        markers.append({
            "time": candles[mss["idx"]]["time"],
            "position": "aboveBar" if side == "long" else "belowBar",
            "shape": "circle",
            "color": "#f2b84b",
            "text": "MSS",
        })
        levels.append(Pool("MSS", mss["level"], 1 if side == "long" else -1, "mss", 3))
        setup.update({"mss": _round(mss["level"], 2), "mss_time": candles[mss["idx"]]["time"]})

        fvg = self._find_fvg(candles, atr, sweep["idx"], mss["idx"], side)
        ob = self._find_ob(candles, sweep["idx"], mss["idx"], side)
        if fvg:
            concepts[7]["state"] = "FVG found"
            zones.append(fvg)
        if ob:
            concepts[7]["state"] = "FVG + OB found" if fvg else "OB found"
            zones.append(ob)
        if not fvg and not ob:
            base["event"] = f"{bias} MSS confirmed, waiting for FVG/OB"
            return base

        entry_src = fvg or ob
        entry = (entry_src["top"] + entry_src["bottom"]) / 2.0
        a = atr[mss["idx"]] or (candles[mss["idx"]]["high"] - candles[mss["idx"]]["low"]) or 1.0
        if side == "long":
            stop = sweep["raid"] - max(a * 0.05, candles[-1]["close"] * 0.00005)
            target = self._opposite_target(pools, entry, side) or (entry + 2.0 * (entry - stop))
            rr = (target - entry) / max(entry - stop, 1e-9)
            pd_ok = day_mid is None or entry < day_mid
        else:
            stop = sweep["raid"] + max(a * 0.05, candles[-1]["close"] * 0.00005)
            target = self._opposite_target(pools, entry, side) or (entry - 2.0 * (stop - entry))
            rr = (entry - target) / max(stop - entry, 1e-9)
            pd_ok = day_mid is None or entry > day_mid

        score = setup["score"]
        if fvg:
            score += 1
        if ob:
            score += 1
        if pd_ok:
            score += 1
            concepts[8]["state"] = "ok"
        else:
            concepts[8]["state"] = "reject"
        if rr >= 1.5:
            score += 1

        setup.update({
            "entry": _round(entry, 2),
            "stop": _round(stop, 2),
            "target": _round(target, 2),
            "rr": _round(rr, 2),
            "score": score,
            "entry_model": entry_src["kind"].upper(),
            "premium_discount_ok": bool(pd_ok),
        })
        levels.extend([
            Pool("Entry", entry, 0, "entry", 5),
            Pool("Stop", stop, 0, "stop", 5),
            Pool("Target", target, 0, "target", 5),
        ])

        if not pd_ok or rr < 1.5 or score < 4:
            setup["rejected"] = True
            base.update({
                "phase": "SCAN",
                "levels": [p.as_level() for p in levels],
                "zones": zones,
                "event": f"{bias} setup rejected: PD={pd_ok}, RR={rr:.2f}, score={score}",
                "kind": "skip",
            })
            return base

        concepts[9]["state"] = "armed"
        fill = self._find_fill_or_exit(candles, mss["idx"], side, entry, stop, target)
        phase = "PENDING"
        event = f"{bias} pending limit at {entry:,.1f}"
        kind = "open"
        if fill and fill.get("filled"):
            markers.append({
                "time": candles[fill["fill_idx"]]["time"],
                "position": "belowBar" if side == "long" else "aboveBar",
                "shape": "circle",
                "color": "#19c37d" if side == "long" else "#ff4d5f",
                "text": "entry",
            })
            phase = "FILLED"
            event = f"{bias} filled @ {entry:,.1f}"
            setup["fill_time"] = candles[fill["fill_idx"]]["time"]
            if fill.get("exit_idx") is not None:
                reason = fill["exit_reason"]
                markers.append({
                    "time": candles[fill["exit_idx"]]["time"],
                    "position": "aboveBar" if reason == "tp" else "belowBar",
                    "shape": "arrowDown" if reason == "tp" else "arrowUp",
                    "color": "#19c37d" if reason == "tp" else "#ff4d5f",
                    "text": reason.upper(),
                })
                phase = "SCAN"
                kind = "win" if reason == "tp" else "loss"
                event = f"{bias} setup closed by {reason.upper()}"
                setup["exit_reason"] = reason
                setup["exit_time"] = candles[fill["exit_idx"]]["time"]
        elif mss["idx"] >= len(candles) - 2:
            phase = "ARMED"
            event = f"{bias} armed on fresh MSS"

        setup["tradable"] = phase in ("ARMED", "PENDING", "FILLED")
        base.update({
            "phase": phase,
            "bias": bias,
            "levels": [p.as_level() for p in levels],
            "zones": zones,
            "markers": markers,
            "setup": setup,
            "event": event,
            "kind": kind,
            "concepts": concepts,
        })
        return base

    def _context_levels(
        self, candles: list[dict], highs: list[int], lows: list[int]
    ) -> tuple[list[Pool], list[Pool], float | None]:
        pools: list[Pool] = []
        last_day = _ny(candles[-1]["time"]).date()
        by_day: dict[object, list[dict]] = {}
        for c in candles:
            by_day.setdefault(_ny(c["time"]).date(), []).append(c)
        prior_days = [d for d in sorted(by_day) if d < last_day]
        day_mid = None
        if prior_days:
            prev = by_day[prior_days[-1]]
            pdh = max(x["high"] for x in prev)
            pdl = min(x["low"] for x in prev)
            day_mid = (pdh + pdl) / 2.0
            pools.extend([
                Pool("PDH", pdh, 1, "pdh", 3),
                Pool("PDL", pdl, -1, "pdl", 3),
                Pool("Prev EQ", day_mid, 0, "eq", 1),
            ])

        cur = by_day.get(last_day, [])
        or_c = [c for c in cur if 9 * 60 + 30 <= _mins(_ny(c["time"])) < 9 * 60 + 45]
        if or_c:
            pools.extend([
                Pool("OR high", max(c["high"] for c in or_c), 1, "orh", 2),
                Pool("OR low", min(c["low"] for c in or_c), -1, "orl", 2),
            ])

        asia = self._session(candles, 20, 0, last_day)
        if asia:
            pools.extend([
                Pool("Asia high", max(c["high"] for c in asia), 1, "asiah", 2),
                Pool("Asia low", min(c["low"] for c in asia), -1, "asial", 2),
            ])

        eq_levels = self._equal_levels(candles, highs, lows)
        pools.extend(eq_levels)

        for i in highs[-4:]:
            pools.append(Pool("swing high", candles[i]["high"], 1, "swingh", 1, candles[i]["time"]))
        for i in lows[-4:]:
            pools.append(Pool("swing low", candles[i]["low"], -1, "swingl", 1, candles[i]["time"]))

        deduped: list[Pool] = []
        for p in sorted(pools, key=lambda x: (-x.priority, x.name)):
            if p.price <= 0 or math.isnan(p.price):
                continue
            tol = max(abs(p.price) * 0.00008, 0.01)
            if any(abs(p.price - q.price) <= tol and p.side == q.side for q in deduped):
                continue
            deduped.append(p)
        return deduped, [p for p in deduped if p.side in (-1, 1)], day_mid

    def _session(self, candles: list[dict], start_hour: int, end_hour: int, day) -> list[dict]:
        start_day = day - timedelta(days=1) if start_hour > end_hour else day
        start = datetime.combine(start_day, dtime(start_hour, 0), tzinfo=NY)
        end = datetime.combine(day, dtime(end_hour, 0), tzinfo=NY)
        return [c for c in candles if start <= _ny(c["time"]) < end]

    def _equal_levels(self, candles: list[dict], highs: list[int], lows: list[int]) -> list[Pool]:
        px = candles[-1]["close"]
        tol = max(px * 0.00035, 0.5)
        out: list[Pool] = []

        def add_pairs(idxs: list[int], key: str, side: int, name: str, kind: str):
            recent = idxs[-30:]
            made: list[float] = []
            for a in range(len(recent)):
                for b in range(a + 1, len(recent)):
                    i, j = recent[a], recent[b]
                    p = (candles[i][key] + candles[j][key]) / 2.0
                    if abs(candles[i][key] - candles[j][key]) <= tol and all(abs(p - m) > tol for m in made):
                        made.append(p)
                        out.append(Pool(name, p, side, kind, 2, candles[j]["time"]))
                        if len(made) >= 4:
                            return

        add_pairs(highs, "high", 1, "equal highs", "eqh")
        add_pairs(lows, "low", -1, "equal lows", "eql")
        return out

    def _killzone_zones(self, candles: list[dict]) -> list[dict]:
        zones: list[dict] = []
        cur = None
        start = None
        prev = None
        for c in candles:
            kz = _killzone(c["time"])
            if kz != cur:
                if cur and start and prev:
                    zones.append({"kind": "killzone", "name": cur, "time1": start, "time2": prev})
                cur = kz
                start = c["time"] if kz else None
            prev = c["time"]
        if cur and start and prev:
            zones.append({"kind": "killzone", "name": cur, "time1": start, "time2": prev})
        return zones[-12:]

    def _latest_sweep(self, candles: list[dict], pools: list[Pool], atr: list[float | None]) -> dict | None:
        best = None
        start = max(8, len(candles) - 180)
        now_price = candles[-1]["close"]
        for i in range(start, len(candles)):
            c = candles[i]
            if not _killzone(c["time"]):
                continue
            a = atr[i] or max(c["high"] - c["low"], now_price * 0.0002)
            pen = max(a * 0.04, now_price * 0.00004)
            for p in pools:
                if abs(p.price - c["close"]) / max(c["close"], 1) > 0.02:
                    continue
                if p.side < 0 and c["low"] < p.price - pen and c["close"] > p.price:
                    cand = {"idx": i, "side": "long", "pool": p, "raid": c["low"]}
                    if best is None or (i, p.priority) >= (best["idx"], best["pool"].priority):
                        best = cand
                if p.side > 0 and c["high"] > p.price + pen and c["close"] < p.price:
                    cand = {"idx": i, "side": "short", "pool": p, "raid": c["high"]}
                    if best is None or (i, p.priority) >= (best["idx"], best["pool"].priority):
                        best = cand
        return best

    def _find_mss(
        self, candles: list[dict], highs: list[int], lows: list[int], atr: list[float | None], sweep: dict
    ) -> dict | None:
        idx = sweep["idx"]
        side = sweep["side"]
        prior = [x for x in (highs if side == "long" else lows) if idx - 45 <= x < idx]
        if prior:
            level_idx = prior[-1]
            level = candles[level_idx]["high" if side == "long" else "low"]
        else:
            look = candles[max(0, idx - 12):idx]
            if not look:
                return None
            level = max(x["high"] for x in look) if side == "long" else min(x["low"] for x in look)
        for j in range(idx + 1, len(candles)):
            c = candles[j]
            broke = c["close"] > level if side == "long" else c["close"] < level
            if not broke:
                continue
            a = atr[j] or max(c["high"] - c["low"], 1.0)
            rng = max(c["high"] - c["low"], 1e-9)
            body = abs(c["close"] - c["open"])
            tr = _tr(c, candles[j - 1]["close"])
            leg = abs(c["close"] - sweep["raid"])
            displaced = tr >= 0.8 * a or (body / rng >= 0.55 and leg >= 0.8 * a)
            if displaced:
                return {"idx": j, "level": level}
        return None

    def _find_fvg(
        self, candles: list[dict], atr: list[float | None], sweep_idx: int, mss_idx: int, side: str
    ) -> dict | None:
        found = None
        end = min(len(candles) - 1, mss_idx + 3)
        for i in range(max(2, sweep_idx), end + 1):
            a = atr[i] or max(candles[i]["high"] - candles[i]["low"], 1.0)
            min_gap = max(a * 0.02, candles[i]["close"] * 0.00002)
            if side == "long" and candles[i]["low"] > candles[i - 2]["high"] + min_gap:
                found = {
                    "kind": "fvg",
                    "name": "bullish FVG",
                    "side": "long",
                    "top": _round(candles[i]["low"], 2),
                    "bottom": _round(candles[i - 2]["high"], 2),
                    "time1": candles[i - 2]["time"],
                    "time2": candles[min(len(candles) - 1, i + 24)]["time"],
                }
            if side == "short" and candles[i]["high"] < candles[i - 2]["low"] - min_gap:
                found = {
                    "kind": "fvg",
                    "name": "bearish FVG",
                    "side": "short",
                    "top": _round(candles[i - 2]["low"], 2),
                    "bottom": _round(candles[i]["high"], 2),
                    "time1": candles[i - 2]["time"],
                    "time2": candles[min(len(candles) - 1, i + 24)]["time"],
                }
        return found

    def _find_ob(self, candles: list[dict], sweep_idx: int, mss_idx: int, side: str) -> dict | None:
        rng = range(mss_idx - 1, max(sweep_idx - 1, 0), -1)
        for i in rng:
            c = candles[i]
            bearish = c["close"] < c["open"]
            bullish = c["close"] > c["open"]
            if (side == "long" and bearish) or (side == "short" and bullish):
                return {
                    "kind": "ob",
                    "name": "order block",
                    "side": side,
                    "top": _round(c["high"], 2),
                    "bottom": _round(c["low"], 2),
                    "time1": c["time"],
                    "time2": candles[min(len(candles) - 1, i + 36)]["time"],
                }
        return None

    def _opposite_target(self, pools: list[Pool], entry: float, side: str) -> float | None:
        if side == "long":
            above = [p.price for p in pools if p.side > 0 and p.price > entry]
            return min(above) if above else None
        below = [p.price for p in pools if p.side < 0 and p.price < entry]
        return max(below) if below else None

    def _find_fill_or_exit(
        self, candles: list[dict], start_idx: int, side: str, entry: float, stop: float, target: float
    ) -> dict | None:
        filled = None
        expiry = start_idx + 24
        for i in range(start_idx + 1, min(len(candles), expiry + 1)):
            c = candles[i]
            if side == "long":
                if c["low"] <= entry <= c["high"]:
                    filled = i
                    break
            else:
                if c["low"] <= entry <= c["high"]:
                    filled = i
                    break
        if filled is None:
            return None
        for i in range(filled, len(candles)):
            c = candles[i]
            if side == "long":
                if c["low"] <= stop:
                    return {"filled": True, "fill_idx": filled, "exit_idx": i, "exit_reason": "sl"}
                if c["high"] >= target:
                    return {"filled": True, "fill_idx": filled, "exit_idx": i, "exit_reason": "tp"}
            else:
                if c["high"] >= stop:
                    return {"filled": True, "fill_idx": filled, "exit_idx": i, "exit_reason": "sl"}
                if c["low"] <= target:
                    return {"filled": True, "fill_idx": filled, "exit_idx": i, "exit_reason": "tp"}
        return {"filled": True, "fill_idx": filled}
