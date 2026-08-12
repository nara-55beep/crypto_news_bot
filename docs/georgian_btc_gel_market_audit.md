# Georgian crypto maker-market audit

Checked on 2026-08-12. The bot admits a venue only when all of these are public
and independently observable:

1. an active central limit order book operated by a Georgian venue;
2. timestamped, uniquely identified public trades for fill ordering and dedupe;
3. public fee and minimum-order metadata; and
4. a live USDT perpetual for the base asset and an executable quote-currency
   conversion book.

Dealer quotes, instant conversion, P2P listings, OTC desks, kiosks and ATM quotes
do not satisfy the fill model. A displayed buy/sell price is not evidence that a
passive order could rest or that later public flow consumed its queue.

## Result

| Platform | Evidence checked | Result |
|---|---|---|
| Cryptal | Complete public pair catalog, every qualifying book/trade tape, USD/GEL/EUR conversion books and Binance USDT perpetual catalog | Included: all qualifying markets; 81 catalog markets and 60 hedgeable markets observed when checked |
| WhiteBIT Georgia | Live public global spot-market catalog (1,199 markets when checked) | Excluded: no Georgia-isolated order book or GEL spot pair; global liquidity is not a Georgian-market signal |
| Bybit Georgia | Live public global spot-instrument catalog (555 instruments when checked) | Excluded: no Georgia-isolated order book or GEL spot pair; global liquidity is not a Georgian-market signal |
| Mycoins | Public product/site surface | Excluded: no public central limit order book plus timestamped trade tape found |
| Coinmania | Public market, wallet and P2P surface | Excluded: no public central limit order book plus timestamped trade tape found |
| Other entities in the NBG VASP register | Registered-provider scope and discoverable public product surfaces | Fail closed: no verifiable public local CLOB and trade tape found |

The National Bank of Georgia workbook dated 2026-08-06 listed 42 registered
VASPs; that register defines the provider scope rather than proving that every
entry operates a market. Registration means an entity may provide one or more
virtual-asset services; it does **not** mean that it operates a public order-book
market. Among the official register and the public market catalogs/product
surfaces that could be independently verified, Cryptal was the only qualifying
Georgian maker venue found. The scanner therefore evaluates the full Cryptal
catalog rather than assuming that BTC/GEL and BTC/USD are the only opportunities.

## Live all-market result observed

Cryptal returned 81 markets. Sixty active markets quoted in USD, GEL, EUR or BTC
had a matching live Binance USDT perpetual when checked, and 58 of those had a
public trade during the prior 24 hours. The largest screened two-sided spreads at
that snapshot were in markets such as MANA-GEL, AXS-GEL, GRT-GEL and CHZ-GEL.
Those are candidates for forward paper collection, not verified profits.

The application now performs a complete scan every five minutes and shares one
rate-limited Cryptal public-data hub across the two fixed BTC collectors, the
all-market scanner and the selected-market collector. One $100 paper ledger follows
only the best qualifying non-BTC candidate at a time; it does not pretend that every
candidate has a separately funded account.

The shared gateway is permanently spaced at no more than one Cryptal request per
second in the dashboard process. If Cryptal nevertheless returns 403/429, the
collector cancels virtual quotes, lengthens the spacing and never automatically
speeds back up during that process. This prevents the earlier 100-success feedback
loop that repeatedly relaxed into another block.

## Primary sources

- NBG VASP page and register: <https://nbg.gov.ge/en/page/virtual-asset-service-providers-vasps>
- Cryptal pair metadata: <https://exchange.cryptal.com/exchange/api/v1/public/pairs>
- Binance USD-M perpetual catalog: <https://fapi.binance.com/fapi/v1/exchangeInfo>
- WhiteBIT public markets: <https://whitebit.com/api/v4/public/markets>
- Bybit public spot instruments: <https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000>
- Mycoins: <https://www.mycoins.ge/>
- Coinmania market: <https://coinmania.ge/assets>

This is a market-structure audit, not a profitability finding. Both included
markets remain independent $100 paper collectors; their balances are not added
together or presented as one fund.
