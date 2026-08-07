import math
from apex_signal_extra import EXTRA_SIGNAL_NAMES, extra_signal_for


def signal_for(g, i, peer, cfg, state, or_hi, or_lo, or_range, or_atr):
    row = g[i]
    prev = g[i - 1] if i > 0 else row
    if not indicators_ok(row) or (peer is not None and not indicators_ok(peer)):
        return 0

    def side_allowed(s):
        return cfg["side"] == "both" or (cfg["side"] == "long" and s > 0) or (cfg["side"] == "short" and s < 0)

    def filt_ok(s):
        ema_ok = (row["close"] > row["vwap"] and row["ema9"] >= row["ema20"]) if s > 0 else (row["close"] < row["vwap"] and row["ema9"] <= row["ema20"])
        peer_ok = True if peer is None else ((peer["close"] > peer["vwap"] and peer["ema9"] >= peer["ema20"]) if s > 0 else (peer["close"] < peer["vwap"] and peer["ema9"] <= peer["ema20"]))
        if cfg["filter"] == "none":
            return True
        if cfg["filter"] == "ema_peer":
            return ema_ok and peer_ok
        if cfg["filter"] == "ema":
            return ema_ok
        return peer_ok

    def finish(s):
        return s if side_allowed(s) and filt_ok(s) else 0

    name = cfg["name"]
    tol = cfg["sweep_atr"] * (row["atr14"] or or_atr)
    atr = row["atr14"] or or_atr
    or_mid = (or_hi + or_lo) / 2.0

    def close_pos(x):
        rng = max(x["high"] - x["low"], 1e-9)
        return (x["close"] - x["low"]) / rng

    def not_daily_opposed(s):
        d = daily_trend_side(row)
        return d == 0 or d == s

    def recent_vwap_bias(s, bars, need):
        lookback = g[max(0, i - bars): i]
        if len(lookback) < need:
            return False
        if s > 0:
            return sum(1 for x in lookback if x["close"] > x["vwap"] and x["ema9"] >= x["ema20"]) >= need
        return sum(1 for x in lookback if x["close"] < x["vwap"] and x["ema9"] <= x["ema20"]) >= need

    def quality_peer_ok(s):
        return peer is None or peer_confirms(peer, s)

    if name in EXTRA_SIGNAL_NAMES:
        ctx = locals()
        ctx.update({"rejection": rejection, "rel_vol_ok": rel_vol_ok, "peer_confirms": peer_confirms, "peer_diverges": peer_diverges})
        return extra_signal_for(ctx)

    if name == "orb_break":
        if prev["close"] <= or_hi and row["close"] > or_hi and row["close"] > row["open"]:
            return finish(1)
        if prev["close"] >= or_lo and row["close"] < or_lo and row["close"] < row["open"]:
            return finish(-1)
        return 0

    if name == "orb_pullback":
        if state["breakout"] == 0:
            if row["close"] > or_hi:
                state["breakout"] = 1
            elif row["close"] < or_lo:
                state["breakout"] = -1
            return 0
        s = state["breakout"]
        if s > 0 and row["low"] <= or_hi + tol and row["close"] > or_hi and rejection(row, 1):
            return finish(1)
        if s < 0 and row["high"] >= or_lo - tol and row["close"] < or_lo and rejection(row, -1):
            return finish(-1)
        return 0

    if name == "orb_fade":
        if row["high"] > or_hi + tol and row["close"] < or_hi and rejection(row, -1):
            return finish(-1)
        if row["low"] < or_lo - tol and row["close"] > or_lo and rejection(row, 1):
            return finish(1)
        return 0

    if name == "prior_sweep":
        if row["prev_high"] is not None and row["high"] > row["prev_high"] + tol and row["close"] < row["prev_high"] and rejection(row, -1):
            return finish(-1)
        if row["prev_low"] is not None and row["low"] < row["prev_low"] - tol and row["close"] > row["prev_low"] and rejection(row, 1):
            return finish(1)
        return 0

    if name == "vwap_reclaim":
        if prev["close"] <= prev["vwap"] and row["close"] > row["vwap"] and row["close"] > row["open"]:
            return finish(1)
        if prev["close"] >= prev["vwap"] and row["close"] < row["vwap"] and row["close"] < row["open"]:
            return finish(-1)
        return 0

    if name == "volume_orb_break":
        if not rel_vol_ok(row, 1.30):
            return 0
        if prev["close"] <= or_hi and row["close"] > or_hi and row["close"] > row["open"] and row["close"] > row["vwap"]:
            return finish(1)
        if prev["close"] >= or_lo and row["close"] < or_lo and row["close"] < row["open"] and row["close"] < row["vwap"]:
            return finish(-1)
        return 0

    if name == "volume_gap_go":
        if state["used_gap"] or row["prev_close"] is None or row["idx"] > cfg["or_bars"] + 3 or not rel_vol_ok(row, 1.25):
            return 0
        gap = row["day_open"] - row["prev_close"]
        if abs(gap) < 0.30 * (row["atr14"] or or_atr):
            return 0
        state["used_gap"] = True
        if gap > 0 and row["close"] > or_hi and row["close"] > row["vwap"]:
            return finish(1)
        if gap < 0 and row["close"] < or_lo and row["close"] < row["vwap"]:
            return finish(-1)
        return 0

    if name == "volume_vwap_drive":
        if not rel_vol_ok(row, 1.35):
            return 0
        if prev["close"] <= prev["vwap"] and row["close"] > row["vwap"] and row["close"] > row["open"] and row["ema9"] >= row["ema20"]:
            return finish(1)
        if prev["close"] >= prev["vwap"] and row["close"] < row["vwap"] and row["close"] < row["open"] and row["ema9"] <= row["ema20"]:
            return finish(-1)
        return 0

    if name == "peer_confirmed_break":
        if prev["close"] <= or_hi and row["close"] > or_hi and row["close"] > row["vwap"] and peer_confirms(peer, 1):
            return finish(1)
        if prev["close"] >= or_lo and row["close"] < or_lo and row["close"] < row["vwap"] and peer_confirms(peer, -1):
            return finish(-1)
        return 0

    if name == "peer_divergence_fade":
        if row["high"] > or_hi + tol and row["close"] < or_hi and rejection(row, -1) and peer_diverges(peer, 1):
            return finish(-1)
        if row["low"] < or_lo - tol and row["close"] > or_lo and rejection(row, 1) and peer_diverges(peer, -1):
            return finish(1)
        return 0

    if name == "quality_orb_continuation":
        if row["idx"] < cfg["or_bars"] or or_range > 1.05 * atr or not rel_vol_ok(row, 1.10):
            return 0
        if row.get("gap") is not None and abs(row["gap"]) > 0.75 * atr:
            return 0
        if (
            prev["close"] <= or_hi
            and row["close"] > or_hi
            and close_pos(row) >= 0.65
            and row["close"] > row["vwap"]
            and row["ema9"] >= row["ema20"]
            and not_daily_opposed(1)
            and quality_peer_ok(1)
        ):
            return finish(1)
        if (
            prev["close"] >= or_lo
            and row["close"] < or_lo
            and close_pos(row) <= 0.35
            and row["close"] < row["vwap"]
            and row["ema9"] <= row["ema20"]
            and not_daily_opposed(-1)
            and quality_peer_ok(-1)
        ):
            return finish(-1)
        return 0

    if name == "vwap_trend_pullback_q":
        if row["idx"] < max(cfg["or_bars"] + 3, 6) or not rel_vol_ok(row, 0.80):
            return 0
        made_up_extension = max(x["high"] for x in g[:i]) > or_hi + 0.25 * atr
        made_down_extension = min(x["low"] for x in g[:i]) < or_lo - 0.25 * atr
        pull_tol = 0.18 * atr
        if (
            made_up_extension
            and recent_vwap_bias(1, 7, 5)
            and row["low"] <= max(row["vwap"], row["ema20"]) + pull_tol
            and row["close"] > row["ema9"]
            and rejection(row, 1)
            and quality_peer_ok(1)
        ):
            return finish(1)
        if (
            made_down_extension
            and recent_vwap_bias(-1, 7, 5)
            and row["high"] >= min(row["vwap"], row["ema20"]) - pull_tol
            and row["close"] < row["ema9"]
            and rejection(row, -1)
            and quality_peer_ok(-1)
        ):
                return finish(-1)
        return 0

    if name == "multi_speed_trend_pullback_q":
        if row["idx"] < max(cfg["or_bars"] + 4, 7) or not rel_vol_ok(row, 0.85):
            return 0
        d = daily_trend_side(row)
        if d == 0:
            return 0
        if row.get("gap") is not None and abs(row["gap"]) > 0.55 * atr:
            return 0
        day_hi = max(x["high"] for x in g[:i])
        day_lo = min(x["low"] for x in g[:i])
        pull_tol = 0.22 * atr
        if (
            d > 0
            and day_hi > or_hi + 0.35 * atr
            and recent_vwap_bias(1, 9, 6)
            and row["low"] <= max(row["vwap"], row["ema20"]) + pull_tol
            and row["close"] > row["ema9"]
            and close_pos(row) >= 0.55
            and quality_peer_ok(1)
        ):
            return finish(1)
        if (
            d < 0
            and day_lo < or_lo - 0.35 * atr
            and recent_vwap_bias(-1, 9, 6)
            and row["high"] >= min(row["vwap"], row["ema20"]) - pull_tol
            and row["close"] < row["ema9"]
            and close_pos(row) <= 0.45
            and quality_peer_ok(-1)
        ):
            return finish(-1)
        return 0

    if name == "failed_break_peer_reversal_q":
        if row["idx"] < cfg["or_bars"] or not rel_vol_ok(row, 0.90):
            return 0
        if state.get("first_break", 0) == 0:
            if row["high"] > or_hi + tol:
                state["first_break"] = 1
            elif row["low"] < or_lo - tol:
                state["first_break"] = -1
            return 0
        if (
            state["first_break"] == 1
            and row["close"] < or_mid
            and row["close"] < row["vwap"]
            and rejection(row, -1)
            and (peer is None or peer_diverges(peer, 1) or peer_confirms(peer, -1))
        ):
            return finish(-1)
        if (
            state["first_break"] == -1
            and row["close"] > or_mid
            and row["close"] > row["vwap"]
            and rejection(row, 1)
            and (peer is None or peer_diverges(peer, -1) or peer_confirms(peer, 1))
        ):
            return finish(1)
        return 0

    if name == "compression_expansion_q":
        avg_rng = row.get("avg_daily_range")
        if row["idx"] < cfg["or_bars"] + 2 or avg_rng is None or row.get("prev_range") is None:
            return 0
        if row["prev_range"] > 0.80 * avg_rng or or_range > 1.10 * atr or not rel_vol_ok(row, 1.05):
            return 0
        day_hi = max(x["high"] for x in g[:i])
        day_lo = min(x["low"] for x in g[:i])
        if (
            prev["close"] <= day_hi
            and row["close"] > day_hi
            and row["close"] > row["vwap"]
            and row["ema9"] >= row["ema20"]
            and quality_peer_ok(1)
            and not_daily_opposed(1)
        ):
            return finish(1)
        if (
            prev["close"] >= day_lo
            and row["close"] < day_lo
            and row["close"] < row["vwap"]
            and row["ema9"] <= row["ema20"]
            and quality_peer_ok(-1)
            and not_daily_opposed(-1)
        ):
            return finish(-1)
        return 0

    if name == "balanced_prior_breakout_q":
        avg_rng = row.get("avg_daily_range")
        if row.get("prev_high") is None or row.get("prev_low") is None or row.get("prev_range") is None or avg_rng is None:
            return 0
        if row["idx"] < max(cfg["or_bars"] + 1, 4) or row["prev_range"] > 0.75 * avg_rng:
            return 0
        if row.get("gap") is not None and abs(row["gap"]) > 0.40 * atr:
            return 0
        if or_range > 1.00 * atr or not rel_vol_ok(row, 1.00):
            return 0
        if (
            prev["close"] <= row["prev_high"]
            and row["close"] > row["prev_high"]
            and close_pos(row) >= 0.65
            and row["close"] > row["vwap"]
            and row["ema9"] >= row["ema20"]
            and quality_peer_ok(1)
            and not_daily_opposed(1)
        ):
            return finish(1)
        if (
            prev["close"] >= row["prev_low"]
            and row["close"] < row["prev_low"]
            and close_pos(row) <= 0.35
            and row["close"] < row["vwap"]
            and row["ema9"] <= row["ema20"]
            and quality_peer_ok(-1)
            and not_daily_opposed(-1)
        ):
            return finish(-1)
        return 0

    if name == "daily_trend_orb":
        s = daily_trend_side(row)
        if s == 0:
            return 0
        if s > 0 and prev["close"] <= or_hi and row["close"] > or_hi and row["close"] > row["vwap"]:
            return finish(1)
        if s < 0 and prev["close"] >= or_lo and row["close"] < or_lo and row["close"] < row["vwap"]:
            return finish(-1)
        return 0

    if name == "daily_breakout_go":
        s = daily_trend_side(row)
        if s == 0 or not rel_vol_ok(row, 1.15):
            return 0
        if s > 0 and row.get("prior20_high") is not None and prev["close"] <= row["prior20_high"] and row["close"] > row["prior20_high"] and row["close"] > row["vwap"]:
            return finish(1)
        if s < 0 and row.get("prior20_low") is not None and prev["close"] >= row["prior20_low"] and row["close"] < row["prior20_low"] and row["close"] < row["vwap"]:
            return finish(-1)
        return 0

    if name == "daily_pullback_reclaim":
        s = daily_trend_side(row)
        if s == 0:
            return 0
        avg_rng = row.get("avg_daily_range") or (row["atr14"] or or_atr)
        gap = 0.0 if row.get("prev_close") is None else row["day_open"] - row["prev_close"]
        if s > 0 and gap < -0.15 * avg_rng and prev["close"] <= prev["vwap"] and row["close"] > row["vwap"] and row["close"] > row["open"]:
            return finish(1)
        if s < 0 and gap > 0.15 * avg_rng and prev["close"] >= prev["vwap"] and row["close"] < row["vwap"] and row["close"] < row["open"]:
            return finish(-1)
        return 0

    if name == "gap_fade":
        if state["used_gap"] or row["prev_close"] is None or row["idx"] > cfg["or_bars"] + 1:
            return 0
        gap = row["day_open"] - row["prev_close"]
        state["used_gap"] = True
        if gap > 0.75 * (row["atr14"] or or_atr) and row["close"] < row["day_open"]:
            return finish(-1)
        if gap < -0.75 * (row["atr14"] or or_atr) and row["close"] > row["day_open"]:
            return finish(1)
        return 0

    if name == "exhaustion_gap_fade_q":
        if row["prev_close"] is None or row["idx"] < cfg["or_bars"] or row["idx"] > cfg["or_bars"] + 6:
            return 0
        gap = row["day_open"] - row["prev_close"]
        if abs(gap) < 0.65 * atr:
            return 0
        high_so_far = max(x["high"] for x in g[: i + 1])
        low_so_far = min(x["low"] for x in g[: i + 1])
        if (
            gap > 0
            and high_so_far > or_hi + 0.20 * atr
            and row["close"] < row["vwap"]
            and row["ema9"] <= row["ema20"]
            and row["close"] < row["day_open"]
            and rejection(row, -1)
            and (peer is None or peer_diverges(peer, 1) or peer_confirms(peer, -1))
        ):
            return finish(-1)
        if (
            gap < 0
            and low_so_far < or_lo - 0.20 * atr
            and row["close"] > row["vwap"]
            and row["ema9"] >= row["ema20"]
            and row["close"] > row["day_open"]
            and rejection(row, 1)
            and (peer is None or peer_diverges(peer, -1) or peer_confirms(peer, 1))
        ):
            return finish(1)
        return 0

    if name == "gap_go":
        if state["used_gap"] or row["prev_close"] is None or row["idx"] > cfg["or_bars"] + 2:
            return 0
        gap = row["day_open"] - row["prev_close"]
        if abs(gap) < 0.35 * (row["atr14"] or or_atr):
            return 0
        state["used_gap"] = True
        if gap > 0 and row["close"] > or_hi and row["close"] > row["vwap"] and row["ema9"] >= row["ema20"]:
            return finish(1)
        if gap < 0 and row["close"] < or_lo and row["close"] < row["vwap"] and row["ema9"] <= row["ema20"]:
            return finish(-1)
        return 0

    if name == "failed_or_reversal":
        if state.get("first_break", 0) == 0:
            if row["high"] > or_hi + tol:
                state["first_break"] = 1
            elif row["low"] < or_lo - tol:
                state["first_break"] = -1
            return 0
        if state["first_break"] == 1 and row["close"] < (or_hi + or_lo) / 2.0 and row["close"] < row["vwap"]:
            return finish(-1)
        if state["first_break"] == -1 and row["close"] > (or_hi + or_lo) / 2.0 and row["close"] > row["vwap"]:
            return finish(1)
        return 0

    if name == "narrow_or_break":
        if or_range > 0.85 * or_atr:
            return 0
        if prev["close"] <= or_hi and row["close"] > or_hi and row["close"] > row["open"] and row["close"] > row["vwap"]:
            return finish(1)
        if prev["close"] >= or_lo and row["close"] < or_lo and row["close"] < row["open"] and row["close"] < row["vwap"]:
            return finish(-1)
        return 0

    if name == "prev_narrow_break":
        if row["prev_range"] is None or row["prev_range"] > 4.0 * (row["atr14"] or or_atr):
            return 0
        if prev["close"] <= or_hi and row["close"] > or_hi and row["close"] > row["vwap"] and row["ema9"] >= row["ema20"]:
            return finish(1)
        if prev["close"] >= or_lo and row["close"] < or_lo and row["close"] < row["vwap"] and row["ema9"] <= row["ema20"]:
            return finish(-1)
        return 0

    if name == "nr7_breakout":
        if state.get("nr7_used", False):
            return 0
        if not row.get("prev_is_nr7") or row.get("prev_high") is None or row.get("prev_low") is None:
            return 0
        tick = cfg.get("price_tick", 0.25)
        if row["high"] >= row["prev_high"] + tick:
            out = finish(1)
            state["nr7_used"] = bool(out)
            return out
        if row["low"] <= row["prev_low"] - tick:
            out = finish(-1)
            state["nr7_used"] = bool(out)
            return out
        return 0

    if name == "prev_day_continuation":
        if row["prev_open"] is None or row["prev_high"] is None or row["prev_low"] is None:
            return 0
        prev_rng = max(row["prev_high"] - row["prev_low"], 1e-9)
        prev_pos = (row["prev_close"] - row["prev_low"]) / prev_rng
        if row["prev_close"] > row["prev_open"] and prev_pos >= 0.62:
            if prev["close"] <= or_hi and row["close"] > or_hi and row["close"] > row["vwap"]:
                return finish(1)
        if row["prev_close"] < row["prev_open"] and prev_pos <= 0.38:
            if prev["close"] >= or_lo and row["close"] < or_lo and row["close"] < row["vwap"]:
                return finish(-1)
        return 0

    if name == "inside_day_break":
        if row["prev_high"] is None or row["prev_low"] is None or row["prev_range"] is None:
            return 0
        if row["prev_range"] > 3.0 * (row["atr14"] or or_atr):
            return 0
        if prev["close"] <= row["prev_high"] and row["close"] > row["prev_high"] and row["close"] > row["vwap"]:
            return finish(1)
        if prev["close"] >= row["prev_low"] and row["close"] < row["prev_low"] and row["close"] < row["vwap"]:
            return finish(-1)
        return 0

    if name == "wide_or_fade":
        if or_range < 0.90 * or_atr:
            return 0
        if row["high"] > or_hi + tol and row["close"] < or_hi and row["close"] < row["open"]:
            return finish(-1)
        if row["low"] < or_lo - tol and row["close"] > or_lo and row["close"] > row["open"]:
            return finish(1)
        return 0

    if name == "opening_drive_pullback":
        if len(g) <= cfg["or_bars"]:
            return 0
        drive_close = g[cfg["or_bars"] - 1]["close"]
        drive_pos = (drive_close - or_lo) / max(or_range, 1e-9)
        if state["breakout"] == 0:
            if drive_pos >= 0.78 and row["close"] > or_hi:
                state["breakout"] = 1
            elif drive_pos <= 0.22 and row["close"] < or_lo:
                state["breakout"] = -1
            return 0
        s = state["breakout"]
        if s > 0 and row["low"] <= max(or_hi, row["ema9"]) + tol and row["close"] > row["vwap"] and rejection(row, 1):
            return finish(1)
        if s < 0 and row["high"] >= min(or_lo, row["ema9"]) - tol and row["close"] < row["vwap"] and rejection(row, -1):
            return finish(-1)
        return 0

    if name == "range_expansion_continuation":
        if row["idx"] < max(cfg["or_bars"] + 2, 8):
            return 0
        if or_range > 1.05 * or_atr:
            return 0
        day_hi = max(x["high"] for x in g[: i + 1])
        day_lo = min(x["low"] for x in g[: i + 1])
        if row["close"] >= day_hi - 0.15 * (row["atr14"] or or_atr) and row["close"] > row["vwap"] and row["ema9"] >= row["ema20"]:
            return finish(1)
        if row["close"] <= day_lo + 0.15 * (row["atr14"] or or_atr) and row["close"] < row["vwap"] and row["ema9"] <= row["ema20"]:
            return finish(-1)
        return 0

    if name == "midday_vwap_trend":
        if row["minute"] < 10 * 60 + 30:
            return 0
        lookback = g[max(0, i - 6): i + 1]
        if len(lookback) < 4:
            return 0
        above = sum(1 for x in lookback if x["close"] > x["vwap"])
        below = sum(1 for x in lookback if x["close"] < x["vwap"])
        pull_tol = 0.20 * (row["atr14"] or or_atr)
        if above >= 5 and row["low"] <= max(row["vwap"], row["ema20"]) + pull_tol and row["close"] > row["ema9"] and rejection(row, 1):
            return finish(1)
        if below >= 5 and row["high"] >= min(row["vwap"], row["ema20"]) - pull_tol and row["close"] < row["ema9"] and rejection(row, -1):
            return finish(-1)
        return 0

    if name == "first_hour_break":
        if row["idx"] < 12:
            return 0
        base = g[:12]
        hi = max(x["high"] for x in base)
        lo = min(x["low"] for x in base)
        rng = max(hi - lo, 1e-9)
        if rng > 1.35 * or_atr:
            return 0
        if prev["close"] <= hi and row["close"] > hi and row["close"] > row["vwap"] and row["ema9"] >= row["ema20"]:
            return finish(1)
        if prev["close"] >= lo and row["close"] < lo and row["close"] < row["vwap"] and row["ema9"] <= row["ema20"]:
                return finish(-1)
        return 0

    if name == "first_hour_retest_q":
        if row["idx"] < 14 or not rel_vol_ok(row, 0.90):
            return 0
        base = g[:12]
        hi = max(x["high"] for x in base)
        lo = min(x["low"] for x in base)
        rng = max(hi - lo, 1e-9)
        if rng > 1.25 * atr or rng < 0.25 * atr:
            return 0
        prior = g[12:i]
        if len(prior) < 2:
            return 0
        pull_tol = 0.22 * atr
        broke_up = max(x["high"] for x in prior) > hi + 0.15 * atr
        broke_down = min(x["low"] for x in prior) < lo - 0.15 * atr
        if (
            broke_up
            and recent_vwap_bias(1, 8, 5)
            and row["low"] <= max(hi, row["vwap"], row["ema20"]) + pull_tol
            and row["close"] > hi
            and row["close"] > row["vwap"]
            and close_pos(row) >= 0.58
            and quality_peer_ok(1)
            and not_daily_opposed(1)
        ):
            return finish(1)
        if (
            broke_down
            and recent_vwap_bias(-1, 8, 5)
            and row["high"] >= min(lo, row["vwap"], row["ema20"]) - pull_tol
            and row["close"] < lo
            and row["close"] < row["vwap"]
            and close_pos(row) <= 0.42
            and quality_peer_ok(-1)
            and not_daily_opposed(-1)
        ):
            return finish(-1)
        return 0

    if name == "vwap_failure":
        if row["minute"] < 10 * 60 + 20:
            return 0
        lookback = g[max(0, i - 8): i]
        if len(lookback) < 6:
            return 0
        above = sum(1 for x in lookback if x["close"] > x["vwap"])
        below = sum(1 for x in lookback if x["close"] < x["vwap"])
        ext = 0.45 * (row["atr14"] or or_atr)
        if above >= 6 and prev["close"] > prev["vwap"] + ext and row["close"] < row["vwap"] and row["close"] < row["open"]:
            return finish(-1)
        if below >= 6 and prev["close"] < prev["vwap"] - ext and row["close"] > row["vwap"] and row["close"] > row["open"]:
            return finish(1)
        return 0

    if name in ("late_day_momentum", "late_day_reversal"):
        if row["minute"] < 14 * 60 + 15 or len(g) < 18:
            return 0
        early = g[:12]
        first_open = early[0]["open"]
        first_close = early[-1]["close"]
        first_ret = first_close - first_open
        day_ret = row["close"] - row["day_open"]
        atr = row["atr14"] or or_atr
        if abs(first_ret) < 0.35 * atr or abs(day_ret) < 0.50 * atr:
            return 0
        s = 1 if first_ret > 0 else -1
        if name == "late_day_reversal":
            s = -s
        if s > 0 and row["close"] > row["vwap"] and row["ema9"] >= row["ema20"]:
            return finish(1)
        if s < 0 and row["close"] < row["vwap"] and row["ema9"] <= row["ema20"]:
            return finish(-1)
        return 0

    return 0


def indicators_ok(row):
    return row["vwap"] is not None and row["atr14"] is not None and row["atr14"] > 0 and row["ema9"] is not None and row["ema20"] is not None


def rel_vol_ok(row, threshold):
    rel = row.get("rel_vol")
    return rel is not None and rel >= threshold


def trend_side(row):
    if row["close"] > row["vwap"] and row["ema9"] >= row["ema20"]:
        return 1
    if row["close"] < row["vwap"] and row["ema9"] <= row["ema20"]:
        return -1
    return 0


def peer_confirms(peer, side):
    if peer is None or not indicators_ok(peer):
        return False
    if trend_side(peer) != side:
        return False
    if side > 0:
        return peer.get("day_open") is None or peer["close"] >= peer["day_open"]
    return peer.get("day_open") is None or peer["close"] <= peer["day_open"]


def peer_diverges(peer, attempted_side):
    if peer is None or not indicators_ok(peer):
        return False
    return trend_side(peer) != attempted_side


def daily_trend_side(row):
    if row.get("trend5") == row.get("trend20") and row.get("trend5") in (1, -1):
        return row["trend5"]
    return 0


def rejection(row, side):
    rng = max(row["high"] - row["low"], 1e-9)
    close_pos = (row["close"] - row["low"]) / rng
    if side > 0:
        return row["close"] > row["open"] and close_pos >= 0.55
    return row["close"] < row["open"] and close_pos <= 0.45
