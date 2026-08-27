"""
Logs every human decision made at the approval queue — approve, reject,
or edit. Backed by the `invoices` Supabase table (scoped to user_id) so
decisions are persistent across restarts and isolated per user.

Reading decisions:
  - Primary:  read_decisions_from_supabase(supabase_client, user_id)
              Returns rows from the `invoices` table for the given user.
  - Fallback: read_all_decisions()
              Reads the legacy local JSONL file. Used by old test scripts
              that don't have a Supabase client. No new writes go here.

Writing decisions:
  - Decisions are written to Supabase via api.py's _serialize() → upsert
    into the `invoices` table. log_decision() below is kept only as a
    backward-compat shim for test_human_loop.py and similar scripts.
"""

import json
import os
from datetime import datetime, timezone

# Legacy flat-file path — kept for read_all_decisions() backward compat.
_LEGACY_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "human_decisions.jsonl")


# ---------------------------------------------------------------------------
# Supabase-backed read  (primary — per-user, persistent)
# ---------------------------------------------------------------------------

def read_decisions_from_supabase(
    supabase_client,
    user_id: str | None = None,
) -> list[dict]:
    """Read all human decisions for the given user from the `invoices` table.

    Returns a list of dicts with keys:
        timestamp, invoice_number, vendor, action, detail
    where `action` is one of: "approve" | "reject" | "edit" | "escalate".
    """
    query = (
        supabase_client.table("invoices")
        .select("thread_id,invoice,decision,created_at")
        .not_.is_("decision", "null")
    )
    if user_id:
        query = query.eq("user_id", user_id)

    rows = query.execute().data or []
    results = []
    for row in rows:
        inv = row.get("invoice") or {}
        raw_dec = row.get("decision")
        if isinstance(raw_dec, dict):
            dec_status = raw_dec.get("status", "")
        else:
            dec_status = str(raw_dec or "")

        # Map graph decision statuses → human-readable action labels
        action_map = {
            "approved_exported": "approve",
            "rejected": "reject",
            "escalate_to_human": "escalate",
        }
        action = action_map.get(dec_status, dec_status)
        results.append({
            "timestamp": row.get("created_at", ""),
            "invoice_number": inv.get("invoice_number") or row.get("thread_id") or "",
            "vendor": inv.get("vendor") or inv.get("vendor_name") or "",
            "action": action,
            "detail": {},
        })
    return results


# ---------------------------------------------------------------------------
# Legacy file-based helpers  (kept for backward compat — no new writes)
# ---------------------------------------------------------------------------

def log_decision(
    invoice_number: str,
    vendor: str,
    action: str,
    detail: dict | None = None,
    supabase_client=None,
    user_id: str | None = None,
) -> dict:
    """Backward-compat shim. Decisions are authoritative in Supabase (written
    by api.py). When a Supabase client is available the entry is a no-op here
    (api.py already owns the write). Falls back to the legacy JSONL file only
    when there is no Supabase client (e.g. standalone test scripts)."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "invoice_number": invoice_number,
        "vendor": vendor,
        "action": action,          # "approve" | "reject" | "edit"
        "detail": detail or {},
    }

    if supabase_client is not None:
        # Authoritative write handled by api.py; nothing to do here.
        return entry

    # Fallback: write to legacy JSONL log for tests / CLI tools.
    os.makedirs(os.path.dirname(_LEGACY_LOG_PATH), exist_ok=True)
    with open(_LEGACY_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_all_decisions() -> list[dict]:
    """Read from the legacy JSONL file. Used by test_human_loop.py and
    report_data.py's fallback path when no Supabase client is present."""
    if not os.path.exists(_LEGACY_LOG_PATH):
        return []
    with open(_LEGACY_LOG_PATH, "r") as f:
        return [json.loads(line) for line in f if line.strip()]
