"""auto_split — Household Expense Tracker Bot

Telegram bot that tracks shared household expenses.
All data is stored locally in SQLite — no external services required
beyond Telegram and (optionally) Gemini for receipt OCR.
Multi-tenant: each Telegram group is an independent household.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
from config import TELEGRAM_TOKEN
from workflows.manual_expense_flow import (
    handle_add_callback,
    handle_add_command,
    handle_add_message,
    handle_expense_command,
    handle_manual_callback,
    is_add_state,
)
from workflows.onboarding_flow import (
    handle_onboarding_callback,
    handle_onboarding_message,
    handle_start,
    is_onboarding_state,
)
from workflows.receipt_flow import (
    handle_photo,
    handle_receipt_callback,
    handle_receipt_message,
    is_receipt_state,
)
from workflows.summary_flow import (
    handle_delete_command,
    handle_edit_command,
    handle_history_command,
    handle_last_command,
    handle_multisummary_command,
    handle_settle_command,
    handle_summary_command,
)
from workflows.export_flow import handle_export_command
from workflows.records_flow import (
    RECORDS_CALLBACK_PREFIXES,
    handle_records_command,
    handle_records_callback,
)
from workflows.fixed_expense_flow import (
    CB_ADDFIXED_PAIDBY_PFX,
    CB_ADDFIXED_SPLIT_EQUAL,
    CB_ADDFIXED_SPLIT_MINE,
    CB_ADDFIXED_SPLIT_FOR_PFX,
    CB_ADDFIXED_START_THIS,
    CB_ADDFIXED_START_NEXT,
    CB_ADDFIXED_START_TYPE,
    CB_ADDFIXED_CONFIRM_YES,
    CB_ADDFIXED_CONFIRM_NO,
    CB_FE_SELECT_PFX,
    CB_FE_SKIP_THIS,
    CB_FE_UNSKIP,
    CB_FE_CANCEL_FUTURE,
    CB_FE_CANCEL_FUTURE_YES,
    CB_FE_CANCEL_FUTURE_NO,
    CB_FE_BACK,
    CB_FE_EDIT,
    CB_FE_EDIT_AMOUNT,
    CB_FE_EDIT_PAIDBY_PFX,
    CB_FE_EDIT_SPLIT_EQUAL,
    CB_FE_EDIT_SPLIT_MINE,
    CB_FE_EDIT_SPLIT_FOR_PFX,
    CB_FE_EDIT_BACK,
    STATE_ADDFIXED_DESC,
    STATE_ADDFIXED_AMOUNT,
    STATE_ADDFIXED_STARTMONTH,
    STATE_FIXEDEXP_EDIT_AMOUNT,
    handle_add_fixed_command,
    handle_add_fixed_callback,
    handle_add_fixed_message,
    handle_fixedexp_command,
    handle_fixedexp_callback,
)
from workflows.settings_flow import (
    SETTINGS_CALLBACK_PREFIXES,
    STATE_SETTINGS_NAME,
    STATE_SETTINGS_TAX,
    handle_settings_command,
    handle_settings_callback,
    handle_settings_message,
    is_settings_state,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /help command
# ---------------------------------------------------------------------------

_HELP_TEXT = (
    "<b>SplitBot — Expense Tracker</b>\n"
    "\n"
    "<b>Add Expenses</b>\n"
    "📸 Send photo — scan a receipt\n"
    "/add — guided entry (buttons)\n"
    '/expense "desc" amt name — quick entry\n'
    "  splits: <code>equal</code> · <code>mine</code> · <code>name</code>\n"
    "\n"
    "<b>Reports</b>\n"
    "/summary [feb] — balance + settlement\n"
    "/history [feb] — expense list\n"
    "/last [feb] — most recent expense\n"
    "/records — monthly overview\n"
    "\n"
    "<b>Manage</b>\n"
    "/settle name 50.00 — record payment\n"
    "/edit EXP-003 amount 55.00 — edit expense\n"
    "/delete EXP-003 — remove expense\n"
    "/export [feb] — download CSV\n"
    "\n"
    "<b>Fixed Expenses</b>\n"
    "/add_fixed — add a recurring monthly expense\n"
    "/fixedexp — view & manage fixed expenses\n"
    "\n"
    "<b>Setup</b>\n"
    "/start — household onboarding\n"
    "/settings — household settings\n"
    "\n"
    "<b>Tips</b>\n"
    "• One photo at a time\n"
    "• After scanning, assign items:\n"
    "  <code>2 karlos</code> → item 2 to Karlos\n"
    "  <code>1 karlos mike</code> → split between them\n"
    "  <code>all except alex</code> → everyone except Alex"
)


async def _handle_help(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /help command."""
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(_HELP_TEXT, parse_mode="HTML")


async def _handle_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /cancel command — clears any active flow state."""
    if update.effective_chat is None or update.effective_user is None:
        return
    if update.effective_message is None:
        return
    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    state_row = database.get_state(user_id, group_id)
    if state_row and state_row.get("state"):
        database.clear_state(user_id, group_id)
        await update.effective_message.reply_text("Cancelled.")
    else:
        await update.effective_message.reply_text("Nothing to cancel.")


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


async def _route_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route non-command text messages to the active flow handler."""
    if update.effective_chat is None or update.effective_user is None:
        return

    group_id: str = str(update.effective_chat.id)
    user_id: str = str(update.effective_user.id)

    if is_onboarding_state(user_id, group_id):
        await handle_onboarding_message(update, context)
    elif is_receipt_state(user_id, group_id):
        await handle_receipt_message(update, context)
    elif is_add_state(user_id, group_id):
        await handle_add_message(update, context)
    elif is_settings_state(user_id, group_id):
        await handle_settings_message(update, context)
    else:
        # Check for add-fixed / fixedexp-edit text input
        state_row = database.get_state(user_id, group_id)
        if state_row and state_row.get("state") in (
            STATE_ADDFIXED_STARTMONTH,
            STATE_ADDFIXED_DESC,
            STATE_ADDFIXED_AMOUNT,
            STATE_FIXEDEXP_EDIT_AMOUNT,
        ):
            await handle_add_fixed_message(update, context)


async def _route_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route inline keyboard callbacks to the appropriate flow handler."""
    query = update.callback_query
    if query is None:
        return

    data: str = query.data or ""

    if data.startswith("onboard:"):
        await handle_onboarding_callback(update, context)
    elif data.startswith("receipt:"):
        await handle_receipt_callback(update, context)
    elif data.startswith("manual:"):
        await handle_manual_callback(update, context)
    elif data.startswith("add:"):
        await handle_add_callback(update, context)
    elif (
        data.startswith(CB_ADDFIXED_PAIDBY_PFX)
        or data.startswith("afs:")
        or data.startswith("afst:")
        or data.startswith("afc:")
    ):
        await handle_add_fixed_callback(update, context)
    elif (
        data.startswith(CB_FE_SELECT_PFX)
        or data.startswith(CB_FE_SKIP_THIS)
        or data.startswith(CB_FE_UNSKIP)
        or data.startswith(CB_FE_CANCEL_FUTURE)
        or data.startswith(CB_FE_CANCEL_FUTURE_YES)
        or data.startswith(CB_FE_CANCEL_FUTURE_NO)
        or data.startswith(CB_FE_EDIT)
        or data.startswith(CB_FE_EDIT_AMOUNT)
        or data.startswith(CB_FE_EDIT_PAIDBY_PFX)
        or data.startswith(CB_FE_EDIT_SPLIT_EQUAL)
        or data.startswith(CB_FE_EDIT_SPLIT_MINE)
        or data.startswith(CB_FE_EDIT_BACK)
        or data == CB_FE_BACK
    ):
        await handle_fixedexp_callback(update, context)
    elif data.startswith("cfg:"):
        await handle_settings_callback(update, context)
    elif data.startswith("rec:"):
        await handle_records_callback(update, context)
    else:
        await query.answer("Unknown action.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Initialize the database, register all handlers, and start polling."""
    database.init_db()
    logger.info("Database initialized.")

    app: Application = Application.builder().token(TELEGRAM_TOKEN).build()

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Unhandled exception:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "⚠️ Something went wrong. Please try again or use /cancel to reset."
                )
            except Exception:
                pass

    app.add_error_handler(_error_handler)

    # --- General ---
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", _handle_help))
    app.add_handler(CommandHandler("cancel", _handle_cancel))

    # --- Expenses ---
    app.add_handler(CommandHandler("add", handle_add_command))
    app.add_handler(CommandHandler("expense", handle_expense_command))

    # --- Fixed expenses ---
    app.add_handler(CommandHandler("add_fixed", handle_add_fixed_command))
    app.add_handler(CommandHandler("fixedexp", handle_fixedexp_command))

    # --- Settings ---
    app.add_handler(CommandHandler("settings", handle_settings_command))

    # --- Reports ---
    app.add_handler(CommandHandler("summary", handle_summary_command))
    app.add_handler(CommandHandler("history", handle_history_command))
    app.add_handler(CommandHandler("last", handle_last_command))
    app.add_handler(CommandHandler("settle", handle_settle_command))
    app.add_handler(CommandHandler("delete", handle_delete_command))
    app.add_handler(CommandHandler("edit", handle_edit_command))
    app.add_handler(CommandHandler("export", handle_export_command))
    app.add_handler(CommandHandler("records", handle_records_command))

    # --- Photo (receipt scanning) ---
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # --- Text messages (state-machine router) ---
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _route_text_message)
    )

    # --- Inline keyboard callbacks (flow router) ---
    app.add_handler(CallbackQueryHandler(_route_callback))

    logger.info("SplitBot starting — polling for updates...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
