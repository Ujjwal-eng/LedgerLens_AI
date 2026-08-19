from pydantic import BaseModel
from typing import List, Literal, Optional

RiskLevel = Literal["low", "medium", "high"]


class RiskFlag(BaseModel):
    flag: str
    detail: str


class RiskResult(BaseModel):
    vendor: str
    invoice_number: str
    risk_score: RiskLevel
    flags: List[RiskFlag]
    vendor_avg_amount: Optional[float] = None
    vendor_std_amount: Optional[float] = None
    vendor_invoice_count: int = 0
