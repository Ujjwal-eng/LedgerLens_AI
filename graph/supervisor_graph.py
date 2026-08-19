r"""
The actual graph wiring.

    START -> extraction --[conditional]--> guardrail_check --[conditional]--> compliance -> risk -> supervisor
                        \--[failed]------> extraction_failed -> END
                                          guardrail_check --[blocked]--> guardrail_blocked -> END

    supervisor --[auto_approve]--------------------------------> export -> persist_memory -> END
    supervisor --[escalate]-----> human_approval (PAUSES HERE)
                                       |--[approve]---> export -> persist_memory -> END
                                       |--[reject]----> reject -> END (NOT persisted — see persist_invoice_node)
                                       |--[edit]------> edit_invoice -> guardrail_check (loop back, re-checked)

"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from graph.state import GraphState
from graph.nodes import (
    extraction_node,
    route_after_extraction,
    extraction_failed_node,
    guardrail_check_node,
    route_after_guardrails,
    guardrail_blocked_node,
    compliance_node,
    risk_node,
    supervisor_node,
    route_after_supervisor,
    human_approval_node,
    route_after_human,
    edit_invoice_node,
    reject_node,
    export_node,
    persist_invoice_node,
)


def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("extraction", extraction_node)
    graph.add_node("extraction_failed", extraction_failed_node)
    graph.add_node("guardrail_check", guardrail_check_node)
    graph.add_node("guardrail_blocked", guardrail_blocked_node)
    graph.add_node("compliance", compliance_node)
    graph.add_node("risk", risk_node)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("human_approval", human_approval_node)
    graph.add_node("edit_invoice", edit_invoice_node)
    graph.add_node("reject", reject_node)
    graph.add_node("export", export_node)
    graph.add_node("persist_memory", persist_invoice_node)

    graph.add_edge(START, "extraction")
    graph.add_conditional_edges(
        "extraction",
        route_after_extraction,
        {"continue": "guardrail_check", "failed": "extraction_failed"},
    )
    graph.add_edge("extraction_failed", END)

    graph.add_conditional_edges(
        "guardrail_check",
        route_after_guardrails,
        {"continue": "compliance", "blocked": "guardrail_blocked"},
    )
    graph.add_edge("guardrail_blocked", END)

    graph.add_edge("compliance", "risk")
    graph.add_edge("risk", "supervisor")

    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {"auto_approve": "export", "escalate": "human_approval"},
    )

    graph.add_conditional_edges(
        "human_approval",
        route_after_human,
        {"approve": "export", "reject": "reject", "edit": "edit_invoice"},
    )

    graph.add_edge("edit_invoice", "guardrail_check")  # loop back — re-check guardrails AND re-validate
    graph.add_edge("export", "persist_memory")
    graph.add_edge("persist_memory", END)
    graph.add_edge("reject", END)

    # The checkpointer needs to serialize our Pydantic objects (Invoice,
    # ComplianceResult, RiskResult) to persist state across the pause.
    # LangGraph refuses to silently deserialize arbitrary types for
    # security reasons — we explicitly allowlist our own known-safe
    # models here rather than suppressing the warning.
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("core.schema", "Invoice"),
            ("core.schema", "LineItem"),
            ("core.compliance_schema", "ComplianceResult"),
            ("core.compliance_schema", "FieldCheck"),
            ("core.risk_schema", "RiskResult"),
            ("core.risk_schema", "RiskFlag"),
        ]
    )
    checkpointer = MemorySaver(serde=serde)

    return graph.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    app = build_graph()
    print(app.get_graph().draw_ascii())