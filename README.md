# auto_split — Household Expense Tracker Bot

Telegram bot for splitting shared household expenses. Send a receipt photo, the bot extracts the data with AI, you choose how to split it — all stored locally in SQLite. No Google account needed.

## Quick Start

```bash
git clone <repo>
cd auto_split
pip install -r requirements.txt
cp .env.example .env   # fill in your token and API key
python3 main.py
```

Then open Telegram, message your bot `/start`, and follow the setup steps.

## Requirements

| Variable | Description |
|----------|-------------|
| `TELEGRAM_TOKEN` | From [@BotFather](https://t.me/botfather) |
| `GEMINI_API_KEY` | From [Google AI Studio](https://aistudio.google.com/app/apikey) (for receipt OCR) |
| `DATABASE_PATH` | *(optional)* Path to SQLite DB file. Default: `auto_split.db` |

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Set up or re-run household onboarding |
| `/add` | Guided expense entry (step-by-step) |
| `/expense "desc" amount name [split]` | Quick one-line expense entry |
| `/summary [month]` | Balance + settlement for a month |
| `/history [month]` | Full expense list for a month |
| `/last` | Show the most recent expense |
| `/settle <name> <amount>` | Record a settlement payment |
| `/delete <expense_id>` | Delete an expense |
| `/export [month] [year]` | Download expenses as a CSV file |
| `/cancel` | Cancel any in-progress flow |
| `/help` | Show all commands |

**Receipt scanning:** Just send a photo — the bot handles the rest.

## Features

- 📷 Receipt OCR via Gemini Vision (extracts merchant, date, items, taxes, tip)
- ✂️ Flexible splits: equal, individual, by item, or custom
- 📊 Real-time balance calculation
- 📅 Fixed monthly expenses (auto-seeded each month)
- 📁 `/export` — generates a `.csv` file with full expense history
- 🏠 Multi-household (each Telegram group is an independent household)
- 🗄️ Local SQLite — no external services beyond Telegram + Gemini

## Architecture

```
main.py                   # Bot entry point, handler registration
database.py               # SQLite schema + CRUD helpers
config.py                 # Env var loading
tools/
  expense_store.py        # Storage layer (wraps database.py)
  balance_calculator.py   # Balance & settlement math
  receipt_extractor.py    # Gemini Vision OCR
  input_parser.py         # Text parsing helpers
workflows/
  onboarding_flow.py      # /start setup wizard
  manual_expense_flow.py  # /add and /expense flows
  receipt_flow.py         # Photo receipt flow
  summary_flow.py         # /summary, /history, /last, /settle, /delete
  export_flow.py          # /export CSV generation
```
