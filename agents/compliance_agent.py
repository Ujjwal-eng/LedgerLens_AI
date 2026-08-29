"""
Independent from the Extraction Agent: it takes an already-extracted,
validated Invoice object and checks it against that vendor's contract. 
Its only job is "does this invoice match what was agreed."
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema import Invoice
from core.compliance_schema import ComplianceResult, FieldCheck
from tools.vectorless_lookup import (
    get_contract,
    check_line_item_pricing,
    check_payment_terms,
    check_invoice_cap,
    check_bulk_discount,
)
from tools.contract_rag import retrieve_relevant_clause

DAMAGE_KEYWORDS = ("damage", "damaged", "defect", "defective", "broken", "faulty", "replacement")


def _build_rag_query(problem_checks: list[FieldCheck]) -> str:
    """Picks a search query based on WHAT actually triggered the flag"""
    queries = []
    for c in problem_checks:
        if c.field == "invoice_total" or c.field == "bulk_order_discount":
            queries.append("large bulk order volume pricing discount")
        elif c.field.startswith("line_item:"):
            desc = c.field.split(":", 1)[1].lower()
            if any(k in desc for k in DAMAGE_KEYWORDS):
                queries.append("damaged incorrect items reported replacement refund policy")
            else:
                queries.append("work outside agreed scope pricing exceptions")
    return " ".join(dict.fromkeys(queries)) or "pricing exceptions unusual charges"


class ComplianceAgent:
    def check(self, invoice: Invoice) -> ComplianceResult:
        contract = get_contract(invoice.vendor)

        if contract is None:
            return ComplianceResult(
                vendor=invoice.vendor,
                invoice_number=invoice.invoice_number,
                overall_status="fail",
                checks=[FieldCheck(
                    field="vendor",
                    status="fail",
                    detail=f"No contract on file for vendor '{invoice.vendor}'",
                )],
            )

        checks: list[FieldCheck] = []
        checks.append(FieldCheck(field="vendor", status="pass", detail=f"Contract found for '{invoice.vendor}'"))

        # Payment terms
        status, detail = check_payment_terms(contract, invoice.invoice_date, invoice.due_date)
        checks.append(FieldCheck(field="payment_terms", status=status, detail=detail))

        # Invoice cap
        status, detail = check_invoice_cap(contract, invoice.amount)
        checks.append(FieldCheck(field="invoice_total", status=status, detail=detail))

        # Bulk-order discount check (subtotal, before tax)
        subtotal = sum(item.amount for item in invoice.line_items)
        status, detail = check_bulk_discount(contract, subtotal)
        checks.append(FieldCheck(field="bulk_order_discount", status=status, detail=detail))

        # Line items
        for item in invoice.line_items:
            status, detail = check_line_item_pricing(contract, item.description, item.rate)
            checks.append(FieldCheck(field=f"line_item:{item.description}", status=status, detail=detail))

        problem_checks = [c for c in checks if c.status in ("fail", "warning")]

        # Traditional RAG: only invoked when something looks off
        relevant_clause, relevant_score = None, None
        if problem_checks:
            query = _build_rag_query(problem_checks)
            results = retrieve_relevant_clause(contract, query, top_k=1)
            if results:
                relevant_clause, relevant_score = results[0]

        overall = "fail" if any(c.status in ("fail", "warning") for c in checks) else "pass"

        return ComplianceResult(
            vendor=invoice.vendor,
            invoice_number=invoice.invoice_number,
            overall_status=overall,
            checks=checks,
            relevant_clause=relevant_clause,
            relevant_clause_score=relevant_score,
        )


if __name__ == "__main__":
    from datetime import date
    from core.schema import LineItem

    test_invoice = Invoice(
        vendor="Sharma Digital Solutions Pvt. Ltd.",
        invoice_number="SDS/2026-27/999",
        invoice_date=date(2026, 7, 1),
        due_date=date(2026, 7, 16),
        amount=250000,
        line_items=[
            LineItem(description="Emergency Migration - Weekend Callout", quantity=1, rate=180000, amount=180000),
        ],
    )

    agent = ComplianceAgent()
    result = agent.check(test_invoice)
    print(result.model_dump_json(indent=2))