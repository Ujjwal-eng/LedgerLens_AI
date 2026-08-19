from pydantic import BaseModel
from typing import List, Literal, Optional

Status = Literal["pass", "fail", "warning"]


class FieldCheck(BaseModel):
    field: str
    status: Status
    detail: str


class ComplianceResult(BaseModel):
    vendor: str
    invoice_number: str
    overall_status: Literal["pass", "fail"]
    checks: List[FieldCheck]
    relevant_clause: Optional[str] = None
    relevant_clause_score: Optional[float] = None
