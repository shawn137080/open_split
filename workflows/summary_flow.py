"""Summary, history, and settlement flow (/summary, /history, /settle commands)."""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Optional

import pytz
from telegram import Update
from telegram.ext import ContextTypes

import database
from tools.balance_calculator import (
    calculate_balances,
    compute_settlement,
    format_balance_summary,
    format_category_breakdown,
)
from tools.expense_store import (
    append_expense,
    get_month_expenses,
    get_next_expense_id,
    get_or_create_month,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Month name → 3-letter abbreviation mapping
# ---------------------------------------------------------------------------

_MONTH_MAP: dict[str, str] = {
    "january": "Jan",
    "jan": "Jan",
    "february": "Feb",
    "feb": "Feb",
    "march": "Mar",
    "mar": "Mar",
    "april": "Apr",
    "apr": "Apr",
    "may": "May",
    "june": "Jun",
    "jun": "Jun",
    "july": "Jul",
    "jul": "Jul",
    "august": "Aug",
    "aug": "Aug",
    "september": "Sep",
    "sep": "Sep",
    "october": "Oct",
    "oct": "Oct",
    "november": "Nov",
    "nov": "Nov",
    "december": "Dec",
    "dec": "Dec",
}

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _current_month_label(timezone: str = "America/Toronto") -> str:
    """Return the current month label, e.g. 'Mar 2026'."""
    tz = pytz.timezone(timezone)
    return datetime.now(tz).strftime("%b %Y")


def _current_year(timezone: str = "America/Toronto") -> int:
    """Return the current year as an integer."""
    tz = pytz.timezone(timezone)
    return datetime.now(tz).year


def _today_date_str(timezone: str = "America/Toronto") -> str:
    """Return today's date as YYYY-MM-DD."""
    tz = pytz.timezone(timezone)
    return datetime.now(tz).strftime("%Y-%m-%d")


def _parse_month_label(
    args: list[str],
    timezone: str = "America/Toronto",
) -> Optional[str]:
    """
    Parse a month label from command args.

    - No args            → current month ("Mar 2026")
    - ["jan"]            → "Jan 2026"  (current year)
    - ["january"]        → "Jan 2026"
    - ["jan", "2025"]    → "Jan 2025"
    - Anything else / out of ±12 month range → None
    """
    if not args:
        return _current_month_label(timezone)

    month_token: str = args[0].lower().strip()
    abbrev: Optional[str] = _MONTH_MAP.get(month_token)
    if abbrev is None:
        return None

    if len(args) >= 2:
        year_token: str = args[1].strip()
        try:
            year: int = int(year_token)
        except ValueError:
            return None
    else:
        year = _current_year(timezone)

    # --- range guard: ±12 months from today ---
    tz = pytz.timezone(timezone)
    now = datetime.now(tz)
    _ABBREV_TO_NUM = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }
    month_num = _ABBREV_TO_NUM.get(abbrev)
    if month_num is None:
        return None
    diff = (year - now.year) * 12 + (month_num - now.month)
    if diff < -36 or diff > 12:
        return None  # out of allowed range

    return f"{abbrev} {year}"


def _format_expense_row(expense: dict, currency: str = "CAD") -> str:
    """
    Format a single expense as a mobile-friendly HTML card.

    Example:
        <b>#EXP-001</b> Whole Foods
        💰 $51.08 · Karlos · Mar 01 2026
          Karlos $25.54 · Partner $25.54
    """
    sym: str = "$"
    exp_id: str = expense.get("expense_id", "?")
    date_raw: str = expense.get("date", "?")
    description: str = html.escape(expense.get("description", "?"))
    total: float = float(expense.get("total", 0.0))
    paid_by: str = expense.get("paid_by", "?")
    is_fixed: bool = bool(expense.get("is_fixed", False))

    try:
        short_date = datetime.strptime(date_raw, "%Y-%m-%d").strftime("%b %d %Y")
    except Exception:
        short_date = date_raw

    tag = " [fixed]" if is_fixed else ""
    line1: str = f"<b>#{exp_id}</b> {description}{tag}"
    line2: str = f"💰 {sym}{total:.2f} · {paid_by} · {short_date}"

    member_shares: dict = expense.get("member_shares") or {}
    if member_shares:
        share_parts = [
            f"{member} {sym}{float(amount):.2f}"
            for member, amount in member_shares.items()
        ]
        line3: str = "  " + " · ".join(share_parts)
    else:
        line3 = ""

    result = f"{line1}\n{line2}"
    if line3:
        result += f"\n{line3}"
    return result


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


async def handle_summary_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /summary [month] command — show balance + settlement for a month."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id: str = str(update.effective_chat.id)

    group: Optional[dict] = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    timezone: str = group.get("timezone") or "America/Toronto"
    currency: str = group.get("currency") or "CAD"

    args: list[str] = list(context.args) if context.args else []

    # Delegate multi-month modes
    _MULTI_MODES = {"all", "q1", "q2", "q3", "q4"}
    if args and args[0].lower() in _MULTI_MODES:
        await handle_multisummary_command(update, context)
        return

    month_label: Optional[str] = _parse_month_label(args, timezone)

    if month_label is None:
        await update.effective_message.reply_text(
            "⚠️ Could not parse month.\n\n"
            "<b>Examples:</b>\n"
            "/summary — current month\n"
            "/summary feb — February\n"
            "/summary feb 2025 — Feb 2025\n"
            "/summary all — all-time overview\n"
            "/summary q1 — Q1 overview",
            parse_mode="HTML",
        )
        return

    members_data: list[dict] = database.get_members(group_id)
    member_names: list[str] = [m["name"] for m in members_data]

    # Seed fixed expenses for the month if not already done
    get_or_create_month(group_id, month_label, member_names, [])

    try:
        expenses: list[dict] = get_month_expenses(group_id, month_label)
    except ValueError:
        await update.effective_message.reply_text(
            f"No expenses recorded for {month_label} yet."
        )
        return
    except Exception as exc:
        logger.exception("Error fetching expenses for /summary: %s", exc)
        await update.effective_message.reply_text(
            "Error fetching data. Please try again."
        )
        return

    balances: list[dict] = calculate_balances(expenses, member_names)
    summary_text: str = format_balance_summary(balances, month_label, currency=currency)
    cat_breakdown: str = format_category_breakdown(expenses, currency=currency)
    if cat_breakdown:
        summary_text += "\n\n" + cat_breakdown
    await update.effective_message.reply_text(summary_text, parse_mode="HTML")


async def handle_history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /history [month] command — list all expenses for a month."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id: str = str(update.effective_chat.id)

    group: Optional[dict] = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    timezone: str = group.get("timezone") or "America/Toronto"
    currency: str = group.get("currency") or "CAD"

    args: list[str] = list(context.args) if context.args else []

    # If last arg is not a month name or year, treat it as a category filter
    category_filter: Optional[str] = None
    if args and args[-1].lower() not in _MONTH_MAP and not args[-1].isdigit():
        category_filter = args[-1]
        args = args[:-1]

    month_label: Optional[str] = _parse_month_label(args, timezone)

    if month_label is None:
        await update.effective_message.reply_text(
            "⚠️ Could not parse month.\n\n"
            "<b>Examples:</b>\n"
            "/history — current month\n"
            "/history jan — January\n"
            "/history jan 2025 — Jan 2025\n"
            "/history feb dining — filter by category",
            parse_mode="HTML",
        )
        return

    members_data: list[dict] = database.get_members(group_id)

    # Seed fixed expenses for the month if not already done
    get_or_create_month(group_id, month_label, [m["name"] for m in members_data], [])

    try:
        expenses: list[dict] = get_month_expenses(group_id, month_label)
    except ValueError:
        await update.effective_message.reply_text(
            f"No expenses found for {month_label}."
        )
        return
    except Exception as exc:
        logger.exception("Error fetching expenses for /history: %s", exc)
        await update.effective_message.reply_text(
            "Error fetching data. Please try again."
        )
        return

    # Apply category filter if requested
    if category_filter:
        expenses = [
            e for e in expenses
            if (e.get("category") or "Other").lower() == category_filter.lower()
        ]
        if not expenses:
            await update.effective_message.reply_text(
                f"No <b>{html.escape(category_filter)}</b> expenses in {month_label}.",
                parse_mode="HTML",
            )
            return

    header = f"📋 <b>{month_label}</b>"
    if category_filter:
        header += f" — {html.escape(category_filter)}"
    header += f" — {len(expenses)} expense(s)\n"
    lines: list[str] = [header]
    for expense in expenses:
        lines.append(_format_expense_row(expense, currency=currency))

    # Telegram message length limit is ~4096 chars; chunk if needed.
    full_text: str = "\n\n".join(lines)
    if len(full_text) <= 4000:
        await update.effective_message.reply_text(full_text, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(lines[0], parse_mode="HTML")
        chunk: list[str] = []
        chunk_len: int = 0
        for row_text in lines[1:]:
            if chunk_len + len(row_text) + 1 > 3800:
                await update.effective_message.reply_text(
                    "\n\n".join(chunk), parse_mode="HTML"
                )
                chunk = []
                chunk_len = 0
            chunk.append(row_text)
            chunk_len += len(row_text) + 1
        if chunk:
            await update.effective_message.reply_text(
                "\n\n".join(chunk), parse_mode="HTML"
            )


async def handle_settle_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /settle [name] [amount] command — record a settlement payment."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id: str = str(update.effective_chat.id)

    group: Optional[dict] = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    timezone: str = group.get("timezone") or "America/Toronto"
    currency: str = group.get("currency") or "CAD"

    args: list[str] = list(context.args) if context.args else []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "⚠️ <b>Usage:</b> /settle &lt;name&gt; &lt;amount&gt;\n"
            "<i>Example: /settle mike 102.50</i>",
            parse_mode="HTML",
        )
        return

    debtor_raw: str = args[0]
    amount_raw: str = args[1]

    try:
        settle_amount: float = float(amount_raw)
    except ValueError:
        await update.effective_message.reply_text(
            f"⚠️ Invalid amount: <code>{html.escape(amount_raw)}</code>\n"
            "Please provide a numeric value.",
            parse_mode="HTML",
        )
        return

    if settle_amount <= 0:
        await update.effective_message.reply_text(
            f"⚠️ Amount must be greater than 0.",
            parse_mode="HTML",
        )
        return

    members_data: list[dict] = database.get_members(group_id)
    member_names: list[str] = [m["name"] for m in members_data]

    debtor_canon: Optional[str] = next(
        (m for m in member_names if m.lower() == debtor_raw.lower()),
        None,
    )
    if debtor_canon is None:
        member_list: str = ", ".join(member_names) if member_names else "(none)"
        await update.effective_message.reply_text(
            f"⚠️ Unknown member: <b>{html.escape(debtor_raw)}</b>\n"
            f"Known: {html.escape(member_list)}",
            parse_mode="HTML",
        )
        return

    month_label: str = _current_month_label(timezone)
    date_str: str = _today_date_str(timezone)

    try:
        get_or_create_month(
            group_id=group_id,
            month_label=month_label,
            members=member_names,
            fixed_expenses=[],
        )
        expenses: list[dict] = database.get_expenses(group_id, month_label)
    except Exception as exc:
        logger.exception("Error fetching expenses for /settle: %s", exc)
        await update.effective_message.reply_text(
            "Error fetching data. Please try again."
        )
        return

    balances: list[dict] = calculate_balances(expenses, member_names)

    debtor_balance_entry: Optional[dict] = next(
        (b for b in balances if b["member"] == debtor_canon), None
    )
    if debtor_balance_entry is None:
        await update.effective_message.reply_text(
            f"⚠️ Could not find balance for <b>{html.escape(debtor_canon)}</b>.",
            parse_mode="HTML",
        )
        return

    if debtor_balance_entry["net_balance"] >= 0:
        net = debtor_balance_entry["net_balance"]
        await update.effective_message.reply_text(
            f"ℹ️ <b>{html.escape(debtor_canon)}</b> doesn't owe anyone right now "
            f"(net: +${net:.2f}).",
            parse_mode="HTML",
        )
        return

    transfers: list[dict] = compute_settlement(balances)

    debtor_transfer: Optional[dict] = next(
        (t for t in transfers if t["from"] == debtor_canon), None
    )
    if debtor_transfer is None:
        await update.effective_message.reply_text(
            f"⚠️ Could not determine who <b>{html.escape(debtor_canon)}</b> owes.\n"
            "Check the current balance with /summary.",
            parse_mode="HTML",
        )
        return

    creditor_canon: str = debtor_transfer["to"]

    member_shares: dict[str, float] = {m: 0.0 for m in member_names}
    member_shares[creditor_canon] = settle_amount
    member_shares[debtor_canon] = 0.0

    try:
        expense_id: str = get_next_expense_id(group_id, month_label)

        settlement_row: dict = {
            "expense_id": expense_id,
            "date": date_str,
            "description": f"Settlement: {debtor_canon} pays {creditor_canon}",
            "category": "Settlement",
            "subtotal": settle_amount,
            "hst_amount": 0.0,
            "hst_pct": 0.0,
            "tip_amount": 0.0,
            "tip_pct": 0.0,
            "total": settle_amount,
            "paid_by": debtor_canon,
            "member_shares": member_shares,
            "notes": "",
        }

        append_expense(group_id=group_id, month_label=month_label, expense=settlement_row)

        updated_expenses: list[dict] = database.get_expenses(group_id, month_label)
        updated_balances: list[dict] = calculate_balances(updated_expenses, member_names)

    except Exception as exc:
        logger.exception("Error recording settlement: %s", exc)
        await update.effective_message.reply_text(
            "Error recording settlement. Please try again."
        )
        return

    sym: str = "$"
    confirm_line: str = (
        f"✅ <b>Settlement recorded</b>\n"
        f"  {html.escape(debtor_canon)} → {html.escape(creditor_canon)} "
        f"{sym}{settle_amount:.2f}\n\n"
    )
    updated_summary: str = format_balance_summary(
        updated_balances, month_label, currency=currency
    )
    await update.effective_message.reply_text(
        confirm_line + updated_summary, parse_mode="HTML"
    )


async def handle_last_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /last [month] — show the most recently added non-fixed expense."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id: str = str(update.effective_chat.id)

    group: Optional[dict] = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    timezone: str = group.get("timezone") or "America/Toronto"
    currency: str = group.get("currency") or "CAD"

    args: list[str] = list(context.args) if context.args else []
    month_label: Optional[str] = _parse_month_label(args, timezone)
    if month_label is None:
        await update.effective_message.reply_text(
            "⚠️ Could not parse month.\n<i>Example: /last feb</i>",
            parse_mode="HTML",
        )
        return

    try:
        expenses: list[dict] = get_month_expenses(group_id, month_label)
    except ValueError:
        await update.effective_message.reply_text(
            f"No expenses recorded for {month_label} yet."
        )
        return
    except Exception as exc:
        logger.exception("Error fetching expenses for /last: %s", exc)
        await update.effective_message.reply_text("Error fetching data. Please try again.")
        return

    non_fixed = [e for e in expenses if not e.get("is_fixed")]
    if not non_fixed:
        await update.effective_message.reply_text(
            f"No expenses recorded for {month_label} yet."
        )
        return

    last = non_fixed[-1]
    await update.effective_message.reply_text(
        f"🕐 <b>Last expense ({month_label})</b>\n\n"
        f"{_format_expense_row(last, currency=currency)}",
        parse_mode="HTML",
    )


async def handle_delete_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /delete <expense_id> — delete an expense from the DB."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id: str = str(update.effective_chat.id)

    group: Optional[dict] = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    args: list[str] = list(context.args) if context.args else []
    if not args:
        await update.effective_message.reply_text(
            "⚠️ <b>Usage:</b> /delete &lt;expense_id&gt;\n"
            "<i>Example: /delete EXP-003</i>\n\n"
            "Use /history to see IDs.",
            parse_mode="HTML",
        )
        return

    expense_id: str = args[0].upper().strip()

    timezone: str = group.get("timezone") or "America/Toronto"
    currency: str = group.get("currency") or "CAD"
    month_label: str = _current_month_label(timezone)

    try:
        expenses: list[dict] = database.get_expenses(group_id, month_label)
    except Exception as exc:
        logger.exception("Error fetching expenses for /delete: %s", exc)
        await update.effective_message.reply_text("Error fetching data. Please try again.")
        return

    target: Optional[dict] = next(
        (e for e in expenses if e["expense_id"] == expense_id), None
    )
    if target is None:
        await update.effective_message.reply_text(
            f"⚠️ <b>{html.escape(expense_id)}</b> not found in {html.escape(month_label)}.\n"
            "Use /history to list expenses.",
            parse_mode="HTML",
        )
        return

    try:
        deleted: bool = database.delete_expense(group_id, expense_id)
        if not deleted:
            await update.effective_message.reply_text(
                f"⚠️ Could not delete <b>{html.escape(expense_id)}</b>. Please try again.",
                parse_mode="HTML",
            )
            return
    except Exception as exc:
        logger.exception("Error deleting expense %s: %s", expense_id, exc)
        await update.effective_message.reply_text("Error deleting expense. Please try again.")
        return

    sym: str = "$"
    total: float = float(target.get("total", 0.0))
    desc: str = html.escape(target.get("description", "?"))
    await update.effective_message.reply_text(
        f"🗑 <b>Deleted</b> #{html.escape(expense_id)}\n"
        f"  {desc} · {sym}{total:.2f}\n\n"
        "Use /summary to see updated balance.",
        parse_mode="HTML",
    )


async def handle_edit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /edit <expense_id> <field> <value> — edit an expense field.

    Fields: amount, desc, category, payer, date
    Example: /edit EXP-003 amount 55.00
             /edit EXP-003 desc New Name
             /edit EXP-003 category Dining
             /edit EXP-003 payer Mike
             /edit EXP-003 date 2026-03-15
    """
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id: str = str(update.effective_chat.id)
    group: Optional[dict] = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    timezone: str = group.get("timezone") or "America/Toronto"
    currency: str = group.get("currency") or "CAD"
    args: list[str] = list(context.args) if context.args else []

    if not args:
        await update.effective_message.reply_text(
            "⚠️ <b>Usage:</b> /edit &lt;id&gt; &lt;field&gt; &lt;value&gt;\n\n"
            "<b>Fields:</b>\n"
            "  <code>amount</code>   — e.g. /edit EXP-003 amount 55.00\n"
            "  <code>desc</code>     — e.g. /edit EXP-003 desc Groceries run\n"
            "  <code>category</code> — e.g. /edit EXP-003 category Dining\n"
            "  <code>payer</code>    — e.g. /edit EXP-003 payer Mike\n"
            "  <code>date</code>     — e.g. /edit EXP-003 date 2026-03-15\n\n"
            "Use /history to see expense IDs.",
            parse_mode="HTML",
        )
        return

    expense_id: str = args[0].upper().strip()

    # Search for expense across all months (current first)
    month_label: str = _current_month_label(timezone)
    target: Optional[dict] = None
    found_month: str = month_label

    all_month_expenses = database.get_expenses(group_id, month_label)
    target = next((e for e in all_month_expenses if e["expense_id"] == expense_id), None)

    if target is None:
        all_months = database.get_all_months_summary(group_id)
        for m in all_months:
            ml: str = m["month_label"]
            if ml == month_label:
                continue
            exps = database.get_expenses(group_id, ml)
            target = next((e for e in exps if e["expense_id"] == expense_id), None)
            if target:
                found_month = ml
                break

    if target is None:
        await update.effective_message.reply_text(
            f"⚠️ <b>{html.escape(expense_id)}</b> not found.\n"
            "Use /history to list expense IDs.",
            parse_mode="HTML",
        )
        return

    # Show current expense if no field/value provided
    if len(args) < 3:
        await update.effective_message.reply_text(
            f"📝 <b>{html.escape(expense_id)}</b> ({html.escape(found_month)})\n\n"
            f"{_format_expense_row(target, currency=currency)}\n\n"
            f"To edit: /edit {expense_id} &lt;field&gt; &lt;value&gt;",
            parse_mode="HTML",
        )
        return

    field: str = args[1].lower()
    value: str = " ".join(args[2:])
    updated: dict = dict(target)

    if field == "amount":
        try:
            new_amount = float(value.replace("$", "").replace(",", ""))
        except ValueError:
            await update.effective_message.reply_text(
                "⚠️ Invalid amount. Example: /edit EXP-003 amount 55.00"
            )
            return
        updated["total"] = new_amount
        updated["subtotal"] = new_amount
        updated["hst_amount"] = 0.0
        updated["tip_amount"] = 0.0

    elif field in ("desc", "description"):
        updated["description"] = value

    elif field == "category":
        updated["category"] = value.title()

    elif field in ("payer", "paid_by"):
        members_data: list[dict] = database.get_members(group_id)
        canon: Optional[str] = next(
            (m["name"] for m in members_data if m["name"].lower() == value.lower()), None
        )
        if canon is None:
            known = ", ".join(m["name"] for m in members_data)
            await update.effective_message.reply_text(
                f"⚠️ Unknown member: <b>{html.escape(value)}</b>\nKnown: {html.escape(known)}",
                parse_mode="HTML",
            )
            return
        updated["paid_by"] = canon

    elif field == "date":
        try:
            datetime.strptime(value, "%Y-%m-%d")
            updated["date"] = value
        except ValueError:
            await update.effective_message.reply_text(
                "⚠️ Invalid date format. Use YYYY-MM-DD, e.g. 2026-03-15"
            )
            return

    else:
        await update.effective_message.reply_text(
            f"⚠️ Unknown field: <b>{html.escape(field)}</b>\n"
            "Valid fields: amount, desc, category, payer, date",
            parse_mode="HTML",
        )
        return

    ok: bool = database.update_expense(group_id, expense_id, updated)
    if not ok:
        await update.effective_message.reply_text("Error updating expense. Please try again.")
        return

    # Reload to get fresh row
    fresh_expenses = database.get_expenses(group_id, found_month)
    fresh = next((e for e in fresh_expenses if e["expense_id"] == expense_id), updated)
    await update.effective_message.reply_text(
        f"✅ <b>{html.escape(expense_id)}</b> updated\n\n"
        f"{_format_expense_row(fresh, currency=currency)}",
        parse_mode="HTML",
    )


async def handle_multisummary_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /summary all or /summary q1/q2/q3/q4 — multi-month overview."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id: str = str(update.effective_chat.id)
    group: Optional[dict] = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text(
            "Please run /start first to set up your household."
        )
        return

    currency: str = group.get("currency") or "CAD"
    sym: str = "$"
    args: list[str] = list(context.args) if context.args else []
    mode: str = args[0].lower() if args else ""

    all_months = database.get_all_months_summary(group_id)
    if not all_months:
        await update.effective_message.reply_text("No expenses recorded yet.")
        return

    # Determine which months to include
    _QUARTER_MONTHS: dict[str, list[str]] = {
        "q1": ["Jan", "Feb", "Mar"],
        "q2": ["Apr", "May", "Jun"],
        "q3": ["Jul", "Aug", "Sep"],
        "q4": ["Oct", "Nov", "Dec"],
    }

    if mode == "all":
        filtered = all_months
        title = "All Time"
    elif mode in _QUARTER_MONTHS:
        abbrevs = _QUARTER_MONTHS[mode]
        filtered = [m for m in all_months if m["month_label"][:3] in abbrevs]
        title = mode.upper()
        if not filtered:
            await update.effective_message.reply_text(f"No expenses found for {title}.")
            return
    else:
        # Should not reach here — caller guards this
        return

    # Build month-by-month table and aggregate category totals
    lines: list[str] = [f"📊 <b>{html.escape(title)} — Overview</b>\n"]
    grand_total: float = 0.0
    grand_count: int = 0
    cat_totals: dict[str, float] = {}

    for m in reversed(filtered):  # oldest first
        ml = m["month_label"]
        total = float(m.get("total_amount") or 0.0)
        count = int(m.get("expense_count") or 0)
        grand_total += total
        grand_count += count
        lines.append(f"  {ml}  {sym}{total:.0f}  ({count} exp)")
        # Aggregate categories
        try:
            exps = database.get_expenses(group_id, ml)
            for e in exps:
                cat = (e.get("category") or "Other").strip()
                if cat == "Settlement":
                    continue
                cat_totals[cat] = cat_totals.get(cat, 0.0) + float(e.get("total", 0.0))
        except Exception:
            pass

    lines.append(f"\n<b>Total</b>  {sym}{grand_total:.2f}  ({grand_count} expenses)")

    if cat_totals:
        sorted_cats = sorted(cat_totals.items(), key=lambda x: -x[1])
        parts = [f"{html.escape(c)} {sym}{v:.0f}" for c, v in sorted_cats]
        lines.append("\n📂 <b>By Category</b>\n  " + " · ".join(parts))

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
