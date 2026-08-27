r"""
Standalone entry point for the Reporting Agent — run this on a schedule,
completely separate from approval_cli.py / the main graph. This
separation is what makes Reporting a genuinely independent agent: it
doesn't get invoked as part of processing any single invoice, it runs
on its own clock and looks backward at what already happened.

Output: printed to console AND appended to data/reports/<user_id>.jsonl,
so each user has their own running history of digests.

"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

from agents.reporting_agent import ReportingAgent
from tools.routing_logger import compute_routing_stats

# Per-user reports are saved as data/reports/<user_id>.jsonl
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "data", "reports")


def get_supabase_client():
    # NOTE: this must be the SERVICE ROLE key, not SUPABASE_KEY (that's the
    # separate vendor-memory client api.py's graph reads from). Listing every
    # user's invoices (fetch_all_user_ids) and reading across users requires
    # bypassing RLS, exactly like api.py's _get_supabase_service_client() does
    # for /api/report/weekly. Using the plain anon/vendor key here will either
    # error out or silently return zero rows depending on your RLS policies.
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("[error] SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set — cannot generate a report without them.")
        sys.exit(1)
    from supabase import create_client
    return create_client(url, key)


def fetch_all_user_ids(client) -> list[str]:
    """Return every distinct user_id that has at least one invoice row in Supabase."""
    response = client.table("invoices").select("user_id").execute()
    rows = response.data or []
    seen = set()
    user_ids = []
    for row in rows:
        uid = row.get("user_id")
        if uid and uid not in seen:
            seen.add(uid)
            user_ids.append(uid)
    return user_ids


def run_once_for_user(
    client,
    user_id: str,
    weekly: bool = False,
    now: datetime | None = None,
) -> None:
    """Generate and persist a daily/weekly digest + routing stats for a single user."""
    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=7 if weekly else 1)

    agent = ReportingAgent()
    digest = agent.run(window_start, now, client, user_id=user_id)

    # ── Routing stats scoped to this user ──────────────────────────────────
    routing = compute_routing_stats(user_id=user_id)

    label = "WEEKLY" if weekly else "DAILY"
    separator = "=" * 70
    print(f"\n{separator}")
    print(f"{label} REPORT — user: {user_id}")
    print(f"Window: {window_start.date()} → {now.date()}")
    print(separator)
    print(digest.narrative)
    print(
        f"\nAuto-approved: {digest.total_auto_approved} | "
        f"Human-approved: {digest.total_human_approved} | "
        f"Rejected: {digest.total_rejected} | "
        f"Flagged: {digest.total_flagged}"
    )
    print(f"Total approved amount: Rs.{digest.total_amount_approved:,.2f}")

    if digest.vendor_breakdown:
        print("\nBy vendor:")
        for v in digest.vendor_breakdown:
            print(f"  {v.vendor}: {v.invoice_count} invoice(s), Rs.{v.total_amount:,.2f}")

    # ── Routing stats ───────────────────────────────────────────────────────
    if routing.get("total", 0) > 0:
        print(
            f"\nRouting (this user): {routing['total']} extraction(s) — "
            f"Groq {routing['groq_pct']:.1f}% | "
            f"Gemini {routing['gemini_pct']:.1f}% | "
            f"Fallbacks {routing['fallback_pct']:.1f}%"
        )
    else:
        print("\nRouting: no extraction events recorded for this user yet.")

    # ── Persist to per-user JSONL ───────────────────────────────────────────
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_path = os.path.join(REPORTS_DIR, f"{user_id}.jsonl")
    record = digest.model_dump()
    record["routing_stats"] = routing
    record["report_type"] = label.lower()
    with open(report_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    print(f"[saved → {report_path}]")


def run_once(weekly: bool = False, user_id: str | None = None) -> None:
    """Generate reports for one specific user or all users in Supabase."""
    now = datetime.now(timezone.utc)
    client = get_supabase_client()

    if user_id:
        # Single user explicitly requested via --user-id flag
        run_once_for_user(client, user_id, weekly=weekly, now=now)
    else:
        # All users — fetch distinct user_ids from the invoices table
        user_ids = fetch_all_user_ids(client)
        if not user_ids:
            print("[warn] No users found in the invoices table — nothing to report.")
            return
        print(f"[info] Generating {'weekly' if weekly else 'daily'} reports for {len(user_ids)} user(s)...")
        for uid in user_ids:
            try:
                run_once_for_user(client, uid, weekly=weekly, now=now)
            except Exception as exc:
                print(f"[error] Failed to generate report for user {uid}: {exc}")


def run_loop():
    """Cross-platform alternative to cron/Task Scheduler — keeps running
    and fires the daily report at 08:00 local time. Needs `pip install
    schedule`. Intended for a machine that stays on; for anything
    production-grade, use cron or Task Scheduler instead."""
    import schedule
    import time

    schedule.every().day.at("08:00").do(run_once, weekly=False)
    print("Scheduled: daily per-user report at 08:00. Leave this running... (Ctrl+C to stop)")
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate per-user invoice processing reports from Supabase."
    )
    parser.add_argument(
        "--weekly", action="store_true",
        help="Generate a 7-day digest instead of daily",
    )
    parser.add_argument(
        "--user-id", dest="user_id", default=None,
        help="Scope the report to a single user_id (omit to run for all users)",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Run forever, firing daily at 08:00 for all users (needs 'schedule' package)",
    )
    args = parser.parse_args()

    if args.loop:
        run_loop()
    else:
        run_once(weekly=args.weekly, user_id=args.user_id)