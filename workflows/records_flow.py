"""Records flow — /records command.

Shows an overview of all months with expenses.
Tap a month → detailed expense list for that month.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
from tools.expense_store import get_month_expenses

logger = logging.getLogger(__name__)

CB_RECORDS_MONTH_PFX = "rec:month:"   # rec:month:Mar 2026
CB_RECORDS_BACK      = "rec:back"

RECORDS_CALLBACK_PREFIXES = ("rec:",)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _months_text(months: list[dict], currency: str) -> str:
    """Build the month-overview message."""
    if not months:
        return "📂 No expenses recorded yet.\n\nUse /add or send a receipt photo to get started."
    lines = ["📂 <b>All Records</b>\n\nTap a month to view details:\n"]
    for m in months:
        total = float(m.get("total_amount") or 0.0)
        count = int(m.get("expense_count") or 0)
        lines.append(f"  • {m['month_label']} — ${total:.2f} ({count} item{'s' if count != 1 else ''})")
    return "\n".join(lines)


def _months_keyboard(months: list[dict]) -> InlineKeyboardMarkup:
    """One button per month, 2 per row."""
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for m in months:
        label = m["month_label"]
        total = float(m.get("total_amount") or 0.0)
        btn = InlineKeyboardButton(
            f"{label}  ${total:.0f}",
            callback_data=f"{CB_RECORDS_MONTH_PFX}{label}",
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def _month_detail_text(month_label: str, expenses: list[dict], currency: str) -> str:
    """Format the detailed expense list for a single month."""
    if not expenses:
        return f"📋 <b>{html.escape(month_label)}</b>\n\nNo expenses found."

    total = sum(float(e.get("total", 0)) for e in expenses)
    lines = [f"📋 <b>{html.escape(month_label)}</b> — Total: ${total:.2f}\n"]

    # Category breakdown
    cat_totals: dict[str, float] = {}
    for e in expenses:
        cat = (e.get("category") or "Other").strip()
        if cat == "Settlement":
            continue
        cat_totals[cat] = cat_totals.get(cat, 0.0) + float(e.get("total", 0.0))
    if cat_totals:
        sorted_cats = sorted(cat_totals.items(), key=lambda x: -x[1])
        cat_line = " · ".join(f"{html.escape(c)} ${v:.0f}" for c, v in sorted_cats)
        lines.append(f"📂 {cat_line}\n")

    for e in expenses:
        exp_id = e.get("expense_id", "?")
        desc = html.escape(e.get("description", "?"))
        cat = html.escape(e.get("category") or "—")
        amt = float(e.get("total", 0))
        paid_by = html.escape(e.get("paid_by", "?"))
        is_fixed = e.get("is_fixed", False)
        tag = " [fixed]" if is_fixed else ""
        shares: dict = e.get("member_shares") or {}
        share_str = " · ".join(f"{k} ${float(v):.2f}" for k, v in shares.items())
        lines.append(
            f"<b>#{html.escape(exp_id)}</b> {desc}{tag} [{cat}] ${amt:.2f} <i>{paid_by}</i>\n"
            f"  ↳ {share_str}"
        )

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


async def handle_records_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle /records — show month overview."""
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id = str(update.effective_chat.id)
    group = database.get_group(group_id)
    if group is None:
        await update.effective_message.reply_text("Please run /start first.")
        return

    currency = group.get("currency") or "CAD"
    months = database.get_all_months_summary(group_id)

    text = _months_text(months, currency)
    if not months:
        await update.effective_message.reply_text(text, parse_mode="HTML")
        return

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_months_keyboard(months),
    )


async def handle_records_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle inline button taps for /records."""
    query = update.callback_query
    if query is None or update.effective_chat is None:
        return
    await query.answer()

    group_id = str(update.effective_chat.id)
    data: str = query.data or ""
    group = database.get_group(group_id)
    currency = (group.get("currency") or "CAD") if group else "CAD"

    # ── back to overview ─────────────────────────────────────────────────────
    if data == CB_RECORDS_BACK:
        months = database.get_all_months_summary(group_id)
        text = _months_text(months, currency)
        if not months:
            await query.edit_message_text(text, parse_mode="HTML")
        else:
            await query.edit_message_text(
                text, parse_mode="HTML", reply_markup=_months_keyboard(months)
            )
        return

    # ── month selected ────────────────────────────────────────────────────────
    if data.startswith(CB_RECORDS_MONTH_PFX):
        month_label = data[len(CB_RECORDS_MONTH_PFX):]
        try:
            expenses = get_month_expenses(group_id, month_label, include_fixed=True)
        except ValueError:
            expenses = []

        text = _month_detail_text(month_label, expenses, currency)
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("← Back to months", callback_data=CB_RECORDS_BACK)
        ]])
        # Telegram message limit is 4096 chars — trim if needed
        if len(text) > 4000:
            text = text[:3990] + "\n…(truncated)"
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_kb)
        return
