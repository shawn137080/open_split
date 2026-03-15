import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return value


TELEGRAM_TOKEN = _require("TELEGRAM_TOKEN")
GEMINI_API_KEY = _require("GEMINI_API_KEY")
DATABASE_PATH = os.getenv("DATABASE_PATH", "auto_split.db")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# Global Pro tier toggle (legacy)
IS_PRO = bool(os.getenv("PRO_LICENSE_KEY"))
