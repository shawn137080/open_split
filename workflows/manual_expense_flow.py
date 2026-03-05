"""Manual expense entry flow (/expense command)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
from tools.balance_calculator import calculate_balances, format_balance_summary
from tools.expense_store import (
    append_expense,
    get_month_expenses,
    get_next_expense_id,
    get_or_create_month,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

STATE_MANUAL_AWAITING_CONFIRM = "MANUAL_AWAITING_CONFIRM"

MANUAL_STATES = {STATE_MANUAL_AWAITING_CONFIRM}

# ---------------------------------------------------------------------------
# Callback data constants
# ---------------------------------------------------------------------------

CB_MANUAL_SAVE = "manual:save"
CB_MANUAL_CANCEL = "manual:cancel"

# ---------------------------------------------------------------------------
# Usage help text
# ---------------------------------------------------------------------------

USAGE_TEXT = (
    "Usage: /expense \"description\" amount paid_by [split]\n"
    "  split options: equal (default), mine, <member_name>\n"
    "Examples:\n"
    "  /expense \"Internet\" 58 karlos equal\n"
    "  /expense groceries 45.20 partner mine\n"
    "  /expense gas 30 karlos partner"
)

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _today_date_str(timezone: str = "America/Toronto") -> str:
    """Return today's date as YYYY-MM-DD in the given timezone."""
    tz = pytz.timezone(timezone)
    return datetime.now(tz).strftime("%Y-%m-%d")


def _today_month_label(timezone: str = "America/Toronto") -> str:
    """Return current month label like 'Mar 2026'."""
    tz = pytz.timezone(timezone)
    return datetime.now(tz).strftime("%b %Y")


def _parse_date_input(text: str, timezone: str = "America/Toronto") -> Optional[str]:
    """
    Parse user date input into a YYYY-MM-DD string.

    Accepts:
      - MM/DD        → infers current year; if future, uses previous year
      - MM/DD/YYYY   → explicit year
    Returns None if unparseable.
    """
    text = text.strip()
    if not text:
        return None

    tz = pytz.timezone(timezone)
    now = datetime.now(tz)

    # Try MM/DD/YYYY first.
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if m:
        try:
            dt = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    # Try MM/DD — assume current year; if that date is in the future, use last year.
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", text)
    if m:
        month_num, day_num = int(m.group(1)), int(m.group(2))
        year = now.year
        try:
            dt = datetime(year, month_num, day_num)
            if dt.date() > now.date():
                dt = datetime(year - 1, month_num, day_num)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    return None


def _confirm_keyboard() -> InlineKeyboardMarkup:
    """Return the Save / Cancel inline keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Save", callback_data=CB_MANUAL_SAVE),
                InlineKeyboardButton("❌ Cancel", callback_data=CB_MANUAL_CANCEL),
            ]
        ]
    )


def _parse_expense_args(
    text: str,
) -> tuple[Optional[str], Optional[float], Optional[str], str]:
    """
    Parse the argument string from /expense (everything after the command).

    Returns (description, amount, paid_by, split) where split defaults to
    "equal".  Any of description, amount, paid_by may be None on parse
    failure.
    """
    text = text.strip()

    # Try to extract a quoted description first.
    quoted_match = re.match(r'^"([^"]+)"\s*(.*)', text)
    if quoted_match:
        description: Optional[str] = quoted_match.group(1).strip()
        rest: str = quoted_match.group(2).strip()
    else:
        # Unquoted: first whitespace-delimited token is the description.
        parts_initial = text.split(None, 1)
        if not parts_initial:
            return (None, None, None, "equal")
        description = parts_initial[0]
        rest = parts_initial[1].strip() if len(parts_initial) > 1 else ""

    # Remaining tokens: amount paid_by [split]
    tokens: list[str] = rest.split()

    if len(tokens) < 2:
        # Not enough tokens for amount + paid_by
        return (description, None, None, "equal")

    # Parse amount
    try:
        amount: Optional[float] = float(tokens[0])
    except ValueError:
        amount = None

    paid_by: Optional[str] = tokens[1] if len(tokens) >= 2 else None

    split: str = tokens[2] if len(tokens) >= 3 else "equal"

    return (description, amount, paid_by, split)


def _build_member_shares(
    member_names: list[str],
    amount: float,
    paid_by: str,
    split: str,
) -> dict[str, float]:
    """
    Build the member_shares dict based on the split argument.

    split options:
      - "equal": divide equally among all members (remainder to first member)
      - "mine": entire amount owed by the payer only; others owe $0
      - <name>: entire amount owed by that specific member; others owe $0
    """
    shares: dict[str, float] = {m: 0.0 for m in member_names}

    split_lower: str = split.lower()

    if split_lower == "equal":
        n: int = len(member_names)
        if n == 0:
            return shares
        base: float = round(amount / n, 2)
        total_assigned: float = round(base * n, 2)
        remainder: float = round(amount - total_assigned, 2)
        for i, member in enumerate(member_names):
            if i == 0:
                shares[member] = round(base + remainder, 2)
            else:
                shares[member] = base
        return shares

    if split_lower == "mine":
        # Payer owes the entire amount; others owe $0.
        # Match paid_by case-insensitively against member_names.
        payer_canon: Optional[str] = next(
            (m for m in member_names if m.lower() == paid_by.lower()), None
        )
        if payer_canon:
            shares[payer_canon] = amount
        else:
            logger.warning(
                "_build_member_shares: paid_by %r not found in member_names %r; "
                "all shares are zero",
                paid_by,
                member_names,
            )
        return shares

    # Otherwise treat split as a member name.
    target: Optional[str] = next(
        (m for m in member_names if m.lower() == split_lower), None
    )
    if target:
        shares[target] = amount
    else:
        logger.warning(
            "_build_member_shares: split name %r not found in member_names %r; "
            "all shares are zero",
            split,
            member_names,
        )
    return shares


def _format_preview(
    description: str,
    amount: float,
    paid_by: str,
    split: str,
    member_shares: dict[str, float],
    date_str: str,
    currency: str = "CAD",
) -> str:
    """Format the expense preview message."""
    sym: str = "$" if currency.upper() in ("CAD", "USD", "AUD") else currency
    lines: list[str] = [
        "📝 Expense Preview",
        "",
        f"Description: {description}",
        f"Amount: {sym}{amount:.2f}",
        f"Paid by: {paid_by}",
        f"Date: {date_str}",
        f"Split: {split}",
        "",
        "Split breakdown:",
    ]
    for member, share in member_shares.items():
        lines.append(f"  {member}: {sym}{share:.2f}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Save helper
# ---------------------------------------------------------------------------


async def _do_save(
    update: Update,
    group_id: str,
    user_id: str,
    ctx: dict,
) -> None:
    """
    Save the manual expense to local SQLite and show the balance summary.

    Steps:
    1. get_or_create_month (seeds fixed expenses)
    2. get_next_expense_id
    3. append_expense
    4. get_month_expenses → calculate_balances
    5. Show success + balance summary
    6. clear_state
    """
    group = database.get_group(group_id)
    if group is None:
        database.clear_state(user_id, group_id)
        return

    timezone: str = group.get("timezone", "America/Toronto")
    currency: str = group.get("currency", "CAD")

    members_data: list[dict] = database.get_members(group_id)
    member_names: list[str] = [m["name"] for m in members_data]

    stored_date: str = ctx.get("date_str", "") or ""
    if stored_date:
        date_str = stored_date
        month_label = datetime.strptime(stored_date, "%Y-%m-%d").strftime("%b %Y")
    else:
        date_str = _today_date_str(timezone)
        month_label = _today_month_label(timezone)

    description: str = ctx.get("description", "")
    amount: float = float(ctx.get("amount", 0.0))
    paid_by: str = ctx.get("paid_by", "")
    member_shares: dict[str, float] = ctx.get("member_shares", {})

    query = update.callback_query

    try:
        # Step 1: ensure month is initialised (seeds fixed expenses)
        get_or_create_month(
            group_id=group_id,
            month_label=month_label,
            members=member_names,
            fixed_expenses=[],
        )

        # Step 2 + 3: get next ID and append expense
        expense_id: str = get_next_expense_id(group_id, month_label)
        expense_row: dict = {
            "expense_id": expense_id,
            "date": date_str,
            "description": description,
            "category": "Other",
            "subtotal": amount,
            "hst_amount": 0.0,
            "hst_pct": 0.0,
            "tip_amount": 0.0,
            "tip_pct": 0.0,
            "total": amount,
            "paid_by": paid_by,
            "member_shares": member_shares,
            "notes": "",
        }
        append_expense(group_id=group_id, month_label=month_label, expense=expense_row)

        # Step 4: fetch all expenses and compute balances
        all_expenses: list[dict] = get_month_expenses(group_id, month_label)
        balances: list[dict] = calculate_balances(all_expenses, member_names)

        # Step 5: show success + balance summary
        balance_text: str = format_balance_summary(balances, month_label, currency=currency)
        success_msg: str = f"Saved! #{expense_id}\n\n{balance_text}"

        if query:
            await query.edit_message_text(success_msg)
        elif update.effective_message:
            await update.effective_message.reply_text(success_msg)

    except Exception as exc:
        logger.exception("Failed to save manual expense: %s", exc)
        err_msg: str = (
            "Something went wrong saving the expense.\n"
            f"Error: {exc}\n\nPlease try again."
        )
        if query:
            await query.edit_message_text(err_msg)
        elif update.effective_message:
            await update.effective_message.reply_text(err_msg)

    # Step 6: clear state regardless of success/failure
    database.clear_state(user_id, group_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_manual_state(user_id: str, group_id: str) -> bool:
    """Return True if user has an active manual expense state."""
    _sr2 = database.get_state(user_id, group_id); state = _sr2.get("state") if _sr2 else None
    return state in MANUAL_STATES


async def handle_expense_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /expense command. Parses args, shows preview, asks to confirm."""
    if update.effective_chat is None or update.effective_user is None:
        return
    if update.effective_message is None:
        return

    group_id: str = str(update.effective_chat.id)
    user_id: str = str(update.effective_user.id)

    # Ensure group is configured.
    group: Optional[dict] = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    # Prevent starting while another flow is active.
    _sr3 = database.get_state(user_id, group_id); existing_state = _sr3.get("state") if _sr3 else None
    if existing_state is not None:
        await update.effective_message.reply_text(
            "You have an active flow in progress. Please finish or cancel it first."
        )
        return

    # Extract the argument text (everything after /expense).
    # We re-parse from the raw message text so quoted strings are preserved —
    # context.args would have already split on whitespace losing the quotes.
    raw_args: str = ""
    msg_text: str = update.effective_message.text or ""
    # Strip the command token (/expense or /expense@botname).
    command_match = re.match(r"^/expense(?:@\S+)?\s*(.*)", msg_text, re.IGNORECASE | re.DOTALL)
    if command_match:
        raw_args = command_match.group(1).strip()

    if not raw_args:
        await update.effective_message.reply_text(USAGE_TEXT)
        return

    description, amount, paid_by_raw, split = _parse_expense_args(raw_args)

    # Validate required fields — split into separate guards so pyright narrows each.
    if description is None:
        await update.effective_message.reply_text(USAGE_TEXT)
        return
    if amount is None:
        await update.effective_message.reply_text(USAGE_TEXT)
        return
    if paid_by_raw is None:
        await update.effective_message.reply_text(USAGE_TEXT)
        return

    if amount <= 0:
        await update.effective_message.reply_text(
            f"Amount must be greater than 0. Got: {amount}\n\n{USAGE_TEXT}"
        )
        return

    # Validate paid_by against known members (case-insensitive).
    members_data: list[dict] = database.get_members(group_id)
    member_names: list[str] = [m["name"] for m in members_data]

    paid_by_member: Optional[dict] = database.get_member_by_name(group_id, paid_by_raw)
    if paid_by_member is None:
        member_list: str = ", ".join(member_names) if member_names else "(none)"
        await update.effective_message.reply_text(
            f"Unknown member: {paid_by_raw!r}\n"
            f"Known members: {member_list}\n\n{USAGE_TEXT}"
        )
        return

    # Canonical paid_by name (preserves original casing from DB).
    paid_by: str = paid_by_member["name"]

    # Validate split arg: must be "equal", "mine", or a known member name.
    split_lower: str = split.lower()
    valid_split: bool = split_lower in ("equal", "mine") or any(
        m.lower() == split_lower for m in member_names
    )
    if not valid_split:
        member_list = ", ".join(member_names) if member_names else "(none)"
        await update.effective_message.reply_text(
            f"Unknown split option: {split!r}\n"
            f"Valid options: equal, mine, or a member name ({member_list})\n\n{USAGE_TEXT}"
        )
        return

    # Build member_shares.
    member_shares: dict[str, float] = _build_member_shares(
        member_names, amount, paid_by, split
    )

    # Get timezone for date display.
    timezone: str = group.get("timezone", "America/Toronto")
    currency: str = group.get("currency", "CAD")
    date_str: str = _today_date_str(timezone)

    # Persist state.
    ctx: dict = {
        "description": description,
        "amount": amount,
        "paid_by": paid_by,
        "split": split,
        "member_shares": member_shares,
    }
    database.set_state(user_id, group_id, STATE_MANUAL_AWAITING_CONFIRM, ctx)

    # Show preview with inline buttons.
    preview: str = _format_preview(
        description=description,
        amount=amount,
        paid_by=paid_by,
        split=split,
        member_shares=member_shares,
        date_str=date_str,
        currency=currency,
    )
    await update.effective_message.reply_text(
        preview,
        reply_markup=_confirm_keyboard(),
    )


async def handle_manual_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline keyboard callbacks for manual expense confirmation."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()

    if update.effective_chat is None or update.effective_user is None:
        return

    group_id: str = str(update.effective_chat.id)
    user_id: str = str(update.effective_user.id)
    data: str = query.data or ""

    if not data.startswith("manual:"):
        return

    _sr = database.get_state(user_id, group_id); state = _sr.get("state") if _sr else None; ctx = _sr.get("context") if _sr else None

    if state != STATE_MANUAL_AWAITING_CONFIRM:
        await query.edit_message_text(
            "No active expense confirmation found. Use /expense to start."
        )
        return

    if ctx is None:
        await query.edit_message_text(
            "No active expense confirmation found. Use /expense to start."
        )
        return

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------
    if data == CB_MANUAL_CANCEL:
        database.clear_state(user_id, group_id)
        await query.edit_message_text("Expense cancelled.")
        return

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    if data == CB_MANUAL_SAVE:
        group: Optional[dict] = database.get_group(group_id)
        if group is None:
            database.clear_state(user_id, group_id)
            await query.edit_message_text(
                "Household is not configured. Please run /start."
            )
            return

        try:
            await query.edit_message_text("Saving expense...")
        except Exception as exc:
            logger.warning("Could not send 'Saving expense...' message: %s", exc)
        await _do_save(update, group_id, user_id, ctx)
        return

    # Unknown callback — ignore silently.
    logger.warning("handle_manual_callback: unhandled callback data %r", data)


# ---------------------------------------------------------------------------
# /add — guided interactive expense entry (step-by-step with buttons)
# ---------------------------------------------------------------------------

STATE_ADD_AWAITING_DESC = "ADD_AWAITING_DESC"
STATE_ADD_AWAITING_AMOUNT = "ADD_AWAITING_AMOUNT"
STATE_ADD_AWAITING_DATE = "ADD_AWAITING_DATE"
STATE_ADD_AWAITING_PAYER = "ADD_AWAITING_PAYER"
STATE_ADD_AWAITING_SPLIT = "ADD_AWAITING_SPLIT"
STATE_ADD_AWAITING_CATEGORY = "ADD_AWAITING_CATEGORY"
STATE_ADD_AWAITING_CONFIRM = "ADD_AWAITING_CONFIRM"

_ADD_STATES = frozenset({
    STATE_ADD_AWAITING_DESC,
    STATE_ADD_AWAITING_AMOUNT,
    STATE_ADD_AWAITING_DATE,
    STATE_ADD_AWAITING_PAYER,
    STATE_ADD_AWAITING_SPLIT,
    STATE_ADD_AWAITING_CATEGORY,
    STATE_ADD_AWAITING_CONFIRM,
})

_CATEGORIES = [
    "Grocery", "Dining", "Transport", "Utilities",
    "Health", "Entertainment", "Shopping", "Other",
]

CB_ADD_PAYER_PREFIX = "add:payer:"        # add:payer:{member_idx}
CB_ADD_SPLIT_EQUAL = "add:split:equal"
CB_ADD_SPLIT_MINE = "add:split:mine"
CB_ADD_SPLIT_MEMBER_PREFIX = "add:split:m:"  # add:split:m:{member_idx}
CB_ADD_DATE_TODAY = "add:date:today"
CB_ADD_CATEGORY_PREFIX = "add:cat:"        # add:cat:Grocery
CB_ADD_SAVE = "add:save"
CB_ADD_CANCEL = "add:cancel"
# Edit-mode field buttons (shown on the confirm screen when editing)
CB_ADD_EDIT_DESC = "add:edit:desc"
CB_ADD_EDIT_AMOUNT = "add:edit:amount"
CB_ADD_EDIT_DATE = "add:edit:date"
CB_ADD_EDIT_PAYER = "add:edit:payer"
CB_ADD_EDIT_SPLIT = "add:edit:split"


def is_add_state(user_id: str, group_id: str) -> bool:
    """Return True if user is in the /add guided expense flow."""
    state_row = database.get_state(user_id, group_id)
    if state_row is None:
        return False
    return state_row.get("state") in _ADD_STATES


def _payer_keyboard(member_names: list[str]) -> InlineKeyboardMarkup:
    """Keyboard for selecting who paid — 2 members per row."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for j, name in enumerate(member_names):
        row.append(InlineKeyboardButton(name, callback_data=f"add:payer:{j}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_ADD_CANCEL)])
    return InlineKeyboardMarkup(rows)


def _split_keyboard(member_names: list[str]) -> InlineKeyboardMarkup:
    """Keyboard for selecting how to split the expense."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("Equal", callback_data=CB_ADD_SPLIT_EQUAL),
            InlineKeyboardButton("Mine only", callback_data=CB_ADD_SPLIT_MINE),
        ]
    ]
    row: list[InlineKeyboardButton] = []
    for j, name in enumerate(member_names):
        row.append(
            InlineKeyboardButton(f"All → {name}", callback_data=f"add:split:m:{j}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_ADD_CANCEL)])
    return InlineKeyboardMarkup(rows)


def _category_keyboard() -> InlineKeyboardMarkup:
    """2-column category picker."""
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(_CATEGORIES), 2):
        pair = _CATEGORIES[i:i + 2]
        rows.append([InlineKeyboardButton(c, callback_data=f"{CB_ADD_CATEGORY_PREFIX}{c}") for c in pair])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_ADD_CANCEL)])
    return InlineKeyboardMarkup(rows)


def _add_confirm_keyboard() -> InlineKeyboardMarkup:
    """Save / Cancel keyboard for the /add confirm step."""
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Save", callback_data=CB_ADD_SAVE),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_ADD_CANCEL),
        ]]
    )


def _date_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for the date step — offers a Today shortcut."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Today", callback_data=CB_ADD_DATE_TODAY)],
        [InlineKeyboardButton("❌ Cancel", callback_data=CB_ADD_CANCEL)],
    ])


def _edit_confirm_keyboard() -> InlineKeyboardMarkup:
    """Save / Cancel + per-field edit buttons for the /edit confirm screen."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Save", callback_data=CB_ADD_SAVE),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_ADD_CANCEL),
        ],
        [
            InlineKeyboardButton("📝 Description", callback_data=CB_ADD_EDIT_DESC),
            InlineKeyboardButton("💰 Amount", callback_data=CB_ADD_EDIT_AMOUNT),
        ],
        [
            InlineKeyboardButton("📅 Date", callback_data=CB_ADD_EDIT_DATE),
            InlineKeyboardButton("👤 Payer", callback_data=CB_ADD_EDIT_PAYER),
        ],
        [InlineKeyboardButton("🔀 Split", callback_data=CB_ADD_EDIT_SPLIT)],
    ])


async def _show_add_confirm(
    update: Update,
    group_id: str,
    user_id: str,
    ctx: dict,
) -> None:
    """Set state to CONFIRM and display the expense preview.

    Uses _edit_confirm_keyboard when ctx['edit_mode'] is True so edit-field
    buttons are shown; otherwise uses the plain Save/Cancel keyboard.
    """
    group = database.get_group(group_id)
    timezone = (group.get("timezone") or "America/Toronto") if group else "America/Toronto"
    currency = (group.get("currency") or "CAD") if group else "CAD"

    date_str: str = ctx.get("date_str") or _today_date_str(timezone)
    preview = _format_preview(
        description=ctx.get("description", ""),
        amount=float(ctx.get("amount", 0.0)),
        paid_by=ctx.get("paid_by", ""),
        split=ctx.get("split", "Equal"),
        member_shares=ctx.get("member_shares", {}),
        date_str=date_str,
        currency=currency,
    )
    keyboard = _edit_confirm_keyboard() if ctx.get("edit_mode") else _add_confirm_keyboard()
    database.set_state(user_id, group_id, STATE_ADD_AWAITING_CONFIRM, ctx)

    query = update.callback_query
    if query:
        await query.edit_message_text(preview, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(preview, reply_markup=keyboard)


async def handle_add_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /add — start a guided interactive expense entry."""
    if update.effective_chat is None or update.effective_user is None:
        return
    if update.effective_message is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    group = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    _sr3 = database.get_state(user_id, group_id); existing_state = _sr3.get("state") if _sr3 else None
    if existing_state is not None:
        await update.effective_message.reply_text(
            "You have an active flow in progress. Use /cancel to clear it first."
        )
        return

    members_data = database.get_members(group_id)
    if not members_data:
        await update.effective_message.reply_text(
            "No members configured. Please complete household setup."
        )
        return

    database.set_state(user_id, group_id, STATE_ADD_AWAITING_DESC, {})
    await update.effective_message.reply_text("Enter merchant name / description:")


async def handle_add_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle text input during the /add guided flow."""
    if update.effective_chat is None or update.effective_user is None:
        return
    if update.effective_message is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    text = (update.effective_message.text or "").strip()

    _sr = database.get_state(user_id, group_id); state = _sr.get("state") if _sr else None; ctx = _sr.get("context") if _sr else None
    if ctx is None:
        ctx = {}

    members_data = database.get_members(group_id)
    member_names = [m["name"] for m in members_data]

    if state == STATE_ADD_AWAITING_DESC:
        if not text:
            await update.effective_message.reply_text("Please enter a description:")
            return
        ctx["description"] = text
        if ctx.get("edit_mode"):
            await _show_add_confirm(update, group_id, user_id, ctx)
            return
        database.set_state(user_id, group_id, STATE_ADD_AWAITING_AMOUNT, ctx)
        await update.effective_message.reply_text("Enter the total amount:")

    elif state == STATE_ADD_AWAITING_AMOUNT:
        try:
            amount = float(text)
        except ValueError:
            await update.effective_message.reply_text(
                "Please enter a valid number (e.g. 45.20):"
            )
            return
        if amount <= 0:
            await update.effective_message.reply_text("Amount must be greater than 0:")
            return
        if ctx.get("edit_mode"):
            # Scale shares proportionally to the new amount.
            old_shares: dict = ctx.get("member_shares") or {}
            old_total = sum(float(v) for v in old_shares.values())
            if old_total > 0.0 and old_shares:
                scale = amount / old_total
                new_shares = {m: round(float(v) * scale, 2) for m, v in old_shares.items()}
                diff = round(amount - sum(new_shares.values()), 2)
                if diff and new_shares:
                    first_key = next(iter(new_shares))
                    new_shares[first_key] = round(new_shares[first_key] + diff, 2)
            else:
                n = len(member_names) or 1
                new_shares = {m: round(amount / n, 2) for m in member_names}
            ctx["amount"] = amount
            ctx["member_shares"] = new_shares
            await _show_add_confirm(update, group_id, user_id, ctx)
            return
        ctx["amount"] = amount
        database.set_state(user_id, group_id, STATE_ADD_AWAITING_DATE, ctx)
        await update.effective_message.reply_text(
            "Enter the date (e.g. 02/19 or 02/19/2025), or tap Today:",
            reply_markup=_date_keyboard(),
        )

    elif state == STATE_ADD_AWAITING_DATE:
        group = database.get_group(group_id)
        timezone = (group.get("timezone") or "America/Toronto") if group else "America/Toronto"
        parsed = _parse_date_input(text, timezone)
        if parsed is None:
            await update.effective_message.reply_text(
                "Could not parse date. Use MM/DD (e.g. 02/19) or MM/DD/YYYY.\nOr tap Today:",
                reply_markup=_date_keyboard(),
            )
            return
        ctx["date_str"] = parsed
        if ctx.get("edit_mode"):
            await _show_add_confirm(update, group_id, user_id, ctx)
            return
        database.set_state(user_id, group_id, STATE_ADD_AWAITING_PAYER, ctx)
        await update.effective_message.reply_text(
            f"Date: {parsed}\n\nWho paid?",
            reply_markup=_payer_keyboard(member_names),
        )

    elif state in (STATE_ADD_AWAITING_PAYER, STATE_ADD_AWAITING_SPLIT,
                   STATE_ADD_AWAITING_CATEGORY, STATE_ADD_AWAITING_CONFIRM):
        await update.effective_message.reply_text("Please use the buttons above.")


async def handle_add_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline keyboard callbacks for the /add guided flow."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if update.effective_chat is None or update.effective_user is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    data = query.data or ""

    _sr = database.get_state(user_id, group_id); state = _sr.get("state") if _sr else None; ctx = _sr.get("context") if _sr else None
    if ctx is None:
        ctx = {}

    group = database.get_group(group_id)
    timezone = (group.get("timezone") or "America/Toronto") if group else "America/Toronto"
    currency = (group.get("currency") or "CAD") if group else "CAD"

    members_data = database.get_members(group_id)
    member_names = [m["name"] for m in members_data]

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------
    if data == CB_ADD_CANCEL:
        database.clear_state(user_id, group_id)
        await query.edit_message_text("Cancelled.")
        return

    # ------------------------------------------------------------------
    # Today button pressed for date step
    # ------------------------------------------------------------------
    if data == CB_ADD_DATE_TODAY:
        date_str = _today_date_str(timezone)
        ctx["date_str"] = date_str
        database.set_state(user_id, group_id, STATE_ADD_AWAITING_PAYER, ctx)
        await query.edit_message_text(
            f"Date: {date_str}\n\nWho paid?",
            reply_markup=_payer_keyboard(member_names),
        )
        return

    # ------------------------------------------------------------------
    # Payer selected
    # ------------------------------------------------------------------
    if data.startswith(CB_ADD_PAYER_PREFIX):
        try:
            j = int(data[len(CB_ADD_PAYER_PREFIX):])
            paid_by = member_names[j]
        except (ValueError, IndexError):
            await query.answer("Invalid selection.")
            return
        ctx["paid_by"] = paid_by
        database.set_state(user_id, group_id, STATE_ADD_AWAITING_SPLIT, ctx)
        await query.edit_message_text(
            f"Paid by: {paid_by}\n\nHow to split?",
            reply_markup=_split_keyboard(member_names),
        )
        return

    # ------------------------------------------------------------------
    # Split selected
    # ------------------------------------------------------------------
    if (
        data in (CB_ADD_SPLIT_EQUAL, CB_ADD_SPLIT_MINE)
        or data.startswith(CB_ADD_SPLIT_MEMBER_PREFIX)
    ):
        amount = float(ctx.get("amount", 0.0))
        paid_by = ctx.get("paid_by", "")

        if data == CB_ADD_SPLIT_EQUAL:
            split_label = "Equal"
            shares = _build_member_shares(member_names, amount, paid_by, "equal")
        elif data == CB_ADD_SPLIT_MINE:
            split_label = "Mine only"
            shares = _build_member_shares(member_names, amount, paid_by, "mine")
        else:
            try:
                j = int(data[len(CB_ADD_SPLIT_MEMBER_PREFIX):])
                target = member_names[j]
            except (ValueError, IndexError):
                await query.answer("Invalid selection.")
                return
            split_label = f"All → {target}"
            shares = _build_member_shares(member_names, amount, paid_by, target)

        ctx["split"] = split_label
        ctx["member_shares"] = shares
        database.set_state(user_id, group_id, STATE_ADD_AWAITING_CATEGORY, ctx)
        await query.edit_message_text("Category?", reply_markup=_category_keyboard())
        return

    # ------------------------------------------------------------------
    # Category selected
    # ------------------------------------------------------------------
    if data.startswith(CB_ADD_CATEGORY_PREFIX):
        category = data[len(CB_ADD_CATEGORY_PREFIX):]
        ctx["category"] = category
        date_str = ctx.get("date_str") or _today_date_str(timezone)
        amount = float(ctx.get("amount", 0.0))
        paid_by = ctx.get("paid_by", "")
        shares = ctx.get("member_shares", {})
        split_label = ctx.get("split", "Equal")
        preview = _format_preview(
            description=ctx.get("description", ""),
            amount=amount,
            paid_by=paid_by,
            split=split_label,
            member_shares=shares,
            date_str=date_str,
            currency=currency,
        )
        database.set_state(user_id, group_id, STATE_ADD_AWAITING_CONFIRM, ctx)
        await query.edit_message_text(
            f"🏷️ Category: <b>{category}</b>\n\n{preview}",
            parse_mode="HTML",
            reply_markup=_add_confirm_keyboard(),
        )
        return

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    if data == CB_ADD_SAVE:
        if group is None:
            database.clear_state(user_id, group_id)
            await query.edit_message_text(
                "Household is not configured. Please run /start."
            )
            return
        try:
            await query.edit_message_text("Saving expense...")
        except Exception:
            pass
        await _do_save(update, group_id, user_id, ctx)
        return

    logger.warning("handle_add_callback: unhandled data %r", data)
