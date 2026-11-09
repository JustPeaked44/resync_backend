import os
import json
import base64
import time
import uuid
import logging
import re
from html import unescape
from collections import defaultdict, deque
import math
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional
from fastapi import Depends, FastAPI, HTTPException, Query, Body, status, Header, File, UploadFile, Form, Request
from fastapi.responses import JSONResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, WithJsonSchema, field_serializer, model_validator
from dotenv import load_dotenv

# Load .env FIRST — must happen before service imports
# so GEMINI_API_KEY, SUPABASE_URL, etc. are available
# when module-level service instances are created.
# override=True ensures .env values WIN over Windows system env vars.
load_dotenv(override=True)

from services.ingestion import DocumentIngestionService, looks_like_html_document, bounded_download, MAX_UPLOAD_BYTES, READ_CHUNK_BYTES, DOCX_COMPLEX_MESSAGE
from services.paper_check import NOT_A_PAPER_MESSAGE, check_research_paper
from services.ml_client import ml_client
from services.citation import CitationAuditService
from services.db_service import DatabasePersistenceService, RUN_TABLE, RUN_ID, RUN_STATUS, RUN_ERROR, RUN_CREATED, RUN_RESULT
from services.notification import NotificationDeliveryService
from services.credits import credits_service
from services.scoring import compute_citation_integrity
from services.section_cache import SectionCache
from services.v7_scan import (comparable_sections, merge_repeated_sections, manuscript_preview_sections, assemble_flow, explain_flow,
                              four_part_score, manuscript_score, band, MIN_SECTION_WORDS)
from services.reference_parser import extract_intext_citations
from services.gemini import scan_context, scan_status_message
from services.gemini import scan_user
from services.manuscript_title import derive_title
from services.scan_access import locked_scan_view
from services import source_files

logger = logging.getLogger("resync.main")
PROCESS_START = datetime.now(timezone.utc)
INTERRUPTED_GRACE_SECONDS = int(os.getenv("INTERRUPTED_GRACE_SECONDS", "180"))
SCAN_DEADLINE_SECONDS = int(os.getenv("SCAN_DEADLINE_SECONDS", "2700"))
INTERRUPTED_MESSAGE = "This scan was interrupted because our server restarted. Nothing was charged. Please start it again."
TIMEOUT_MESSAGE = "This scan took too long and was stopped. Nothing was charged. Please start it again."
GENERIC_FAILURE_MESSAGE = "We could not finish this scan. Please try again. If it keeps happening, tell us this scan number: {scan_number}."
QUEUE_MESSAGE = "Other scans are running. Yours will start soon."
_active_scans = set()



def _references_text(parsed_sections: Dict[str, str], section_roles: Dict[str, str]) -> str:
    return max((text for heading, text in parsed_sections.items()
                if section_roles.get(heading) == "references"
                and re.sub(r" \(\d+\)$", "", heading.strip().lower()) in ("references", "bibliography")),
               key=len, default="")

gemini_key = os.getenv("GEMINI_API_KEY")
supabase_url = os.getenv("SUPABASE_URL")

from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor


@asynccontextmanager
async def application_lifespan(app):
    executor = ThreadPoolExecutor(max_workers=4)
    asyncio.get_running_loop().set_default_executor(executor)
    async def sweep_later():
        await asyncio.sleep(INTERRUPTED_GRACE_SECONDS)
        await _sweep_interrupted()
    sweep = asyncio.create_task(sweep_later())

    async def warm_up_model():
        """Load the local ML model while the server is idle, so the first scan does not wait ~30 seconds for it (the Modal worker
        has its own warm-up). Best effort: a failure here only means the first scan loads it as before."""
        if ml_client.backend != "local":
            return
        try:
            def load():
                import torch
                from services import v7_logic
                torch.set_num_threads(max(1, int(os.getenv("ML_THREADS", "1"))))
                v7_logic.get_model()
            await asyncio.to_thread(load)
            logger.info("Local ML model is warm")
        except Exception as exc:
            logger.warning("Could not warm up the local ML model: %s", type(exc).__name__)
    warm = asyncio.create_task(warm_up_model())
    try:
        yield
    finally:
        sweep.cancel()
        warm.cancel()
        await asyncio.gather(sweep, warm, return_exceptions=True)
        executor.shutdown(wait=True)


app = FastAPI(
    title="Resync AI API",
    description="Backend API for Research Manuscript Correlation & Inconsistency Detection",
    version="1.0.0",
    lifespan=application_lifespan,
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://resync.vercel.app",
        "https://resync-rqtz.onrender.com",
        "http://localhost:3000",
        "http://localhost:5173",
        "https://nmqc58bh-5173.asse.devtunnels.ms",
        "https://resync-web-m.onrender.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
)

async def _read_upload(file: UploadFile, message: str) -> bytearray:
    # Starlette's multipart file is seekable; reserve once to avoid repeated large reallocations.
    try:
        position = file.file.tell()
        file.file.seek(0, 2)
        expected = max(0, min(MAX_UPLOAD_BYTES, file.file.tell() - position))
        file.file.seek(position)
    except (AttributeError, OSError):
        expected = 0
    data = bytearray(expected)
    used = 0
    while True:
        chunk = await file.read(min(READ_CHUNK_BYTES, MAX_UPLOAD_BYTES - used + 1))
        if not chunk:
            del data[used:]
            return data
        if used + len(chunk) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=message)
        data[used:used + len(chunk)] = chunk
        used += len(chunk)


@app.get("/")
def read_root():
    return {"status": "Resync FastAPI Server is Live!"}

class NotAResearchPaper(HTTPException):
    """The document has no research-paper structure, so it is not scanned (and nothing is charged)."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=NOT_A_PAPER_MESSAGE)

    def __str__(self) -> str:
        return NOT_A_PAPER_MESSAGE


def _scan_concurrency() -> int:
    try:
        limit = int(os.getenv("SCAN_CONCURRENCY", "1"))
    except ValueError:
        limit = 1
    return limit if limit > 0 else 1


# Gemini's rolling request limit is shared by every scan in the process (services/gemini.py), so more scans never exceed
# it: they split it.
SCAN_CONCURRENCY = _scan_concurrency()
SCAN_SLOT = asyncio.Semaphore(SCAN_CONCURRENCY)
# shortcut: tickets live in one process; use durable storage if scans must survive restarts or multiple workers.
_scan_queue = deque()
_scan_durations = deque(maxlen=20)
_scan_workers = []
_running_scan_starts = []

MAX_QUEUED_SCANS = max(0, int(os.getenv("MAX_QUEUED_SCANS", "8")))
MAX_QUEUED_BYTES = max(0, int(os.getenv("MAX_QUEUED_BYTES", str(80 * 1024 * 1024))))
QUEUE_FULL_MESSAGE = "Many scans are waiting right now. Please try again in a few minutes."
USER_FULL_MESSAGE = "You already have scans in progress. Please wait for them to finish."
# Reservations cover provisioning as well as queued and running work; no await occurs in admission.
_scan_reservations = {}
_scan_reservations_by_id = {}


def _check_base64_size(req):
    if req.file_content_base64 and len(req.file_content_base64) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
        raise HTTPException(status_code=413, detail="File exceeds maximum allowed size of 25 MB.")


def _reserve_scan(user_id, size, queued=True):
    if sum(entry["user"] == user_id for entry in _scan_reservations.values()) >= 2:
        raise HTTPException(status_code=429, detail=USER_FULL_MESSAGE)
    waiting = [entry for entry in _scan_reservations.values() if entry["queued"]]
    if queued and (len(waiting) >= MAX_QUEUED_SCANS or sum(entry["bytes"] for entry in waiting) + size > MAX_QUEUED_BYTES):
        raise HTTPException(status_code=429, detail=QUEUE_FULL_MESSAGE)
    token = object()
    _scan_reservations[token] = {"user": user_id, "bytes": size if queued else 0, "queued": queued}
    return token


def _release_reservation(token):
    _scan_reservations.pop(token, None)


def _start_reserved_scan(scan_id):
    token = _scan_reservations_by_id.get(scan_id)
    entry = _scan_reservations.get(token)
    if entry is not None:
        entry["queued"] = False
        entry["bytes"] = 0



def _queue_state(scan_id):
    for position, job in enumerate(_scan_queue, 1):
        if job[1] == scan_id:
            average = sum(_scan_durations) / len(_scan_durations) if _scan_durations else 240
            now = time.monotonic()
            if len(_running_scan_starts) >= SCAN_CONCURRENCY:
                remaining = min(max(0, average - (now - started)) for started in _running_scan_starts)
            else:
                remaining = average if SCAN_SLOT.locked() else 0
            seconds = math.ceil(remaining + ((position - 1) // SCAN_CONCURRENCY) * average)
            return position, seconds, "wait", QUEUE_MESSAGE
    return None, None, None, None


def _enqueue_scan(*job):
    _active_scans.add(job[1])
    _scan_queue.append(job)
    _scan_workers[:] = [worker for worker in _scan_workers if not worker.done()]
    # A live worker that is not running a scan is about to take the next job from the queue.
    idle = len(_scan_workers) - len(_running_scan_starts)
    for _ in range(min(SCAN_CONCURRENCY - len(_scan_workers), len(_scan_queue) - idle)):
        _scan_workers.append(asyncio.create_task(_drain_scan_queue()))


async def _drain_scan_queue():
    while _scan_queue:
        async with SCAN_SLOT:
            if not _scan_queue:
                break
            job = _scan_queue.popleft()
            _start_reserved_scan(job[1])
            started = time.monotonic()
            _running_scan_starts.append(started)
            try:
                await _run_scan_job(*job)
            finally:
                _scan_durations.append(time.monotonic() - started)
                _running_scan_starts.remove(started)
                token = _scan_reservations_by_id.pop(job[1], None)
                _release_reservation(token)
                del job


# ---------------------------------------------------------------------------
# Auth — every endpoint below that reads/writes a specific user's data must
# depend on this and cross-check its result against the client-supplied
# user_id / X-User-Id. Previously those fields were trusted as-is, so any
# caller could act as any user_id. get_user() calls Supabase's Auth server
# to verify the bearer token server-side (not a local/offline JWT decode) --
# no new dependency needed since supabase-py already exposes it.
# ---------------------------------------------------------------------------
async def get_authenticated_user_id(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Include 'Authorization: Bearer <supabase-access-token>'.",
        )
    token = authorization.split(" ", 1)[1].strip()
    db_client = DatabasePersistenceService.get_client()
    try:
        user_resp = await asyncio.to_thread(db_client.auth.get_user, token)
    except Exception as exc:
        logger.warning("Supabase token check failed: %s: %s", type(exc).__name__, str(exc)[:200])
        user_resp = None
    if not user_resp or not user_resp.user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session.")
    return user_resp.user.id


def _assert_owner(authenticated_user_id: str, claimed_user_id: str) -> None:
    """403s if the verified caller isn't the user_id they're claiming to act as."""
    if authenticated_user_id != claimed_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to act on behalf of this user.",
        )


# ---------------------------------------------------------------------------
# Scan rate limiting protects the Gemini free-tier RPM budget for free scans.
# Checked once in
# _provision_analysis_run, the single gate shared by both scan endpoints.
# ponytail: in-memory per-process counter -- resets on restart and doesn't
# share state across multiple server instances. Move to Redis/DB-backed
# counting if this ever runs with >1 worker/instance.
# ---------------------------------------------------------------------------
_SCAN_RATE_LIMIT_WINDOW_S = 300
_SCAN_RATE_LIMIT_MAX = 5
_scan_rate_limit_log: Dict[str, List[float]] = defaultdict(list)
_scan_rate_limit_lock = asyncio.Lock()


async def _check_scan_rate_limit(user_id: str) -> None:
    now = time.monotonic()
    async with _scan_rate_limit_lock:
        recent = [t for t in _scan_rate_limit_log[user_id] if now - t < _SCAN_RATE_LIMIT_WINDOW_S]
        if len(recent) >= _SCAN_RATE_LIMIT_MAX:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Too many scans started recently. Limit is {_SCAN_RATE_LIMIT_MAX} "
                    f"per {_SCAN_RATE_LIMIT_WINDOW_S // 60} minutes."
                ),
            )
        recent.append(now)
        _scan_rate_limit_log[user_id] = recent


# ---------------------------------------------------------------------------
# Debug/test endpoints — unauthenticated and uncredited, so they can burn
# real Gemini quota for free. Fine for local dev; set ENABLE_TEST_ENDPOINTS=
# false in production to 404 them.
# ---------------------------------------------------------------------------
ENABLE_TEST_ENDPOINTS = os.getenv("ENABLE_TEST_ENDPOINTS", "true").strip().lower() not in ("false", "0", "no")


def _require_test_endpoints_enabled() -> None:
    if not ENABLE_TEST_ENDPOINTS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


class V7Section(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., min_length=1, max_length=60000)


class V7AnalyzeRequest(BaseModel):
    # Order matters: sections are compared chronologically, each against the next.
    sections: Optional[List[V7Section]] = Field(default=None, max_length=30)
    manuscript_id: Optional[str] = Field(
        default=None,
        description="A manuscript this user already scanned. Its saved sections are used instead of the ones in `sections`.")

    @model_validator(mode="after")
    def _needs_sections_or_a_saved_manuscript(self):
        if self.sections is not None and len(self.sections) < 2:
            raise ValueError("sections needs at least 2 items")
        if self.sections is None and not self.manuscript_id:
            raise ValueError("Send `sections`, or the `manuscript_id` of a manuscript you already scanned.")
        return self


@app.post("/analyze_v7")
async def analyze_v7(req: V7AnalyzeRequest, user_id: str = Depends(get_authenticated_user_id)):
    """v7 correlation of consecutive manuscript sections, returned as a nested manuscript_flow."""
    await _check_scan_rate_limit(user_id)
    if req.sections is not None:
        sections = [s.model_dump() for s in req.sections]
    else:
        # The manuscript was parsed when it was scanned: read its saved sections instead of parsing the file again.
        try:
            uuid.UUID(req.manuscript_id)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="manuscript_id must be a UUID.")
        saved = await SectionCache.load_for_manuscript(req.manuscript_id, user_id)
        if not saved:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="No saved sections were found for this manuscript. Scan it once first, or send `sections`.")
        parsed = saved["parse_result"]
        roles = parsed.get("section_roles", {})
        sections = [{**section, "role": roles.get(section["name"])} for section in comparable_sections(
            parsed["parsed_sections"], parsed.get("section_order", list(parsed["parsed_sections"])), roles)]
        if len(sections) < 2:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="This manuscript has fewer than 2 sections to compare.")
    async with SCAN_SLOT:
        try:
            result = await asyncio.to_thread(ml_client.analyze_v7, sections)
        except Exception:
            logger.exception("v7 analysis failed")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="v7 model is unavailable.")

        manuscript_flow = assemble_flow(sections, result["pairs"])
        findings, recommendations, synthesis = await explain_flow(sections, manuscript_flow)
    _, dimensions, dimension_weights = four_part_score(manuscript_flow)
    score, breakdown = manuscript_score(manuscript_flow, result.get("transition_averages"))
    return {"result_version": "v7", "manuscript_score": score, "dimension_breakdown": breakdown,
            "dimensions": dimensions, "dimension_weights": dimension_weights,
            "manuscript_flow": manuscript_flow, "inconsistencies": findings,
            "recommendations": recommendations, "ai_synthesis": synthesis}


@app.get("/api/test-ingestion")
async def test_ingestion(doc_url: str = Query(..., description="Public Google Docs Link")):
    """Endpoint to test Google Docs text extraction."""
    _require_test_endpoints_enabled()
    extracted_text = await google_doc_text(doc_url)
    return {
        "status": "success",
        "doc_url": doc_url,
        "character_count": len(extracted_text),
        "text_preview": extracted_text[:500] + "..."
    }

@app.post("/api/test-upload-ingestion")
async def test_upload_ingestion(file: UploadFile = File(..., description="Manuscript file (.docx or .pdf)")):
    """Endpoint to test .docx / .pdf file text extraction."""
    _require_test_endpoints_enabled()
    file_bytes = await _read_upload(file, "File exceeds maximum allowed size of 25 MB.")
    extracted_text = await DocumentIngestionService.extract_plaintext_from_upload(file_bytes, file.filename or "uploaded.docx")
    return {
        "status": "success",
        "filename": file.filename,
        "character_count": len(extracted_text),
        "text_preview": extracted_text[:500] + "..."
    }

@app.post("/api/test-parser")
async def test_parser(
    doc_url: str = Query(..., description="Public Google Docs Link"),
    template_toc: Optional[List[str]] = Body(
        default=[], 
        description="Optional custom Department TOC headers list. Leave empty [] to use default TOC template."
    )
):
    """
    Endpoint to test spaCy Section Parsing & Missing Section Detection
    against a mandatory Department Template TOC.
    """
    _require_test_endpoints_enabled()
    from services.parser import ManuscriptParserService
    raw_text = await google_doc_text(doc_url)
    parse_result = ManuscriptParserService.parse_manuscript_sections(raw_text, template_toc)
    
    return {
        "status": "success",
        "doc_url": doc_url,
        "analysis": parse_result
    }

# ---------------------------------------------------------------------------
# Master Production Scan Endpoint
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    """Request body for the master production scan pipeline."""
    user_id: str = Field(..., description="Supabase auth user UUID")
    manuscript_id: Optional[str] = Field(
        default=None,
        description=(
            "UUID of an existing manuscript row in public.manuscript. Omit to "
            "have the pipeline create a new manuscript row automatically from "
            "manuscript_title + doc_url — every scan-and-go submission is its "
            "own manuscript rather than all users sharing one hardcoded row."
        ),
    )
    manuscript_title: Optional[str] = Field(
        default=None,
        description="Used to create a new manuscript row when manuscript_id is omitted.",
    )
    doc_url: Optional[str] = Field(
        default=None,
        description="Public Google Docs share URL or document source identifier."
    )
    file_content_base64: Optional[str] = Field(
        default=None,
        description="Optional base64-encoded .docx or .pdf file payload for JSON scan submissions."
    )
    filename: Optional[str] = Field(
        default=None,
        description="Optional document filename (e.g. 'Thesis.docx')."
    )
    template_toc: Optional[List[str]] = Field(
        default=None,
        description="Optional ordered list of mandatory section headers for the Department TOC template."
    )
    style_reference_url: Optional[str] = Field(
        default=None,
        description="Optional URL to a style reference document (reserved for future use)."
    )


class InconsistencyReport(BaseModel):
    """A quote-verified v7 finding."""
    section_a: str
    section_b: str
    pair_order: int
    coherence_score: float
    explanation_what: str
    explanation_why: str
    suggested_fix: str
    inconsistency_id: str = ""
    evidence_a: str = ""
    evidence_b: str = ""
    evidence_quotes: List[Dict[str, str]] = []
    evidence_verified: bool = False
    finding_status: str = "material_issue"


class RecommendationReport(BaseModel):
    recommendation_id: str
    id: str
    section: str
    section_a: str
    section_b: str
    pair_order: int
    category: Optional[Literal["objectives", "methodology", "claims", "numeric"]] = Field(
        default=None, exclude_if=lambda value: value is None)
    original_sentence: Optional[str]
    issue: str = ""
    kind: Literal["edit", "add"] = "edit"
    placement: Optional[Literal["start", "end"]] = Field(default=None, exclude_if=lambda value: value is None)
    recommended_revision: str
    recommendation_text: str
    recommendation_priority: str


class CitationReport(BaseModel):
    """Serialisable representation of a single citation audit result."""
    citation_raw_reference_text: str
    citation_is_accessible: bool
    citation_status: str = "no_link"
    citation_primary_link: Optional[str] = None
    citation_authors_parsed: Optional[str] = None
    citation_year_parsed: Optional[int] = None
    citation_crossref_title: Optional[str] = None
    citation_title_match_score: Optional[float] = None
    citation_is_cited_in_text: Optional[bool] = None

    @field_serializer("citation_authors_parsed", "citation_crossref_title")
    def serialize_metadata(self, value: Optional[str]) -> Optional[str]:
        return unescape(re.sub(r"<[^>]*>", "", value)) if value is not None else None


class ScoreBreakdown(BaseModel):
    overall_score: float
    band: str
    cross_chapter_coherence_score: float
    citation_integrity_score: Optional[float] = None
    dimensions: Dict[str, Optional[float]]
    dimension_weights: Optional[Dict[str, float]] = None
    dimension_breakdown: Optional[Dict[str, Dict[str, Any]]] = None
    citation_detail: Dict[str, Any]


class AITextIndicatorReport(BaseModel):
    """Advisory-only stylometric indicator. Never factored into the score."""
    overall_score: Optional[float] = None
    section_scores: Dict[str, Optional[float]] = {}
    flagged_sections: List[str] = []
    disclaimer: str = ""


_scanned_manuscripts: Dict[Any, bool] = {}   # (user, text fingerprint) of scans that finished, so a repeat can say nothing changed


ManuscriptSection = Annotated[Dict[str, Any], WithJsonSchema({
    "type": "object", "additionalProperties": True,
    "properties": {
        "display_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "blocks": {"anyOf": [{"type": "array", "items": {"type": "object", "additionalProperties": True}}, {"type": "null"}]},
    },
})]


class ScanResponse(BaseModel):
    """Consolidated report returned by the master production scan endpoint."""
    status: str
    analysis_run_id: str
    user_id: str
    manuscript_id: str
    doc_url: str
    manuscript_title: Optional[str] = None
    result_version: Optional[str] = None
    overall_coherence_score: float
    sections_analyzed: List[str]
    manuscript_sections: Optional[List[ManuscriptSection]] = None
    access: Optional[Dict[str, Any]] = None
    manuscript_flow: List[Dict[str, Any]]
    inconsistencies_found: int
    inconsistencies: List[InconsistencyReport]
    recommendations: List[RecommendationReport]
    ai_synthesis: Dict[str, Any]
    citations_audited: int
    citations: List[CitationReport]
    score_breakdown: Optional[ScoreBreakdown] = None
    dimension_breakdown: Optional[Dict[str, Dict[str, Any]]] = None
    rescan_unchanged: bool = False  # this exact text was scanned before: nothing in the manuscript has changed since
    structure_note: Optional[str] = None  # set when the paper had no usual headings, so the report can say how it was split
    ai_text_indicator: Optional[AITextIndicatorReport] = None
    db_save_status: str
    notification_dispatched: bool
    credits_remaining: Optional[int] = None


class IssueFeedbackRequest(BaseModel):
    """Request body for submitting user feedback on a specific inconsistency finding."""
    helpful: bool = Field(..., description="True if the finding was useful, False otherwise")
    comment: Optional[str] = Field(default=None, description="Optional free-text comment")


class IssueFeedbackResponse(BaseModel):
    """Response confirming successful feedback submission."""
    status: str
    feedback_id: str

# ---------------------------------------------------------------------------
# Scan pipeline helpers — shared by the synchronous endpoint (/api/scans/run,
# kept for the Android client) and the async job endpoints (/api/scans/start
# + /api/scans/{id}, used by the web app to avoid proxy request timeouts).
# ---------------------------------------------------------------------------

async def _resolve_manuscript_id(db_client: Any, req: ScanRequest, default_source_name: Optional[str] = None) -> str:
    """Returns req.manuscript_id if given (validated to exist), otherwise
    creates a new manuscript row from manuscript_title + doc_url / filename.

    Scan-and-go means every submission is its own manuscript: reusing one
    hardcoded manuscript_id across every user (the previous frontend
    default) made every scan's history entry point at the same row, which
    is why history titles had to fall back to a truncated doc_url instead
    of a real title.
    """
    if req.manuscript_id:
        ms_resp = await asyncio.to_thread(
            lambda: db_client.table("manuscript").select("manuscript_id").eq("manuscript_id", req.manuscript_id).execute()
        )
        if not ms_resp.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Manuscript with ID {req.manuscript_id} not found."
            )
        return req.manuscript_id

    new_manuscript_id = str(uuid.uuid4())
    source_name = req.doc_url or req.filename or default_source_name or "Uploaded Document"
    title = req.manuscript_title or req.filename or req.doc_url or default_source_name or "Untitled Manuscript"
    await asyncio.to_thread(
        lambda: db_client.table("manuscript").insert({
            "manuscript_id": new_manuscript_id,
            "user_id": req.user_id,
            "manuscript_title": title,
            "manuscript_source_url": source_name,
        }).execute()
    )
    logger.info("Created manuscript row %s for user %s", new_manuscript_id, req.user_id)
    return new_manuscript_id


async def _provision_analysis_run(req: ScanRequest, default_source_name: Optional[str] = None) -> tuple[str, str, int]:
    """
    Validates the user, resolves/creates the manuscript, and inserts the
    `analysis_run` row in 'processing' state. Scans are free.

    Returns (analysis_run_id, manuscript_id, credits_remaining).
    """
    await _check_scan_rate_limit(req.user_id)

    analysis_run_id = str(uuid.uuid4())
    try:
        db_client = DatabasePersistenceService.get_client()

        # Bug 2/3: Pre-validation - Verify user_id exists.
        user_resp = await asyncio.to_thread(
            lambda: db_client.table("user").select("user_id").eq("user_id", req.user_id).execute()
        )
        if not user_resp.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {req.user_id} not found."
            )

        manuscript_id = await _resolve_manuscript_id(db_client, req, default_source_name=default_source_name)

        # Bug 4: analysis_run_template_toc is a text column, stringify the list
        toc_str = json.dumps(req.template_toc) if req.template_toc else None
        source_identifier = req.doc_url or req.filename or default_source_name or "uploaded_file.docx"

        # Bug 1: Add user_id to the insert payload
        insertion = asyncio.create_task(asyncio.to_thread(
            lambda: db_client.table(RUN_TABLE).insert({
                RUN_ID: analysis_run_id,
                "user_id": req.user_id,
                "manuscript_id": manuscript_id,
                "analysis_run_status": "processing",
                "analysis_run_template_toc": toc_str,
                "analysis_run_coherence_score": 0,
                # Plain-text mirror of the job state. The polling endpoint reads
                # this rather than the enum column, because the enum has no
                # 'failed' member (see migration 003).
                RUN_STATUS: "processing",
                "doc_url": source_identifier,
            }).execute()
        ))
        try:
            await asyncio.shield(insertion)
        except asyncio.CancelledError:
            while not insertion.done():
                try:
                    await asyncio.shield(insertion)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                insertion.result()
            except Exception:
                logger.exception("Scan insertion failed during cancellation: %s", analysis_run_id)
            raise
        logger.info(f"Provisioned analysis_run row: {analysis_run_id}")
    except asyncio.CancelledError:
        await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
        raise
    except HTTPException:
        raise
    except Exception as exc:
        await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
        logger.error(f"Failed to provision analysis_run in DB: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database setup failed before scan could start."
        )

    try:
        balance = await credits_service.get_balance(req.user_id)
    except BaseException:
        await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
        raise
    return analysis_run_id, manuscript_id, balance


async def _execute_scan_pipeline(
    req: ScanRequest,
    analysis_run_id: str,
    manuscript_id: str,
    credits_remaining: Optional[int] = None,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> ScanResponse:
    """Run the v7 scan and persist the contract response."""
    db_client = DatabasePersistenceService.get_client()

    # ------------------------------------------------------------------
    # Step 1 – Ingestion
    # ------------------------------------------------------------------
    source_identifier = req.doc_url or req.filename or filename or "Uploaded Manuscript"
    from services.parser import ManuscriptParserService

    # A file this user already scanned is not read or parsed again: its sections were saved the first time (SectionCache).
    payload = file_bytes
    if payload is None and req.file_content_base64:
        _check_base64_size(req)
        import base64
        try:
            payload = base64.b64decode(req.file_content_base64)
            req.file_content_base64 = None
            if len(payload) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="File exceeds maximum allowed size of 25 MB.")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid base64 encoding in file_content_base64."
            )
    content_hash = SectionCache.content_hash(payload, req.template_toc) if payload else None
    cached = await SectionCache.find(req.user_id, content_hash) if content_hash else None
    parse_result: Optional[Dict[str, Any]] = None
    source_records = None
    found = None
    if cached:
        raw_text, parse_result = cached["raw_text"], cached["parse_result"]
        logger.info("Scan %s reused the saved sections of manuscript %s", analysis_run_id, cached["manuscript_id"])
    elif payload:
        fname = filename or req.filename or req.doc_url or "document.docx"
        raw_text, source_records = await DocumentIngestionService.extract_plaintext_from_upload(payload, fname, include_metadata=True)
    elif req.doc_url and ("docs.google.com" in req.doc_url or req.doc_url.startswith("http")):
        raw_text = await google_doc_text(req.doc_url)
        # The link had to be fetched, but if its text is unchanged it can still skip the parser.
        content_hash = SectionCache.content_hash(raw_text, req.template_toc)
        found = await SectionCache.find(req.user_id, content_hash)
        parse_result = found["parse_result"] if found else None
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid manuscript provided. Please provide a public Google Docs link or upload a .docx/.pdf file."
        )

    # Has this person already had exactly this text scanned? Then a rescan found nothing new to review.
    seen_key = (req.user_id, content_hash) if content_hash else None
    rescan_unchanged = bool(cached) or bool(found) or (seen_key in _scanned_manuscripts if seen_key else False)

    derived_title = derive_title(raw_text) if req.doc_url and not req.manuscript_title else None

    # ------------------------------------------------------------------
    # Step 2 – Parser
    # ------------------------------------------------------------------
    # Offloaded to a worker thread: these are CPU-bound and would otherwise
    # block the event loop, stalling concurrent status-poll requests.
    if parse_result is None:
        parse_result = await asyncio.to_thread(
            ManuscriptParserService.parse_manuscript_sections, raw_text, req.template_toc, source_records
        )
    source_records = None
    paper = check_research_paper(raw_text, parse_result, has_template=bool(req.template_toc))
    if not paper["ok"]:
        logger.info("Scan %s refused: not a research paper (%s)", analysis_run_id, paper["reason"])
        raise NotAResearchPaper()
    # Saved for this manuscript either way, so /analyze_v7 can find it by manuscript_id (the save does not hold up the scan).
    SectionCache.save_in_background(manuscript_id, req.user_id, content_hash, parse_result, raw_text)
    parsed_sections: Dict[str, str] = parse_result["parsed_sections"]
    section_order: List[str] = parse_result.get("section_order", list(parsed_sections))
    section_roles: Dict[str, str] = parse_result.get("section_roles", {})

    # Score physical neighbors after merging repeated literal headings.
    # A reference list is not prose, so it has no meaningful link to the section before it.
    sections = comparable_sections(parsed_sections, section_order, section_roles)
    structure_note = _structure_note(parse_result.get("structure"), len(sections))
    # The citation audit is network-bound and independent of the model, so it starts now and runs while the local
    # model rates the sections. The Gemini reviews and the AI-text check then run at the same time too; nothing is
    # saved until all of them have finished (Step 6).
    # No 20,000-char slice here anymore — that cap silently truncated a
    # real 150-reference APA list mid-entry. CitationAuditService applies
    # its own much larger DoS-guard cap (150,000 chars) internally.
    references_text = _references_text(parsed_sections, section_roles)
    citation_task = asyncio.create_task(CitationAuditService.audit_citations(references_text, body_text=raw_text))
    # The whole-paper check needs only the sections, so it starts now too and runs while the model rates the pairs.
    from services import full_check as _full_check
    full_check_task = _full_check.start(sections)
    try:
        # The model rates every pair of sections; section roles tell it which far-apart pairs should agree.
        ml_result = await asyncio.to_thread(ml_client.analyze_v7, [{**section, "role": section_roles.get(section["name"])} for section in sections])
        manuscript_flow = assemble_flow(sections, ml_result["pairs"])
        (inconsistencies_data, recommendations, ai_synthesis), ai_text_result = await asyncio.gather(
            explain_flow(sections, manuscript_flow, section_roles, full_check_task=full_check_task),
            asyncio.to_thread(ml_client.analyze_ai_text, {section["name"]: section["text"] for section in sections}),
        )
        citation_audit_result: Dict[str, Any] = await citation_task
    except BaseException:
        citation_task.cancel()  # do not leave the audit running if the scan failed
        if full_check_task is not None:
            full_check_task.cancel()
        await asyncio.gather(*(task for task in (citation_task, full_check_task) if task is not None), return_exceptions=True)
        raise
    _, dimensions, dimension_weights = four_part_score(manuscript_flow)
    overall_score, breakdown = manuscript_score(manuscript_flow, ml_result.get("transition_averages"))

    # ------------------------------------------------------------------
    # Step 5 – Citation Audit (result of the task started above)
    # ------------------------------------------------------------------
    citations: List[Dict[str, Any]] = citation_audit_result["citations"]
    citation_crossmatch: Dict[str, Any] = citation_audit_result["crossmatch"]

    references_section_present: bool = bool(references_text.strip())
    intext_citation_count = len(extract_intext_citations(raw_text))

    # Free the full manuscript string from memory early
    del raw_text

    # ------------------------------------------------------------------
    # Step 5b – Citation Integrity
    # ------------------------------------------------------------------
    citation_integrity_result = compute_citation_integrity(
        citations, citation_crossmatch, intext_citation_count, references_section_present
    )

    score_breakdown_dict = {
        "overall_score": overall_score,
        "band": band(overall_score),
        "cross_chapter_coherence_score": overall_score,
        "citation_integrity_score": citation_integrity_result.score,
        "dimensions": dimensions,
        "dimension_weights": dimension_weights,
        "dimension_breakdown": breakdown,
        "citation_detail": {
            "well_formed_ratio": citation_integrity_result.well_formed_ratio,
            "link_resolution_rate": citation_integrity_result.link_resolution_rate,
            "cross_match_score": citation_integrity_result.cross_match_score,
            "total_entries": citation_integrity_result.total_entries,
        },
    }

    first_free = await credits_service.is_first_scan(req.user_id, analysis_run_id)   # a user's first scan is free
    result = ScanResponse(
        status="completed",
        analysis_run_id=analysis_run_id,
        user_id=req.user_id,
        manuscript_id=manuscript_id,
        doc_url=source_identifier,
        manuscript_title=req.manuscript_title or derived_title,
        result_version="v7",
        overall_coherence_score=overall_score,
        sections_analyzed=[section["name"] for section in sections],
        manuscript_sections=manuscript_preview_sections(sections, parse_result),
        access={"unlocked": False, "unlock_cost": 0 if first_free else 1, "first_free": first_free, "locked_counts": {
            "sections": max(0, len(sections) - 1),
            "recommendations": max(0, len(recommendations) - 2),
            "citations": len(citations)}},
        manuscript_flow=manuscript_flow,
        inconsistencies_found=len(inconsistencies_data),
        inconsistencies=[InconsistencyReport(**item) for item in inconsistencies_data],
        recommendations=[RecommendationReport(**item) for item in recommendations],
        ai_synthesis=ai_synthesis,
        citations_audited=len(citations),
        citations=[CitationReport(
            citation_raw_reference_text=c.get("citation_raw_reference_text", ""),
            citation_is_accessible=c.get("citation_is_accessible", False),
            citation_status=c.get("citation_status", "no_link"),
            citation_primary_link=c.get("citation_primary_link"),
            citation_authors_parsed=c.get("citation_authors_parsed"),
            citation_year_parsed=c.get("citation_year_parsed"),
            citation_crossref_title=c.get("citation_crossref_title"),
            citation_title_match_score=c.get("citation_title_match_score"),
            citation_is_cited_in_text=c.get("citation_is_cited_in_text"),
        ) for c in citations],
        score_breakdown=ScoreBreakdown(**score_breakdown_dict),
        dimension_breakdown=breakdown,
        structure_note=structure_note,
        rescan_unchanged=rescan_unchanged,
        ai_text_indicator=AITextIndicatorReport(**ai_text_result),
        credits_remaining=credits_remaining,
        db_save_status="pending",
        notification_dispatched=False,
    )
    db_result: Dict[str, Any] = await DatabasePersistenceService.save_scan_transaction(
        user_id=req.user_id,
        manuscript_id=manuscript_id,
        analysis_run_id=analysis_run_id,
        coherence_score=overall_score,
        inconsistencies=inconsistencies_data,   # accepts Pydantic models or dicts
        recommendations=recommendations,
        citations=citations,
        crossmatch_issues=citation_crossmatch,
        score_breakdown=score_breakdown_dict,
        ai_text_indicator={
            "overall_score": ai_text_result["overall_score"],
            "section_scores": ai_text_result["section_scores"],
            "flagged_sections": ai_text_result["flagged_sections"],
        },
        doc_url=source_identifier,
        sections_analyzed=[section["name"] for section in sections],
        inconsistencies_found=len(inconsistencies_data),
        citations_audited=len(citations),
    )

    result.db_save_status = db_result.get("status", "unknown")
    manuscript_title: str = req.manuscript_title or derived_title or source_identifier
    try:
        ms_row = (
            db_client
            .table("manuscript")
            .select("manuscript_title")
            .eq("manuscript_id", manuscript_id)
            .single()
            .execute()
        )
        if not derived_title and ms_row and ms_row.data and ms_row.data.get("manuscript_title"):
            manuscript_title = ms_row.data["manuscript_title"]
    except Exception:
        pass  # silently fall back to URL/filename — non-critical

    try:
        result.notification_dispatched = await NotificationDeliveryService.send_scan_completion_alert(
            user_id=req.user_id,
            manuscript_title=manuscript_title,
            coherence_score=int(overall_score),
            analysis_run_id=analysis_run_id
        )
    except Exception:
        logger.exception("Scan notification failed: %s", analysis_run_id)
        result.notification_dispatched = False

    # Publish once with the notification outcome; a failed publish may follow a sent notification.
    try:
        if not await _save_completed_result(result):
            raise RuntimeError("Scan is no longer processing")
    except Exception:
        logger.exception("Failed to publish scan %s after notification", analysis_run_id)
        raise
    if seen_key:
        _scanned_manuscripts[seen_key] = True
        while len(_scanned_manuscripts) > 5000:
            _scanned_manuscripts.pop(next(iter(_scanned_manuscripts)))
    return result



# ---------------------------------------------------------------------------
# Scan Endpoints
# ---------------------------------------------------------------------------

class ScanJobResponse(BaseModel):
    """202 response acknowledging an accepted async scan job."""
    status: str
    analysis_run_id: str
    queue_position: Optional[int] = None
    estimated_wait_seconds: Optional[int] = None
    status_message: Optional[str] = None
    status_message_level: Optional[str] = None
    elapsed_seconds: Optional[int] = None
    failure_kind: Optional[str] = None


class ScanStatusResponse(BaseModel):
    """Polling response: job state, plus the full report once completed."""
    status: str = Field(..., description="processing | completed | failed")
    analysis_run_id: str
    error: Optional[str] = None
    elapsed_seconds: Optional[int] = None
    failure_kind: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    queue_position: Optional[int] = None
    estimated_wait_seconds: Optional[int] = None
    status_message: Optional[str] = Field(default=None, description="Plain-language AI wait or busy notice while processing")
    status_message_level: Optional[str] = Field(default=None, description="info | wait, matching status_message")


async def _save_completed_result(result: ScanResponse) -> bool:
    db_client = DatabasePersistenceService.get_client()
    response = await asyncio.to_thread(
        lambda: db_client.table(RUN_TABLE).update({
            RUN_STATUS: "completed", RUN_RESULT: result.model_dump(mode="json"),
            "analysis_run_status": "completed", "analysis_run_completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq(RUN_ID, result.analysis_run_id).eq(RUN_STATUS, "processing").execute()
    )
    return bool(response.data)


async def _mark_failed(scan_id: str, message: str) -> None:
    for attempt in range(2):
        try:
            client = DatabasePersistenceService.get_client()
            query = client.table(RUN_TABLE).update({RUN_STATUS: "failed", RUN_ERROR: message}) \
                .eq(RUN_ID, scan_id).eq(RUN_STATUS, "processing")
            if message == INTERRUPTED_MESSAGE:
                query = query.is_(RUN_RESULT, "null").lt(RUN_CREATED, PROCESS_START.isoformat())
            await asyncio.to_thread(query.execute)
            return
        except Exception:
            logger.exception("Could not mark scan %s failed (attempt %s)", scan_id, attempt + 1)


def _safe_refusal(message: str) -> bool:
    return message in (NOT_A_PAPER_MESSAGE, DOCX_COMPLEX_MESSAGE) or message.startswith((
        "Google Doc not found.", "Unable to open the document.", "Unable to access document.",
        "Document access restricted.", "Invalid Google Docs URL format.",
        "The submitted Google Doc appears to be empty.", "The uploaded document appears to be empty",
        "Unsupported file format.", "No valid manuscript provided.", "Invalid base64 encoding",
        "This Google Doc is too large", "This Google Doc is larger", "Document is too large to process",
    ))


def _failure_message(scan_id: str, exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return TIMEOUT_MESSAGE
    if isinstance(exc, HTTPException) and isinstance(exc.detail, str):
        detail = re.sub(r"^\d{3}:\s*", "", exc.detail)
        if _safe_refusal(detail):
            return detail
    return GENERIC_FAILURE_MESSAGE.format(scan_number=scan_id[:8])


def _log_scan_memory(scan_id, phase):
    try:
        with open("/proc/self/status", encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    logger.info("Scan %s %s memory %s", scan_id[:8], phase, line.split(":", 1)[1].strip())
                    break
    except OSError:
        pass


async def _run_scan_job(req, analysis_run_id, manuscript_id, credits_remaining, file_bytes=None, filename=None):
    _log_scan_memory(analysis_run_id, "start")
    scan_context.set(analysis_run_id)
    scan_user.set(req.user_id)
    try:
        await asyncio.wait_for(_execute_scan_pipeline(req, analysis_run_id, manuscript_id, credits_remaining,
                               file_bytes=file_bytes, filename=filename), timeout=SCAN_DEADLINE_SECONDS)
        logger.info("Async scan job completed: %s", analysis_run_id)
    except asyncio.CancelledError:
        await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
        raise
    except Exception as exc:
        logger.exception("Async scan job failed: %s", analysis_run_id)
        await _mark_failed(analysis_run_id, _failure_message(analysis_run_id, exc))
    finally:
        _log_scan_memory(analysis_run_id, "end")
        _active_scans.discard(analysis_run_id)


async def _sweep_interrupted():
    try:
        client = DatabasePersistenceService.get_client()
        query = client.table(RUN_TABLE).update({RUN_STATUS: "failed", RUN_ERROR: INTERRUPTED_MESSAGE}) \
            .eq(RUN_STATUS, "processing").is_(RUN_RESULT, "null").lt(RUN_CREATED, PROCESS_START.isoformat())
        for scan_id in tuple(_active_scans):
            query = query.neq(RUN_ID, scan_id)
        await asyncio.to_thread(query.execute)
    except Exception:
        logger.exception("Interrupted scan sweep failed")


@app.post(
    "/api/scans/run",
    response_model=ScanResponse,
    summary="Master Production Scan (synchronous)",
    description=(
        "Runs the full RESYNC analysis pipeline and blocks until it finishes: "
        "Ingestion → Parser → v7 scoring and explanation → Citation → DB → Notification. "
        "Retained for the Android client. Long manuscripts can exceed upstream "
        "proxy request timeouts on this endpoint — new clients should prefer "
        "POST /api/scans/start."
    ),
    tags=["Scans"]
)
async def run_scan(
    req: ScanRequest,
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> ScanResponse:
    """Synchronous free scan; returns the locked result after persistence.

    Only one scan runs at a time (see SCAN_SLOT). Rather than blocking here
    and risking Render's 100s proxy timeout while merely queued, this fails
    fast with 503 so the Android client gets an honest "busy, retry" instead
    of a silent timeout."""
    _assert_owner(authenticated_user_id, req.user_id)
    _check_base64_size(req)
    reservation = _reserve_scan(req.user_id, len(req.file_content_base64 or ""))
    try:
        analysis_run_id, manuscript_id, credits_remaining = await _provision_analysis_run(req)
        _active_scans.add(analysis_run_id)
        try:
            await asyncio.wait_for(SCAN_SLOT.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
            _active_scans.discard(analysis_run_id)
            raise HTTPException(
                status_code=503,
                detail="Another scan is currently in progress. Please retry shortly.",
                headers={"Retry-After": "60"},
            )
        except asyncio.CancelledError:
            await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
            _active_scans.discard(analysis_run_id)
            raise
        _scan_reservations[reservation]["queued"] = False
        _scan_reservations[reservation]["bytes"] = 0
        _log_scan_memory(analysis_run_id, "start")
        scan_context.set(analysis_run_id)
        scan_user.set(req.user_id)
        try:
            result = await asyncio.wait_for(_execute_scan_pipeline(req, analysis_run_id, manuscript_id, credits_remaining), SCAN_DEADLINE_SECONDS)
            return ScanResponse.model_validate(locked_scan_view(result.model_dump(mode="json")))
        except BaseException as exc:
            logger.exception("Synchronous scan failed: %s", analysis_run_id)
            await _mark_failed(analysis_run_id, _failure_message(analysis_run_id, exc))
            raise
        finally:
            _log_scan_memory(analysis_run_id, "end")
            _active_scans.discard(analysis_run_id)
            SCAN_SLOT.release()
    finally:
        _release_reservation(reservation)


@app.post(
    "/api/scans/start",
    response_model=ScanJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start Scan (asynchronous)",
    description=(
        "Validates the request, provisions the analysis_run row, and starts the "
        "pipeline in the background. Returns immediately with the analysis_run_id; "
        "poll GET /api/scans/{analysis_run_id} for progress and the final report. "
        "Use this instead of /api/scans/run when the client sits behind a proxy "
        "or tunnel with a request timeout."
    ),
    tags=["Scans"]
)
async def start_scan(
    req: ScanRequest,
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> ScanJobResponse:
    """
    Accepts a scan job and returns straight away.

    FK validation happens inline so invalid user_id/manuscript_id fails
    fast with 404 instead of failing in the background.
    """
    _assert_owner(authenticated_user_id, req.user_id)
    _check_base64_size(req)
    reservation = _reserve_scan(req.user_id, len(req.file_content_base64 or ""))
    handed_off = False
    try:
        analysis_run_id, manuscript_id, credits_remaining = await _provision_analysis_run(req)
        _scan_reservations_by_id[analysis_run_id] = reservation
        try:
            _enqueue_scan(req, analysis_run_id, manuscript_id, credits_remaining)
            handed_off = True
        except Exception:
            _active_scans.discard(analysis_run_id)
            _scan_queue_copy = [job for job in _scan_queue if job[1] != analysis_run_id]
            _scan_queue.clear()
            _scan_queue.extend(_scan_queue_copy)
            await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
            raise
        logger.info("Accepted async scan job: %s", analysis_run_id)
        position, estimate, level, message = _queue_state(analysis_run_id)
        return ScanJobResponse(status="processing", analysis_run_id=analysis_run_id, queue_position=position,
                               estimated_wait_seconds=estimate, status_message=message, status_message_level=level, elapsed_seconds=0)
    finally:
        if not handed_off:
            _release_reservation(reservation)
            if "analysis_run_id" in locals():
                _scan_reservations_by_id.pop(analysis_run_id, None)


@app.post(
    "/api/scans/upload",
    response_model=ScanJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start Scan from Uploaded File (asynchronous)",
    description=(
        "Upload a .docx or .pdf file directly (max 25MB). Validates the file, "
        "provisions the analysis_run row, and processes the scan in the background. "
        "Poll GET /api/scans/{analysis_run_id} for progress and final results."
    ),
    tags=["Scans"]
)
async def upload_and_start_scan(
    file: UploadFile = File(..., description="Manuscript document (.docx or .pdf)"),
    user_id: str = Form(..., description="Supabase auth user UUID"),
    manuscript_id: Optional[str] = Form(None, description="Optional existing manuscript UUID"),
    manuscript_title: Optional[str] = Form(None, description="Optional manuscript title"),
    template_toc: Optional[str] = Form(None, description="Optional JSON array string of Department TOC headers"),
    style_reference_url: Optional[str] = Form(None, description="Optional style reference URL"),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> ScanJobResponse:
    """
    Accepts an uploaded Word (.docx) or PDF manuscript and starts an asynchronous scan job.
    """
    _assert_owner(authenticated_user_id, user_id)

    # 25 MB max file size
    MAX_FILE_SIZE = 25 * 1024 * 1024
    file_bytes = await _read_upload(file, "File exceeds maximum allowed size of 25 MB.")
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds maximum allowed size of 25 MB."
        )
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty."
        )

    parsed_toc: Optional[List[str]] = None
    if template_toc:
        try:
            parsed_toc = json.loads(template_toc)
            if not isinstance(parsed_toc, list):
                parsed_toc = None
        except Exception:
            pass

    req = ScanRequest(
        user_id=user_id,
        manuscript_id=manuscript_id,
        manuscript_title=manuscript_title or file.filename or "Uploaded Manuscript",
        doc_url=file.filename or "uploaded_file.docx",
        filename=file.filename,
        template_toc=parsed_toc,
        style_reference_url=style_reference_url,
    )

    reservation = _reserve_scan(req.user_id, len(file_bytes))
    handed_off = False
    try:
        analysis_run_id, resolved_manuscript_id, credits_remaining = await _provision_analysis_run(
            req, default_source_name=file.filename
        )
        _scan_reservations_by_id[analysis_run_id] = reservation
        try:
            _enqueue_scan(req, analysis_run_id, resolved_manuscript_id, credits_remaining,
                          file_bytes, file.filename)
            handed_off = True
        except Exception:
            _active_scans.discard(analysis_run_id)
            _scan_queue_copy = [job for job in _scan_queue if job[1] != analysis_run_id]
            _scan_queue.clear()
            _scan_queue.extend(_scan_queue_copy)
            await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
            raise
        logger.info("Accepted async file scan job: %s (file: %s)", analysis_run_id, file.filename)
        position, estimate, level, message = _queue_state(analysis_run_id)
        return ScanJobResponse(status="processing", analysis_run_id=analysis_run_id, queue_position=position,
                               estimated_wait_seconds=estimate, status_message=message, status_message_level=level, elapsed_seconds=0)
    finally:
        if not handed_off:
            _release_reservation(reservation)
            if "analysis_run_id" in locals():
                _scan_reservations_by_id.pop(analysis_run_id, None)


@app.post(
    "/api/scans/upload/run",
    response_model=ScanResponse,
    summary="Run Scan from Uploaded File (synchronous)",
    description="Synchronously runs scan pipeline on an uploaded .docx or .pdf file.",
    tags=["Scans"]
)
async def upload_and_run_scan(
    file: UploadFile = File(..., description="Manuscript document (.docx or .pdf)"),
    user_id: str = Form(..., description="Supabase auth user UUID"),
    manuscript_id: Optional[str] = Form(None, description="Optional existing manuscript UUID"),
    manuscript_title: Optional[str] = Form(None, description="Optional manuscript title"),
    template_toc: Optional[str] = Form(None, description="Optional JSON array string of Department TOC headers"),
    style_reference_url: Optional[str] = Form(None, description="Optional style reference URL"),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> ScanResponse:
    """
    Synchronously runs the analysis pipeline on an uploaded .docx or .pdf file.
    """
    _assert_owner(authenticated_user_id, user_id)

    MAX_FILE_SIZE = 25 * 1024 * 1024
    file_bytes = await _read_upload(file, "File exceeds maximum allowed size of 25 MB.")
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds maximum allowed size of 25 MB."
        )
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty."
        )

    parsed_toc: Optional[List[str]] = None
    if template_toc:
        try:
            parsed_toc = json.loads(template_toc)
            if not isinstance(parsed_toc, list):
                parsed_toc = None
        except Exception:
            pass

    req = ScanRequest(
        user_id=user_id,
        manuscript_id=manuscript_id,
        manuscript_title=manuscript_title or file.filename or "Uploaded Manuscript",
        doc_url=file.filename or "uploaded_file.docx",
        filename=file.filename,
        template_toc=parsed_toc,
        style_reference_url=style_reference_url,
    )

    reservation = _reserve_scan(req.user_id, len(file_bytes))
    try:
        analysis_run_id, resolved_manuscript_id, credits_remaining = await _provision_analysis_run(
            req, default_source_name=file.filename
        )
        _active_scans.add(analysis_run_id)
        try:
            await asyncio.wait_for(SCAN_SLOT.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
            _active_scans.discard(analysis_run_id)
            raise HTTPException(
                status_code=503,
                detail="Another scan is currently in progress. Please retry shortly.",
                headers={"Retry-After": "60"},
            )
        except asyncio.CancelledError:
            await _mark_failed(analysis_run_id, GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8]))
            _active_scans.discard(analysis_run_id)
            raise
        _scan_reservations[reservation]["queued"] = False
        _scan_reservations[reservation]["bytes"] = 0
        _log_scan_memory(analysis_run_id, "start")
        scan_context.set(analysis_run_id)
        scan_user.set(req.user_id)
        try:
            result = await asyncio.wait_for(_execute_scan_pipeline(
                req, analysis_run_id, resolved_manuscript_id, credits_remaining,
                file_bytes=file_bytes, filename=file.filename,
            ), SCAN_DEADLINE_SECONDS)
            return ScanResponse.model_validate(locked_scan_view(result.model_dump(mode="json")))
        except BaseException as exc:
            logger.exception("Synchronous scan failed: %s", analysis_run_id)
            await _mark_failed(analysis_run_id, _failure_message(analysis_run_id, exc))
            raise
        finally:
            _log_scan_memory(analysis_run_id, "end")
            _active_scans.discard(analysis_run_id)
            SCAN_SLOT.release()
    finally:
        _release_reservation(reservation)


@app.get(
    "/api/scans/{analysis_run_id}",
    response_model=ScanStatusResponse,
    summary="Poll Scan Status",
    description=(
        "Returns the current state of a scan job started via POST /api/scans/start. "
        "While running: status='processing'. On success: status='completed' with the "
        "full report in `result`. On failure: status='failed' with `error`."
    ),
    tags=["Scans"]
)
async def get_scan_status(
    analysis_run_id: str,
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> ScanStatusResponse:
    """Polling endpoint backing the async scan job flow."""
    db_client = DatabasePersistenceService.get_client()

    try:
        run_resp = await asyncio.to_thread(
            lambda: db_client
                .table(RUN_TABLE)
                .select(", ".join((RUN_ID, "user_id", RUN_STATUS, RUN_ERROR, RUN_RESULT, RUN_CREATED)))
                .eq(RUN_ID, analysis_run_id)
                .execute()
        )
    except Exception as exc:
        logger.error("Failed to read scan status for %s: %s", analysis_run_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read scan status."
        )

    if not run_resp.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scan {analysis_run_id} not found."
        )

    row = run_resp.data[0]
    # IDOR guard: this row must belong to the authenticated caller. 404
    # (not 403) so an existing-but-foreign analysis_run_id can't be
    # distinguished from a nonexistent one by response code alone.
    if row.get("user_id") != authenticated_user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scan {analysis_run_id} not found."
        )
    job_status = row.get(RUN_STATUS) or "processing"
    created = row.get(RUN_CREATED)
    try:
        created = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else created
        elapsed = max(0, int((datetime.now(timezone.utc) - created).total_seconds())) if created else None
    except (ValueError, TypeError):
        created, elapsed = None, None
    if (job_status == "processing" and not row.get(RUN_RESULT) and created and created < PROCESS_START
            and analysis_run_id not in _active_scans
            and (datetime.now(timezone.utc) - PROCESS_START).total_seconds() >= INTERRUPTED_GRACE_SECONDS):
        await _mark_failed(analysis_run_id, INTERRUPTED_MESSAGE)
        run_resp = await asyncio.to_thread(lambda: db_client.table(RUN_TABLE)
            .select(", ".join((RUN_ID, "user_id", RUN_STATUS, RUN_ERROR, RUN_RESULT, RUN_CREATED)))
            .eq(RUN_ID, analysis_run_id).execute())
        row = run_resp.data[0]
        job_status = row.get(RUN_STATUS) or "processing"

    if job_status == "failed":
        error = row.get(RUN_ERROR) or ""
        generic = GENERIC_FAILURE_MESSAGE.format(scan_number=analysis_run_id[:8])
        if error not in (INTERRUPTED_MESSAGE, TIMEOUT_MESSAGE, generic) and not _safe_refusal(error):
            error = generic
        return ScanStatusResponse(
            status="failed",
            analysis_run_id=analysis_run_id,
            error=error,
            elapsed_seconds=elapsed,
            failure_kind=("interrupted" if error == INTERRUPTED_MESSAGE else
                          "timeout" if error == TIMEOUT_MESSAGE else
                          "error" if error == generic else "refused")
        )

    # Older rows can say completed without a saved report.
    result_json = row.get(RUN_RESULT)
    if job_status == "completed" and result_json:
        return ScanStatusResponse(
            status="completed",
            analysis_run_id=analysis_run_id,
            result=locked_scan_view(result_json)
        )

    position, estimate, level, message = _queue_state(analysis_run_id)
    if position is None:
        level, message = scan_status_message(analysis_run_id)
    return ScanStatusResponse(status="processing", analysis_run_id=analysis_run_id,
                              elapsed_seconds=elapsed, queue_position=position, estimated_wait_seconds=estimate,
                              status_message=message, status_message_level=level)


@app.post("/api/scans/{scan_id}/unlock", tags=["Scans"])
async def unlock_scan(scan_id: str, authenticated_user_id: str = Depends(get_authenticated_user_id)):
    try:
        free = await credits_service.is_first_scan(authenticated_user_id, scan_id)   # decided here, never by the browser
        outcome = await credits_service.unlock_report(authenticated_user_id, scan_id, free=free)
        return {**outcome, "free": True} if free and isinstance(outcome, dict) else outcome
    except Exception as exc:
        message = str(exc)
        if "INSUFFICIENT_CREDITS" in message:
            return JSONResponse(status_code=402, content={
                "detail": "Not enough credits to unlock this report.", "credits_remaining": 0})
        if "SCAN_NOT_FOUND" in message:
            raise HTTPException(status_code=404, detail="Scan not found.") from exc
        if "SCAN_NOT_READY" in message:
            raise HTTPException(status_code=409, detail="Scan is not ready.") from exc
        logger.exception("Failed to unlock scan %s", scan_id)
        raise HTTPException(status_code=503, detail="Could not unlock report.") from exc


import httpx
import re


def _structure_note(structure: Optional[str], parts: int) -> Optional[str]:
    """A plain sentence for papers whose sections were not found from the usual headings."""
    if structure == "split":
        return (f"We could not find section headings in your paper, so it was split into {parts} equal parts and each part was "
                "compared with the next. Clear headings such as Introduction, Methods and Results give more exact results.")
    if structure == "structure":
        return "Your headings are not the usual ones, so they were found from the layout of your paper. Check that the sections look right."
    return None

# In-process cache for manuscript previews: { scan_id: { "text": "...", "expires_at": float } }
# TTL is 10 minutes. Max keys = 100 to prevent OOM.
_manuscript_cache = {}

CACHE_MAX_BYTES = max(0, int(os.getenv("DOCUMENT_CACHE_MAX_BYTES", str(48 * 1024 * 1024))))
CACHE_MAX_ENTRIES = 100


def _cache_size(value):
    import sys
    if isinstance(value, BaseModel):
        return sys.getsizeof(value) + _cache_size(value.__dict__)
    if isinstance(value, dict):
        return sys.getsizeof(value) + sum(_cache_size(k) + _cache_size(v) for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return sys.getsizeof(value) + sum(_cache_size(item) for item in value)
    return sys.getsizeof(value)


def _prune_document_cache(cache, now, expiry):
    for key in list(cache):
        if expiry(cache[key]) <= now:
            del cache[key]
    while cache and (len(cache) > CACHE_MAX_ENTRIES or _cache_size(cache) > CACHE_MAX_BYTES):
        del cache[next(iter(cache))]


def _preview_cache_prune(now):
    _prune_document_cache(_manuscript_cache, now, lambda value: value["expires_at"])


def _outline_cache_prune(now):
    _prune_document_cache(_outline_cache, now, lambda value: value[0] + OUTLINE_CACHE_SECONDS)


@app.get(
    "/api/scans/{scan_id}/manuscript",
    response_model=None,
    summary="Fetch Manuscript Preview",
    tags=["Scans"]
)
async def get_manuscript_preview(
    scan_id: str,
    nocache: bool = False,
    authenticated_user_id: str = Depends(get_authenticated_user_id)
) -> dict:
    """Fetches the full manuscript text from Google Docs for the result preview pane."""
    db_client = DatabasePersistenceService.get_client()
    
    try:
        run_resp = await asyncio.to_thread(
            lambda: db_client
                .table("analysis_run")
                .select("user_id, result_json")
                .eq("analysis_run_id", scan_id)
                .execute()
        )
    except Exception as exc:
        logger.error("Failed to read scan %s for manuscript preview: %s", scan_id, exc)
        return {"available": False, "reason": "unreachable"}

    if not run_resp.data:
        return {"available": False, "reason": "unreachable"}
        
    row = run_resp.data[0]
    
    if row.get("user_id") != authenticated_user_id:
        return {"available": False, "reason": "unreachable"}
        
    result_json = row.get(RUN_RESULT) or {}
    if result_json.get("manuscript_sections"):
        sections = locked_scan_view(result_json).get("manuscript_sections") or []
        return {"available": bool(sections), "text": "\n\n".join(section["text"] or "" for section in sections if section["text"] is not None)}
    if not (result_json.get("access") or {}).get("unlocked"):
        return {"available": False, "reason": "locked"}
    doc_url = result_json.get("doc_url")
    
    if not doc_url or "docs.google.com" not in doc_url:
        raise HTTPException(
            status_code=400,
            detail="Document URL is not a supported Google Docs link."
        )
        
    now = time.time()
    _preview_cache_prune(now)
    if not nocache and scan_id in _manuscript_cache:
        cached = _manuscript_cache[scan_id]
        if now < cached["expires_at"]:
            return {
                "available": True,
                "text": cached["text"],
                "fetched_at": datetime.fromtimestamp(cached["fetched_at"], tz=timezone.utc).isoformat()
            }
        else:
            del _manuscript_cache[scan_id]
            
    match = re.search(r"/document/d/([a-zA-Z0-9-_]+)", doc_url)
    if not match:
        raise HTTPException(
            status_code=400,
            detail="Document URL is not a supported Google Docs link."
        )
    doc_id = match.group(1)
    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    
    _BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA}
        ) as client:
            async with client.stream("GET", export_url) as resp:
                if resp.status_code in (401, 403):
                    return {"available": False, "reason": "private"}
                if resp.status_code == 404:
                    return {"available": False, "reason": "unreachable"}
                resp.raise_for_status()
                raw_text = (await bounded_download(resp)).decode(resp.encoding or "utf-8", errors="replace")

            # Google returns an HTML login-redirect page instead of 403
            # when the doc is private. Catch it here.
            if looks_like_html_document(raw_text):
                return {"available": False, "reason": "private"}

    except Exception as exc:
        logger.warning("Failed to fetch manuscript %s from Google Docs: %s", doc_id, exc)
        return {"available": False, "reason": "google_fetch_failed"}
        
    processed_text = DocumentIngestionService.strip_document_noise(raw_text.lstrip("\ufeff"))
    
    _preview_cache_prune(now)
    _manuscript_cache.pop(scan_id, None)
    _manuscript_cache[scan_id] = {
        "text": processed_text,
        "expires_at": now + 600,
        "fetched_at": now
    }
    _preview_cache_prune(now)
    
    return {
        "available": True,
        "text": processed_text,
        "fetched_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    }


# ---------------------------------------------------------------------------
# Issue Feedback Endpoint
# ---------------------------------------------------------------------------

async def _owned_scan(scan_id: str, user_id: str) -> bool:
    """True when this scan exists and belongs to the signed-in user."""
    client = DatabasePersistenceService.get_client()
    try:
        response = await asyncio.to_thread(lambda: client.table("analysis_run").select("user_id").eq("analysis_run_id", scan_id).execute())
    except Exception as exc:
        logger.error("Could not check the owner of scan %s: %s", scan_id, exc)
        return False
    return bool(response.data) and response.data[0].get("user_id") == user_id


@app.put("/api/scans/{scan_id}/source-file", response_model=None, tags=["Scans"], summary="Keep the original Word file of a scan")
async def put_source_file(scan_id: str, file: UploadFile = File(...), authenticated_user_id: str = Depends(get_authenticated_user_id)) -> dict:
    """Stores the student's original .docx (pictures and all) so the report can show it exactly as written on any device."""
    if not source_files.enabled():
        return {"stored": False, "reason": "disabled"}
    if not await _owned_scan(scan_id, authenticated_user_id):
        raise HTTPException(status_code=404, detail="Scan not found.")
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only Word (.docx) files can be kept.")
    data = await _read_upload(file, "File exceeds maximum allowed size of 25 MB.")
    if not source_files.looks_like_docx(data):
        raise HTTPException(status_code=400, detail="That file is not a Word document.")
    stored = await source_files.save(DatabasePersistenceService.get_client(), authenticated_user_id, scan_id, data)
    return {"stored": stored}


@app.get("/api/scans/{scan_id}/source-file", response_model=None, tags=["Scans"], summary="The original Word file of a scan")
async def get_source_file(scan_id: str, authenticated_user_id: str = Depends(get_authenticated_user_id)):
    from fastapi.responses import Response
    if not source_files.enabled() or not await _owned_scan(scan_id, authenticated_user_id):
        raise HTTPException(status_code=404, detail="No stored file.")
    data = await source_files.load(DatabasePersistenceService.get_client(), authenticated_user_id, scan_id)
    if not data:
        raise HTTPException(status_code=404, detail="No stored file.")
    return Response(content=data, media_type=source_files.DOCX_TYPE, headers={"Cache-Control": "private, no-store"})


@app.post(
    "/api/issues/{issue_id}/feedback",
    response_model=IssueFeedbackResponse,
    status_code=201,
    summary="Submit Issue Feedback",
    description=(
        "Records a user's thumbs-up or thumbs-down reaction to a specific inconsistency "
        "finding. The authenticated user must own the scan that produced this issue."
    ),
    tags=["Feedback"]
)
async def submit_issue_feedback(
    issue_id: str,
    body: IssueFeedbackRequest,
    x_user_id: str = Header(..., alias="X-User-Id", description="Supabase auth user UUID"),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> IssueFeedbackResponse:
    """
    Stores user feedback for a specific inconsistency in public.issue_feedback.

    Security:
    - Validates the caller's bearer token actually belongs to x_user_id.
    - Validates that the inconsistency_id belongs to a scan owned by the authenticated user.
    - Prevents users from attaching feedback to another user's issues.
    """
    _assert_owner(authenticated_user_id, x_user_id)
    try:
        db_client = DatabasePersistenceService.get_client()

        # Security check: verify this inconsistency exists
        ownership_resp = await asyncio.to_thread(
            lambda: db_client
                .table("inconsistency")
                .select("inconsistency_id, analysis_run_id")
                .eq("inconsistency_id", issue_id)
                .execute()
        )
        if not ownership_resp.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Inconsistency {issue_id} not found."
            )

        analysis_run_id = ownership_resp.data[0]["analysis_run_id"]

        # Security check: verify the analysis_run belongs to the requesting user
        run_resp = await asyncio.to_thread(
            lambda: db_client
                .table("analysis_run")
                .select("user_id")
                .eq("analysis_run_id", analysis_run_id)
                .single()
                .execute()
        )
        if not run_resp.data or run_resp.data.get("user_id") != x_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to submit feedback for this issue."
            )

        feedback_id = str(uuid.uuid4())

        await asyncio.to_thread(
            lambda: db_client.table("issue_feedback").insert({
                "feedback_id": feedback_id,
                "inconsistency_id": issue_id,
                "user_id": x_user_id,
                "is_helpful": body.helpful,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        )

        logger.info(
            "Feedback recorded: feedback_id=%s, issue_id=%s, user=%s, helpful=%s",
            feedback_id, issue_id, x_user_id, body.helpful
        )

        return IssueFeedbackResponse(status="created", feedback_id=feedback_id)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to record feedback for issue %s: %s", issue_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to record feedback."
        )


# ---------------------------------------------------------------------------
# Credits Endpoints — pay-per-scan ledger
#
# purchase_checkout / purchase_confirm are the seam a real payment
# provider (PayMongo, Stripe) would occupy: a webhook would call exactly
# what confirm_checkout calls today (services.credits.CreditsService.
# confirm_checkout -> the purchase_credits RPC) instead of being invoked
# directly by the client. Nothing else in the credits flow changes.
# ---------------------------------------------------------------------------

class CreditBalanceResponse(BaseModel):
    user_id: str
    balance: int
    first_scan_free: bool = False   # the user's first scan is free


class CheckoutRequest(BaseModel):
    credit_amount: int = Field(..., gt=0, le=100, description="Number of credits to purchase")
    # Frontend may pass a discounted per-credit rate (e.g. bulk packages). Default 25.0 PHP.
    unit_price: float = Field(default=25.0, gt=0, le=500.0)


class CheckoutResponse(BaseModel):
    pymt_txn_id: str
    checkout_reference: str
    amount: float
    credit_amount: int


class ConfirmCheckoutRequest(BaseModel):
    pymt_txn_id: str


class ConfirmCheckoutResponse(BaseModel):
    status: str
    balance: int


class CreditLedgerEntry(BaseModel):
    ledger_id: str
    kind: str
    delta: int
    balance_after: int
    analysis_run_id: Optional[str] = None
    note: Optional[str] = None
    created_at: str


class CreditHistoryResponse(BaseModel):
    user_id: str
    entries: List[CreditLedgerEntry]


@app.get(
    "/api/credits/balance",
    response_model=CreditBalanceResponse,
    summary="Get Credit Balance",
    tags=["Credits"]
)
async def get_credit_balance(
    x_user_id: str = Header(..., alias="X-User-Id", description="Supabase auth user UUID"),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> CreditBalanceResponse:
    _assert_owner(authenticated_user_id, x_user_id)
    balance = await credits_service.get_balance(x_user_id)
    return CreditBalanceResponse(user_id=x_user_id, balance=balance, first_scan_free=await credits_service.has_free_scan(x_user_id))


@app.post(
    "/api/credits/checkout",
    response_model=CheckoutResponse,
    summary="Start a (simulated) credit purchase",
    description=(
        "Creates a pending payment transaction and returns a mock checkout "
        "reference. No real payment provider is wired up — this is a "
        "capstone-safe simulated ledger, not a live charge. Call "
        "POST /api/credits/confirm with the returned pymt_txn_id to complete it."
    ),
    tags=["Credits"]
)
async def create_credit_checkout(
    body: CheckoutRequest,
    x_user_id: str = Header(..., alias="X-User-Id", description="Supabase auth user UUID"),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> CheckoutResponse:
    _assert_owner(authenticated_user_id, x_user_id)
    try:
        result = await credits_service.create_checkout(
            x_user_id, body.credit_amount, unit_price=body.unit_price
        )
        return CheckoutResponse(**result)
    except Exception as exc:
        logger.error("Failed to create checkout for user %s: %s", x_user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to start checkout."
        )


@app.post(
    "/api/credits/confirm",
    response_model=ConfirmCheckoutResponse,
    summary="Confirm a (simulated) credit purchase",
    description=(
        "Marks the transaction paid and credits the wallet. Idempotent — "
        "confirming an already-paid transaction just returns the current "
        "balance rather than double-crediting it."
    ),
    tags=["Credits"]
)
async def confirm_credit_checkout(
    body: ConfirmCheckoutRequest,
    x_user_id: str = Header(..., alias="X-User-Id", description="Supabase auth user UUID"),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> ConfirmCheckoutResponse:
    _assert_owner(authenticated_user_id, x_user_id)
    try:
        balance = await credits_service.confirm_checkout(x_user_id, body.pymt_txn_id)
        return ConfirmCheckoutResponse(status="paid", balance=balance)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to confirm checkout %s for user %s: %s", body.pymt_txn_id, x_user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to confirm checkout."
        )


class EmailCheckRequest(BaseModel):
    email: str = Field(..., max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailCheckResponse(BaseModel):
    registered: bool


_email_checks: Dict[str, List[float]] = {}
EMAIL_CHECKS_PER_MINUTE = 5


def _email_registered_sync(email: str) -> bool:
    client = DatabasePersistenceService.get_client()
    try:
        return bool(client.rpc("email_registered", {"p_email": email}).execute().data)
    except Exception as exc:
        # The SQL function (migration 013) is not installed: look through the user list instead.
        logger.warning("email_registered RPC unavailable, scanning users instead: %s", exc)
    for page in range(1, 26):
        users = client.auth.admin.list_users(page=page, per_page=200)
        if not users:
            return False
        if any((getattr(user, "email", "") or "").lower() == email for user in users):
            return True
        if len(users) < 200:
            return False
    return False


@app.post(
    "/api/auth/email-registered",
    response_model=EmailCheckResponse,
    summary="Does this email belong to an account?",
    description="Used by Forgot password to say 'Email not found' instead of silently sending nothing. Rate limited per caller.",
    tags=["Auth"],
)
async def email_registered(body: EmailCheckRequest, request: Request) -> EmailCheckResponse:
    caller = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [moment for moment in _email_checks.get(caller, []) if now - moment < 60]
    if len(recent) >= EMAIL_CHECKS_PER_MINUTE:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts. Please wait a minute and try again.")
    _email_checks[caller] = recent + [now]
    try:
        registered = await asyncio.to_thread(_email_registered_sync, body.email.strip().lower())
    except Exception as exc:
        logger.error("Email check failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="We could not check that email right now. Please try again.")
    return EmailCheckResponse(registered=registered)


OUTLINE_CACHE_SECONDS = 300
OUTLINE_FETCH_SECONDS = 25
_outline_cache: Dict[str, Any] = {}


class OutlineSection(BaseModel):
    name: str
    words: int
    role: Optional[str] = None


class OutlineResponse(BaseModel):
    title: str
    words: int
    pages: int
    structure: str
    sections: List[OutlineSection]
    excerpt: List[str]
    looks_like_paper: bool = True
    paper_problem: Optional[str] = None


@app.post(
    "/api/manuscripts/outline",
    response_model=OutlineResponse,
    summary="Show the sections the scan will compare, before running it",
    description=(
        "Reads an uploaded Word file or a public Google Doc and returns its title, length and the sections the parser "
        "finds, without spending a credit or starting a scan."
    ),
    tags=["Manuscripts"],
)
async def manuscript_outline(
    file: Optional[UploadFile] = File(None, description="Word document"),
    doc_url: Optional[str] = Form(None),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> OutlineResponse:
    if file is not None:
        data = await _read_upload(file, "This file is larger than 25 MB.")
        raw_text = await DocumentIngestionService.extract_plaintext_from_upload(data, file.filename or "uploaded.docx")
    elif doc_url and doc_url.startswith("http"):
        # The same link is often asked about twice (when it is pasted and again on the confirm step): remember it briefly.
        _outline_cache_prune(time.time())
        cached = _outline_cache.get(doc_url)
        if cached and time.time() - cached[0] < OUTLINE_CACHE_SECONDS:
            return cached[1]
        try:
            raw_text = await asyncio.wait_for(google_doc_text(doc_url), timeout=OUTLINE_FETCH_SECONDS)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                                detail="Google Docs is taking too long to answer. Try again, or run the scan without a preview.")
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Send a Word file or a Google Docs link.")
    from services.parser import ManuscriptParserService
    parsed = await asyncio.to_thread(ManuscriptParserService.parse_manuscript_sections, raw_text, None)
    roles = parsed.get("section_roles", {})
    sections = parsed["parsed_sections"]
    order = parsed.get("section_order", list(sections))
    paragraphs = [line.strip() for line in raw_text.splitlines() if line.strip()]
    title = paragraphs[0] if paragraphs and len(paragraphs[0]) <= 160 else ""
    words = len(raw_text.split())
    response = OutlineResponse(
        title=title,
        words=words,
        pages=max(1, math.ceil(words / 275)),
        structure=parsed.get("structure", "headings"),
        sections=[OutlineSection(name=name, words=len(sections[name].split()), role=roles.get(name)) for name in order if name in sections],
        excerpt=[line[:420] for line in paragraphs[1:7]],
    )
    paper = check_research_paper(raw_text, parsed)
    response.looks_like_paper = paper["ok"]
    response.paper_problem = None if paper["ok"] else NOT_A_PAPER_MESSAGE
    if file is None and doc_url:
        now = time.time()
        _outline_cache_prune(now)
        _outline_cache.pop(doc_url, None)
        _outline_cache[doc_url] = (now, response)
        _outline_cache_prune(now)
    return response


GDOC_DOCX_CACHE_SECONDS = 300
GDOC_MAX_DOWNLOAD = 120 * 1024 * 1024
_gdoc_docx_cache: Dict[str, Any] = {}
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# A 1x1 transparent PNG that stands in for every picture.
_PLACEHOLDER_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class _BufferReader:
    """Seekable view of an existing buffer, without duplicating it."""
    def __init__(self, data):
        self.data = memoryview(data)
        self.position = 0

    def seek(self, offset, whence=0):
        if whence == 1:
            offset += self.position
        elif whence == 2:
            offset += len(self.data)
        if offset < 0:
            raise ValueError("negative seek")
        self.position = offset
        return offset

    def tell(self):
        return self.position

    def read(self, size=-1):
        end = len(self.data) if size < 0 else min(len(self.data), self.position + size)
        result = self.data[self.position:end].tobytes()
        self.position = end
        return result

    def seekable(self):
        return True


def slim_docx_bytes(data: bytes) -> bytes:
    """Swaps every picture in a Word file for a tiny placeholder. Only the writing is compared, so the pictures only make
    the download, the preview and the scan slow. Text, tables and layout are untouched; anything unexpected returns the
    original bytes."""
    import io
    import zipfile
    if len(data) < 1_000_000:
        return data
    try:
        with zipfile.ZipFile(_BufferReader(data)) as source:
            names = source.namelist()
            if not any(name.lower().startswith("word/media/") and not name.endswith("/") for name in names):
                return data
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as target:
                for item in source.infolist():
                    is_picture = item.filename.lower().startswith("word/media/") and not item.filename.endswith("/")
                    if is_picture:
                        target.writestr(item.filename, _PLACEHOLDER_PNG)
                    else:
                        import shutil
                        with source.open(item) as reader, target.open(item.filename, "w") as writer:
                            shutil.copyfileobj(reader, writer, length=READ_CHUNK_BYTES)
        slim = out.getvalue()
        return slim if len(slim) < len(data) else data
    except Exception as exc:  # a file that cannot be read this way is still sent as it is
        logger.warning("Could not shrink the Google Doc download: %s", exc)
        return data


def _slim_download(source, size):
    import shutil
    import tempfile
    import zipfile
    if size < 1_000_000:
        return None
    try:
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            if not any(item.filename.lower().startswith("word/media/") and not item.filename.endswith("/") for item in archive.infolist()):
                return None
            with tempfile.TemporaryFile() as output:
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as target:
                    for item in archive.infolist():
                        if item.filename.lower().startswith("word/media/") and not item.filename.endswith("/"):
                            target.writestr(item.filename, _PLACEHOLDER_PNG)
                        else:
                            with archive.open(item) as reader, target.open(item.filename, "w") as writer:
                                shutil.copyfileobj(reader, writer, length=READ_CHUNK_BYTES)
                length = output.tell()
                if length >= size or length > MAX_UPLOAD_BYTES:
                    return None
                output.seek(0)
                return output.read()
    except Exception as exc:
        logger.warning("Could not shrink the Google Doc download: %s", exc)
        return None


async def download_google_docx(doc_id: str):
    """Download a public Google Doc with bounded temporary storage."""
    import httpx
    import tempfile
    from urllib.parse import unquote
    cached = _gdoc_docx_cache.get(doc_id)
    if cached and time.time() - cached[0] < GDOC_DOCX_CACHE_SECONDS:
        return cached[1], cached[2]
    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=docx"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    with tempfile.TemporaryFile() as raw:
        size = 0
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(90.0, connect=15.0), headers=headers) as client:
                async with client.stream("GET", export_url) as response:
                    code, content_type = response.status_code, response.headers.get("content-type", "")
                    disposition = response.headers.get("content-disposition", "")
                    if code == 200 and "html" not in content_type:
                        async for chunk in response.aiter_bytes(READ_CHUNK_BYTES):
                            size += len(chunk)
                            if size > GDOC_MAX_DOWNLOAD:
                                raise HTTPException(status_code=413, detail="This Google Doc is too large to open (over 120 MB).")
                            raw.write(chunk)
        except httpx.HTTPError as exc:
            logger.error("Google Doc download failed for %s: %s", doc_id, exc)
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Google Docs is taking too long to answer. Try again in a moment.")
        if code == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Google Doc not found. Please verify the document URL exists.")
        raw.seek(0)
        if code != 200 or "html" in content_type or raw.read(2) != b"PK":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Unable to open the document. Make sure sharing is set to 'Anyone with the link can view'.")
        data = await asyncio.to_thread(_slim_download, raw, size)
        if data is None:
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="This Google Doc is larger than 25 MB even without its pictures.")
            raw.seek(0)
            data = raw.read()
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"([^\"]+)\"", disposition)
    filename = unquote(match.group(1) or match.group(2)) if match else "Google Doc.docx"
    if not filename.lower().endswith(".docx"):
        filename += ".docx"
    _gdoc_docx_cache[doc_id] = (time.time(), data, filename)
    for key in [key for key, entry in _gdoc_docx_cache.items() if time.time() - entry[0] > GDOC_DOCX_CACHE_SECONDS][:20]:
        _gdoc_docx_cache.pop(key, None)
    while len(_gdoc_docx_cache) > 8:
        _gdoc_docx_cache.pop(next(iter(_gdoc_docx_cache)), None)
    return data, filename


async def google_doc_text(doc_url: str) -> str:
    """The text of a Google Doc, read the way an uploaded Word file is read (the same export, the same parsing), so a link
    and an upload of the same paper give the same sections and the same score. Only if that export cannot be had does it
    fall back to Google's plain-text export."""
    try:
        if os.getenv("GDOC_AS_DOCX", "on").strip().lower() in ("off", "false", "0", "no"):
            raise RuntimeError("switched off")
        data, filename = await download_google_docx(DocumentIngestionService.extract_google_doc_id(doc_url))
        text = await DocumentIngestionService.extract_plaintext_from_upload(data, filename)
        if text and text.strip():
            return text
    except Exception as error:
        logger.warning("Google Doc %s could not be read as a Word file (%s); using its text export", doc_url[:60], error)
    return await DocumentIngestionService.fetch_plaintext_from_gdoc(doc_url)


@app.post(
    "/api/manuscripts/google-docx",
    summary="Download a public Google Doc as a Word file",
    description=(
        "Fetches the Google Doc behind a link as .docx so it can be previewed, scanned and highlighted exactly like a "
        "Word file the student uploaded."
    ),
    tags=["Manuscripts"],
)
async def google_doc_as_docx(
    doc_url: str = Form(...),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
):
    from fastapi.responses import Response
    from urllib.parse import quote
    data, filename = await download_google_docx(DocumentIngestionService.extract_google_doc_id(doc_url))
    return Response(content=memoryview(data) if isinstance(data, bytearray) else data, media_type=DOCX_MEDIA_TYPE,
                    headers={"X-Doc-Filename": quote(filename), "Access-Control-Expose-Headers": "X-Doc-Filename"})


class PurchaseProof(BaseModel):
    reference_number: str = Field(..., min_length=4, max_length=40, pattern=r"^[A-Za-z0-9 \-]+$")
    payer_name: Optional[str] = Field(default=None, max_length=120)
    file_name: Optional[str] = Field(default=None, max_length=200)
    content_type: str
    proof_data: str = Field(..., description="The receipt picture or PDF, base64 encoded")


class PurchaseRequest(BaseModel):
    credit_amount: int = Field(..., gt=0, le=100)
    method: str = Field(default="card", pattern="^(card|gcash|bank_transfer)$")
    proof: Optional[PurchaseProof] = None


class PurchaseResponse(BaseModel):
    pymt_txn_id: str
    checkout_reference: str
    amount: float
    credit_amount: int
    balance: int
    method: str


@app.post(
    "/api/credits/purchase",
    response_model=PurchaseResponse,
    summary="Buy credits",
    description=(
        "Records the purchase and adds the credits to the caller's wallet. The price is set by the server. "
        "No money is taken yet; a payment provider's confirmation would slot in here."
    ),
    tags=["Credits"],
)
async def purchase_credits(
    body: PurchaseRequest,
    x_user_id: str = Header(..., alias="X-User-Id", description="Supabase auth user UUID"),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> PurchaseResponse:
    _assert_owner(authenticated_user_id, x_user_id)
    proof = None
    if body.proof:
        if body.proof.content_type not in PROOF_TYPES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Upload a PNG, JPEG, WebP or PDF file.")
        if len(body.proof.proof_data) * 3 // 4 > PROOF_MAX_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="The proof of payment is larger than 2 MB.")
        proof = {"reference_number": body.proof.reference_number.strip(), "payer_name": body.proof.payer_name,
                 "file_name": body.proof.file_name, "content_type": body.proof.content_type,
                 "proof_data": f"data:{body.proof.content_type};base64,{body.proof.proof_data}"}
    try:
        return PurchaseResponse(**await credits_service.purchase(x_user_id, body.credit_amount, body.method, proof))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except Exception as exc:
        logger.error("Credit purchase failed for user %s: %s", x_user_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="The purchase could not be completed. No credits were added. Please try again.")


PROOF_MAX_BYTES = 2 * 1024 * 1024
PROOF_TYPES = ("image/png", "image/jpeg", "image/webp", "application/pdf")


class PaymentProofRequest(BaseModel):
    pymt_txn_id: str
    method: str = Field(..., pattern="^(gcash|bank_transfer)$")
    reference_number: str = Field(..., min_length=4, max_length=40, pattern=r"^[A-Za-z0-9 \-]+$")
    payer_name: Optional[str] = Field(default=None, max_length=120)
    file_name: Optional[str] = Field(default=None, max_length=200)
    content_type: str
    proof_data: str = Field(..., description="The receipt picture or PDF, base64 encoded")


class PaymentProofResponse(BaseModel):
    status: str
    proof_id: str


@app.post(
    "/api/credits/proof",
    response_model=PaymentProofResponse,
    summary="Upload proof of payment for a pending credit purchase",
    description=(
        "Keeps the receipt picture and reference number for a pending transaction. In this simulated sandbox the "
        "purchase is then confirmed with POST /api/credits/confirm."
    ),
    tags=["Credits"]
)
async def upload_payment_proof(
    body: PaymentProofRequest,
    x_user_id: str = Header(..., alias="X-User-Id", description="Supabase auth user UUID"),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> PaymentProofResponse:
    _assert_owner(authenticated_user_id, x_user_id)
    if body.content_type not in PROOF_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Upload a PNG, JPEG, WebP or PDF file.")
    if len(body.proof_data) * 3 // 4 > PROOF_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="The proof of payment is larger than 2 MB.")
    try:
        proof_id = await credits_service.save_payment_proof(
            x_user_id, body.pymt_txn_id, body.method, body.reference_number.strip(), body.payer_name,
            body.file_name, body.content_type, f"data:{body.content_type};base64,{body.proof_data}")
        return PaymentProofResponse(status="received", proof_id=proof_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to save payment proof for %s, user %s: %s", body.pymt_txn_id, x_user_id, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to save the proof of payment.")


@app.get(
    "/api/credits/history",
    response_model=CreditHistoryResponse,
    summary="Get Credit Ledger History",
    tags=["Credits"]
)
async def get_credit_history(
    x_user_id: str = Header(..., alias="X-User-Id", description="Supabase auth user UUID"),
    limit: int = Query(50, ge=1, le=200),
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> CreditHistoryResponse:
    _assert_owner(authenticated_user_id, x_user_id)
    entries = await credits_service.get_ledger_history(x_user_id, limit=limit)
    return CreditHistoryResponse(
        user_id=x_user_id,
        entries=[
            CreditLedgerEntry(
                ledger_id=e["ledger_id"],
                kind=e["kind"],
                delta=e["delta"],
                balance_after=e["balance_after"],
                analysis_run_id=e.get("analysis_run_id"),
                note=e.get("note"),
                created_at=e["created_at"],
            )
            for e in entries
        ],
    )
