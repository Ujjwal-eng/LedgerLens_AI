import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.report_schema import ReportDigest, VendorBreakdown
from tools.report_data import fetch_approved_invoices, fetch_decisions_in_window


class ReportingAgent:
    def compute_stats(
        self, window_start: datetime, window_end: datetime,
        approved_invoices: list[dict], decisions: list[dict],
    ) -> ReportDigest:
        # Auto-approvals never touch the decision log (no human involved),
        # so: any approved invoice backed by an "approve" decision entry
        # was human-approved; everything else was auto-approved.
        #
        # NOTE: invoice_number is a business document field, not a unique
        # key for a processing run — the same PDF (e.g. a reused sample
        # invoice) can be run multiple times and always extracts the same
        # invoice_number. Matching via plain set membership meant ONE
        # human "approve" decision would mark EVERY approved invoice that
        # happens to share that invoice_number as human-approved, even
        # ones that were actually auto-approved in a different run. Using
        # a counter consumes each decision exactly once so a single
        # approval can only account for a single invoice.
        approve_counts = Counter(d["invoice_number"] for d in decisions if d["action"] == "approve")
        total_human_approved = 0
        for inv in approved_invoices:
            number = inv["invoice_number"]
            if approve_counts.get(number, 0) > 0:
                approve_counts[number] -= 1
                total_human_approved += 1
        total_auto_approved = len(approved_invoices) - total_human_approved

        total_rejected = len({d["invoice_number"] for d in decisions if d["action"] == "reject"})
        total_flagged = len({d["invoice_number"] for d in decisions})  # anything a human ever saw

        total_amount_approved = sum(inv["amount"] for inv in approved_invoices)
        total_processed = len(approved_invoices) + total_rejected

        vendor_totals: dict[str, dict] = {}
        for inv in approved_invoices:
            v = inv["vendor_name"]
            vendor_totals.setdefault(v, {"count": 0, "amount": 0.0})
            vendor_totals[v]["count"] += 1
            vendor_totals[v]["amount"] += inv["amount"]

        vendor_breakdown = [
            VendorBreakdown(vendor=v, invoice_count=data["count"], total_amount=data["amount"])
            for v, data in sorted(vendor_totals.items(), key=lambda kv: -kv[1]["amount"])
        ]

        return ReportDigest(
            window_start=window_start,
            window_end=window_end,
            total_processed=total_processed,
            total_auto_approved=total_auto_approved,
            total_human_approved=total_human_approved,
            total_rejected=total_rejected,
            total_flagged=total_flagged,
            total_amount_approved=total_amount_approved,
            vendor_breakdown=vendor_breakdown,
        )

    def generate_narrative(self, digest: ReportDigest) -> str:
        """The one LLM call in this agent. Falls back to a manual
        f-string digest if Groq isn't configured/reachable, so the
        report is still usable without an API key."""
        try:
            from groq import Groq
            client = Groq(api_key=os.environ["GROQ_API_KEY"])

            prompt = (
                f"Write a short (2-3 sentence) plain-English digest of this invoice "
                f"processing summary. Be factual, no fluff:\n\n"
                f"Window: {digest.window_start.date()} to {digest.window_end.date()}\n"
                f"Total processed: {digest.total_processed}\n"
                f"Auto-approved: {digest.total_auto_approved}\n"
                f"Human-approved: {digest.total_human_approved}\n"
                f"Rejected: {digest.total_rejected}\n"
                f"Flagged for human review: {digest.total_flagged}\n"
                f"Total approved amount: Rs.{digest.total_amount_approved:,.2f}\n"
                f"Top vendors: {', '.join(v.vendor for v in digest.vendor_breakdown[:3])}"
            )
            response = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"[warn] LLM narrative generation unavailable ({e}), using fallback digest.")
            return (
                f"{digest.total_processed} invoices processed "
                f"({digest.total_auto_approved} auto-approved, {digest.total_human_approved} human-approved, "
                f"{digest.total_rejected} rejected), {digest.total_flagged} flagged for review, "
                f"Rs.{digest.total_amount_approved:,.2f} total approved."
            )

    def run(
        self,
        window_start: datetime,
        window_end: datetime,
        supabase_client,
        user_id: str | None = None,
    ) -> ReportDigest:
        approved_invoices = fetch_approved_invoices(
            supabase_client, window_start, window_end, user_id=user_id
        )
        decisions = fetch_decisions_in_window(
            window_start, window_end,
            supabase_client=supabase_client,
            user_id=user_id,
        )

        digest = self.compute_stats(window_start, window_end, approved_invoices, decisions)
        digest.narrative = self.generate_narrative(digest)
        return digest


if __name__ == "__main__":
    # Quick manual smoke test with a mock client
    from tools.mock_supabase import MockSupabaseClient

    client = MockSupabaseClient()
    now = datetime.now(timezone.utc)
    client.table("vendor_invoices").insert({
        "vendor_name": "Test Vendor", "invoice_number": "T001", "amount": 5000,
        "invoice_date": now.isoformat(), "created_at": now.isoformat(),
    }).execute()

    agent = ReportingAgent()
    digest = agent.run(now - timedelta(days=1), now + timedelta(days=1), client)
    print(digest.model_dump_json(indent=2))