"""
FastAPI backend — the web equivalent of approval_cli.py.

approval_cli.py drives the graph interactively in a terminal: it calls
app.invoke(...), and whenever the graph pauses at human_approval it reads
your answer from stdin and resumes with Command(resume=human_input).

This file does the exact same thing, just triggered by HTTP requests
from the browser instead of input(). Nothing about the pipeline itself
is reimplemented or mocked here — every request goes through the real
build_graph() (extraction -> guardrails -> compliance -> risk ->
supervisor -> human_approval / export), same as the CLI.

PERSISTENCE MODEL (this is the part that changed from the original
single-process demo build):
  - Auth is real email/password Supabase Auth. Every request that
    touches user data must carry `Authorization: Bearer <access_token>`.
  - Invoice results, the approval queue (just a filter over invoices),
    and per-user "temporary" vendor contracts live in Supabase Postgres
    (see supabase_schema.sql) — not in an in-memory dict, not in
    web_app_state.json.
  - Uploaded invoice PDFs are stored in a PRIVATE Supabase Storage
    bucket ("invoices" by default), never on local disk beyond a
    short-lived temp file used only while the graph is actually
    running against them.
  - Sample invoices and the 5 built-in vendor contracts
    (data/vendor_contracts.json) are bundled with the app and remain
    readable by everybody, logged in or not — they are not per-user
    data.
  - 25 invoices / 20 contracts per user is enforced as a ROLLING
    30-DAY QUOTA (count of rows created in the last 30 days), not a
    retention/deletion policy — nothing is auto-deleted.

Run with:
    uvicorn api:app --reload --port 8000

Required env vars (via .env): SUPABASE_URL, SUPABASE_ANON_KEY,
SUPABASE_SERVICE_ROLE_KEY, GROQ_API_KEY, GEMINI_API_KEY. SUPABASE_KEY
(the key already used for the vendor-memory Supabase client the graph
itself reads from) is still honored separately — see
_get_supabase_client() below.

Place this file at your project root, next to approval_cli.py, so the
`graph.*` / `core.*` / `agents.*` / `tools.*` imports resolve the same
way they do there.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()  # same reason approval_cli.py loads this first — SUPABASE_URL
                # etc. must be in os.environ before anything below reads them

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from langgraph.types import Command
from graph.supervisor_graph import build_graph
from agents.reporting_agent import ReportingAgent
from tools.routing_logger import compute_routing_stats

# ------------------------------------------------------------------
# Setup — Supabase clients.
#
# Three DIFFERENT keys/clients, three different jobs:
#   - supabase_client  (SUPABASE_KEY)              -> passed into the
#     graph's own config; this is the pre-existing vendor-memory client
#     the Compliance/Risk agents themselves read from. Untouched here.
#   - supabase_service (SUPABASE_SERVICE_ROLE_KEY)  -> our new web-app
#     persistence layer: Postgres tables + Storage bucket. Bypasses
#     RLS, so every query below manually filters by user_id, same as
#     the ownership checks the old in-memory dicts did.
#   - supabase_auth     (SUPABASE_ANON_KEY)         -> signup/login/
#     token verification. Auth calls are meant to be made with the
#     anon key, never the service key.
# ------------------------------------------------------------------

def _get_supabase_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        print("[warn] SUPABASE_URL/SUPABASE_KEY not set — running WITHOUT persistent "
              "vendor memory. Every invoice will show as first-time.")
        return None
    from supabase import create_client
    return create_client(url, key)


def _get_supabase_service_client():
    """Service role is backend-only and never sent to the browser. Powers
    Postgres persistence (invoices/staged_uploads/temp_contracts) and the
    private Storage bucket."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("[warn] SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set — invoice "
              "persistence, contracts, and PDF storage will not work.")
        return None
    from supabase import create_client
    return create_client(url, key)


def _get_supabase_auth_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        print("[warn] SUPABASE_URL/SUPABASE_ANON_KEY not set — signup/login/logout "
              "will not work.")
        return None
    from supabase import create_client
    return create_client(url, key)


STATE_RETENTION_DAYS = 30  # rolling window for the usage quotas below
MAX_CONTRACTS_PER_USER = 20
MAX_INVOICES_PER_USER = 25

INVOICE_BUCKET = os.environ.get("SUPABASE_INVOICE_BUCKET", "invoices")
SUPABASE_SOURCE_PREFIX = "supabase://"  # marks an invoices.source_path as a Storage object

# Uploaded invoice PDFs must be <= 5 MB and exactly 1 page.
MAX_PDF_SIZE_BYTES = 5 * 1024 * 1024

# Scratch space for downloading a PDF out of Storage just long enough
# to feed its path to the graph. Nothing here is meant to survive past
# a single request.
SCRATCH_DIR = Path(tempfile.gettempdir()) / "ledgerlens_scratch"
SCRATCH_DIR.mkdir(exist_ok=True)


def _validate_pdf_bytes(contents: bytes) -> None:
    """Raises HTTPException if the given PDF bytes violate the 5 MB /
    1-page limit. Does not touch disk."""
    if len(contents) > MAX_PDF_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="PDF file must be 5 MB or smaller")

    try:
        num_pages = len(PdfReader(BytesIO(contents)).pages)
    except PdfReadError as e:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {e}") from e

    if num_pages != 1:
        raise HTTPException(status_code=400, detail="PDF must be exactly 1 page")

# Where the built frontend (index.html / app.js / style.css) lives.
# Override with WEB_DIR=/path/to/frontend if yours isn't at ./web.
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web"))

# Sample PDFs the frontend can run without the user needing their own
# invoice file — same folders eval_extraction.py expects alongside it.
# These are bundled with the deployment and shared by every user, so
# they intentionally stay on local disk rather than in per-user Storage.
# Override with SAMPLE_INVOICES_DIR / SAMPLE_INVOICES_SCANNED_DIR if
# yours live somewhere else.
SAMPLE_DIRS = {
    "text": Path(os.environ.get("SAMPLE_INVOICES_DIR", Path(__file__).parent / "sample_invoices")),
}

# vendor_contracts.json — same file tools/vectorless_lookup.py's
# get_contract() reads from (it resolves this as
# os.path.dirname(vectorless_lookup.py)/../data/vendor_contracts.json,
# i.e. <project_root>/data/vendor_contracts.json). These are the 5
# built-in contracts and are shared by everybody. Override with
# CONTRACTS_PATH if yours lives somewhere else.
CONTRACTS_PATH = Path(os.environ.get("CONTRACTS_PATH", Path(__file__).parent / "data" / "vendor_contracts.json"))

# Folder containing eval_guardrails.py / eval_compliance.py / eval_risk.py
# / eval_extraction.py + ground_truth.json (Phase 9). Override with
# EVALS_DIR if yours isn't at ./evals.
EVALS_DIR = Path(os.environ.get("EVALS_DIR", Path(__file__).parent / "evals"))

graph_app = build_graph()
reporting_agent = ReportingAgent()
supabase_client = _get_supabase_client()
supabase_service = _get_supabase_service_client()
supabase_auth = _get_supabase_auth_client()


def _require_service_client():
    if supabase_service is None:
        raise HTTPException(
            status_code=500,
            detail="Server is missing SUPABASE_SERVICE_ROLE_KEY — invoice/contract "
                   "persistence and PDF storage are not configured.",
        )
    return supabase_service


# ------------------------------------------------------------------
# Manually-added ("temporary") vendor contracts
# ------------------------------------------------------------------
# These let someone upload an invoice for a vendor that ISN'T in
# vendor_contracts.json and still get a real compliance check, by
# typing in the contract terms themselves. They live in the
# temp_contracts Postgres table, scoped to the owning user, and never
# modify vendor_contracts.json.
#
# Keyed by vendor name, lowercased, so lookups are case-insensitive —
# this HAS to match whatever string extraction_agent.py pulls out of
# the PDF as `invoice.vendor` exactly (case aside), since that's the
# only thing get_contract() has to go on. The frontend's contract form
# should say this explicitly.

# Each browser request carries a Supabase JWT in `Authorization: Bearer
# <token>`; ACTIVE_USER_ID threads the validated user id down into the
# graph's own contract-lookup patch below (see _patched_get_contract),
# since that code runs deep inside agents/compliance_agent.py with no
# direct access to the current request.
ACTIVE_USER_ID: ContextVar[str | None] = ContextVar("active_user_id", default=None)

# Which thread_ids this SERVER PROCESS has already invoked the graph
# for. LangGraph's MemorySaver checkpointer is in-memory and does not
# survive a restart; if a thread_id isn't in this set, the pending
# approval state has to be rebuilt by re-running the graph from the
# stored PDF before a decision can be resumed. This replaces the old
# `needs_graph_restore` flag that used to get set only at JSON-load
# time — now it's just "have we touched this thread since the process
# started", which is exactly the condition that matters.
LIVE_GRAPH_THREADS: set[str] = set()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rolling_window_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)).isoformat()


# ------------------------------------------------------------------
# Auth — email/password via Supabase Auth
# ------------------------------------------------------------------

class AuthRequest(BaseModel):
    email: EmailStr
    password: str


def _auth_error_message(exc: Exception, fallback: str) -> str:
    # gotrue exceptions usually carry a human-readable `.message`.
    return getattr(exc, "message", None) or str(exc) or fallback


def _get_user_id(authorization: Optional[str] = Header(None)) -> str:
    """FastAPI dependency: validates the bearer token on every
    user-scoped request and returns the Supabase auth user id. This is
    the ONLY thing that decides whose data a request can see — replaces
    the old anonymous X-LedgerLens-User-ID header entirely."""
    if supabase_auth is None:
        raise HTTPException(status_code=500, detail="Auth is not configured on this server")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in required")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Sign in required")
    try:
        response = supabase_auth.auth.get_user(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Your session has expired — please log in again") from exc
    user = getattr(response, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Your session has expired — please log in again")
    return user.id


# ------------------------------------------------------------------
# Postgres-backed persistence helpers (replace the old in-memory
# REGISTRY / TEMP_CONTRACTS / STAGED_UPLOADS dicts and web_app_state.json)
# ------------------------------------------------------------------

def _contract_row_to_dict(row: dict) -> dict:
    """Shape matches exactly what tools/vectorless_lookup.get_contract()
    returns for a real vendor_contracts.json entry, so the compliance
    agent can't tell the difference."""
    contract = {
        "vendor_name": row["vendor_name"],
        "gstin": row["gstin"],
        "payment_terms_days": row["payment_terms_days"],
        "max_invoice_amount": row["max_invoice_amount"],
        "pricing_rules": row.get("pricing_rules") or {},
        "clauses": row.get("clauses") or [],
        "created_at": row.get("created_at"),
    }
    if row.get("discount_percentage") is not None:
        contract["discount_percentage"] = row["discount_percentage"]
    return contract


def _contracts_for_user(user_id: str) -> dict[str, dict]:
    """All of this user's manually-added vendor contracts, keyed by
    lowercased vendor name."""
    db = _require_service_client()
    res = db.table("temp_contracts").select("*").eq("user_id", user_id).execute()
    return {row["vendor_key"]: _contract_row_to_dict(row) for row in (res.data or [])}


def _upsert_temp_contract(user_id: str, contract: dict, previous_vendor_name: str | None = None) -> None:
    db = _require_service_client()
    vendor_key = contract["vendor_name"].strip().lower()
    previous_key = (previous_vendor_name or "").strip().lower()
    if previous_key and previous_key != vendor_key:
        db.table("temp_contracts").delete().eq("user_id", user_id).eq("vendor_key", previous_key).execute()
    row = {
        "user_id": user_id,
        "vendor_key": vendor_key,
        "vendor_name": contract["vendor_name"],
        "gstin": contract["gstin"],
        "payment_terms_days": contract["payment_terms_days"],
        "max_invoice_amount": contract["max_invoice_amount"],
        "discount_percentage": contract.get("discount_percentage"),
        "pricing_rules": contract.get("pricing_rules", {}),
        "clauses": contract.get("clauses", []),
        "created_at": contract.get("created_at") or _utc_now(),
    }
    db.table("temp_contracts").upsert(row, on_conflict="user_id,vendor_key").execute()


def _delete_temp_contract(user_id: str, vendor_key: str) -> Optional[dict]:
    db = _require_service_client()
    res = db.table("temp_contracts").select("*").eq("user_id", user_id).eq("vendor_key", vendor_key).limit(1).execute()
    if not res.data:
        return None
    db.table("temp_contracts").delete().eq("user_id", user_id).eq("vendor_key", vendor_key).execute()
    return _contract_row_to_dict(res.data[0])


def _count_recent(table: str, user_id: str) -> int:
    db = _require_service_client()
    res = (
        db.table(table)
        .select("*", count="exact")
        .eq("user_id", user_id)
        .gte("created_at", _rolling_window_cutoff())
        .execute()
    )
    return res.count or 0


def _ensure_contract_capacity(user_id: str, vendor_name: str, previous_vendor_name: str | None = None) -> None:
    target_key = vendor_name.strip().lower()
    previous_key = (previous_vendor_name or "").strip().lower()
    existing_keys = set(_contracts_for_user(user_id).keys())
    is_existing = target_key in existing_keys or (previous_key and previous_key in existing_keys)
    if not is_existing and _count_recent("temp_contracts", user_id) >= MAX_CONTRACTS_PER_USER:
        raise HTTPException(status_code=429, detail=f"You can save at most {MAX_CONTRACTS_PER_USER} vendor contracts per 30 days")


def _ensure_invoice_capacity(user_id: str, additional: int = 1) -> None:
    if _count_recent("invoices", user_id) + additional > MAX_INVOICES_PER_USER:
        raise HTTPException(status_code=429, detail=f"You can process at most {MAX_INVOICES_PER_USER} invoices per 30 days")


# NOTE ON SHAPE: confirmed against your real tools/vectorless_lookup.py —
# get_contract() returns a plain dict shaped exactly like one entry in
# vendor_contracts.json (or None). Good — nothing to change there.
#
# It also does exact-then-fuzzy matching (difflib.SequenceMatcher,
# 0.85 threshold) against vendor names, to tolerate OCR/extraction
# typos in invoice.vendor. The patch below replicates that SAME
# algorithm over real contracts merged with this user's temp_contracts
# rows — not just an exact match — so a manually-added vendor gets the
# same typo tolerance a real one does. On an exact-name collision, the
# manually-added contract wins (lets you override a real contract's
# terms for this session, e.g. to test a change before editing the
# real file).
try:
    import tools.vectorless_lookup as _vectorless_lookup
    import agents.compliance_agent as _compliance_agent_module

    def _patched_get_contract(vendor_name: str):
        merged = dict(_vectorless_lookup._CONTRACTS_BY_NAME)
        user_id = ACTIVE_USER_ID.get()
        if user_id:
            merged.update({c["vendor_name"]: c for c in _contracts_for_user(user_id).values()})

        if vendor_name in merged:
            return merged[vendor_name]

        best_match, best_score = None, 0.0
        for name, contract in merged.items():
            score = _vectorless_lookup.SequenceMatcher(None, vendor_name.lower(), name.lower()).ratio()
            if score > best_score:
                best_match, best_score = contract, score

        return best_match if best_score >= 0.85 else None

    _compliance_agent_module.get_contract = _patched_get_contract
except Exception as e:  # pragma: no cover - surfaced loudly, not swallowed
    print(f"[warn] Could not patch agents.compliance_agent.get_contract for manual "
          f"vendor contracts — manually-added vendors will show 'no contract on file' "
          f"until this is fixed: {e}")

# Cache of the most recent /api/evaluations/run result, so GET
# /api/evaluations can return it without re-running anything (the eval
# scripts are the REAL eval_*.py files under EVALS_DIR — nothing here
# reimplements their scoring logic, this just calls their run()). This
# is process-local scratch state, not user data, so it stays in memory.
EVAL_CACHE: Optional[dict] = None

# ground_truth.json — same file eval_guardrails.py / eval_compliance.py /
# eval_risk.py / eval_extraction.py read from (Phase 9), keyed by an
# invoice id whose entry carries "source_file" (e.g.
# "sample_invoices/invoice_01_....pdf") plus the expected extracted
# fields and expected guardrail/compliance status. Override with
# GROUND_TRUTH_PATH if yours isn't at EVALS_DIR/ground_truth.json.
GROUND_TRUTH_PATH = Path(os.environ.get("GROUND_TRUTH_PATH", EVALS_DIR / "ground_truth.json"))


def _load_ground_truth() -> dict:
    if not GROUND_TRUTH_PATH.exists():
        return {}
    with GROUND_TRUTH_PATH.open() as f:
        return json.load(f)


def _find_ground_truth_entry(filename: str) -> Optional[dict]:
    """Matches a processed invoice back to its ground_truth.json entry by
    filename (sample invoices only — a freshly-uploaded invoice has no
    entry here, which is exactly the signal used to fall back to
    evaluating against a manually-added vendor contract instead)."""
    stem = Path(filename).stem
    for key, rec in _load_ground_truth().items():
        source_name = Path(rec.get("source_file", "")).stem
        if source_name == stem or key == stem:
            return rec
    return None


def _eval_check(checks: list, field: str, expected, actual, *, passed: Optional[bool] = None) -> None:
    if passed is None:
        passed = expected == actual
    checks.append({"field": field, "expected": expected, "actual": actual, "passed": bool(passed)})


def _compare_invoice_to_ground_truth(entry: dict, gt: dict) -> dict:
    """Scores one processed invoice against its ground_truth.json entry —
    the extracted fields plus the guardrail/compliance outcome."""
    invoice = entry.get("invoice") or {}
    checks: list = []

    _eval_check(checks, "invoice_number", gt.get("invoice_number"), invoice.get("invoice_number"))
    _eval_check(checks, "vendor", gt.get("vendor"), invoice.get("vendor"))
    _eval_check(checks, "invoice_date", gt.get("invoice_date"), invoice.get("invoice_date"))
    _eval_check(checks, "due_date", gt.get("due_date"), invoice.get("due_date"))

    exp_amount, act_amount = gt.get("amount"), invoice.get("amount")
    amount_ok = exp_amount is not None and act_amount is not None and abs(exp_amount - act_amount) < 0.01
    _eval_check(checks, "amount", exp_amount, act_amount, passed=amount_ok)

    exp_guardrail = gt.get("expected_guardrail_status")
    guardrail_passed = entry.get("guardrail_passed")
    act_guardrail = "pass" if guardrail_passed else ("fail" if guardrail_passed is False else None)
    _eval_check(checks, "guardrail_status", exp_guardrail, act_guardrail)

    exp_compliance = gt.get("expected_compliance_status")
    compliance = entry.get("compliance") or {}
    act_compliance = compliance.get("overall_status") or compliance.get("status")
    _eval_check(checks, "compliance_status", exp_compliance, act_compliance)

    return {
        "eval_type": "ground_truth",
        "reference": gt.get("source_file"),
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
    }


def _compare_invoice_to_manual_contract(entry: dict, user_id: str) -> dict:
    """For a freshly-uploaded invoice that has no ground_truth.json entry:
    scores it against whatever manually-added ("temporary") vendor
    contract was on file for it at compliance time, since that's the
    only reference available for a brand-new vendor."""
    invoice = entry.get("invoice") or {}
    vendor = (invoice.get("vendor") or "").strip().lower()
    contract = next(
        (c for c in _contracts_for_user(user_id).values() if c.get("vendor_name", "").strip().lower() == vendor),
        None,
    )

    if contract is None:
        return {
            "eval_type": "manual_contract",
            "reference": None,
            "checks": [],
            "passed": None,
            "note": "No manually-added vendor contract on file for this invoice's vendor, "
                    "and no ground_truth.json entry either — nothing to evaluate against.",
        }

    checks: list = []

    amount = invoice.get("amount")
    max_amount = contract.get("max_invoice_amount")
    if amount is not None and max_amount is not None:
        _eval_check(checks, "amount_within_limit", f"<= {max_amount}", amount, passed=amount <= max_amount)

    try:
        from datetime import date
        d1 = date.fromisoformat(invoice.get("invoice_date"))
        d2 = date.fromisoformat(invoice.get("due_date"))
        actual_terms = (d2 - d1).days
        expected_terms = contract.get("payment_terms_days")
        _eval_check(checks, "payment_terms_days", expected_terms, actual_terms)
    except (TypeError, ValueError):
        pass  # invoice_date/due_date missing or unparseable — skip this check

    compliance = entry.get("compliance") or {}
    act_compliance = compliance.get("overall_status") or compliance.get("status")
    expected_compliance = "pass" if all(c["passed"] for c in checks) else "fail"
    _eval_check(checks, "compliance_status_consistency", expected_compliance, act_compliance)

    return {
        "eval_type": "manual_contract",
        "reference": contract.get("vendor_name"),
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
    }


def _load_eval_module(name: str):
    """Loads e.g. EVALS_DIR/eval_guardrails.py as a module by file path,
    so its own `os.path.dirname(__file__)`-relative lookups (ground_truth.json,
    '..' for core/agents/tools) resolve exactly the way they do when you
    run `python eval_guardrails.py` directly."""
    path = EVALS_DIR / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — set EVALS_DIR if your evals live elsewhere")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

app = FastAPI(title="LedgerLens AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Health check — used by Docker HEALTHCHECK and load balancers
# ------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health():
    """Simple liveness probe. Returns 200 when the server is up."""
    return {"status": "ok"}


# ------------------------------------------------------------------
# Request/response shapes
# ------------------------------------------------------------------

class DecisionRequest(BaseModel):
    action: str                    # "approve" | "reject" | "edit"
    note: Optional[str] = None     # used for "reject"
    fields: Optional[dict] = None  # used for "edit" — same field=value
                                    # convention edit_invoice_node expects


class SampleInvoiceRequest(BaseModel):
    path: str  # one of the relative paths returned by GET /api/sample-invoices


class PricingRuleRow(BaseModel):
    item: str
    min: float
    max: float


class TempContractRequest(BaseModel):
    """Shape of the manual 'Add Vendor Contract' form. `vendor_name`
    must match (case-insensitive) whatever extraction pulls out of the
    invoice PDF as `invoice.vendor`, or the compliance check won't find
    this contract."""
    vendor_name: str
    gstin: str
    payment_terms_days: int
    max_invoice_amount: float
    discount_percentage: Optional[float] = None
    pricing_rules: list[PricingRuleRow] = []
    clauses: list[str] = []
    previous_vendor_name: Optional[str] = None


class BatchRunRequest(BaseModel):
    upload_ids: list[str]


def _contract_from_request(body: TempContractRequest) -> dict:
    """Convert the shared contract form into the compliance lookup shape."""
    contract = {
        "vendor_name": body.vendor_name.strip(),
        "gstin": body.gstin.strip(),
        "payment_terms_days": body.payment_terms_days,
        "max_invoice_amount": body.max_invoice_amount,
        "pricing_rules": {
            row.item.strip(): {"min": row.min, "max": row.max}
            for row in body.pricing_rules if row.item.strip()
        },
        "clauses": [c.strip() for c in body.clauses if c.strip()],
    }
    if body.discount_percentage is not None:
        contract["discount_percentage"] = body.discount_percentage
    return contract


# ------------------------------------------------------------------
# Supabase Storage helpers — private "invoices" bucket
# ------------------------------------------------------------------

def _storage_path_for(user_id: str, folder: str, name: str) -> str:
    return f"{user_id}/{folder}/{name}"


def _upload_pdf(storage_path: str, contents: bytes) -> None:
    db = _require_service_client()
    db.storage.from_(INVOICE_BUCKET).upload(
        storage_path,
        contents,
        file_options={"content-type": "application/pdf", "upsert": "true"},
    )


def _download_pdf_bytes(storage_path: str) -> bytes:
    db = _require_service_client()
    return db.storage.from_(INVOICE_BUCKET).download(storage_path)


def _delete_pdf(storage_path: str) -> None:
    if supabase_service is None:
        return
    try:
        supabase_service.storage.from_(INVOICE_BUCKET).remove([storage_path])
    except Exception:
        pass  # best-effort cleanup only


@contextmanager
def _local_pdf_path(source_path: str):
    """Yields a local filesystem Path the graph can read `invoice_path`
    from, regardless of whether the PDF lives in the private Storage
    bucket (a real user upload) or on local disk (a bundled sample
    invoice, shared by everybody). Storage-backed files are downloaded
    to a short-lived scratch file and cleaned up on exit; sample files
    are left untouched since they're permanent app assets."""
    if source_path.startswith(SUPABASE_SOURCE_PREFIX):
        storage_path = source_path[len(SUPABASE_SOURCE_PREFIX):]
        contents = _download_pdf_bytes(storage_path)
        scratch = SCRATCH_DIR / f"{uuid.uuid4().hex}_{Path(storage_path).name}"
        scratch.write_bytes(contents)
        try:
            yield scratch
        finally:
            scratch.unlink(missing_ok=True)
    else:
        yield Path(source_path)


# ------------------------------------------------------------------
# State serialization — GraphState (Pydantic models + tuples) -> the
# plain JSON shape app.js expects, persisted to the invoices table.
# ------------------------------------------------------------------

# A decision is "terminal" once the graph has actually resolved it —
# an "edit" resume produces a fresh interrupt (still pending), so it's
# not in this set. Only approve/reject ever end here.
_TERMINAL_DECISION_STATUSES = {"approved_exported", "rejected"}


def _serialize(thread_id: str, filename: str, created_at: str, raw_state: dict, *, user_id: str,
                source_path: str | None = None, contract: dict | None = None,
                decided_at: str | None = None) -> dict:
    invoice = raw_state.get("invoice")
    compliance = raw_state.get("compliance_result")
    risk = raw_state.get("risk_result")
    violations = raw_state.get("guardrail_violations") or []

    interrupts = raw_state.get("__interrupt__")
    pending_approval = interrupts[0].value if interrupts else None

    decision = raw_state.get("decision")
    decision_status = decision.get("status") if isinstance(decision, dict) else decision
    # Stamp decided_at exactly once, the moment a decision first becomes
    # terminal — callers pass through any existing value (e.g. from the
    # row this invoice already had) so a later re-serialize never
    # overwrites it. This is what makes "invoices decided in the last N
    # days" (what Reports actually wants) distinct from "invoices
    # created in the last N days" (created_at, stamped once at upload).
    if decided_at is None and decision_status in _TERMINAL_DECISION_STATUSES:
        decided_at = _utc_now()

    obj = {
        "thread_id": thread_id,
        "user_id": user_id,
        "filename": filename,
        "created_at": created_at,
        "decided_at": decided_at,
        "source_path": source_path,
        "invoice": invoice.model_dump(mode="json") if invoice else None,
        "extraction_error": raw_state.get("extraction_error"),
        "guardrail_passed": raw_state.get("guardrail_passed"),
        "guardrail_violations": [[name, detail] for name, detail in violations],
        "compliance": compliance.model_dump(mode="json") if compliance else None,
        "risk": risk.model_dump(mode="json") if risk else None,
        "decision": decision,
        "pending_approval": pending_approval,
        "contract": contract,
    }

    db = _require_service_client()
    db.table("invoices").upsert(obj, on_conflict="thread_id").execute()
    return obj


def _config_for(thread_id: str, user_id: str | None = None) -> dict:
    return {"configurable": {
        "thread_id": thread_id,
        "supabase_client": supabase_service,
        "user_id": user_id,
    }}


def _run_graph(config: dict, *, user_id: str, initial_input: dict | None = None, resume_payload: dict | None = None) -> dict:
    token = ACTIVE_USER_ID.set(user_id)
    try:
        if resume_payload is not None:
            return graph_app.invoke(Command(resume=resume_payload), config=config)
        return graph_app.invoke(initial_input, config=config)
    except Exception as e:
        # Surfaces provider/graph errors (bad API key, Supabase down, a
        # genuinely malformed PDF, etc.) as a normal HTTP error instead
        # of a raw 500 traceback with no message.
        raise HTTPException(status_code=500, detail=f"Pipeline error: {e}") from e
    finally:
        ACTIVE_USER_ID.reset(token)


def _get_invoice_row(thread_id: str) -> Optional[dict]:
    db = _require_service_client()
    res = db.table("invoices").select("*").eq("thread_id", thread_id).limit(1).execute()
    return res.data[0] if res.data else None


def _owned_invoice(thread_id: str, user_id: str) -> dict:
    entry = _get_invoice_row(thread_id)
    if entry is None or entry.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Unknown invoice")
    return entry


def _restore_pending_graph_if_needed(entry: dict, user_id: str) -> None:
    """MemorySaver does not survive a process restart. If this process
    hasn't touched this thread_id yet, re-run the saved PDF to restore
    the paused approval state before accepting the user's decision."""
    thread_id = entry["thread_id"]
    if thread_id in LIVE_GRAPH_THREADS:
        return
    source_path = entry.get("source_path")
    if not source_path:
        raise HTTPException(status_code=409, detail="This pending invoice can no longer be restored")
    contract = entry.get("contract") or {}
    with _local_pdf_path(source_path) as local_path:
        if not local_path.exists():
            raise HTTPException(status_code=409, detail="This pending invoice can no longer be restored")
        _run_graph(
            _config_for(thread_id, user_id=user_id), user_id=user_id,
            initial_input={"invoice_path": str(local_path), "discount_percentage": contract.get("discount_percentage", 0)},
        )
    LIVE_GRAPH_THREADS.add(thread_id)


# ------------------------------------------------------------------
# Routes — Auth
# ------------------------------------------------------------------

@app.post("/api/auth/signup")
async def signup(body: AuthRequest):
    if supabase_auth is None:
        raise HTTPException(status_code=500, detail="Auth is not configured on this server")
    try:
        result = supabase_auth.auth.sign_up({"email": body.email, "password": body.password})
    except Exception as e:
        raise HTTPException(status_code=400, detail=_auth_error_message(e, "Could not create account")) from e

    if result.session is None:
        # Email confirmation is required before a session is issued —
        # this is a Supabase project setting, not something this code
        # controls. The account (and its empty invoice/contract history)
        # already exists at this point.
        return {
            "confirmation_required": True,
            "email": body.email,
            "message": "Account created. Check your email to confirm it, then log in.",
        }

    return {
        "confirmation_required": False,
        "access_token": result.session.access_token,
        "refresh_token": result.session.refresh_token,
        "user_id": result.user.id,
        "email": result.user.email,
    }


@app.post("/api/auth/login")
async def login(body: AuthRequest):
    if supabase_auth is None:
        raise HTTPException(status_code=500, detail="Auth is not configured on this server")
    try:
        result = supabase_auth.auth.sign_in_with_password({"email": body.email, "password": body.password})
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid email or password") from e
    if result.session is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {
        "access_token": result.session.access_token,
        "refresh_token": result.session.refresh_token,
        "user_id": result.user.id,
        "email": result.user.email,
    }


@app.post("/api/auth/logout")
async def logout(user_id: str = Depends(_get_user_id)):
    # JWTs are stateless — there's nothing server-side to invalidate for
    # the demo auth flow beyond the client discarding its tokens. This
    # endpoint exists mainly so the frontend has a single place to call
    # (and a natural spot to plug in `auth.admin.sign_out` if you later
    # add refresh-token revocation).
    return {"success": True}


@app.get("/api/auth/me")
async def me(user_id: str = Depends(_get_user_id)):
    return {"user_id": user_id}


# ------------------------------------------------------------------
# Routes — Invoices / approval queue / staged uploads
# ------------------------------------------------------------------

@app.get("/api/invoices")
async def list_invoices(user_id: str = Depends(_get_user_id)):
    """Backs the dashboard/all-invoices/reports/routing pages and the
    approval queue (client filters for pending_approval). New users
    simply have no rows yet — nothing special to initialize."""
    db = _require_service_client()
    res = db.table("invoices").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return res.data or []


@app.post("/api/invoices")
async def stage_invoice(file: UploadFile = File(...), user_id: str = Depends(_get_user_id)):
    """Step 1: stage a PDF in the private Storage bucket; it cannot
    enter the graph yet."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    contents = await file.read()
    _validate_pdf_bytes(contents)

    db = _require_service_client()
    upload_id = uuid.uuid4().hex
    storage_path = _storage_path_for(user_id, "staged", f"{upload_id}_{file.filename}")
    _upload_pdf(storage_path, contents)

    row = {
        "upload_id": upload_id,
        "user_id": user_id,
        "filename": file.filename,
        "source_path": f"{SUPABASE_SOURCE_PREFIX}{storage_path}",
        "created_at": _utc_now(),
        "contract": None,
    }
    db.table("staged_uploads").insert(row).execute()
    return {"upload_id": upload_id, "filename": file.filename, "status": "awaiting_contract"}


@app.get("/api/staged-invoices")
async def list_staged_invoices(user_id: str = Depends(_get_user_id)):
    """Only browser-uploaded PDFs live in this collection; samples are
    intentionally excluded, so they can never enter uploaded batch mode."""
    db = _require_service_client()
    res = db.table("staged_uploads").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    return [
        {
            "upload_id": staged["upload_id"],
            "filename": staged["filename"],
            "created_at": staged["created_at"],
            "contract": staged.get("contract"),
            "status": "ready_to_run" if staged.get("contract") else "awaiting_contract",
        }
        for staged in (res.data or [])
    ]


def _get_owned_staged(upload_id: str, user_id: str) -> dict:
    db = _require_service_client()
    res = db.table("staged_uploads").select("*").eq("upload_id", upload_id).limit(1).execute()
    staged = res.data[0] if res.data else None
    if staged is None or staged.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Unknown staged upload")
    return staged


@app.get("/api/invoices/{upload_id}/file")
async def get_staged_invoice_file(upload_id: str, user_id: str = Depends(_get_user_id)):
    """Serves the raw PDF bytes for one of the current user's staged
    (New Invoices) uploads, so the frontend can open it directly in a
    tab. _get_owned_staged 404s if the upload doesn't belong to
    user_id, so one user can never fetch another user's PDF.
    content_disposition_type="inline" is what makes the browser open
    the PDF in its built-in viewer instead of downloading it."""
    staged = _get_owned_staged(upload_id, user_id)
    source_path = staged["source_path"]
    if not source_path.startswith(SUPABASE_SOURCE_PREFIX):
        raise HTTPException(status_code=404, detail="Staged upload has no readable file")
    storage_path = source_path[len(SUPABASE_SOURCE_PREFIX):]
    contents = _download_pdf_bytes(storage_path)
    return Response(
        content=contents,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{staged["filename"]}"'},
    )


@app.post("/api/invoices/{upload_id}/contract")
async def set_staged_invoice_contract(upload_id: str, body: TempContractRequest, user_id: str = Depends(_get_user_id)):
    """Step 2: bind a per-user contract to this staged PDF."""
    staged = _get_owned_staged(upload_id, user_id)
    if not body.vendor_name.strip() or not body.gstin.strip():
        raise HTTPException(status_code=400, detail="vendor_name and gstin are required")
    if body.discount_percentage is not None and not 0 <= body.discount_percentage <= 100:
        raise HTTPException(status_code=400, detail="discount_percentage must be between 0 and 100")

    _ensure_contract_capacity(user_id, body.vendor_name, body.previous_vendor_name)
    contract = _contract_from_request(body)
    contract["created_at"] = _utc_now()

    db = _require_service_client()
    db.table("staged_uploads").update({"contract": contract}).eq("upload_id", upload_id).execute()
    # Compliance looks up contracts by extracted vendor name for this user.
    _upsert_temp_contract(user_id, contract, previous_vendor_name=body.previous_vendor_name)
    return {"upload_id": upload_id, "status": "ready_to_run", "contract": contract}


@app.post("/api/invoices/{upload_id}/run")
async def run_staged_invoice(upload_id: str, user_id: str = Depends(_get_user_id)):
    """Step 3: explicitly invoke the graph after the contract step."""
    staged = _get_owned_staged(upload_id, user_id)
    if staged.get("contract") is None:
        raise HTTPException(status_code=409, detail="Add a vendor contract before running this invoice")
    _ensure_invoice_capacity(user_id)

    thread_id = uuid.uuid4().hex
    with _local_pdf_path(staged["source_path"]) as local_path:
        raw_state = _run_graph(
            _config_for(thread_id, user_id=user_id), user_id=user_id,
            initial_input={
                "invoice_path": str(local_path),
                "discount_percentage": staged["contract"].get("discount_percentage", 0),
            },
        )
    LIVE_GRAPH_THREADS.add(thread_id)
    result = _serialize(
        thread_id, staged["filename"], staged["created_at"], raw_state,
        user_id=user_id, source_path=staged["source_path"], contract=staged["contract"],
    )

    db = _require_service_client()
    db.table("staged_uploads").delete().eq("upload_id", upload_id).execute()
    return result


@app.post("/api/invoices/batch-run")
async def run_uploaded_invoice_batch(body: BatchRunRequest, user_id: str = Depends(_get_user_id)):
    """Run only explicitly selected browser uploads, sequentially, after
    every selected invoice has its own contract. Sample files cannot reach
    this endpoint because they are never staged uploads."""
    upload_ids = list(dict.fromkeys(body.upload_ids))
    if not 2 <= len(upload_ids) <= 5:
        raise HTTPException(status_code=400, detail="Select at least 2 and at most 5 uploaded invoices")
    _ensure_invoice_capacity(user_id, len(upload_ids))

    staged_invoices = [_get_owned_staged(upload_id, user_id) for upload_id in upload_ids]
    for staged in staged_invoices:
        if staged.get("contract") is None:
            raise HTTPException(status_code=409, detail=f"Add a vendor contract for {staged['filename']} before batch run")

    db = _require_service_client()
    results = []
    for staged in staged_invoices:
        thread_id = uuid.uuid4().hex
        with _local_pdf_path(staged["source_path"]) as local_path:
            raw_state = _run_graph(
                _config_for(thread_id, user_id=user_id), user_id=user_id,
                initial_input={
                    "invoice_path": str(local_path),
                    "discount_percentage": staged["contract"].get("discount_percentage", 0),
                },
            )
        LIVE_GRAPH_THREADS.add(thread_id)
        result = _serialize(
            thread_id, staged["filename"], staged["created_at"], raw_state,
            user_id=user_id, source_path=staged["source_path"], contract=staged["contract"],
        )
        db.table("staged_uploads").delete().eq("upload_id", staged["upload_id"]).execute()
        results.append(result)
    return {"results": results}


@app.get("/api/sample-invoices")
async def list_sample_invoices():
    """Backs the 'try a sample invoice' picker on the Upload page — no
    upload needed, just runs an existing PDF from SAMPLE_DIRS through
    the same graph.invoke() the real upload endpoint uses. Bundled with
    the app, so this stays open to everybody, logged in or not."""
    items = []
    for category, folder in SAMPLE_DIRS.items():
        if not folder.exists():
            continue
        for pdf_path in sorted(folder.glob("*.pdf")):
            items.append({
                "filename": pdf_path.name,
                # .as_posix() forces forward slashes even on Windows —
                # the frontend embeds this string into an inline onclick
                # handler, and a bare backslash there gets silently
                # swallowed by the JS parser (e.g. "\C" -> "C"), which is
                # what was producing the "unknown sample invoice path"
                # error with a mangled URL.
                "path": pdf_path.relative_to(Path(__file__).parent).as_posix(),
                "category": category,  # "text" or "scanned"
            })
    return items


@app.get("/api/sample-invoices/file")
async def get_sample_invoice_file(path: str):
    """Serves the raw PDF bytes for a sample invoice so the frontend can
    open it directly in a new tab — the browser (or OS, e.g. Adobe
    Acrobat if that's the configured PDF handler) takes it from there.
    Same path-traversal guard as POST /api/invoices/sample below.
    Public: sample invoices are shared by everybody."""
    resolved = (Path(__file__).parent / path.replace("\\", "/")).resolve()
    allowed_roots = [d.resolve() for d in SAMPLE_DIRS.values()]
    if not resolved.exists() or not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=400, detail="Unknown sample invoice path")
    # content_disposition_type="inline" is what makes the browser/OS open the
    # PDF in a viewer (built-in tab viewer, or Adobe Acrobat if that's the
    # configured default handler) instead of forcing a download. FileResponse
    # defaults to "attachment", which is what was triggering the download.
    return FileResponse(
        resolved,
        media_type="application/pdf",
        filename=resolved.name,
        content_disposition_type="inline",
    )


@app.post("/api/invoices/sample")
async def upload_sample_invoice(body: SampleInvoiceRequest, user_id: str = Depends(_get_user_id)):
    """Same pipeline as POST /api/invoices, just pointed at a file
    already bundled with the deployment instead of a browser upload —
    still counts against this user's rolling invoice quota and is
    still private to them once processed, even though the source PDF
    itself is shared."""
    resolved = (Path(__file__).parent / body.path.replace("\\", "/")).resolve()
    allowed_roots = [d.resolve() for d in SAMPLE_DIRS.values()]
    if not resolved.exists() or not any(resolved.is_relative_to(root) for root in allowed_roots):
        # is_relative_to prevents ../../ path traversal — only files
        # actually inside SAMPLE_DIRS can be run this way.
        raise HTTPException(status_code=400, detail="Unknown sample invoice path")

    _ensure_invoice_capacity(user_id)
    thread_id = uuid.uuid4().hex
    created_at = _utc_now()
    config = _config_for(thread_id, user_id=user_id)
    raw_state = _run_graph(config, user_id=user_id, initial_input={"invoice_path": str(resolved)})
    LIVE_GRAPH_THREADS.add(thread_id)

    return _serialize(thread_id, resolved.name, created_at, raw_state, user_id=user_id, source_path=str(resolved))


@app.post("/api/invoices/{thread_id}/decision")
async def submit_decision(thread_id: str, body: DecisionRequest, user_id: str = Depends(_get_user_id)):
    """Same as prompt_human()'s return value being fed into
    Command(resume=human_input) in approval_cli.py's run() loop — one
    decision, one resume, return whatever the graph does next (which
    may be a fresh interrupt after an edit, or a final decision)."""
    existing = _owned_invoice(thread_id, user_id)
    if not existing.get("pending_approval"):
        raise HTTPException(status_code=409, detail="This invoice isn't awaiting approval")
    if body.action not in ("approve", "reject", "edit"):
        raise HTTPException(status_code=400, detail="action must be 'approve', 'reject', or 'edit'")

    resume_payload: dict = {"action": body.action}
    if body.action == "reject":
        resume_payload["note"] = body.note or ""
    if body.action == "edit":
        resume_payload["fields"] = body.fields or {}

    _restore_pending_graph_if_needed(existing, user_id)
    config = _config_for(thread_id, user_id=user_id)
    raw_state = _run_graph(config, user_id=user_id, resume_payload=resume_payload)

    return _serialize(
        thread_id, existing["filename"], existing["created_at"], raw_state,
        user_id=user_id, source_path=existing.get("source_path"), contract=existing.get("contract"),
        decided_at=existing.get("decided_at"),
    )


@app.post("/api/invoices/{thread_id}/contract-edit")
async def edit_invoice_contract(thread_id: str, body: TempContractRequest, user_id: str = Depends(_get_user_id)):
    """Replace the pending invoice's manual contract, then resume the
    graph through the same edit route used by human approval."""
    existing = _owned_invoice(thread_id, user_id)
    if not existing.get("pending_approval"):
        raise HTTPException(status_code=409, detail="This invoice isn't awaiting approval")
    if not body.vendor_name.strip() or not body.gstin.strip():
        raise HTTPException(status_code=400, detail="vendor_name and gstin are required")
    if body.discount_percentage is not None and not 0 <= body.discount_percentage <= 100:
        raise HTTPException(status_code=400, detail="discount_percentage must be between 0 and 100")

    previous_vendor_name = (existing.get("contract") or {}).get("vendor_name")
    _ensure_contract_capacity(user_id, body.vendor_name, previous_vendor_name)
    contract = _contract_from_request(body)
    contract["created_at"] = _utc_now()
    _upsert_temp_contract(user_id, contract, previous_vendor_name=previous_vendor_name)

    _restore_pending_graph_if_needed(existing, user_id)
    raw_state = _run_graph(
        _config_for(thread_id, user_id=user_id), user_id=user_id,
        resume_payload={"action": "edit", "fields": {}, "contract": contract},
    )
    return _serialize(
        thread_id, existing["filename"], existing["created_at"], raw_state,
        user_id=user_id, source_path=existing.get("source_path"), contract=contract,
        decided_at=existing.get("decided_at"),
    )


@app.get("/api/invoices/{thread_id}")
async def get_invoice(thread_id: str, user_id: str = Depends(_get_user_id)):
    return _owned_invoice(thread_id, user_id)


@app.post("/api/invoices/{thread_id}/evaluate")
async def evaluate_invoice(thread_id: str, user_id: str = Depends(_get_user_id)):
    """Evaluates ONE already-processed invoice — never the whole batch.
    If it's a sample invoice with a ground_truth.json entry, scores
    extraction + guardrail + compliance against that. Otherwise (a
    freshly-uploaded invoice), scores compliance against whatever
    manually-added vendor contract was used for it, if any."""
    entry = _owned_invoice(thread_id, user_id)
    if not entry.get("invoice"):
        raise HTTPException(status_code=400, detail="This invoice hasn't finished processing yet — nothing to evaluate")

    gt_entry = _find_ground_truth_entry(entry["filename"])
    if gt_entry is not None:
        result = _compare_invoice_to_ground_truth(entry, gt_entry)
    else:
        result = _compare_invoice_to_manual_contract(entry, user_id)

    return {"thread_id": thread_id, "filename": entry["filename"], **result}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "supabase_vendor_memory_connected": supabase_client is not None,
        "supabase_persistence_connected": supabase_service is not None,
        "supabase_auth_connected": supabase_auth is not None,
    }


@app.get("/api/contracts")
async def list_contracts():
    """Powers the Vendor Contracts page — the actual vendor_contracts.json
    the Compliance Agent's tools.vectorless_lookup.get_contract() reads
    from, served as-is (no transformation) so what's shown is exactly
    what compliance checks are being run against. Public: these 5
    built-in contracts are shared by everybody."""
    if not CONTRACTS_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail=f"vendor_contracts.json not found at {CONTRACTS_PATH} — set CONTRACTS_PATH if yours lives elsewhere",
        )
    with CONTRACTS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/temp-contracts")
async def list_temp_contracts(user_id: str = Depends(_get_user_id)):
    """Manually added contracts for this account."""
    return list(_contracts_for_user(user_id).values())


@app.post("/api/temp-contracts")
async def add_temp_contract(body: TempContractRequest, user_id: str = Depends(_get_user_id)):
    vendor_name = body.vendor_name.strip()
    if not vendor_name:
        raise HTTPException(status_code=400, detail="vendor_name is required")
    if not body.gstin.strip():
        raise HTTPException(status_code=400, detail="gstin is required")
    if body.discount_percentage is not None and not 0 <= body.discount_percentage <= 100:
        raise HTTPException(status_code=400, detail="discount_percentage must be between 0 and 100")

    _ensure_contract_capacity(user_id, vendor_name, body.previous_vendor_name)
    contract = _contract_from_request(body)
    contract["created_at"] = _utc_now()
    _upsert_temp_contract(user_id, contract, previous_vendor_name=body.previous_vendor_name)
    return contract


@app.delete("/api/temp-contracts/{vendor_key}")
async def delete_temp_contract(vendor_key: str, user_id: str = Depends(_get_user_id)):
    removed = _delete_temp_contract(user_id, vendor_key.strip().lower())
    if removed is None:
        raise HTTPException(status_code=404, detail="No manually-added contract for that vendor")
    return {"deleted": True, "vendor_name": removed["vendor_name"]}


@app.get("/api/evaluations")
async def get_evaluations():
    """Returns the last /api/evaluations/run result without re-running
    anything. has_run=false until you've triggered a run at least once
    (evals don't run automatically on server startup — guardrails/
    compliance/risk are cheap, but extraction costs real API calls)."""
    if EVAL_CACHE is None:
        return {"has_run": False}
    return {"has_run": True, **EVAL_CACHE}


@app.post("/api/evaluations/run")
async def run_evaluations(include_extraction: bool = False):
    """Runs the REAL eval_*.py scripts from EVALS_DIR — same run()
    functions you'd get from `python eval_guardrails.py` etc. Nothing
    about scoring is reimplemented here.

    Guardrails/Compliance/Risk are fast and fully offline (Compliance
    mocks only the RAG clause-retrieval call, same as eval_compliance.py
    does). Extraction needs real GROQ_API_KEY/GEMINI_API_KEY calls and
    your actual sample PDFs on disk, so it's opt-in via
    ?include_extraction=true — expect it to take noticeably longer and
    to make real API calls.
    """
    global EVAL_CACHE

    agents_run: dict = {}
    load_errors: dict = {}

    for name in ("eval_guardrails", "eval_compliance", "eval_risk"):
        try:
            module = _load_eval_module(name)
            agents_run[name] = module.run()
        except Exception as e:
            load_errors[name] = str(e)

    if include_extraction:
        try:
            module = _load_eval_module("eval_extraction")
            agents_run["eval_extraction"] = module.run()
        except Exception as e:
            load_errors["eval_extraction"] = str(e)

    if not agents_run and load_errors:
        # Nothing at all ran — most likely EVALS_DIR is wrong.
        raise HTTPException(status_code=500, detail=f"Could not run any evaluations: {load_errors}")

    EVAL_CACHE = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "include_extraction": include_extraction,
        "agents": agents_run,
        "errors": load_errors,
    }
    return {"has_run": True, **EVAL_CACHE}


# ------------------------------------------------------------------
# Routes — Reports & routing stats
#
# Web equivalents of run_scheduled_report.py / show_routing_stats.py,
# scoped to the authenticated user instead of a --user-id flag, so the
# frontend can trigger a digest / show routing breakdown with a button
# click instead of shelling out to the CLI scripts. Same
# ReportingAgent.run() and compute_routing_stats() the scripts call —
# nothing about the underlying logic is reimplemented here.
# ------------------------------------------------------------------

def _generate_report(user_id: str, *, days: int, report_type: str) -> dict:
    """Shared by /api/report/daily and /api/report/weekly — same
    ReportingAgent.run() + compute_routing_stats() run_scheduled_report.py
    calls, just scoped to whichever window the route asks for."""
    db = _require_service_client()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)

    digest = reporting_agent.run(window_start, now, db, user_id=user_id)
    routing = compute_routing_stats(user_id=user_id)

    return {
        "report_type": report_type,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        **digest.model_dump(),
        "routing_stats": routing,
    }


# @app.get("/api/report/daily")
# async def daily_report(user_id: str = Depends(_get_user_id)):
#     """Yesterday's digest for the signed-in user only — the same digest
#     run_scheduled_report.py --user-id <uid> would produce, just generated
#     on demand instead of on a cron schedule."""
#     return _generate_report(user_id, days=1, report_type="daily")


@app.get("/api/report/weekly")
async def weekly_report(user_id: str = Depends(_get_user_id)):
    """Last-7-days digest for the signed-in user only — the same digest
    run_scheduled_report.py --weekly --user-id <uid> would produce, just
    generated on demand instead of on a cron schedule."""
    return _generate_report(user_id, days=7, report_type="weekly")


@app.get("/api/routing")
async def routing_stats(user_id: str = Depends(_get_user_id)):
    """Per-user routing breakdown ("X% Groq / Y% Gemini / Z% fallback")
    for the routing/dashboard page — the same data
    show_routing_stats.py --user-id <uid> prints, scoped automatically
    to whoever is signed in rather than taking a --user-id flag."""
    return compute_routing_stats(user_id=user_id)


# ------------------------------------------------------------------
# Static frontend — serves index.html / app.js / style.css directly so
# the browser can hit http://localhost:8000/ with no separate server
# and no CORS to worry about.
# ------------------------------------------------------------------

if WEB_DIR.exists():
    @app.get("/")
    async def root():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
else:
    print(f"[warn] WEB_DIR ({WEB_DIR}) not found — frontend won't be served. "
          f"Set WEB_DIR or copy index.html/app.js/style.css into ./web, "
          f"or just serve them separately and set window.LEDGERLENS_API_BASE.")