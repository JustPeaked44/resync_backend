import asyncio
from types import SimpleNamespace
import pytest

import main
from services import parser, v7_explain
from services import v7_scan

@pytest.fixture(autouse=True)
def identity_calibration(monkeypatch):
    """These tests check the scoring structure on raw values; the real curve is tested in test_v7_calibration."""
    monkeypatch.setattr(v7_scan, "CALIBRATION", ((0.0, 0.0), (1.0, 1.0)))


@pytest.fixture(autouse=True)
def fake_manuscripts_count_as_papers(monkeypatch):
    """The fake manuscripts here are a few words long; the research-paper check has its own tests (test_paper_check)."""
    monkeypatch.setattr(main, "check_research_paper", lambda *_, **__: {"ok": True, "reason": ""})


def build_scan(monkeypatch, reuse_saved_sections=False):
    captured = {"ingested": 0, "parsed": 0, "cache_find": [], "cache_save": []}
    sections = {"Related Studies": "A source sentence with several words.",
                "Related Studies (2)": "A second source sentence with several words.",
                "Results": "The resulting text also contains several words."}

    class DB:
        def table(self, *_):
            return self

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def single(self):
            return self

        def execute(self):
            return SimpleNamespace(data={})

    async def ingest(*_):
        captured["ingested"] += 1
        return "raw manuscript"

    def parse(*_):
        captured["parsed"] += 1
        return {"parsed_sections": sections, "section_order": list(sections),
                "section_roles": {"Results": "results"}}

    def ml_v7(items):
        captured["ml_sections"] = items
        return {"pairs": [{"scores": {"overall_coherence": 0.6, "objectives_alignment": 0.7,
                                       "methodology_fit": 0.8, "numeric_consistency": 0.9,
                                       "claim_support": 0.5, "pair_weight": 0.4},
                           "culprits_a": [], "culprits_b": []}]}

    def ai_text(*_):
        return {"overall_score": None, "section_scores": {}, "flagged_sections": [], "disclaimer": "Advisory"}

    async def fake_gemini(payload):
        import json
        prompt = payload["contents"][0]["parts"][0]["text"]
        raw = ({"summary": "The texts correlate partly."} if "Summarize" in prompt else {
            "coherence_scores": {"overall": 1},
            "highlighted_inconsistencies": [{"section": "Results", "quote": "The resulting text also contains several words.",
                                             "what": "Partial link", "why": "The shared topic is narrow."}],
            "recommendations": [{"section": "Results", "original_sentence": "The resulting text also contains several words.",
                                 "recommended_revision": "Clarify the link."}],
            "reference_list": [{"section": "Related Studies", "sentence": "A source sentence with several words."}]})

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": json.dumps(raw)}]}}]}
        return Response()

    async def citation(*_, **__):
        return {"citations": [], "crossmatch": {}}

    async def save(**kwargs):
        captured["save"] = kwargs
        return {"status": "success"}

    async def notify(**_):
        return True

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: DB())
    monkeypatch.setattr(main.DatabasePersistenceService, "save_scan_transaction", save)
    monkeypatch.setattr(main.DocumentIngestionService, "extract_plaintext_from_upload", ingest)
    monkeypatch.setattr(parser.ManuscriptParserService, "parse_manuscript_sections", parse)
    monkeypatch.setattr(main.ml_client, "analyze_v7", ml_v7)
    monkeypatch.setattr(main.ml_client, "analyze_ai_text", ai_text)
    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake_gemini)
    monkeypatch.setattr(main.CitationAuditService, "audit_citations", citation)
    monkeypatch.setattr(main.NotificationDeliveryService, "send_scan_completion_alert", notify)

    async def find_saved(user_id, content_hash):
        captured["cache_find"].append((user_id, content_hash))
        if not reuse_saved_sections:
            return None
        return {"manuscript_id": "earlier-scan", "raw_text": "raw manuscript",
                "parse_result": {"parsed_sections": sections, "section_order": list(sections),
                                 "section_roles": {"Results": "results"}, "structure": "headings"}}

    async def no_batch(*_, **__):
        return {}   # this fake Gemini answers one pair at a time, so every pair falls back to its own review

    monkeypatch.setattr(v7_explain, "explain_batch", no_batch)
    monkeypatch.setattr(v7_explain, "suggest_bridges_batch", no_batch)
    monkeypatch.setattr(main.SectionCache, "find", find_saved)
    monkeypatch.setattr(main.SectionCache, "save_in_background", lambda *args: captured["cache_save"].append(args))
    req = main.ScanRequest(user_id="user", manuscript_title="Test", filename="test.docx")
    result = asyncio.run(main._execute_scan_pipeline(req, "run", "ms", 4, b"doc", "test.docx"))
    payload = result.model_dump(mode="json")
    return payload, captured, sections


@pytest.fixture
def assembled_scan(monkeypatch):
    return build_scan(monkeypatch)


def test_a_first_scan_reads_and_parses_the_file_then_saves_its_sections(monkeypatch):
    payload, captured, sections = build_scan(monkeypatch)
    assert captured["ingested"] == 1 and captured["parsed"] == 1
    [(user_id, digest)] = captured["cache_find"]
    assert user_id == "user" and len(digest) == 64
    [(manuscript_id, saved_user, saved_hash, parse_result, raw_text)] = captured["cache_save"]
    assert (manuscript_id, saved_user, saved_hash, raw_text) == ("ms", "user", digest, "raw manuscript")
    assert parse_result["parsed_sections"] == sections


def test_scanning_a_saved_file_again_skips_reading_and_parsing_it(monkeypatch):
    first, _, _ = build_scan(monkeypatch)
    again, captured, _ = build_scan(monkeypatch, reuse_saved_sections=True)
    assert captured["ingested"] == 0 and captured["parsed"] == 0                  # neither step ran
    assert again["overall_coherence_score"] == first["overall_coherence_score"]    # and the report is the same
    assert again["manuscript_flow"] == first["manuscript_flow"] or len(again["manuscript_flow"]) == len(first["manuscript_flow"])
    assert [args[0] for args in captured["cache_save"]] == ["ms"]                  # this scan's manuscript can now be found by its id


def test_scan_contract_and_persistence_with_fakes(assembled_scan):
    payload, captured, sections = assembled_scan
    assert [{"name": s["name"], "text": s["text"]} for s in captured["ml_sections"]] == [
        {"name": "Related Studies", "text": sections["Related Studies"] + "\n" + sections["Related Studies (2)"]},
        {"name": "Results", "text": sections["Results"]}]
    assert payload["manuscript_sections"] == [{"name": s["name"], "text": s["text"]} for s in captured["ml_sections"]]
    assert payload["result_version"] == "v7" and payload["overall_coherence_score"] == 75.0  # the one pair has a Moderate Logical Support contradiction (keeps 25%); Numeric is N/A: (100 + 100 + 25) / 3, rounded up
    assert payload["score_breakdown"]["band"] == "Solid"   # 75: a Moderate contradiction keeps part of its credit, so it lands above the Solid line (70)
    assert payload["score_breakdown"]["dimensions"] == {
        "objectives_alignment": 0.7, "methodology_fit": 0.8,
        "numeric_consistency": None, "claim_support": 0.5}
    assert payload["score_breakdown"]["dimension_weights"] == {
        "objectives_alignment": 1 / 3, "methodology_fit": 1 / 3,
        "numeric_consistency": 0.0, "claim_support": 1 / 3}
    assert payload["manuscript_flow"][0]["pair_score"] == pytest.approx(2 / 3)
    assert payload["manuscript_flow"][0]["numeric_applicable"] is False
    assert payload["manuscript_flow"][0]["explanation"]["coherence_scores"]["overall"] == 0.6
    assert payload["inconsistencies_found"] == 1
    assert payload["inconsistencies"][0]["evidence_verified"] is True
    assert payload["inconsistencies"][0]["coherence_score"] == 66.67
    assert payload["recommendations"][0]["original_sentence"] == "The resulting text also contains several words."
    assert payload["ai_synthesis"]["summary"] == "The texts correlate partly."
    assert payload["ai_synthesis"]["strongest_pair"]["score"] == 66.67
    assert payload["ai_synthesis"]["strongest_pair"]["overall_coherence"] == 0.6
    assert captured["save"]["coherence_score"] == 75.0
    assert captured["save"]["inconsistencies"][0]["inconsistency_id"] == payload["inconsistencies"][0]["inconsistency_id"]


def test_google_doc_title_comes_from_cleaned_first_line(assembled_scan, monkeypatch):
    async def fetch(_):
        return "\n# **`A Useful Manuscript Title`**\nBody text."

    monkeypatch.setattr(main.DocumentIngestionService, "fetch_plaintext_from_gdoc", fetch)
    req = main.ScanRequest(user_id="user", doc_url="https://docs.google.com/document/d/test/edit")
    result = asyncio.run(main._execute_scan_pipeline(req, "run2", "ms2"))
    assert result.manuscript_title == "A Useful Manuscript Title"


def test_full_v7_response_contract_and_legacy_deserialization(assembled_scan):
    payload, _, _ = assembled_scan
    assert set(payload) == {
            "status", "analysis_run_id", "user_id", "manuscript_id", "doc_url", "manuscript_title", "result_version",
        "overall_coherence_score", "sections_analyzed", "manuscript_sections", "access", "manuscript_flow", "inconsistencies_found",
        "inconsistencies", "recommendations", "ai_synthesis", "citations_audited", "citations",
        "score_breakdown", "dimension_breakdown", "structure_note", "rescan_unchanged", "ai_text_indicator", "db_save_status", "notification_dispatched",
        "credits_remaining"}
    assert payload["result_version"] == "v7"
    assert len(payload["manuscript_flow"]) == 1
    pair = payload["manuscript_flow"][0]
    assert set(pair) == {"order", "section_a", "section_b", "pair_weight", "pair_score", "numeric_applicable", "scores",
                         "skipped_reason", "culprit_sentences", "explanation", "severity", "failed_dimension",
                         "index_a", "index_b", "adjacent"}
    assert set(pair["scores"]) == {"objectives_alignment", "methodology_fit", "numeric_consistency",
                                   "claim_support", "overall_coherence"}
    assert set(pair["culprit_sentences"]) == {"section_a", "section_b"}
    assert set(pair["explanation"]) == {"severity", "failed_dimension", "coherence_scores", "link_summary", "comparison", "highlighted_inconsistencies",
                                        "recommendations", "reference_list", "bridges"}
    assert set(pair["explanation"]["coherence_scores"]) == {"overall", "objectives", "methodology", "numeric", "claims"}
    assert set(pair["explanation"]["highlighted_inconsistencies"][0]) == {"section", "quote", "what", "why"}
    assert set(pair["explanation"]["recommendations"][0]) == {"section", "original_sentence", "issue", "recommended_revision", "category"}
    assert pair["explanation"]["reference_list"] == [{"section": "Related Studies", "sentence": "A source sentence with several words."}]
    assert set(payload["inconsistencies"][0]) == {"section_a", "section_b", "pair_order", "coherence_score",
                                                  "explanation_what", "explanation_why", "suggested_fix",
                                                  "inconsistency_id", "evidence_a", "evidence_b", "evidence_quotes",
                                                  "evidence_verified", "finding_status"}
    assert payload["inconsistencies"][0]["inconsistency_id"]
    assert set(payload["inconsistencies"][0]["evidence_quotes"][0]) == {"section", "text"}
    assert set(payload["recommendations"][0]) == {"id", "section", "recommendation_id", "section_a", "section_b",
                                                "pair_order", "category", "original_sentence", "recommended_revision",
                                                "recommendation_text", "recommendation_priority", "issue", "kind"}
    assert payload["recommendations"][0]["recommendation_id"]
    assert payload["recommendations"][0]["id"] == "r-1-1"
    assert set(payload["ai_synthesis"]) == {"summary", "strongest_pair", "weakest_pair", "review_status"}
    for key in ("strongest_pair", "weakest_pair"):
        assert set(payload["ai_synthesis"][key]) == {"order", "section_a", "section_b", "overall_coherence", "score"}
    assert set(payload["score_breakdown"]) == {"overall_score", "band", "cross_chapter_coherence_score",
                                               "citation_integrity_score", "dimensions", "dimension_weights", "dimension_breakdown", "citation_detail"}
    assert set(payload["score_breakdown"]["dimensions"]) == {"objectives_alignment", "methodology_fit",
                                                               "numeric_consistency", "claim_support"}
    assert set(payload["score_breakdown"]["dimension_weights"]) == set(payload["score_breakdown"]["dimensions"])
    assert set(payload["score_breakdown"]["citation_detail"]) == {"well_formed_ratio", "link_resolution_rate",
                                                                    "cross_match_score", "total_entries"}
    assert set(payload["ai_text_indicator"]) == {"overall_score", "section_scores", "flagged_sections", "disclaimer"}


def test_saved_legacy_row_without_new_fields_deserializes(assembled_scan):
    payload, _, _ = assembled_scan
    legacy = dict(payload)
    legacy.pop("result_version")
    legacy.pop("manuscript_sections")
    legacy.pop("access")
    legacy["manuscript_flow"] = [dict(pair) for pair in payload["manuscript_flow"]]
    for pair in legacy["manuscript_flow"]:
        pair.pop("pair_score")
        pair.pop("numeric_applicable")
    legacy["score_breakdown"] = dict(payload["score_breakdown"])
    legacy["score_breakdown"].pop("dimension_weights")
    legacy["recommendations"] = [dict(item) for item in payload["recommendations"]]
    for item in legacy["recommendations"]:
        item.pop("category")
    loaded = main.ScanResponse.model_validate(legacy)
    assert loaded.result_version is None
    assert loaded.manuscript_sections is None
    assert loaded.access is None
    assert loaded.score_breakdown.dimension_weights is None
    assert all("category" not in item for item in loaded.model_dump(mode="json")["recommendations"])
    assert main.ScanResponse.model_validate(dict(legacy, result_version=None)).result_version is None


def test_analyze_v7_uses_same_four_part_contract(monkeypatch):
    async def no_limit(_):
        pass

    async def no_explanation(*_):
        return [], [], {"summary": None, "strongest_pair": None, "weakest_pair": None}

    monkeypatch.setattr(main, "_check_scan_rate_limit", no_limit)
    monkeypatch.setattr(main, "explain_flow", no_explanation)
    monkeypatch.setattr(main.ml_client, "analyze_v7", lambda _: {"manuscript_score": 0.01, "pairs": [{
        "scores": {"objectives_alignment": 1, "methodology_fit": 0.5,
                   "claim_support": 0.75, "numeric_consistency": 0.1,
                   "overall_coherence": 0.2, "pair_weight": 0.9}}]})
    request = main.V7AnalyzeRequest(sections=[
        {"name": "A", "text": "The sample included n = 120 participants."},
        {"name": "B", "text": "Results reached 78% among the participants."}])
    result = asyncio.run(main.analyze_v7(request, user_id="user"))
    assert result["result_version"] == "v7"
    assert result["manuscript_score"] == 78.0   # explain_flow is faked, so the pair is unreviewed and the model rating decides: only numeric (rated 0.1) fails and keeps 10%
    assert result["dimension_breakdown"]["evidence_consistency"]["score"] == 10.0   # loose link keeps its own rating (0.1)
    assert result["dimensions"] == {"objectives_alignment": 1.0, "methodology_fit": 0.5,
                                    "numeric_consistency": 0.1, "claim_support": 0.75}
    assert set(result["dimension_weights"].values()) == {0.25}
    assert result["manuscript_flow"][0]["pair_score"] == pytest.approx(0.5875)


def test_legacy_poll_model_passes_payload_through():
    legacy = {"status": "completed", "overall_coherence_score": 42}
    result = main.ScanStatusResponse(status="completed", analysis_run_id="old", result=legacy)
    assert result.model_dump()["result"] == legacy


def test_persistence_rounds_only_legacy_integer_mirror(monkeypatch):
    updates = []

    class Table:
        def update(self, payload):
            updates.append(payload)
            return self

        def eq(self, *_):
            return self

        def execute(self):
            return SimpleNamespace(data=[])

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: SimpleNamespace(table=lambda *_: Table()))
    asyncio.run(main.DatabasePersistenceService.save_scan_transaction(
        user_id="u", manuscript_id="m", analysis_run_id="r", coherence_score=77.1,
        inconsistencies=[], recommendations=[], citations=[],
        score_breakdown={"overall_score": 77.1, "band": "Solid", "cross_chapter_coherence_score": 77.1}))
    assert updates[0]["analysis_run_coherence_score"] == 77
    assert updates[0]["functional_metric_score"] == 77.1
    assert "structural_completeness_score" not in updates[0]


def test_the_first_scan_of_a_text_is_not_flagged_as_unchanged(monkeypatch):
    main._scanned_manuscripts.clear()
    payload, _, _ = build_scan(monkeypatch)
    assert payload["rescan_unchanged"] is False


def test_scanning_the_same_text_again_says_nothing_has_changed(monkeypatch):
    main._scanned_manuscripts.clear()
    build_scan(monkeypatch)
    second, _, _ = build_scan(monkeypatch)
    assert second["rescan_unchanged"] is True


def test_a_saved_parse_also_counts_as_the_same_text(monkeypatch):
    main._scanned_manuscripts.clear()
    payload, _, _ = build_scan(monkeypatch, reuse_saved_sections=True)
    assert payload["rescan_unchanged"] is True
