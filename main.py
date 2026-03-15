"""auto_split — Household Expense Tracker Bot

Telegram bot that tracks shared household expenses.
All data is stored locally in SQLite — no external services required
beyond Telegram and (optionally) Gemini for receipt OCR.
Multi-tenant: each Telegram group is an independent household.
"""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
from config import IS_PRO, TELEGRAM_TOKEN, ADMIN_TELEGRAM_ID, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
try:
    from pro.saas_bridge import init_saas
except ImportError:
    init_saas = None  # type: ignore
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
    CB_MONTHPICK_SUMMARY,
    CB_MONTHPICK_HISTORY,
    CB_MONTHPICK_EXPORT,
    handle_delete_command,
    handle_edit_command,
    handle_history_command,
    handle_last_command,
    handle_multisummary_command,
    handle_owe_command,
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
from workflows.feedback_flow import handle_feedback

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /help command
# ---------------------------------------------------------------------------

_HELP_TEXT = (
    "🥜 <b>Welcome to NutSplit!</b> 🐿️\n"
    "I am Pip the Squirrel, your friendly household expense tracker. I help you track shared expenses, split bills, and keep balances clear!\n"
    "\n"
    "Here is what I can do for you:\n"
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
    "/owe — who owes whom right now\n"
    "/records — monthly overview\n"
    "\n"
    "<b>Manage</b>\n"
    "/settle name 50.00 — record payment\n"
    "/edit EXP-003 amount 55.00 — edit expense\n"
    "/delete EXP-003 — remove expense\n"
    "/export [feb] — download CSV\n"
    "/feedback — submit a bug or feature request\n"
    "\n"
    "<b>Fixed Expenses</b>\n"
    "/add_fixed — add a recurring monthly expense\n"
    "/fixedexp — view & manage fixed expenses\n"
    "\n"
    "<b>Community Edition</b>\n"
    "This is the free open-source core. For unlimited AI scanning and premium features, check out:\n"
    "🚀 <b><a href='https://t.me/NutSplitBot'>Official NutSplit Pro</a></b>"
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


async def _handle_admin_upgrade(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Hidden command to upgrade a group to Pro."""
    if not update.effective_user or not update.effective_message:
        return
    user_id = str(update.effective_user.id)
    if not ADMIN_TELEGRAM_ID or user_id != str(ADMIN_TELEGRAM_ID):
        await update.effective_message.reply_text("⛔ Unauthorized.")
        return
    
    if not context.args:
        await update.effective_message.reply_text("Usage: /admin_upgrade <group_id>")
        return
        
    target_group = context.args[0]
    database.enable_pro(target_group, 1)
    await update.effective_message.reply_text(f"✅ Group {target_group} upgraded to Pro.")


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
        state_row = database.get_state(user_id, group_id)
        if state_row and state_row.get("state") in (
            STATE_ADDFIXED_STARTMONTH,
            STATE_ADDFIXED_DESC,
            STATE_ADDFIXED_AMOUNT,
            STATE_FIXEDEXP_EDIT_AMOUNT,
        ):
            await handle_add_fixed_message(update, context)
        else:
            # Fallback to natural language routing via LLM
            text = update.effective_message.text
            if text and not text.startswith("/"):
                # Run the synchronous LLM call in a thread pool to avoid blocking the event loop
                import asyncio
                from tools.llm_router import route_intent
                loop = asyncio.get_running_loop()
                intent = await loop.run_in_executor(None, route_intent, text)
                
                pro_active = IS_PRO or database.is_group_pro(group_id)

                if intent == "summary":
                    await handle_summary_command(update, context)
                elif intent == "history":
                    await handle_history_command(update, context)
                elif intent == "owe":
                    await handle_owe_command(update, context)
                elif intent in ("stats", "budget", "upgrade"):
                    await update.effective_message.reply_text(
                        "🚀 <b>NutSplit Pro Feature</b>\n\n"
                        "Visual stats and budgets are available in our official hosted version. "
                        "Join thousands of users at @NutSplitBot!",
                        parse_mode="HTML"
                    )
                elif intent == "export":
                    await handle_export_command(update, context)
                elif intent == "fixed":
                    await handle_fixedexp_command(update, context)
                elif intent == "help":
                    await _handle_help(update, context)


async def _handle_month_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle month picker button presses from /summary, /history, /export."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data: str = query.data or ""

    if data.startswith(CB_MONTHPICK_SUMMARY):
        month_label = data[len(CB_MONTHPICK_SUMMARY):]
        context.args = month_label.split()  # type: ignore[assignment]
        await query.edit_message_text(f"📊 Loading {month_label}…")
        await handle_summary_command(update, context)
    elif data.startswith(CB_MONTHPICK_HISTORY):
        month_label = data[len(CB_MONTHPICK_HISTORY):]
        context.args = month_label.split()  # type: ignore[assignment]
        await query.edit_message_text(f"📋 Loading {month_label}…")
        await handle_history_command(update, context)
    elif data.startswith(CB_MONTHPICK_EXPORT):
        month_label = data[len(CB_MONTHPICK_EXPORT):]
        context.args = month_label.split()  # type: ignore[assignment]
        await query.edit_message_text(f"📁 Exporting {month_label}…")
        await handle_export_command(update, context)


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
        or data.startswith(CB_FE_EDIT_SPLIT_FOR_PFX)
        or data.startswith(CB_FE_EDIT_BACK)
        or data == CB_FE_BACK
    ):
        await handle_fixedexp_callback(update, context)
    elif data.startswith("cfg:"):
        await handle_settings_callback(update, context)
    elif data.startswith("rec:"):
        await handle_records_callback(update, context)
    elif data.startswith("mp:"):
        await _handle_month_pick_callback(update, context)
# SaaS features are now bridged via pro.saas_bridge


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
    app.add_handler(CommandHandler("summary",  handle_summary_command))
    app.add_handler(CommandHandler("history",  handle_history_command))
    app.add_handler(CommandHandler("last",     handle_last_command))
    app.add_handler(CommandHandler("owe",      handle_owe_command))
    app.add_handler(CommandHandler("settle",   handle_settle_command))
    app.add_handler(CommandHandler("delete",   handle_delete_command))
    app.add_handler(CommandHandler("edit",     handle_edit_command))
    app.add_handler(CommandHandler("export",   handle_export_command))
    app.add_handler(CommandHandler("records",  handle_records_command))
    app.add_handler(CommandHandler("feedback", handle_feedback))

    # --- Pro teaser ---
    async def _pro_teaser(u, c):
        await u.effective_message.reply_text(
            "🚀 <b>NutSplit Pro</b>\n\n"
            "This feature (visual stats, budgets, and unlimited AI OCR) is available in our official hosted version.\n\n"
            "👉 <b><a href='https://t.me/NutSplitBot'>Try it here!</a></b>",
            parse_mode="HTML"
        )

    app.add_handler(CommandHandler("stats",   _pro_teaser))
    app.add_handler(CommandHandler("budget",  _pro_teaser))
    app.add_handler(CommandHandler("upgrade", _pro_teaser))

    # --- Admin ---
    app.add_handler(CommandHandler("admin_upgrade", _handle_admin_upgrade))

    # --- Photo (receipt scanning) ---
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # --- Text messages (state-machine router) ---
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _route_text_message)
    )

    # --- Inline keyboard callbacks (flow router) ---
    app.add_handler(CallbackQueryHandler(_route_callback))

    async def _set_commands(application: Application) -> None:
        await application.bot.set_my_commands([
            BotCommand("add",       "Guided expense entry"),
            BotCommand("summary",   "Balance & settlement [month]"),
            BotCommand("history",   "Expense list [month]"),
            BotCommand("owe",       "Quick: who owes whom"),
            BotCommand("records",   "Monthly overview"),
            BotCommand("last",      "Most recent expense"),
            BotCommand("settle",    "Record a payment"),
            BotCommand("edit",      "Edit an expense"),
            BotCommand("delete",    "Delete an expense"),
            BotCommand("export",    "Download CSV [month]"),
            BotCommand("add_fixed", "Add recurring fixed expense"),
            BotCommand("fixedexp",  "Manage fixed expenses"),
            BotCommand("stats",     "Spending trends ⭐ Pro"),
            BotCommand("budget",    "Category budgets ⭐ Pro"),
            BotCommand("upgrade",   "Get NutSplit Pro ($4.99/mo)"),
            BotCommand("settings",  "Household settings"),
            BotCommand("start",     "Household onboarding"),
            BotCommand("help",      "Show all commands"),
            BotCommand("cancel",    "Cancel current flow"),
        ])

    app.post_init = _set_commands

    if init_saas:
        init_saas(app)

    logger.info("SplitBot starting — polling for updates...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
