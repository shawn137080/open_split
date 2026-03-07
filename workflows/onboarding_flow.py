"""Onboarding conversation flow for new households."""

import logging
import re
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

STATE_AWAITING_HOUSEHOLD_NAME = "AWAITING_HOUSEHOLD_NAME"
STATE_AWAITING_MEMBER_NAME = "AWAITING_MEMBER_NAME"
STATE_AWAITING_MORE_MEMBERS = "AWAITING_MORE_MEMBERS"
STATE_COMPLETE = "COMPLETE"

ONBOARDING_STATES = {
    STATE_AWAITING_HOUSEHOLD_NAME,
    STATE_AWAITING_MEMBER_NAME,
    STATE_AWAITING_MORE_MEMBERS,
}

# Callback data prefixes
CB_ADD_MEMBER = "onboard:add_member"
CB_DONE_MEMBERS = "onboard:done_members"


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def _more_members_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Add member", callback_data=CB_ADD_MEMBER),
            InlineKeyboardButton("Done ✅", callback_data=CB_DONE_MEMBERS),
        ]]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    """Persist household to the database and show the completion message."""
    household_name: str = ctx["household_name"]
    members: list = ctx["members"]

    # Create the group record
    database.create_group(
        group_id=group_id,
        household_name=household_name,
        admin_user_id=user_id,
    )

    # Add members
    for name in members:
        database.add_member(group_id=group_id, name=name)

    # Clear state
    database.clear_state(user_id, group_id)

    members_text = ", ".join(members)
    completion_msg = (
        f"🎉 <b>Setup complete! Pip the Squirrel is ready to track.</b> 🐿️\n\n"
        f"Household: <b>{household_name}</b>\n"
        f"Members: {members_text}\n\n"
        f"<b>What's next:</b>\n"
        f"  • /add_fixed — set up recurring monthly expenses (rent, internet…)\n"
        f"  • /settings — set timezone, currency, and default tax rate\n"
        f"  • Send a photo of a receipt to let Pip scan it 📸\n"
        f"  • /help — see all commands"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(completion_msg, parse_mode="HTML")
    elif update.effective_message:
        await update.effective_message.reply_text(completion_msg, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_onboarding_state(user_id: str, group_id: str) -> bool:
    """Return True if the user has an active onboarding state."""
    state_row = database.get_state(user_id, group_id)
    if state_row is None:
        return False
    return state_row.get("state") in ONBOARDING_STATES


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command. Entry point for onboarding."""
    if update.effective_chat is None or update.effective_user is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    # If the group is already configured, show status only
    if database.group_exists(group_id):
        await update.effective_message.reply_text(
            "🐿️ Pip says this household is already configured! Use /help to see what I can do."
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
        "👋 <b>Welcome to NutSplit!</b> Pip the Squirrel is here to help you track expenses without the fuss 🐿️.\n\n"
        "First, what should we call your household? (e.g., 'The Nut House', 'Apt 4B')"
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

    state_row = database.get_state(user_id, group_id)
    state = state_row.get("state") if state_row else None
    ctx = state_row.get("context") or {} if state_row else {}

    if state is None or state not in ONBOARDING_STATES:
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
            f"🌰 Got it! Now let's add your first household member.\n"
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
            f"🌰 Added {text}! Add another member?",
            reply_markup=_more_members_keyboard(),
        )
        return

    # ------------------------------------------------------------------
    # AWAITING_MORE_MEMBERS — expect button presses
    # ------------------------------------------------------------------
    if state == STATE_AWAITING_MORE_MEMBERS:
        await update.effective_message.reply_text(
            "Please use the buttons below to continue.",
            reply_markup=_more_members_keyboard(),
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

    state_row = database.get_state(user_id, group_id)
    state = state_row.get("state") if state_row else None
    ctx = state_row.get("context") or {} if state_row else {}

    if state is None or state not in ONBOARDING_STATES:
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
    # Done adding members → finalize immediately
    # ------------------------------------------------------------------
    if data == CB_DONE_MEMBERS:
        members: list = ctx.get("members", [])
        if len(members) < 2:
            await query.edit_message_text(
                "You need at least 2 household members to continue.\n"
                "Who else lives in the household?",
                reply_markup=_more_members_keyboard(),
            )
            database.set_state(user_id, group_id, STATE_AWAITING_MEMBER_NAME, ctx)
            return

        # All done — finalize
        await _finalize_setup(update, context, user_id, group_id, ctx)
        return
