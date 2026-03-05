"""Tax rate lookup by timezone / region.

Returns the default sales tax percentage (e.g. 13.0 for Ontario HST).
Used during onboarding and /settings to pre-fill the household default.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canada — keyed by pytz timezone string
# ---------------------------------------------------------------------------
_CA: dict[str, tuple[str, float]] = {
    # Ontario: HST 13%
    "America/Toronto":          ("HST (Ontario) 13%",    13.0),
    "America/Nipigon":          ("HST (Ontario) 13%",    13.0),
    "America/Thunder_Bay":      ("HST (Ontario) 13%",    13.0),
    # British Columbia: GST 5% + PST 7% = 12%
    "America/Vancouver":        ("GST+PST (BC) 12%",     12.0),
    # Alberta: GST 5% only
    "America/Edmonton":         ("GST (Alberta) 5%",      5.0),
    "America/Calgary":          ("GST (Alberta) 5%",      5.0),
    # Quebec: GST 5% + QST 9.975% ≈ 14.975%
    "America/Montreal":         ("GST+QST (Quebec) 15%", 14.975),
    # Manitoba: GST 5% + PST 7% = 12%
    "America/Winnipeg":         ("GST+PST (Manitoba) 12%", 12.0),
    "America/Rainy_River":      ("GST+PST (Manitoba) 12%", 12.0),
    # Saskatchewan: GST 5% + PST 6% = 11%
    "America/Regina":           ("GST+PST (SK) 11%",     11.0),
    "America/Swift_Current":    ("GST+PST (SK) 11%",     11.0),
    # Nova Scotia: HST 15%
    "America/Halifax":          ("HST (Nova Scotia) 15%", 15.0),
    "America/Glace_Bay":        ("HST (Nova Scotia) 15%", 15.0),
    "America/Moncton":          ("HST (NB) 15%",         15.0),
    # Newfoundland: HST 15%
    "America/St_Johns":         ("HST (NL) 15%",         15.0),
    # PEI: HST 15%
    "America/St_Johns":         ("HST (PEI) 15%",        15.0),
    # Yukon / NWT / Nunavut: GST 5%
    "America/Whitehorse":       ("GST (Yukon) 5%",        5.0),
    "America/Yellowknife":      ("GST (NWT) 5%",          5.0),
    "America/Rankin_Inlet":     ("GST (Nunavut) 5%",      5.0),
}

# ---------------------------------------------------------------------------
# United States — common states
# ---------------------------------------------------------------------------
_US: dict[str, tuple[str, float]] = {
    # No sales tax
    "America/New_York":         ("NY State Tax 8%",       8.0),
    "America/Chicago":          ("IL State Tax 6.25%",    6.25),
    "America/Los_Angeles":      ("CA State Tax 7.25%",    7.25),
    "America/Denver":           ("CO State Tax 2.9%",     2.9),
    "America/Phoenix":          ("AZ State Tax 5.6%",     5.6),
    "America/Detroit":          ("MI State Tax 6%",       6.0),
    "America/Indiana/Indianapolis": ("IN State Tax 7%",   7.0),
    "America/Kentucky/Louisville": ("KY State Tax 6%",    6.0),
    "America/Boise":            ("ID State Tax 6%",       6.0),
    "America/Anchorage":        ("No state tax (AK) 0%",  0.0),
    "Pacific/Honolulu":         ("HI GET 4%",             4.0),
}

# Merge all
_ALL: dict[str, tuple[str, float]] = {**_CA, **_US}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Canonical timezone list for /settings (grouped for buttons)
CANADA_TIMEZONES: list[tuple[str, str]] = [
    ("America/Toronto",    "🍁 Ontario (HST 13%)"),
    ("America/Vancouver",  "🍁 BC (12%)"),
    ("America/Edmonton",   "🍁 Alberta (GST 5%)"),
    ("America/Montreal",   "🍁 Quebec (15%)"),
    ("America/Winnipeg",   "🍁 Manitoba (12%)"),
    ("America/Regina",     "🍁 Saskatchewan (11%)"),
    ("America/Halifax",    "🍁 Nova Scotia/NB (HST 15%)"),
    ("America/St_Johns",   "🍁 Newfoundland (HST 15%)"),
    ("America/Whitehorse", "🍁 Yukon/NWT (GST 5%)"),
]

US_TIMEZONES: list[tuple[str, str]] = [
    ("America/New_York",      "🇺🇸 New York (~8%)"),
    ("America/Chicago",       "🇺🇸 Chicago/Illinois (6.25%)"),
    ("America/Los_Angeles",   "🇺🇸 California (7.25%)"),
    ("America/Denver",        "🇺🇸 Colorado (2.9%)"),
    ("America/Phoenix",       "🇺🇸 Arizona (5.6%)"),
    ("America/Detroit",       "🇺🇸 Michigan (6%)"),
    ("America/Anchorage",     "🇺🇸 Alaska (0%)"),
]

OTHER_TIMEZONES: list[tuple[str, str]] = [
    ("Europe/London",     "🇬🇧 UK (VAT 20%)"),
    ("Europe/Paris",      "🇪🇺 France (TVA 20%)"),
    ("Asia/Singapore",    "🇸🇬 Singapore (GST 9%)"),
    ("Asia/Hong_Kong",    "🇭🇰 Hong Kong (0%)"),
    ("Asia/Shanghai",     "🇨🇳 China (VAT 13%)"),
    ("Australia/Sydney",  "🇦🇺 Australia (GST 10%)"),
    ("UTC",               "🌐 Other / No tax"),
]

# Add Europe/Asia/Oceania to _ALL
_EXTRA: dict[str, tuple[str, float]] = {
    "Europe/London":    ("VAT (UK) 20%",        20.0),
    "Europe/Paris":     ("TVA (France) 20%",     20.0),
    "Asia/Singapore":   ("GST (SG) 9%",           9.0),
    "Asia/Hong_Kong":   ("No tax (HK) 0%",         0.0),
    "Asia/Shanghai":    ("VAT (China) 13%",       13.0),
    "Australia/Sydney": ("GST (AU) 10%",          10.0),
    "UTC":              ("No tax 0%",              0.0),
}
_ALL.update(_EXTRA)


def get_tax_rate(timezone: str) -> tuple[str, float]:
    """
    Return (label, pct) for a timezone.
    Falls back to ("No tax 0%", 0.0) if unknown.
    """
    return _ALL.get(timezone, ("No tax 0%", 0.0))


def tax_pct_for_timezone(timezone: str) -> float:
    """Return just the tax percentage float for a given timezone."""
    return get_tax_rate(timezone)[1]
