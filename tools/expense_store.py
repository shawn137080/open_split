"""Local SQLite expense store — drop-in replacement for sheets_manager.py.

All public functions mirror the old sheets_manager API surface, but instead of
reading/writing Google Sheets they go directly to the SQLite database via the
database module.
"""

from __future__ import annotations

import database


# ---------------------------------------------------------------------------
# Public API (mirrors sheets_manager)
# ---------------------------------------------------------------------------


def get_or_create_month(
    group_id: str,
    month_label: str,
    members: list[str],
    fixed_expenses: list[dict],
) -> None:
    """
    Ensure the month is initialised: seed fixed expenses if this is the first
    call for this group+month. Safe to call multiple times.

    fixed_expenses: list of dicts from database.get_fixed_expenses() — they
    are already stored in the fixed_expenses table, so we use
    database.seed_fixed_expenses_for_month() which reads them directly.
    The `fixed_expenses` arg is accepted for API compatibility but unused here;
    the DB call is authoritative.
    """
    database.seed_fixed_expenses_for_month(group_id, month_label, members)


def append_expense(
    group_id: str,
    month_label: str,
    expense: dict,
) -> str:
    """
    Append one expense row to the DB. Returns the expense_id used.

    expense dict keys (same as the old sheets_manager API):
        expense_id (optional), date, description, category, subtotal,
        hst_amount, hst_pct, tip_amount, tip_pct, total, paid_by,
        member_shares, notes
    """
    return database.add_expense(group_id, month_label, expense)


def get_month_expenses(
    group_id: str,
    month_label: str,
    include_fixed: bool = True,
) -> list[dict]:
    """
    Return all expense rows for a group+month as list of dicts.

    Each dict has the same keys as the old sheets_manager response:
        expense_id, date, description, category, subtotal, hst_amount,
        hst_pct, tip_amount, tip_pct, total, paid_by, member_shares,
        notes, is_fixed
    Raises ValueError if no expenses exist for that month (same behaviour as
    the old tab-not-found error, so callers don't need to change).
    """
    rows = database.get_expenses(group_id, month_label, include_fixed=include_fixed)
    if not rows:
        raise ValueError(f"No expenses found for {month_label} in group {group_id}.")
    return rows


def get_next_expense_id(group_id: str, month_label: str) -> str:
    """Return the next available expense ID, e.g. 'EXP-001'."""
    return database.get_next_expense_id(group_id, month_label)


def delete_expense(group_id: str, expense_id: str) -> bool:
    """
    Delete the expense with the given ID. Returns True if deleted, False if not found.
    """
    return database.delete_expense(group_id, expense_id)


def update_expense(
    group_id: str,
    expense_id: str,
    updated_expense: dict,
) -> bool:
    """
    Update an existing expense row. Returns True if found and updated.
    """
    return database.update_expense(group_id, expense_id, updated_expense)


def has_expenses_for_month(group_id: str, month_label: str) -> bool:
    """Return True if any expenses exist for this group+month."""
    rows = database.get_expenses(group_id, month_label, include_fixed=True)
    return len(rows) > 0
