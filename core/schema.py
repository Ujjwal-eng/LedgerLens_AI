"""
Strict schema for the Extraction Agent's output.
Every invoice — regardless of layout, vendor, or source (text PDF vs.
scanned image) — must be forced into this shape.
"""

from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import date


class LineItem(BaseModel):
    description: str
    quantity: float = Field(default=1, description="Quantity billed")
    rate: float = Field(description="Unit rate before tax")
    amount: float = Field(description="quantity * rate for this line")


class Invoice(BaseModel):
    vendor: str = Field(description="Vendor / seller name as printed on the invoice")
    invoice_number: str
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    currency: str = Field(default="INR")
    amount: float = Field(description="Grand total, tax included")
    line_items: List[LineItem] = Field(default_factory=list)

    # metadata the pipeline needs but which isn't part of the "clean" invoice data
    source_file: Optional[str] = None
    extraction_method: Optional[str] = Field(
        default=None, description="'groq_text' or 'gemini_vision'"
    )

