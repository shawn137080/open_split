# SplitBot — Household Expense Tracker

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Telegram Bot](https://img.shields.io/badge/platform-Telegram-26a5e4)

Telegram bot for splitting shared household expenses. Send a receipt photo → AI extracts the data → you choose how to split → balance tracked automatically.

**Open-source & self-hostable.** A managed hosted version with extras is also available.

---

## ✨ Open Source vs Hosted

| Feature | Self-hosted (free) | Hosted Pro |
|---|---|---|
| Expense tracking & splitting | ✅ | ✅ |
| Receipt OCR (Gemini Flash) | ✅ | ✅ |
| Fixed recurring expenses | ✅ | ✅ |
| CSV export | ✅ | ✅ |
| Monthly records & summaries | ✅ | ✅ |
| Monthly spending trends `/stats` | — | ✅ |
| Budget alerts | — | ✅ |
| Cloud DB backup | — | ✅ |
| Zero-ops setup | — | ✅ |
| Priority support | — | ✅ |

> Self-hosting takes ~10 minutes. See setup instructions below.

---

## Local Development Setup

### Step 1 — Clone and install

```bash
git clone https://github.com/<your-username>/open_split.git
cd open_split
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Step 2 — Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_TOKEN=your_telegram_bot_token   # from @BotFather
GEMINI_API_KEY=your_gemini_api_key       # from aistudio.google.com
DATABASE_PATH=auto_split.db              # local path, default is fine
```

### Step 3 — Run

```bash
python main.py
```

Open Telegram, send `/start` to your bot, and follow the onboarding steps.

> ⚠️ **Do not run locally if the VPS bot is already running** — Telegram only allows one polling instance per token. Two instances will cause a `409 Conflict` error.

---

## VPS Deployment (Docker)

### Prerequisites

- VPS with Docker + Docker Compose installed
- SSH root access
- GitHub repo with your code pushed

### Step 1 — Clone repo on server

```bash
ssh root@<your-vps-ip>
git clone https://github.com/<your-username>/open_split.git /opt/open_split
cd /opt/open_split
```

### Step 2 — Configure environment on server

```bash
cp .env.example .env
nano .env
```

```env
TELEGRAM_TOKEN=your_telegram_bot_token
GEMINI_API_KEY=your_gemini_api_key
DATABASE_PATH=/app/data/auto_split.db   # ← must use this path for persistent storage
```

> ⚠️ Always use `DATABASE_PATH=/app/data/auto_split.db` on the VPS.  
> This stores your database inside a Docker named volume that survives git pulls and container rebuilds.

### Step 3 — Start the bot

```bash
docker compose up -d --build
docker compose logs bot --tail=20
```

You should see:
```
Database initialized.
SplitBot starting — polling for updates...
```

---

## Migrate Local DB to VPS

If you have data in your local `auto_split.db` and want to move it to the server:

**1. Upload from your local machine:**

```bash
scp auto_split.db root@<your-vps-ip>:/tmp/auto_split.db
```

**2. Copy into the Docker volume (on the server):**

```bash
VOLUME_PATH=$(docker volume inspect open_split_bot_data --format '{{.Mountpoint}}')
cp /tmp/auto_split.db "$VOLUME_PATH/auto_split.db"
```

**3. Restart the bot:**

```bash
cd /opt/open_split && docker compose restart bot
```

---

## Deploying Updates

From your local machine, run:

```bash
./deploy.sh "describe your changes"
```

This script automatically:
1. Commits and pushes all local changes to GitHub
2. SSHs into the server and pulls the latest code
3. Rebuilds the Docker image and restarts the bot

> Data is safe — the database lives in a Docker volume, completely separate from the code.

---

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Household onboarding |
| `/add` | Guided expense entry |
| `/expense "desc" amount name [split]` | Quick one-line entry |
| `/summary [month]` | Balance + settlement |
| `/history [month]` | Full expense list |
| `/records` | Monthly overview by month |
| `/last` | Most recent expense |
| `/settle <name> <amount>` | Record a payment |
| `/edit <id> amount <value>` | Edit an expense |
| `/delete <id>` | Delete an expense |
| `/export [month]` | Download CSV |
| `/add_fixed` | Add a recurring fixed expense |
| `/fixedexp` | Manage fixed expenses |
| `/settings` | Household settings |
| `/cancel` | Cancel current flow |
| `/help` | Show all commands |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_TOKEN` | ✅ | From [@BotFather](https://t.me/botfather) |
| `GEMINI_API_KEY` | ✅ | From [Google AI Studio](https://aistudio.google.com/app/apikey) |
| `DATABASE_PATH` | optional | Default: `auto_split.db`. Use `/app/data/auto_split.db` on VPS |

### OCR Model

Receipt scanning uses **Gemini 2.5 Flash** (`gemini-2.5-flash`) via the Gemini API.

**Free tier limits** (no credit card required):
- 1,500 requests/day
- 10 requests/minute

This is more than enough for household use. Get your key at [aistudio.google.com](https://aistudio.google.com/app/apikey).

---

## Architecture

```
main.py                   # Entry point, all handler registration
database.py               # SQLite schema + CRUD
config.py                 # Env var loading
tools/
  expense_store.py        # Storage layer
  balance_calculator.py   # Balance & settlement math
  receipt_extractor.py    # Gemini Vision OCR
  tax_rates.py            # Tax/tip helpers
workflows/
  onboarding_flow.py      # /start
  manual_expense_flow.py  # /add, /expense
  receipt_flow.py         # Photo receipt scanning
  summary_flow.py         # /summary, /history, /last, /settle, /edit, /delete
  records_flow.py         # /records
  settings_flow.py        # /settings
  fixed_expense_flow.py   # /add_fixed, /fixedexp
  export_flow.py          # /export
```
