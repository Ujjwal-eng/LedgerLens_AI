"""
It only cares about fraud/anomaly signals, using the vendor's own billing history.

Flags checked:
  - First-time vendor (no history at all — not necessarily bad, but worth
    a closer look before auto-approving)
  - Price deviation beyond 2 standard deviations from vendor's average
    (and auto-escalates straight to HIGH if it's beyond 4 std devs)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema import Invoice
from core.risk_schema import RiskResult, RiskFlag
from tools.duplicate_detector import check_exact_duplicate, check_fuzzy_duplicate
from tools.historical_pattern import compute_stats, check_price_deviation, fetch_vendor_history


DEVIATION_THRESHOLD = 2.0          # flag at all beyond this many std devs
SEVERE_DEVIATION_MULTIPLIER = 2.0  # auto-escalate to HIGH beyond threshold * this


class RiskAgent:
    def assess(self, invoice: Invoice, history: list[dict]) -> RiskResult:
        flags: list[RiskFlag] = []
        severe_deviation = False

        # Duplicate checks
        is_exact_dup, exact_detail = check_exact_duplicate(invoice.vendor, invoice.invoice_number, history)
        if is_exact_dup:
            flags.append(RiskFlag(flag="duplicate_invoice", detail=exact_detail))

        if not is_exact_dup:
            is_fuzzy_dup, fuzzy_detail = check_fuzzy_duplicate(
                invoice.vendor, invoice.invoice_number, invoice.amount, invoice.invoice_date, history
            )
            if is_fuzzy_dup:
                flags.append(RiskFlag(flag="possible_duplicate", detail=fuzzy_detail))

        # First-time vendor
        stats = compute_stats(history)
        if stats["count"] == 0:
            flags.append(RiskFlag(
                flag="first_time_vendor",
                detail="No prior invoices on file for this vendor — nothing to compare against yet",
            ))

        # Price deviation (only meaningful once there's enough history)
        is_deviant, deviation_detail, is_severe = check_price_deviation(
            invoice.amount, stats["avg_amount"], stats["std_amount"],
            history_count=stats["count"], threshold=DEVIATION_THRESHOLD,
            severe_multiplier=SEVERE_DEVIATION_MULTIPLIER,
        )
        if is_deviant:
            flags.append(RiskFlag(flag="price_deviation", detail=deviation_detail))
            if is_severe:
                severe_deviation = True
                flags.append(RiskFlag(
                    flag="severe_price_deviation",
                    detail="Deviation is severe enough to auto-escalate regardless of other flags",
                ))

        risk_score = self._score(flags, severe_deviation)

        return RiskResult(
            vendor=invoice.vendor,
            invoice_number=invoice.invoice_number,
            risk_score=risk_score,
            flags=flags,
            vendor_avg_amount=stats["avg_amount"],
            vendor_std_amount=stats["std_amount"],
            vendor_invoice_count=stats["count"],
        )

    @staticmethod
    def _score(flags: list[RiskFlag], severe_deviation: bool = False) -> str:
        flag_names = {f.flag for f in flags}

        # Exact duplicates are always high risk, full stop.
        if "duplicate_invoice" in flag_names:
            return "high"

        # An extreme price deviation is dangerous on its own — doesn't
        # need a second unrelated flag to justify HIGH.
        if severe_deviation:
            return "high"

        # Otherwise, two or more softer signals together escalate to high.
        soft_flags = flag_names & {"possible_duplicate", "price_deviation", "first_time_vendor"}
        if len(soft_flags) >= 2:
            return "high"
        if len(soft_flags) == 1:
            return "medium"
        return "low"

    def assess_live(self, invoice: Invoice, supabase_client) -> RiskResult:
        """Production entry point: fetches real history from Supabase,
        then delegates to the pure assess() method above."""
        history = fetch_vendor_history(supabase_client, invoice.vendor)
        return self.assess(invoice, history)