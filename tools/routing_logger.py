"""
Logs every extraction provider attempt — which tier, which provider,
whether it succeeded, and whether a fallback was triggered. This is
what "X% routed to the low-cost path" actually gets computed from,
and it's also your evidence trail for the Phase 10 checkpoint (kill
Groq, confirm the system fails over — the log shows exactly that
happening, not just a print statement you have to trust).

Storage:
  - Local JSONL (data/routing_log.jsonl): written by log_routing() for
    every extraction attempt regardless of user. Used by the existing
    show_routing_stats.py CLI and test_llm_gateway.py.
  - user_id is attached to every log entry so per-user stats can be
    derived by filtering on that field if needed in the future.
"""

import json
import os
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "routing_log.jsonl")


def log_routing(
    invoice_path: str, tier: str, provider: str, success: bool,
    fallback_triggered: bool, error: str | None = None,
    user_id: str | None = None,
) -> dict:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "invoice_path": invoice_path,
        "tier": tier,                          # "text_pdf" | "ocr" | "low_confidence_ocr"
        "provider": provider,                  # "groq" | "gemini"
        "success": success,
        "fallback_triggered": fallback_triggered,
        "error": error,
        "user_id": user_id,                    # scopes log entries to the invoking user
    }
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_all_routing_logs(user_id: str | None = None) -> list[dict]:
    """Read routing log entries from the local JSONL file.
    Pass user_id to filter to a specific user's entries only."""
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, "r") as f:
        logs = [json.loads(line) for line in f if line.strip()]
    if user_id:
        logs = [l for l in logs if l.get("user_id") == user_id]
    return logs


def compute_routing_stats(user_id: str | None = None) -> dict:
    """The resume metric: how much traffic went through the cheap/fast
    path (Groq) vs. how often the system had to fall back to Gemini.
    Pass user_id to scope stats to a single user."""
    logs = read_all_routing_logs(user_id=user_id)
    successful_attempts = [l for l in logs if l["success"]]
    total = len(successful_attempts)

    if total == 0:
        return {"total": 0}

    groq_count = sum(1 for l in successful_attempts if l["provider"] == "groq")
    gemini_count = total - groq_count
    fallback_count = sum(1 for l in successful_attempts if l["fallback_triggered"])

    return {
        "total": total,
        "groq_count": groq_count,
        "gemini_count": gemini_count,
        "fallback_count": fallback_count,
        "groq_pct": groq_count / total * 100,
        "gemini_pct": gemini_count / total * 100,
        "fallback_pct": fallback_count / total * 100,
    }