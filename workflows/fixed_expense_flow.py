"""Fixed expense management flows.

Commands:
  /add-fixed    — guided wizard to add a new fixed monthly expense
  /fixedexp     — view, edit, skip, and disable fixed expenses
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State names
# ---------------------------------------------------------------------------

STATE_ADDFIXED_DESC        = "addfixed_desc"
STATE_ADDFIXED_AMOUNT      = "addfixed_amount"
STATE_ADDFIXED_PAIDBY      = "addfixed_paidby"
STATE_ADDFIXED_SPLIT       = "addfixed_split"
STATE_ADDFIXED_STARTMONTH  = "addfixed_startmonth"
STATE_ADDFIXED_CONFIRM     = "addfixed_confirm"

STATE_FIXEDEXP_LIST        = "fixedexp_list"
STATE_FIXEDEXP_DETAIL      = "fixedexp_detail"
STATE_FIXEDEXP_EDIT_AMOUNT = "fixedexp_edit_amount"

# ---------------------------------------------------------------------------
# Callback data prefixes
# ---------------------------------------------------------------------------

CB_ADDFIXED_PAIDBY_PFX   = "afpb:"
CB_ADDFIXED_SPLIT_EQUAL  = "afs:equal"
CB_ADDFIXED_SPLIT_MINE   = "afs:mine"   # entire amount borne by payer
CB_ADDFIXED_START_THIS   = "afst:this"
CB_ADDFIXED_START_NEXT   = "afst:next"
CB_ADDFIXED_START_TYPE   = "afst:type"  # user will type a month
CB_ADDFIXED_CONFIRM_YES  = "afc:yes"
CB_ADDFIXED_CONFIRM_NO   = "afc:no"

CB_FE_SELECT_PFX         = "fes:"       # fes:<id>
CB_FE_SKIP_THIS          = "feskip:"    # feskip:<id>
CB_FE_UNSKIP             = "feunskip:"
CB_FE_CANCEL_FUTURE      = "fecf:"      # fecf:<id>  — show confirmation
CB_FE_CANCEL_FUTURE_YES  = "fecfyes:"   # fecfyes:<id> — confirmed
CB_FE_CANCEL_FUTURE_NO   = "fecfno:"    # fecfno:<id>  — aborted → back to detail
CB_FE_BACK               = "feback"

# Edit callbacks
CB_FE_EDIT               = "fee_edit:"  # fee_edit:<id>  — show edit menu
CB_FE_EDIT_AMOUNT        = "fee_amt:"   # fee_amt:<id>   — prompt for new amount
CB_FE_EDIT_PAIDBY_PFX    = "fee_pb:"    # fee_pb:<id>:<name>
CB_FE_EDIT_SPLIT_EQUAL   = "fee_se:"    # fee_se:<id>
CB_FE_EDIT_SPLIT_MINE    = "fee_sm:"    # fee_sm:<id>
CB_FE_EDIT_BACK          = "fee_back:"  # fee_back:<id>  — back to detail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MONTH_LABELS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _current_month_label(timezone: str = "America/Toronto") -> str:
    tz = pytz.timezone(timezone)
    return datetime.now(tz).strftime("%b %Y")


def _next_month_label(timezone: str = "America/Toronto") -> str:
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    if now.month == 12:
        return f"Jan {now.year + 1}"
    return f"{_MONTH_LABELS[now.month]} {now.year}"


def _split_label(split_type: str, paid_by: str) -> str:
    if split_type == "equal":
        return "Equal split"
    return f"All on {paid_by}"


def _fe_list_text(fixed_expenses: list[dict], month_label: str) -> str:
    if not fixed_expenses:
        return "No active fixed expenses."
    lines = [f"📌 <b>Fixed Expenses</b> (active as of {month_label})\n"]
    for i, fe in enumerate(fixed_expenses, 1):
        lines.append(
            f"{i}. <b>{fe['description']}</b> — ${fe['amount']:.2f} "
            f"(paid by {fe['paid_by_name']}, {_split_label(fe['split_type'], fe['paid_by_name'])})"
        )
    lines.append("\nTap an item to manage it.")
    return "\n".join(lines)


def _fe_detail_text(fe: dict, month_label: str, is_skipped_this_month: bool) -> str:
    skip_note = " [SKIPPED this month]" if is_skipped_this_month else ""
    start = fe.get("start_month") or "always"
    end = fe.get("end_month")
    active_range = f"from {start}" + (f" until {end}" if end else " onwards")
    return (
        f"📌 <b>{fe['description']}</b>{skip_note}\n"
        f"Amount: ${fe['amount']:.2f}\n"
        f"Paid by: {fe['paid_by_name']}\n"
        f"Split: {_split_label(fe['split_type'], fe['paid_by_name'])}\n"
        f"Active: {active_range}\n\n"
        f"Current month: <b>{month_label}</b>"
    )


def _fe_detail_keyboard(fe_id: int, is_skipped: bool) -> InlineKeyboardMarkup:
    if is_skipped:
        skip_btn = InlineKeyboardButton(
            "✅ Re-enable this month", callback_data=f"{CB_FE_UNSKIP}{fe_id}"
        )
    else:
        skip_btn = InlineKeyboardButton(
            "⏭ Skip this month", callback_data=f"{CB_FE_SKIP_THIS}{fe_id}"
        )
    return InlineKeyboardMarkup([
        [skip_btn],
        [InlineKeyboardButton("✏️ Edit", callback_data=f"{CB_FE_EDIT}{fe_id}")],
        [InlineKeyboardButton("🚫 Cancel from now on", callback_data=f"{CB_FE_CANCEL_FUTURE}{fe_id}")],
        [InlineKeyboardButton("← Back", callback_data=CB_FE_BACK)],
    ])


def _fe_list_keyboard(fixed_expenses: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for i, fe in enumerate(fixed_expenses, 1):
        buttons.append([InlineKeyboardButton(
            f"{i}. {fe['description']} (${fe['amount']:.2f})",
            callback_data=f"{CB_FE_SELECT_PFX}{fe['id']}",
        )])
    return InlineKeyboardMarkup(buttons)


def _fe_edit_keyboard(fe_id: int, members: list[str]) -> InlineKeyboardMarkup:
    """Show edit options: amount, payer, split."""
    payer_buttons = [
        InlineKeyboardButton(m, callback_data=f"{CB_FE_EDIT_PAIDBY_PFX}{fe_id}:{m}")
        for m in members
    ]
    rows = [
        [InlineKeyboardButton("💰 Change amount", callback_data=f"{CB_FE_EDIT_AMOUNT}{fe_id}")],
        [InlineKeyboardButton("👤 Change payer:", callback_data=f"noop")],
        payer_buttons,
        [
            InlineKeyboardButton("Equal split", callback_data=f"{CB_FE_EDIT_SPLIT_EQUAL}{fe_id}"),
            InlineKeyboardButton("Payer bears all", callback_data=f"{CB_FE_EDIT_SPLIT_MINE}{fe_id}"),
        ],
        [InlineKeyboardButton("← Back", callback_data=f"{CB_FE_EDIT_BACK}{fe_id}")],
    ]
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# /add-fixed  command & conversation
# ---------------------------------------------------------------------------


async def handle_add_fixed_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Start the /add-fixed flow."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else group_id

    group = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text("Please run /start first.")
        return

    database.set_state(user_id, group_id, STATE_ADDFIXED_DESC, {})
    await update.effective_message.reply_text(
        "➕ <b>Add Fixed Expense</b>\n\nWhat's the description? (e.g. Internet, Rent, Netflix)",
        parse_mode="HTML",
    )


async def handle_add_fixed_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle text input for the /add-fixed flow."""
    if update.effective_chat is None or update.effective_message is None:
        return
    if update.message is None or update.message.text is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else group_id
    text = update.message.text.strip()

    state_row = database.get_state(user_id, group_id)
    if state_row is None:
        return

    state = state_row.get("state", "")
    ctx: dict = state_row.get("context") or {}

    # ---- description ----
    if state == STATE_ADDFIXED_DESC:
        ctx["description"] = text
        database.set_state(user_id, group_id, STATE_ADDFIXED_AMOUNT, ctx)
        await update.message.reply_text(f'Amount for "{text}"? (e.g. 56.50)')
        return

    # ---- amount ----
    if state == STATE_ADDFIXED_AMOUNT:
        try:
            amount = float(text.replace("$", "").replace(",", ""))
        except ValueError:
            await update.message.reply_text("Please enter a valid number, e.g. 56.50")
            return
        ctx["amount"] = amount
        members = database.get_members(group_id)
        if not members:
            await update.message.reply_text("No members found. Please run /start first.")
            return
        ctx["members"] = [m["name"] for m in members]
        database.set_state(user_id, group_id, STATE_ADDFIXED_PAIDBY, ctx)
        buttons = [[InlineKeyboardButton(m["name"], callback_data=f"{CB_ADDFIXED_PAIDBY_PFX}{m['name']}")]
                   for m in members]
        await update.message.reply_text(
            f"${amount:.2f} — Who pays?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ---- start month (typed) ----
    if state == STATE_ADDFIXED_STARTMONTH and ctx.get("awaiting_typed_month"):
        parsed = _parse_month_input(text)
        if parsed is None:
            await update.message.reply_text(
                'Could not parse month. Try "Mar 2026" or "march 2026".'
            )
            return
        ctx["start_month"] = parsed
        ctx.pop("awaiting_typed_month", None)
        database.set_state(user_id, group_id, STATE_ADDFIXED_CONFIRM, ctx)
        await _show_addfixed_confirm(update, ctx)
        return

    # ---- edit amount (fixedexp flow) ----
    if state == STATE_FIXEDEXP_EDIT_AMOUNT:
        fe_id: int = ctx.get("edit_fe_id", 0)
        try:
            new_amount = float(text.replace("$", "").replace(",", ""))
        except ValueError:
            await update.message.reply_text("Please enter a valid number, e.g. 56.50")
            return
        if new_amount <= 0:
            await update.message.reply_text("Amount must be greater than 0.")
            return
        database.update_fixed_expense(fe_id, amount=new_amount)
        database.clear_state(user_id, group_id)
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        name = fe["description"] if fe else "Expense"
        await update.message.reply_text(
            f"✅ <b>{name}</b> updated to ${new_amount:.2f}/month.",
            parse_mode="HTML",
        )
        return


def _parse_month_input(text: str) -> Optional[str]:
    """Try to parse a user-typed month string into 'Mon YYYY' format."""
    _map = {
        "january": "Jan", "jan": "Jan",
        "february": "Feb", "feb": "Feb",
        "march": "Mar", "mar": "Mar",
        "april": "Apr", "apr": "Apr",
        "may": "May",
        "june": "Jun", "jun": "Jun",
        "july": "Jul", "jul": "Jul",
        "august": "Aug", "aug": "Aug",
        "september": "Sep", "sep": "Sep",
        "october": "Oct", "oct": "Oct",
        "november": "Nov", "nov": "Nov",
        "december": "Dec", "dec": "Dec",
    }
    parts = text.strip().lower().split()
    if len(parts) == 1:
        abbr = _map.get(parts[0])
        if abbr:
            return f"{abbr} {datetime.now().year}"
    elif len(parts) == 2:
        abbr = _map.get(parts[0])
        try:
            year = int(parts[1])
            if abbr and 2020 <= year <= 2099:
                return f"{abbr} {year}"
        except ValueError:
            pass
    return None


async def _show_addfixed_confirm(update: Update, ctx: dict) -> None:
    desc = ctx.get("description", "?")
    amount = ctx.get("amount", 0.0)
    paid_by = ctx.get("paid_by", "?")
    split = ctx.get("split_type", "equal")
    start = ctx.get("start_month", "this month")
    split_label = "Equal split" if split == "equal" else f"All on {paid_by}"
    text = (
        f"📋 <b>Confirm Fixed Expense</b>\n\n"
        f"Description: {desc}\n"
        f"Amount: ${float(amount):.2f}\n"
        f"Paid by: {paid_by}\n"
        f"Split: {split_label}\n"
        f"Starts: {start}\n\n"
        f"Save it?"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save", callback_data=CB_ADDFIXED_CONFIRM_YES),
        InlineKeyboardButton("❌ Cancel", callback_data=CB_ADDFIXED_CONFIRM_NO),
    ]])
    msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_add_fixed_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle inline button presses for /add-fixed flow."""
    query = update.callback_query
    if query is None or update.effective_chat is None:
        return
    await query.answer()

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else group_id
    data: str = query.data or ""

    state_row = database.get_state(user_id, group_id)
    if state_row is None:
        return
    ctx: dict = state_row.get("context") or {}
    timezone: str = (database.get_group(group_id) or {}).get("timezone") or "America/Toronto"

    # ---- paid by ----
    if data.startswith(CB_ADDFIXED_PAIDBY_PFX):
        paid_by = data[len(CB_ADDFIXED_PAIDBY_PFX):]
        ctx["paid_by"] = paid_by
        database.set_state(user_id, group_id, STATE_ADDFIXED_SPLIT, ctx)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Equal split", callback_data=CB_ADDFIXED_SPLIT_EQUAL),
            InlineKeyboardButton(f"All on {paid_by}", callback_data=CB_ADDFIXED_SPLIT_MINE),
        ]])
        await query.edit_message_text(
            f"Paid by {paid_by}. How to split?",
            reply_markup=keyboard,
        )
        return

    # ---- split type → ask for start month ----
    if data in (CB_ADDFIXED_SPLIT_EQUAL, CB_ADDFIXED_SPLIT_MINE):
        split = "equal" if data == CB_ADDFIXED_SPLIT_EQUAL else ctx.get("paid_by", "equal").lower()
        ctx["split_type"] = split
        database.set_state(user_id, group_id, STATE_ADDFIXED_STARTMONTH, ctx)
        cur = _current_month_label(timezone)
        nxt = _next_month_label(timezone)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"This month ({cur})", callback_data=CB_ADDFIXED_START_THIS),
                InlineKeyboardButton(f"Next month ({nxt})", callback_data=CB_ADDFIXED_START_NEXT),
            ],
            [InlineKeyboardButton("Other month...", callback_data=CB_ADDFIXED_START_TYPE)],
        ])
        await query.edit_message_text("When does this start?", reply_markup=keyboard)
        return

    # ---- start month choice ----
    if data == CB_ADDFIXED_START_THIS:
        ctx["start_month"] = _current_month_label(timezone)
        database.set_state(user_id, group_id, STATE_ADDFIXED_CONFIRM, ctx)
        await query.edit_message_text("Got it!")
        await _show_addfixed_confirm(update, ctx)
        return

    if data == CB_ADDFIXED_START_NEXT:
        ctx["start_month"] = _next_month_label(timezone)
        database.set_state(user_id, group_id, STATE_ADDFIXED_CONFIRM, ctx)
        await query.edit_message_text("Got it!")
        await _show_addfixed_confirm(update, ctx)
        return

    if data == CB_ADDFIXED_START_TYPE:
        ctx["awaiting_typed_month"] = True
        database.set_state(user_id, group_id, STATE_ADDFIXED_STARTMONTH, ctx)
        await query.edit_message_text('Type the start month, e.g. "Apr 2026" or "april".')
        return

    # ---- confirm ----
    if data == CB_ADDFIXED_CONFIRM_YES:
        desc: str = ctx.get("description", "")
        amount: float = float(ctx.get("amount", 0.0))
        paid_by_name: str = ctx.get("paid_by", "")
        split_type: str = ctx.get("split_type", "equal")
        start_month: Optional[str] = ctx.get("start_month")

        member = database.get_member_by_name(group_id, paid_by_name)
        if member is None:
            await query.edit_message_text(f"Member '{paid_by_name}' not found. Cancelled.")
            database.clear_state(user_id, group_id)
            return

        database.add_fixed_expense(
            group_id=group_id,
            description=desc,
            amount=amount,
            paid_by_member_id=member["id"],
            split_type=split_type,
            start_month=start_month,
        )
        database.clear_state(user_id, group_id)
        await query.edit_message_text(
            f"✅ <b>{desc}</b> ${amount:.2f}/month added as a fixed expense "
            f"starting {start_month or 'this month'}!",
            parse_mode="HTML",
        )
        return

    if data == CB_ADDFIXED_CONFIRM_NO:
        database.clear_state(user_id, group_id)
        await query.edit_message_text("Cancelled.")
        return


# ---------------------------------------------------------------------------
# /fixedexp  command & callbacks
# ---------------------------------------------------------------------------


async def handle_fixedexp_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show the fixed expense management list."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id = str(update.effective_chat.id)
    group = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text("Please run /start first.")
        return

    timezone: str = group.get("timezone") or "America/Toronto"
    month_label = _current_month_label(timezone)
    fixed = database.get_fixed_expenses(group_id)

    text = _fe_list_text(fixed, month_label)
    if not fixed:
        await update.effective_message.reply_text(text)
        return

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_fe_list_keyboard(fixed),
    )


async def handle_fixedexp_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle inline buttons for /fixedexp."""
    query = update.callback_query
    if query is None or update.effective_chat is None:
        return
    await query.answer()

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id) if update.effective_user else group_id
    data: str = query.data or ""

    group = database.get_group(group_id)
    timezone: str = (group or {}).get("timezone") or "America/Toronto"
    month_label = _current_month_label(timezone)

    # ---- select fixed expense → show detail ----
    if data.startswith(CB_FE_SELECT_PFX):
        fe_id = int(data[len(CB_FE_SELECT_PFX):])
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        if fe is None:
            await query.edit_message_text("Fixed expense not found (maybe already disabled).")
            return
        exceptions = database.get_fixed_expense_exceptions(fe_id)
        is_skipped = month_label in exceptions
        await query.edit_message_text(
            _fe_detail_text(fe, month_label, is_skipped),
            parse_mode="HTML",
            reply_markup=_fe_detail_keyboard(fe_id, is_skipped),
        )
        return

    # ---- skip this month ----
    if data.startswith(CB_FE_SKIP_THIS):
        fe_id = int(data[len(CB_FE_SKIP_THIS):])
        database.add_fixed_expense_exception(fe_id, month_label)
        database.unseed_fixed_expense_for_month(group_id, fe_id, month_label)
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        name = fe["description"] if fe else "Expense"
        await query.edit_message_text(
            f"⏭ <b>{name}</b> skipped for {month_label}. It will resume next month.\n\n"
            f"Use /fixedexp to manage it again.",
            parse_mode="HTML",
        )
        return

    # ---- restore skipped month ----
    if data.startswith(CB_FE_UNSKIP):
        fe_id = int(data[len(CB_FE_UNSKIP):])
        database.remove_fixed_expense_exception(fe_id, month_label)
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        name = fe["description"] if fe else "Expense"
        await query.edit_message_text(
            f"✅ <b>{name}</b> re-enabled for {month_label}.\n\n"
            f"It will appear next time /summary or /history seeds this month.",
            parse_mode="HTML",
        )
        return

    # ---- cancel from now on — show confirmation ----
    if data.startswith(CB_FE_CANCEL_FUTURE) and not data.startswith(CB_FE_CANCEL_FUTURE_YES) and not data.startswith(CB_FE_CANCEL_FUTURE_NO):
        fe_id = int(data[len(CB_FE_CANCEL_FUTURE):])
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        name = fe["description"] if fe else "Expense"
        await query.edit_message_text(
            f"⚠️ Stop <b>{name}</b> from {month_label} onwards?\n\n"
            f"Past months are unaffected. This cannot be undone.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚫 Yes, cancel it", callback_data=f"{CB_FE_CANCEL_FUTURE_YES}{fe_id}"),
                InlineKeyboardButton("← Keep it", callback_data=f"{CB_FE_CANCEL_FUTURE_NO}{fe_id}"),
            ]]),
        )
        return

    # ---- cancel confirmed ----
    if data.startswith(CB_FE_CANCEL_FUTURE_YES):
        fe_id = int(data[len(CB_FE_CANCEL_FUTURE_YES):])
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        name = fe["description"] if fe else "Expense"
        database.deactivate_fixed_expense(fe_id, end_month=month_label)
        await query.edit_message_text(
            f"🚫 <b>{name}</b> cancelled from {month_label} onwards. "
            f"Past months are unaffected.",
            parse_mode="HTML",
        )
        return

    # ---- cancel aborted → back to detail ----
    if data.startswith(CB_FE_CANCEL_FUTURE_NO):
        fe_id = int(data[len(CB_FE_CANCEL_FUTURE_NO):])
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        if fe is None:
            await query.edit_message_text("Fixed expense not found.")
            return
        exceptions = database.get_fixed_expense_exceptions(fe_id)
        is_skipped = month_label in exceptions
        await query.edit_message_text(
            _fe_detail_text(fe, month_label, is_skipped),
            parse_mode="HTML",
            reply_markup=_fe_detail_keyboard(fe_id, is_skipped),
        )
        return

    # ---- edit menu ----
    if data.startswith(CB_FE_EDIT):
        fe_id = int(data[len(CB_FE_EDIT):])
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        if fe is None:
            await query.edit_message_text("Fixed expense not found.")
            return
        members = database.get_members(group_id)
        member_names = [m["name"] for m in members]
        await query.edit_message_text(
            f"✏️ <b>Edit: {fe['description']}</b>\n"
            f"Current: ${fe['amount']:.2f} · {fe['paid_by_name']} · "
            f"{_split_label(fe['split_type'], fe['paid_by_name'])}\n\n"
            f"What do you want to change?",
            parse_mode="HTML",
            reply_markup=_fe_edit_keyboard(fe_id, member_names),
        )
        return

    # ---- edit: prompt for new amount ----
    if data.startswith(CB_FE_EDIT_AMOUNT):
        fe_id = int(data[len(CB_FE_EDIT_AMOUNT):])
        database.set_state(user_id, group_id, STATE_FIXEDEXP_EDIT_AMOUNT, {"edit_fe_id": fe_id})
        await query.edit_message_text(
            "💰 Type the new amount (e.g. 65.00):"
        )
        return

    # ---- edit: change payer ----
    if data.startswith(CB_FE_EDIT_PAIDBY_PFX):
        rest = data[len(CB_FE_EDIT_PAIDBY_PFX):]
        parts = rest.split(":", 1)
        if len(parts) != 2:
            return
        fe_id = int(parts[0])
        new_payer = parts[1]
        member = database.get_member_by_name(group_id, new_payer)
        if member is None:
            await query.answer("Member not found.", show_alert=True)
            return
        database.update_fixed_expense(fe_id, paid_by_member_id=member["id"])
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        name = fe["description"] if fe else "Expense"
        await query.edit_message_text(
            f"✅ <b>{name}</b> payer changed to {new_payer}.",
            parse_mode="HTML",
        )
        return

    # ---- edit: change split ----
    if data.startswith(CB_FE_EDIT_SPLIT_EQUAL):
        fe_id = int(data[len(CB_FE_EDIT_SPLIT_EQUAL):])
        database.update_fixed_expense(fe_id, split_type="equal")
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        name = fe["description"] if fe else "Expense"
        await query.edit_message_text(
            f"✅ <b>{name}</b> split changed to equal.",
            parse_mode="HTML",
        )
        return

    if data.startswith(CB_FE_EDIT_SPLIT_MINE):
        fe_id = int(data[len(CB_FE_EDIT_SPLIT_MINE):])
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        if fe is None:
            return
        payer_name = fe["paid_by_name"].lower()
        database.update_fixed_expense(fe_id, split_type=payer_name)
        name = fe["description"]
        await query.edit_message_text(
            f"✅ <b>{name}</b> split changed — {fe['paid_by_name']} bears all.",
            parse_mode="HTML",
        )
        return

    # ---- edit: back to detail ----
    if data.startswith(CB_FE_EDIT_BACK):
        fe_id = int(data[len(CB_FE_EDIT_BACK):])
        fixed = database.get_fixed_expenses(group_id)
        fe = next((f for f in fixed if f["id"] == fe_id), None)
        if fe is None:
            await query.edit_message_text("Fixed expense not found.")
            return
        exceptions = database.get_fixed_expense_exceptions(fe_id)
        is_skipped = month_label in exceptions
        await query.edit_message_text(
            _fe_detail_text(fe, month_label, is_skipped),
            parse_mode="HTML",
            reply_markup=_fe_detail_keyboard(fe_id, is_skipped),
        )
        return

    # ---- back to list ----
    if data == CB_FE_BACK:
        fixed = database.get_fixed_expenses(group_id)
        text = _fe_list_text(fixed, month_label)
        if not fixed:
            await query.edit_message_text(text)
        else:
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=_fe_list_keyboard(fixed),
            )
        return
