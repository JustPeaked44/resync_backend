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
    assert payload["result_version"] == "v7" and payload["overall_coherence_score"] == 60.0
    assert payload["score_breakdown"]["band"] == "Needs Revision"
    assert payload["manuscript_flow"][0]["explanation"]["coherence_scores"]["overall"] == 0.6
    assert payload["inconsistencies_found"] == 1
    assert payload["inconsistencies"][0]["evidence_verified"] is True
    assert payload["recommendations"][0]["original_sentence"] == "The resulting text also contains several words."
    assert payload["ai_synthesis"]["summary"] == "The texts correlate partly."
    assert captured["save"]["coherence_score"] == 60.0
    assert captured["save"]["inconsistencies"][0]["inconsistency_id"] == payload["inconsistencies"][0]["inconsistency_id"]


def test_full_v7_response_contract_and_legacy_deserialization(assembled_scan):
    payload, _, _ = assembled_scan
    assert set(payload) == {
        "status", "analysis_run_id", "user_id", "manuscript_id", "doc_url", "result_version",
        "overall_coherence_score", "sections_analyzed", "manuscript_flow", "inconsistencies_found",
        "inconsistencies", "recommendations", "ai_synthesis", "citations_audited", "citations",
        "score_breakdown", "ai_text_indicator", "db_save_status", "notification_dispatched",
        "credits_remaining"}
    assert payload["result_version"] == "v7"
    assert len(payload["manuscript_flow"]) == 1
    pair = payload["manuscript_flow"][0]
    assert set(pair) == {"order", "section_a", "section_b", "pair_weight", "scores",
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
    assert set(payload["recommendations"][0]) == {"recommendation_id", "section_a", "section_b",
                                                "pair_order", "original_sentence", "recommended_revision",
                                                "recommendation_text", "recommendation_priority"}
    assert payload["recommendations"][0]["recommendation_id"]
    assert set(payload["ai_synthesis"]) == {"summary", "strongest_pair", "weakest_pair"}
    for key in ("strongest_pair", "weakest_pair"):
        assert set(payload["ai_synthesis"][key]) == {"order", "section_a", "section_b", "overall_coherence", "score"}
    assert set(payload["score_breakdown"]) == {"overall_score", "band", "cross_chapter_coherence_score",
                                               "citation_integrity_score", "dimensions", "citation_detail"}
    assert set(payload["score_breakdown"]["dimensions"]) == {"objectives_alignment", "methodology_fit",
                                                               "numeric_consistency", "claim_support"}
    assert set(payload["score_breakdown"]["citation_detail"]) == {"well_formed_ratio", "link_resolution_rate",
                                                                    "cross_match_score", "total_entries"}
    assert set(payload["ai_text_indicator"]) == {"overall_score", "section_scores", "flagged_sections", "disclaimer"}
    legacy = dict(payload)
    legacy.pop("result_version")
    assert main.ScanResponse.model_validate(legacy).result_version is None
    assert main.ScanResponse.model_validate(dict(legacy, result_version=None)).result_version is None


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
