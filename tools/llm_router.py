"""Natural language router using Gemini Flash to determine user intent."""

import logging
from google import genai
from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GEMINI_API_KEY)
_MODEL = "gemini-2.5-flash"

_PROMPT = """You are a natural language router for a Telegram Expense Bot called NutSplit.
The user will provide a text message. Your job is to classify their INTENT into one of the following exact strings:
- "summary": The user wants to see their monthly summary, balances, or settlement (e.g., "what's the summary", "how much did we spend", "show me february balances").
- "history": The user wants to see a list of individual expenses (e.g., "show me the history", "what did we buy", "list expenses").
- "owe": The user wants to quickly know who owes whom (e.g., "who owes who", "do I owe money", "balances").
- "stats": The user wants to see spending trends or charts (e.g., "show my stats", "spending trends", "category pie chart").
- "budget": The user is asking about their budget limits or setting a budget (e.g., "what is my dining budget", "set budget to 100").
- "help": The user is asking what the bot can do or asking for help.
- "export": The user wants to download or export a CSV of their data.
- "fixed": The user wants to manage recurring/fixed expenses like rent or internet (e.g., "show fixed expenses").
- "unknown": The text does not clearly match any of the above, or it looks like chit-chat, or it looks like a quick manual expense entry format (e.g. '50 dinner' or '12.50 coffee'). Note: DO NOT route manual expense entries here, just return unknown.

Return ONLY the classification string. Nothing else."""

def route_intent(text: str) -> str:
    """Returns the classified intent for the given text using Gemini."""
    try:
        response = _client.models.generate_content(
            model=_MODEL,
            contents=[_PROMPT, f"User message: {text}"],
            config=genai.types.GenerateContentConfig(temperature=0.0)
        )
        intent = response.text.strip().lower()
        
        valid_intents = {"summary", "history", "owe", "stats", "budget", "help", "export", "fixed", "unknown"}
        if intent in valid_intents:
            return intent
        return "unknown"
    except Exception as e:
        logger.error("Error in LLM routing: %s", e)
        return "unknown"
