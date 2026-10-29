import asyncio
import json
import pytest
from services import v7_explain
from services.v7_scan import assemble_flow, flatten_explanations

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
    assert len(result["highlighted_inconsistencies"]) == 2
    assert result["highlighted_inconsistencies"][1]["quote"] is None
    assert result["recommendations"] == []
    assert result["reference_list"] == [{"section": "A", "sentence": 'A curly ‘quote’.'}]


def test_normalized_quote_returns_exact_source_span_for_highlight():
    source = 'A curly ‘quote’ appears in this section.'
    result = v7_explain.normalize_explanation(
        {"recommendations": [{"section": "A", "original_sentence": "A  curly 'quote'",
                              "recommended_revision": "Revise the connection."}]},
        SCORES, "A", source, "B", "B text.")
    assert result["recommendations"][0]["original_sentence"] == "A curly ‘quote’"
    assert result["recommendations"][0]["original_sentence"] in source


def test_fake_gemini_pair_and_summary(monkeypatch):
    prompts = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            value = {"summary": "## **B correlates partly with A.**"} if "Summarize" in prompts[-1] else {
                "coherence_scores": {"overall": 1.0}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": []}
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(value)}]}}]}

    async def fake(payload):
        prompts.append(payload["contents"][0]["parts"][0]["text"])
        return Response()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake)
    result = asyncio.run(v7_explain.explain_pair("A", "A text.", "B", "B text.", SCORES, [], []))
    summary = asyncio.run(v7_explain.explain_summary([], []))
    assert result["coherence_scores"]["overall"] == 0.4
    assert summary == "B correlates partly with A."
    assert all(rule in prompt for rule in ("Do not assume or mention research type", "Do not demand specific structural changes",
                                      "Do not use outside knowledge", "Only assess whether Section B logically correlates",
                                      "Never change model scores") for prompt in prompts)
    assert all("never mention the model, pytorch, or how scores are produced" in prompt.lower() and
               "whole-number percentage (e.g. 80%)" in prompt for prompt in prompts)


def test_summary_failure_returns_null(monkeypatch):
    async def fail(_):
        raise RuntimeError("quota exhausted")

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fail)
    assert asyncio.run(v7_explain.explain_summary([], [])) is None


@pytest.mark.parametrize("raw,issues,recommendations,references", [
    ({"highlighted_inconsistencies": [{"section": "Section B (B)", "quote": "fabricated",
                                    "what": "Contradiction", "why": "A and B disagree."}],
      "recommendations": [{"section": "Section B (B)", "original_sentence": "fabricated",
                           "recommended_revision": "Align the statement with A."}],
      "reference_list": [{"section": "Section A (A)", "sentence": "A says improvement."}]}, 1, 0, 1),
    ({"highlighted_inconsistencies": [{"section": "B", "quote": "B says no effect.",
                                    "what": "No major inconsistencies found.", "why": "placeholder"}],
      "recommendations": [{"section": "B", "original_sentence": "B says no effect.",
                           "recommended_revision": "No major inconsistencies found."}]}, 0, 0, 0),
    ({"highlighted_inconsistencies": [{"section": "unknown", "quote": "fabricated",
                                    "what": "gap", "why": "reason"},
                                   {"section": "B", "quote": "B says no effect.", "what": "gap"}],
      "recommendations": [{"section": "unknown", "recommended_revision": "Revise."},
                          {"section": "B", "recommended_revision": ""}],
      "reference_list": [{"section": "B", "sentence": "fabricated"}]}, 0, 0, 0),
])
def test_fake_gemini_preserves_findings_but_rejects_unsupported_evidence(monkeypatch, raw, issues, recommendations, references):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(raw)}]}}]}

    async def fake(_):
        return Response()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake)
    result = asyncio.run(v7_explain.explain_pair("A", "A says improvement.", "B", "B says no effect.", SCORES, [], []))
    assert len(result["highlighted_inconsistencies"]) == issues
    assert len(result["recommendations"]) == recommendations
    assert len(result["reference_list"]) == references
    if issues:
        assert result["highlighted_inconsistencies"][0]["quote"] is None
        flow = assemble_flow([{"name": "A", "text": "A says improvement."},
                              {"name": "B", "text": "B says no effect."}], [{"scores": dict(SCORES, pair_weight=1)}])
        flow[0]["explanation"] = result
        findings, revisions = flatten_explanations(flow)
        assert len(findings) == 1 and revisions == []
        assert findings[0]["evidence_verified"] is True
        assert findings[0]["evidence_quotes"] == [{"section": "A", "text": "A says improvement."}]


def test_fabricated_quote_never_becomes_a_highlight():
    explanation = v7_explain.normalize_explanation(
        {"highlighted_inconsistencies": [{"section": "B", "quote": "fabricated",
                                         "what": "Contradiction", "why": "A and B disagree."}]},
        SCORES, "A", "A says improvement.", "B", "B says no effect.")
    flow = assemble_flow([{"name": "A", "text": "A says improvement."},
                          {"name": "B", "text": "B says no effect."}], [{"scores": dict(SCORES, pair_weight=1)}])
    flow[0]["explanation"] = explanation
    findings, _ = flatten_explanations(flow)
    assert findings[0]["explanation_what"] == "Contradiction"
    assert findings[0]["evidence_quotes"] == []
    assert findings[0]["evidence_a"] == findings[0]["evidence_b"] == ""
    assert findings[0]["evidence_verified"] is False


def test_recommendation_sentence_must_be_in_its_own_pair(monkeypatch):
    raw = {"recommendations": [
        {"section": "A", "original_sentence": "Only in a different section.",
         "recommended_revision": "Wrong pair."},
        {"section": "A", "original_sentence": "A says improvement.",
         "recommended_revision": "A says improvement, consistent with B."},
        {"section": "A", "original_sentence": "Time Frame:\n2 Weeks\nand produce a package.",
         "recommended_revision": "Rewrite the table as a sentence."},
        {"section": "B", "original_sentence": "B says no effect.",
         "recommended_revision": "B says no effect, despite A."}]}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(raw)}]}}]}

    async def fake(_):
        return Response()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake)
    result = asyncio.run(v7_explain.explain_pair(
        "A", "A says improvement.\nTime Frame:\n2 Weeks\nand produce a package.",
        "B", "B says no effect.", SCORES, [], []))
    assert [r["original_sentence"] for r in result["recommendations"]] == [
        "A says improvement.", "B says no effect."]


def test_user_facing_gemini_strings_are_plain_text(monkeypatch):
    raw = {"highlighted_inconsistencies": [{
        "section": "A", "quote": "**`A says improvement.`**",
        "what": "## **`claim_support`** — unsupported link",
        "why": "- **Because** the arguments differ."}],
        "recommendations": [{"section": "B", "original_sentence": "**B says no effect.**",
                             "recommended_revision": "1. **B** says no effect, despite A."}],
        "reference_list": [{"section": "A", "sentence": "A says improvement."}]}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(raw)}]}}]}

    async def fake(_):
        return Response()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake)
    result = asyncio.run(v7_explain.explain_pair(
        "A", "A says improvement.", "B", "B says no effect.", SCORES, [], []))
    assert result["highlighted_inconsistencies"][0]["what"] == "claim_support — unsupported link"
    assert result["highlighted_inconsistencies"][0]["quote"] == "A says improvement."
    assert result["highlighted_inconsistencies"][0]["why"] == "Because the arguments differ."
    assert result["recommendations"][0]["recommended_revision"] == "B says no effect, despite A."
    assert result["recommendations"][0]["original_sentence"] == "B says no effect."
    assert result["reference_list"][0]["sentence"] == "A says improvement."
    assert v7_explain.plain_text("# **`AI-text note`**") == "AI-text note"
    assert v7_explain.plain_text("● **A listed recommendation.**") == "A listed recommendation."


def test_one_fake_gemini_call_covers_each_verified_passage(monkeypatch):
    calls = []
    raw = {"highlighted_inconsistencies": [
        {"section": "B", "quote": "B says no effect.", "what": "Conflict", "why": "A reports improvement."},
        {"section": "A", "quote": "not in A", "what": "Unsupported", "why": "No source."}],
        "recommendations": [
            {"section": "A", "original_sentence": "A says improvement.", "recommended_revision": "A reports improvement relative to B."},
            {"section": "A", "original_sentence": "not in A", "recommended_revision": "Invented."}]}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(raw)}]}}]}

    async def fake(payload):
        calls.append(payload)
        return Response()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake)
    result = asyncio.run(v7_explain.explain_pair(
        "A", "A says improvement. A has another culprit.", "B", "B says no effect.", SCORES,
        [{"sentence": "A says improvement."}, {"sentence": "A has another culprit."}],
        [{"sentence": "not in B"}]))
    assert len(calls) == 1
    assert "EACH culprit sentence" in calls[0]["contents"][0]["parts"][0]["text"]
    assert [(r["section"], r["original_sentence"]) for r in result["recommendations"]] == [
        ("A", "A says improvement."), ("A", "A has another culprit."), ("B", "B says no effect.")]
    assert result["highlighted_inconsistencies"][1]["quote"] is None
    sections = [{"name": "A", "text": "A says improvement. A has another culprit."},
                {"name": "B", "text": "B says no effect."}]
    flow = assemble_flow(sections, [{"scores": dict(SCORES, pair_weight=1)}])
    flow[0]["explanation"] = result
    _, recommendations = flatten_explanations(flow, sections)
    assert [r["id"] for r in recommendations] == ["r-1-1", "r-1-2", "r-1-3"]
    assert all(r["original_sentence"] in next(s["text"] for s in sections
                                              if s["name"] == r["target_section_name"]) for r in recommendations)


def test_shared_section_passage_has_one_highlight_recommendation():
    sections = [{"name": "A", "text": "A sentence."}, {"name": "B", "text": "Shared sentence."},
                {"name": "C", "text": "C sentence."}]
    flow = assemble_flow(sections, [{"scores": dict(SCORES, pair_weight=1)}] * 2)
    explanation = {"highlighted_inconsistencies": [], "reference_list": [],
                   "recommendations": [{"section": "B", "original_sentence": "Shared sentence.",
                                        "recommended_revision": "Revised shared sentence."}]}
    flow[0]["explanation"] = explanation
    flow[1]["explanation"] = explanation
    _, recommendations = flatten_explanations(flow, sections)
    assert [(r["id"], r["section"], r["original_sentence"]) for r in recommendations] == [
        ("r-1-1", "B", "Shared sentence.")]
