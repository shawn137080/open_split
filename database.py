"""SQLite database setup and helpers for auto_split."""

from __future__ import annotations


import json
import os
import sqlite3

# Resolve database path the same way config.py does, but without importing
# config so that this module can be tested in isolation (config raises
# EnvironmentError when TELEGRAM_TOKEN / GEMINI_API_KEY are absent).
DATABASE_PATH: str = os.path.abspath(os.getenv("DATABASE_PATH", "auto_split.db"))

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Open a connection with Row factory and WAL journal mode enabled."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Convert a sqlite3.Row to a plain dict, or return None."""
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't exist."""
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id        TEXT PRIMARY KEY,
                household_name  TEXT NOT NULL,
                admin_user_id   TEXT NOT NULL,
                timezone        TEXT NOT NULL DEFAULT 'America/Toronto',
                currency        TEXT NOT NULL DEFAULT 'CAD',
                default_tax_pct REAL NOT NULL DEFAULT 0.0,
                is_pro          INTEGER NOT NULL DEFAULT 0,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS members (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id          TEXT NOT NULL,
                name              TEXT NOT NULL,
                telegram_user_id  TEXT,
                is_admin          INTEGER NOT NULL DEFAULT 0,
                joined_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (group_id) REFERENCES groups(group_id)
            );

            CREATE TABLE IF NOT EXISTS fixed_expenses (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id           TEXT NOT NULL,
                description        TEXT NOT NULL,
                amount             REAL NOT NULL,
                paid_by_member_id  INTEGER NOT NULL,
                split_type         TEXT NOT NULL DEFAULT 'equal',
                active             INTEGER NOT NULL DEFAULT 1,
                start_month        TEXT,
                end_month          TEXT,
                FOREIGN KEY (group_id)          REFERENCES groups(group_id),
                FOREIGN KEY (paid_by_member_id) REFERENCES members(id)
            );

            CREATE TABLE IF NOT EXISTS fixed_expense_exceptions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                fixed_expense_id    INTEGER NOT NULL,
                month_label         TEXT NOT NULL,
                UNIQUE (fixed_expense_id, month_label),
                FOREIGN KEY (fixed_expense_id) REFERENCES fixed_expenses(id)
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id        TEXT NOT NULL,
                expense_id      TEXT NOT NULL,
                month_label     TEXT NOT NULL,
                date            TEXT NOT NULL DEFAULT '',
                description     TEXT NOT NULL DEFAULT '',
                category        TEXT NOT NULL DEFAULT '',
                subtotal        REAL NOT NULL DEFAULT 0.0,
                hst_amount      REAL NOT NULL DEFAULT 0.0,
                hst_pct         REAL NOT NULL DEFAULT 0.0,
                tip_amount      REAL NOT NULL DEFAULT 0.0,
                tip_pct         REAL NOT NULL DEFAULT 0.0,
                total           REAL NOT NULL DEFAULT 0.0,
                paid_by         TEXT NOT NULL DEFAULT '',
                member_shares   TEXT NOT NULL DEFAULT '{}',
                notes           TEXT NOT NULL DEFAULT '',
                is_fixed        INTEGER NOT NULL DEFAULT 0,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (group_id, expense_id, month_label),
                FOREIGN KEY (group_id) REFERENCES groups(group_id)
            );

            CREATE TABLE IF NOT EXISTS conversation_state (
                user_id       TEXT NOT NULL,
                group_id      TEXT NOT NULL,
                state         TEXT NOT NULL,
                context_json  TEXT,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, group_id)
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL,
                group_id      TEXT NOT NULL,
                message       TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()

        # --- safe migrations for existing DBs ---
        for col, col_def in [
            ("start_month",    "TEXT"),
            ("end_month",      "TEXT"),
            ("default_tax_pct", "REAL NOT NULL DEFAULT 0.0"),
            ("is_pro",         "INTEGER NOT NULL DEFAULT 0"),
        ]:
            table = "groups" if col in ("default_tax_pct", "is_pro") else "fixed_expenses"
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                conn.commit()
            except Exception:
                pass  # column already exists — safe to ignore

        conn.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                group_id      TEXT NOT NULL,
                category      TEXT NOT NULL,
                amount        REAL NOT NULL,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, category),
                FOREIGN KEY (group_id) REFERENCES groups(group_id)
            )
        """)
        conn.commit()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ocr_usage (
                group_id TEXT NOT NULL,
                month TEXT NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (group_id, month)
            )
        """)
        conn.commit()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS fixed_expense_exceptions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                fixed_expense_id    INTEGER NOT NULL,
                month_label         TEXT NOT NULL,
                UNIQUE (fixed_expense_id, month_label),
                FOREIGN KEY (fixed_expense_id) REFERENCES fixed_expenses(id)
            )
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

def create_group(
    group_id: str,
    household_name: str,
    admin_user_id: str,
    timezone: str = "America/Toronto",
    currency: str = "CAD",
    default_tax_pct: float = 0.0,
) -> None:
    """Create a new household group. Raises ValueError if group already exists."""
    conn = _connect()
    try:
        try:
            conn.execute(
                """
                INSERT INTO groups
                    (group_id, household_name, admin_user_id, timezone, currency, default_tax_pct)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, household_name, admin_user_id, timezone, currency, default_tax_pct),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError(f"Group '{group_id}' already exists.")
    finally:
        conn.close()


def get_group(group_id: str) -> dict | None:
    """Return group dict or None if not found."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        result = _row_to_dict(row)
    finally:
        conn.close()
    return result


def update_group(
    group_id: str,
    household_name: str | None = None,
    timezone: str | None = None,
    currency: str | None = None,
    default_tax_pct: float | None = None,
) -> None:
    """Patch any subset of group fields. Only non-None values are updated."""
    conn = _connect()
    try:
        updates = []
        values = []
        if household_name is not None:
            updates.append("household_name = ?")
            values.append(household_name)
        if timezone is not None:
            updates.append("timezone = ?")
            values.append(timezone)
        if currency is not None:
            updates.append("currency = ?")
            values.append(currency)
        if default_tax_pct is not None:
            updates.append("default_tax_pct = ?")
            values.append(default_tax_pct)
        if not updates:
            return
        values.append(group_id)
        conn.execute(
            f"UPDATE groups SET {', '.join(updates)} WHERE group_id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def enable_pro(group_id: str, is_pro: int = 1) -> None:
    """Enable or disable Pro mode for a group."""
    conn = _connect()
    try:
        conn.execute("UPDATE groups SET is_pro = ? WHERE group_id = ?", (is_pro, group_id))
        conn.commit()
    finally:
        conn.close()


def is_group_pro(group_id: str) -> bool:
    """Check if a group has Pro mode enabled."""
    conn = _connect()
    try:
        row = conn.execute("SELECT is_pro FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        result = bool(row and row["is_pro"])
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

def set_budget(group_id: str, category: str, amount: float) -> None:
    """Set or update a monthly budget limit for a category."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO budgets (group_id, category, amount)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, category) DO UPDATE SET
                amount = excluded.amount,
                updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, category, amount),
        )
        conn.commit()
    finally:
        conn.close()


def get_budget(group_id: str, category: str) -> float | None:
    """Get the budget for a specific category, or None if not set."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT amount FROM budgets WHERE group_id = ? AND category = ?",
            (group_id, category),
        ).fetchone()
        result = float(row["amount"]) if row else None
    finally:
        conn.close()
    return result


def get_all_budgets(group_id: str) -> list[dict]:
    """Get all budgets for a group."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM budgets WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        result = []
        for r in rows:
            if r:
                d = _row_to_dict(r)
                if d is not None:
                    result.append(d)
    finally:
        conn.close()
    return result


def group_exists(group_id: str) -> bool:
    """Return True if group is already set up."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        result: bool = row is not None
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def add_member(
    group_id: str,
    name: str,
    telegram_user_id: str | None = None,
    is_admin: bool = False,
) -> int:
    """Insert a member and return their id."""
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO members (group_id, name, telegram_user_id, is_admin)
            VALUES (?, ?, ?, ?)
            """,
            (group_id, name, telegram_user_id, 1 if is_admin else 0),
        )
        conn.commit()
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("INSERT into members did not produce a row id")
        member_id: int = row_id
    finally:
        conn.close()
    return member_id


def get_members(group_id: str) -> list[dict]:
    """Return all members for a group as list of dicts."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM members WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        result: list[dict] = [dict(row) for row in rows]
    finally:
        conn.close()
    return result


def get_member_by_name(group_id: str, name: str) -> dict | None:
    """Case-insensitive name lookup. Return member dict or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM members WHERE group_id = ? AND LOWER(name) = LOWER(?)",
            (group_id, name),
        ).fetchone()
        result = _row_to_dict(row)
    finally:
        conn.close()
    return result


def get_member_by_telegram_id(group_id: str, telegram_user_id: str) -> dict | None:
    """Return member dict or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM members WHERE group_id = ? AND telegram_user_id = ?",
            (group_id, telegram_user_id),
        ).fetchone()
        result = _row_to_dict(row)
    finally:
        conn.close()
    return result


def remove_member(member_id: int) -> None:
    """Delete a member by id. Deactivates their fixed expenses first to avoid FK violations."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE fixed_expenses SET active = 0 WHERE paid_by_member_id = ?",
            (member_id,),
        )
        conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixed Expenses
# ---------------------------------------------------------------------------

def add_fixed_expense(
    group_id: str,
    description: str,
    amount: float,
    paid_by_member_id: int,
    split_type: str = "equal",
    start_month: str | None = None,
) -> int:
    """Insert a fixed expense and return its id."""
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO fixed_expenses
                (group_id, description, amount, paid_by_member_id, split_type, start_month)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (group_id, description, amount, paid_by_member_id, split_type, start_month),
        )
        conn.commit()
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("INSERT into fixed_expenses did not produce a row id")
        expense_id: int = row_id
    finally:
        conn.close()
    return expense_id


def get_fixed_expenses(group_id: str, active_only: bool = True) -> list[dict]:
    """Return fixed expenses for a group. If active_only, only active ones."""
    conn = _connect()
    try:
        if active_only:
            rows = conn.execute(
                """
                SELECT fe.*, m.name AS paid_by_name
                FROM fixed_expenses fe
                JOIN members m ON fe.paid_by_member_id = m.id
                WHERE fe.group_id = ? AND fe.active = 1
                ORDER BY fe.id
                """,
                (group_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT fe.*, m.name AS paid_by_name
                FROM fixed_expenses fe
                JOIN members m ON fe.paid_by_member_id = m.id
                WHERE fe.group_id = ?
                ORDER BY fe.id
                """,
                (group_id,),
            ).fetchall()
        result: list[dict] = [dict(row) for row in rows]
    finally:
        conn.close()
    return result


def update_fixed_expense(
    expense_id: int,
    description: str | None = None,
    amount: float | None = None,
    paid_by_member_id: int | None = None,
    split_type: str | None = None,
) -> bool:
    """Patch any subset of fixed expense fields. Returns True if found and updated."""
    updates = []
    values = []
    if description is not None:
        updates.append("description = ?")
        values.append(description)
    if amount is not None:
        updates.append("amount = ?")
        values.append(amount)
    if paid_by_member_id is not None:
        updates.append("paid_by_member_id = ?")
        values.append(paid_by_member_id)
    if split_type is not None:
        updates.append("split_type = ?")
        values.append(split_type)
    if not updates:
        return False
    values.append(expense_id)
    conn = _connect()
    try:
        cursor = conn.execute(
            f"UPDATE fixed_expenses SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        conn.commit()
        updated: bool = cursor.rowcount > 0
    finally:
        conn.close()
    return updated


def deactivate_fixed_expense(expense_id: int, end_month: str | None = None) -> None:
    """Set end_month to stop future seeding (or active=0 for permanent removal)."""
    conn = _connect()
    try:
        if end_month:
            conn.execute(
                "UPDATE fixed_expenses SET end_month = ? WHERE id = ?",
                (end_month, expense_id),
            )
        else:
            conn.execute(
                "UPDATE fixed_expenses SET active = 0 WHERE id = ?",
                (expense_id,),
            )
        conn.commit()
    finally:
        conn.close()


def add_fixed_expense_exception(fixed_expense_id: int, month_label: str) -> None:
    """Skip a fixed expense for one specific month. Idempotent."""
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO fixed_expense_exceptions (fixed_expense_id, month_label)
            VALUES (?, ?)
            """,
            (fixed_expense_id, month_label),
        )
        conn.commit()
    finally:
        conn.close()


def remove_fixed_expense_exception(fixed_expense_id: int, month_label: str) -> None:
    """Re-enable a fixed expense for a month that was previously skipped."""
    conn = _connect()
    try:
        conn.execute(
            """
            DELETE FROM fixed_expense_exceptions
            WHERE fixed_expense_id = ? AND month_label = ?
            """,
            (fixed_expense_id, month_label),
        )
        conn.commit()
    finally:
        conn.close()


def get_fixed_expense_exceptions(fixed_expense_id: int) -> list[str]:
    """Return list of month_labels where the fixed expense is skipped."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT month_label FROM fixed_expense_exceptions WHERE fixed_expense_id = ?",
            (fixed_expense_id,),
        ).fetchall()
    finally:
        conn.close()
    return [r["month_label"] for r in rows]


def unseed_fixed_expense_for_month(group_id: str, fixed_expense_id: int, month_label: str) -> None:
    """Remove a seeded fixed expense row from the expenses table for a specific month."""
    conn = _connect()
    try:
        # The seeded row description matches the fixed expense. Use the fixed_expense_id
        # stored in description+is_fixed or a direct join. We'll use description match.
        fe = conn.execute(
            "SELECT description FROM fixed_expenses WHERE id = ?",
            (fixed_expense_id,),
        ).fetchone()
        if fe:
            conn.execute(
                """
                DELETE FROM expenses
                WHERE group_id = ? AND month_label = ? AND is_fixed = 1
                AND description = ?
                """,
                (group_id, month_label, fe["description"]),
            )
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------


def get_all_months_summary(group_id: str) -> list[dict]:
    """Return all months that have expenses, newest first, with totals."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT month_label,
                   COUNT(*) AS expense_count,
                   SUM(total) AS total_amount
            FROM expenses
            WHERE group_id = ?
            GROUP BY month_label
            ORDER BY month_label DESC
            """,
            (group_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_next_expense_id(group_id: str, month_label: str) -> str:
    """Return the next sequential expense ID for the group+month, e.g. 'EXP-001'."""
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT expense_id FROM expenses
            WHERE group_id = ? AND month_label = ? AND is_fixed = 0
            ORDER BY id DESC LIMIT 1
            """,
            (group_id, month_label),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return "EXP-001"
    last_id: str = row["expense_id"]
    prefix = "EXP-"
    try:
        num = int(last_id[len(prefix):])
    except (ValueError, IndexError):
        num = 0
    return f"{prefix}{num + 1:03d}"


def add_expense(
    group_id: str,
    month_label: str,
    expense: dict,
) -> str:
    """
    Insert an expense row. If expense["expense_id"] is absent or empty,
    one is auto-generated. Returns the expense_id used.
    """
    expense_id: str = (expense.get("expense_id") or "").strip()
    if not expense_id:
        expense_id = get_next_expense_id(group_id, month_label)

    member_shares_json: str = json.dumps(expense.get("member_shares") or {})
    is_fixed: int = 1 if expense.get("is_fixed") else 0

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO expenses (
                group_id, expense_id, month_label, date, description, category,
                subtotal, hst_amount, hst_pct, tip_amount, tip_pct, total,
                paid_by, member_shares, notes, is_fixed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                expense_id,
                month_label,
                expense.get("date", ""),
                expense.get("description", ""),
                expense.get("category", ""),
                float(expense.get("subtotal", 0.0)),
                float(expense.get("hst_amount", 0.0)),
                float(expense.get("hst_pct", 0.0)),
                float(expense.get("tip_amount", 0.0)),
                float(expense.get("tip_pct", 0.0)),
                float(expense.get("total", 0.0)),
                expense.get("paid_by", ""),
                member_shares_json,
                expense.get("notes", ""),
                is_fixed,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return expense_id


def get_expenses(
    group_id: str,
    month_label: str,
    include_fixed: bool = True,
) -> list[dict]:
    """Return all expense rows for a group+month as list of dicts."""
    conn = _connect()
    try:
        if include_fixed:
            rows = conn.execute(
                "SELECT * FROM expenses WHERE group_id = ? AND month_label = ? ORDER BY id",
                (group_id, month_label),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM expenses WHERE group_id = ? AND month_label = ? AND is_fixed = 0 ORDER BY id",
                (group_id, month_label),
            ).fetchall()
    finally:
        conn.close()

    result: list[dict] = []
    for row in rows:
        d = dict(row)
        raw = d.get("member_shares", "{}")
        try:
            d["member_shares"] = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            d["member_shares"] = {}
        result.append(d)
    return result

def delete_expense(group_id: str, month_label: str, expense_id: str) -> bool:
    """Delete an expense by ID and month. Returns True if a row was deleted."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM expenses WHERE group_id = ? AND month_label = ? AND expense_id = ?",
            (group_id, month_label, expense_id),
        )
        conn.commit()
        deleted: bool = cursor.rowcount > 0
    finally:
        conn.close()
    return deleted

def update_expense(group_id: str, month_label: str, expense_id: str, updated: dict) -> bool:
    """Update an existing expense. Returns True if found and updated."""
    member_shares_json: str = json.dumps(updated.get("member_shares") or {})
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            UPDATE expenses SET
                date          = ?,
                description   = ?,
                category      = ?,
                subtotal      = ?,
                hst_amount    = ?,
                hst_pct       = ?,
                tip_amount    = ?,
                tip_pct       = ?,
                total         = ?,
                paid_by       = ?,
                member_shares = ?,
                notes         = ?
            WHERE group_id = ? AND month_label = ? AND expense_id = ?
            """,
            (
                updated.get("date", ""),
                updated.get("description", ""),
                updated.get("category", ""),
                float(updated.get("subtotal", 0.0)),
                float(updated.get("hst_amount", 0.0)),
                float(updated.get("hst_pct", 0.0)),
                float(updated.get("tip_amount", 0.0)),
                float(updated.get("tip_pct", 0.0)),
                float(updated.get("total", 0.0)),
                updated.get("paid_by", ""),
                member_shares_json,
                updated.get("notes", ""),
                group_id,
                month_label,
                expense_id,
            ),
        )
        conn.commit()
        found: bool = cursor.rowcount > 0
    finally:
        conn.close()
    return found


def seed_fixed_expenses_for_month(
    group_id: str,
    month_label: str,
    members: list[str],
) -> None:
    """
    Auto-populate the month with eligible fixed expenses.
    Safe to call multiple times — skips individually if that fixed expense
    was already seeded or has an exception for this month.

    Respects:
    - start_month: only seed if start_month is None or start_month <= month_label
    - end_month: only seed if end_month is None or end_month > month_label
    - active=1
    - fixed_expense_exceptions table: skip if exception exists for this month
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT fe.id, fe.description, fe.amount, fe.split_type,
                   fe.start_month, fe.end_month, m.name AS paid_by_name
            FROM fixed_expenses fe
            JOIN members m ON fe.paid_by_member_id = m.id
            WHERE fe.group_id = ? AND fe.active = 1
            """,
            (group_id,),
        ).fetchall()
    finally:
        conn.close()

    n = len(members)
    for row in rows:
        fe_id: int = row["id"]
        start: str | None = row["start_month"]
        end: str | None = row["end_month"]

        # Skip if month is before start_month
        if start and month_label < start:
            continue
        # Skip if month is at or after end_month
        if end and month_label >= end:
            continue

        # Check exception for this specific month
        conn2 = _connect()
        try:
            exc = conn2.execute(
                "SELECT 1 FROM fixed_expense_exceptions WHERE fixed_expense_id = ? AND month_label = ?",
                (fe_id, month_label),
            ).fetchone()
            already = conn2.execute(
                """
                SELECT 1 FROM expenses
                WHERE group_id = ? AND month_label = ? AND is_fixed = 1 AND description = ?
                """,
                (group_id, month_label, row["description"]),
            ).fetchone()
        finally:
            conn2.close()

        if exc or already:
            continue

        amount = float(row["amount"])
        split_type: str = row["split_type"]
        paid_by: str = row["paid_by_name"]

        if split_type == "equal" and n > 0:
            base = round(amount / n, 2)
            distributed = round(base * n, 2)
            remainder = round(amount - distributed, 2)
            member_shares: dict[str, float] = {m: base for m in members}
            member_shares[members[0]] = round(member_shares[members[0]] + remainder, 2)
        elif split_type in [m.lower() for m in members]:
            owner = next((m for m in members if m.lower() == split_type), members[0])
            member_shares = {m: (amount if m == owner else 0.0) for m in members}
        else:
            base = round(amount / n, 2) if n > 0 else 0.0
            member_shares = {m: base for m in members}

        add_expense(
            group_id=group_id,
            month_label=month_label,
            expense={
                # Use FIX-{id} to avoid colliding with EXP-xxx regular expenses
                "expense_id": f"FIX-{fe_id:03d}",
                "date": "",
                "description": row["description"],
                "category": "Fixed",
                "subtotal": amount,
                "hst_amount": 0.0,
                "hst_pct": 0.0,
                "tip_amount": 0.0,
                "tip_pct": 0.0,
                "total": amount,
                "paid_by": paid_by,
                "member_shares": member_shares,
                "notes": "",
                "is_fixed": True,
            },
        )


# ---------------------------------------------------------------------------
# Conversation State
# ---------------------------------------------------------------------------

def set_state(
    user_id: str,
    group_id: str,
    state: str,
    context: dict | None = None,
) -> None:
    """Upsert conversation state. context is serialized to JSON."""
    context_json: str | None = json.dumps(context) if context is not None else None
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO conversation_state (user_id, group_id, state, context_json, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id, group_id) DO UPDATE SET
                state        = excluded.state,
                context_json = excluded.context_json,
                updated_at   = CURRENT_TIMESTAMP
            """,
            (user_id, group_id, state, context_json),
        )
        conn.commit()
    finally:
        conn.close()


def get_state(user_id: str, group_id: str) -> dict | None:
    """Return {"state": ..., "context": ...} or None if no active state."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT state, context_json FROM conversation_state WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ).fetchone()
        if row is None:
            return None
        raw_json: str | None = row["context_json"]
        context_result: dict | None = json.loads(raw_json) if raw_json is not None else None
        return {"state": row["state"], "context": context_result}
    finally:
        conn.close()


def clear_state(user_id: str, group_id: str) -> None:
    """Delete conversation state for a user in a group."""
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM conversation_state WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        )
        conn.commit()
    finally:
        conn.close()
