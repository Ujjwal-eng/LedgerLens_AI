"""
Phase 9 — Risk Agent evaluation.

Unlike Guardrails/Compliance, Risk is history-dependent — the same
invoice can be "clean" or "flagged" depending on what was processed
before it. This eval is deliberately SCOPED to invoices that were
engineered for a specific, unambiguous risk outcome (duplicates,
first-time vendor, a controlled price outlier) — not the organically
random Sharma baseline invoices (01-10), where predicting exact
std-deviation flags by hand would be guesswork, not ground truth.

Each vendor gets a fresh, isolated history (separate MockSupabaseClient
instances aren't used here — instead each vendor's sequence starts with
an empty history list, mirroring test_memory.py's approach) so results
don't depend on dict ordering or cross-vendor interference.
"""

import sys
import os
import json
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema import Invoice, LineItem
from agents.risk_agent import RiskAgent

GROUND_TRUTH_PATH = os.path.join(os.path.dirname(__file__), "ground_truth.json")

# (invoice_key, expected_flags) in processing order, grouped by vendor —
# each vendor's list shares one running history, separate from the others.
RISK_SCENARIOS = {
    "Nair Office Supplies Co.": [
        ("NOS_01_clean_autoapprove", {"first_time_vendor"}),
    ],
    "Kapoor Logistics & Freight Pvt. Ltd.": [
        ("KLF_01_clean_baseline", {"first_time_vendor"}),
        ("KLF_02_EXACT_duplicate", {"duplicate_invoice"}),
    ],
    "Reddy Legal & Compliance Advisors": [
        ("RLC_01_normal_history", {"first_time_vendor"}),
        ("RLC_02_normal_history", set()),
        ("RLC_03_normal_history", set()),
        ("RLC_04_normal_history", set()),
        ("RLC_05_price_deviation_outlier", {"price_deviation", "severe_price_deviation"}),
    ],
    "Sharma Digital Solutions Pvt. Ltd. (duplicate test)": [
        ("invoice_01_SDS-2026-27-101", {"first_time_vendor"}),
        ("invoice_11_SDS-2026-27-101", {"duplicate_invoice"}),
        ("SDS_13_EXACT_duplicate_of_invoice01", {"duplicate_invoice"}),
    ],
}


def build_invoice(entry: dict) -> Invoice:
    return Invoice(
        vendor=entry["vendor"], invoice_number=entry["invoice_number"],
        invoice_date=date.fromisoformat(entry["invoice_date"]), due_date=date.fromisoformat(entry["due_date"]),
        amount=entry["amount"], line_items=[LineItem(**li) for li in entry["line_items"]],
    )


def run():
    with open(GROUND_TRUTH_PATH) as f:
        ground_truth = json.load(f)

    agent = RiskAgent()
    total = 0
    correct = 0
    results = []

    for group_label, sequence in RISK_SCENARIOS.items():
        history: list[dict] = []
        for key, expected_flags in sequence:
            entry = ground_truth[key]
            invoice = build_invoice(entry)
            result = agent.assess(invoice, history)
            actual_flags = {f.flag for f in result.flags}

            # Exact-set match: nothing unexpected fired, and nothing expected was missed.
            is_correct = actual_flags == expected_flags

            total += 1
            correct += is_correct
            results.append({
                "invoice": key, "expected": sorted(expected_flags), "actual": sorted(actual_flags),
                "correct": is_correct, "risk_score": result.risk_score,
            })

            # Only persist to history if this invoice would genuinely have
            # been approved (mirrors persist_invoice_node's real behavior —
            # we don't learn from duplicates/rejects as "normal" pattern)
            if "duplicate_invoice" not in actual_flags:
                history.append({
                    "vendor_name": invoice.vendor, "invoice_number": invoice.invoice_number,
                    "amount": invoice.amount, "invoice_date": invoice.invoice_date.isoformat(),
                })

    accuracy = correct / total if total else 0

    print(f"{'='*90}\nRISK EVAL — {correct}/{total} correct ({accuracy*100:.1f}%)\n{'='*90}")
    for r in results:
        mark = "OK  " if r["correct"] else "FAIL"
        print(f"[{mark}] {r['invoice']:45s} expected={r['expected']} actual={r['actual']} ({r['risk_score']})")

    return {"agent": "risk", "total": total, "correct": correct, "accuracy": accuracy, "results": results}


if __name__ == "__main__":
    run()