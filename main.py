import os
import json
import time
import uuid
import logging
import re
from html import unescape
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Body, status, Header, File, UploadFile, Form
from fastapi.responses import JSONResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_serializer
from dotenv import load_dotenv

# Load .env FIRST — must happen before service imports
# so GEMINI_API_KEY, SUPABASE_URL, etc. are available
# when module-level service instances are created.
# override=True ensures .env values WIN over Windows system env vars.
load_dotenv(override=True)

from services.ingestion import DocumentIngestionService, looks_like_html_document
from services.ml_client import ml_client
from services.citation import CitationAuditService
from services.db_service import DatabasePersistenceService
from services.notification import NotificationDeliveryService
from services.credits import credits_service, InsufficientCreditsError
from services.scoring import compute_citation_integrity
from services.v7_scan import (merge_repeated_sections, assemble_flow, explain_flow,
                              four_part_score, band)
from services.reference_parser import extract_intext_citations
from services.v7_explain import plain_text
from services.scan_access import locked_scan_view

logger = logging.getLogger("resync.main")


def _references_text(parsed_sections: Dict[str, str], section_roles: Dict[str, str]) -> str:
    return max((text for heading, text in parsed_sections.items()
                if section_roles.get(heading) == "references"
                and re.sub(r" \(\d+\)$", "", heading.strip().lower()) in ("references", "bibliography")),
               key=len, default="")

gemini_key = os.getenv("GEMINI_API_KEY")
supabase_url = os.getenv("SUPABASE_URL")

app = FastAPI(
    title="Resync AI API",
    description="Backend API for Research Manuscript Correlation & Inconsistency Detection",
    version="1.0.0"
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
)

@app.get("/")
def read_root():
    return {"status": "Resync FastAPI Server is Live!"}

# ponytail: single-slot global lock — one scan at a time on 0.1 CPU / 512MB.
# Raise to 2+ only on a Standard (2GB) instance. Held across the whole
# pipeline (not just the CPU-bound steps) so concurrent scans do not
# multiply the Gemini pair and summary request rate.
SCAN_SLOT = asyncio.Semaphore(1)


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
    except Exception:
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
# Scan rate limiting — protects the Gemini free-tier RPM budget. The credit
# ledger already bounds *cost*, but nothing previously bounded *burst rate*
# (a user with credits, or with CREDITS_ENABLED=false, could loop
# /api/scans/start and blow through Gemini's 15 RPM). Checked once in
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
    sections: List[V7Section] = Field(..., min_length=2, max_length=30)


@app.post("/analyze_v7")
async def analyze_v7(req: V7AnalyzeRequest, user_id: str = Depends(get_authenticated_user_id)):
    """v7 correlation of consecutive manuscript sections, returned as a nested manuscript_flow."""
    await _check_scan_rate_limit(user_id)
    sections = [s.model_dump() for s in req.sections]
    async with SCAN_SLOT:
        try:
            result = await asyncio.to_thread(ml_client.analyze_v7, sections)
        except Exception:
            logger.exception("v7 analysis failed")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="v7 model is unavailable.")

        manuscript_flow = assemble_flow(sections, result["pairs"])
        findings, recommendations, synthesis = await explain_flow(sections, manuscript_flow)
    score, dimensions, dimension_weights = four_part_score(manuscript_flow)
    return {"result_version": "v7", "manuscript_score": score,
            "dimensions": dimensions, "dimension_weights": dimension_weights,
            "manuscript_flow": manuscript_flow, "inconsistencies": findings,
            "recommendations": recommendations, "ai_synthesis": synthesis}


@app.get("/api/test-ingestion")
async def test_ingestion(doc_url: str = Query(..., description="Public Google Docs Link")):
    """Endpoint to test Google Docs text extraction."""
    _require_test_endpoints_enabled()
    extracted_text = await DocumentIngestionService.fetch_plaintext_from_gdoc(doc_url)
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
    file_bytes = await file.read()
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
    raw_text = await DocumentIngestionService.fetch_plaintext_from_gdoc(doc_url)
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
    original_sentence: Optional[str]
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
    citation_detail: Dict[str, Any]


class AITextIndicatorReport(BaseModel):
    """Advisory-only stylometric indicator. Never factored into the score."""
    overall_score: Optional[float] = None
    section_scores: Dict[str, Optional[float]] = {}
    flagged_sections: List[str] = []
    disclaimer: str = ""


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
    manuscript_sections: Optional[List[Dict[str, Optional[str]]]] = None
    access: Optional[Dict[str, Any]] = None
    manuscript_flow: List[Dict[str, Any]]
    inconsistencies_found: int
    inconsistencies: List[InconsistencyReport]
    recommendations: List[RecommendationReport]
    ai_synthesis: Dict[str, Any]
    citations_audited: int
    citations: List[CitationReport]
    score_breakdown: Optional[ScoreBreakdown] = None
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
        await asyncio.to_thread(
            lambda: db_client.table("analysis_run").insert({
                "analysis_run_id": analysis_run_id,
                "user_id": req.user_id,
                "manuscript_id": manuscript_id,
                "analysis_run_status": "processing",
                "analysis_run_template_toc": toc_str,
                "analysis_run_coherence_score": 0,
                # Plain-text mirror of the job state. The polling endpoint reads
                # this rather than the enum column, because the enum has no
                # 'failed' member (see migration 003).
                "status": "processing",
                "doc_url": source_identifier,
            }).execute()
        )
        logger.info(f"Provisioned analysis_run row: {analysis_run_id}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to provision analysis_run in DB: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database setup failed before scan could start."
        )

    return analysis_run_id, manuscript_id, await credits_service.get_balance(req.user_id)


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
    if file_bytes:
        fname = filename or req.filename or req.doc_url or "document.docx"
        raw_text: str = await DocumentIngestionService.extract_plaintext_from_upload(file_bytes, fname)
    elif req.file_content_base64:
        import base64
        try:
            decoded_bytes = base64.b64decode(req.file_content_base64)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid base64 encoding in file_content_base64."
            )
        fname = req.filename or req.doc_url or "document.docx"
        raw_text = await DocumentIngestionService.extract_plaintext_from_upload(decoded_bytes, fname)
    elif req.doc_url and ("docs.google.com" in req.doc_url or req.doc_url.startswith("http")):
        raw_text = await DocumentIngestionService.fetch_plaintext_from_gdoc(req.doc_url)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid manuscript provided. Please provide a public Google Docs link or upload a .docx/.pdf file."
        )

    derived_title = (plain_text(next((line for line in raw_text.splitlines() if line.strip()), ""))[:120]
                     if req.doc_url and not req.manuscript_title else None)

    # ------------------------------------------------------------------
    # Step 2 – Parser
    # ------------------------------------------------------------------
    # Offloaded to a worker thread: these are CPU-bound and would otherwise
    # block the event loop, stalling concurrent status-poll requests.
    from services.parser import ManuscriptParserService
    parse_result: Dict[str, Any] = await asyncio.to_thread(
        ManuscriptParserService.parse_manuscript_sections, raw_text, req.template_toc
    )
    parsed_sections: Dict[str, str] = parse_result["parsed_sections"]
    section_order: List[str] = parse_result.get("section_order", list(parsed_sections))
    section_roles: Dict[str, str] = parse_result.get("section_roles", {})

    # Score physical neighbors after merging repeated literal headings.
    sections = merge_repeated_sections(parsed_sections, section_order)
    ml_result = await asyncio.to_thread(ml_client.analyze_v7, sections)
    manuscript_flow = assemble_flow(sections, ml_result["pairs"])
    inconsistencies_data, recommendations, ai_synthesis = await explain_flow(sections, manuscript_flow)
    overall_score, dimensions, dimension_weights = four_part_score(manuscript_flow)
    ai_text_result = await asyncio.to_thread(
        ml_client.analyze_ai_text, {section["name"]: section["text"] for section in sections}
    )

    # ------------------------------------------------------------------
    # Step 5 – Citation Audit
    # ------------------------------------------------------------------
    # No 20,000-char slice here anymore — that cap silently truncated a
    # real 150-reference APA list mid-entry. CitationAuditService applies
    # its own much larger DoS-guard cap (150,000 chars) internally.
    references_text = _references_text(parsed_sections, section_roles)
    citation_audit_result: Dict[str, Any] = await CitationAuditService.audit_citations(
        references_text, body_text=raw_text
    )
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
        "citation_detail": {
            "well_formed_ratio": citation_integrity_result.well_formed_ratio,
            "link_resolution_rate": citation_integrity_result.link_resolution_rate,
            "cross_match_score": citation_integrity_result.cross_match_score,
            "total_entries": citation_integrity_result.total_entries,
        },
    }

    # ------------------------------------------------------------------
    # Step 6 – Database Persistence
    # ------------------------------------------------------------------
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
    db_save_status: str = db_result.get("status", "unknown")

    # ------------------------------------------------------------------
    # Step 7 – Notification (non-blocking background task)
    # ------------------------------------------------------------------
    # Derive a human-readable manuscript title for the push message.
    # We use the source identifier as a fallback since the title may not be
    # in the request model; the notification service only needs a string.
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

    notification_dispatched = await NotificationDeliveryService.send_scan_completion_alert(
        user_id=req.user_id,
        manuscript_title=manuscript_title,
        coherence_score=int(overall_score),
        analysis_run_id=analysis_run_id
    )

    # ------------------------------------------------------------------
    # Step 8 – Return consolidated ScanResponse
    # ------------------------------------------------------------------
    return ScanResponse(
        status="completed",
        analysis_run_id=analysis_run_id,
        user_id=req.user_id,
        manuscript_id=manuscript_id,
        doc_url=source_identifier,
        manuscript_title=req.manuscript_title or derived_title,
        result_version="v7",
        overall_coherence_score=overall_score,
        sections_analyzed=[section["name"] for section in sections],
        manuscript_sections=sections,
        access={"unlocked": False, "unlock_cost": 1, "locked_counts": {
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
        ai_text_indicator=AITextIndicatorReport(**ai_text_result),
        credits_remaining=credits_remaining,
        db_save_status=db_save_status,
        notification_dispatched=notification_dispatched,
    )


# ---------------------------------------------------------------------------
# Scan Endpoints
# ---------------------------------------------------------------------------

class ScanJobResponse(BaseModel):
    """202 response acknowledging an accepted async scan job."""
    status: str
    analysis_run_id: str


class ScanStatusResponse(BaseModel):
    """Polling response: job state, plus the full report once completed."""
    status: str = Field(..., description="processing | completed | failed")
    analysis_run_id: str
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


async def _save_completed_result(result: ScanResponse) -> None:
    db_client = DatabasePersistenceService.get_client()
    await asyncio.to_thread(
        lambda: db_client.table("analysis_run").update({
            "status": "completed", "result_json": result.model_dump(mode="json"),
        }).eq("analysis_run_id", result.analysis_run_id).execute()
    )


async def _run_scan_job(
    req: ScanRequest,
    analysis_run_id: str,
    manuscript_id: str,
    credits_remaining: int,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
) -> None:
    """
    Background wrapper around the scan pipeline for the async job endpoint.

    Persists the finished report to `analysis_run.result_json` so the polling
    endpoint can return it verbatim — identical to what the synchronous
    endpoint would have returned, with no field lost in reconstruction.

    Any failure is recorded on the row. Without the status
    update, an exception would be swallowed by the background-task runner
    and leave the job pinned at 'processing' forever, with the client
    polling indefinitely.

    Waits its turn on SCAN_SLOT if another scan is already running — the
    client already polls GET /api/scans/{id}, so a longer wait here just
    surfaces as 'processing' for a bit longer, which is the correct UX for
    a background job (unlike the sync endpoint, this has no proxy timeout
    to race against).
    """
    async with SCAN_SLOT:
        try:
            result = await _execute_scan_pipeline(
                req,
                analysis_run_id,
                manuscript_id,
                credits_remaining,
                file_bytes=file_bytes,
                filename=filename,
            )
            await _save_completed_result(result)
            logger.info("Async scan job completed: %s", analysis_run_id)
        except Exception as exc:
            logger.exception("Async scan job failed: %s", analysis_run_id)
            try:
                db_client = DatabasePersistenceService.get_client()
                await asyncio.to_thread(
                    lambda: db_client.table("analysis_run").update({
                        "status": "failed",
                        "error_message": str(exc)[:1000],
                    }).eq("analysis_run_id", analysis_run_id).execute()
                )
            except Exception:
                logger.exception("Could not mark scan job %s as failed", analysis_run_id)


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
    analysis_run_id, manuscript_id, credits_remaining = await _provision_analysis_run(req)
    try:
        await asyncio.wait_for(SCAN_SLOT.acquire(), timeout=5.0)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Another scan is currently in progress. Please retry shortly.",
            headers={"Retry-After": "60"},
        )
    try:
        result = await _execute_scan_pipeline(req, analysis_run_id, manuscript_id, credits_remaining)
        await _save_completed_result(result)
        return ScanResponse.model_validate(locked_scan_view(result.model_dump(mode="json")))
    except Exception:
        raise
    finally:
        SCAN_SLOT.release()


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
    background_tasks: BackgroundTasks,
    authenticated_user_id: str = Depends(get_authenticated_user_id),
) -> ScanJobResponse:
    """
    Accepts a scan job and returns straight away.

    FK validation happens inline so invalid user_id/manuscript_id fails
    fast with 404 instead of failing in the background.
    """
    _assert_owner(authenticated_user_id, req.user_id)
    analysis_run_id, manuscript_id, credits_remaining = await _provision_analysis_run(req)
    background_tasks.add_task(_run_scan_job, req, analysis_run_id, manuscript_id, credits_remaining)
    logger.info("Accepted async scan job: %s", analysis_run_id)
    return ScanJobResponse(status="processing", analysis_run_id=analysis_run_id)


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
    background_tasks: BackgroundTasks,
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
    file_bytes = await file.read()
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

    analysis_run_id, resolved_manuscript_id, credits_remaining = await _provision_analysis_run(
        req, default_source_name=file.filename
    )
    background_tasks.add_task(
        _run_scan_job,
        req,
        analysis_run_id,
        resolved_manuscript_id,
        credits_remaining,
        file_bytes=file_bytes,
        filename=file.filename,
    )
    logger.info("Accepted async file scan job: %s (file: %s)", analysis_run_id, file.filename)
    return ScanJobResponse(status="processing", analysis_run_id=analysis_run_id)


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
    file_bytes = await file.read()
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

    analysis_run_id, resolved_manuscript_id, credits_remaining = await _provision_analysis_run(
        req, default_source_name=file.filename
    )
    try:
        await asyncio.wait_for(SCAN_SLOT.acquire(), timeout=5.0)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Another scan is currently in progress. Please retry shortly.",
            headers={"Retry-After": "60"},
        )
    try:
        result = await _execute_scan_pipeline(
            req,
            analysis_run_id,
            resolved_manuscript_id,
            credits_remaining,
            file_bytes=file_bytes,
            filename=file.filename,
        )
        await _save_completed_result(result)
        return ScanResponse.model_validate(locked_scan_view(result.model_dump(mode="json")))
    except Exception:
        raise
    finally:
        SCAN_SLOT.release()


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
                .table("analysis_run")
                .select("analysis_run_id, user_id, status, error_message, result_json")
                .eq("analysis_run_id", analysis_run_id)
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
    job_status = row.get("status") or "processing"

    if job_status == "failed":
        return ScanStatusResponse(
            status="failed",
            analysis_run_id=analysis_run_id,
            error=row.get("error_message") or "Scan failed for an unknown reason."
        )

    # `save_scan_transaction` flips status to 'completed' before the job
    # wrapper writes result_json, so treat 'completed without a report' as
    # still in progress. This closes that race instead of briefly handing
    # the client a completed status with an empty result.
    result_json = row.get("result_json")
    if job_status == "completed" and result_json:
        return ScanStatusResponse(
            status="completed",
            analysis_run_id=analysis_run_id,
            result=locked_scan_view(result_json)
        )

    return ScanStatusResponse(status="processing", analysis_run_id=analysis_run_id)


@app.post("/api/scans/{scan_id}/unlock", tags=["Scans"])
async def unlock_scan(scan_id: str, authenticated_user_id: str = Depends(get_authenticated_user_id)):
    try:
        return await credits_service.unlock_report(authenticated_user_id, scan_id)
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

# In-process cache for manuscript previews: { scan_id: { "text": "...", "expires_at": float } }
# TTL is 10 minutes. Max keys = 100 to prevent OOM.
_manuscript_cache = {}

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
        
    result_json = row.get("result_json") or {}
    if result_json.get("access") and (result_json["access"].get("unlocked") is False or result_json.get("manuscript_sections")):
        sections = locked_scan_view(result_json).get("manuscript_sections") or []
        return {"available": bool(sections), "text": "\n\n".join(section["text"] or "" for section in sections if section["text"] is not None)}
    doc_url = result_json.get("doc_url")
    
    if not doc_url or "docs.google.com" not in doc_url:
        raise HTTPException(
            status_code=400,
            detail="Document URL is not a supported Google Docs link."
        )
        
    now = time.time()
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
            resp = await client.get(export_url)

            if resp.status_code in (401, 403):
                return {"available": False, "reason": "private"}
            if resp.status_code == 404:
                return {"available": False, "reason": "unreachable"}
            resp.raise_for_status()

            raw_text = resp.text

            # Google returns an HTML login-redirect page instead of 403
            # when the doc is private. Catch it here.
            if looks_like_html_document(raw_text):
                return {"available": False, "reason": "private"}

    except Exception as exc:
        logger.warning("Failed to fetch manuscript %s from Google Docs: %s", doc_id, exc)
        return {"available": False, "reason": "google_fetch_failed"}
        
    processed_text = DocumentIngestionService.strip_document_noise(raw_text.lstrip("\ufeff"))
    
    if len(_manuscript_cache) >= 100:
        oldest_key = min(_manuscript_cache.keys(), key=lambda k: _manuscript_cache[k]["expires_at"])
        del _manuscript_cache[oldest_key]
        
    _manuscript_cache[scan_id] = {
        "text": processed_text,
        "expires_at": now + 600,
        "fetched_at": now
    }
    
    return {
        "available": True,
        "text": processed_text,
        "fetched_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    }


# ---------------------------------------------------------------------------
# Issue Feedback Endpoint
# ---------------------------------------------------------------------------

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
    return CreditBalanceResponse(user_id=x_user_id, balance=balance)


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
