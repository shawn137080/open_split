# 🥜 NutSplit Core — Open Source Community Edition

[![Telegram Bot](https://img.shields.io/badge/platform-Telegram-26a5e4)](https://t.me/NutSplitBot)
[![Website](https://img.shields.io/badge/website-nutsplit.app-orange)](https://nutsplit.app)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

NutSplit is the frictionless AI expense tracker that lives directly inside your Telegram groups. **No app to download, no accounts to create. Just pure, organized household finance.**

> [!IMPORTANT]
> ### 🚀 Join the Official Bot
> The easiest way to start tracking with your roommates today is via the **[Official NutSplit Bot (@NutSplitBot)](https://t.me/NutSplitBot)**. 
> 
> **Why use the official bot?**
> - ⚡ **Zero Setup**: Start in 30 seconds.
> - 📸 **Unlimited OCR Scanning**: Gemini-powered receipt extraction.
> - 📊 **Premium Charts**: Real-time spending trends and category budgets.
> - 🛠 **Pro Support**: Priority bug fixes and feature updates.

---

## ✨ Features

- **📸 AI Receipt OCR**: Snap a photo and Gemini AI extracts amounts, tax, and catetories instantly.
- **⚖️ Flexible Splits**: Equal, custom shares, or "A pays for B".
- **📊 Pro Trends & Budgets**: Monthly visual spending charts and category budget alerts.
- **🔄 Recurring Expenses**: Set rent and utilities once, auto-seeded every month.
- **🔒 Privacy First**: Data lives in your group; we only store what's necessary to track your balances.

---

## 💎 NutSplit Pro

Unlock the full power of Pip the Squirrel for just **$4.99/month**.

| Feature | Free Tier | Pro Tier |
|---|---|---|
| Expense tracking & splitting | ✅ | ✅ |
| Receipt OCR (Gemini AI) | 10 scans/mo | **Unlimited** |
| Spending trends `/stats` | — | ✅ |
| Budget alerts `/budget` | — | ✅ |
| Export Data (CSV) | ✅ | ✅ |

> **Upgrading is easy**: Just run the `/upgrade` command directly inside the Telegram Bot.

---

## 🛠 Open Core & Self-Hosting

NutSplit follows an **Open Core** model. We believe in transparency and empowering the developer community. You can review our core logic, contribute features, or run a limited private instance.

### 📜 What is Open Source?
- **Core Orchestration**: Our Telegram message routing and state management.
- **AI Prompting**: The logic behind our receipt extraction and NL routing.
- **Math Engine**: All balance calculations and settlement algorithms.

### 🔒 NutSplit Pro (Closed Source)
The official SaaS infrastructure (Stripe integrations, advanced visual analytics, and automated multi-tenant scaling) is maintained in a private repository to support the sustainable development of this project. 

> **[Try NutSplit Pro for $4.99/mo](https://t.me/NutSplitBot)**

### Option 1 — Railway (one-click)

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https://github.com/shawn137080/nutsplit)

1. Click the button above
2. Set **2 environment variables**:
   - `TELEGRAM_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `GEMINI_API_KEY` — from [aistudio.google.com](https://aistudio.google.com/)
3. Click **Deploy** — done ✅

> Free tier is enough to run this bot. Railway gives $5/month free credit.

---

### Option 2 — Docker (any VPS)

```bash
git clone https://github.com/shawn137080/nutsplit.git && cd nutsplit
cp .env.example .env   # fill in your tokens
docker compose up -d
```

---

## Local Development Setup

### Step 1 — Clone and install

```bash
git clone https://github.com/<your-username>/nutsplit.git
cd nutsplit
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
git clone https://github.com/<your-username>/nutsplit.git /opt/nutsplit
cd /opt/nutsplit
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
NutSplit starting — polling for updates...
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
VOLUME_PATH=$(docker volume inspect nutsplit_bot_data --format '{{.Mountpoint}}')
cp /tmp/auto_split.db "$VOLUME_PATH/auto_split.db"
```

**3. Restart the bot:**

```bash
cd /opt/nutsplit && docker compose restart bot
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
