import asyncio
from types import SimpleNamespace
import pytest

import main
from services import parser, v7_explain


@pytest.fixture
def assembled_scan(monkeypatch):
    captured = {}
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
        return "raw manuscript"

    def parse(*_):
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

    req = main.ScanRequest(user_id="user", manuscript_title="Test", filename="test.docx")
    result = asyncio.run(main._execute_scan_pipeline(req, "run", "ms", 4, b"doc", "test.docx"))
    payload = result.model_dump(mode="json")
    return payload, captured, sections


def test_scan_contract_and_persistence_with_fakes(assembled_scan):
    payload, captured, sections = assembled_scan
    assert captured["ml_sections"] == [
        {"name": "Related Studies", "text": sections["Related Studies"] + "\n" + sections["Related Studies (2)"]},
        {"name": "Results", "text": sections["Results"]}]
    assert payload["manuscript_sections"] == captured["ml_sections"]
    assert payload["result_version"] == "v7" and payload["overall_coherence_score"] == 66.67
    assert payload["score_breakdown"]["band"] == "Needs Revision"
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
    assert captured["save"]["coherence_score"] == 66.67
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
        "score_breakdown", "ai_text_indicator", "db_save_status", "notification_dispatched",
        "credits_remaining"}
    assert payload["result_version"] == "v7"
    assert len(payload["manuscript_flow"]) == 1
    pair = payload["manuscript_flow"][0]
    assert set(pair) == {"order", "section_a", "section_b", "pair_weight", "pair_score", "numeric_applicable", "scores",
                         "skipped_reason", "culprit_sentences", "explanation"}
    assert set(pair["scores"]) == {"objectives_alignment", "methodology_fit", "numeric_consistency",
                                   "claim_support", "overall_coherence"}
    assert set(pair["culprit_sentences"]) == {"section_a", "section_b"}
    assert set(pair["explanation"]) == {"coherence_scores", "highlighted_inconsistencies",
                                        "recommendations", "reference_list"}
    assert set(pair["explanation"]["coherence_scores"]) == {"overall", "objectives", "methodology", "numeric", "claims"}
    assert set(pair["explanation"]["highlighted_inconsistencies"][0]) == {"section", "quote", "what", "why"}
    assert set(pair["explanation"]["recommendations"][0]) == {"section", "original_sentence", "recommended_revision"}
    assert pair["explanation"]["reference_list"] == [{"section": "Related Studies", "sentence": "A source sentence with several words."}]
    assert set(payload["inconsistencies"][0]) == {"section_a", "section_b", "pair_order", "coherence_score",
                                                  "explanation_what", "explanation_why", "suggested_fix",
                                                  "inconsistency_id", "evidence_a", "evidence_b", "evidence_quotes",
                                                  "evidence_verified", "finding_status"}
    assert payload["inconsistencies"][0]["inconsistency_id"]
    assert set(payload["inconsistencies"][0]["evidence_quotes"][0]) == {"section", "text"}
    assert set(payload["recommendations"][0]) == {"id", "section", "recommendation_id", "section_a", "section_b",
                                                "pair_order", "original_sentence", "recommended_revision",
                                                "recommendation_text", "recommendation_priority"}
    assert payload["recommendations"][0]["recommendation_id"]
    assert payload["recommendations"][0]["id"] == "r-1-1"
    assert set(payload["ai_synthesis"]) == {"summary", "strongest_pair", "weakest_pair"}
    for key in ("strongest_pair", "weakest_pair"):
        assert set(payload["ai_synthesis"][key]) == {"order", "section_a", "section_b", "overall_coherence", "score"}
    assert set(payload["score_breakdown"]) == {"overall_score", "band", "cross_chapter_coherence_score",
                                               "citation_integrity_score", "dimensions", "dimension_weights", "citation_detail"}
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
    loaded = main.ScanResponse.model_validate(legacy)
    assert loaded.result_version is None
    assert loaded.manuscript_sections is None
    assert loaded.access is None
    assert loaded.score_breakdown.dimension_weights is None
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
    assert result["manuscript_score"] == 58.75
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
