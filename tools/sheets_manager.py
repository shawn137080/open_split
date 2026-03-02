"""Google Sheets manager for household expense data."""

import json
import os
import pickle
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials  # noqa: F401 – kept for completeness
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Column indices (0-based) for the fixed portion of each monthly tab
_COL_DATE = 0
_COL_DESCRIPTION = 1
_COL_CATEGORY = 2
_COL_SUBTOTAL = 3
_COL_HST_DOLLAR = 4
_COL_HST_PCT = 5
_COL_TIP_DOLLAR = 6
_COL_TIP_PCT = 7
_COL_TOTAL = 8
_COL_PAID_BY = 9
_MEMBER_COL_START = 10  # dynamic member columns begin here



# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _get_service():  # type: ignore[return]
    """Return an authenticated Google Sheets API service."""
    credentials_file: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    token_file: str = os.getenv("GOOGLE_TOKEN_FILE", "token.pickle")

    creds = None

    # Detect credential type by inspecting the JSON file.
    if os.path.exists(credentials_file):
        with open(credentials_file, "r", encoding="utf-8") as fh:
            cred_data: dict = json.load(fh)
        if cred_data.get("type") == "service_account":
            # --- Service account path ---
            sa_creds = service_account.Credentials.from_service_account_file(
                credentials_file, scopes=SCOPES
            )
            return build("sheets", "v4", credentials=sa_creds)

    # --- OAuth path ---
    if os.path.exists(token_file):
        with open(token_file, "rb") as fh:
            creds = pickle.load(fh)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_file):
                raise FileNotFoundError(
                    f"Credentials file not found: {credentials_file}. "
                    "Set GOOGLE_CREDENTIALS_FILE env var to the correct path."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "wb") as fh:
            pickle.dump(creds, fh)

    return build("sheets", "v4", credentials=creds)


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def _build_header(members: list) -> list:
    """Build the column header row for a monthly tab."""
    base: list = [
        "Date",
        "Description",
        "Category",
        "Subtotal",
        "HST $",
        "HST %",
        "Tip $",
        "Tip %",
        "Total",
        "Paid By",
    ]
    member_cols: list = [f"{m} $" for m in members]
    return base + member_cols + ["Expense ID", "Notes"]


def _expense_id_col_index(members: list) -> int:
    """Return the 0-based column index of the 'Expense ID' column."""
    return _MEMBER_COL_START + len(members)


# ---------------------------------------------------------------------------
# Internal sheet helpers
# ---------------------------------------------------------------------------


def _col_letter(index: int) -> str:
    """Convert a 0-based column index to an A1-notation letter (supports up to ZZ)."""
    result = ""
    n = index + 1  # make 1-based
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _get_sheet_tabs(service: Any, sheet_id: str) -> list:
    """Return the list of sheet tab properties for a spreadsheet."""
    result: dict = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tabs: list = result.get("sheets", [])
    return tabs


def _tab_exists(service: Any, sheet_id: str, title: str) -> bool:
    """Return True if a tab with the given title exists."""
    tabs = _get_sheet_tabs(service, sheet_id)
    return any(tab["properties"]["title"] == title for tab in tabs)


def _get_sheet_id_by_title(service: Any, sheet_id: str, title: str) -> int | None:
    """Return the numeric sheetId (gid) for a tab title, or None."""
    tabs = _get_sheet_tabs(service, sheet_id)
    for tab in tabs:
        if tab["properties"]["title"] == title:
            gid: int = int(tab["properties"]["sheetId"])
            return gid
    return None


def _read_tab_values(service: Any, sheet_id: str, month_label: str) -> Any:
    """Return all values from a tab as a plain list-of-lists (may contain empty rows)."""
    try:
        result: dict = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"'{month_label}'")
            .execute()
        )
        return result.get("values", [])
    except HttpError as exc:
        if exc.resp.status == 400:
            raise ValueError(
                f"Tab '{month_label}' not found in sheet {sheet_id}."
            ) from exc
        raise


def _data_rows(values: Any) -> list[Any]:
    """Return all rows after the header as a plain list (skips index 0)."""
    all_rows: list[Any] = list(values)
    return [all_rows[i] for i in range(1, len(all_rows))]


def _find_exp_id_col(header_row: list, members: list) -> int:
    """Return the 0-based index of the 'Expense ID' column."""
    for i, cell in enumerate(header_row):
        if str(cell) == "Expense ID":
            return i
    return _expense_id_col_index(members)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _bold_header_request(sheet_gid: int, num_cols: int) -> dict:
    """Return a batchUpdate request to bold and shade the first row."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_gid,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                }
            },
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }
    }


def _currency_format_requests(
    sheet_gid: int, col_indices: list, num_data_rows: int = 1000
) -> list:
    """Return batchUpdate requests to format given columns as $#,##0.00."""
    requests: list = []
    for col in col_indices:
        col_int: int = int(col)
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_gid,
                        "startRowIndex": 1,
                        "endRowIndex": num_data_rows,
                        "startColumnIndex": col_int,
                        "endColumnIndex": col_int + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "CURRENCY",
                                "pattern": "$#,##0.00",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )
    return requests


def _apply_month_tab_formatting(
    service: Any, sheet_id: str, sheet_gid: int, members: list
) -> None:
    """Apply bold header + currency column formatting to a monthly tab."""
    header = _build_header(members)
    num_cols: int = len(header)

    currency_cols: list = (
        [_COL_SUBTOTAL, _COL_HST_DOLLAR, _COL_TIP_DOLLAR, _COL_TOTAL]
        + list(range(_MEMBER_COL_START, _MEMBER_COL_START + len(members)))
    )

    fmt_requests: list = [_bold_header_request(sheet_gid, num_cols)]
    fmt_requests.extend(_currency_format_requests(sheet_gid, currency_cols))

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": fmt_requests},
    ).execute()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_sheet(household_name: str, members: list) -> str:
    """
    Create a new Google Sheet for the household.
    Creates a 'Summary' tab (renames 'Sheet1').
    Returns the sheet_id.
    """
    service = _get_service()

    body: dict = {
        "properties": {"title": f"{household_name} — Expenses"},
        "sheets": [
            {
                "properties": {
                    "title": "Summary",
                    "index": 0,
                    "tabColor": {"red": 0.2, "green": 0.6, "blue": 1.0},
                }
            }
        ],
    }
    spreadsheet: dict = service.spreadsheets().create(body=body).execute()
    sheet_id: str = str(spreadsheet["spreadsheetId"])

    summary_gid: int = int(spreadsheet["sheets"][0]["properties"]["sheetId"])

    summary_header: list = [["Member", "Total Paid", "Total Owed", "Net Balance"]]
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="'Summary'!A1",
        valueInputOption="RAW",
        body={"values": summary_header},
    ).execute()

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [_bold_header_request(summary_gid, 4)]},
    ).execute()

    return sheet_id


def link_sheet(sheet_id: str) -> bool:
    """
    Verify that a given sheet_id exists and is accessible.
    Returns True if accessible, False otherwise.
    """
    try:
        service = _get_service()
        service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        return True
    except HttpError as exc:
        if exc.resp.status in (403, 404):
            return False
        raise
    except Exception:
        return False


def get_or_create_month_tab(
    sheet_id: str,
    month_label: str,
    members: list,
    fixed_expenses: list,
) -> None:
    """
    Create a monthly tab if it doesn't exist (e.g., 'Mar 2026').
    If creating: adds header row + fixed expense rows.
    If exists: does nothing.

    fixed_expenses: list of dicts with keys:
        description, amount, paid_by_name, member_shares ({member_name: amount})
    """
    service = _get_service()

    if _tab_exists(service, sheet_id, month_label):
        return

    add_response: dict = (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": month_label,
                                "tabColor": {"red": 0.4, "green": 0.8, "blue": 0.4},
                            }
                        }
                    }
                ]
            },
        )
        .execute()
    )
    new_gid: int = int(
        add_response["replies"][0]["addSheet"]["properties"]["sheetId"]
    )

    header = _build_header(members)
    rows: list = [header]
    for fe in fixed_expenses:
        rows.append(_build_fixed_expense_row(fe, members))

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{month_label}'!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()

    _apply_month_tab_formatting(service, sheet_id, new_gid, members)


def _build_fixed_expense_row(fe: dict, members: list) -> list:
    """Build a spreadsheet row for a fixed expense (no Expense ID)."""
    amount: float = float(fe.get("amount", 0.0))
    paid_by: str = str(fe.get("paid_by_name", ""))
    description: str = str(fe.get("description", ""))
    member_shares: dict = fe.get("member_shares", {})

    row: list = [
        "",           # Date — left blank for fixed expenses
        description,
        "Fixed",      # Category
        amount,       # Subtotal
        "",           # HST $
        "",           # HST %
        "",           # Tip $
        "",           # Tip %
        amount,       # Total
        paid_by,
    ]
    for m in members:
        row.append(member_shares.get(m, ""))
    row.append("")    # Expense ID — empty for fixed expenses
    row.append("")    # Notes
    return row


def append_expense_row(
    sheet_id: str,
    month_label: str,
    members: list,
    expense: dict,
) -> None:
    """
    Append one expense row to the correct monthly tab.

    expense dict keys:
        expense_id, date, description, category, subtotal, hst_amount, hst_pct,
        tip_amount, tip_pct, total, paid_by, member_shares, notes (optional)
    """
    service = _get_service()

    if not _tab_exists(service, sheet_id, month_label):
        raise ValueError(
            f"Tab '{month_label}' does not exist in sheet {sheet_id}. "
            "Call get_or_create_month_tab first."
        )

    member_shares: dict = expense.get("member_shares", {})
    row: list = [
        expense.get("date", ""),
        expense.get("description", ""),
        expense.get("category", ""),
        expense.get("subtotal", 0.0),
        expense.get("hst_amount", 0.0),
        expense.get("hst_pct", 0.0),
        expense.get("tip_amount", 0.0),
        expense.get("tip_pct", 0.0),
        expense.get("total", 0.0),
        expense.get("paid_by", ""),
    ]
    for m in members:
        row.append(member_shares.get(m, 0.0))
    row.append(expense.get("expense_id", ""))
    row.append(expense.get("notes", ""))

    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"'{month_label}'!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def update_summary_tab(
    sheet_id: str,
    members: list,
    month_label: str,
    balances: list,
) -> None:
    """
    Overwrite the Summary tab with current balance data.

    balances: list of dicts with keys:
        member, total_paid, total_owed, net_balance
    """
    service = _get_service()

    if not _tab_exists(service, sheet_id, "Summary"):
        raise ValueError(f"'Summary' tab not found in sheet {sheet_id}.")

    summary_gid_or_none = _get_sheet_id_by_title(service, sheet_id, "Summary")
    if summary_gid_or_none is None:
        raise ValueError(f"'Summary' tab not found in sheet {sheet_id}.")
    summary_gid: int = summary_gid_or_none

    header: list = [["Member", "Total Paid", "Total Owed", "Net Balance", "As of Month"]]
    data_rows: list = []
    for b in balances:
        data_rows.append(
            [
                b.get("member", ""),
                b.get("total_paid", 0.0),
                b.get("total_owed", 0.0),
                b.get("net_balance", 0.0),
                month_label,
            ]
        )

    all_rows: list = header + data_rows

    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range="'Summary'",
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="'Summary'!A1",
        valueInputOption="RAW",
        body={"values": all_rows},
    ).execute()

    fmt_requests: list = [_bold_header_request(summary_gid, 5)]
    fmt_requests.extend(_currency_format_requests(summary_gid, [1, 2, 3]))
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": fmt_requests},
    ).execute()


def get_next_expense_id(sheet_id: str, month_label: str) -> str:
    """
    Return the next available expense ID for a month tab (e.g. 'EXP-001').
    Looks at existing rows and increments. Returns 'EXP-001' if no expenses yet.
    """
    service = _get_service()

    if not _tab_exists(service, sheet_id, month_label):
        return "EXP-001"

    values: list = _read_tab_values(service, sheet_id, month_label)
    if len(values) < 2:
        return "EXP-001"

    header_row: list = list(values[0])
    exp_id_col: int = _find_exp_id_col(header_row, [])

    found_nums: list = []
    data_rows: list = list(values[1:])
    for row in data_rows:
        row_list: list = list(row)
        if len(row_list) > exp_id_col:
            cell_val: str = str(row_list[exp_id_col]).strip()
            prefix = "EXP-"
            if cell_val.startswith(prefix):
                suffix: str = cell_val[len(prefix):]
                try:
                    found_nums.append(int(suffix))
                except ValueError:
                    pass

    base: int = max(found_nums) if found_nums else 0
    next_num: int = base + 1
    return f"EXP-{next_num:03d}"


def update_expense_row(
    sheet_id: str,
    month_label: str,
    expense_id: str,
    members: list,
    updated_expense: dict,
) -> bool:
    """
    Find the row with the given expense_id and update it in place.
    Returns True if found and updated, False if expense_id not found.
    """
    service = _get_service()

    if not _tab_exists(service, sheet_id, month_label):
        raise ValueError(f"Tab '{month_label}' not found in sheet {sheet_id}.")

    values: list = _read_tab_values(service, sheet_id, month_label)
    if len(values) < 2:
        return False

    header_row: list = list(values[0])
    exp_id_col: int = _find_exp_id_col(header_row, members)

    target_row_index: int = 0
    data_rows: list = list(values[1:])
    for i, raw_row in enumerate(data_rows):
        row: list = list(raw_row)
        if len(row) > exp_id_col and str(row[exp_id_col]).strip() == expense_id:
            target_row_index = i + 2  # +1 for header, +1 for 1-based sheet rows
            break

    if target_row_index == 0:
        return False

    member_shares: dict = updated_expense.get("member_shares", {})
    new_row: list = [
        updated_expense.get("date", ""),
        updated_expense.get("description", ""),
        updated_expense.get("category", ""),
        updated_expense.get("subtotal", 0.0),
        updated_expense.get("hst_amount", 0.0),
        updated_expense.get("hst_pct", 0.0),
        updated_expense.get("tip_amount", 0.0),
        updated_expense.get("tip_pct", 0.0),
        updated_expense.get("total", 0.0),
        updated_expense.get("paid_by", ""),
    ]
    for m in members:
        new_row.append(member_shares.get(m, 0.0))
    new_row.append(expense_id)
    new_row.append(updated_expense.get("notes", ""))

    num_cols: int = len(new_row)
    end_col: str = _col_letter(num_cols - 1)
    update_range: str = (
        f"'{month_label}'!A{target_row_index}:{end_col}{target_row_index}"
    )

    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=update_range,
        valueInputOption="RAW",
        body={"values": [new_row]},
    ).execute()

    return True


def delete_expense_row(
    sheet_id: str,
    month_label: str,
    expense_id: str,
) -> bool:
    """
    Find the row with the given expense_id and delete it.
    Returns True if found and deleted, False if not found.
    """
    service = _get_service()

    if not _tab_exists(service, sheet_id, month_label):
        raise ValueError(f"Tab '{month_label}' not found in sheet {sheet_id}.")

    values: list = _read_tab_values(service, sheet_id, month_label)
    if len(values) < 2:
        return False

    header_row: list = list(values[0])
    exp_id_col: int = _find_exp_id_col(header_row, [])
    # If "Expense ID" column not found, can't locate the row
    if exp_id_col >= len(header_row):
        return False

    target_row_index: int = 0
    data_rows: list = list(values[1:])
    for i, raw_row in enumerate(data_rows):
        row: list = list(raw_row)
        if len(row) > exp_id_col and str(row[exp_id_col]).strip() == expense_id:
            target_row_index = i + 2  # +1 header, +1 for 1-based rows
            break

    if target_row_index == 0:
        return False

    tab_gid_or_none = _get_sheet_id_by_title(service, sheet_id, month_label)
    if tab_gid_or_none is None:
        raise ValueError(f"Tab '{month_label}' not found in sheet {sheet_id}.")
    tab_gid: int = tab_gid_or_none

    start_index: int = target_row_index - 1  # convert 1-based → 0-based

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": tab_gid,
                            "dimension": "ROWS",
                            "startIndex": start_index,
                            "endIndex": target_row_index,
                        }
                    }
                }
            ]
        },
    ).execute()

    return True


def get_month_expenses(sheet_id: str, month_label: str) -> list:
    """
    Return all expense rows from a monthly tab as list of dicts.
    Skips the header row and fixed expense rows (those without an Expense ID).
    """
    service = _get_service()

    if not _tab_exists(service, sheet_id, month_label):
        raise ValueError(f"Tab '{month_label}' not found in sheet {sheet_id}.")

    values: list = _read_tab_values(service, sheet_id, month_label)
    if len(values) < 2:
        return []

    header_row: list = list(values[0])

    # Build column-name → index map
    col_map: dict = {str(name): idx for idx, name in enumerate(header_row)}

    def _cell(row: list, col_name: str, default: Any = "") -> Any:
        idx: Any = col_map.get(col_name)
        if idx is None:
            return default
        col_idx: int = int(idx)
        if col_idx >= len(row):
            return default
        return row[col_idx]

    def _to_float(val: Any) -> float:
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    # Member columns: end with " $" but are not "HST $" or "Tip $"
    member_cols: list = [
        col for col in header_row
        if str(col).endswith(" $") and col not in ("HST $", "Tip $")
    ]

    expenses: list = []
    data_rows: list = list(values[1:])
    for raw_row in data_rows:
        row: list = list(raw_row)
        exp_id: str = str(_cell(row, "Expense ID")).strip()
        if not exp_id:
            continue  # skip fixed expenses and blank rows

        member_shares: dict = {}
        for mc in member_cols:
            member_name: str = str(mc)[:-2]  # strip trailing " $"
            member_shares[member_name] = _to_float(_cell(row, str(mc), 0.0))

        expenses.append(
            {
                "expense_id": exp_id,
                "date": _cell(row, "Date"),
                "description": _cell(row, "Description"),
                "category": _cell(row, "Category"),
                "subtotal": _to_float(_cell(row, "Subtotal", 0.0)),
                "hst_amount": _to_float(_cell(row, "HST $", 0.0)),
                "hst_pct": _to_float(_cell(row, "HST %", 0.0)),
                "tip_amount": _to_float(_cell(row, "Tip $", 0.0)),
                "tip_pct": _to_float(_cell(row, "Tip %", 0.0)),
                "total": _to_float(_cell(row, "Total", 0.0)),
                "paid_by": _cell(row, "Paid By"),
                "member_shares": member_shares,
                "notes": _cell(row, "Notes"),
            }
        )

    return expenses
