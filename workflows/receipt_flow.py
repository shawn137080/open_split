"""Receipt photo processing flow."""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
from tools.balance_calculator import (
    calculate_balances,
    format_balance_summary,
    parse_member_shares,
)
from tools.receipt_extractor import extract_receipt, format_extraction_for_display
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

STATE_RECEIPT_PROCESSING = "RECEIPT_PROCESSING"
STATE_RECEIPT_AWAITING_CONFIRM = "RECEIPT_AWAITING_CONFIRM"
STATE_RECEIPT_AWAITING_PAYER = "RECEIPT_AWAITING_PAYER"
STATE_RECEIPT_AWAITING_SPLIT = "RECEIPT_AWAITING_SPLIT"
STATE_RECEIPT_AWAITING_ASSIGNMENT = "RECEIPT_AWAITING_ASSIGNMENT"
STATE_RECEIPT_AWAITING_SAVE_CONFIRM = "RECEIPT_AWAITING_SAVE_CONFIRM"
STATE_RECEIPT_ITEM_ASSIGN = "RECEIPT_ITEM_ASSIGN"

# States used in the manual-entry path (unreadable receipt)
STATE_RECEIPT_MANUAL_MERCHANT = "RECEIPT_MANUAL_MERCHANT"
STATE_RECEIPT_MANUAL_AMOUNT = "RECEIPT_MANUAL_AMOUNT"
STATE_RECEIPT_MANUAL_DATE = "RECEIPT_MANUAL_DATE"

RECEIPT_STATES = {
    STATE_RECEIPT_PROCESSING,
    STATE_RECEIPT_AWAITING_CONFIRM,
    STATE_RECEIPT_AWAITING_PAYER,
    STATE_RECEIPT_AWAITING_SPLIT,
    STATE_RECEIPT_AWAITING_ASSIGNMENT,
    STATE_RECEIPT_AWAITING_SAVE_CONFIRM,
    STATE_RECEIPT_ITEM_ASSIGN,
    STATE_RECEIPT_MANUAL_MERCHANT,
    STATE_RECEIPT_MANUAL_AMOUNT,
    STATE_RECEIPT_MANUAL_DATE,
}

# ---------------------------------------------------------------------------
# Callback data constants
# ---------------------------------------------------------------------------

CB_CONFIRM = "receipt:confirm"
CB_EDIT = "receipt:edit"
CB_USE_TODAY = "receipt:use_today"
CB_KEEP_MONTH = "receipt:keep_month"
CB_SAVE_DUPLICATE = "receipt:save_duplicate"
CB_CANCEL = "receipt:cancel"
CB_PAYER_PREFIX = "receipt:payer:"
CB_SPLIT_EQUAL = "receipt:split_equal"
CB_SPLIT_ASSIGN = "receipt:split_assign"
CB_SAVE = "receipt:save"
CB_REASSIGN = "receipt:reassign"
CB_ASSIGN_ITEM_PREFIX = "receipt:item:"   # receipt:item:{item_idx}:{member_idx or "s"}
CB_ASSIGN_SUBMIT = "receipt:item_done"    # note: underscore, not colon — won't match prefix
CB_CATEGORY_MENU = "receipt:catmenu"
CB_CAT_PREFIX = "receipt:cat:"           # receipt:cat:Dining  (won't match item: prefix)
CB_CAT_BACK = "receipt:catback"
CB_VIEW_EXPENSE = "receipt:view:"        # receipt:view:<expense_id>


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


_CATEGORIES = [
    "Grocery", "Dining", "Transport", "Utilities",
    "Health", "Entertainment", "Shopping", "Other",
]


def _confirm_keyboard(category: str | None = None) -> InlineKeyboardMarkup:
    cat_label = f"📁 {category}" if category else "📁 Set Category"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=CB_CONFIRM),
            InlineKeyboardButton("✏️ Edit", callback_data=CB_EDIT),
        ],
        [InlineKeyboardButton(cat_label, callback_data=CB_CATEGORY_MENU)],
    ])


def _category_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(c, callback_data=f"{CB_CAT_PREFIX}{c}") for c in _CATEGORIES[i:i+3]]
        for i in range(0, len(_CATEGORIES), 3)
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data=CB_CAT_BACK)])
    return InlineKeyboardMarkup(rows)


def _month_keyboard(receipt_month: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                f"Save to {receipt_month}", callback_data=CB_KEEP_MONTH
            ),
            InlineKeyboardButton("Use today's date", callback_data=CB_USE_TODAY),
        ]]
    )


def _duplicate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Save anyway", callback_data=CB_SAVE_DUPLICATE),
            InlineKeyboardButton("Cancel", callback_data=CB_CANCEL),
        ]]
    )


def _payer_keyboard(members: list[str]) -> InlineKeyboardMarkup:
    buttons: list[InlineKeyboardButton] = [
        InlineKeyboardButton(name, callback_data=f"{CB_PAYER_PREFIX}{name}")
        for name in members
    ]
    # Arrange into rows of 3
    rows: list[list[InlineKeyboardButton]] = [
        buttons[i : i + 3] for i in range(0, len(buttons), 3)
    ]
    return InlineKeyboardMarkup(rows)


def _split_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("All equal", callback_data=CB_SPLIT_EQUAL),
            InlineKeyboardButton("Assign items", callback_data=CB_SPLIT_ASSIGN),
        ]]
    )


def _save_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Save", callback_data=CB_SAVE),
            InlineKeyboardButton("Re-assign", callback_data=CB_REASSIGN),
        ]]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Cancel", callback_data=CB_CANCEL)]]
    )


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

_NUM_EMOJIS = [
    "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
    "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳",
]


def _get_month_label(
    dt: datetime, timezone: str = "America/Toronto"
) -> str:
    """Return a month label like 'Mar 2026' for the given datetime."""
    tz = pytz.timezone(timezone)
    local_dt = dt.astimezone(tz)
    return local_dt.strftime("%b %Y")


def _today_month_label(timezone: str = "America/Toronto") -> str:
    """Return the month label for today."""
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    return now.strftime("%b %Y")


def _today_date_str(timezone: str = "America/Toronto") -> str:
    """Return today's date as YYYY-MM-DD string."""
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d")


def _parse_receipt_month(date_str: Optional[str], timezone: str = "America/Toronto") -> Optional[str]:
    """
    Given a date string in YYYY-MM-DD format, return its month label.
    Returns None if the date_str is not parseable.
    """
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        tz = pytz.timezone(timezone)
        dt_aware = tz.localize(dt)
        return _get_month_label(dt_aware, timezone)
    except Exception:
        return None


def _is_duplicate(
    group_id: str,
    month_label: str,
    merchant: Optional[str],
    total: Optional[float],
    date_str: Optional[str],
) -> bool:
    """
    Check if an expense with same merchant+total+date already exists in the DB.
    Returns False on any error (safe default).
    """
    if not group_id or not merchant or total is None or not date_str:
        return False
    try:
        expenses = database.get_expenses(group_id, month_label)
    except Exception:
        return False

    merchant_lower = merchant.lower().strip()
    for exp in expenses:
        exp_merchant = str(exp.get("description", "")).lower().strip()
        exp_total = exp.get("total")
        exp_date = str(exp.get("date", "")).strip()
        if (
            exp_merchant == merchant_lower
            and exp_total is not None
            and abs(float(exp_total) - float(total)) < 0.01
            and exp_date == date_str
        ):
            return True
    return False


def _format_items_text(items: list[dict]) -> str:
    """Format items list for display in split selection message."""
    if not items:
        return ""
    lines = ["Items detected:"]
    for i, item in enumerate(items):
        name = item.get("name", "Item")
        price = item.get("price")
        taxable = item.get("taxable", False)
        num = _NUM_EMOJIS[i] if i < len(_NUM_EMOJIS) else f"{i + 1}."
        tax_tag = " H" if taxable else ""
        if price is not None:
            lines.append(f"{num} {name} ${float(price):.2f}{tax_tag}")
        else:
            lines.append(f"{num} {name}{tax_tag}")
    return "\n".join(lines)


def _equal_split(members: list[str], total: float) -> dict:
    """Split total equally among all members, remainder to first."""
    if not members:
        return {}
    n = len(members)
    base = round(total / n, 2)
    total_assigned = round(base * n, 2)
    remainder = round(total - total_assigned, 2)
    shares = {m: base for m in members}
    shares[members[0]] = round(shares[members[0]] + remainder, 2)
    return shares


def _format_split_summary(member_shares: dict) -> str:
    """Format member_shares dict as a readable summary."""
    lines = []
    for member, amount in member_shares.items():
        lines.append(f"  {member} owes: ${float(amount):.2f}")
    return "\n".join(lines)


def _item_assign_keyboard(
    items: list[dict],
    member_names: list[str],
    assignments: dict,
) -> InlineKeyboardMarkup:
    """Build per-item assignment keyboard.

    assignments: {str(item_idx): int(member_idx) | "s"} — "s" means split equally.
    The selected choice shows a checkmark prefix.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for i, _item in enumerate(items):
        num = _NUM_EMOJIS[i] if i < len(_NUM_EMOJIS) else f"{i + 1}."
        current = assignments.get(str(i))
        buttons: list[InlineKeyboardButton] = []
        for j, name in enumerate(member_names):
            check = "✓" if current == j else ""
            buttons.append(InlineKeyboardButton(
                f"{check}{name}",
                callback_data=f"receipt:item:{i}:{j}",
            ))
        share_check = "✓" if current == "s" else ""
        buttons.append(InlineKeyboardButton(
            f"{share_check}Share",
            callback_data=f"receipt:item:{i}:s",
        ))
        rows.append(buttons)

    assigned = sum(1 for v in assignments.values() if v is not None)
    rows.append([InlineKeyboardButton(
        f"✅ Submit ({assigned}/{len(items)} assigned)",
        callback_data=CB_ASSIGN_SUBMIT,
    )])
    rows.append([InlineKeyboardButton("Cancel", callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def _build_item_assign_text(
    items: list[dict],
    member_names: list[str],
    assignments: dict,
) -> str:
    """Build message text showing all items with current assignment status."""
    lines = ["Select who paid for each item:"]
    for i, item in enumerate(items):
        num = _NUM_EMOJIS[i] if i < len(_NUM_EMOJIS) else f"{i + 1}."
        name = item.get("name") or f"Item {i + 1}"
        price = item.get("price")
        taxable = item.get("taxable", False)
        price_str = f" ${float(price):.2f}" if price is not None else ""
        tax_tag = " H" if taxable else ""
        current = assignments.get(str(i))
        if current is None:
            tag = ""
        elif current == "s":
            tag = " — Share"
        elif isinstance(current, int) and 0 <= current < len(member_names):
            tag = f" — {member_names[current]}"
        else:
            tag = ""
        lines.append(f"{num} {name}{price_str}{tax_tag}{tag}")
    return "\n".join(lines)


def _assignments_to_shares(
    items: list[dict],
    assignments: dict,
    member_names: list[str],
    total: float,
    hst_pct: Optional[float] = None,
) -> dict:
    """Convert per-item assignments to member_shares, scaled to the receipt total.

    assignments: {str(item_idx): int(member_idx) | "s" | None}
    Unassigned and "s" items are split equally.
    Items marked taxable get their price multiplied by (1 + hst_pct/100).
    The result is proportionally scaled so shares sum to total.
    """
    n = len(member_names)
    if n == 0:
        return {}

    hst_mult = 1.0 + (hst_pct / 100.0) if hst_pct else 1.0
    shares: dict[str, float] = {m: 0.0 for m in member_names}

    for idx, item in enumerate(items):
        price = float(item.get("price") or 0.0)
        taxable = bool(item.get("taxable", False))
        effective = price * hst_mult if taxable else price

        current = assignments.get(str(idx))
        if current is None or current == "s":
            per = effective / n
            for m in member_names:
                shares[m] += per
        elif isinstance(current, int) and 0 <= current < n:
            shares[member_names[current]] += effective

    item_sum = sum(shares.values())
    if item_sum <= 0.0:
        return _equal_split(member_names, total)

    # Scale proportionally to actual total (absorbs HST on non-taxable items, coupons, etc.)
    if total > 0.0:
        scale = total / item_sum
        shares = {m: v * scale for m, v in shares.items()}

    # Round to 2 dp and fix any penny rounding difference
    shares = {m: round(v, 2) for m, v in shares.items()}
    diff = round(total - sum(shares.values()), 2)
    if diff != 0.0:
        shares[member_names[0]] = round(shares[member_names[0]] + diff, 2)

    return shares


async def _ask_item_assign(
    update: Update,
    group_id: str,
    user_id: str,
    ctx: dict,
) -> None:
    """Transition to RECEIPT_ITEM_ASSIGN and show per-item button keyboard."""
    extracted = ctx.get("extracted", {})
    items = extracted.get("items") or []
    members_data = database.get_members(group_id)
    member_names = [m["name"] for m in members_data]

    ctx["_member_names"] = member_names
    ctx["item_assignments"] = {}
    database.set_state(user_id, group_id, STATE_RECEIPT_ITEM_ASSIGN, ctx)

    text = _build_item_assign_text(items, member_names, {})
    keyboard = _item_assign_keyboard(items, member_names, {})

    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


def _get_fixed_expenses_for_sheet(group_id: str, members: list[str]) -> list[dict]:
    """
    Build the fixed_expenses list in the format expected by get_or_create_month_tab.
    Returns list of dicts: {description, amount, paid_by_name, member_shares}.
    """
    db_fixed = database.get_fixed_expenses(group_id)
    result = []
    # Fetch member list once — avoid N round-trips for N fixed expenses
    all_members = database.get_members(group_id)
    member_id_to_name: dict = {m["id"]: m.get("name", "") for m in all_members}
    for fe in db_fixed:
        amount = float(fe.get("amount", 0.0))
        # Look up the member name from paid_by_member_id
        paid_by_member_id = fe.get("paid_by_member_id")
        paid_by_name = member_id_to_name.get(paid_by_member_id, "") if paid_by_member_id else ""

        split_type = fe.get("split_type", "equal")
        # Build member_shares: equal split or assigned to one person
        if split_type == "equal":
            shares = _equal_split(members, amount)
        else:
            # split_type is a member name
            shares = {m: 0.0 for m in members}
            target = split_type.lower()
            for m in members:
                if m.lower() == target:
                    shares[m] = amount
                    break
            else:
                # fallback: equal split
                shares = _equal_split(members, amount)

        result.append({
            "description": fe.get("description", ""),
            "amount": amount,
            "paid_by_name": paid_by_name,
            "member_shares": shares,
        })
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_receipt_state(user_id: str, group_id: str) -> bool:
    """Return True if user has an active receipt processing state."""
    _sr2 = database.get_state(user_id, group_id); state = _sr2.get("state") if _sr2 else None
    return state in RECEIPT_STATES


async def handle_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Entry point when a photo is received in a configured group.
    Downloads the photo, calls extract_receipt(), shows extracted data,
    handles month mismatch and duplicate detection.
    """
    if update.effective_chat is None or update.effective_user is None:
        return
    if update.effective_message is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    # Edge case 2: group not set up
    group = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    # Edge case 3: sequential processing guard
    _sr3 = database.get_state(user_id, group_id); existing_state = _sr3.get("state") if _sr3 else None
    if existing_state in RECEIPT_STATES:
        await update.effective_message.reply_text(
            "I'm still processing your previous receipt. "
            "Please finish or cancel it first."
        )
        return

    # Send "processing" message
    processing_msg = await update.effective_message.reply_text(
        "🐿️ Pip is carefully reading your receipt..."
    )

    # Set state immediately to RECEIPT_PROCESSING to block concurrent photos
    database.set_state(
        user_id, group_id, STATE_RECEIPT_PROCESSING,
        {"processing_message_id": processing_msg.message_id}
    )

    # Download the photo (largest available size)
    photo = update.effective_message.photo
    if not photo:
        await processing_msg.edit_text(
            "No photo found. Please send a receipt photo."
        )
        database.clear_state(user_id, group_id)
        return

    photo_file_info = photo[-1]  # largest resolution
    photo_file = await photo_file_info.get_file()
    image_bytes = await photo_file.download_as_bytearray()

    # Determine MIME type — Telegram photos are always JPEG
    mime_type = "image/jpeg"

    # --- SaaS Gating: Check Pro status and monthly OCR usage ---
    from main import IS_PRO  # Global override
    is_pro_group = database.is_group_pro(group_id)
    
    if not (IS_PRO or is_pro_group):
        usage = database.get_ocr_count(group_id)
        if usage >= 10:
            await processing_msg.edit_text(
                "⚠️ **Monthly Limit Reached**\n\n"
                "You've used your 10 free receipt scans this month. "
                "Upgrade to **NutSplit Pro** for unlimited AI scanning, spending trends, and category budgets!\n\n"
                "Run /upgrade to unlock everything. 🥜",
                parse_mode="Markdown"
            )
            database.clear_state(user_id, group_id)
            return

    # Call Gemini extraction
    try:
        extracted = extract_receipt(bytes(image_bytes), mime_type)
        # Increment usage if NOT pro
        if not (IS_PRO or is_pro_group):
            database.increment_ocr_count(group_id)
    except Exception as exc:
        logger.exception("Receipt extraction failed: %s", exc)
        await processing_msg.edit_text(
            "🌰 Oops! Pip dropped the receipt and couldn't read it. Please try again."
        )
        database.clear_state(user_id, group_id)
        return

    timezone = group.get("timezone", "America/Toronto")

    # Edge case 1: completely unreadable receipt
    if extracted.get("failed_fields") == ["all"]:
        database.set_state(
            user_id, group_id, STATE_RECEIPT_MANUAL_MERCHANT,
            {
                "extracted": extracted,
                "month_label": _today_month_label(timezone),
                "paid_by": None,
                "member_shares": None,
                "expense_id": None,
                "original_month": None,
            }
        )
        await processing_msg.edit_text(
            "🌰 Pip couldn't decipher this one! Let's enter it manually:\n\n"
            "What's the merchant name?",
            reply_markup=_cancel_keyboard(),
        )
        return

    # Determine month label
    today_month = _today_month_label(timezone)
    receipt_date = extracted.get("date")
    receipt_month = _parse_receipt_month(receipt_date, timezone)

    # Format extraction for display
    display_text = format_extraction_for_display(extracted)

    # Build the initial state context
    ctx: dict = {
        "extracted": extracted,
        "month_label": today_month,
        "paid_by": None,
        "member_shares": None,
        "expense_id": None,
        "original_month": receipt_month if receipt_month and receipt_month != today_month else None,
    }

    # Edge case 4: receipt from a different month
    if receipt_month and receipt_month != today_month:
        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        await processing_msg.edit_text(
            f"{display_text}\n\n"
            f"This receipt is from {receipt_month}. "
            f"Save to the {receipt_month} tab or use today's date?",
            reply_markup=_month_keyboard(receipt_month),
        )
        return

    # No month mismatch — check for duplicates
    receipt_date = extracted.get("date")
    if receipt_date:
        merchant = extracted.get("merchant")
        total_val = extracted.get("total")
        if _is_duplicate(group_id, today_month, merchant, total_val, receipt_date):
            database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
            await processing_msg.edit_text(
                f"{display_text}\n\n"
                f"Possible duplicate: {merchant} ${total_val:.2f} already saved. "
                f"Save anyway?",
                reply_markup=_duplicate_keyboard(),
            )
            return

    # Normal flow: show extracted data with confirm/edit keyboard
    database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
    await processing_msg.edit_text(
        display_text,
        reply_markup=_confirm_keyboard(ctx.get("extracted", {}).get("category") if ctx else None),
    )


async def _ask_payer(
    update: Update,
    group_id: str,
    user_id: str,
    ctx: dict,
) -> None:
    """Transition to RECEIPT_AWAITING_PAYER and ask who paid."""
    members_data = database.get_members(group_id)
    member_names = [m["name"] for m in members_data]

    ctx["_member_names"] = member_names
    database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_PAYER, ctx)

    query = update.callback_query
    if query:
        await query.edit_message_text(
            "Who paid for this?",
            reply_markup=_payer_keyboard(member_names),
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "Who paid for this?",
            reply_markup=_payer_keyboard(member_names),
        )


async def _ask_split(
    update: Update,
    group_id: str,
    user_id: str,
    ctx: dict,
) -> None:
    """Transition to RECEIPT_AWAITING_SPLIT and show split options."""
    extracted = ctx.get("extracted", {})
    items = extracted.get("items") or []

    items_text = _format_items_text(items)
    members_data = database.get_members(group_id)
    member_names = [m["name"] for m in members_data]
    members_list = ", ".join(member_names)

    if items_text:
        body = (
            f"{items_text}\n\n"
            f"Default: split equally among all members ({members_list}).\n"
            "To assign items: reply \"2 karlos\" or \"1 karlos partner\"\n"
            "\"all except [name]\" also works."
        )
    else:
        body = (
            f"Default: split equally among all members ({members_list}).\n"
            "To assign items: reply item assignments like \"2 karlos\" or "
            "\"1 karlos partner\"."
        )

    database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_SPLIT, ctx)

    query = update.callback_query
    if query:
        await query.edit_message_text(body, reply_markup=_split_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(body, reply_markup=_split_keyboard())


async def _show_save_confirm(
    update: Update,
    group_id: str,
    user_id: str,
    ctx: dict,
) -> None:
    """Show the final split summary and save/re-assign buttons."""
    member_shares = ctx.get("member_shares", {})
    paid_by = ctx.get("paid_by", "")
    extracted = ctx.get("extracted", {})
    total = extracted.get("total") or 0.0

    split_text = _format_split_summary(member_shares)

    database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_SAVE_CONFIRM, ctx)

    msg = (
        f"Here's the split:\n{split_text}\n\n"
        f"Paid by: {paid_by}\n"
        f"Total: ${float(total):.2f}"
    )

    query = update.callback_query
    if query:
        await query.edit_message_text(msg, reply_markup=_save_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(msg, reply_markup=_save_keyboard())


async def _do_save(
    update: Update,
    group_id: str,
    user_id: str,
    ctx: dict,
) -> None:
    """
    Perform the actual save to local SQLite:
    1. get_or_create_month (seeds fixed expenses)
    2. get_next_expense_id
    3. append_expense
    4. get_month_expenses → calculate_balances
    5. Show success + balance summary
    6. clear_state
    """
    group = database.get_group(group_id)
    if group is None:
        return

    timezone = group.get("timezone", "America/Toronto")
    currency = group.get("currency", "CAD")

    members_data = database.get_members(group_id)
    member_names = [m["name"] for m in members_data]

    month_label = ctx.get("month_label", _today_month_label(timezone))
    extracted = ctx.get("extracted", {})
    paid_by = ctx.get("paid_by", "")
    member_shares = ctx.get("member_shares", {})

    # Determine the date to use
    receipt_date = extracted.get("date") or _today_date_str(timezone)

    query = update.callback_query

    try:
        # Step 1: ensure month is initialised (seeds fixed expenses)
        get_or_create_month(
            group_id=group_id,
            month_label=month_label,
            members=member_names,
            fixed_expenses=[],
        )

        # Steps 2+3: get ID and save expense
        expense_id = get_next_expense_id(group_id, month_label)
        expense_row = {
            "expense_id": expense_id,
            "date": receipt_date,
            "description": extracted.get("merchant") or "",
            "category": extracted.get("category") or "Other",
            "subtotal": extracted.get("subtotal") or 0.0,
            "hst_amount": extracted.get("hst_amount") or 0.0,
            "hst_pct": extracted.get("hst_pct") or 0.0,
            "tip_amount": extracted.get("tip_amount") or 0.0,
            "tip_pct": extracted.get("tip_pct") or 0.0,
            "total": extracted.get("total") or 0.0,
            "paid_by": paid_by,
            "member_shares": member_shares,
            "notes": "",
        }
        append_expense(group_id=group_id, month_label=month_label, expense=expense_row)

        # Step 4: calculate balances
        all_expenses = get_month_expenses(group_id, month_label)
        balances = calculate_balances(all_expenses, member_names)

        # Step 5: show success + balance summary
        balance_text = format_balance_summary(balances, month_label, currency=currency)
        success_msg = f"🌰 <b>Tucked away!</b> (Receipt #{html.escape(expense_id)})\n\n{balance_text}"
        view_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 View expense", callback_data=f"{CB_VIEW_EXPENSE}{expense_id}"),
        ]])

        if query:
            await query.edit_message_text(success_msg, parse_mode="HTML", reply_markup=view_kb)
        elif update.effective_message:
            await update.effective_message.reply_text(success_msg, parse_mode="HTML", reply_markup=view_kb)

    except Exception as exc:
        logger.exception("Failed to save expense: %s", exc)
        err_msg = (
            "⚠️ <b>Something went wrong</b>\n"
            f"{html.escape(str(exc))}\n\nPlease try again."
        )
        if query:
            await query.edit_message_text(err_msg, parse_mode="HTML")
        elif update.effective_message:
            await update.effective_message.reply_text(err_msg, parse_mode="HTML")

    # Step 8: clear state regardless of success/failure
    database.clear_state(user_id, group_id)


# ---------------------------------------------------------------------------
# Message handler (text messages during receipt flow)
# ---------------------------------------------------------------------------


async def handle_receipt_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle text messages during receipt flow (item assignments, manual entry)."""
    if update.effective_chat is None or update.effective_user is None:
        return
    if update.effective_message is None or update.effective_message.text is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    text = update.effective_message.text.strip()

    _sr = database.get_state(user_id, group_id); state = _sr.get("state") if _sr else None; ctx = _sr.get("context") if _sr else None

    if state not in RECEIPT_STATES:
        return
    if ctx is None:
        return

    # ------------------------------------------------------------------
    # Manual entry: merchant name
    # ------------------------------------------------------------------
    if state == STATE_RECEIPT_MANUAL_MERCHANT:
        if not text:
            await update.effective_message.reply_text(
                "Please enter the merchant name.",
                reply_markup=_cancel_keyboard(),
            )
            return
        ctx["extracted"]["merchant"] = text
        database.set_state(user_id, group_id, STATE_RECEIPT_MANUAL_AMOUNT, ctx)
        await update.effective_message.reply_text(
            f"Got it: {text}. What was the total amount? (e.g., 45.20)",
            reply_markup=_cancel_keyboard(),
        )
        return

    # ------------------------------------------------------------------
    # Manual entry: total amount
    # ------------------------------------------------------------------
    if state == STATE_RECEIPT_MANUAL_AMOUNT:
        try:
            amount = float(text.replace("$", "").replace(",", ".").strip())
            if amount <= 0:
                raise ValueError("Amount must be positive")
        except ValueError:
            await update.effective_message.reply_text(
                "Please enter a valid amount (e.g., 45.20).",
                reply_markup=_cancel_keyboard(),
            )
            return
        ctx["extracted"]["total"] = amount
        ctx["extracted"]["subtotal"] = amount
        database.set_state(user_id, group_id, STATE_RECEIPT_MANUAL_DATE, ctx)
        await update.effective_message.reply_text(
            f"Amount: ${amount:.2f}. What was the date? (YYYY-MM-DD, or 'today')",
            reply_markup=_cancel_keyboard(),
        )
        return

    # ------------------------------------------------------------------
    # Manual entry: date
    # ------------------------------------------------------------------
    if state == STATE_RECEIPT_MANUAL_DATE:
        group = database.get_group(group_id)
        timezone = group.get("timezone", "America/Toronto") if group else "America/Toronto"

        if text.lower() == "today":
            date_str = _today_date_str(timezone)
        else:
            try:
                datetime.strptime(text, "%Y-%m-%d")
                date_str = text
            except ValueError:
                await update.effective_message.reply_text(
                    "Please enter a valid date in YYYY-MM-DD format, or 'today'.",
                    reply_markup=_cancel_keyboard(),
                )
                return

        ctx["extracted"]["date"] = date_str
        receipt_month = _parse_receipt_month(date_str, timezone)
        ctx["month_label"] = receipt_month or _today_month_label(timezone)
        ctx["original_month"] = None

        # Show what was entered and confirm
        extracted = ctx["extracted"]
        display = format_extraction_for_display(extracted)
        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        await update.effective_message.reply_text(
            display,
            reply_markup=_confirm_keyboard(ctx.get("extracted", {}).get("category") if ctx else None),
        )
        return

    # ------------------------------------------------------------------
    # Item assignment text during RECEIPT_AWAITING_ASSIGNMENT
    # ------------------------------------------------------------------
    if state == STATE_RECEIPT_AWAITING_ASSIGNMENT:
        members_data = database.get_members(group_id)
        member_names = [m["name"] for m in members_data]
        extracted = ctx.get("extracted", {})
        items = extracted.get("items") or []
        total = float(extracted.get("total") or 0.0)

        # Determine the sender's name
        sender_name = ""
        if update.effective_user:
            telegram_id = str(update.effective_user.id)
            sender_member = database.get_member_by_telegram_id(group_id, telegram_id)
            if sender_member:
                sender_name = sender_member.get("name", "")
        if not sender_name and member_names:
            sender_name = member_names[0]

        try:
            shares = parse_member_shares(text, member_names, items, total, sender_name)
        except Exception as exc:
            logger.warning("parse_member_shares failed: %s", exc)
            shares = _equal_split(member_names, total)

        ctx["member_shares"] = shares
        await _show_save_confirm(update, group_id, user_id, ctx)
        return

    # ------------------------------------------------------------------
    # Text received when awaiting confirmation — may be an edit reply
    # ------------------------------------------------------------------
    if state == STATE_RECEIPT_AWAITING_CONFIRM:
        if ctx.get("_awaiting_edit"):
            # Parse "field: value" edits
            if ":" in text:
                field_raw, _, value_raw = text.partition(":")
                field = field_raw.strip().lower()
                value = value_raw.strip()
                extracted = ctx.get("extracted", {})
                valid_fields = {"merchant", "date", "total", "category"}
                if field in valid_fields:
                    if field == "total":
                        try:
                            extracted["total"] = float(
                                value.replace("$", "").replace(",", ".").strip()
                            )
                        except ValueError:
                            await update.effective_message.reply_text(
                                "Invalid total amount. Try again:\n  total: 52.30",
                                reply_markup=_cancel_keyboard(),
                            )
                            return
                    elif field == "date":
                        try:
                            datetime.strptime(value, "%Y-%m-%d")
                            extracted["date"] = value
                        except ValueError:
                            await update.effective_message.reply_text(
                                "Invalid date. Use YYYY-MM-DD:\n  date: 2026-03-01",
                                reply_markup=_cancel_keyboard(),
                            )
                            return
                    else:
                        extracted[field] = value
                    ctx["extracted"] = extracted
                    ctx["_awaiting_edit"] = False
                    display = format_extraction_for_display(extracted)
                    database.set_state(
                        user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx
                    )
                    await update.effective_message.reply_text(
                        display, reply_markup=_confirm_keyboard(ctx.get("extracted", {}).get("category") if ctx else None)
                    )
                    return
                else:
                    await update.effective_message.reply_text(
                        f"Unknown field '{field_raw.strip()}'. "
                        "Valid fields: merchant, date, total, category.",
                        reply_markup=_cancel_keyboard(),
                    )
                    return
            else:
                await update.effective_message.reply_text(
                    "Use format: field: new value\nExample: merchant: Costco",
                    reply_markup=_cancel_keyboard(),
                )
                return

        # Not in edit mode — re-prompt with data and confirm buttons
        extracted = ctx.get("extracted", {})
        display = format_extraction_for_display(extracted)
        await update.effective_message.reply_text(
            f"{display}\n\nPlease use the buttons above.",
            reply_markup=_confirm_keyboard(ctx.get("extracted", {}).get("category") if ctx else None),
        )
        return

    if state == STATE_RECEIPT_AWAITING_PAYER:
        members_data = database.get_members(group_id)
        member_names = [m["name"] for m in members_data]
        await update.effective_message.reply_text(
            "Please use the buttons to select who paid.",
            reply_markup=_payer_keyboard(member_names),
        )
        return

    if state == STATE_RECEIPT_AWAITING_SPLIT:
        await update.effective_message.reply_text(
            "Please use the buttons to select a split option.",
            reply_markup=_split_keyboard(),
        )
        return

    if state == STATE_RECEIPT_AWAITING_SAVE_CONFIRM:
        member_shares = ctx.get("member_shares", {})
        split_text = _format_split_summary(member_shares)
        await update.effective_message.reply_text(
            f"Here's the split:\n{split_text}\n\nPlease use the buttons.",
            reply_markup=_save_keyboard(),
        )
        return

    if state == STATE_RECEIPT_ITEM_ASSIGN:
        extracted = ctx.get("extracted", {})
        items = extracted.get("items") or []
        member_names = ctx.get("_member_names") or [m["name"] for m in database.get_members(group_id)]
        assignments = ctx.get("item_assignments", {})
        assign_text = _build_item_assign_text(items, member_names, assignments)
        await update.effective_message.reply_text(
            f"{assign_text}\n\nPlease use the buttons above to assign items.",
            reply_markup=_item_assign_keyboard(items, member_names, assignments),
        )
        return


# ---------------------------------------------------------------------------
# Callback handler (inline keyboard presses during receipt flow)
# ---------------------------------------------------------------------------


async def handle_receipt_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline keyboard callbacks during receipt flow."""
    query = update.callback_query
    if query is None:
        return

    await query.answer()

    if update.effective_chat is None or update.effective_user is None:
        return

    group_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    data: str = query.data or ""

    if not data.startswith("receipt:"):
        return

    # ------------------------------------------------------------------
    # View expense (post-save, no active flow required)
    # ------------------------------------------------------------------
    if data.startswith(CB_VIEW_EXPENSE):
        eid = data[len(CB_VIEW_EXPENSE):]
        group = database.get_group(group_id)
        currency = (group.get("currency") or "CAD") if group else "CAD"
        sym = "$"
        # Search all months for this expense_id
        found_exp = None
        for m in database.get_all_months_summary(group_id):
            exps = database.get_expenses(group_id, m["month_label"])
            found_exp = next((e for e in exps if e["expense_id"] == eid), None)
            if found_exp:
                break
        if found_exp is None:
            await query.edit_message_text(f"Expense {html.escape(eid)} not found.")
            return
        desc = html.escape(found_exp.get("description", "?"))
        total_val = float(found_exp.get("total", 0.0))
        paid_by = found_exp.get("paid_by", "?")
        date_raw = found_exp.get("date", "?")
        try:
            short_date = datetime.strptime(date_raw, "%Y-%m-%d").strftime("%b %d %Y")
        except Exception:
            short_date = date_raw
        shares: dict = found_exp.get("member_shares") or {}
        share_parts = [f"{m} {sym}{float(a):.2f}" for m, a in shares.items()]
        share_str = "  " + " · ".join(share_parts) if share_parts else ""
        text = f"<b>#{html.escape(eid)}</b> {desc}\n💰 {sym}{total_val:.2f} · {paid_by} · {short_date}"
        if share_str:
            text += f"\n{share_str}"
        await query.edit_message_text(text, parse_mode="HTML")
        return

    _sr = database.get_state(user_id, group_id); state = _sr.get("state") if _sr else None; ctx = _sr.get("context") if _sr else None

    if state not in RECEIPT_STATES:
        await query.edit_message_text(
            "No active receipt flow found. Send a receipt photo to start."
        )
        return
    if ctx is None:
        await query.edit_message_text(
            "No active receipt flow found. Send a receipt photo to start."
        )
        return

    group = database.get_group(group_id)
    timezone = group.get("timezone", "America/Toronto") if group else "America/Toronto"

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------
    if data == CB_CANCEL:
        database.clear_state(user_id, group_id)
        await query.edit_message_text("Receipt flow cancelled.")
        return

    # ------------------------------------------------------------------
    # Month handling: use today's date
    # ------------------------------------------------------------------
    if data == CB_USE_TODAY:
        today_str = _today_date_str(timezone)
        ctx["extracted"]["date"] = today_str
        ctx["month_label"] = _today_month_label(timezone)
        ctx["original_month"] = None

        # Check for duplicates now with the new date
        merchant = ctx["extracted"].get("merchant")
        total = ctx["extracted"].get("total")
        if merchant and total is not None and _is_duplicate(
            group_id, ctx["month_label"], merchant, total, today_str
        ):
            database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
            await query.edit_message_text(
                f"Possible duplicate: {merchant} ${float(total):.2f} already saved. "
                f"Save anyway?",
                reply_markup=_duplicate_keyboard(),
            )
            return

        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        display = format_extraction_for_display(ctx["extracted"])
        await query.edit_message_text(display, reply_markup=_confirm_keyboard(ctx.get("extracted", {}).get("category") if ctx else None))
        return

    # ------------------------------------------------------------------
    # Month handling: keep original receipt month
    # ------------------------------------------------------------------
    if data == CB_KEEP_MONTH:
        original_month = ctx.get("original_month")
        if original_month:
            ctx["month_label"] = original_month

        # Check for duplicates in the receipt's own month
        merchant = ctx["extracted"].get("merchant")
        total = ctx["extracted"].get("total")
        receipt_date = ctx["extracted"].get("date")
        if merchant and total is not None and receipt_date and _is_duplicate(
            group_id, ctx["month_label"], merchant, total, receipt_date
        ):
            database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
            await query.edit_message_text(
                f"Possible duplicate: {merchant} ${float(total):.2f} already saved "
                f"in {ctx['month_label']}. Save anyway?",
                reply_markup=_duplicate_keyboard(),
            )
            return

        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        display = format_extraction_for_display(ctx["extracted"])
        await query.edit_message_text(display, reply_markup=_confirm_keyboard(ctx.get("extracted", {}).get("category") if ctx else None))
        return

    # ------------------------------------------------------------------
    # Duplicate: save anyway
    # ------------------------------------------------------------------
    if data == CB_SAVE_DUPLICATE:
        # Proceed to payer selection despite duplicate
        await _ask_payer(update, group_id, user_id, ctx)
        return

    # ------------------------------------------------------------------
    # Category menu (show category picker)
    # ------------------------------------------------------------------
    if data == CB_CATEGORY_MENU:
        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        current = ctx.get("extracted", {}).get("category") or "—"
        await query.edit_message_text(
            f"Select a category:\nCurrent: <b>{html.escape(current)}</b>",
            reply_markup=_category_keyboard(),
            parse_mode="HTML",
        )
        return

    # ------------------------------------------------------------------
    # Category selected
    # ------------------------------------------------------------------
    if data.startswith(CB_CAT_PREFIX):
        category = data[len(CB_CAT_PREFIX):]
        ctx.setdefault("extracted", {})["category"] = category
        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        display = format_extraction_for_display(ctx["extracted"])
        await query.edit_message_text(display, reply_markup=_confirm_keyboard(category))
        return

    # ------------------------------------------------------------------
    # Back from category menu
    # ------------------------------------------------------------------
    if data == CB_CAT_BACK:
        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        extracted = ctx.get("extracted", {})
        display = format_extraction_for_display(extracted)
        await query.edit_message_text(
            display, reply_markup=_confirm_keyboard(extracted.get("category"))
        )
        return

    # ------------------------------------------------------------------
    # Confirm data
    # ------------------------------------------------------------------
    if data == CB_CONFIRM:
        await _ask_payer(update, group_id, user_id, ctx)
        return

    # ------------------------------------------------------------------
    # Edit a field (show field selection or inform user to reply)
    # ------------------------------------------------------------------
    if data == CB_EDIT:
        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        extracted = ctx.get("extracted", {})
        merchant = extracted.get("merchant") or "—"
        date_val = extracted.get("date") or "—"
        total = extracted.get("total")
        total_str = f"${float(total):.2f}" if total is not None else "—"
        category = extracted.get("category") or "—"

        await query.edit_message_text(
            f"Which field do you want to edit?\n\n"
            f"Current values:\n"
            f"  Merchant: {merchant}\n"
            f"  Date: {date_val}\n"
            f"  Total: {total_str}\n"
            f"  Category: {category}\n\n"
            "Reply with: field: new value\n"
            "Example: merchant: Costco\n"
            "Example: total: 52.30\n"
            "Example: date: 2026-03-01\n"
            "Example: category: Grocery",
            reply_markup=_cancel_keyboard(),
        )
        # Set a flag in ctx to handle the edit reply
        ctx["_awaiting_edit"] = True
        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_CONFIRM, ctx)
        return

    # ------------------------------------------------------------------
    # Payer selection: receipt:payer:<name>
    # ------------------------------------------------------------------
    if data.startswith(CB_PAYER_PREFIX):
        payer_name = data[len(CB_PAYER_PREFIX):]
        ctx["paid_by"] = payer_name
        database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_PAYER, ctx)
        await _ask_split(update, group_id, user_id, ctx)
        return

    # ------------------------------------------------------------------
    # Split equally
    # ------------------------------------------------------------------
    if data == CB_SPLIT_EQUAL:
        members_data = database.get_members(group_id)
        member_names = [m["name"] for m in members_data]
        extracted = ctx.get("extracted", {})
        total = float(extracted.get("total") or 0.0)
        shares = _equal_split(member_names, total)
        ctx["member_shares"] = shares
        await _show_save_confirm(update, group_id, user_id, ctx)
        return

    # ------------------------------------------------------------------
    # Split by item assignment — use button UI if items exist
    # ------------------------------------------------------------------
    if data == CB_SPLIT_ASSIGN:
        extracted = ctx.get("extracted", {})
        items = extracted.get("items") or []

        if items:
            await _ask_item_assign(update, group_id, user_id, ctx)
        else:
            # No items detected — fall back to text-based assignment
            members_data = database.get_members(group_id)
            member_names = [m["name"] for m in members_data]
            members_str = ", ".join(f'"{m.lower()}"' for m in member_names)
            prompt = (
                "No itemized list available. You can still type assignments:\n"
                f"  all {member_names[0].lower() if member_names else 'name'}\n"
                f"  all except karlos\n\n"
                f"Member names: {members_str}"
            )
            database.set_state(user_id, group_id, STATE_RECEIPT_AWAITING_ASSIGNMENT, ctx)
            await query.edit_message_text(prompt, reply_markup=_cancel_keyboard())
        return

    # ------------------------------------------------------------------
    # Save confirmed
    # ------------------------------------------------------------------
    if data == CB_SAVE:
        await _do_save(update, group_id, user_id, ctx)
        return

    # ------------------------------------------------------------------
    # Re-assign: go back to split selection
    # ------------------------------------------------------------------
    if data == CB_REASSIGN:
        await _ask_split(update, group_id, user_id, ctx)
        return

    # ------------------------------------------------------------------
    # Per-item assignment button: receipt:item:{idx}:{member_idx or "s"}
    # ------------------------------------------------------------------
    if data.startswith(CB_ASSIGN_ITEM_PREFIX):
        parts = data.split(":")
        # Expected: ["receipt", "item", idx_str, value_str]
        if len(parts) != 4:
            return
        try:
            item_idx = int(parts[2])
        except ValueError:
            return
        value_str = parts[3]
        value: int | str = "s" if value_str == "s" else int(value_str)

        assignments: dict = ctx.get("item_assignments", {})
        assignments[str(item_idx)] = value
        ctx["item_assignments"] = assignments
        database.set_state(user_id, group_id, STATE_RECEIPT_ITEM_ASSIGN, ctx)

        extracted = ctx.get("extracted", {})
        items = extracted.get("items") or []
        member_names = ctx.get("_member_names") or [m["name"] for m in database.get_members(group_id)]

        text = _build_item_assign_text(items, member_names, assignments)
        keyboard = _item_assign_keyboard(items, member_names, assignments)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    # ------------------------------------------------------------------
    # Submit item assignments
    # ------------------------------------------------------------------
    if data == CB_ASSIGN_SUBMIT:
        assignments = ctx.get("item_assignments", {})
        extracted = ctx.get("extracted", {})
        items = extracted.get("items") or []
        total = float(extracted.get("total") or 0.0)
        hst_pct = extracted.get("hst_pct")
        member_names = ctx.get("_member_names") or [m["name"] for m in database.get_members(group_id)]

        shares = _assignments_to_shares(items, assignments, member_names, total, hst_pct)
        ctx["member_shares"] = shares
        await _show_save_confirm(update, group_id, user_id, ctx)
        return
