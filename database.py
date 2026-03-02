"""SQLite database setup and helpers for auto_split."""

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
                sheet_id        TEXT,
                admin_user_id   TEXT NOT NULL,
                timezone        TEXT NOT NULL DEFAULT 'America/Toronto',
                currency        TEXT NOT NULL DEFAULT 'CAD',
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
                FOREIGN KEY (group_id)          REFERENCES groups(group_id),
                FOREIGN KEY (paid_by_member_id) REFERENCES members(id)
            );

            CREATE TABLE IF NOT EXISTS conversation_state (
                user_id       TEXT NOT NULL,
                group_id      TEXT NOT NULL,
                state         TEXT NOT NULL,
                context_json  TEXT,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, group_id)
            );
            """
        )
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
) -> None:
    """Create a new household group. Raises ValueError if group already exists."""
    conn = _connect()
    try:
        try:
            conn.execute(
                """
                INSERT INTO groups (group_id, household_name, admin_user_id, timezone, currency)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, household_name, admin_user_id, timezone, currency),
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


def update_group_sheet_id(group_id: str, sheet_id: str) -> None:
    """Set the Google Sheet ID for a household."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE groups SET sheet_id = ? WHERE group_id = ?",
            (sheet_id, group_id),
        )
        conn.commit()
    finally:
        conn.close()


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
) -> int:
    """Insert a fixed expense and return its id."""
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO fixed_expenses (group_id, description, amount, paid_by_member_id, split_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group_id, description, amount, paid_by_member_id, split_type),
        )
        conn.commit()
        row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("INSERT into fixed_expenses did not produce a row id")
        expense_id: int = row_id
    finally:
        conn.close()
    return expense_id


def get_fixed_expenses(group_id: str) -> list[dict]:
    """Return all active fixed expenses for a group."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM fixed_expenses WHERE group_id = ? AND active = 1",
            (group_id,),
        ).fetchall()
        result: list[dict] = [dict(row) for row in rows]
    finally:
        conn.close()
    return result


def deactivate_fixed_expense(expense_id: int) -> None:
    """Soft-delete: set active=0."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE fixed_expenses SET active = 0 WHERE id = ?",
            (expense_id,),
        )
        conn.commit()
    finally:
        conn.close()


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


def get_state(user_id: str, group_id: str) -> tuple[str | None, dict | None]:
    """Return (state, context_dict) or (None, None) if no state."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT state, context_json FROM conversation_state WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ).fetchone()
        if row is None:
            state_result: str | None = None
            context_result: dict | None = None
        else:
            state_result = row["state"]
            raw_json: str | None = row["context_json"]
            context_result = json.loads(raw_json) if raw_json is not None else None
    finally:
        conn.close()
    return (state_result, context_result)


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
