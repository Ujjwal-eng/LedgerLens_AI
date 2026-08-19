"""
Job: read one invoice (PDF, text-based or scanned) and return a validated
Invoice object.
"""

import os
import sys
import json
import base64
from io import BytesIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pdfplumber
from PIL import Image

from core.schema import Invoice
from tools.ocr_tool import ocr_extract
from tools.llm_gateway import extract_with_fallback, ProviderError

MIN_TEXT_CHARS = 40
OCR_MIN_TEXT_CHARS = 40
OCR_MIN_CONFIDENCE = 65.0

EXTRACTION_INSTRUCTIONS = """You are an invoice data extraction engine.
Read the invoice content given to you and return ONLY a JSON object matching
exactly this schema — no markdown fences, no commentary, no extra keys:

{
  "vendor": string,
  "invoice_number": string,
  "invoice_date": "YYYY-MM-DD" or null,
  "due_date": "YYYY-MM-DD" or null,
  "currency": string (e.g. "INR"),
  "amount": number (grand total, tax included),
  "line_items": [
    {"description": string, "quantity": number, "rate": number, "amount": number}
  ]
}

Rules:
- amount must be the final grand total on the invoice (after tax), not the subtotal.
- If a field is genuinely not present on the invoice, use null (or [] for line_items).
- Never invent values. Never invent line items that aren't on the invoice.
- Dates must be normalized to YYYY-MM-DD.
- The text you're given may contain minor OCR errors (e.g. '0' read as 'O',
  'I' read as 'l'). Use context to correct obvious character-level OCR
  mistakes in names and numbers where confident, but never guess at
  numeric amounts you can't reasonably reconstruct.
- CRITICAL: For each line item, extract the "amount" value EXACTLY as printed
  in the amount/total column for that line. Do NOT recalculate it from
  quantity * rate. Do NOT correct it to match the subtotal or grand total.
  If the invoice prints "35,000" as the line amount, you must output 35000,
  even if quantity * rate would give a different number. Our downstream
  validation depends on seeing the raw printed values to detect arithmetic
  errors on the invoice itself.
"""


def extract_text_from_pdf(path: str) -> str:
    text = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text += page_text + "\n"
    return text.strip()


def pdf_page_to_image(path: str) -> Image.Image:
    from pdf2image import convert_from_path
    images = convert_from_path(path, dpi=200)
    return images[0]


def image_to_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _parse_and_validate(raw_json_str: str, source_file: str, method: str) -> Invoice:
    cleaned = raw_json_str.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1)

    data = json.loads(cleaned)
    data["source_file"] = source_file
    data["extraction_method"] = method
    return Invoice(**data)


def _method_label(tier: str, provider: str, fallback: bool) -> str:
    if fallback:
        return "gemini_vision_fallback"
    return {("text_pdf", "groq"): "groq_text", ("ocr", "groq"): "groq_ocr",
            ("low_confidence_ocr", "gemini"): "gemini_vision"}.get((tier, provider), f"{provider}_{tier}")


def extract_invoice(path: str) -> Invoice:
    """Main entry point — three-tier routing, all provider calls via the gateway."""

    # TIER 1: digital text PDF
    text = extract_text_from_pdf(path)
    if len(text) >= MIN_TEXT_CHARS:
        image = pdf_page_to_image(path)  # prepared upfront in case Groq fails and we need the fallback
        raw, provider, fallback = extract_with_fallback(
            EXTRACTION_INSTRUCTIONS, path, tier="text_pdf",
            text=text, image_bytes=image_to_bytes(image),
        )
        return _parse_and_validate(raw, path, _method_label("text_pdf", provider, fallback))

    # It's scanned — rasterize once, reused by OCR and vision tiers below.
    image = pdf_page_to_image(path)
    image_bytes = image_to_bytes(image)

    # TIER 2: OCR, if confident enough
    ocr_text, ocr_confidence = ocr_extract(image)
    print(f"[info] OCR on {path}: {len(ocr_text)} chars, confidence {ocr_confidence:.1f}/100")

    if len(ocr_text) >= OCR_MIN_TEXT_CHARS and ocr_confidence >= OCR_MIN_CONFIDENCE:
        raw, provider, fallback = extract_with_fallback(
            EXTRACTION_INSTRUCTIONS, path, tier="ocr",
            text=ocr_text, image_bytes=image_bytes,
        )
        return _parse_and_validate(raw, path, _method_label("ocr", provider, fallback))

    # TIER 3: OCR wasn't good enough — go straight to vision (Gemini is the
    # normal choice here, not a fallback from anything).
    raw, provider, fallback = extract_with_fallback(
        EXTRACTION_INSTRUCTIONS, path, tier="low_confidence_ocr",
        text=None, image_bytes=image_bytes,
    )
    return _parse_and_validate(raw, path, _method_label("low_confidence_ocr", provider, fallback))


if __name__ == "__main__":
    result = extract_invoice(sys.argv[1])
    print(result.model_dump_json(indent=2))