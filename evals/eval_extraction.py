"""
Phase 9 — Extraction Agent evaluation.

Unlike Guardrails/Compliance/Risk, this genuinely needs the real
extraction pipeline — Groq/Gemini API calls and the actual PDF files —
so it can't run in a sandboxed environment. Run this on your machine
where GROQ_API_KEY / GEMINI_API_KEY are set and sample_invoices/ +
sample_invoices_scanned/ exist alongside this script's parent folder.

Computes field-level precision/recall against ground_truth.json:
  - vendor: exact string match
  - invoice_number: exact string match
  - amount: match within 1% (accounts for float rounding)
  - due_date: exact match
  - line_items: count match + per-item description/amount match
    (matched by position after sorting by amount, since extraction
    order may not exactly match ground truth order)
"""

import sys
import os
import json
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from agents.extraction_agent import extract_invoice

GROUND_TRUTH_PATH = os.path.join(os.path.dirname(__file__), "ground_truth.json")
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")


def fields_match(extracted, expected_entry) -> dict:
    """Returns a dict of field_name -> bool for each checked field."""
    scores = {}

    scores["vendor"] = extracted.vendor.strip().lower() == expected_entry["vendor"].strip().lower()
    scores["invoice_number"] = extracted.invoice_number.strip() == expected_entry["invoice_number"].strip()

    expected_amount = expected_entry["amount"]
    scores["amount"] = expected_amount != 0 and abs(extracted.amount - expected_amount) / expected_amount <= 0.01

    if extracted.due_date and expected_entry.get("due_date"):
        scores["due_date"] = extracted.due_date.isoformat() == expected_entry["due_date"]
    else:
        scores["due_date"] = extracted.due_date is None and expected_entry.get("due_date") is None

    expected_items = expected_entry["line_items"]
    scores["line_item_count"] = len(extracted.line_items) == len(expected_items)

    # Per-item match: sort both by amount, compare pairwise (order-agnostic)
    extracted_sorted = sorted(extracted.line_items, key=lambda li: li.amount)
    expected_sorted = sorted(expected_items, key=lambda li: li["amount"])
    item_matches = 0
    for e_li, gt_li in zip(extracted_sorted, expected_sorted):
        amount_ok = gt_li["amount"] != 0 and abs(e_li.amount - gt_li["amount"]) / gt_li["amount"] <= 0.01
        desc_ok = gt_li["description"].strip().lower() in e_li.description.strip().lower() or \
                  e_li.description.strip().lower() in gt_li["description"].strip().lower()
        if amount_ok and desc_ok:
            item_matches += 1
    scores["line_items_content"] = item_matches == len(expected_items) if expected_items else True

    return scores


def run():
    with open(GROUND_TRUTH_PATH) as f:
        ground_truth = json.load(f)

    field_names = ["vendor", "invoice_number", "amount", "due_date", "line_item_count", "line_items_content"]
    totals = {f: 0 for f in field_names}
    correct = {f: 0 for f in field_names}
    per_invoice_fully_correct = 0
    results = []
    errors = []

    for key, entry in ground_truth.items():
        path = os.path.join(PROJECT_ROOT, entry["source_file"])
        if not os.path.exists(path):
            errors.append(f"{key}: file not found at {path}")
            continue

        try:
            extracted = extract_invoice(path)
        except Exception as e:
            errors.append(f"{key}: extraction raised {e}")
            continue

        scores = fields_match(extracted, entry)
        for f in field_names:
            totals[f] += 1
            correct[f] += scores[f]

        all_correct = all(scores.values())
        per_invoice_fully_correct += all_correct

        results.append({"invoice": key, "scores": scores, "all_correct": all_correct,
                         "extraction_method": extracted.extraction_method})

    print(f"{'='*90}\nEXTRACTION EVAL\n{'='*90}")
    if errors:
        print(f"\n{len(errors)} invoice(s) could not be evaluated:")
        for e in errors:
            print(f"  - {e}")

    n = len(results)
    if n == 0:
        print("\nNo invoices were successfully processed — check API keys and file paths.")
        return

    print(f"\nPer-field accuracy (n={n}):")
    for f in field_names:
        acc = correct[f] / totals[f] if totals[f] else 0
        print(f"  {f:20s} {correct[f]:3d}/{totals[f]:3d}  ({acc*100:5.1f}%)")

    overall = per_invoice_fully_correct / n
    print(f"\nFully-correct invoices (ALL fields match): {per_invoice_fully_correct}/{n} ({overall*100:.1f}%)")

    print("\nBy extraction method:")
    by_method = {}
    for r in results:
        m = r["extraction_method"] or "unknown"
        by_method.setdefault(m, {"total": 0, "correct": 0})
        by_method[m]["total"] += 1
        by_method[m]["correct"] += r["all_correct"]
    for m, d in by_method.items():
        print(f"  {m:15s} {d['correct']}/{d['total']} fully correct")

    for r in results:
        if not r["all_correct"]:
            failed_fields = [f for f, ok in r["scores"].items() if not ok]
            print(f"\n[MISMATCH] {r['invoice']} ({r['extraction_method']}): failed on {failed_fields}")

    return {"agent": "extraction", "n": n, "field_accuracy": {f: correct[f]/totals[f] for f in field_names},
            "fully_correct": per_invoice_fully_correct, "overall_accuracy": overall, "results": results}


if __name__ == "__main__":
    run()