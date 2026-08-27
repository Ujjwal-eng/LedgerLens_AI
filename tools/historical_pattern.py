"""
Two parts, deliberately separated:
  - `fetch_vendor_history()` — the only part that talks to Supabase.
  - `compute_stats()` / `check_price_deviation()` — pure functions that
    take a plain list of past-invoice dicts. 
"""

import statistics
from typing import Optional


def fetch_vendor_history(
    supabase_client, vendor_name: str, user_id: str | None = None
) -> list[dict]:
    """Fetch this vendor's approved invoice history for the given user.
    Filters to the `invoices` table (the single source of truth) rather
    than the old separate `vendor_invoices` table so no data is siloed."""
    query = (
        supabase_client.table("invoices")
        .select("invoice,decision,created_at")
        .eq("decision->>status", "approved_exported")
    )
    if user_id:
        query = query.eq("user_id", user_id)
    rows = query.execute().data or []
    result = []
    for row in rows:
        inv = row.get("invoice")
        if not inv:
            continue
        v = inv.get("vendor") or inv.get("vendor_name") or ""
        if v.lower() != vendor_name.lower():
            continue
        result.append({
            "vendor_name": v,
            "invoice_number": inv.get("invoice_number") or "",
            "amount": inv.get("amount") or 0.0,
            "invoice_date": inv.get("invoice_date"),
            "created_at": row.get("created_at"),
        })
    return result


def save_processed_invoice(
    supabase_client, invoice_record: dict, user_id: str | None = None
) -> None:
    """No-op: the invoice is already persisted to the `invoices` table by
    api.py's _serialize() → upsert. The old separate `vendor_invoices`
    insert is removed so we don't write to a non-existent table and
    don't duplicate data. Risk history is derived from `invoices` via
    fetch_vendor_history() above."""
    # Kept as a hook in case a future migration needs it; nothing to do now.
    pass


def compute_stats(history: list[dict]) -> dict:
    amounts = [h["amount"] for h in history]
    count = len(amounts)

    if count == 0:
        return {"count": 0, "avg_amount": None, "std_amount": None}
    if count == 1:
        return {"count": 1, "avg_amount": amounts[0], "std_amount": 0.0}

    return {
        "count": count,
        "avg_amount": statistics.mean(amounts),
        "std_amount": statistics.stdev(amounts),
    }


def check_price_deviation(
    amount: float, avg_amount: Optional[float], std_amount: Optional[float],
    history_count: int = 0, threshold: float = 2.0, severe_multiplier: float = 2.0,
) -> tuple[bool, str, bool]:

    if avg_amount is None:
        return False, "no history to compare against", False

    if std_amount is None:
        return False, "insufficient history to compute deviation", False

    if std_amount == 0:
        if history_count < 2 or avg_amount == 0:
            return False, "insufficient history to compute deviation", False

        if amount == avg_amount:
            return False, f"matches vendor's consistent history (always Rs.{avg_amount:,.2f})", False

        relative_diff = abs(amount - avg_amount) / avg_amount
        detail = (
            f"Rs.{amount:,.2f} breaks this vendor's perfectly consistent history "
            f"(always exactly Rs.{avg_amount:,.2f}) — a {relative_diff * 100:.0f}% change with zero prior variance"
        )
        return True, detail, True  # any break from a zero-variance history is inherently severe

    deviation = abs(amount - avg_amount) / std_amount
    is_flagged = deviation > threshold
    is_severe = deviation > threshold * severe_multiplier

    if is_flagged:
        detail = (
            f"Rs.{amount:,.2f} is {deviation:.1f} standard deviations from this vendor's "
            f"average of Rs.{avg_amount:,.2f}"
        )
    else:
        detail = f"within normal range ({deviation:.1f} std devs from average)"

    return is_flagged, detail, is_severe