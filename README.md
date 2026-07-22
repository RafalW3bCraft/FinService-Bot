# FinService Bot

A Telegram bot that routes administrator-reviewed financial referral offers to verified Telegram channels. Runs in **polling mode** with a local SQLite database — no cloud infrastructure or webhooks required.

> This project is an information and referral system. It is not a lender, insurer, broker, or financial adviser.

## Features

- 13 financial-service categories with English, Hindi, and Gujarati rendering
- Administrator-only offer submission via single CSV row or bulk CSV upload
- HTTPS and approved-domain validation for referral links
- Per-service Telegram channel routing with bot-admin verification
- Duplicate offer fingerprint protection
- Safe publication retry with ambiguous-delivery review flag
- Privacy deletion (`/delete_me`) and full audit logging

## Requirements

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- At least one Telegram administrator user ID

## Installation

```bash
pip install -e ".[dev]"
```

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token from @BotFather |
| `ADMIN_IDS` | ✅ | Comma-separated Telegram user IDs |
| `ALLOWED_REFERRAL_DOMAINS` | — | Approved domains for referral links |
| `SERVICE_CONFIG_PATH` | — | Path to services.yaml (default: `config/services.yaml`) |
| `SESSION_TTL_MINUTES` | — | Workflow session timeout (default: 30) |
| `PUBLISH_BATCH_SIZE` | — | Offers published per cycle (default: 10) |
| `MAX_CSV_ROWS` | — | Maximum rows per CSV upload (default: 1000) |

## Running

```bash
# With .env file
finservice-bot --env-file .env poll

# Custom database path
finservice-bot --env-file .env poll --db /path/to/bot.db

# Verbose logging
finservice-bot --env-file .env --log-level DEBUG poll
```

## Commands

### Public

| Command | Description |
|---|---|
| `/start` | Start the bot |
| `/help` | List available commands |
| `/privacy` | View data handling information |
| `/delete_me` | Delete your user data |

### Administrator

| Command | Description |
|---|---|
| `/add_offer` | Queue one offer (interactive CSV row) |
| `/template` | Download the CSV column template |
| `/setup_channels <key> <@channel>` | Verify and activate a service channel |
| `/list_services` | List all service routes and channel status |
| `/stats` | Show offer counts by status |
| `/audit` | View recent audit log entries |
| `/prune` | Remove expired sessions and history |
| `/block <user_id>` | Block a user |
| `/unblock <user_id>` | Unblock a user |
| `/cancel` | Cancel an active workflow session |

## CSV Format

Offers are submitted via `/add_offer` (one row) or a CSV file upload.

Required columns: `service_type`, `provider`, `title_en`, `referral_link`

Optional columns: `title_hi`, `title_gu`, `description_en`, `description_hi`, `description_gu`, `validity`, `terms`

Use `/template` to download a filled example.

### Service Types

`credit_card` · `loan_personal` · `loan_business` · `loan_home` · `bank_account_savings` · `bank_account_current` · `credit_builder` · `insurance_health` · `insurance_vehicle` · `insurance_pa` · `demat_account` · `investment_mutual_fund` · `investment_fixed_income`

## Project Structure

```
src/finservice_bot/
    bot/            Telegram handlers and application builder
    services/       Channel verification and offer publisher
    storage/        SQLite repository and schema
    config.py       Service catalog loader
    cli.py          Command-line entry point
    settings.py     Environment-based configuration
    validation.py   CSV and URL validation
    rendering.py    Multi-language Telegram HTML renderer
config/
    services.yaml   Financial-service catalog (13 services)
tests/
    unit/           Unit and integration tests
```

## Running Tests

```bash
pytest tests/unit -q
```

## License

See [LICENSE](LICENSE).
