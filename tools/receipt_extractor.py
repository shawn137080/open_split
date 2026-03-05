"""Gemini Vision receipt data extractor."""

import json
import logging
import time
from datetime import datetime

from google import genai
from google.genai import types

from config import GEMINI_API_KEY

_client = genai.Client(api_key=GEMINI_API_KEY)
_MODEL = "gemini-2.5-flash"

_PROMPT = """You are a receipt OCR assistant. Extract all information from this receipt image and return ONLY a valid JSON object with NO markdown formatting.

Return this exact JSON structure:
{
  "merchant": "store name or null",
  "date": "YYYY-MM-DD or null if unclear",
  "category": "one of: Grocery, Dining, Transport, Utilities, Health, Entertainment, Shopping, Other — pick based on merchant context: restaurants/cafes/bars/food courts→Dining; supermarkets/wholesale/food stores→Grocery; gas stations/parking/transit/rideshare/auto→Transport; phone/internet/electricity/water/rent→Utilities; pharmacy/clinic/gym/optician→Health; movies/streaming/games/events→Entertainment; clothing/electronics/gifts/hardware→Shopping; anything else→Other",
  "subtotal": number or null,
  "hst_amount": number or null,
  "hst_pct": number or null,
  "tip_amount": number or null,
  "tip_pct": number or null,
  "total": number or null,
  "items": [{"name": "item name", "price": number or null, "quantity": number or null, "taxable": true or false}],
  "currency": "CAD",
  "confidence": "high/medium/low"
}

Rules:
- Return ONLY the JSON object, no other text
- Use null (not "null") for missing values
- Amounts should be numbers, not strings
- If receipt is in another language, still extract numbers and translate merchant name if possible
- Items list can be empty [] if items are not visible
- Set confidence to "low" if image is blurry or hard to read
- TAXABLE INDICATOR: Set taxable: true if the item has an "H" marker (HST applies). On Canadian receipts "H" appears next to the price.
- DISCOUNTS — CRITICAL: If a price ends with a minus sign ("3.00-") or starts with minus ("-3.00"), it is a DISCOUNT. Set its price as a NEGATIVE number (e.g. -3.00). Never treat a discount as a positive item.
- Items whose code starts with "TPD/" are coupon/discount redemption lines — ALWAYS set their price as NEGATIVE.
- Items labelled "DISCOUNT", "COUPON", "SAVINGS", "MEMBER SAVINGS", or similar are discounts — ALWAYS set price as NEGATIVE.
- The "H" flag on a discount line (e.g. "3.00- H") means pre-tax discount; still set price as negative.
- Do NOT include subtotal, tax, total, payment method, or store header lines as items.
"""

# Data fields that, when None, count as failed extractions
_DATA_FIELDS = [
    "merchant",
    "date",
    "category",
    "subtotal",
    "hst_amount",
    "hst_pct",
    "tip_amount",
    "tip_pct",
    "total",
]

_EMPTY_RESULT = {
    "merchant": None,
    "date": None,
    "category": None,
    "subtotal": None,
    "hst_amount": None,
    "hst_pct": None,
    "tip_amount": None,
    "tip_pct": None,
    "total": None,
    "items": None,
    "currency": "CAD",
    "confidence": "low",
    "failed_fields": ["all"],
}

# Human-readable labels for failed field names
_FIELD_LABELS = {
    "merchant": "merchant",
    "date": "date",
    "category": "category",
    "subtotal": "subtotal",
    "hst_amount": "HST amount",
    "hst_pct": "HST %",
    "tip_amount": "tip amount",
    "tip_pct": "tip %",
    "total": "total",
}


def _call_gemini(image_bytes: bytes, mime_type: str) -> dict:
    """Send image to Gemini and return parsed JSON dict."""
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    response = _client.models.generate_content(
        model=_MODEL,
        contents=[_PROMPT, image_part],
    )
    raw_text = response.text.strip()

    # Strip markdown code fences if present
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        # Drop the opening fence line (```json or ```)
        lines = lines[1:]
        # Drop the closing fence line if present
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw_text = "\n".join(lines).strip()

    return json.loads(raw_text)


def _normalize(raw: dict) -> dict:
    """Validate and normalise the raw dict from Gemini into the data contract."""
    result = {
        "merchant": raw.get("merchant"),
        "date": raw.get("date"),
        "category": raw.get("category"),
        "subtotal": raw.get("subtotal"),
        "hst_amount": raw.get("hst_amount"),
        "hst_pct": raw.get("hst_pct"),
        "tip_amount": raw.get("tip_amount"),
        "tip_pct": raw.get("tip_pct"),
        "total": raw.get("total"),
        "items": raw.get("items", []),
        "currency": raw.get("currency") or "CAD",
        "confidence": raw.get("confidence", "low"),
    }

    # Coerce numeric fields — cast strings like "45.20" to float
    for field in ("subtotal", "hst_amount", "hst_pct", "tip_amount", "tip_pct", "total"):
        val = result[field]
        if val is not None:
            try:
                result[field] = float(val)
            except (TypeError, ValueError):
                result[field] = None

    # Build failed_fields: any data field that is still None
    failed = [f for f in _DATA_FIELDS if result[f] is None]
    result["failed_fields"] = failed

    return result


def extract_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Send image to Gemini Vision and extract structured receipt data.

    Returns the data contract dict.
    On API failure: retries once, then returns a minimal dict with all fields
    None and failed_fields=["all"] so the caller knows to prompt manual entry.
    """
    for attempt in range(2):
        try:
            raw = _call_gemini(image_bytes, mime_type)
            return _normalize(raw)
        except Exception as exc:
            logging.warning("Receipt extraction attempt %d failed: %s", attempt + 1, exc, exc_info=True)
            if attempt == 0:
                time.sleep(2)
            else:
                return dict(_EMPTY_RESULT)

    # Should not be reached, but ensures a return value in all paths
    return dict(_EMPTY_RESULT)


def format_extraction_for_display(data: dict) -> str:
    """Format extracted receipt data as a human-readable Telegram message string.

    Example output:
    Merchant: Whole Foods
    Date: Feb 28, 2026
    Subtotal: $45.20
    HST (13%): $5.88
    Tip: -
    Total: $51.08
    Category: Grocery

    Could not extract: tip amount, tip %
    """

    def fmt_amount(val) -> str:
        if val is None:
            return "\u2014"
        return f"${val:.2f}"

    def fmt_pct(val) -> str:
        if val is None:
            return ""
        return f" ({val:.0f}%)"

    merchant = data.get("merchant") or "\u2014"
    date_raw = data.get("date")
    currency = data.get("currency", "CAD")

    # Format date nicely if it looks like ISO format
    date_str = "\u2014"
    if date_raw:
        try:
            dt = datetime.strptime(date_raw, "%Y-%m-%d")
            date_str = dt.strftime("%b %-d, %Y")
        except ValueError:
            date_str = date_raw  # Fall back to raw string if not parseable

    subtotal = data.get("subtotal")
    hst_amount = data.get("hst_amount")
    hst_pct = data.get("hst_pct")
    tip_amount = data.get("tip_amount")
    tip_pct = data.get("tip_pct")
    total = data.get("total")
    category = data.get("category") or "\u2014"
    confidence = data.get("confidence", "low")
    failed_fields = data.get("failed_fields", [])

    hst_label = f"HST{fmt_pct(hst_pct)}"
    tip_label = f"Tip{fmt_pct(tip_pct)}"

    lines = [
        f"\U0001f4c4 Merchant: {merchant}",
        f"\U0001f4c5 Date: {date_str}",
        f"\U0001f4b0 Subtotal: {fmt_amount(subtotal)}",
        f"\U0001f9fe {hst_label}: {fmt_amount(hst_amount)}",
        f"\U0001f4a1 {tip_label}: {fmt_amount(tip_amount)}",
        f"\u2705 Total: {fmt_amount(total)}",
        f"\U0001f3f7\ufe0f Category: {category}",
    ]

    # Add currency note if not CAD
    if currency and currency != "CAD":
        lines.append(f"\U0001f4b1 Currency: {currency}")

    # Low confidence warning
    if confidence == "low":
        lines.append("")
        lines.append("\u26a0\ufe0f Low confidence \u2014 image may be blurry")

    # Failed fields warning
    if failed_fields == ["all"]:
        lines.append("")
        lines.append(
            "\u26a0\ufe0f Could not extract any data from this receipt. "
            "Please enter details manually."
        )
    elif failed_fields:
        readable = [_FIELD_LABELS.get(f, f) for f in failed_fields]
        lines.append("")
        lines.append(f"\u26a0\ufe0f Could not extract: {', '.join(readable)}")

    return "\n".join(lines)
