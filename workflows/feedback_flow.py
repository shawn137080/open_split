"""Implementation of the /feedback command."""

import logging
from telegram import Update
from telegram.ext import ContextTypes
import database

logger = logging.getLogger(__name__)

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /feedback command to collect user feedback."""
    message = update.effective_message
    if not message or not message.text:
        return

    chat_id = str(message.chat.id)
    user_id = str(message.from_user.id) if message.from_user else "unknown"

    # Extract the feedback text after the command
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("Please provide your feedback after the command. Example:\n/feedback I wish I could export this to a PDF.")
        return

    feedback_text = args[1].strip()

    try:
        # We can implement save_feedback in database.py
        conn = database._connect()
        try:
            conn.execute(
                "INSERT INTO feedback (user_id, group_id, message) VALUES (?, ?, ?)",
                (user_id, chat_id, feedback_text)
            )
            conn.commit()
        finally:
            conn.close()

        logger.info(f"Feedback received from user {user_id} in group {chat_id}: {feedback_text}")
        await message.reply_text("Thank you! Your feedback has been recorded safely in our database. Pip 🐿️ will make sure the devs see it!")
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")
        await message.reply_text("Sorry, there was an error saving your feedback. Please try again later.")
