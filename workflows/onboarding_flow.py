"""Onboarding conversation flow for new households."""

import logging
import re
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
from tools.sheets_manager import create_sheet, link_sheet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

STATE_AWAITING_HOUSEHOLD_NAME = "AWAITING_HOUSEHOLD_NAME"
STATE_AWAITING_MEMBER_NAME = "AWAITING_MEMBER_NAME"
STATE_AWAITING_MORE_MEMBERS = "AWAITING_MORE_MEMBERS"
STATE_AWAITING_FIXED_EXPENSE = "AWAITING_FIXED_EXPENSE"
STATE_AWAITING_SHEET_CHOICE = "AWAITING_SHEET_CHOICE"
STATE_AWAITING_SHEET_ID = "AWAITING_SHEET_ID"
STATE_COMPLETE = "COMPLETE"

ONBOARDING_STATES = {
    STATE_AWAITING_HOUSEHOLD_NAME,
    STATE_AWAITING_MEMBER_NAME,
    STATE_AWAITING_MORE_MEMBERS,
    STATE_AWAITING_FIXED_EXPENSE,
    STATE_AWAITING_SHEET_CHOICE,
    STATE_AWAITING_SHEET_ID,
}

# Callback data prefixes
CB_ADD_MEMBER = "onboard:add_member"
CB_DONE_MEMBERS = "onboard:done_members"
CB_ADD_FIXED = "onboard:add_fixed"
CB_SKIP_FIXED = "onboard:skip_fixed"
CB_NEW_SHEET = "onboard:new_sheet"
CB_LINK_SHEET = "onboard:link_sheet"


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def _more_members_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Add member", callback_data=CB_ADD_MEMBER),
            InlineKeyboardButton("Done adding members", callback_data=CB_DONE_MEMBERS),
        ]]
    )


def _fixed_expense_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Add fixed expense", callback_data=CB_ADD_FIXED),
            InlineKeyboardButton("Skip for now", callback_data=CB_SKIP_FIXED),
        ]]
    )


def _add_another_fixed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Add another", callback_data=CB_ADD_FIXED),
            InlineKeyboardButton("Done with fixed expenses", callback_data=CB_SKIP_FIXED),
        ]]
    )


def _sheet_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Create new Google Sheet", callback_data=CB_NEW_SHEET),
            InlineKeyboardButton("Link existing Sheet", callback_data=CB_LINK_SHEET),
        ]]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_sheet_id(text: str) -> str:
    """Extract a Google Sheet ID from a URL or return the raw string."""
    # Pattern: /spreadsheets/d/<id>/
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", text)
    if match:
        return match.group(1)
    return text.strip()


def _parse_fixed_expense(text: str) -> Optional[dict]:
    """
    Parse 'Name | Amount | WhoPays | Split' format.
    Returns dict with keys: description, amount, paid_by, split_type
    or None if the format is invalid.
    """
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 4:
        return None
    description, amount_str, paid_by, split_type = parts
    if not description or not paid_by or not split_type:
        return None
    try:
        amount = float(amount_str.replace(",", ".").replace("$", "").strip())
    except ValueError:
        return None
    if amount <= 0:
        return None
    return {
        "description": description,
        "amount": amount,
        "paid_by": paid_by,
        "split_type": split_type.lower(),
    }


async def _finalize_setup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: str,
    group_id: str,
    ctx: dict,
) -> None:
    """
    Persist everything to the database and notify the group.
    Called after sheet_id is known (either created or linked).
    """
    household_name: str = ctx["household_name"]
    members: list = ctx["members"]
    fixed_expenses: list = ctx.get("fixed_expenses", [])
    sheet_id: str = ctx["sheet_id"]

    # Create the group record
    database.create_group(
        group_id=group_id,
        household_name=household_name,
        admin_user_id=user_id,
    )
    database.update_group_sheet_id(group_id, sheet_id)

    # Add members; track name -> db id for fixed expense linkage
    member_id_map: dict = {}
    for name in members:
        mid = database.add_member(group_id=group_id, name=name)
        member_id_map[name.lower()] = mid

    # Add fixed expenses
    for fe in fixed_expenses:
        paid_by_name: str = fe["paid_by"]
        paid_by_id: Optional[int] = member_id_map.get(paid_by_name.lower())
        if paid_by_id is None:
            import logging
            logging.warning(
                "Fixed expense '%s': paid_by '%s' not found in members %s — skipping.",
                fe["description"], paid_by_name, list(member_id_map.keys()),
            )
            continue  # skip rather than silently misattribute
        database.add_fixed_expense(
            group_id=group_id,
            description=fe["description"],
            amount=fe["amount"],
            paid_by_member_id=paid_by_id,
            split_type=fe["split_type"],
        )

    # Clear state
    database.clear_state(user_id, group_id)

    # Build completion message
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    members_text = "\n".join(f"  - {m}" for m in members)
    fixed_text = ""
    if fixed_expenses:
        fe_lines = "\n".join(
            f"  - {fe['description']}: ${fe['amount']:.2f} (paid by {fe['paid_by']}, split: {fe['split_type']})"
            for fe in fixed_expenses
        )
        fixed_text = f"\nFixed monthly expenses:\n{fe_lines}"

    completion_msg = (
        f"Setup complete!\n\n"
        f"Household: {household_name}\n"
        f"Members:\n{members_text}"
        f"{fixed_text}\n\n"
        f"Sheet: {sheet_url}\n\n"
        f"To add an expense, just send a receipt photo!"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(completion_msg)
    else:
        await update.effective_message.reply_text(completion_msg)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_onboarding_state(user_id: str, group_id: str) -> bool:
    """Return True if the user has an active onboarding state."""
    state, _ = database.get_state(user_id, group_id)
    return state in ONBOARDING_STATES


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command. Entry point for onboarding."""
    if update.effective_chat is None or update.effective_user is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    # If the group is already configured, show status only
    if database.group_exists(group_id):
        await update.effective_message.reply_text(
            "This household is already configured. Use /settings to view."
        )
        return

    # Start onboarding
    database.set_state(
        user_id=user_id,
        group_id=group_id,
        state=STATE_AWAITING_HOUSEHOLD_NAME,
        context={
            "household_name": "",
            "members": [],
            "fixed_expenses": [],
            "step": STATE_AWAITING_HOUSEHOLD_NAME,
        },
    )

    await update.effective_message.reply_text(
        "Welcome to auto_split! Let's set up your household.\n\n"
        "What's your household name? (e.g., 'The Smith Household')"
    )


async def handle_onboarding_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle text messages during onboarding state machine."""
    if update.effective_chat is None or update.effective_user is None:
        return
    if update.effective_message is None or update.effective_message.text is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    text = update.effective_message.text.strip()

    state, ctx = database.get_state(user_id, group_id)

    if state is None or ctx is None or state not in ONBOARDING_STATES:
        return  # Not in onboarding — ignore

    # ------------------------------------------------------------------
    # AWAITING_HOUSEHOLD_NAME
    # ------------------------------------------------------------------
    if state == STATE_AWAITING_HOUSEHOLD_NAME:
        if not text:
            await update.effective_message.reply_text(
                "Please enter a name for your household."
            )
            return

        ctx["household_name"] = text
        ctx["step"] = STATE_AWAITING_MEMBER_NAME
        database.set_state(user_id, group_id, STATE_AWAITING_MEMBER_NAME, ctx)

        await update.effective_message.reply_text(
            f"Got it! Now add your first household member.\n"
            f"What's the first member's name?"
        )
        return

    # ------------------------------------------------------------------
    # AWAITING_MEMBER_NAME
    # ------------------------------------------------------------------
    if state == STATE_AWAITING_MEMBER_NAME:
        if not text:
            await update.effective_message.reply_text(
                "Please enter the member's name."
            )
            return

        ctx["members"].append(text)
        ctx["step"] = STATE_AWAITING_MORE_MEMBERS
        database.set_state(user_id, group_id, STATE_AWAITING_MORE_MEMBERS, ctx)

        await update.effective_message.reply_text(
            f"Added {text}! Add another member?",
            reply_markup=_more_members_keyboard(),
        )
        return

    # ------------------------------------------------------------------
    # AWAITING_FIXED_EXPENSE  (text entry for expense details)
    # ------------------------------------------------------------------
    if state == STATE_AWAITING_FIXED_EXPENSE:
        parsed = _parse_fixed_expense(text)
        if parsed is None:
            await update.effective_message.reply_text(
                "I couldn't parse that. Please use the format:\n"
                "Name | Amount | WhoPays | Split\n\n"
                "Example: Internet | 58 | Karlos | equal\n"
                "Split options: equal (split evenly), or a member's name "
                "(that person owes it all)"
            )
            return

        ctx["fixed_expenses"].append(parsed)
        ctx["step"] = STATE_AWAITING_FIXED_EXPENSE
        database.set_state(user_id, group_id, STATE_AWAITING_FIXED_EXPENSE, ctx)

        await update.effective_message.reply_text(
            f"Added fixed expense: {parsed['description']} — "
            f"${parsed['amount']:.2f} paid by {parsed['paid_by']} "
            f"(split: {parsed['split_type']}).\n\n"
            "Add another fixed expense?",
            reply_markup=_add_another_fixed_keyboard(),
        )
        return

    # ------------------------------------------------------------------
    # AWAITING_SHEET_ID
    # ------------------------------------------------------------------
    if state == STATE_AWAITING_SHEET_ID:
        sheet_id = _extract_sheet_id(text)
        accessible = link_sheet(sheet_id)

        if not accessible:
            await update.effective_message.reply_text(
                "Couldn't access that sheet. Make sure it's shared with the "
                "bot's account. Try again:"
            )
            return

        ctx["sheet_id"] = sheet_id
        # Do not set STATE_COMPLETE here — _finalize_setup owns the state transition
        # and calls clear_state() as its final step.
        await _finalize_setup(update, context, user_id, group_id, ctx)
        return

    # ------------------------------------------------------------------
    # AWAITING_MORE_MEMBERS / AWAITING_SHEET_CHOICE — expect button presses
    # ------------------------------------------------------------------
    if state in (STATE_AWAITING_MORE_MEMBERS, STATE_AWAITING_SHEET_CHOICE):
        # User typed instead of pressing a button; re-show the keyboard
        if state == STATE_AWAITING_MORE_MEMBERS:
            await update.effective_message.reply_text(
                "Please use the buttons below to continue.",
                reply_markup=_more_members_keyboard(),
            )
        else:
            await update.effective_message.reply_text(
                "Please use the buttons below to choose a sheet option.",
                reply_markup=_sheet_choice_keyboard(),
            )
        return


async def handle_onboarding_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline keyboard callbacks during onboarding."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()  # Acknowledge the callback immediately

    if update.effective_chat is None or update.effective_user is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    data: str = query.data or ""

    if not data.startswith("onboard:"):
        return  # Not ours

    state, ctx = database.get_state(user_id, group_id)

    if state is None or ctx is None or state not in ONBOARDING_STATES:
        await query.edit_message_text(
            "No active onboarding session found. Send /start to begin."
        )
        return

    # ------------------------------------------------------------------
    # Add member button
    # ------------------------------------------------------------------
    if data == CB_ADD_MEMBER:
        ctx["step"] = STATE_AWAITING_MEMBER_NAME
        database.set_state(user_id, group_id, STATE_AWAITING_MEMBER_NAME, ctx)
        await query.edit_message_text(
            "What's the next member's name?"
        )
        return

    # ------------------------------------------------------------------
    # Done adding members
    # ------------------------------------------------------------------
    if data == CB_DONE_MEMBERS:
        members: list = ctx.get("members", [])
        if len(members) < 2:
            await query.edit_message_text(
                "You need at least 2 household members to continue.\n"
                "Who else lives in the household?",
                reply_markup=_more_members_keyboard(),
            )
            # Put back into AWAITING_MEMBER_NAME so next text is captured
            ctx["step"] = STATE_AWAITING_MEMBER_NAME
            database.set_state(user_id, group_id, STATE_AWAITING_MEMBER_NAME, ctx)
            return

        ctx["step"] = STATE_AWAITING_FIXED_EXPENSE
        database.set_state(user_id, group_id, STATE_AWAITING_FIXED_EXPENSE, ctx)

        members_list = ", ".join(members)
        await query.edit_message_text(
            f"Great! Your household has: {members_list}\n\n"
            "Do you have any fixed monthly expenses to set up?\n"
            "(e.g., Rent, Internet, Netflix — expenses that repeat every month)\n\n"
            "You can always add these later with /add-fixed",
            reply_markup=_fixed_expense_keyboard(),
        )
        return

    # ------------------------------------------------------------------
    # Add fixed expense button
    # ------------------------------------------------------------------
    if data == CB_ADD_FIXED:
        ctx["step"] = STATE_AWAITING_FIXED_EXPENSE
        database.set_state(user_id, group_id, STATE_AWAITING_FIXED_EXPENSE, ctx)

        await query.edit_message_text(
            "Tell me about this fixed expense in this format:\n"
            "Name | Amount | WhoPays | Split\n\n"
            "Example: Internet | 58 | Karlos | equal\n\n"
            "Split options:\n"
            "  - equal — split evenly among all members\n"
            "  - a member's name — that person owes it all"
        )
        return

    # ------------------------------------------------------------------
    # Skip fixed expenses
    # ------------------------------------------------------------------
    if data == CB_SKIP_FIXED:
        ctx["step"] = STATE_AWAITING_SHEET_CHOICE
        database.set_state(user_id, group_id, STATE_AWAITING_SHEET_CHOICE, ctx)

        await query.edit_message_text(
            "Last step! How should I store your expenses?",
            reply_markup=_sheet_choice_keyboard(),
        )
        return

    # ------------------------------------------------------------------
    # Create new Google Sheet
    # ------------------------------------------------------------------
    if data == CB_NEW_SHEET:
        household_name: str = ctx["household_name"]
        members = ctx.get("members", [])

        await query.edit_message_text("Creating your Google Sheet...")

        try:
            sheet_id = create_sheet(household_name, members)
        except Exception as exc:
            logger.exception("Sheet creation failed: %s", exc)
            await query.edit_message_text(
                "Something went wrong while creating the Google Sheet.\n"
                "Please try again:",
                reply_markup=_sheet_choice_keyboard(),
            )
            return

        ctx["sheet_id"] = sheet_id
        await _finalize_setup(update, context, user_id, group_id, ctx)
        return

    # ------------------------------------------------------------------
    # Link existing Google Sheet
    # ------------------------------------------------------------------
    if data == CB_LINK_SHEET:
        ctx["step"] = STATE_AWAITING_SHEET_ID
        database.set_state(user_id, group_id, STATE_AWAITING_SHEET_ID, ctx)

        await query.edit_message_text(
            "Please send me the Google Sheet ID or URL.\n\n"
            "Make sure the sheet is shared with the bot's Google account."
        )
        return
