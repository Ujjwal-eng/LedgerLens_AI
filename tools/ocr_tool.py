"""
OCR tool — Tesseract-based text extraction for scanned invoices.
a real OCR engine in the loop, not just "text PDF vs. send straight to vision LLM."
Runs fully locally (no API calls), so it's fast and free to run on every
scanned page before deciding whether Gemini vision is even needed.


"""

import pytesseract
from PIL import Image


def ocr_extract(image: Image.Image) -> tuple[str, float]:
    """Returns (extracted_text, mean_confidence_0_to_100)."""
    # image_to_string gives readable text with line breaks preserved —
    # better for downstream LLM structuring than the word-by-word dict.
    text = pytesseract.image_to_string(image)

    # image_to_data gives per-word confidence scores, used to judge
    # whether this OCR pass is trustworthy.
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    confidences = [int(c) for c in data["conf"] if c not in ("-1", -1) and int(c) >= 0]
    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    return text.strip(), mean_confidence


if __name__ == "__main__":
    import sys
    img = Image.open(sys.argv[1])
    text, confidence = ocr_extract(img)
    print(f"Confidence: {confidence:.1f}/100\n")
    print(text)
