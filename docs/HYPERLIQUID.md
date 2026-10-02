# Connecting Claude to Hyperliquid testnet

On the testnet, Claude places real orders and real stop orders on Hyperliquid, but with free test money. It's the dress rehearsal before real money.

Take your time with this. Nothing here can lose money except the small deposit in step 3, which stays in your own account.

## What you'll need

- About 20 minutes
- A browser wallet: **Rabby** (rabby.io) or **MetaMask**
- At least **$10 of USDC on the Arbitrum network**, plus about **$1 of ETH on Arbitrum** to pay the deposit fee

## 1. Make a wallet just for the bot

Install Rabby or MetaMask and create a **new** wallet. Don't reuse one that holds anything else.

Write the recovery phrase on paper and keep it somewhere safe. Never type it into a website, and never send it to anyone, including me.

## 2. Get USDC and a little ETH onto Arbitrum

Send at least $10 of USDC and about $1 of ETH to your new wallet's address, **on the Arbitrum network**. Most exchanges let you pick Arbitrum when you withdraw. Double-check the network before sending.

## 3. Make one small deposit on the real Hyperliquid

Hyperliquid only gives out free test money to wallets that have used the real site, to stop bots draining it.

1. Go to **app.hyperliquid.xyz** and click **Connect**, then choose your wallet. Check the address has no "-testnet" in it: the testnet site can't see real USDC.
2. Click **Deposit** and deposit **at least 5 USDC**.

This money stays in your account. It will also be ready for when Claude goes live.

## 4. Claim test money

1. Go to **app.hyperliquid-testnet.xyz** and connect the **same wallet**.
2. Find the **faucet** (Portfolio page, or the "Claim mock USDC" button) and claim **1,000 mock USDC**.

3. Move it into your **Perps** balance: open **Portfolio**, tap **Transfer**, and move the USDC from **Spot to Perps**. The faucet puts it in Spot, but the bot trades perps.

Don't use **Deposit** on the testnet site. It asks you to switch to "Arbitrum Sepolia", a test network, and will show a balance of 0. The faucet is the only way to get testnet money.

The bot only treats $100 of it as Claude's bankroll, so it behaves exactly as it will with real money.

## 5. Create a trade-only API wallet

Still on **app.hyperliquid-testnet.xyz**:

1. Open **More**, then **API**.
2. Give it a name like `survival-bot` and click **Generate**.
3. **Copy the private key** that appears and keep it somewhere safe for the next step. It's only shown once.
4. Set how long it's valid for (the maximum is fine), then click **Authorize** and approve it in your wallet.

If your wallet never pops up to approve it, turn on **testnets** in your wallet's settings (the testnet uses the Arbitrum Sepolia test network), then reload the page. Phone wallet apps are often unreliable here, so a computer with the wallet's browser extension is the easiest way to do this step.

This API wallet can place trades for your account but **can never withdraw**. If its key ever leaked, the worst anyone could do is make bad trades.

## 6. Switch Claude over

1. Open the Survival Bot dashboard on the **Claude** tab.
2. If Claude has an open position, press the **Kill switch** first.
3. Under **Where Claude trades**, click **Change**, then pick **Hyperliquid testnet**.
4. Paste your **main wallet address** (starts with 0x, 42 characters, from Rabby or MetaMask). This is public and safe to share.
5. Paste the **API wallet private key** from step 5.
6. Click **Switch**.

The bot checks with Hyperliquid that the API wallet really belongs to your account and that the account has funds. If something's wrong it tells you what. Then Claude starts a new life on the testnet, keeping its lessons.

## Watching it

- Trades and stop orders show up on **app.hyperliquid-testnet.xyz** under your account, as well as in the dashboard.
- If a stop fires on the exchange, the bot notices within a minute and records it.
- Hosting and thinking costs come off Claude's $100 bankroll as usual, even though they aren't actually taken from the exchange account.

## Good to know

- **API wallets expire.** When yours does, the dashboard will show errors from the exchange. Make a new one (step 5) and switch again.
- **Never paste your main wallet's private key or recovery phrase** anywhere in the bot. It only needs the API wallet's key.
- **Real money stays locked** in the bot until the testnet run has proved itself. Unlocking it is a deliberate change we'll make together.
