"""
Offline, no-API-key regression tests for the reasoning service's pairing
logic, evidence-integrity check, and role-text aggregation -- covering
the Aug 2026 review findings:

- the XAI used to be driven by the deprecated linear adjacent-pair scale
  instead of the calibrated role-pair scale (fixed: analyze_inconsistencies
  now partitions cross_chapter_result.pair_scores at PAR);
- weak-but-unimportant pairs used to rank equally with critical ones
  (fixed: weight * (PAR - score) deficit ranking);
- _closest_sentence could silently fabricate evidence by falling back to
  a section's first sentence on zero word-overlap (fixed: _verify_quote
  never substitutes -- it returns ("", False) instead).

All network calls (_analyze_pair, _verify_strong_pairs) are monkeypatched
so this suite runs without GEMINI_API_KEY and without hitting the network.

Run with: python -m pytest tests/test_reasoning_ranking.py -v
(or, with no pytest installed, `python tests/test_reasoning_ranking.py`).
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.reasoning import ReasoningService, InconsistencyOutput, VerificationOutput
from services.scoring import CrossChapterCoherenceResult, PAR_SCORE, aggregate_role_texts


def _pair(role_a, role_b, weight, score, included=True):
    d = {"role_a": role_a, "role_b": role_b, "weight": weight, "included": included}
    if included:
        d["raw_similarity"] = 0.5
        d["score"] = score
    else:
        d["reason"] = "role missing or empty"
    return d


def test_ranking_prefers_weight_times_deficit_and_respects_max_pairs():
    # objectives<->methodology: weight 0.22, deficit 30 -> priority 6.6 (highest)
    # objectives<->conclusion:  weight 0.16, deficit 50 -> priority 8.0 (higher still)
    # discussion<->conclusion:  weight 0.06, deficit 70 -> priority 4.2 (lower)
    # results<->discussion:     weight 0.16, deficit 10 -> priority 1.6 (lowest)
    pair_scores = [
        _pair("objectives", "methodology", 0.22, PAR_SCORE - 30),
        _pair("objectives", "conclusion", 0.16, PAR_SCORE - 50),
        _pair("discussion", "conclusion", 0.06, PAR_SCORE - 70),
        _pair("results", "discussion", 0.16, PAR_SCORE - 10),
        _pair("methodology", "results", 0.20, PAR_SCORE + 5),  # strong, not a defect
    ]
    result = CrossChapterCoherenceResult(score=50.0, pair_scores=pair_scores)

    seen_defect_pairs = []

    async def fake_analyze_pair(self, role_a, text_a, role_b, text_b, score):
        seen_defect_pairs.append((role_a, role_b))
        return InconsistencyOutput(
            section_a=role_a, section_b=role_b, coherence_score=score,
            explanation_what="x", explanation_why="y", suggested_fix="z",
        ), None

    async def fake_verify(self, pairs, role_texts, excerpt_chars=800):
        return []

    svc = ReasoningService()
    ReasoningService._analyze_pair = fake_analyze_pair
    ReasoningService._verify_strong_pairs = fake_verify

    findings, verifications, dismissed = asyncio.run(
        svc.analyze_inconsistencies(result, {}, max_pairs=3)
    )

    assert len(findings) == 3, f"expected 3 findings (max_pairs), got {len(findings)}"
    assert seen_defect_pairs[0] == ("objectives", "conclusion"), (
        f"expected highest weight*deficit pair first, got order {seen_defect_pairs}"
    )
    assert ("results", "discussion") not in seen_defect_pairs, (
        "lowest-priority pair should have been dropped by max_pairs=3"
    )
    assert ("methodology", "results") not in seen_defect_pairs, (
        "a pair scoring >= PAR must never enter the defect list"
    )


def test_excluded_pairs_never_enter_defect_list():
    pair_scores = [
        _pair("introduction", "objectives", 0.12, PAR_SCORE - 20, included=False),
    ]
    result = CrossChapterCoherenceResult(score=50.0, pair_scores=pair_scores)

    called = []

    async def fake_analyze_pair(self, role_a, text_a, role_b, text_b, score):
        called.append((role_a, role_b))
        return None, None

    async def fake_verify(self, pairs, role_texts, excerpt_chars=800):
        return []

    ReasoningService._analyze_pair = fake_analyze_pair
    ReasoningService._verify_strong_pairs = fake_verify

    svc = ReasoningService()
    findings, verifications, dismissed = asyncio.run(svc.analyze_inconsistencies(result, {}))
    assert called == [], "an excluded (included=False) pair must never be explained"
    assert findings == []


def test_document_pair_excluded_from_defect_explanation_but_verifiable():
    pair_scores = [
        _pair("abstract", "__document__", 0.08, PAR_SCORE - 40),
    ]
    result = CrossChapterCoherenceResult(score=40.0, pair_scores=pair_scores)

    defect_called = []
    verify_called = []

    async def fake_analyze_pair(self, role_a, text_a, role_b, text_b, score):
        defect_called.append((role_a, role_b))
        return None, None

    async def fake_verify(self, pairs, role_texts, excerpt_chars=800):
        verify_called.extend((p["role_a"], p["role_b"]) for p in pairs)
        return []

    ReasoningService._analyze_pair = fake_analyze_pair
    ReasoningService._verify_strong_pairs = fake_verify

    svc = ReasoningService()
    asyncio.run(svc.analyze_inconsistencies(result, {}))
    assert defect_called == [], "('abstract', '__document__') has no second section to quote -- must not be explained"


def test_strong_pairs_routed_to_verification_not_explanation():
    pair_scores = [
        _pair("methodology", "results", 0.20, PAR_SCORE + 10),
    ]
    result = CrossChapterCoherenceResult(score=90.0, pair_scores=pair_scores)

    defect_called = []
    verify_called = []

    async def fake_analyze_pair(self, role_a, text_a, role_b, text_b, score):
        defect_called.append((role_a, role_b))
        return None, None

    async def fake_verify(self, pairs, role_texts, excerpt_chars=800):
        verify_called.extend((p["role_a"], p["role_b"]) for p in pairs)
        return [VerificationOutput(role_a="methodology", role_b="results", score=90.0,
                                    alignment="substantive", note="ok")]

    ReasoningService._analyze_pair = fake_analyze_pair
    ReasoningService._verify_strong_pairs = fake_verify

    svc = ReasoningService()
    findings, verifications, dismissed = asyncio.run(svc.analyze_inconsistencies(result, {}))
    assert defect_called == []
    assert verify_called == [("methodology", "results")]
    assert len(verifications) == 1 and verifications[0].alignment == "substantive"


def test_no_material_issue_is_dismissed_not_a_finding():
    pair_scores = [_pair("objectives", "methodology", 0.22, PAR_SCORE - 25)]
    result = CrossChapterCoherenceResult(score=55.0, pair_scores=pair_scores)

    async def fake_analyze_pair(self, role_a, text_a, role_b, text_b, score):
        return None, {"role_a": role_a, "role_b": role_b, "score": score, "reason": "actually fine"}

    async def fake_verify(self, pairs, role_texts, excerpt_chars=800):
        return []

    ReasoningService._analyze_pair = fake_analyze_pair
    ReasoningService._verify_strong_pairs = fake_verify

    svc = ReasoningService()
    findings, verifications, dismissed = asyncio.run(svc.analyze_inconsistencies(result, {}))
    assert findings == [], "a no_material_issue result must not become a finding"
    assert len(dismissed) == 1 and dismissed[0]["reason"] == "actually fine"


def test_verify_quote_rejects_unverifiable_quote_without_substituting():
    # Regression: the old _closest_sentence fell back to max(sentences, key=overlap),
    # which returns sentences[0] when every sentence has zero overlap --
    # silently presenting the section's first sentence as if it were the
    # model's cited evidence.
    text = "The system uses a convolutional neural network. It was trained on leaf images. Accuracy reached 94%."
    quote, verified = ReasoningService._verify_quote("This sentence does not appear anywhere in the text.", text)
    assert verified is False
    assert quote == "", f"must return empty, not a substituted sentence; got {quote!r}"
    assert quote != "The system uses a convolutional neural network.", (
        "must not silently fall back to the first sentence"
    )


def test_verify_quote_accepts_normalized_match():
    # A Google Docs export commonly renders a typographic (curly) apostrophe;
    # the model may echo it back straight. Whitespace can also drift (a
    # hard-wrapped double space). Neither should cause a genuine quote to
    # be rejected.
    text = "The researcher’s own dataset  achieved 94% accuracy on the test set."
    quote = "The researcher's own dataset achieved 94% accuracy"
    result_quote, verified = ReasoningService._verify_quote(quote, text)
    assert verified is True
    assert result_quote == quote  # returns the original claimed quote, not a rewrite


def test_verify_quote_empty_claim_trivially_verifies():
    quote, verified = ReasoningService._verify_quote("", "some section text")
    assert verified is True
    assert quote == ""


def test_aggregate_role_texts_concatenates_shared_role():
    parsed_sections = {
        "Rationale of the Study": "Farmers lack access to diagnostic tools.",
        "Background": "Papaya diseases cause significant crop loss.",
        "Methodology": "We built a mobile app using TensorFlow Lite.",
    }
    section_roles = {
        "Rationale of the Study": "introduction",
        "Background": "introduction",
        "Methodology": "methodology",
    }
    role_texts = aggregate_role_texts(parsed_sections, section_roles)
    assert "Farmers lack access" in role_texts["introduction"]
    assert "Papaya diseases cause" in role_texts["introduction"]
    assert role_texts["introduction"].index("Farmers") < role_texts["introduction"].index("Papaya")
    assert "TensorFlow Lite" in role_texts["methodology"]
    assert "__document__" in role_texts
    assert "TensorFlow Lite" in role_texts["__document__"]
    assert "Farmers lack access" in role_texts["__document__"]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
