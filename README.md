# auto_split

Household expense tracker Telegram bot. Send receipt photos, the bot extracts data via AI, you choose how to split, it saves to Google Sheets.

## Setup

1. Copy `.env.example` to `.env` and fill in your credentials
2. Set up Google OAuth: obtain `credentials.json` from Google Cloud Console
3. Install dependencies: `pip install -r requirements.txt`
4. Run: `python main.py`

## Features

- Receipt photo OCR via Gemini Vision
- Per-item expense splitting (equal, individual, partial group)
- Automatic Google Sheets update (monthly tabs, auto-created)
- Fixed monthly expenses (auto-populated each month)
- Multi-household support (each Telegram group = separate household)
- Commands: /summary, /settle, /expense, /history, /edit, /delete, /settings
