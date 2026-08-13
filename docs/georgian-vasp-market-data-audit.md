# Georgian VASP market-data audit

Audit date: 2026-08-13. Registry snapshot: 2026-08-06.

The [National Bank of Georgia VASP register](https://nbg.gov.ge/en/page/virtual-asset-service-providers-vasps)
is the scope authority. Registration means that an entity can provide one or more
virtual-asset services; it does **not** mean that the entity runs an exchange, has a
public API, or exposes executable liquidity.

## Machine-readable local markets

| Venue | Registration | Verified public surface | Integration |
|---|---:|---|---|
| [Cryptal](https://support.cryptal.com/hc/en-us/articles/360014358800-How-do-I-start-trading) | 0002-9404 | Local order books plus timestamped trades | Existing passive-maker paper collector |
| [Coinet](https://www.coinet.ge/) | 0006-9404 | REST dealer buy/sell quotes and public min/max | Fixed-quote opportunity screen |
| [Mycoins](https://mycoins.ge/) | 0001-9404 | SignalR dealer buy/sell quote stream | Fixed-quote opportunity screen; size fails closed |
| [PLEX / PlatformaEX](https://www.platformaex.com/) | 0017-9404 | Directed conversion routes, output fees and limits | Manual-route screen; never called automated |

WhiteBIT and Bybit have [public](https://docs.whitebit.com/) global order-book APIs, but the audit did not verify a
distinct Georgian GEL order book or that a global API order is contracted through the
registered Georgian entity. They are therefore not labelled Georgian local liquidity.
Coinmania's public-looking endpoint returned a browser challenge to the bot and is not
silently scraped around that control.

## Full active register scope

The scanner carries all 42 active head-office records so the UI reports the denominator,
not only the venues that happened to be integrable:

Mycoins, Cryptal, Bitanica, Bitexchange, Cryptomat, Coinet, WhiteBIT, Bithold,
Alltrust.me, AURUM, CoinSwap, Coinmania, Bitnet, Digital Currency, CryptoExchange,
PLEX, GECRYPTO, Bybit Georgia Limited, Crypto Exchange, Werty, Matio, Stellex,
Cryptox, SPEX, City Pay, Crypto Change Batumi, Bitrust, Gaus Crypto, Coinero,
BITARI, Bitcasa, PayBit, FinSec, Coinsflow, SOLUNEX, CoinnetX, Bipayhi, Covex,
BitFi, Digital Assets Sakartvelo, GLOBAL CRYPTO, and Fintrust.

## What the screen proves—and does not

The fixed-quote screen compares local dealer quotes with executable Binance spot sides,
requires a Binance perpetual for temporary transfer hedging, and deducts spot and hedge
fees, slippage, current basis, a $2.50 transfer reserve, and an operational reserve.
PlatformaEX output fees are applied at the configured $100 test size.

It does not assume that a displayed dealer quote will be accepted. KYC, deposit and
withdrawal availability, exact network fees, route limits and settlement time still
need confirmation. Consequently these rows can become **screen candidates**, but never
paper fills or live orders. Only Cryptal keeps the existing public-print fill model, and
that model remains explicitly non-evidentiary.
