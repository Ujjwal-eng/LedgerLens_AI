"""
Vectorless RAG — exact-field contract lookups.
For structured, exact-match data (payment terms, agreed price ranges,
invoice caps).
"""

import json
import os
from difflib import SequenceMatcher

CONTRACTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "vendor_contracts.json")

with open(CONTRACTS_PATH, "r") as f:
    _CONTRACTS = json.load(f)

_CONTRACTS_BY_NAME = {c["vendor_name"]: c for c in _CONTRACTS}


def get_contract(vendor_name: str) -> dict | None:
    if vendor_name in _CONTRACTS_BY_NAME:
        return _CONTRACTS_BY_NAME[vendor_name]

    best_match, best_score = None, 0.0
    for name, contract in _CONTRACTS_BY_NAME.items():
        score = SequenceMatcher(None, vendor_name.lower(), name.lower()).ratio()
        if score > best_score:
            best_match, best_score = contract, score

    return best_match if best_score >= 0.85 else None


def _match_pricing_rule(contract: dict, description: str) -> tuple[str, dict] | None:
    """Fuzzy-match a line item description against the contract's known"""
    best_key, best_score = None, 0.0
    for key in contract["pricing_rules"]:
        score = SequenceMatcher(None, description.lower(), key.lower()).ratio()
        if score > best_score:
            best_key, best_score = key, score

    if best_score >= 0.55:
        return best_key, contract["pricing_rules"][best_key]
    return None


def check_line_item_pricing(contract: dict, description: str, rate: float) -> tuple[str, str]:
    """Returns (status, detail) for a single line item's rate."""
    match = _match_pricing_rule(contract, description)
    if match is None:
        return "warning", f"'{description}' does not match any known service category in the contract"

    matched_key, price_range = match
    if price_range["min"] <= rate <= price_range["max"]:
        return "pass", f"'{description}' matched '{matched_key}' — rate Rs.{rate:,.2f} within agreed range"
    return (
        "fail",
        f"'{description}' matched '{matched_key}' — rate Rs.{rate:,.2f} outside agreed range "
        f"(Rs.{price_range['min']:,.2f}\u2013Rs.{price_range['max']:,.2f})",
    )


def check_payment_terms(contract: dict, invoice_date, due_date) -> tuple[str, str]:
    if invoice_date is None or due_date is None:
        return "warning", "invoice_date or due_date missing — cannot verify payment terms"

    actual_days = (due_date - invoice_date).days
    expected_days = contract["payment_terms_days"]

    if actual_days == expected_days:
        return "pass", f"Net {actual_days} matches agreed Net {expected_days} terms"
    return "fail", f"Net {actual_days} on invoice does not match agreed Net {expected_days} terms"


def check_invoice_cap(contract: dict, amount: float) -> tuple[str, str]:
    cap = contract["max_invoice_amount"]
    if amount <= cap:
        return "pass", f"Total Rs.{amount:,.2f} within vendor's normal invoice cap (Rs.{cap:,.2f})"
    return "fail", f"Total Rs.{amount:,.2f} exceeds vendor's normal invoice cap (Rs.{cap:,.2f})"


def check_bulk_discount(contract: dict, subtotal: float) -> tuple[str, str]:
    threshold = contract.get("bulk_discount_threshold")
    if threshold is None:
        return "pass", "vendor contract has no bulk-order discount clause — not applicable"

    if subtotal >= threshold:
        return "warning", (
            f"Order subtotal Rs.{subtotal:,.2f} is at/above the Rs.{threshold:,.2f} "
            f"bulk-order threshold — verify any applicable volume discount was applied"
        )
    return "pass", f"Subtotal Rs.{subtotal:,.2f} below bulk-order discount threshold (Rs.{threshold:,.2f})"