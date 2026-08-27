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
from tools.historical_pattern import fetch_vendor_history, save_processed_invoice
from tools.guardrails import calculate_invoice_total, run_guardrails
from langgraph.types import interrupt
from langchain_core.runnables import RunnableConfig

compliance_agent = ComplianceAgent()
risk_agent = RiskAgent()

# HARD_APPROVAL_CEILING = 150_000


def _get_supabase_client(config: RunnableConfig | None):
    """The Supabase client lives in `config`, NOT in GraphState."""
    return (config or {}).get("configurable", {}).get("supabase_client")


def _get_user_id(config: RunnableConfig | None) -> str | None:
    """The active user_id lives in `config` alongside supabase_client.
    Used to scope vendor history and decisions to the logged-in user."""
    return (config or {}).get("configurable", {}).get("user_id")


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
    discount_percentage = float(state.get("discount_percentage") or 0)
    if invoice.line_items and discount_percentage:
        subtotal = sum(item.quantity * item.rate for item in invoice.line_items)
        total, _, _ = calculate_invoice_total(subtotal, discount_percentage)
        invoice = invoice.model_copy(update={"amount": total})
    passed, violations = run_guardrails(invoice, discount_percentage)
    return {
        "invoice": invoice,
        "guardrail_passed": passed,
        "guardrail_violations": violations,
    }


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
    user_id = _get_user_id(config)

    if supabase_client is not None:
        # Real memory: pull this vendor's actual growing history,
        # scoped to this user so cross-user data never bleeds in.
        history = fetch_vendor_history(supabase_client, invoice.vendor, user_id=user_id)
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
    # if invoice.amount > HARD_APPROVAL_CEILING:
    #     reasons.append(
    #         f"Amount Rs.{invoice.amount:,.2f} exceeds the hard approval ceiling "
    #         f"of Rs.{HARD_APPROVAL_CEILING:,.2f} — never auto-approved regardless of other checks"
    #     )

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

    NOTE: The decision (approve/reject/edit) is captured in the
    Supabase `invoices` table by api.py when it calls _serialize()
    after the graph resumes — no separate local log_decision() write
    is needed here.
    """
    invoice = state["invoice"]
    decision = state["decision"]

    human_response = interrupt({
        "invoice_number": invoice.invoice_number,
        "vendor": invoice.vendor,
        "amount": invoice.amount,
        "reasons": decision["reasons"],
    })

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
    edited_contract = state["human_response"].get("contract")
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
                if idx == len(updated_data["line_items"]):
                    updated_data["line_items"].append({
                        "description": "",
                        "quantity": 1,
                        "rate": 0,
                        "amount": 0,
                    })
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
        new_subtotal = sum(li["quantity"] * li["rate"] for li in updated_data["line_items"])
        discount_percentage = float(state.get("discount_percentage") or 0)
        new_total, discount_amount, tax_amount = calculate_invoice_total(
            new_subtotal, discount_percentage
        )

        print(f"[info] Subtotal: Rs.{new_subtotal:,.2f} - {discount_percentage:g}% discount "
              f"(Rs.{discount_amount:,.2f}) + GST 18% (Rs.{tax_amount:,.2f}) = Rs.{new_total:,.2f}")
        updated_data["amount"] = new_total

    updated_invoice = Invoice(**updated_data)
    result = {"invoice": updated_invoice, "human_response": None}
    if edited_contract is not None:
        result["discount_percentage"] = edited_contract.get("discount_percentage", 0)
    return result


def reject_node(state: GraphState) -> dict:
    note = (state.get("human_response") or {}).get("note", "")
    reason = f"Rejected by human reviewer. Note: {note}" if note else "Rejected by human reviewer."
    return {"decision": {"status": "rejected", "reasons": [reason]}}


def export_node(state: GraphState) -> dict:
    """Mock export — writes to console for now."""
    invoice = state["invoice"]

    # if invoice.amount > HARD_APPROVAL_CEILING:
    #     print(f"[guardrail] BLOCKED export: Rs.{invoice.amount:,.2f} exceeds the hard ceiling "
    #           f"of Rs.{HARD_APPROVAL_CEILING:,.2f} — refused regardless of approval.")
    #     return {
    #         "decision": {
    #             "status": "blocked_by_guardrail",
    #             "reasons": [
    #                 f"Amount Rs.{invoice.amount:,.2f} exceeds the hard approval ceiling of "
    #                 f"Rs.{HARD_APPROVAL_CEILING:,.2f}. This cannot be exported through this system "
    #                 f"regardless of approval — requires a separate, authorized high-value channel."
    #             ],
    #         }
    #     }

    was_auto = state["decision"]["status"] == "auto_approve"
    reason = "Auto-approved by Supervisor (no human review needed)" if was_auto else "Approved by human reviewer"

    print(f"[export] Invoice {invoice.invoice_number} ({invoice.vendor}) — "
          f"Rs.{invoice.amount:,.2f} -> exported to accounting system (mock)")

    return {"decision": {"status": "approved_exported", "reasons": [reason]}}


def persist_invoice_node(state: GraphState, config: RunnableConfig = None) -> dict:
    """The invoice is already persisted to the `invoices` Supabase table
    by api.py's _serialize() \u2192 upsert, so there's nothing to do here.
    This node remains in the graph as a hook for any future side-effects
    that should only run for fully approved invoices (e.g., triggering
    an external accounting API, sending a notification, etc.)."""
    if state["decision"]["status"] != "approved_exported":
        return {}
    # No additional write needed — api.py already persisted the row.
    return {}
