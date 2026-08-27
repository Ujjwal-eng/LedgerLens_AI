from datetime import datetime


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return ts


def _decision_time(row: dict) -> datetime | None:
    """Reports care about when an invoice was *decided* (approved/
    rejected), not when it was first uploaded — those can be days
    apart if an invoice sits staged before someone acts on it.
    decided_at is stamped once, only when a decision turns terminal
    (see api.py's _serialize()). Falls back to created_at for legacy
    rows written before that column existed, so old data doesn't just
    vanish from every report."""
    return _parse_ts(row.get("decided_at")) or _parse_ts(row.get("created_at"))


def fetch_approved_invoices(
    supabase_client,
    window_start: datetime,
    window_end: datetime,
    user_id: str | None = None,
) -> list[dict]:
    """All invoices persisted to the `invoices` table whose *decision*
    (not upload) falls within the window for the given user (reporting
    needs the whole per-user picture). Each row's `invoice` field is a
    JSONB blob — we normalise it into a flat dict with `vendor_name` and
    `amount` so the reporting agent can work with it unchanged."""
    query = supabase_client.table("invoices").select("*")
    if user_id:
        query = query.eq("user_id", user_id)
    # Only include rows where the invoice was actually approved (decision = 'approve')
    # and extraction succeeded (invoice column is not null).
    response = query.execute()
    rows = response.data or []

    def in_window(row: dict) -> bool:
        ts = _decision_time(row)
        if ts is None:
            return False
        return window_start <= ts <= window_end

    def to_invoice_dict(row: dict) -> dict | None:
        """Flatten the JSONB `invoice` blob + decision into the shape
        ReportingAgent.compute_stats() expects: vendor_name, amount,
        invoice_number.

        The `decision` column is a JSONB dict:
            {"status": "approved_exported"|"rejected"|..., "reasons": [...]}
        Only include rows where the invoice was actually approved.
        """
        inv = row.get("invoice")
        if not inv:
            return None
        raw_decision = row.get("decision")
        if isinstance(raw_decision, dict):
            decision_status = raw_decision.get("status", "")
        else:
            # Fallback: plain string (legacy rows or tests)
            decision_status = str(raw_decision or "")
        if decision_status != "approved_exported":
            return None
        return {
            "vendor_name": inv.get("vendor") or inv.get("vendor_name") or "",
            "invoice_number": inv.get("invoice_number") or row.get("thread_id") or "",
            "amount": inv.get("amount") or 0.0,
            "invoice_date": inv.get("invoice_date"),
            "created_at": row.get("created_at"),
        }

    results = []
    for row in rows:
        if not in_window(row):
            continue
        d = to_invoice_dict(row)
        if d is not None:
            results.append(d)
    return results


def fetch_decisions_in_window(
    window_start: datetime,
    window_end: datetime,
    supabase_client=None,
    user_id: str | None = None,
) -> list[dict]:
    """Every human decision (approve/reject/edit) logged within the window
    for the given user, read directly from the `invoices` Supabase table
    (which is the authoritative per-user store) instead of a server-wide
    flat file."""
    if supabase_client is not None and user_id:
        query = (
            supabase_client.table("invoices")
            .select("thread_id,invoice,decision,created_at,decided_at")
            .eq("user_id", user_id)
            .not_.is_("decision", "null")
        )
        rows = query.execute().data or []
        results = []
        for row in rows:
            ts = _decision_time(row)
            if ts is None:
                continue
            if not (window_start <= ts <= window_end):
                continue
            inv = row.get("invoice") or {}
            raw_dec = row.get("decision")
            if isinstance(raw_dec, dict): # isinstance is for extracting status from decision objects
                dec_status = raw_dec.get("status", "")
            else:
                dec_status = str(raw_dec or "")
            # Map graph decision statuses to the action labels
            # ReportingAgent.compute_stats() checks for.
            #
            # IMPORTANT: compute_stats() assumes this list contains only
            # HUMAN decisions — "auto-approvals never touch the decision
            # log". An approved_exported invoice is human-approved only
            # if its reasons do NOT mention auto-approval. The export
            # node stamps "Auto-approved by Supervisor …" for auto and
            # "Approved by human reviewer" for human; we check the
            # reasons to tell them apart.
            if dec_status == "approved_exported":
                reasons = raw_dec.get("reasons", []) if isinstance(raw_dec, dict) else []
                is_auto = any("auto-approved" in r.lower() for r in reasons)
                action = "auto_approve" if is_auto else "approve"
            elif dec_status == "rejected":
                action = "reject"
            elif dec_status == "escalate_to_human":
                action = "escalate"
            else:
                action = dec_status
            results.append({
                "timestamp": ts.isoformat(),
                "invoice_number": inv.get("invoice_number") or row.get("thread_id") or "",
                "vendor": inv.get("vendor") or inv.get("vendor_name") or "",
                "action": action,
                "detail": {},
            })
        return results

    # Fallback: read the legacy local JSONL log (kept for backward-compat
    # with old test scripts that don't have a Supabase client).
    try:
        from tools.decision_logger import read_all_decisions
        all_decisions = read_all_decisions()
    except Exception:
        return []

    def in_window(entry: dict) -> bool:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            return window_start <= ts <= window_end
        except (KeyError, ValueError):
            return False

    return [d for d in all_decisions if in_window(d)]