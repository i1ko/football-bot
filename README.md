# Football bot

Telegram bot for the Football Alicante community. Python entry point: `app.py`.

## Local setup

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env  # Only if .env does not already exist.
```

Set `TELEGRAM_BOT_TOKEN` in the ignored `.env` file. Start with:

```sh
set -a
source .env
set +a
.venv/bin/python app.py
```

Do not run a second polling instance with the production bot token while the
hosted bot is running. Use a separate bot token for local development.

## Production data

`football_data.sqlite3` is runtime data, excluded from Git. The bot opens it next
to `bot.py`. Preserve the existing database when deploying; an empty database
does not restore existing members, polls or settings. Make a consistent backup
while the bot is stopped before the first deployment, then restart it.

## SillyDev deployment

1. Push this source to a private GitHub repository.
2. Connect the repository using the server's GitHub tab in SillyDev.
3. Provide `TELEGRAM_BOT_TOKEN` in the bot process environment before deploying
   this version. A `.env` file alone is not loaded by Python. If the panel does
   not expose custom environment variables, confirm an appropriate startup
   configuration before deployment.
4. Install dependencies from `requirements.txt` and start `python app.py`.
5. Verify the deployment preserves `football_data.sqlite3` and other runtime
   data before enabling automatic deployments.
6. Verify the deployed commit and bot startup logs after each deployment.

Never commit tokens, `.env`, databases, dependency folders or caches.
