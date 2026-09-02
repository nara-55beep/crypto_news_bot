# trenchflippermoney live-copy setup

The paper page can now copy new buys and sells from
`0x3ad83204e37fb98cd83ac523dfb65f7f99bb716d` with real funds on Robinhood
Chain. Live execution is off until every opt-in below is complete.

## What it does

- The bot mirrors each newly observed buy and sell through GMGN on Robinhood
  Chain.
- It allocates up to **$10 total** across its open positions.
- A source transaction is recorded before submission so a restart cannot send
  the same order twice.
- Sells mirror the source wallet's sell percentage and use only the token amount
  this bot recorded buying. Existing wallet holdings are never included.
- Resetting the paper ledger does not clear or sell real positions. The red
  **CLOSE LIVE** button sells all live positions recorded by this bot.

Copy trading illiquid meme coins can still lose the entire bankroll through
price movement, slippage, failed exits, contract behavior, or fees.

## 1. Prepare Phantom

Use a dedicated Phantom account that holds no assets other than the amount for
this bot. In Phantom, enable **Robinhood Chain** under Settings > Active
Networks. The chain uses ETH for trading and gas.

Do not paste a Phantom recovery phrase or blockchain private key into this
repository, the dashboard, chat, or `GMGN_PRIVATE_KEY`.

## 2. Bind the wallet to GMGN OpenAPI

Install the official CLI and create its local API-signing key pair:

```powershell
npm install -g gmgn-cli
gmgn-cli config
```

Open the URL printed by the second command. On GMGN, connect Phantom, select the
same Robinhood Chain address that will hold the $10, enable swap capability,
and create the API key. Apply that API key locally:

```powershell
gmgn-cli config --apply YOUR_API_KEY
gmgn-cli portfolio info --raw
```

The wallet shown by `portfolio info` must exactly match the Phantom `0x...`
address. `gmgn-cli config` stores an Ed25519 API-signing PEM in
`~/.config/gmgn/`; it is not the Phantom wallet private key.

## 3. Connect the public address

Start the dashboard, open `/paper`, and click **Connect Phantom**. This reads
and stores only the selected public EVM address. It does not read a recovery
phrase, export a wallet key, or sign a trade.

Alternatively, set the public address before starting the bot:

```powershell
$env:GMGN_COPY_WALLET_ADDRESS = "0xYOUR_PHANTOM_EVM_ADDRESS"
```

## 4. Fund and arm live mode

Fund that address with no more than $10 worth of ETH on Robinhood Chain, then
set both independent live-trading switches in the same terminal that launches
the bot:

```powershell
$env:GMGN_COPY_LIVE_ENABLED = "1"
$env:GMGN_ALLOW_AUTOMATED_TRADES = "1"
python main.py
```

The paper page must show **LIVE ARMED**, the expected wallet address, and a $10
maximum. If it says **LIVE BLOCKED**, follow the exact status message; no real
order is submitted while blocked.

To disarm before a later launch, unset either switch. Pausing the panel stops
new observations but does not sell real positions; use **CLOSE LIVE** if the
tracked live positions should be liquidated immediately.
