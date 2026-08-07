EXTRA_SIGNAL_NAMES = {
    "first_mom_q",
    "first_fade_q",
    "two_mom_q",
    "two_fade_q",
    "prior_break_eod_q",
    "opening_impulse_momentum_q",
    "opening_impulse_fade_q",
    "volatility_compression_accept_q",
    "second_entry_trend_q",
    "vvg_late_reversal_q",
}


def extra_signal_for(ctx):
    name = ctx["name"]
    g = ctx["g"]
    i = ctx["i"]
    peer = ctx["peer"]
    cfg = ctx["cfg"]
    state = ctx["state"]
    or_hi = ctx["or_hi"]
    row = ctx["row"]
    prev = ctx["prev"]
    atr = ctx["atr"]
    close_pos = ctx["close_pos"]
    finish = ctx["finish"]
    not_daily_opposed = ctx["not_daily_opposed"]
    quality_peer_ok = ctx["quality_peer_ok"]
    recent_vwap_bias = ctx["recent_vwap_bias"]
    rejection = ctx["rejection"]
    rel_vol_ok = ctx["rel_vol_ok"]
    peer_confirms = ctx["peer_confirms"]
    peer_diverges = ctx["peer_diverges"]

    if name in ("first_mom_q", "first_fade_q", "two_mom_q", "two_fade_q"):
        look_idx = cfg["or_bars"] if name.startswith("first_") else max(cfg["or_bars"], 2 * cfg["or_bars"] - 1)
        if row["idx"] != look_idx:
            return 0
        impulse = row["close"] - row["day_open"]
        if abs(impulse) < cfg.get("min_impulse_atr", 0.10) * atr:
            return 0
        s = 1 if impulse > 0 else -1
        if "_fade_" in name:
            s = -s
        if cfg.get("peer_mode") == "confirm" and not quality_peer_ok(s):
            return 0
        if cfg.get("daily_mode") == "align" and not_daily_opposed(s) is False:
            return 0
        return finish(s)

    if name == "prior_break_eod_q":
        if row.get("prev_high") is None or row.get("prev_low") is None:
            return 0
        if state.get("used_prior_break", False):
            return 0
        if row["idx"] < cfg["or_bars"]:
            return 0
        if prev["close"] <= row["prev_high"] and row["close"] > row["prev_high"]:
            if cfg.get("peer_mode") == "confirm" and not quality_peer_ok(1):
                return 0
            out = finish(1)
            state["used_prior_break"] = bool(out)
            return out
        if prev["close"] >= row["prev_low"] and row["close"] < row["prev_low"]:
            if cfg.get("peer_mode") == "confirm" and not quality_peer_ok(-1):
                return 0
            out = finish(-1)
            state["used_prior_break"] = bool(out)
            return out
        return 0

    if name == "opening_impulse_momentum_q":
        if row["idx"] < cfg["or_bars"] or row["idx"] > cfg["or_bars"] + 4:
            return 0
        if ctx["or_range"] < 0.20 * atr or ctx["or_range"] > 1.35 * atr or not rel_vol_ok(row, 0.90):
            return 0
        if row.get("gap") is not None and abs(row["gap"]) > 0.75 * atr:
            return 0
        impulse = row["close"] - row["day_open"]
        if abs(impulse) < 0.25 * atr:
            return 0
        s = 1 if impulse > 0 else -1
        if not_daily_opposed(s) is False:
            return 0
        if (
            s > 0
            and row["close"] > max(or_hi, row["vwap"])
            and row["ema9"] >= row["ema20"]
            and close_pos(row) >= 0.58
            and quality_peer_ok(1)
        ):
            return finish(1)
        if (
            s < 0
            and row["close"] < min(ctx["or_lo"], row["vwap"])
            and row["ema9"] <= row["ema20"]
            and close_pos(row) <= 0.42
            and quality_peer_ok(-1)
        ):
            return finish(-1)
        return 0

    if name == "opening_impulse_fade_q":
        if row["idx"] < cfg["or_bars"] + 1 or row["idx"] > cfg["or_bars"] + 8:
            return 0
        if ctx["or_range"] < 0.35 * atr or ctx["or_range"] > 1.55 * atr or not rel_vol_ok(row, 0.75):
            return 0
        impulse = g[cfg["or_bars"] - 1]["close"] - row["day_open"]
        if abs(impulse) < 0.30 * atr:
            return 0
        or_mid = (or_hi + ctx["or_lo"]) / 2.0
        if (
            impulse > 0
            and prev["close"] >= or_mid
            and row["close"] < or_mid
            and row["close"] < row["vwap"]
            and close_pos(row) <= 0.45
            and (peer is None or peer_diverges(peer, 1) or peer_confirms(peer, -1))
        ):
            return finish(-1)
        if (
            impulse < 0
            and prev["close"] <= or_mid
            and row["close"] > or_mid
            and row["close"] > row["vwap"]
            and close_pos(row) >= 0.55
            and (peer is None or peer_diverges(peer, -1) or peer_confirms(peer, 1))
        ):
            return finish(1)
        return 0

    if name == "volatility_compression_accept_q":
        avg_rng = row.get("avg_daily_range")
        if row["idx"] < max(cfg["or_bars"] + 5, 9) or avg_rng is None or row.get("prev_range") is None:
            return 0
        if row["prev_range"] > 0.95 * avg_rng or ctx["or_range"] > 1.05 * atr or not rel_vol_ok(row, 0.95):
            return 0
        if row.get("gap") is not None and abs(row["gap"]) > 0.60 * atr:
            return 0
        box = g[max(cfg["or_bars"], i - 6): i]
        if len(box) < 4:
            return 0
        box_hi = max(x["high"] for x in box)
        box_lo = min(x["low"] for x in box)
        if box_hi - box_lo > 0.70 * atr:
            return 0
        accept = 0.05 * atr
        if (
            prev["close"] <= box_hi
            and row["close"] > box_hi + accept
            and row["close"] > row["vwap"]
            and row["ema9"] >= row["ema20"]
            and close_pos(row) >= 0.62
            and quality_peer_ok(1)
            and not_daily_opposed(1)
        ):
            return finish(1)
        if (
            prev["close"] >= box_lo
            and row["close"] < box_lo - accept
            and row["close"] < row["vwap"]
            and row["ema9"] <= row["ema20"]
            and close_pos(row) <= 0.38
            and quality_peer_ok(-1)
            and not_daily_opposed(-1)
        ):
            return finish(-1)
        return 0

    if name == "second_entry_trend_q":
        if row["idx"] < max(cfg["or_bars"] + 6, 10) or not rel_vol_ok(row, 0.75):
            return 0
        day_hi = max(x["high"] for x in g[: i + 1])
        day_lo = min(x["low"] for x in g[: i + 1])
        s = 0
        if day_hi > or_hi + 0.35 * atr and recent_vwap_bias(1, 10, 7) and row["close"] > row["vwap"] and row["ema9"] >= row["ema20"]:
            s = 1
        elif day_lo < ctx["or_lo"] - 0.35 * atr and recent_vwap_bias(-1, 10, 7) and row["close"] < row["vwap"] and row["ema9"] <= row["ema20"]:
            s = -1
        if s == 0 or not_daily_opposed(s) is False:
            state["se_trend"] = 0
            state["se_pullbacks"] = 0
            state["se_in_pullback"] = False
            return 0
        if state.get("se_trend", 0) != s:
            state["se_trend"] = s
            state["se_pullbacks"] = 0
            state["se_in_pullback"] = False
        pull_tol = 0.18 * atr
        if s > 0:
            in_pullback = row["low"] <= max(row["ema20"], row["vwap"]) + pull_tol or row["close"] <= row["ema9"]
            if in_pullback and not state.get("se_in_pullback", False):
                state["se_pullbacks"] = state.get("se_pullbacks", 0) + 1
            state["se_in_pullback"] = in_pullback
            if state.get("se_pullbacks", 0) >= 2 and row["close"] > row["ema9"] and row["close"] > row["vwap"] and close_pos(row) >= 0.58 and rejection(row, 1) and quality_peer_ok(1):
                state["se_pullbacks"] = 0
                return finish(1)
        else:
            in_pullback = row["high"] >= min(row["ema20"], row["vwap"]) - pull_tol or row["close"] >= row["ema9"]
            if in_pullback and not state.get("se_in_pullback", False):
                state["se_pullbacks"] = state.get("se_pullbacks", 0) + 1
            state["se_in_pullback"] = in_pullback
            if state.get("se_pullbacks", 0) >= 2 and row["close"] < row["ema9"] and row["close"] < row["vwap"] and close_pos(row) <= 0.42 and rejection(row, -1) and quality_peer_ok(-1):
                state["se_pullbacks"] = 0
                return finish(-1)
        return 0

    if name == "vvg_late_reversal_q":
        if row["minute"] < 14 * 60 + 15 or len(g) < 18:
            return 0
        early = g[:12]
        morning_ret = early[-1]["close"] - early[0]["open"]
        day_ret = row["close"] - row["day_open"]
        if abs(morning_ret) < 0.35 * atr or abs(day_ret) < 0.60 * atr:
            return 0
        s = 1 if morning_ret > 0 else -1
        if s > 0:
            if day_ret > 0.60 * atr and prev["close"] >= prev["ema20"] and row["close"] < row["ema20"] and row["close"] < row["open"] and close_pos(row) <= 0.45 and (peer is None or peer_diverges(peer, 1) or peer_confirms(peer, -1)):
                return finish(-1)
        else:
            if day_ret < -0.60 * atr and prev["close"] <= prev["ema20"] and row["close"] > row["ema20"] and row["close"] > row["open"] and close_pos(row) >= 0.55 and (peer is None or peer_diverges(peer, -1) or peer_confirms(peer, 1)):
                return finish(1)
        return 0

    return 0
