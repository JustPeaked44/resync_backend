import asyncio
import json
import pytest
from services import v7_explain
from services.v7_scan import assemble_flow, flatten_explanations

@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(v7_explain, "EXPLAIN_RETRY_PAUSE", 0)


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
    assert set(result) == {"severity", "failed_dimension", "coherence_scores", "link_summary", "comparison", "highlighted_inconsistencies", "recommendations", "reference_list", "bridges"}
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
    assert "recommended_revision, category" in prompts[0]
    assert all(f"{index}." in prompts[0] for index in range(1, 6))


def test_summary_failure_returns_null(monkeypatch):
    async def fail(_):
        raise RuntimeError("quota exhausted")

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fail)
    assert asyncio.run(v7_explain.explain_summary([], [])) is None


@pytest.mark.parametrize("gemini_category,numeric_applicable,expected", [
    ("Methodology", False, "methodology"),
    ("claim_support", False, "claims"),
    ("claim", False, "claims"),
    ("numbers", False, "numeric"),
    ("numeric_consistency", False, "numeric"),
    ("objectives_alignment", False, "objectives"),
    ("methodology_fit", False, "methodology"),
    (None, False, "claims"),
    ("unrelated", False, "claims"),
    ("unrelated", True, "numeric"),
])
def test_fake_gemini_recommendation_category(monkeypatch, gemini_category, numeric_applicable, expected):
    scores = dict(SCORES, numeric_applicable=numeric_applicable, claim_support=0.2,
                  numeric_consistency=0.1)
    raw = {"recommendations": [{"section": "A", "original_sentence": "A text.",
                                "recommended_revision": "A text, linked to B.",
                                "category": gemini_category}]}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(raw)}]}}]}

    async def fake(_):
        return Response()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake)
    result = asyncio.run(v7_explain.explain_pair("A", "A text.", "B", "B text.", scores, [], []))
    assert result["recommendations"][0]["category"] == expected


def test_empty_gemini_answer_is_severity_none_without_generic_advice(monkeypatch):
    async def fake(_):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
        return Response()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake)
    scores = dict(SCORES, numeric_applicable=False, numeric_consistency=0.01, methodology_fit=0.1)
    result = asyncio.run(v7_explain.explain_pair("A", "A text.", "B", "B text.", scores,
                                                 [{"sentence": "A text."}], []))
    assert result["severity"] == "None" and result["recommendations"] == []  # culprit hints alone invent no advice


@pytest.mark.parametrize("raw,issues,recommendations,references", [
    ({"highlighted_inconsistencies": [{"section": "Section B (B)", "quote": "fabricated",
                                    "what": "Contradiction", "why": "A and B disagree."}],
      "recommendations": [{"section": "Section B (B)", "original_sentence": "fabricated",
                           "recommended_revision": "Align the statement with A."}],
      "reference_list": [{"section": "Section A (A)", "sentence": "A says improvement."}]}, 0, 0, 0),   # strict: nothing quotable, so no issue
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
def test_fake_gemini_rejects_issues_it_cannot_quote_from_the_paper(monkeypatch, raw, issues, recommendations, references):
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
        assert len(findings) == 1
        assert revisions == []  # no generic advice is invented when Gemini gave none
        assert findings[0]["evidence_verified"] is True
        assert findings[0]["evidence_quotes"] == [{"section": "A", "text": "A says improvement."}]


def test_an_issue_with_a_fabricated_quote_is_dropped_and_a_verified_quote_is_kept():
    base = {"highlighted_inconsistencies": [{"section": "B", "quote": "fabricated", "what": "Contradiction", "why": "A and B disagree."}]}
    dropped = v7_explain.normalize_explanation(dict(base, severity="Critical"), SCORES, "A", "A says improvement.", "B", "B says no effect.")
    assert dropped["severity"] == "None" and dropped["highlighted_inconsistencies"] == []   # strict: no quotable contradiction
    real = {"severity": "Critical", "highlighted_inconsistencies": [{"section": "B", "quote": "B says no effect.", "what": "Contradiction", "why": "A and B disagree."}]}
    kept = v7_explain.normalize_explanation(real, SCORES, "A", "A says improvement.", "B", "B says no effect.")
    assert kept["severity"] == "Critical" and kept["highlighted_inconsistencies"][0]["quote"] == "B says no effect."
    flow = assemble_flow([{"name": "A", "text": "A says improvement."}, {"name": "B", "text": "B says no effect."}], [{"scores": dict(SCORES, pair_weight=1)}])
    flow[0]["explanation"] = kept
    findings, _ = flatten_explanations(flow)
    assert findings[0]["explanation_what"] == "Contradiction" and findings[0]["evidence_verified"] is True


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


def test_one_fake_gemini_call_returns_only_verified_recommendations(monkeypatch):
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
    assert "STRICT TRIAGE RULES" in calls[0]["contents"][0]["parts"][0]["text"]
    assert result["severity"] == "Moderate"
    assert [(r["section"], r["original_sentence"]) for r in result["recommendations"]] == [("A", "A says improvement.")]
    assert result["highlighted_inconsistencies"][1]["quote"] is None
    sections = [{"name": "A", "text": "A says improvement. A has another culprit."},
                {"name": "B", "text": "B says no effect."}]
    flow = assemble_flow(sections, [{"scores": dict(SCORES, pair_weight=1)}])
    flow[0]["explanation"] = result
    _, recommendations = flatten_explanations(flow, sections)
    assert [r["id"] for r in recommendations] == ["r-1-1"]
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


def test_bridges_are_validated_capped_and_listed_first():
    low = dict(SCORES, overall_coherence=0.5)
    raw = {"severity": "Moderate", "highlighted_inconsistencies": [{"section": "A", "quote": "A text here.", "what": "w", "why": "y"}], "bridges": [
        {"section": "Section B (B)", "placement": "start", "sentence": "Section A sets the goal that B tests."},
        {"section": "A", "placement": "end", "sentence": "B tests this goal with a pre-test."},
        {"section": "A", "placement": "middle", "sentence": "Invalid placement."},
        {"section": "Z", "placement": "end", "sentence": "Unknown section."},
        {"section": "A", "placement": "start", "sentence": "Over the two-bridge cap."}]}
    result = v7_explain.normalize_explanation(raw, low, "A", "A text here.", "B", "B text here.")
    assert [b["placement"] for b in result["bridges"]] == ["start", "end"]
    assert result["bridges"][0]["sentence"] == 'The "A" section sets the goal that B tests.'
    flow = assemble_flow([{"name": "A", "text": "A text here."}, {"name": "B", "text": "B text here."}],
                         [{"scores": dict(low, pair_weight=1)}])
    flow[0]["explanation"] = result
    _, recommendations = flatten_explanations(flow)
    assert [r["kind"] for r in recommendations] == ["add", "add"]
    assert recommendations[0]["original_sentence"] is None and recommendations[0]["placement"] == "start"
    far = v7_explain.normalize_explanation(raw, low, "A", "A text here.", "B", "B text here.", adjacent=False)
    assert far["severity"] == "Moderate" and far["bridges"] == []        # a linking sentence only fits neighboring sections
    # severity "None" (no real issue) clears findings and advice, whatever else the model returned. A loosely linked
    # neighbor pair still keeps its linking sentences as a soft suggestion; a well linked one keeps nothing.
    loose = v7_explain.normalize_explanation(dict(raw, severity="None"), low, "A", "t", "B", "t")
    assert loose["severity"] == "None" and loose["highlighted_inconsistencies"] == [] and loose["recommendations"] == []
    assert [b["placement"] for b in loose["bridges"]] == ["start", "end"]
    tight = v7_explain.normalize_explanation(dict(raw, severity="None"), dict(SCORES, overall_coherence=0.8), "A", "t", "B", "t")
    assert tight["severity"] == "None" and tight["bridges"] == []
    not_neighbors = v7_explain.normalize_explanation(dict(raw, severity="None"), low, "A", "t", "B", "t", adjacent=False)
    assert not_neighbors["bridges"] == []


def test_a_transient_gemini_failure_is_retried_and_exhausted_retries_leave_severity_unknown(monkeypatch):
    calls = {"n": 0}

    class Ok:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"severity": "None"})}]}}]}

    async def flaky(_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("503")
        return Ok()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", flaky)
    recovered = asyncio.run(v7_explain.explain_pair("A", "A text.", "B", "B text.", SCORES, [], []))
    assert calls["n"] == 2 and recovered["severity"] == "None"

    async def always_down(_):
        raise RuntimeError("503")

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", always_down)
    failed = asyncio.run(v7_explain.explain_pair("A", "A text.", "B", "B text.", SCORES, [], []))
    assert failed["severity"] is None and failed["failed_dimension"] is None


def test_no_op_edits_and_meta_bridges_are_dropped_and_the_weakest_dimension_is_charged():
    scores = dict(SCORES, overall_coherence=0.5, objectives_alignment=0.6, methodology_fit=0.3,
                  numeric_consistency=0.1, claim_support=0.5, numeric_applicable=False)
    raw = {"severity": "Moderate", "failed_dimension": "claim_support",   # Gemini's pick is advisory only
           "recommendations": [
               {"section": "A", "original_sentence": "Same sentence here.", "issue": "i", "recommended_revision": "Same  sentence   here."},
               {"section": "A", "original_sentence": "Another one.", "issue": "i", "recommended_revision": "A better one."}],
           "bridges": [{"section": "A", "placement": "end", "sentence": "Building on this, X holds."},
                       {"section": "B", "placement": "start", "sentence": "As the next section shows, Y."},
                       {"section": "B", "placement": "start", "sentence": "The tables store the data entered on the screens."}]}
    result = v7_explain.normalize_explanation(raw, scores, "A", "Same sentence here. Another one.", "B", "B text.")
    assert [r["original_sentence"] for r in result["recommendations"]] == ["Another one."]
    assert [b["sentence"] for b in result["bridges"]] == ["The tables store the data entered on the screens."]
    # methodology is the weakest part the model rated; numeric is excluded because it does not apply here
    assert result["failed_dimension"] == "methodology_fit"
    assert v7_explain.normalize_explanation(dict(raw, severity="None"), scores, "A", "t", "B", "t")["failed_dimension"] == "none"


def test_gemini_sees_the_end_of_the_first_section_and_the_start_of_the_second(monkeypatch):
    prompts = []

    class Ok:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"severity": "None"})}]}}]}

    async def fake(payload):
        prompts.append(payload["contents"][0]["parts"][0]["text"])
        return Ok()

    monkeypatch.setattr(v7_explain.gemini, "post_gemini_with_retry", fake)
    long_a = "START_OF_A " + "filler words go here. " * 600 + "END_OF_A"
    long_b = "START_OF_B " + "other filler words. " * 600 + "END_OF_B"
    asyncio.run(v7_explain.explain_pair("A", long_a, "B", long_b, SCORES, [], []))
    prompt = prompts[0]
    assert "END_OF_A" in prompt and "START_OF_A" not in prompt     # the tail of the first section
    assert "START_OF_B" in prompt and "END_OF_B" not in prompt     # the head of the second section


def test_truncate_tail_keeps_the_end_on_a_whole_word():
    from services.gemini import truncate_tail
    text = " ".join(f"word{i}" for i in range(2000))
    cut = truncate_tail(text, 100)
    assert cut.startswith("[...] word") and cut.endswith("word1999") and len(cut) <= 110
    assert truncate_tail("short text", 100) == "short text"
