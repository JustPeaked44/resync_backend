import asyncio
from types import SimpleNamespace

import main
from services import parser, v7_explain


def test_scan_contract_and_persistence_with_fakes(monkeypatch):
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
            "recommendations": [{"section": "Results", "original_sentence": "fabricated",
                                 "recommended_revision": "Clarify the link."}],
            "reference_list": []})

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
    assert captured["ml_sections"] == [
        {"name": "Related Studies", "text": sections["Related Studies"] + "\n" + sections["Related Studies (2)"]},
        {"name": "Results", "text": sections["Results"]}]
    assert payload["result_version"] == "v7" and payload["overall_coherence_score"] == 60.0
    assert payload["score_breakdown"]["band"] == "Needs Revision"
    assert payload["manuscript_flow"][0]["explanation"]["coherence_scores"]["overall"] == 0.6
    assert payload["inconsistencies_found"] == 1
    assert payload["inconsistencies"][0]["evidence_verified"] is True
    assert payload["recommendations"][0]["original_sentence"] is None
    assert payload["ai_synthesis"]["summary"] == "The texts correlate partly."
    assert captured["save"]["coherence_score"] == 60.0
    assert captured["save"]["inconsistencies"][0]["inconsistency_id"] == payload["inconsistencies"][0]["inconsistency_id"]


def test_legacy_poll_model_passes_payload_through():
    legacy = {"status": "completed", "overall_coherence_score": 42}
    result = main.ScanStatusResponse(status="completed", analysis_run_id="old", result=legacy)
    assert result.model_dump()["result"] == legacy
