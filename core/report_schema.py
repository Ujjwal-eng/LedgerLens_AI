from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class VendorBreakdown(BaseModel):
    vendor: str
    invoice_count: int
    total_amount: float


class ReportDigest(BaseModel):
    window_start: datetime
    window_end: datetime
    total_processed: int          # approved (auto + human) + rejected, everything that reached a final outcome
    total_auto_approved: int
    total_human_approved: int
    total_rejected: int
    total_flagged: int            # anything that needed a human look at all (approved, rejected, or edited)
    total_amount_approved: float
    currency: str = "INR"
    vendor_breakdown: list[VendorBreakdown] = []
    narrative: Optional[str] = None   # LLM-generated summary sentence(s)