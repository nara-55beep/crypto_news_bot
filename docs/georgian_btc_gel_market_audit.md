# Georgian BTC/GEL maker-market audit

Checked on 2026-08-12. The bot admits a venue only when all of these are public
and independently observable:

1. an active BTC/GEL-equivalent central limit order book;
2. timestamped, uniquely identified public trades for fill ordering and dedupe;
3. public fee and minimum-order metadata; and
4. a usable BTCUSDT hedge and quote-currency conversion book.

Dealer quotes, instant conversion, P2P listings, OTC desks, kiosks and ATM quotes
do not satisfy the fill model. A displayed buy/sell price is not evidence that a
passive order could rest or that later public flow consumed its queue.

## Result

| Platform | Evidence checked | Result |
|---|---|---|
| Cryptal | Public pair metadata, `BTC-GEL` and `BTC-USD` books/trades, `USDT-GEL` and `USDT-USD` conversion books | Included: BTC-TOGEL and BTC-TOUSD |
| WhiteBIT Georgia | Live public spot-market catalog (1,199 markets when checked) | Excluded: zero GEL spot pairs |
| Bybit Georgia | Live public spot-instrument catalog (555 instruments when checked) | Excluded: zero GEL spot pairs |
| Mycoins | Public product/site surface | Excluded: no public BTC/GEL order book plus timestamped trade tape found |
| Coinmania | Public market, wallet and P2P surface | Excluded: no public BTC/GEL order book plus timestamped trade tape found |
| Other entities in the NBG VASP register | Registered-provider scope and discoverable public product surfaces | Fail closed: no verifiable public BTC/GEL CLOB and trade tape found |

The National Bank of Georgia workbook dated 2026-08-06 listed 42 registered
VASPs; that register defines the provider scope rather than proving that every
entry operates a market. Registration means an entity may provide one or more
virtual-asset services; it does **not** mean that it operates a public order-book
market. Among the official register and the public market catalogs/product
surfaces that could be independently verified, Cryptal was the only qualifying
Georgian BTC/GEL-equivalent maker venue found. Therefore only one new executable
paper adapter is added.

## Live metadata observed

Cryptal advertised `BTC-GEL` as `BTC-TOGEL`, with trading enabled, a 0.25%
maker fee, a 0.25% taker fee, 0.000001 BTC size precision and a 10 TOGEL minimum
cost. The paper ledger also consumes `USDT-GEL` to translate Binance BTCUSDT fair
value into TOGEL and `USDT-USD` to report a common USD-equivalent P&L.

## Primary sources

- NBG VASP page and register: <https://nbg.gov.ge/en/page/virtual-asset-service-providers-vasps>
- Cryptal pair metadata: <https://exchange.cryptal.com/exchange/api/v1/public/pairs>
- Cryptal BTC-TOGEL order book: <https://exchange.cryptal.com/exchange/api/v1/public/orderbook/BTC-GEL?limit=25>
- Cryptal BTC-TOGEL trades: <https://exchange.cryptal.com/exchange/api/v1/public/trades/BTC-GEL?limit=100>
- WhiteBIT public markets: <https://whitebit.com/api/v4/public/markets>
- Bybit public spot instruments: <https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000>
- Mycoins: <https://www.mycoins.ge/>
- Coinmania market: <https://coinmania.ge/assets>

This is a market-structure audit, not a profitability finding. Both included
markets remain independent $100 paper collectors; their balances are not added
together or presented as one fund.
