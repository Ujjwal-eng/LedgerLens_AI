import sys
import os
import json
from datetime import date
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema import Invoice, LineItem
from agents.compliance_agent import ComplianceAgent

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

    agent = ComplianceAgent()
    total = 0
    correct = 0
    results = []

    with patch("agents.compliance_agent.retrieve_relevant_clause", return_value=[("mocked clause", 0.7)]):
        for key, entry in ground_truth.items():
            expected_status = entry["expected_compliance_status"]
            if expected_status is None:
                continue  # guardrail-blocked invoices never reach Compliance — not scoreable here

            total += 1
            invoice = build_invoice(entry)
            result = agent.check(invoice)
            actual_status = result.overall_status

            is_correct = actual_status == expected_status
            correct += is_correct
            results.append({
                "invoice": key, "expected": expected_status, "actual": actual_status,
                "correct": is_correct,
                "flagged_checks": [c.field for c in result.checks if c.status in ("fail", "warning")],
            })

    accuracy = correct / total if total else 0

    print(f"{'='*90}\nCOMPLIANCE EVAL — {correct}/{total} correct ({accuracy*100:.1f}%)\n{'='*90}")
    for r in results:
        mark = "OK  " if r["correct"] else "FAIL"
        print(f"[{mark}] {r['invoice']:45s} expected={str(r['expected']):8s} actual={r['actual']:8s}"
              f"{'  flagged=' + str(r['flagged_checks']) if r['flagged_checks'] else ''}")

    return {"agent": "compliance", "total": total, "correct": correct, "accuracy": accuracy, "results": results}


if __name__ == "__main__":
    run()