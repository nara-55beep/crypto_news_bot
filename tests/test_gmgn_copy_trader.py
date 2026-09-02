from gmgn_copy_trader import GMGNCopyTrader, _extract_trades


def test_extracts_nested_trade_records():
    trades = _extract_trades({"data": [{"side": "buy", "symbol": "TEST",
                                         "price": "2", "amount_usd": "10",
                                         "tx_hash": "abc"}]})
    assert trades == [{"id": "abc", "side": "buy", "token": "TEST",
                       "price": 2.0, "qty": None, "usd": 10.0, "ts": trades[0]["ts"]}]


def test_copy_buy_and_sell_updates_paper_balance():
    bot = GMGNCopyTrader()
    bot._apply_once({"id": "buy", "side": "buy", "token": "TEST",
                     "price": 2.0, "qty": 5.0, "usd": 10.0})
    bot._apply_once({"id": "sell", "side": "sell", "token": "TEST",
                     "price": 3.0, "qty": 5.0, "usd": 15.0})
    assert bot.balance == 55.0
    assert bot.positions == {}
    assert bot.history[0]["pnl"] == 5.0


def test_first_feed_is_baseline_not_a_trade():
    bot = GMGNCopyTrader()
    bot._baselined = False
    trades = [{"id": "old", "side": "buy", "token": "TEST",
               "price": 2.0, "qty": 5.0, "usd": 10.0}]
    bot._seen.update(t["id"] for t in trades)
    bot._baselined = True
    assert bot.balance == 50.0
    assert bot.positions == {}
