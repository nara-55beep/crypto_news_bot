import asyncio
import time

from gmgn_copy_trader import (
    GMGNCopyTrader,
    NATIVE_TOKEN,
    POLL_SECONDS,
    _extract_trades,
)


WALLET = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"


def make_bot(tmp_path, **kwargs):
    return GMGNCopyTrader(state_path=str(tmp_path / "state.json"), **kwargs)


def live_trade(side="buy", trade_id="trade-1"):
    return {
        "id": trade_id,
        "side": side,
        "token": "TEST",
        "token_address": TOKEN,
        "quote_address": NATIVE_TOKEN,
        "quote_amount": 0.1,
        "quote_decimals": 18,
        "price": 2.0,
        "qty": 50.0,
        "usd": 100.0,
        "ts": time.time(),
    }


def arm(bot, monkeypatch):
    monkeypatch.setenv("GMGN_ALLOW_AUTOMATED_TRADES", "1")


def test_extracts_gmgn_trade_contract_and_quote_fields():
    trades = _extract_trades({"activities": [{
        "event_type": "buy",
        "token": {"address": TOKEN, "symbol": "TEST"},
        "token_amount": "5",
        "quote_amount": "0.01",
        "quote_token": {"token_address": NATIVE_TOKEN, "decimals": 18},
        "price_usd": "2",
        "cost_usd": "10",
        "tx_hash": "abc",
        "timestamp": 1_700_000_000,
    }]})
    assert trades == [{
        "id": "abc",
        "side": "buy",
        "token": "TEST",
        "token_address": TOKEN,
        "quote_address": NATIVE_TOKEN,
        "quote_amount": 0.01,
        "quote_decimals": 18,
        "price": 2.0,
        "qty": 5.0,
        "usd": 10.0,
        "ts": 1_700_000_000.0,
    }]


def test_copy_buy_and_sell_updates_paper_balance(tmp_path):
    bot = make_bot(tmp_path)
    bot.reset()
    bot._baselined = True
    bot._apply_once({"id": "buy", "side": "buy", "token": "TEST",
                     "price": 2.0, "qty": 5.0, "usd": 10.0})
    bot._apply_once({"id": "sell", "side": "sell", "token": "TEST",
                     "price": 3.0, "qty": 5.0, "usd": 15.0})
    assert bot.balance == 55.0
    assert bot.positions == {}
    assert bot.history[0]["pnl"] == 5.0
    assert bot.events[0]["ts"] > 0
    assert bot.events[0]["observed_ts"] > 0


def test_first_feed_is_baseline_not_a_trade(tmp_path):
    bot = make_bot(tmp_path)
    bot.reset()
    trades = [{"id": "old", "side": "buy", "token": "TEST",
               "price": 2.0, "qty": 5.0, "usd": 10.0}]
    bot._seen.update(t["id"] for t in trades)
    bot._live_seen.update(t["id"] for t in trades)
    bot._baselined = True
    assert bot.balance == 50.0
    assert bot.positions == {}
    assert bot._live_seen == {"old"}


def test_default_poll_interval_is_rate_limit_safe():
    assert POLL_SECONDS == 10.0


def test_live_buy_is_capped_and_persisted_at_most_once(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, live_enabled=True, wallet_address=WALLET)
    arm(bot, monkeypatch)
    calls = []

    async def fake_cli(args, timeout=30.0):
        calls.append(args)
        return {
            "confirmation": {"state": "confirmed"},
            "filled_output_amount": "123456",
            "hash": "0xabc",
        }

    bot._run_cli = fake_cli
    trade = live_trade()
    asyncio.run(bot._claim_and_copy_live(trade))
    asyncio.run(bot._claim_and_copy_live(trade))

    swap_calls = [call for call in calls if call and call[0] == "swap"]
    assert len(swap_calls) == 1
    swap = swap_calls[0]
    assert swap[swap.index("--amount") + 1] == "10000000000000000"
    assert swap[swap.index("--from") + 1] == WALLET
    assert "--yes" in swap
    assert bot.live_positions[TOKEN]["raw_qty"] == 123456
    assert bot.live_positions[TOKEN]["cost_usd"] == 10.0
    assert bot.live_positions[TOKEN]["source_qty"] == 50.0
    assert trade["id"] in bot._live_seen

    restored = make_bot(tmp_path, live_enabled=True, wallet_address=WALLET)
    assert restored.live_positions[TOKEN]["raw_qty"] == 123456
    assert trade["id"] in restored._live_seen


def test_live_sell_uses_only_the_recorded_bot_position(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, live_enabled=True, wallet_address=WALLET)
    arm(bot, monkeypatch)
    bot.live_positions[TOKEN] = {
        "token": "TEST",
        "token_address": TOKEN,
        "quote_address": NATIVE_TOKEN,
        "raw_qty": 321,
        "cost_usd": 4.0,
    }
    calls = []

    async def fake_cli(args, timeout=30.0):
        calls.append(args)
        return {
            "confirmation": {"state": "confirmed"},
            "filled_output_amount": "1",
            "hash": "0xdef",
        }

    bot._run_cli = fake_cli
    asyncio.run(bot._claim_and_copy_live(live_trade("sell", "sell-1")))

    swap = calls[0]
    assert swap[swap.index("--input-token") + 1] == TOKEN
    assert swap[swap.index("--output-token") + 1] == NATIVE_TOKEN
    assert swap[swap.index("--amount") + 1] == "321"
    assert bot.live_positions == {}


def test_live_sell_mirrors_source_partial_sell(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, live_enabled=True, wallet_address=WALLET)
    arm(bot, monkeypatch)
    bot.live_positions[TOKEN] = {
        "token": "TEST",
        "token_address": TOKEN,
        "quote_address": NATIVE_TOKEN,
        "raw_qty": 1000,
        "cost_usd": 10.0,
        "source_qty": 100.0,
    }
    calls = []

    async def fake_cli(args, timeout=30.0):
        calls.append(args)
        return {"confirmation": {"state": "confirmed"}, "filled_output_amount": "1"}

    trade = live_trade("sell", "partial-sell")
    trade["qty"] = 25.0
    bot._run_cli = fake_cli
    asyncio.run(bot._claim_and_copy_live(trade))

    swap = calls[0]
    assert swap[swap.index("--amount") + 1] == "250"
    assert bot.live_positions[TOKEN]["raw_qty"] == 750
    assert bot.live_positions[TOKEN]["cost_usd"] == 7.5
    assert bot.live_positions[TOKEN]["source_qty"] == 75.0


def test_paper_reset_does_not_forget_live_position(tmp_path):
    bot = make_bot(tmp_path)
    bot.live_positions[TOKEN] = {
        "token": "TEST", "token_address": TOKEN, "quote_address": NATIVE_TOKEN,
        "raw_qty": 10, "cost_usd": 2.0,
    }
    bot.reset()
    assert TOKEN in bot.live_positions
    assert bot.balance == 50.0


def test_emergency_close_requires_exact_confirmation(tmp_path):
    bot = make_bot(tmp_path, live_enabled=True, wallet_address=WALLET)
    bot.live_positions[TOKEN] = {
        "token": "TEST", "token_address": TOKEN, "quote_address": NATIVE_TOKEN,
        "raw_qty": 10, "cost_usd": 2.0,
    }
    result = asyncio.run(bot.liquidate_live("yes"))
    assert result == {"ok": False, "error": "confirmation phrase did not match"}
    assert TOKEN in bot.live_positions


def test_connect_phantom_persists_only_public_address(tmp_path):
    bot = make_bot(tmp_path)
    result = asyncio.run(bot.set_live_wallet(WALLET))
    assert result["ok"] is True
    assert result["state"]["live"]["wallet_address"] == WALLET

    restored = make_bot(tmp_path)
    assert restored.live_wallet_address == WALLET


def test_swap_checks_third_and_final_poll_result(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, live_enabled=True, wallet_address=WALLET)
    arm(bot, monkeypatch)
    responses = [
        {"order_id": "order-1", "status": "pending"},
        {"order_id": "order-1", "status": "processed"},
        {"order_id": "order-1", "status": "processed"},
        {"order_id": "order-1", "confirmation": {"state": "confirmed"},
         "filled_output_amount": "5"},
    ]

    async def fake_cli(args, timeout=30.0):
        return responses.pop(0)

    async def no_wait(_seconds):
        return None

    bot._run_cli = fake_cli
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    result = asyncio.run(bot._execute_swap(NATIVE_TOKEN, TOKEN, 1))
    assert result["confirmation"]["state"] == "confirmed"
    assert responses == []


def test_emergency_close_works_while_live_copy_is_disarmed(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, live_enabled=False, wallet_address=WALLET)
    monkeypatch.setenv("GMGN_ALLOW_AUTOMATED_TRADES", "1")
    bot.live_positions[TOKEN] = {
        "token": "TEST", "token_address": TOKEN, "quote_address": NATIVE_TOKEN,
        "raw_qty": 10, "cost_usd": 2.0,
    }
    calls = []

    async def fake_cli(args, timeout=30.0):
        calls.append(args)
        return {
            "confirmation": {"state": "confirmed"},
            "filled_output_amount": "1",
            "hash": "0xclose",
        }

    bot._run_cli = fake_cli
    result = asyncio.run(bot.liquidate_live("CLOSE LIVE POSITIONS"))

    assert result["ok"] is True
    assert bot.live_positions == {}
    assert any(call and call[0] == "swap" for call in calls)
