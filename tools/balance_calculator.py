"""Balance calculation for household expense settlement."""

from __future__ import annotations

import copy
import html
import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS: dict[str, str] = {
    "CAD": "$",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "AUD": "$",
}

_NEAR_ZERO = 0.01  # threshold for treating a balance as settled (spec: within 0.01)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def calculate_balances(expenses: list[dict], members: list[str]) -> list[dict]:
    """
    Compute per-member balance from a list of expense rows.

    Returns list of dicts:
    [
        {
            "member": "Karlos",
            "total_paid": 423.00,    # sum of total where paid_by == member
            "total_owed": 312.50,    # sum of member_shares[member] across all rows
            "net_balance": 110.50,   # total_paid - total_owed
                                     # positive = others owe them
                                     # negative = they owe others
        },
        ...
    ]

    Always returns one entry per member, even if they have no expenses (all zeros).
    """
    # Initialise accumulators for each member.
    paid: dict[str, float] = {m: 0.0 for m in members}
    owed: dict[str, float] = {m: 0.0 for m in members}

    for row in expenses:
        payer: str = row.get("paid_by", "")
        total: float = float(row.get("total", 0.0))
        raw_shares = row.get("member_shares") or {}
        shares: dict[str, float] = {str(k): float(v) for k, v in raw_shares.items()}

        # Accumulate what the payer paid.
        if payer in paid:
            paid[payer] += total

        # Accumulate what each member owes for this expense.
        for member, amount in shares.items():
            member_str: str = str(member)
            if member_str in owed:
                owed[member_str] += float(amount)

    result: list[dict] = []
    for member in members:
        total_paid = round(paid[member], 2)
        total_owed = round(owed[member], 2)
        net_balance = round(total_paid - total_owed, 2)
        result.append(
            {
                "member": member,
                "total_paid": total_paid,
                "total_owed": total_owed,
                "net_balance": net_balance,
            }
        )

    return result


def format_balance_summary(
    balances: list[dict],
    month_label: str,
    currency: str = "CAD",
) -> str:
    """
    Format balances as a mobile-friendly HTML card layout.

    Example output:
        📊 <b>Feb 2026 — Summary</b>

        👤 <b>Sean</b>
          Paid: $424.02 · Owed: $291.00 · Net: <b>+$133.02</b>

        👤 <b>Mike</b>
          Paid: $153.62 · Owed: $291.00 · Net: <b>-$137.38</b>

        💸 <b>Settlement</b>
          Mike → Sean $137.38
    """
    sym: str = CURRENCY_SYMBOLS.get(currency.upper(), "$")

    lines: list[str] = []
    lines.append(f"📊 <b>{html.escape(month_label)} — Summary</b>")

    for entry in balances:
        net_val = entry["net_balance"]
        sign = "+" if net_val >= 0 else "-"
        net_str = f"{sign}{sym}{abs(net_val):.2f}"
        lines.append(
            f"\n👤 <b>{html.escape(entry['member'])}</b>\n"
            f"  Paid: {sym}{entry['total_paid']:.2f} · "
            f"Owed: {sym}{entry['total_owed']:.2f} · "
            f"Net: <b>{net_str}</b>"
        )

    transfers = compute_settlement(balances)
    if not transfers:
        lines.append("\n✅ All square! No transfers needed.")
    else:
        lines.append("\n💸 <b>Settlement</b>")
        for t in transfers:
            lines.append(
                f"  {html.escape(t['from'])} → {html.escape(t['to'])} {sym}{t['amount']:.2f}"
            )

    return "\n".join(lines)


def format_category_breakdown(expenses: list[dict], currency: str = "CAD") -> str:
    """
    Return a one-line category breakdown, e.g.:
        📂 <b>By Category</b>  Grocery $187 · Dining $254 · Transport $62
    Returns empty string if no categorised expenses.
    Excludes Settlement rows.
    """
    sym: str = CURRENCY_SYMBOLS.get(currency.upper(), "$")
    totals: dict[str, float] = {}
    for e in expenses:
        cat: str = (e.get("category") or "Other").strip()
        if cat == "Settlement":
            continue
        totals[cat] = totals.get(cat, 0.0) + float(e.get("total", 0.0))
    if not totals:
        return ""
    sorted_cats = sorted(totals.items(), key=lambda x: -x[1])
    parts = [f"{html.escape(cat)} {sym}{amt:.0f}" for cat, amt in sorted_cats]
    return "📂 <b>By Category</b>\n  " + " · ".join(parts)


def compute_settlement(balances: list[dict]) -> list[dict]:
    """
    Compute the minimal set of transfers to settle all balances.

    Uses a greedy algorithm: the largest debtor pays the largest creditor first.
    Returns a list of transfer dicts: [{"from": ..., "to": ..., "amount": ...}]
    Returns an empty list when all net balances are effectively zero.
    """
    # Work on mutable copies so we do not modify the caller's data.
    working: list[dict] = [
        {"member": b["member"], "balance": b["net_balance"]}
        for b in balances
    ]

    transfers: list[dict] = []

    while True:
        # Separate into debtors (negative) and creditors (positive).
        debtors = sorted(
            [w for w in working if w["balance"] < -_NEAR_ZERO],
            key=lambda x: x["balance"],  # most negative first
        )
        creditors = sorted(
            [w for w in working if w["balance"] > _NEAR_ZERO],
            key=lambda x: -x["balance"],  # most positive first
        )

        if not debtors or not creditors:
            break

        debtor = debtors[0]
        creditor = creditors[0]

        transfer_amount = min(abs(debtor["balance"]), creditor["balance"])
        transfer_amount = round(transfer_amount, 2)

        transfers.append(
            {
                "from": debtor["member"],
                "to": creditor["member"],
                "amount": transfer_amount,
            }
        )

        # Update working balances.
        for w in working:
            if w["member"] == debtor["member"]:
                w["balance"] = round(w["balance"] + transfer_amount, 2)
            elif w["member"] == creditor["member"]:
                w["balance"] = round(w["balance"] - transfer_amount, 2)

    return transfers


def parse_member_shares(
    raw_text: str,
    members: list[str],
    items: list[dict],
    total: float,
    sender_name: str,
) -> dict:
    """
    Parse user's text input for item assignments into a member_shares dict.

    Input format (from Telegram message):
      - "3 mine"                  → item 3 entirely the sender's
      - "1 karlos partner"        → item 1 split equally between karlos and partner
      - "2 partner"               → item 2 entirely partner's
      - "all except karlos"       → all items to all members except karlos
      - "1 mine, 3 partner, ..."  → multiple assignments separated by commas

    items: list of dicts: [{"name": "Chicken", "price": 12.00}, ...]
    total: fallback total when items list is empty
    sender_name: resolves "mine" / "me"

    Returns {member_name: amount_owed}.
    Unassigned amounts are split equally among all members.
    Returns equal split if raw_text is empty or unparseable.
    """
    # Build a lower-case lookup for member names.
    member_lower: dict[str, str] = {m.lower(): m for m in members}
    # Canonicalise the sender name; fall back to the raw value if not in the list.
    sender_canon: str = member_lower.get(sender_name.lower(), sender_name)

    # Initialise per-member accumulator.
    shares: dict[str, float] = {m: 0.0 for m in members}

    # Guard: empty input → equal split.
    if not raw_text or not raw_text.strip():
        return _equal_split(members, total)

    # If items list is empty, treat total as a single unnamed item for parsing.
    if not items:
        items = [{"name": "total", "price": float(total)}]

    num_items = len(items)

    # Track which item indices have been explicitly assigned.
    assigned: dict[int, list[str]] = {}  # item_idx (0-based) → [member, ...]

    text = raw_text.strip()

    # -----------------------------------------------------------------------
    # Handle "all except <name>" pattern.
    # -----------------------------------------------------------------------
    except_match = re.match(
        r"^all\s+except\s+(.+)$", text, re.IGNORECASE
    )
    if except_match:
        excluded_raw = except_match.group(1).strip().lower()
        excluded = _resolve_name(excluded_raw, member_lower, sender_canon)
        if excluded:
            recipients = [m for m in members if m != excluded]
        else:
            recipients = members  # couldn't resolve, fall back to all
        if not recipients:
            recipients = members
        for idx in range(num_items):
            assigned[idx] = recipients
        return _compute_shares(items, assigned, members, total)

    # -----------------------------------------------------------------------
    # Parse individual assignment clauses separated by commas.
    # -----------------------------------------------------------------------
    clauses = [c.strip() for c in text.split(",") if c.strip()]

    for clause in clauses:
        tokens = clause.split()
        if not tokens:
            continue

        # First token may be an item number or "all".
        first = tokens[0].lower()

        if first == "all":
            # "all <name...>" — assign all items to the named members.
            name_tokens = tokens[1:]
            resolved = _resolve_names(name_tokens, member_lower, sender_canon)
            if not resolved:
                resolved = members
            for idx in range(num_items):
                assigned[idx] = resolved
            continue

        # Try to interpret first token as an item index (1-based).
        try:
            item_num = int(first)
            item_idx = item_num - 1
        except ValueError:
            # Not a number — treat entire clause as a name list applied to all.
            resolved = _resolve_names(tokens, member_lower, sender_canon)
            if resolved:
                for idx in range(num_items):
                    assigned[idx] = resolved
            continue

        if item_idx < 0 or item_idx >= num_items:
            continue  # out of range — skip

        name_tokens = tokens[1:]
        resolved = _resolve_names(name_tokens, member_lower, sender_canon)
        if not resolved:
            # No names given — skip this clause.
            continue

        assigned[item_idx] = resolved

    return _compute_shares(items, assigned, members, total)


def apply_settlement(
    balances: list[dict],
    payer: str,
    recipient: str,
    amount: float,
) -> list[dict]:
    """
    Apply a settlement payment and return updated balances.

    When 'payer' transfers 'amount' to 'recipient':
      - payer.net_balance increases by amount (debt reduced)
      - recipient.net_balance decreases by amount (credit reduced)
    total_paid and total_owed are NOT modified.

    Returns a new list (deep copy) with updated balances.
    """
    updated = copy.deepcopy(balances)
    for entry in updated:
        if entry["member"] == payer:
            entry["net_balance"] = round(entry["net_balance"] + amount, 2)
        elif entry["member"] == recipient:
            entry["net_balance"] = round(entry["net_balance"] - amount, 2)
    return updated


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _equal_split(members: list[str], total: float) -> dict:
    """Split total equally among all members, remainder to first member."""
    if not members:
        return {}
    n = len(members)
    base = round(total / n, 2)
    total_assigned = round(base * n, 2)
    remainder = round(total - total_assigned, 2)
    shares = {m: base for m in members}
    shares[members[0]] = round(shares[members[0]] + remainder, 2)
    return shares


def _resolve_name(token: str, lookup: dict[str, str], fallback) -> str | None:
    """Resolve a single lower-case token to a canonical member name."""
    if token in ("mine", "me"):
        return fallback  # fallback is sender_canon
    return lookup.get(token, None)


def _resolve_names(
    tokens: list[str], lookup: dict[str, str], sender_canon: str
) -> list[str]:
    """Resolve a list of lower-case tokens to canonical member names (deduped, ordered)."""
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        t = token.lower().strip(",").strip()
        if t in ("mine", "me"):
            canon = sender_canon
        else:
            canon = lookup.get(t)
        if canon and canon not in seen:
            seen.add(canon)
            result.append(canon)
    return result


def _compute_shares(
    items: list[dict],
    assigned: dict[int, list[str]],
    members: list[str],
    total: float,
) -> dict:
    """
    Given per-item assignments, compute the member_shares dict.

    Unassigned items are split equally among all members.
    Per-item splits round to 2 dp; remainder goes to the first named member
    for that item (serves as the payer proxy for rounding).
    """
    shares: dict[str, float] = {m: 0.0 for m in members}
    unassigned_total: float = 0.0

    for idx, item in enumerate(items):
        price = float(item.get("price", 0.0))
        if idx in assigned:
            recipients = assigned[idx]
            n = len(recipients)
            if n == 0:
                unassigned_total += price
                continue
            base = round(price / n, 2)
            distributed = round(base * n, 2)
            remainder = round(price - distributed, 2)
            for i, member in enumerate(recipients):
                if member in shares:
                    if i == 0:
                        shares[member] = round(shares[member] + base + remainder, 2)
                    else:
                        shares[member] = round(shares[member] + base, 2)
        else:
            unassigned_total += price

    # Distribute the unassigned portion equally among all members.
    if unassigned_total > 0.0 and members:
        n = len(members)
        base = round(unassigned_total / n, 2)
        distributed = round(base * n, 2)
        remainder = round(unassigned_total - distributed, 2)
        for i, member in enumerate(members):
            if i == 0:
                shares[member] = round(shares[member] + base + remainder, 2)
            else:
                shares[member] = round(shares[member] + base, 2)

    return shares


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    members = ["Karlos", "Partner"]

    expenses = [
        {
            "expense_id": "EXP-001",
            "date": "2026-03-01",
            "description": "Groceries",
            "category": "Food",
            "subtotal": 100.00,
            "hst_amount": 13.00,
            "hst_pct": 13.0,
            "tip_amount": 0.0,
            "tip_pct": 0.0,
            "total": 113.00,
            "paid_by": "Karlos",
            "member_shares": {"Karlos": 56.50, "Partner": 56.50},
            "notes": "",
        },
        {
            "expense_id": "EXP-002",
            "date": "2026-03-05",
            "description": "Dinner",
            "category": "Restaurants",
            "subtotal": 80.00,
            "hst_amount": 10.40,
            "hst_pct": 13.0,
            "tip_amount": 16.00,
            "tip_pct": 20.0,
            "total": 106.40,
            "paid_by": "Partner",
            "member_shares": {"Karlos": 53.20, "Partner": 53.20},
            "notes": "",
        },
        {
            "expense_id": "EXP-003",
            "date": "2026-03-10",
            "description": "Utilities",
            "category": "Bills",
            "subtotal": 200.00,
            "hst_amount": 0.0,
            "hst_pct": 0.0,
            "tip_amount": 0.0,
            "tip_pct": 0.0,
            "total": 200.00,
            "paid_by": "Karlos",
            "member_shares": {"Karlos": 100.00, "Partner": 100.00},
            "notes": "",
        },
    ]

    print("=== calculate_balances ===")
    balances = calculate_balances(expenses, members)
    for b in balances:
        print(b)

    print()
    print("=== compute_settlement ===")
    transfers = compute_settlement(balances)
    for t in transfers:
        print(t)

    print()
    print("=== format_balance_summary ===")
    summary = format_balance_summary(balances, "March 2026", currency="CAD")
    print(summary)

    print()
    print("=== apply_settlement ===")
    updated = apply_settlement(balances, transfers[0]["from"], transfers[0]["to"], transfers[0]["amount"])
    for b in updated:
        print(b)

    print()
    print("=== parse_member_shares ===")
    items = [
        {"name": "Chicken", "price": 12.00},
        {"name": "Salad", "price": 8.00},
        {"name": "Wine", "price": 20.00},
    ]
    test_cases = [
        ("3 mine", "Karlos"),
        ("1 karlos partner", "Karlos"),
        ("2 partner", "Karlos"),
        ("all except karlos", "Karlos"),
        ("1 mine, 2 partner, 3 karlos partner", "Karlos"),
        ("", "Karlos"),
    ]
    for raw, sender in test_cases:
        result = parse_member_shares(raw, members, items, 40.00, sender)
        print(f"  input={repr(raw)!s:40s}  shares={result}")

    print()
    print("All tests passed.")
