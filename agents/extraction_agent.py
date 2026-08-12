"""
Job: read one invoice (PDF, text-based or scanned) and return a validated
`Invoice` object.

Routing logic:
  1. Try pulling text directly from the PDF with pdfplumber.
  2. If there's enough real text -> send it to Groq as a text-extraction call.
  3. If the PDF has little/no extractable text (i.e. it's a scan) ->
     rasterize the page(s) and send to Gemini as a vision call.
"""

import os

import json # converts python dict to json text since databases and API's talk using JSON text

import base64 # converts binary images to text to send scanned invoice to AI model over internet

from io import BytesIO # Creates a "fake file" lets Python read or modify a file (like an uploaded PDF) immediately 
#without forcing you to actually save a physical copy to your hard drive first.

import pdfplumber # Lets Python read PDF files

from pydantic import ValidationError # Lets Python check if the extracted data is correct

from core.schema import Invoice 

def _get_groq_client():
    from groq import Groq
    return Groq(api_key=os.environ["GROQ_API_KEY"])


def _get_gemini_client():
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    return genai


MIN_TEXT_CHARS = 40  # below this, treat the PDF as "scanned / no usable text"

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
"""


def extract_text_from_pdf(path: str) -> str: 
    text = ""
    with pdfplumber.open(path) as pdf: # here path means the file location from which to be extracted
        for page in pdf.pages: # It iterates through each page of the PDF file that was opened
            page_text = page.extract_text() or ""
            text += page_text + "\n"
    return text.strip()


def pdf_to_image_b64(path: str) -> str:
    """Rasterize page 1 of the PDF to a base64 PNG, for the vision fallback."""
    from pdf2image import convert_from_path
    images = convert_from_path(path, dpi=200)
    buf = BytesIO()
    images[0].save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _parse_and_validate(raw_json_str: str, source_file: str, method: str) -> Invoice:
    # Strip accidental markdown fences before parsing, just in case
    cleaned = raw_json_str.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1)

    data = json.loads(cleaned)
    data["source_file"] = source_file # pasting two more info in data
    data["extraction_method"] = method
    return Invoice(**data) # ** here is for filling in data in the exact schema


def extract_via_groq(text: str, source_file: str) -> Invoice:
    client = _get_groq_client()
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",  # fast + cheap, good for structured text extraction
        messages=[
            {"role": "system", "content": EXTRACTION_INSTRUCTIONS},
            {"role": "user", "content": f"Invoice text:\n\n{text}"},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    return _parse_and_validate(raw, source_file, "groq_text")


def extract_via_gemini_vision(image_b64: str, source_file: str) -> Invoice:
    genai = _get_gemini_client()
    model = genai.GenerativeModel("gemini-2.0-flash")
    response = model.generate_content(
        [
            EXTRACTION_INSTRUCTIONS,
            {"mime_type": "image/png", "data": base64.b64decode(image_b64)},
        ],
        generation_config={"temperature": 0, "response_mime_type": "application/json"},
    )
    return _parse_and_validate(response.text, source_file, "gemini_vision")


def extract_invoice(path: str) -> Invoice:
    """Main entry point: routes to Groq (text) or Gemini (vision) automatically."""
    text = extract_text_from_pdf(path)

    if len(text) >= MIN_TEXT_CHARS:
        try:
            return extract_via_groq(text, source_file=path)
        except (ValidationError, json.JSONDecodeError, KeyError) as e:
            print(f"[warn] Groq extraction failed for {path} ({e}), falling back to vision.")

    # Fallback: little/no text found, or the text-path extraction failed validation
    image_b64 = pdf_to_image_b64(path)
    return extract_via_gemini_vision(image_b64, source_file=path)


if __name__ == "__main__":
    import sys
    result = extract_invoice(sys.argv[1])
    print(result.model_dump_json(indent=2))
