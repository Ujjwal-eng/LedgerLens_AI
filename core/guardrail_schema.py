from pydantic import BaseModel
from typing import List


class GuardrailViolation(BaseModel):
    check: str
    detail: str


class GuardrailResult(BaseModel):
    passed: bool
    violations: List[GuardrailViolation] = []
    