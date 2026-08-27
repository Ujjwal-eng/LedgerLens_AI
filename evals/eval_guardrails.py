"""
Phase 9 — Guardrail evaluation.

Fully offline, no API needed: builds an Invoice object directly from
each ground_truth.json entry (skipping PDF extraction, since guardrails
operate on already-extracted data) and runs the REAL run_guardrails()
against it. Compares pass/blocked against the known-correct expectation.
"""

import sys
import os
import json
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema import Invoice, LineItem
from tools.guardrails import run_guardrails

GROUND_TRUTH_PATH = os.path.join(os.path.dirname(__file__), "ground_truth.json")


def build_invoice(entry: dict) -> Invoice:
    return Invoice(
        vendor=entry["vendor"],
        invoice_number=entry["invoice_number"],
        invoice_date=date.fromisoformat(entry["invoice_date"]),
        due_date=date.fromisoformat(entry["due_date"]),
        amount=entry["amount"],
        line_items=[LineItem(**li) for li in entry["line_items"]],
    )


def run():
    with open(GROUND_TRUTH_PATH) as f:
        ground_truth = json.load(f)

    total = len(ground_truth)
    correct = 0
    results = []

    for key, entry in ground_truth.items():
        invoice = build_invoice(entry)
        passed, violations = run_guardrails(invoice)
        actual_status = "pass" if passed else "blocked"
        expected_status = entry["expected_guardrail_status"]

        is_correct = actual_status == expected_status
        correct += is_correct
        results.append({
            "invoice": key, "expected": expected_status, "actual": actual_status,
            "correct": is_correct, "violations": [v[0] for v in violations],
        })

    accuracy = correct / total if total else 0

    print(f"{'='*90}\nGUARDRAIL EVAL — {correct}/{total} correct ({accuracy*100:.1f}%)\n{'='*90}")
    for r in results:
        mark = "OK  " if r["correct"] else "FAIL"
        print(f"[{mark}] {r['invoice']:45s} expected={r['expected']:8s} actual={r['actual']:8s}"
              f"{'  violations=' + str(r['violations']) if r['violations'] else ''}")

    return {"agent": "guardrails", "total": total, "correct": correct, "accuracy": accuracy, "results": results}


if __name__ == "__main__":
    run()