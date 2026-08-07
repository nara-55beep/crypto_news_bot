"""
live.py — CCXT Binance USD-M live/testnet executor.

It runs the SAME strategy as the backtest on freshly-fetched CLOSED candles. On a signal it:
  1. sizes the position from real account equity (1% risk / stop distance),
  2. sends a market entry,
  3. places exchange-side reduce-only brackets (STOP_MARKET + TAKE_PROFIT_MARKET) so your stop and
     target survive even if this process dies,
  4. polls the position; when TP1 fills it moves the stop to breakeven.

SAFETY: defaults to testnet (config.testnet=True). Run it there first. Live trading risks real money.
"""
from __future__ import annotations
import time

from .config import Config
from . import data_feed, indicators
from .strategy import TrendSweepStrategy
from .risk import RiskManager, position_size
from .trade_logger import TradeCSV, get_logger


class LiveTrader:
    def __init__(self, cfg: Config, trades_csv="output/live_trades.csv"):
        self.cfg = cfg
        self.log = get_logger("trend_sweep_bot.live", logfile="output/live.log")
        self.ex = data_feed.make_exchange(cfg, signed=True)
        self.strat = TrendSweepStrategy(cfg)
        self.risk = RiskManager(cfg)
        self.csv = TradeCSV(trades_csv)
        self.pos = None                 # local mirror of the open position
        self._last_bar_t = 0
        try:
            self.ex.set_leverage(cfg.leverage, cfg.symbol)
        except Exception as e:
            self.log.warning("set_leverage failed: %s", e)

    # ---- account ----
    def equity(self):
        try:
            bal = self.ex.fetch_balance()
            usdt = (bal.get(self.cfg.quote) or {})
            return float(usdt.get("total") or usdt.get("free") or 0.0)
        except Exception as e:
            self.log.warning("fetch_balance failed: %s", e)
            return 0.0

    def live_position(self):
        try:
            for p in self.ex.fetch_positions([self.cfg.symbol]):
                amt = float(p.get("contracts") or p.get("contractSize") or 0) or \
                      abs(float((p.get("info") or {}).get("positionAmt") or 0))
                if amt:
                    side = "long" if (p.get("side") == "long" or
                                      float((p.get("info") or {}).get("positionAmt") or 0) > 0) else "short"
                    return {"qty": amt, "side": side,
                            "entry": float(p.get("entryPrice") or 0)}
        except Exception as e:
            self.log.warning("fetch_positions failed: %s", e)
        return None

    # ---- orders ----
    def _bracket(self, side, qty, stop, tp1, tp2):
        opp = "sell" if side == "long" else "buy"
        try:
            self.ex.create_order(self.cfg.symbol, "STOP_MARKET", opp, qty, None,
                                 {"stopPrice": stop, "reduceOnly": True})
            self.ex.create_order(self.cfg.symbol, "TAKE_PROFIT_MARKET", opp,
                                 qty * self.cfg.tp1_size, None,
                                 {"stopPrice": tp1, "reduceOnly": True})
            self.ex.create_order(self.cfg.symbol, "TAKE_PROFIT_MARKET", opp,
                                 qty * (1 - self.cfg.tp1_size), None,
                                 {"stopPrice": tp2, "reduceOnly": True})
        except Exception as e:
            self.log.error("bracket placement failed: %s", e)

    def _cancel_all(self):
        try:
            self.ex.cancel_all_orders(self.cfg.symbol)
        except Exception as e:
            self.log.warning("cancel_all failed: %s", e)

    def _enter(self, sig):
        eq = self.equity()
        qty = position_size(eq, self.cfg.risk_pct, sig["entry"], sig["stop"])
        qty = float(self.ex.amount_to_precision(self.cfg.symbol, qty))
        if qty <= 0:
            self.log.info("size rounded to 0 — skip"); return
        side = sig["side"]
        try:
            self.ex.create_order(self.cfg.symbol, "market", "buy" if side == "long" else "sell", qty)
        except Exception as e:
            self.log.error("entry order failed: %s", e); return
        self._bracket(side, qty, sig["stop"], sig["tp1"], sig["tp2"])
        self.pos = {"side": side, "entry": sig["entry"], "stop": sig["stop"], "stop0": sig["stop"],
                    "tp1": sig["tp1"], "tp2": sig["tp2"], "qty0": qty, "tp1_done": False,
                    "opened_at": time.time(), "risk": abs(sig["entry"] - sig["stop"]) * qty}
        self.risk.on_open(time.time())
        self.log.info("ENTRY %s %s qty=%s stop=%.2f tp1=%.2f tp2=%.2f",
                      side, self.cfg.symbol, qty, sig["stop"], sig["tp1"], sig["tp2"])

    def _manage(self):
        """Reconcile with the exchange: detect TP1 (size halved -> move stop to BE) and full close."""
        if self.pos is None:
            return
        lp = self.live_position()
        if lp is None:                      # fully closed by a bracket
            self._cancel_all()
            self.log.info("position closed on exchange (bracket hit)")
            self.pos = None
            return
        if not self.pos["tp1_done"] and lp["qty"] <= self.pos["qty0"] * (1 - self.cfg.tp1_size) * 1.05:
            self.pos["tp1_done"] = True
            if self.cfg.move_stop_be_after_tp1:
                opp = "sell" if self.pos["side"] == "long" else "buy"
                try:
                    self.ex.cancel_all_orders(self.cfg.symbol)
                    self.ex.create_order(self.cfg.symbol, "STOP_MARKET", opp, lp["qty"], None,
                                         {"stopPrice": self.pos["entry"], "reduceOnly": True})
                    self.ex.create_order(self.cfg.symbol, "TAKE_PROFIT_MARKET", opp, lp["qty"], None,
                                         {"stopPrice": self.pos["tp2"], "reduceOnly": True})
                    self.log.info("TP1 hit -> stop moved to breakeven %.2f", self.pos["entry"])
                except Exception as e:
                    self.log.error("breakeven move failed: %s", e)

    # ---- main loop ----
    def run(self, poll_sec=20):
        self.log.info("LIVE trader started on %s (%s, testnet=%s)",
                      self.cfg.symbol, self.cfg.exchange, self.cfg.testnet)
        while True:
            try:
                self._manage()
                c5 = data_feed.recent(self.ex, self.cfg.symbol, self.cfg.tf_entry, 300)
                if not c5:
                    time.sleep(poll_sec); continue
                newest = c5[-1]["t"]
                if newest == self._last_bar_t or self.pos is not None:
                    time.sleep(poll_sec); continue
                self._last_bar_t = newest
                c4h = data_feed.recent(self.ex, self.cfg.symbol, self.cfg.tf_trend, 200)
                c1d = data_feed.recent(self.ex, self.cfg.symbol, self.cfg.tf_daily, 60)
                trend = indicators.classify_trend(c4h, self.cfg.swing_width,
                                                  self.cfg.trend_ema_len, self.cfg.trend_lookback)
                pdh, pdl = indicators.prev_day_high_low(c1d, newest)
                vwap = indicators.daily_vwap(c5, newest)
                atr_val = indicators.atr(c5[-(self.cfg.atr_len + 2):], self.cfg.atr_len)
                ctx = {"trend": trend, "pdh": pdh, "pdl": pdl, "vwap": vwap, "atr": atr_val}
                sig = self.strat.on_bar(c5, len(c5) - 1, ctx)
                if sig:
                    ok, why = self.risk.can_trade(newest)
                    if ok:
                        self._enter(sig)
                    else:
                        self.log.info("signal but blocked: %s", why)
            except Exception as e:
                self.log.error("loop error: %s", e)
            time.sleep(poll_sec)
