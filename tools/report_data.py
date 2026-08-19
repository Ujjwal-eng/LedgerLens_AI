from datetime import datetime
from tools.decision_logger import read_all_decisions


def fetch_approved_invoices(supabase_client, window_start: datetime, window_end: datetime) -> list[dict]:
    """All invoices persisted to vendor_invoices within the window,
    across ALL vendors (unlike historical_pattern.fetch_vendor_history,
    which filters to one vendor — reporting needs the whole picture)."""
    response = supabase_client.table("vendor_invoices").select("*").execute()
    rows = response.data or []

    def in_window(row):
        ts = row.get("created_at") or row.get("invoice_date") # searches for timestamp
        if not ts:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return window_start <= ts <= window_end

    return [r for r in rows if in_window(r)]


def fetch_decisions_in_window(window_start: datetime, window_end: datetime) -> list[dict]:
    """Every human decision (approve/reject/edit) logged within the window."""
    all_decisions = read_all_decisions()

    def in_window(entry):
        ts = datetime.fromisoformat(entry["timestamp"])
        return window_start <= ts <= window_end

    return [d for d in all_decisions if in_window(d)]