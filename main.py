import os
import json
import time
import uuid
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Body, status, Header
import asyncio
from services.deterministic import DeterministicAuditService, deterministic_service
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load .env FIRST — must happen before service imports
# so GEMINI_API_KEY, SUPABASE_URL, etc. are available
# when module-level service instances are created.
# override=True ensures .env values WIN over Windows system env vars.
load_dotenv(override=True)

# Cap torch's intra-op thread pool before any model is imported below —
# Render free tier gives 0.1 CPU, but torch defaults to os.cpu_count() of
# the host machine (8-16), causing thread contention under concurrent scans.
import torch
torch.set_num_threads(1)

from services.ingestion import DocumentIngestionService
from services.parser import ManuscriptParserService
from services.embedding import embedding_service
from services.reasoning import reasoning_service
from services.citation import CitationAuditService
from services.db_service import DatabasePersistenceService
from services.notification import NotificationDeliveryService
from services.credits import credits_service, InsufficientCreditsError
from services.scoring import (
    compute_structural_completeness,
    compute_cross_chapter_coherence,
    compute_citation_integrity,
    compute_functional_metric,
    aggregate_role_texts,
    PAR_SCORE,
)
from services.ai_text import compute_manuscript_ai_text_indicator
from services.reference_parser import extract_intext_citations

logger = logging.getLogger("resync.main")

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
# pipeline (not just the CPU-bound steps) so it also serializes the Gemini
# calls in reasoning.py, keeping two concurrent scans from doubling the
# request rate against the shared 15 RPM free-tier quota.
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
    raw_text = await DocumentIngestionService.fetch_plaintext_from_gdoc(doc_url)
    parse_result = ManuscriptParserService.parse_manuscript_sections(raw_text, template_toc)
    
    return {
        "status": "success",
        "doc_url": doc_url,
        "analysis": parse_result
    }

@app.post("/api/test-embedding")
async def test_embedding(
    doc_url: str = Query(..., description="Public Google Docs Link"),
    template_toc: Optional[List[str]] = Body(
        default=[],
        description="Optional custom Department TOC headers list."
    )
):
    """
    Endpoint to test sentence-transformers embedding generation & pairwise coherence score calculation.
    """
    _require_test_endpoints_enabled()
    raw_text = await DocumentIngestionService.fetch_plaintext_from_gdoc(doc_url)
    parse_result = ManuscriptParserService.parse_manuscript_sections(raw_text, template_toc)
    parsed_sections = parse_result.get("parsed_sections", {})
    
    coherence_result = embedding_service.compute_coherence(parsed_sections)
    
    return {
        "status": "success",
        "doc_url": doc_url,
        "sections_analyzed": list(parsed_sections.keys()),
        "sections_with_content": [s for s in parsed_sections.keys() if s not in coherence_result.empty_sections],
        "empty_sections": coherence_result.empty_sections,
        "overall_coherence_score": coherence_result.overall_score,
        "section_scores": coherence_result.section_scores
    }

@app.post("/api/test-reasoning")
async def test_reasoning(
    doc_url: str = Query(..., description="Public Google Docs Link"),
    par: float = Query(PAR_SCORE, description="Calibrated coherence PAR (0-100). Pairs below this get explained; pairs at/above get verified."),
    template_toc: Optional[List[str]] = Body(
        default=[],
        description="Optional custom Department TOC headers list."
    )
):
    """
    Endpoint to test Gemini XAI reasoning output for role-pair coherence.
    """
    _require_test_endpoints_enabled()
    raw_text = await DocumentIngestionService.fetch_plaintext_from_gdoc(doc_url)
    parse_result = ManuscriptParserService.parse_manuscript_sections(raw_text, template_toc)
    parsed_sections = parse_result.get("parsed_sections", {})
    section_roles = parse_result.get("section_roles", {})

    coherence_result = embedding_service.compute_coherence(parsed_sections)
    cross_chapter_result = compute_cross_chapter_coherence(
        coherence_result.embeddings, section_roles, parsed_sections
    )
    role_texts = aggregate_role_texts(parsed_sections, section_roles)
    inconsistencies, verifications, dismissed_pairs = await reasoning_service.analyze_inconsistencies(
        cross_chapter_result, role_texts, par=par
    )

    return {
        "status": "success",
        "doc_url": doc_url,
        "par_used": par,
        "cross_chapter_coherence_score": cross_chapter_result.score,
        "pair_scores": cross_chapter_result.pair_scores,
        "inconsistencies_found": len(inconsistencies),
        "inconsistencies": inconsistencies,
        "verifications": verifications,
        "dismissed_pairs": dismissed_pairs,
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
    doc_url: str = Field(..., description="Public Google Docs share URL")
    template_toc: Optional[List[str]] = Field(
        default=None,
        description="Optional ordered list of mandatory section headers for the Department TOC template."
    )
    style_reference_url: Optional[str] = Field(
        default=None,
        description="Optional URL to a style reference document (reserved for future use)."
    )


class InconsistencyReport(BaseModel):
    """Serialisable representation of a single XAI inconsistency finding."""
    section_a: str
    section_b: str
    # Optional: a deterministic numeric-audit flag has no coherence score
    # at all -- None is distinguishable from a real score of zero, which
    # a hardcoded 0.0 default was not.
    coherence_score: Optional[float] = None
    explanation_what: str
    explanation_why: str
    suggested_fix: str
    inconsistency_id: str = ""
    evidence_a: str = ""
    evidence_b: str = ""
    evidence_verified: bool = False
    objectives_unaddressed: List[str] = []
    finding_status: str = "material_issue"


class VerificationReport(BaseModel):
    """A high-scoring (>= PAR) role pair, checked for whether its
    calibrated score reflects substantive alignment or just shared
    academic vocabulary/register."""
    role_a: str
    role_b: str
    score: float
    alignment: str
    note: str


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


class ScoreBreakdown(BaseModel):
    """The three-criteria functional metric, replacing a single flat score."""
    overall_score: float
    band: str
    structural_completeness_score: Optional[float] = None
    cross_chapter_coherence_score: Optional[float] = None
    citation_integrity_score: Optional[float] = None
    biggest_lever: Optional[Dict[str, Any]] = None
    structural_detail: Dict[str, Any] = {}
    coherence_detail: Dict[str, Any] = {}
    citation_detail: Dict[str, Any] = {}


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
    overall_coherence_score: float
    sections_analyzed: List[str]
    missing_sections: List[str]
    has_all_required_sections: bool
    section_scores: List[Dict[str, Any]]
    inconsistencies_found: int
    inconsistencies: List[InconsistencyReport]
    verifications: List[VerificationReport] = []
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

async def _resolve_manuscript_id(db_client: Any, req: ScanRequest) -> str:
    """Returns req.manuscript_id if given (validated to exist), otherwise
    creates a new manuscript row from manuscript_title + doc_url.

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
    title = (req.manuscript_title or req.doc_url or "Untitled Manuscript")[:250]
    await asyncio.to_thread(
        lambda: db_client.table("manuscript").insert({
            "manuscript_id": new_manuscript_id,
            "user_id": req.user_id,
            "manuscript_title": title,
            "manuscript_source_url": req.doc_url,
        }).execute()
    )
    logger.info("Created manuscript row %s for user %s", new_manuscript_id, req.user_id)
    return new_manuscript_id


async def _provision_analysis_run(req: ScanRequest) -> tuple[str, str, int]:
    """
    Validates the user, resolves/creates the manuscript, inserts the
    `analysis_run` row in 'processing' state, and debits one scan credit.

    Runs *before* a scan is accepted so that bad requests — and an empty
    credit balance — fail fast with a real HTTP status, rather than
    surfacing later inside a background task where the client can no
    longer see them.

    Returns (analysis_run_id, manuscript_id, credits_remaining).
    Raises HTTPException(402) via InsufficientCreditsError if the user has
    no credits left; the provisional analysis_run row is deleted first so
    a declined scan never leaves a phantom "processing" row behind.
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

        manuscript_id = await _resolve_manuscript_id(db_client, req)

        # Bug 4: analysis_run_template_toc is a text column, stringify the list
        toc_str = json.dumps(req.template_toc) if req.template_toc else None

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
                "doc_url": req.doc_url,
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

    try:
        credits_remaining = await credits_service.debit_scan_credit(req.user_id, analysis_run_id)
    except InsufficientCreditsError as exc:
        try:
            await asyncio.to_thread(
                lambda: db_client.table("analysis_run").delete().eq("analysis_run_id", analysis_run_id).execute()
            )
        except Exception:
            logger.exception("Failed to clean up declined analysis_run %s", analysis_run_id)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"message": "No scan credits remaining.", "balance": exc.balance},
        )

    return analysis_run_id, manuscript_id, credits_remaining


async def _execute_scan_pipeline(
    req: ScanRequest,
    analysis_run_id: str,
    manuscript_id: str,
    credits_remaining: Optional[int] = None,
) -> ScanResponse:
    """
    Runs the full RESYNC analysis pipeline against an already-provisioned
    analysis_run row and returns the consolidated report.

    Pipeline sequence
    -----------------
    1. Ingestion    – fetch plaintext from Google Docs
    2. Parser       – segment text into sections using the Department TOC template
    3. Embedding    – compute pairwise cosine-similarity coherence scores
    3b. Structural  – score completeness against the required/optional roles
    3c. Coherence   – role-aware weighted cross-chapter coherence matrix
    4. Reasoning    – call Gemini for XAI analysis on low-scoring section pairs
    5. Citation     – segment references, verify links, cross-match in-text citations
    5b. Citation    – score citation integrity from the audit output
    5c. Composite   – combine the three sub-scores into the functional metric
    5d. AI-text     – advisory stylometric indicator (never affects the score)
    6. Database     – persist analysis_run, inconsistencies, citations to Supabase
    7. Notification – fire OneSignal push alert
    8. Return consolidated ScanResponse JSON report
    """
    db_client = DatabasePersistenceService.get_client()

    # ------------------------------------------------------------------
    # Step 1 – Ingestion
    # ------------------------------------------------------------------
    raw_text: str = await DocumentIngestionService.fetch_plaintext_from_gdoc(req.doc_url)

    # ------------------------------------------------------------------
    # Step 2 – Parser
    # ------------------------------------------------------------------
    # Offloaded to a worker thread: these are CPU-bound and would otherwise
    # block the event loop, stalling concurrent status-poll requests.
    parse_result: Dict[str, Any] = await asyncio.to_thread(
        ManuscriptParserService.parse_manuscript_sections, raw_text, req.template_toc
    )
    parsed_sections: Dict[str, str] = parse_result["parsed_sections"]
    missing_sections: List[str] = parse_result["missing_sections"]
    has_all_required_sections: bool = parse_result["has_all_required_sections"]

    # ------------------------------------------------------------------
    # Step 3 – Embedding / Coherence
    # ------------------------------------------------------------------
    coherence_result = await asyncio.to_thread(
        embedding_service.compute_coherence, parsed_sections
    )

    section_roles: Dict[str, str] = parse_result.get("section_roles", {})

    # ------------------------------------------------------------------
    # Step 3b – Structural Completeness
    # ------------------------------------------------------------------
    structural_result = compute_structural_completeness(
        parsed_sections, section_roles,
        detection_confidence=parse_result.get("detection_confidence", 1.0),
    )

    # ------------------------------------------------------------------
    # Step 3c – Cross-Chapter Coherence (role-aware weighted pair matrix,
    # replacing the flat adjacent-pair mean as the scoring signal — the
    # legacy adjacent-pair coherence_result.section_scores below is still
    # computed and returned as-is for the existing Reports tab / mobile
    # app, but no longer drives the manuscript score).
    # ------------------------------------------------------------------
    cross_chapter_result = compute_cross_chapter_coherence(
        coherence_result.embeddings, section_roles, parsed_sections
    )

    # ------------------------------------------------------------------
    # Step 4 – Reasoning (sequential to respect Gemini free-tier RPM)
    # ------------------------------------------------------------------
    # Driven by the calibrated role-pair scores (cross_chapter_result),
    # not the deprecated linear adjacent-pair coherence_result.section_scores
    # -- see the Aug 2026 review note above Step 3c. role_texts groups
    # section body text by the same canonical role used to compute those
    # scores, so the XAI explains the same content that was scored.
    role_texts: Dict[str, str] = aggregate_role_texts(parsed_sections, section_roles)
    inconsistency_outputs, verification_outputs, dismissed_pairs = await reasoning_service.analyze_inconsistencies(
        cross_chapter_result, role_texts
    )

    # Serialise to plain dicts for the DB service and response model
    inconsistencies_data: List[Dict[str, Any]] = [
        item.model_dump() for item in inconsistency_outputs
    ]

    # ------------------------------------------------------------------
    # Step 4b – Deterministic Numeric Audit
    # ------------------------------------------------------------------
    deterministic_flags: List[Dict[str, Any]] = []
    for pair in coherence_result.section_scores:
        sec_a_name = pair.get("section_a", "")
        sec_b_name = pair.get("section_b", "")
        role_a = section_roles.get(sec_a_name, sec_a_name.lower())
        role_b = section_roles.get(sec_b_name, sec_b_name.lower())
        text_a = parsed_sections.get(sec_a_name, "")
        text_b = parsed_sections.get(sec_b_name, "")
        flags = deterministic_service.audit_numeric_consistency(text_a, text_b, role_a, role_b)
        deterministic_flags.extend(flags)

    # Merge deterministic flags into inconsistencies_data
    for flag in deterministic_flags:
        sections_list = flag.get("sections") or []
        section_a = sections_list[0] if sections_list else ""
        section_b = sections_list[1] if len(sections_list) > 1 else section_a

        flag_type = flag.get("type")
        if flag_type == "deterministic_invalid_value":
            value_type = flag.get("value_type", "numeric").replace("_", " ")
            found = flag.get("found")
            explanation_what = f"Deterministic check: invalid {value_type} reported ({found})."
            explanation_why = (
                f"The reported {value_type} falls outside the range that is statistically "
                "possible, which usually indicates a typo or reporting error."
            )
            suggested_fix = (
                f"Verify and correct the {value_type} value ({found}) against the "
                "source data or statistical output."
            )
        elif flag_type == "deterministic_significance_mismatch":
            p_value = flag.get("p_value")
            claimed = flag.get("claimed_significance")
            implied = flag.get("implied_significance")
            explanation_what = (
                f"Deterministic check: text claims the result is '{claimed}' but the "
                f"reported p-value (p = {p_value}) implies it is '{implied}'."
            )
            explanation_why = (
                "The stated significance conclusion contradicts the reported p-value, "
                "which can mislead readers about the strength of the evidence."
            )
            suggested_fix = (
                f"Reconcile the wording with the reported p-value (p = {p_value}), or "
                "correct the p-value if the significance claim is the intended one."
            )
        else:  # deterministic_numeric_mismatch (sample size / percentage)
            value_type = flag.get("value_type", "numeric")
            explanation_what = (
                f"Deterministic check: {value_type} mismatch. "
                f"Expected {flag.get('expected')}, found {flag.get('found')}."
            )
            explanation_why = (
                "A numeric value in the Methodology section does not match the value "
                "reported in the Results section. This may indicate a data entry error "
                "or inconsistent reporting."
            )
            suggested_fix = (
                f"Verify that the {value_type} value "
                f"({flag.get('expected')} vs {flag.get('found')}) is consistent "
                "across both sections and matches the actual study data."
            )

        inconsistencies_data.append({
            "inconsistency_id": str(uuid.uuid4()),
            "section_a": section_a,
            "section_b": section_b,
            # None, not 0.0 -- a deterministic numeric check has no
            # coherence score at all; 0.0 was indistinguishable from a
            # real (and catastrophic) coherence score of zero.
            "coherence_score": None,
            "explanation_what": explanation_what,
            "explanation_why": explanation_why,
            "suggested_fix": suggested_fix,
            "evidence_a": flag.get("evidence_a", ""),
            "evidence_b": flag.get("evidence_b", ""),
            # Extracted directly from the source text, not model-generated.
            "evidence_verified": True,
            "objectives_unaddressed": [],
            "finding_status": "material_issue",
        })

    # ------------------------------------------------------------------
    # Step 5 – Citation Audit
    # ------------------------------------------------------------------
    # No 20,000-char slice here anymore — that cap silently truncated a
    # real 150-reference APA list mid-entry. CitationAuditService applies
    # its own much larger DoS-guard cap (150,000 chars) internally.
    references_text: str = (
        parsed_sections.get("References") or parsed_sections.get("references") or ""
    )
    citation_audit_result: Dict[str, Any] = await CitationAuditService.audit_citations(
        references_text, body_text=raw_text
    )
    citations: List[Dict[str, Any]] = citation_audit_result["citations"]
    citation_crossmatch: Dict[str, Any] = citation_audit_result["crossmatch"]

    references_section_present: bool = bool(references_text.strip())
    intext_citation_count = len(extract_intext_citations(raw_text))

    # ------------------------------------------------------------------
    # Step 5b – Citation Integrity
    # ------------------------------------------------------------------
    citation_integrity_result = compute_citation_integrity(
        citations, citation_crossmatch, intext_citation_count, references_section_present
    )

    # ------------------------------------------------------------------
    # Step 5c – Composite Functional Metric
    # ------------------------------------------------------------------
    functional_metric = compute_functional_metric(
        structural_result, cross_chapter_result, citation_integrity_result
    )

    # scoring.py is deterministic-only and knows nothing about the XAI
    # layer, so the reasoning-layer outputs (dismissed false-positive
    # pairs, strong-pair verification notes) are folded into the same
    # coherence_detail dict here rather than in scoring.py -- it already
    # rides whole into both persistence and the API response.
    score_breakdown_dict: Dict[str, Any] = functional_metric.to_dict()
    score_breakdown_dict.setdefault("coherence_detail", {})
    score_breakdown_dict["coherence_detail"]["dismissed_pairs"] = dismissed_pairs
    score_breakdown_dict["coherence_detail"]["verifications"] = [
        v.model_dump() for v in verification_outputs
    ]

    # ------------------------------------------------------------------
    # Step 5d – Advisory AI-Text Indicator (never affects the score)
    # ------------------------------------------------------------------
    ai_text_result = await asyncio.to_thread(
        compute_manuscript_ai_text_indicator,
        parsed_sections,
        embedding_service._model,
    )

    # ------------------------------------------------------------------
    # Step 6 – Database Persistence
    # ------------------------------------------------------------------
    # Build lightweight section-level recommendations from the XAI output
    # (one entry per low-coherence pair directing the researcher to the fix).
    recommendations: List[Dict[str, Any]] = [
        {
            "target_section_name": item["section_a"],
            "recommendation_text": item["suggested_fix"],
            "recommendation_priority": (
                # A deterministic numeric-audit flag has no coherence
                # score (None) and is always actionable -- treat that the
                # same as a severe coherence gap.
                "High" if item["coherence_score"] is None or item["coherence_score"] < 50
                else "Medium"
            ),
        }
        for item in inconsistencies_data
        if item.get("suggested_fix")
    ]

    db_result: Dict[str, Any] = await DatabasePersistenceService.save_scan_transaction(
        user_id=req.user_id,
        manuscript_id=manuscript_id,
        analysis_run_id=analysis_run_id,
        coherence_score=int(functional_metric.overall_score),
        inconsistencies=inconsistency_outputs,   # accepts Pydantic models or dicts
        recommendations=recommendations,
        citations=citations,
        crossmatch_issues=citation_crossmatch,
        score_breakdown=score_breakdown_dict,
        ai_text_indicator={
            "overall_score": ai_text_result.overall_score,
            "section_scores": ai_text_result.section_scores,
            "flagged_sections": ai_text_result.flagged_sections,
        },
        doc_url=req.doc_url,
        sections_analyzed=list(parsed_sections.keys()),
        missing_sections=missing_sections,
        has_all_required_sections=has_all_required_sections,
        inconsistencies_found=len(inconsistencies_data),
        citations_audited=len(citations),
    )
    db_save_status: str = db_result.get("status", "unknown")

    # ------------------------------------------------------------------
    # Step 7 – Notification (non-blocking background task)
    # ------------------------------------------------------------------
    # Derive a human-readable manuscript title for the push message.
    # We use the Google Doc URL as a fallback since the title is not in
    # the request model; the notification service only needs a string.
    manuscript_title: str = req.doc_url
    try:
        ms_row = (
            db_client
            .table("manuscript")
            .select("manuscript_title")
            .eq("manuscript_id", manuscript_id)
            .single()
            .execute()
        )
        if ms_row and ms_row.data and ms_row.data.get("manuscript_title"):
            manuscript_title = ms_row.data["manuscript_title"]
    except Exception:
        pass  # silently fall back to URL — non-critical

    notification_dispatched = await NotificationDeliveryService.send_scan_completion_alert(
        user_id=req.user_id,
        manuscript_title=manuscript_title,
        coherence_score=int(functional_metric.overall_score),
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
        doc_url=req.doc_url,
        overall_coherence_score=functional_metric.overall_score,
        sections_analyzed=list(parsed_sections.keys()),
        missing_sections=missing_sections,
        has_all_required_sections=has_all_required_sections,
        section_scores=coherence_result.section_scores,
        inconsistencies_found=len(inconsistencies_data),
        inconsistencies=[
            InconsistencyReport(**item) for item in inconsistencies_data
        ],
        verifications=[
            VerificationReport(**v.model_dump()) for v in verification_outputs
        ],
        citations_audited=len(citations),
        citations=[
            CitationReport(
                citation_raw_reference_text=c.get("citation_raw_reference_text", ""),
                citation_is_accessible=c.get("citation_is_accessible", False),
                citation_status=c.get("citation_status", "no_link"),
                citation_primary_link=c.get("citation_primary_link"),
                citation_authors_parsed=c.get("citation_authors_parsed"),
                citation_year_parsed=c.get("citation_year_parsed"),
                citation_crossref_title=c.get("citation_crossref_title"),
                citation_title_match_score=c.get("citation_title_match_score"),
                citation_is_cited_in_text=c.get("citation_is_cited_in_text"),
            )
            for c in citations
        ],
        score_breakdown=ScoreBreakdown(**score_breakdown_dict),
        ai_text_indicator=AITextIndicatorReport(
            overall_score=ai_text_result.overall_score,
            section_scores=ai_text_result.section_scores,
            flagged_sections=ai_text_result.flagged_sections,
            disclaimer=ai_text_result.disclaimer,
        ),
        credits_remaining=credits_remaining,
        db_save_status=db_save_status,
        notification_dispatched=notification_dispatched
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
    result: Optional[ScanResponse] = None


async def _run_scan_job(
    req: ScanRequest, analysis_run_id: str, manuscript_id: str, credits_remaining: int
) -> None:
    """
    Background wrapper around the scan pipeline for the async job endpoint.

    Persists the finished report to `analysis_run.result_json` so the polling
    endpoint can return it verbatim — identical to what the synchronous
    endpoint would have returned, with no field lost in reconstruction.

    Any failure is recorded on the row and the debited credit is refunded —
    a failed scan must never cost the student a credit. Without the status
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
            result = await _execute_scan_pipeline(req, analysis_run_id, manuscript_id, credits_remaining)
            db_client = DatabasePersistenceService.get_client()
            await asyncio.to_thread(
                lambda: db_client.table("analysis_run").update({
                    "status": "completed",
                    "result_json": result.model_dump(mode="json"),
                }).eq("analysis_run_id", analysis_run_id).execute()
            )
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
            await credits_service.refund_scan_credit(req.user_id, analysis_run_id)


@app.post(
    "/api/scans/run",
    response_model=ScanResponse,
    summary="Master Production Scan (synchronous)",
    description=(
        "Runs the full RESYNC analysis pipeline and blocks until it finishes: "
        "Ingestion → Parser → Embedding → Reasoning → Citation → DB → Notification. "
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
    """Synchronous scan — provisions the run row, debits a credit, then
    awaits the full pipeline. Refunds the credit if the pipeline throws.

    Only one scan runs at a time (see SCAN_SLOT). Rather than blocking here
    and risking Render's 100s proxy timeout while merely queued, this fails
    fast with 503 so the Android client gets an honest "busy, retry" instead
    of a silent timeout."""
    _assert_owner(authenticated_user_id, req.user_id)
    analysis_run_id, manuscript_id, credits_remaining = await _provision_analysis_run(req)
    try:
        await asyncio.wait_for(SCAN_SLOT.acquire(), timeout=5.0)
    except asyncio.TimeoutError:
        await credits_service.refund_scan_credit(req.user_id, analysis_run_id)
        raise HTTPException(
            status_code=503,
            detail="Another scan is currently in progress. Please retry shortly.",
            headers={"Retry-After": "60"},
        )
    try:
        return await _execute_scan_pipeline(req, analysis_run_id, manuscript_id, credits_remaining)
    except Exception:
        await credits_service.refund_scan_credit(req.user_id, analysis_run_id)
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

    FK validation and the credit debit both still happen inline, so an
    invalid user_id/manuscript_id fails fast with 404, and an empty
    balance fails fast with 402 — rather than being accepted and failing
    in the background where the client can't see why.
    """
    _assert_owner(authenticated_user_id, req.user_id)
    analysis_run_id, manuscript_id, credits_remaining = await _provision_analysis_run(req)
    background_tasks.add_task(_run_scan_job, req, analysis_run_id, manuscript_id, credits_remaining)
    logger.info("Accepted async scan job: %s", analysis_run_id)
    return ScanJobResponse(status="processing", analysis_run_id=analysis_run_id)


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
            result=ScanResponse(**result_json)
        )

    return ScanStatusResponse(status="processing", analysis_run_id=analysis_run_id)


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
        result = await credits_service.create_checkout(x_user_id, body.credit_amount)
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
