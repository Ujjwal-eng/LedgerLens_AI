"""
Guardrails — Phase 8

Hard, code-level checks on EXTRACTED data, run immediately after
Extraction and BEFORE Compliance or Risk ever see the invoice. The
point: these don't ask "does an agent think this looks right" — they
verify basic arithmetic and sanity that should hold regardless of what
any LLM concluded. If the numbers don't add up, nothing downstream
should be trusted to reason about them.

Uses the same CGST/SGST 9%+9% assumption as edit_invoice_node's
auto-recalculation (graph/nodes.py) — kept consistent so the guardrail
and the edit-total logic agree on what "correct" tax math looks like.
"""

CGST_RATE = 0.09
SGST_RATE = 0.09
ASSUMED_TAX_RATE = CGST_RATE + SGST_RATE

LINE_ITEM_TOTAL_TOLERANCE = 0.02   # 2% relative tolerance, invoice subtotal+tax vs stated total
LINE_ITEM_MATH_TOLERANCE = 0.02    # 2% (or Rs.1, whichever is larger) per line: qty*rate vs stated amount
MAX_REASONABLE_LINE_ITEMS = 50     # sanity ceiling — guards against a runaway/hallucinated item list


def calculate_invoice_total(subtotal: float, discount_percentage: float = 0.0) -> tuple[float, float, float]:
    """Apply the contract discount, then add fixed GST on the subtotal.

    GST remains 18% of the original subtotal (9% CGST + 9% SGST), as
    required by the LedgerLens invoice workflow.
    """
    discount_amount = round(subtotal * (discount_percentage / 100), 2)
    tax_amount = round(subtotal * ASSUMED_TAX_RATE, 2)
    return round(subtotal - discount_amount + tax_amount, 2), discount_amount, tax_amount


def check_line_items_sum_to_total(invoice, discount_percentage: float = 0.0) -> tuple[bool, str]:
    """Catches hallucinated invoice totals: if the line items don't add
    up (with tax) to anywhere near the stated grand total, something
    was extracted wrong — either the total, a line item, or both."""
    if not invoice.line_items:
        return True, "no line items to cross-check (skipped)"

    subtotal = sum(li.quantity * li.rate for li in invoice.line_items)
    if subtotal <= 0:
        return False, "line items sum to Rs.0 or less — likely extraction failure"

    expected_total, discount_amount, tax_amount = calculate_invoice_total(subtotal, discount_percentage)
    relative_diff = abs(invoice.amount - expected_total) / expected_total

    if relative_diff > LINE_ITEM_TOTAL_TOLERANCE:
        return False, (
            f"Line items subtotal Rs.{subtotal:,.2f} - {discount_percentage:g}% discount (Rs.{discount_amount:,.2f}) "
            f"+ 18% GST (Rs.{tax_amount:,.2f}) = Rs.{expected_total:,.2f} expected "
            f"doesn't reconcile with the extracted invoice total of Rs.{invoice.amount:,.2f} "
            f"({relative_diff * 100:.1f}% mismatch) — possible hallucinated or misread total"
        )
    return True, f"line items reconcile with invoice total ({relative_diff * 100:.1f}% diff, within tolerance)"


def check_line_item_internal_math(invoice) -> tuple[bool, str]:
    """Catches hallucinated/misread individual line items: each line's
    amount should equal quantity * rate. A mismatch here means the
    extraction produced internally inconsistent numbers for that line —
    not a judgment call, just arithmetic that doesn't hold."""
    bad_items = []
    for li in invoice.line_items:
        expected = round(li.quantity * li.rate, 2)
        tolerance = max(1.0, expected * LINE_ITEM_MATH_TOLERANCE)
        if abs(li.amount - expected) > tolerance:
            bad_items.append(
                f"'{li.description}': {li.quantity} x Rs.{li.rate:,.2f} = Rs.{expected:,.2f} expected, "
                f"but amount states Rs.{li.amount:,.2f}"
            )
    if bad_items:
        return False, "Line item math doesn't reconcile: " + "; ".join(bad_items)
    return True, "all line item amounts match quantity x rate"


def check_positive_values(invoice) -> tuple[bool, str]:
    """Extraction shouldn't ever produce zero/negative amounts on a
    real invoice — a common failure mode when an LLM misreads a field
    or a currency symbol gets absorbed into a number."""
    if invoice.amount <= 0:
        return False, f"invoice total is non-positive (Rs.{invoice.amount:,.2f})"
    for li in invoice.line_items:
        if li.rate <= 0 or li.quantity <= 0 or li.amount <= 0:
            return False, (
                f"line item '{li.description}' has a non-positive quantity, rate, or amount "
                f"(qty={li.quantity}, rate={li.rate}, amount={li.amount})"
            )
    return True, "all amounts positive"


def check_reasonable_line_item_count(invoice, max_items: int = MAX_REASONABLE_LINE_ITEMS) -> tuple[bool, str]:
    """Guards against a runaway extraction producing an absurd number
    of line items (e.g. a vision model hallucinating repeated rows)."""
    count = len(invoice.line_items)
    if count > max_items:
        return False, f"{count} line items exceeds the sanity limit of {max_items} — possible extraction runaway"
    return True, f"{count} line item(s), within sanity limit"


def run_guardrails(invoice, discount_percentage: float = 0.0) -> tuple[bool, list[tuple[str, str]]]:
    """Runs every guardrail check. Returns (all_passed, violations) where
    violations is a list of (check_name, detail) for anything that failed."""
    checks = [
        ("line_items_sum_to_total", check_line_items_sum_to_total(invoice, discount_percentage)),
        ("line_item_internal_math", check_line_item_internal_math(invoice)),
        ("positive_values", check_positive_values(invoice)),
        ("reasonable_item_count", check_reasonable_line_item_count(invoice)),
    ]
    violations = [(name, detail) for name, (passed, detail) in checks if not passed]
    return len(violations) == 0, violations
