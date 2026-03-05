"""Export flow — /export command generates a CSV and sends it via Telegram."""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Optional

import pytz
from telegram import Update
from telegram.ext import ContextTypes

import database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Month label helpers (duplicated locally to keep this module self-contained)
# ---------------------------------------------------------------------------

_MONTH_MAP: dict[str, str] = {
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


def _current_month_label(timezone: str = "America/Toronto") -> str:
    tz = pytz.timezone(timezone)
    return datetime.now(tz).strftime("%b %Y")


def _current_year(timezone: str = "America/Toronto") -> int:
    tz = pytz.timezone(timezone)
    return datetime.now(tz).year


def _parse_month_label(
    args: list[str],
    timezone: str = "America/Toronto",
) -> Optional[str]:
    """
    Parse optional month/year args.

    /export                 → current month
    /export jan             → Jan <current year>
    /export jan 2025        → Jan 2025
    """
    if not args:
        return _current_month_label(timezone)

    abbrev = _MONTH_MAP.get(args[0].lower().strip())
    if abbrev is None:
        return None

    if len(args) >= 2:
        try:
            year = int(args[1])
        except ValueError:
            return None
    else:
        year = _current_year(timezone)

    return f"{abbrev} {year}"


# ---------------------------------------------------------------------------
# CSV builder
# ---------------------------------------------------------------------------


def _build_csv(expenses: list[dict], member_names: list[str], currency: str) -> bytes:
    """
    Build a CSV in memory and return the raw bytes.

    Columns:
        Expense ID | Date | Description | Category | Subtotal |
        HST Amount | HST % | Tip Amount | Tip % | Total |
        Paid By | <Member1> | <Member2> | ... | Fixed | Notes
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    # Header
    member_cols = [f"{m} ({currency})" for m in member_names]
    writer.writerow([
        "Expense ID", "Date", "Description", "Category",
        f"Subtotal ({currency})", f"HST Amount ({currency})", "HST %",
        f"Tip Amount ({currency})", "Tip %", f"Total ({currency})",
        "Paid By", *member_cols, "Fixed?", "Notes",
    ])

    # Rows
    for exp in expenses:
        member_shares: dict = exp.get("member_shares") or {}
        share_vals: list[str] = [
            f"{float(member_shares.get(m, 0.0)):.2f}" for m in member_names
        ]
        writer.writerow([
            exp.get("expense_id", ""),
            exp.get("date", ""),
            exp.get("description", ""),
            exp.get("category", ""),
            f"{float(exp.get('subtotal', 0.0)):.2f}",
            f"{float(exp.get('hst_amount', 0.0)):.2f}",
            f"{float(exp.get('hst_pct', 0.0)):.2f}",
            f"{float(exp.get('tip_amount', 0.0)):.2f}",
            f"{float(exp.get('tip_pct', 0.0)):.2f}",
            f"{float(exp.get('total', 0.0)):.2f}",
            exp.get("paid_by", ""),
            *share_vals,
            "Yes" if exp.get("is_fixed") else "No",
            exp.get("notes", ""),
        ])

    # Totals row
    total_sum = sum(float(e.get("total", 0.0)) for e in expenses)
    member_totals: list[str] = []
    for m in member_names:
        m_total = sum(
            float((e.get("member_shares") or {}).get(m, 0.0)) for e in expenses
        )
        member_totals.append(f"{m_total:.2f}")

    writer.writerow([
        "TOTAL", "", "", "", "", "", "", "", "", f"{total_sum:.2f}",
        "", *member_totals, "", "",
    ])

    return buf.getvalue().encode("utf-8-sig")  # utf-8-sig adds BOM for Excel compat


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def handle_export_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Handle /export [month] [year] command.

    Fetches all expenses for the requested month and sends a CSV document.
    """
    if update.effective_chat is None or update.effective_message is None:
        return

    group_id = str(update.effective_chat.id)
    group = database.get_group(group_id)

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
            "Could not parse month. Examples:\n"
            "  /export            → current month\n"
            "  /export jan        → January (current year)\n"
            "  /export jan 2025   → January 2025"
        )
        return

    # Fetch all expenses including fixed
    expenses = database.get_expenses(group_id, month_label, include_fixed=True)
    if not expenses:
        await update.effective_message.reply_text(
            f"No expenses found for {month_label}."
        )
        return

    # Fetch member list for column headers
    members_data = database.get_members(group_id)
    member_names: list[str] = [m["name"] for m in members_data]

    # Build CSV bytes in memory (no temp files on disk)
    try:
        csv_bytes = _build_csv(expenses, member_names, currency)
    except Exception as exc:
        logger.exception("Error building CSV for /export: %s", exc)
        await update.effective_message.reply_text(
            "Error generating CSV. Please try again."
        )
        return

    # Build a nice filename, e.g. "expenses_Mar_2026.csv"
    filename = f"expenses_{month_label.replace(' ', '_')}.csv"

    # Send the file via Telegram
    await update.effective_message.reply_document(
        document=io.BytesIO(csv_bytes),
        filename=filename,
        caption=(
            f"📊 {month_label} — {len(expenses)} expense(s)\n"
            f"Total: ${sum(float(e.get('total', 0.0)) for e in expenses):.2f} {currency}"
        ),
    )
