# Running the bot on your Umbrel

Your Umbrel is a good home for the bot. It's always on, it costs next to nothing to run, and the bot runs in its own Docker container alongside your other apps.

You'll type a handful of commands. Copy and paste each one, press Enter, and wait for it to finish before the next.

## 1. Open a terminal on your Umbrel

Either:

- **In the browser:** open your Umbrel dashboard, go to **Settings**, then **Advanced settings**, then **Terminal**, and pick **umbrelOS**.
- **From your computer:** open Terminal (Mac) or PowerShell (Windows) and type `ssh umbrel@umbrel.local`. The password is your Umbrel password.

## 2. Download, set a password and start it

Swap `MyPassword123` for your own password, then paste this as **one line**:

```bash
cd ~ && git clone https://github.com/chadtfreak/cryptotradingbot.git survival-bot && cd survival-bot && sed -i 's/change-me/MyPassword123/' docker-compose.yml && sudo docker compose up -d --build
```

It may ask for your Umbrel password at the `sudo` part. The first build takes a few minutes. After that the bot starts by itself whenever the Umbrel restarts.

**Paste tips for the Umbrel browser terminal:**
- It joins multiple pasted lines into one, which breaks things. That's why the command above is a single line.
- If you see `\E[200~` or "command not found" right after pasting, press Enter on a blank line and paste again.
- If it says `git: command not found`, use this line instead:

```bash
cd ~ && sudo docker run --rm -v "$PWD":/git alpine/git clone https://github.com/chadtfreak/cryptotradingbot.git survival-bot && cd survival-bot && sed -i 's/change-me/MyPassword123/' docker-compose.yml && sudo docker compose up -d --build
```

## 3. Open the dashboard

On any computer or phone on your home wifi, go to:

**http://umbrel.local:8765**

Your browser will ask for a username and password. Type anything for the username and use the password you picked in step 2.

## Everyday stuff

Open the terminal (step 1), type `cd ~/survival-bot` and press Enter, then:

| To do this | Type this |
|---|---|
| Get the latest version after I make changes | `git pull && sudo docker compose up -d --build` |
| See what it's doing behind the scenes | `sudo docker logs -f survival-bot` (press Ctrl+C to stop watching) |
| Stop the bot | `sudo docker compose down` |
| Start it again | `sudo docker compose up -d` |
| Run a backtest | `sudo docker compose run --rm survival-bot python -m bot backtest` |

Trade history and balances live in the `data` folder inside `survival-bot`, so updating or restarting doesn't lose anything.

## Something not working?

- **Dashboard won't load:** try `http://` followed by your Umbrel's IP address and `:8765` (you'll find the IP in your router or Umbrel settings).
- **"Set your own DASHBOARD_PASSWORD":** the password wasn't set. Run `cd ~/survival-bot && sed -i 's/change-me/MyPassword123/' docker-compose.yml && sudo docker compose up -d --build` with your own password.
- **Anything else:** run the logs command above and paste me what it says.
