# Running the bot on your Umbrel

The bot installs as a proper Umbrel app, with its own icon on your Umbrel home screen. Umbrel's own login protects it, and you update it from the Umbrel screens like any other app.

## Install the app

**1. Add the app store.** In your Umbrel, open the **App Store**, click the **three dots** in the top right corner and choose **Community App Stores**. Paste this link and click **Add**:

```
https://github.com/chadtfreak/cryptotradingbot
```

**2. Install it.** Open **Chad's Apps**, click **Survival Bot**, then **Install**. When it's done, the icon appears on your home screen. Click it to open the dashboard.

## Give Claude its API key

Claude needs an Anthropic API key to think. It's billed separately from a Claude subscription.

1. Go to **console.anthropic.com**, sign up, and add a card under **Billing**.
2. Under **Limits**, set a monthly spend limit of **$15**. The bot has its own $15 cap, so this is a second safety net.
3. Under **API keys**, create a key and copy it. It starts with `sk-ant-`.
4. Open the Survival Bot dashboard, paste the key into the **Anthropic API key** box under **Claude's brain**, and click **Save key**.

The key is stored only on your Umbrel. It never goes to GitHub, and the dashboard never shows it again. Claude takes its first look at the market within a minute.

## Moving over from the terminal version

If you already ran the bot with the terminal commands, this copies its history into the app so it carries on the same life instead of being born again.

Install the app first (above), then open the terminal (**Settings → Advanced settings → Terminal → umbrelOS**) and paste this as **one line**:

```bash
cd ~/survival-bot && sudo docker compose down && sudo docker stop chad-survival-bot_server_1 && sudo cp data/bot.db ~/umbrel/app-data/chad-survival-bot/data/bot.db && sudo docker start chad-survival-bot_server_1
```

That stops the old version, copies its history across, and restarts the app. Open the app and check the "Born" date at the top matches the original.

Once you're happy, you can delete the old copy with `rm -rf ~/survival-bot`. There's no rush.

## Phone shortcut

Open **http://umbrel.local:8766** on your phone while on your home wifi and log in with your Umbrel password.

- **iPhone (Safari):** tap the **Share** button, then **Add to Home Screen**.
- **Android (Chrome):** tap the **three dots**, then **Add to Home screen** or **Install app**.

You'll get the green heartbeat icon on your phone, and it opens full screen like an app.

## Updates

When I push a new version, Umbrel shows an update for Survival Bot. Click **Update** and its history carries over.

## Handy terminal commands

| To do this | Type this |
|---|---|
| See what it's doing behind the scenes | `sudo docker logs -f chad-survival-bot_server_1` (Ctrl+C to stop watching) |
| Run a backtest | `sudo docker exec chad-survival-bot_server_1 python -m bot backtest` |

Restarting, stopping and uninstalling are all in the app's menu: right click the icon on the Umbrel home screen (or long press on a phone).

**Careful:** uninstalling the app deletes its trade history. Stop or restart it instead if you just want to give it a kick.

## Something not working?

- **The app store won't add, or the install fails:** send me a screenshot of the error.
- **The move-over command says "No such file or directory":** send me a screenshot. Your Umbrel may keep app data in a slightly different place.
- **Anything else:** run the logs command above and send me what it says.

## Paste tips for the Umbrel browser terminal

- It joins multiple pasted lines into one, which breaks things. That's why every command here is a single line.
- If you see `\E[200~` or "command not found" right after pasting, press Enter on a blank line and paste again.
