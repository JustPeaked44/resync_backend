import os
import logging
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class DatabasePersistenceService:
    """
    Database persistence service for persisting manuscript scan analysis transactions
    to Supabase PostgreSQL tables.
    """

    _client: Optional[Client] = None

    @classmethod
    def get_client(cls) -> Client:
        """
        Initializes and returns a Supabase Client using service role credentials.
        """
        if cls._client is None:
            supabase_url = os.getenv("SUPABASE_URL")
            supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
            if not supabase_url or not supabase_key:
                raise ValueError(
                    "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured in environment."
                )
            cls._client = create_client(supabase_url, supabase_key)
        return cls._client

    @classmethod
    async def save_scan_transaction(
        cls,
        user_id: str,
        manuscript_id: str,
        analysis_run_id: str,
        coherence_score: int,
        inconsistencies: List[Dict[str, Any]],
        recommendations: List[Dict[str, Any]],
        citations: List[Dict[str, Any]],
        doc_url: Optional[str] = None,
        sections_analyzed: Optional[List[str]] = None,
        missing_sections: Optional[List[str]] = None,
        has_all_required_sections: Optional[bool] = None,
        inconsistencies_found: Optional[int] = None,
        citations_audited: Optional[int] = None,
        crossmatch_issues: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        score_breakdown: Optional[Dict[str, Any]] = None,
        ai_text_indicator: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        1. Initialize Supabase client using SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from .env.
        2. Update `public.analysis_run` where analysis_run_id = analysis_run_id:
           - analysis_run_status = 'completed'
           - analysis_run_coherence_score = coherence_score
           - analysis_run_completed_at = 'now()'
           - the three functional-metric sub-scores, band, and biggest lever
        3. Insert rows into `public.inconsistency` (linking each to analysis_run_id).
        4. Insert rows into `public.recommendation` (linking each to analysis_run_id).
        5. Insert rows into `public.citation` (linking each to analysis_run_id), carrying
           the full segmentation/verification detail rather than just the old two fields.
        5b. Insert rows into `public.citation_crossmatch_issue` for orphan in-text
            citations and uncited references.
        6. Return {"status": "success", "analysis_run_id": analysis_run_id}

        Credit accounting is no longer done here — it happens up front in
        main.py's _provision_analysis_run via services.credits before the
        scan even starts, so an out-of-credits user is blocked with HTTP 402
        instead of finding out for free after the scan already ran.
        """
        client = cls.get_client()
        now_iso = datetime.now(timezone.utc).isoformat()

        # Step 2: Update public.analysis_run
        # Bug 5: Wrap synchronous Supabase call in asyncio.to_thread
        analysis_run_update: Dict[str, Any] = {
            "analysis_run_status": "completed", # Bug 6: Valid enum is 'completed'
            "analysis_run_coherence_score": int(coherence_score),
            "analysis_run_completed_at": now_iso,
            # Denormalized copies consumed directly by the web app's history
            # query (App.tsx) via the Supabase client — see migration 002.
            "overall_coherence_score": float(coherence_score),
            "status": "completed",
        }
        if doc_url is not None:
            analysis_run_update["doc_url"] = doc_url
        if sections_analyzed is not None:
            analysis_run_update["sections_analyzed"] = sections_analyzed
        if missing_sections is not None:
            analysis_run_update["missing_sections"] = missing_sections
        if has_all_required_sections is not None:
            analysis_run_update["has_all_required_sections"] = has_all_required_sections
        if inconsistencies_found is not None:
            analysis_run_update["inconsistencies_found"] = inconsistencies_found
        if citations_audited is not None:
            analysis_run_update["citations_audited"] = citations_audited
        if score_breakdown is not None:
            analysis_run_update["structural_completeness_score"] = score_breakdown.get("structural_completeness_score")
            analysis_run_update["cross_chapter_coherence_score"] = score_breakdown.get("cross_chapter_coherence_score")
            analysis_run_update["citation_integrity_score"] = score_breakdown.get("citation_integrity_score")
            analysis_run_update["functional_metric_score"] = score_breakdown.get("overall_score")
            analysis_run_update["functional_metric_band"] = score_breakdown.get("band")
            lever = score_breakdown.get("biggest_lever") or {}
            analysis_run_update["biggest_lever"] = lever.get("criterion")
            analysis_run_update["biggest_lever_detail"] = lever
            analysis_run_update["score_breakdown_json"] = score_breakdown
        if ai_text_indicator is not None:
            analysis_run_update["ai_text_indicator_score"] = ai_text_indicator.get("overall_score")
            analysis_run_update["ai_text_indicator_json"] = ai_text_indicator

        await asyncio.to_thread(
            lambda: client.table("analysis_run").update(analysis_run_update)
                .eq("analysis_run_id", analysis_run_id).execute()
        )

        # Step 3: Insert rows into public.inconsistency
        if inconsistencies:
            inconsistency_rows = []
            for item in inconsistencies:
                data = (
                    item.model_dump()
                    if hasattr(item, "model_dump")
                    else (item.dict() if hasattr(item, "dict") else dict(item))
                )
                row = {
                    "inconsistency_id": data.get("inconsistency_id") or str(__import__('uuid').uuid4()),
                    "analysis_run_id": analysis_run_id,
                    # Bug 6: Normalize enum values to match DB definitions (lower case for type, PascalCase for severity)
                    "inconsistency_type": (data.get("inconsistency_type") or data.get("type") or "contextual").lower(),
                    "inconsistency_severity": str(data.get("inconsistency_severity") or data.get("severity") or "Medium").capitalize(),
                    "inconsistency_description": (
                        data.get("inconsistency_description")
                        or data.get("description")
                        or data.get("explanation_what")
                        or data.get("inconsistency_explanation_what")
                        or "Semantic inconsistency detected"
                    ),
                    "inconsistency_explanation_what": (
                        data.get("inconsistency_explanation_what")
                        or data.get("explanation_what")
                        or ""
                    ),
                    "inconsistency_explanation_why": (
                        data.get("inconsistency_explanation_why")
                        or data.get("explanation_why")
                        or ""
                    ),
                    "inconsistency_suggested_fix": (
                        data.get("inconsistency_suggested_fix")
                        or data.get("suggested_fix")
                        or ""
                    ),
                    "inconsistency_text_offset_start": data.get(
                        "inconsistency_text_offset_start",
                        data.get("text_offset_start", 0)
                    ),
                    "inconsistency_text_offset_end": data.get(
                        "inconsistency_text_offset_end",
                        data.get("text_offset_end", 0)
                    ),
                    "inconsistency_status": data.get(
                        "inconsistency_status",
                        data.get("status", "active")
                    ),
                    "primary_section_name": (
                        data.get("primary_section_name")
                        or data.get("section_a")
                        or "General"
                    ),
                    "conflicting_section_name": (
                        data.get("conflicting_section_name")
                        or data.get("section_b")
                        or ""
                    ),
                    # Denormalized copies consumed directly by the web app's
                    # history query (App.tsx) via the Supabase client — see
                    # migration 002.
                    "section_a": data.get("section_a") or data.get("primary_section_name") or "General",
                    "section_b": data.get("section_b") or data.get("conflicting_section_name") or "",
                    "coherence_score": data.get("coherence_score"),
                    "explanation_what": data.get("explanation_what") or data.get("inconsistency_explanation_what") or "",
                    "explanation_why": data.get("explanation_why") or data.get("inconsistency_explanation_why") or "",
                    "suggested_fix": data.get("suggested_fix") or data.get("inconsistency_suggested_fix") or "",
                    "severity": str(data.get("severity") or data.get("inconsistency_severity") or "Medium").capitalize(),
                    # Previously computed by the XAI and returned to the
                    # client, then silently dropped here -- reload from
                    # history lost the evidence quotes and unaddressed
                    # objectives entirely. Requires migration 007.
                    "evidence_a": data.get("evidence_a") or "",
                    "evidence_b": data.get("evidence_b") or "",
                    "evidence_verified": bool(data.get("evidence_verified", False)),
                    "objectives_unaddressed": data.get("objectives_unaddressed") or [],
                }
                if "inconsistency_id" in data:
                    row["inconsistency_id"] = data["inconsistency_id"]
                inconsistency_rows.append(row)

            if inconsistency_rows:
                # Bug 5: Wrap synchronous Supabase call in asyncio.to_thread
                await asyncio.to_thread(
                    lambda: client.table("inconsistency").insert(inconsistency_rows).execute()
                )

        # Step 4: Insert rows into public.recommendation
        if recommendations:
            recommendation_rows = []
            for item in recommendations:
                data = (
                    item.model_dump()
                    if hasattr(item, "model_dump")
                    else (item.dict() if hasattr(item, "dict") else dict(item))
                )
                row = {
                    "analysis_run_id": analysis_run_id,
                    "target_section_name": (
                        data.get("target_section_name")
                        or data.get("section_name")
                        or data.get("section")
                        or ""
                    ),
                    "recommendation_text": (
                        data.get("recommendation_text")
                        or data.get("text")
                        or data.get("recommendation")
                        or ""
                    ),
                    "recommendation_priority": str(
                        data.get("recommendation_priority")
                        or data.get("priority")
                        or "Medium"
                    ).capitalize(),
                }
                if "recommendation_id" in data:
                    row["recommendation_id"] = data["recommendation_id"]
                recommendation_rows.append(row)

            if recommendation_rows:
                # Bug 5: Wrap synchronous Supabase call in asyncio.to_thread
                await asyncio.to_thread(
                    lambda: client.table("recommendation").insert(recommendation_rows).execute()
                )

        # Step 5: Insert rows into public.citation
        if citations:
            citation_rows = []
            for item in citations:
                data = (
                    item.model_dump()
                    if hasattr(item, "model_dump")
                    else (item.dict() if hasattr(item, "dict") else dict(item))
                )
                row = {
                    "analysis_run_id": analysis_run_id,
                    "citation_raw_reference_text": (
                        data.get("citation_raw_reference_text")
                        or data.get("raw_reference_text")
                        or data.get("text")
                        or data.get("reference")
                        or data.get("url")
                        or ""
                    ),
                    "citation_is_accessible": data.get(
                        "citation_is_accessible",
                        data.get("is_accessible", False)
                    ),
                    "citation_verified_at": data.get("citation_verified_at") or now_iso,
                    "citation_entry_index": data.get("citation_entry_index"),
                    "citation_authors_parsed": data.get("citation_authors_parsed"),
                    "citation_year_parsed": data.get("citation_year_parsed"),
                    "citation_links": data.get("citation_links") or [],
                    "citation_primary_link": data.get("citation_primary_link"),
                    "citation_status": data.get("citation_status", "no_link"),
                    "citation_verification_tier": data.get("citation_verification_tier"),
                    "citation_crossref_title": data.get("citation_crossref_title"),
                    "citation_crossref_year": data.get("citation_crossref_year"),
                    "citation_title_match_score": data.get("citation_title_match_score"),
                    "citation_http_status_code": data.get("citation_http_status_code"),
                    "citation_is_cited_in_text": data.get("citation_is_cited_in_text"),
                    "citation_error_detail": data.get("citation_error_detail"),
                }
                if "citation_id" in data and data["citation_id"]:
                    row["citation_id"] = data["citation_id"]
                citation_rows.append(row)

            if citation_rows:
                # Bug 5: Wrap synchronous Supabase call in asyncio.to_thread
                await asyncio.to_thread(
                    lambda: client.table("citation").insert(citation_rows).execute()
                )

        # Step 5b: Insert in-text/reference-list cross-match findings
        if crossmatch_issues:
            issue_rows: List[Dict[str, Any]] = []
            for orphan in crossmatch_issues.get("orphan_intext_citations", []):
                issue_rows.append({
                    "analysis_run_id": analysis_run_id,
                    "issue_type": "orphan_intext",
                    "surname": orphan.get("surname"),
                    "year": int(orphan["year"]) if str(orphan.get("year", "")).isdigit() else None,
                    "context_sentence": orphan.get("context"),
                })
            for uncited in crossmatch_issues.get("uncited_references", []):
                issue_rows.append({
                    "analysis_run_id": analysis_run_id,
                    "issue_type": "uncited_reference",
                    "surname": uncited.get("first_author_surname"),
                    "year": uncited.get("year_parsed"),
                    "reference_raw_text": uncited.get("citation_raw_reference_text"),
                })
            if issue_rows:
                await asyncio.to_thread(
                    lambda: client.table("citation_crossmatch_issue").insert(issue_rows).execute()
                )

        # Step 6: Return response dict
        return {
            "status": "success",
            "analysis_run_id": analysis_run_id
        }
