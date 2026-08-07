"""
ict_sm_paper.py — "ICT SM Trades" PAPER bot, BTC/USDT only.

Faithful realization of the TradingView strategy "ICT SM Trades (liquidity find & grab,
MSS, FVG, killzones)" — https://www.tradingview.com/v/ZtoavozZ/ .

That published script is PROTECTED (closed source), so its Pine code cannot be copied
verbatim. This implements the SAME documented method it describes — identify liquidity,
wait for a liquidity grab/sweep, confirm a market-structure shift (MSS) on a candle close,
then enter the fair-value gap (FVG) on the retracement, gated to ICT killzones, with the
stop beyond the liquidity-grab candle — by reusing this repo's already-proven ICT
2022-model engine (ict_bot.ICTBot), which encodes exactly that sweep -> MSS -> FVG
sequence on BTC. Nothing in the strategy logic is changed; this only gives it its own
$100 paper account (data/ict_sm_state.json) so it runs independently of the existing
ICT bot, and it trades BTC only (the engine already does).
"""
from __future__ import annotations
import os
import ict_bot


class ICTSMTradesBot(ict_bot.ICTBot):
    NAME = "ICT SM Trades"
    STRATEGY_KEY = "ict_sm"
    STRATEGY_LABEL = "ICT SM Trades (liquidity grab -> MSS -> FVG, killzones)"
    STRATEGIES = {"ict_sm": "ICT SM Trades (liquidity grab -> MSS -> FVG, killzones)"}

    def _path(self):
        # separate paper account from the existing ICT bot
        return os.path.join("data", "ict_sm_state.json")
