"""
Each node wraps one specialist agent (or the Supervisor's decision logic).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.extraction_agent import extract_invoice
from agents.compliance_agent import ComplianceAgent
from agents.risk_agent import RiskAgent
from graph.state import GraphState
from tools.decision_logger import log_decision
from tools.historical_pattern import fetch_vendor_history, save_processed_invoice
from tools.guardrails import run_guardrails
from langgraph.types import interrupt
from langchain_core.runnables import RunnableConfig

compliance_agent = ComplianceAgent()
risk_agent = RiskAgent()

HARD_APPROVAL_CEILING = 150_000


def _get_supabase_client(config: RunnableConfig | None):
    """The Supabase client lives in `config`, NOT in GraphState."""
    return (config or {}).get("configurable", {}).get("supabase_client")


def extraction_node(state: GraphState) -> dict:
    try:
        invoice = extract_invoice(state["invoice_path"])
        return {"invoice": invoice, "extraction_error": None}
    except Exception as e:
        return {"invoice": None, "extraction_error": str(e)}


def route_after_extraction(state: GraphState) -> str:
    return "continue" if state.get("invoice") is not None else "failed"


def extraction_failed_node(state: GraphState) -> dict:
    return {
        "decision": {
            "status": "extraction_failed",
            "reasons": [state.get("extraction_error", "Unknown extraction error")],
        }
    }


def guardrail_check_node(state: GraphState) -> dict:
    invoice = state["invoice"]
    passed, violations = run_guardrails(invoice)
    return {"guardrail_passed": passed, "guardrail_violations": violations}


def route_after_guardrails(state: GraphState) -> str:
    return "continue" if state.get("guardrail_passed") else "blocked"


def guardrail_blocked_node(state: GraphState) -> dict:
    reasons = [f"[{name}] {detail}" for name, detail in state.get("guardrail_violations", [])]
    return {"decision": {"status": "blocked_by_guardrail", "reasons": reasons}}


def compliance_node(state: GraphState) -> dict:
    result = compliance_agent.check(state["invoice"])
    return {"compliance_result": result}


def risk_node(state: GraphState, config: RunnableConfig = None) -> dict:
    invoice = state["invoice"]
    supabase_client = _get_supabase_client(config)

    if supabase_client is not None:
        # Real memory: pull this vendor's actual growing history.
        history = fetch_vendor_history(supabase_client, invoice.vendor)
    else:
        # No Supabase configured (tests, or running without persistence) —
        # fall back to whatever was passed in directly.
        history = state.get("vendor_history", [])

    result = risk_agent.assess(invoice, history)
    return {"risk_result": result}


def supervisor_node(state: GraphState) -> dict:
    invoice = state["invoice"]
    compliance = state["compliance_result"]
    risk = state["risk_result"]

    reasons = []

    # Hard guardrail — checked first, overrides everything else
    if invoice.amount > HARD_APPROVAL_CEILING:
        reasons.append(
            f"Amount Rs.{invoice.amount:,.2f} exceeds the hard approval ceiling "
            f"of Rs.{HARD_APPROVAL_CEILING:,.2f} — never auto-approved regardless of other checks"
        )

    if compliance.overall_status == "fail":
        problem_field_checks = [c for c in compliance.checks if c.status in ("fail", "warning")]
        has_hard_fail = any(c.status == "fail" for c in problem_field_checks)
        label = "Compliance check failed" if has_hard_fail else "Compliance flagged for review"
        reasons.append(f"{label}: {'; '.join(c.detail for c in problem_field_checks)}")

        if compliance.relevant_clause:
            reasons.append(f"Relevant contract clause: {compliance.relevant_clause}")

    if risk.risk_score in ("medium", "high"):
        flag_details = [f.detail for f in risk.flags]
        reasons.append(f"Risk score is {risk.risk_score.upper()}: {'; '.join(flag_details)}")

    if reasons:
        decision = {"status": "escalate_to_human", "reasons": reasons}
    else:
        decision = {
            "status": "auto_approve",
            "reasons": ["Compliance passed, risk is low, amount within approval ceiling"],
        }

    return {"decision": decision}


def route_after_supervisor(state: GraphState) -> str:
    return "auto_approve" if state["decision"]["status"] == "auto_approve" else "escalate"


def human_approval_node(state: GraphState) -> dict:
    """This is where the graph actually PAUSES. `interrupt()` halts
    execution here and returns its payload to whoever called `.invoke()`
    — a human (or a UI) reviews that payload, then resumes the graph by
    calling `.invoke(Command(resume={...}))` with their decision.
    """
    invoice = state["invoice"]
    decision = state["decision"]

    human_response = interrupt({
        "invoice_number": invoice.invoice_number,
        "vendor": invoice.vendor,
        "amount": invoice.amount,
        "reasons": decision["reasons"],
    })

    log_decision(
        invoice_number=invoice.invoice_number,
        vendor=invoice.vendor,
        action=human_response.get("action", "unknown"),
        detail=human_response,
    )

    return {"human_response": human_response}


def route_after_human(state: GraphState) -> str:
    action = state["human_response"].get("action", "reject")
    return {"approve": "approve", "reject": "reject", "edit": "edit"}.get(action, "reject")


CGST_RATE = 0.09
SGST_RATE = 0.09


def edit_invoice_node(state: GraphState) -> dict:
    """Applies the human's field corrections, then routes back to
    Compliance to re-validate against the CORRECTED data — not a rubber
    stamp. An edited invoice goes through the same checks a fresh one
    would; if it still doesn't pass, it comes right back to this same
    human approval queue for another look.

    Supports two kinds of keys:
      - Top-level fields:  amount=41000, due_date=2026-08-16
      - Line item fields:  line_items.0.rate=15000
        (dotted: line_items.<index>.<field>, index starts at 0)

    Editing a line item's rate or quantity auto-recomputes that line's
    amount (qty * rate). It also auto-recomputes the invoice's overall
    `amount` (grand total) directly from the new subtotal using the
    known CGST/SGST rates (9% + 9%) — not derived from the old total,
    which is a more reliable and transparent calculation. If you
    explicitly set `amount=` yourself in the same edit batch, your
    value wins — the auto-recompute only fills in what you didn't
    specify.

    Invalid edits (bad index, unparseable value) are skipped with a
    printed warning rather than crashing the whole approval flow —
    every other valid edit in the same batch still applies.
    """
    from datetime import date as date_cls
    from core.schema import Invoice

    invoice = state["invoice"]
    edits = state["human_response"].get("fields", {})
    updated_data = invoice.model_dump()

    amount_explicitly_set = "amount" in edits
    line_items_changed = False

    for key, value in edits.items():
        try:
            if key.startswith("line_items."):
                _, idx_str, field = key.split(".", 2)
                idx = int(idx_str)
                if field in ("rate", "amount", "quantity"):
                    value = float(str(value).replace(",", ""))
                updated_data["line_items"][idx][field] = value

                if field in ("rate", "quantity"):
                    li = updated_data["line_items"][idx]
                    li["amount"] = li["quantity"] * li["rate"]

                line_items_changed = True

            elif key in ("invoice_date", "due_date"):
                updated_data[key] = date_cls.fromisoformat(value) if isinstance(value, str) else value

            elif key == "amount":
                updated_data[key] = float(str(value).replace(",", ""))

            else:
                updated_data[key] = value

        except (ValueError, IndexError, KeyError) as e:
            print(f"[warn] Skipping invalid edit '{key}={value}': {e}")

    if line_items_changed and not amount_explicitly_set:
        new_subtotal = sum(li["amount"] for li in updated_data["line_items"])
        cgst = round(new_subtotal * CGST_RATE, 2)
        sgst = round(new_subtotal * SGST_RATE, 2)
        new_total = round(new_subtotal + cgst + sgst, 2)

        print(f"[info] Subtotal: Rs.{new_subtotal:,.2f} + CGST 9% (Rs.{cgst:,.2f}) "
              f"+ SGST 9% (Rs.{sgst:,.2f}) = Rs.{new_total:,.2f}")
        updated_data["amount"] = new_total

    updated_invoice = Invoice(**updated_data)
    return {"invoice": updated_invoice, "human_response": None}


def reject_node(state: GraphState) -> dict:
    note = (state.get("human_response") or {}).get("note", "")
    reason = f"Rejected by human reviewer. Note: {note}" if note else "Rejected by human reviewer."
    return {"decision": {"status": "rejected", "reasons": [reason]}}


def export_node(state: GraphState) -> dict:
    """Mock export — writes to console for now."""
    invoice = state["invoice"]

    if invoice.amount > HARD_APPROVAL_CEILING:
        print(f"[guardrail] BLOCKED export: Rs.{invoice.amount:,.2f} exceeds the hard ceiling "
              f"of Rs.{HARD_APPROVAL_CEILING:,.2f} — refused regardless of approval.")
        return {
            "decision": {
                "status": "blocked_by_guardrail",
                "reasons": [
                    f"Amount Rs.{invoice.amount:,.2f} exceeds the hard approval ceiling of "
                    f"Rs.{HARD_APPROVAL_CEILING:,.2f}. This cannot be exported through this system "
                    f"regardless of approval — requires a separate, authorized high-value channel."
                ],
            }
        }

    was_auto = state["decision"]["status"] == "auto_approve"
    reason = "Auto-approved by Supervisor (no human review needed)" if was_auto else "Approved by human reviewer"

    print(f"[export] Invoice {invoice.invoice_number} ({invoice.vendor}) — "
          f"Rs.{invoice.amount:,.2f} -> exported to accounting system (mock)")

    return {"decision": {"status": "approved_exported", "reasons": [reason]}}


def persist_invoice_node(state: GraphState, config: RunnableConfig = None) -> dict:
    """Writes this invoice into vendor history so it counts toward every
    FUTURE risk assessment for this vendor. Only reached via the export
    path — i.e. only for invoices that were actually approved (auto or
    by a human). Rejected invoices deliberately never reach this node
    (see graph wiring): a rejected invoice (fraud, duplicate, wrong
    vendor) shouldn't be learned as "normal" billing behavior."""
    # Guard against a guardrail-blocked invoice being written to history
    # as if it were approved — export_node can override the decision to
    # "blocked_by_guardrail" even after a human clicked approve, and
    # that must NOT be learned as normal billing behavior.
    if state["decision"]["status"] != "approved_exported":
        return {}

    supabase_client = _get_supabase_client(config)
    if supabase_client is None:
        return {}  # no persistence configured — safe no-op for tests

    invoice = state["invoice"]
    save_processed_invoice(supabase_client, {
        "vendor_name": invoice.vendor,
        "invoice_number": invoice.invoice_number,
        "amount": invoice.amount,
        "currency": invoice.currency,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "line_items": [li.model_dump() for li in invoice.line_items],
    })
    return {}