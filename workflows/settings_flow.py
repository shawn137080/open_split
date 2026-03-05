"""Settings flow — /settings command.

Lets household admin view and update:
  - Household name
  - Timezone (with auto-filled tax rate)
  - Currency
  - Default tax rate (manual override)
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
from tools.tax_rates import (
    CANADA_TIMEZONES,
    OTHER_TIMEZONES,
    US_TIMEZONES,
    get_tax_rate,
    tax_pct_for_timezone,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

STATE_SETTINGS_MAIN        = "settings_main"
STATE_SETTINGS_NAME        = "settings_name"
STATE_SETTINGS_CURRENCY    = "settings_currency"
STATE_SETTINGS_TAX         = "settings_tax"

# ---------------------------------------------------------------------------
# Callback data
# ---------------------------------------------------------------------------

CB_SET_NAME         = "cfg:name"
CB_SET_TZ           = "cfg:tz"
CB_SET_TZ_CA        = "cfg:tz_ca"
CB_SET_TZ_US        = "cfg:tz_us"
CB_SET_TZ_OTHER     = "cfg:tz_other"
CB_SET_TZ_PFX       = "cfg:tz:"      # cfg:tz:<timezone>
CB_SET_CURRENCY     = "cfg:currency"
CB_SET_TAX          = "cfg:tax"
CB_SET_CURRENCY_PFX = "cfg:cur:"     # cfg:cur:CAD
CB_BACK_MAIN        = "cfg:back"

SETTINGS_CALLBACK_PREFIXES = (
    "cfg:",
)

_CURRENCIES = ["CAD", "USD", "GBP", "EUR", "SGD", "HKD", "AUD"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_text(group: dict) -> str:
    tz = group.get("timezone") or "America/Toronto"
    tax_label, _ = get_tax_rate(tz)
    tax_override = group.get("default_tax_pct", 0.0)
    return (
        f"⚙️ <b>Settings — {group.get('household_name', '?')}</b>\n\n"
        f"🕐 Timezone: <code>{tz}</code>\n"
        f"💰 Currency: {group.get('currency', 'CAD')}\n"
        f"🧾 Default Tax: {tax_override:.2f}% "
        f"<i>({tax_label})</i>\n\n"
        "Tap a field to edit it."
    )


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Household Name",   callback_data=CB_SET_NAME)],
        [InlineKeyboardButton("🌍 Timezone / Region", callback_data=CB_SET_TZ)],
        [InlineKeyboardButton("💱 Currency",          callback_data=CB_SET_CURRENCY)],
        [InlineKeyboardButton("🧾 Tax Rate (manual)", callback_data=CB_SET_TAX)],
    ])


def _tz_region_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍁 Canada",      callback_data=CB_SET_TZ_CA)],
        [InlineKeyboardButton("🇺🇸 United States", callback_data=CB_SET_TZ_US)],
        [InlineKeyboardButton("🌐 Other",       callback_data=CB_SET_TZ_OTHER)],
        [InlineKeyboardButton("← Back",         callback_data=CB_BACK_MAIN)],
    ])


def _tz_list_keyboard(zones: list[tuple[str, str]], back_data: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(label, callback_data=f"{CB_SET_TZ_PFX}{tz}")]
               for tz, label in zones]
    buttons.append([InlineKeyboardButton("← Back", callback_data=back_data)])
    return InlineKeyboardMarkup(buttons)


def _currency_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(c, callback_data=f"{CB_SET_CURRENCY_PFX}{c}")]
            for c in _CURRENCIES]
    rows.append([InlineKeyboardButton("← Back", callback_data=CB_BACK_MAIN)])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# /settings  command
# ---------------------------------------------------------------------------


async def handle_settings_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id = str(update.effective_chat.id)
    group = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text("Please run /start first.")
        return

    await update.effective_message.reply_text(
        _settings_text(group),
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


# ---------------------------------------------------------------------------
# Callback router
# ---------------------------------------------------------------------------


async def handle_settings_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None or update.effective_chat is None:
        return
    await query.answer()

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else group_id
    data: str = query.data or ""

    group = database.get_group(group_id)
    if group is None:
        await query.edit_message_text("Please run /start first.")
        return

    # ── main menu ───────────────────────────────────────────────────────────
    if data == CB_BACK_MAIN:
        database.clear_state(user_id, group_id)
        await query.edit_message_text(
            _settings_text(group),
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
        return

    # ── name ────────────────────────────────────────────────────────────────
    if data == CB_SET_NAME:
        database.set_state(user_id, group_id, STATE_SETTINGS_NAME, {})
        await query.edit_message_text(
            "Type the new household name:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Cancel", callback_data=CB_BACK_MAIN)
            ]]),
        )
        return

    # ── timezone region picker ───────────────────────────────────────────────
    if data == CB_SET_TZ:
        await query.edit_message_text(
            "Select your region:",
            reply_markup=_tz_region_keyboard(),
        )
        return

    if data == CB_SET_TZ_CA:
        await query.edit_message_text(
            "🍁 Select your province/territory:",
            reply_markup=_tz_list_keyboard(CANADA_TIMEZONES, CB_SET_TZ),
        )
        return

    if data == CB_SET_TZ_US:
        await query.edit_message_text(
            "🇺🇸 Select your state (common):",
            reply_markup=_tz_list_keyboard(US_TIMEZONES, CB_SET_TZ),
        )
        return

    if data == CB_SET_TZ_OTHER:
        await query.edit_message_text(
            "🌐 Select region:",
            reply_markup=_tz_list_keyboard(OTHER_TIMEZONES, CB_SET_TZ),
        )
        return

    if data.startswith(CB_SET_TZ_PFX):
        tz = data[len(CB_SET_TZ_PFX):]
        tax_label, tax_pct = get_tax_rate(tz)
        database.update_group(group_id, timezone=tz, default_tax_pct=tax_pct)
        group = database.get_group(group_id)
        await query.edit_message_text(
            f"✅ Timezone set to <code>{tz}</code>\n"
            f"🧾 Default tax auto-set to <b>{tax_pct:.3g}%</b> ({tax_label})\n\n"
            + _settings_text(group),  # type: ignore[arg-type]
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
        return

    # ── currency ─────────────────────────────────────────────────────────────
    if data == CB_SET_CURRENCY:
        await query.edit_message_text(
            "Select currency:",
            reply_markup=_currency_keyboard(),
        )
        return

    if data.startswith(CB_SET_CURRENCY_PFX):
        currency = data[len(CB_SET_CURRENCY_PFX):]
        database.update_group(group_id, currency=currency)
        group = database.get_group(group_id)
        await query.edit_message_text(
            f"✅ Currency set to <b>{currency}</b>\n\n" + _settings_text(group),  # type: ignore[arg-type]
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
        return

    # ── manual tax override ───────────────────────────────────────────────────
    if data == CB_SET_TAX:
        database.set_state(user_id, group_id, STATE_SETTINGS_TAX, {})
        current = group.get("default_tax_pct", 0.0)
        await query.edit_message_text(
            f"Current tax rate: <b>{current:.3g}%</b>\n\n"
            "Type the new tax rate (e.g. <code>13</code> for 13%)\n"
            "Enter <code>0</code> for no tax.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("← Cancel", callback_data=CB_BACK_MAIN)
            ]]),
        )
        return


# ---------------------------------------------------------------------------
# Text message handler (for name and tax input)
# ---------------------------------------------------------------------------


async def handle_settings_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_chat is None or update.message is None:
        return
    if update.message.text is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else group_id
    text = update.message.text.strip()

    state_row = database.get_state(user_id, group_id)
    if state_row is None:
        return

    state = state_row.get("state", "")

    # ── name input ────────────────────────────────────────────────────────────
    if state == STATE_SETTINGS_NAME:
        if not text:
            await update.message.reply_text("Name can't be empty. Try again:")
            return
        database.update_group(group_id, household_name=text)
        database.clear_state(user_id, group_id)
        group = database.get_group(group_id)
        await update.message.reply_text(
            f"✅ Household name updated to <b>{text}</b>",
            parse_mode="HTML",
        )
        if group:
            await update.message.reply_text(
                _settings_text(group),
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
        return

    # ── tax rate input ─────────────────────────────────────────────────────────
    if state == STATE_SETTINGS_TAX:
        try:
            pct = float(text.replace("%", "").strip())
            if pct < 0 or pct > 100:
                raise ValueError("out of range")
        except ValueError:
            await update.message.reply_text("Please enter a valid percentage, e.g. 13 or 13.5")
            return
        try:
            database.update_group(group_id, default_tax_pct=pct)
        except Exception as exc:
            logger.exception("update_group failed for tax: %s", exc)
            await update.message.reply_text(
                f"⚠️ Could not save tax rate: {exc}\nPlease try again."
            )
            return
        database.clear_state(user_id, group_id)
        group = database.get_group(group_id)
        await update.message.reply_text(
            f"✅ Default tax rate set to <b>{pct:.3g}%</b>",
            parse_mode="HTML",
        )
        if group:
            await update.message.reply_text(
                _settings_text(group),
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
        return


def is_settings_state(user_id: str, group_id: str) -> bool:
    """Return True if the user is in an active settings flow state."""
    state_row = database.get_state(user_id, group_id)
    if state_row is None:
        return False
    return state_row.get("state", "").startswith("settings_")
