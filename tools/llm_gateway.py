"""
LLM Gateway — Phase 10

Every call to Groq or Gemini goes through here, not through
extraction_agent.py directly. Centralizing it is what makes fallback
actually reliable: a provider failure (bad key, network down, rate
limit, hitting a billing/price cap) isn't caught anywhere else — it
would crash the whole extraction instead of failing over to the other
provider. Only structured-output PARSING failures were caught before;
a genuine provider outage was not.

ProviderError wraps ANY failure from a provider call — auth errors,
network errors, timeouts, rate limits, quota/price-cap errors — so the
caller doesn't need to know Groq/Gemini's specific exception types.
Killing/rate-limiting a key raises a real exception from that
provider's SDK, which gets caught here and triggers the same fallback
path regardless of the specific underlying cause.

Provider roles (now that the Gemini key is on a paid plan):
  - Gemini is PRIMARY for every tier — text extraction, confident OCR,
    and vision (blurred/low-confidence scans).
  - Groq is the FALLBACK, used only when Gemini fails for a tier that
    has raw text available (text_pdf, ocr). Groq has no vision
    capability, so it can't stand in for Gemini on the vision-only
    tier (low_confidence_ocr) — if Gemini fails there, there's nothing
    left to fall back to.
"""

import base64
import os
from tools.routing_logger import log_routing


class ProviderError(Exception):
    def __init__(self, provider: str, original: Exception):
        self.provider = provider
        self.original = original
        super().__init__(f"{provider} failed: {original}")


def call_groq_text(text: str, instructions: str) -> str:
    try:
        from groq import Groq
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": f"Invoice text:\n\n{text}"},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content
    except Exception as e:
        # Deliberately broad: auth errors, network errors, timeouts, rate
        # limits, and missing-key errors (KeyError on os.environ) all need
        # to trigger the same fallback behavior — the caller shouldn't
        # need to enumerate every possible Groq SDK exception type.
        raise ProviderError("groq", e) from e


def call_gemini_text(text: str, instructions: str) -> str:
    """Gemini's text-only counterpart to call_groq_text — used when
    Gemini is the primary provider for a tier that has raw text
    (text_pdf, ocr) rather than only an image."""
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[instructions, f"Invoice text:\n\n{text}"],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        return response.text
    except Exception as e:
        raise ProviderError("gemini", e) from e


def call_gemini_vision(image_bytes: bytes, instructions: str) -> str:
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[
                instructions,
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            ],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        return response.text
    except Exception as e:
        raise ProviderError("gemini", e) from e


def extract_with_fallback(
    instructions: str, invoice_path: str, tier: str,
    text: str | None = None, image_bytes: bytes | None = None,
    user_id: str | None = None,
) -> tuple[str, str, bool]:
    """Tries Gemini first — text if available, vision otherwise — since
    Gemini is now the primary (paid) provider for every tier. Falls
    back to Groq text on ANY Gemini failure, but only when there's raw
    text to hand it (text_pdf, ocr); the vision-only tier
    (low_confidence_ocr) has no fallback since Groq can't do vision.

    Returns (raw_json_str, provider_used, fallback_was_triggered).
    Raises ProviderError if every available option failed.
    """
    if text is not None:
        try:
            raw = call_gemini_text(text, instructions)
            log_routing(invoice_path, tier, "gemini", success=True, fallback_triggered=False, user_id=user_id)
            return raw, "gemini", False
        except ProviderError as e:
            log_routing(invoice_path, tier, "gemini", success=False, fallback_triggered=False, error=str(e), user_id=user_id)
            print(f"[gateway] Gemini failed for {invoice_path} ({e}) — falling back to Groq text.")
            try:
                raw = call_groq_text(text, instructions)
                log_routing(invoice_path, tier, "groq", success=True, fallback_triggered=True, user_id=user_id)
                return raw, "groq", True
            except ProviderError as e2:
                log_routing(invoice_path, tier, "groq", success=False, fallback_triggered=True, error=str(e2), user_id=user_id)
                raise ProviderError("gemini+groq", e2) from e2

    if image_bytes is not None:
        try:
            raw = call_gemini_vision(image_bytes, instructions)
            log_routing(invoice_path, tier, "gemini", success=True, fallback_triggered=False, user_id=user_id)
            return raw, "gemini", False
        except ProviderError as e:
            log_routing(invoice_path, tier, "gemini", success=False, fallback_triggered=False, error=str(e), user_id=user_id)
            raise  # no Groq fallback possible — Groq has no vision capability

    raise ValueError("extract_with_fallback called with neither text nor image_bytes")