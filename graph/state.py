"""
The state object every node reads from and writes to as it flows through
the graph. Each node returns a partial dict of just the keys it updates —
LangGraph merges that into the running state automatically.
"""

from typing import TypedDict, Optional, List
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.schema import Invoice
from core.compliance_schema import ComplianceResult
from core.risk_schema import RiskResult


class GraphState(TypedDict, total=False):
    # input
    invoice_path: str
    vendor_history: List[dict]  # injected at invocation time (or fetched from Supabase in prod)

    # produced by extraction_node
    invoice: Optional[Invoice]
    extraction_error: Optional[str]

    # produced by guardrail_check_node
    guardrail_passed: Optional[bool]
    guardrail_violations: Optional[List[tuple]]

    # produced by compliance_node / risk_node
    compliance_result: Optional[ComplianceResult]
    risk_result: Optional[RiskResult]

    # produced by supervisor_node / extraction_failed_node
    decision: dict

    human_response: Optional[dict]
