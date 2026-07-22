# FinService Bot

A Telegram bot for publishing administrator-reviewed financial referral offers to verified channels. Runs in local polling mode with SQLite.

## How to run

```bash
finservice-bot --env-file .env poll
```

Or with the configured workflow: click **Run**.

## Environment

All required keys are in `.env`. The bot needs:
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `ADMIN_IDS` — comma-separated Telegram user IDs

## User preferences

- Strictly CLI-based; no web server or webhook mode
- No cloud dependencies (no GCP, Firebase, Firestore)
- SQLite database at `finservice.db` by default
- Code must be clean: no dead code, no unused imports, no placeholder comments
