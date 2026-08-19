"""
Two layers of checking:
  1. EXACT duplicate: same vendor + same invoice_number seen before.
     This catches literal re-submission (same invoice uploaded twice,
     or a vendor billing the same invoice number again).
  2. FUZZY duplicate: same vendor + same amount within a few days,
     but a DIFFERENT invoice number. This catches "resubmitted under
     a new number" style duplicates that an exact match would miss.
"""

import hashlib
from datetime import date, timedelta


def _invoice_hash(vendor: str, invoice_number: str) -> str:
    key = f"{vendor.strip().lower()}|{invoice_number.strip().lower()}"
    return hashlib.sha256(key.encode()).hexdigest()


def check_exact_duplicate(vendor: str, invoice_number: str, history: list[dict]) -> tuple[bool, str | None]:
    target_hash = _invoice_hash(vendor, invoice_number)
    for past in history:
        past_hash = _invoice_hash(past["vendor_name"], past["invoice_number"])
        if past_hash == target_hash:
            return True, f"Invoice number '{invoice_number}' already exists for this vendor"
    return False, None


def check_fuzzy_duplicate(
    vendor: str,
    invoice_number: str,
    amount: float,
    invoice_date: date | None,
    history: list[dict],
    amount_tolerance: float = 1.0,   # exact amount match by default
    day_window: int = 5,
) -> tuple[bool, str | None]:
    if invoice_date is None:
        return False, None

    for past in history:
        if past["invoice_number"] == invoice_number:
            continue  # already covered by exact check
        if past["vendor_name"].strip().lower() != vendor.strip().lower():
            continue

        amount_match = abs(past["amount"] - amount) <= amount_tolerance
        if not amount_match:
            continue

        past_date = past.get("invoice_date")
        if past_date is None:
            continue
        if isinstance(past_date, str):  # converts date in string to math for calculation
            past_date = date.fromisoformat(past_date)

        if abs((past_date - invoice_date).days) <= day_window:
            return True, (
                f"Same vendor billed Rs.{amount:,.2f} on {past_date} under a different "
                f"invoice number ('{past['invoice_number']}') within {day_window} days"
            )

    return False, None
