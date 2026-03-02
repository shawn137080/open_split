"""
auto_split — Household Expense Tracker Bot
Telegram bot that tracks shared expenses and updates Google Sheets.
"""
import logging
from telegram.ext import Application
from config import TELEGRAM_TOKEN

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def main():
    """Start the bot."""
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    # Handlers registered in Task 10
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
