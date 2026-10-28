import asyncio
import json
from services import v7_explain

SCORES = {"overall_coherence": 0.4, "objectives_alignment": 0.8, "methodology_fit": 0.7,
          "numeric_consistency": 0.6, "claim_support": 0.5}


def test_normalize_verifies_quotes_and_preserves_model_scores():
    raw = {"coherence_scores": {"overall": 0.99},
           "highlighted_inconsistencies": [
               {"section": "A", "quote": "A  curly 'quote'.", "what": "gap", "why": "reason"},
               {"section": "B", "quote": "fabricated", "what": "gap", "why": "reason"}],
           "recommendations": [{"section": "B", "original_sentence": "fabricated", "recommended_revision": "Revise B."}],
           "reference_list": [{"section": "A", "sentence": "A  curly 'quote'."},
                              {"section": "B", "sentence": "fabricated"}]}
    result = v7_explain.normalize_explanation(raw, SCORES, "A", 'A curly ‘quote’.', "B", "B text.")
    assert set(result) == {"coherence_scores", "highlighted_inconsistencies", "recommendations", "reference_list"}
    assert result["coherence_scores"]["overall"] == 0.4
    assert len(result["highlighted_inconsistencies"]) == 1
    assert result["recommendations"][0]["original_sentence"] is None
    assert result["reference_list"] == [{"section": "A", "quote": "A  curly 'quote'."}]


def test_fake_gemini_pair_and_summary(monkeypatch):
    prompts = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            value = {"summary": "B correlates partly with A."} if "Summarize" in prompts[-1] else {
                "coherence_scores": {"overall": 1.0}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": []}
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(value)}]}}]}

    async def fake(payload):
        prompts.append(payload["contents"][0]["parts"][0]["text"])
        return Response()

    monkeypatch.setattr(v7_explain.reasoning_service, "_post_gemini_with_retry", fake)
    result = asyncio.run(v7_explain.explain_pair("A", "A text.", "B", "B text.", SCORES, [], []))
    summary = asyncio.run(v7_explain.explain_summary([], []))
    assert result["coherence_scores"]["overall"] == 0.4
    assert summary == "B correlates partly with A."
    assert all(rule in prompt for rule in ("Do not assume or mention research type", "Do not demand specific structural changes",
                                      "Do not use outside knowledge", "Only assess whether Section B logically correlates",
                                      "Never change model scores") for prompt in prompts)
