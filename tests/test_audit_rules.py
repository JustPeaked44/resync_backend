"""Evidence, numeric-context, negation and fallback rules (audit round). Real-world accuracy still needs labelled manuscripts."""
import asyncio
import json

import pytest

from services import gemini, v7_explain
from services.v7_scan import (apply_figure_conflicts, assemble_flow, figure_verdict, flatten_explanations, manuscript_score,
                              polarity_conflict, select_pairs_to_explain)

SCORES = {"overall_coherence": 0.4, "objectives_alignment": 0.8, "methodology_fit": 0.7, "numeric_consistency": 0.6, "claim_support": 0.5}


def explain(raw, text_a="A text. We enrolled 96 students.", text_b="B text. The group did not improve."):
    return v7_explain.normalize_explanation(raw, SCORES, "A", text_a, "B", text_b)


def flow_for(text_a, text_b, rating=0.9):
    sections = [{"name": "methodology", "text": text_a}, {"name": "results", "text": text_b}]
    keys = ("overall_coherence", "objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support")
    return sections, assemble_flow(sections, [{"scores": {**{k: rating for k in keys}, "pair_weight": 1}}])


# ---- 1. a low score alone is not an inconsistency
def test_a_low_model_score_without_a_quoted_contradiction_is_not_an_issue():
    result = explain({"severity": "None", "highlighted_inconsistencies": ["No major inconsistencies found."]})
    assert result["severity"] == "None" and result["highlighted_inconsistencies"] == [] and result["recommendations"] == []


# ---- 2 and 3. high similarity with reversed meaning is still sent for review
def test_negation_between_otherwise_identical_sentences_is_detected():
    assert polarity_conflict("The intervention improved reading scores significantly.", "The intervention did not improve reading scores significantly.")
    assert not polarity_conflict("The sample was drawn from two schools in the city.", "Interviews were transcribed and coded by two raters.")


def test_a_pair_the_model_rates_as_similar_is_still_reviewed_when_one_side_denies_the_other():
    sections = [{"name": "a", "text": "The intervention improved reading scores significantly across both groups."},
                {"name": "b", "text": "The intervention did not improve reading scores significantly across both groups."}]
    flow = assemble_flow(sections, [{"scores": {"overall_coherence": 0.95, "objectives_alignment": 0.95, "methodology_fit": 0.95,
                                                 "numeric_consistency": 0.95, "claim_support": 0.95, "pair_weight": 1}}])
    assert select_pairs_to_explain(flow, sections) == flow     # above 0.85, but the reversal makes it worth a review
    calm = [{"name": "a", "text": "The sample came from two schools in one city region."}, {"name": "b", "text": "Interviews were coded by two raters."}]
    assert select_pairs_to_explain(assemble_flow(calm, [{"scores": {"overall_coherence": 0.95, "objectives_alignment": 0.95, "methodology_fit": 0.95,
                                                                     "numeric_consistency": 0.95, "claim_support": 0.95, "pair_weight": 1}}]), calm) == []


# ---- 4 to 7. numbers need context
def test_a_drop_the_paper_accounts_for_is_not_flagged():
    sections, flow = flow_for("We recruited 120 students. ", "24 students withdrew before the final test, so 96 students were analysed.")
    apply_figure_conflicts(flow, sections)
    assert flow[0]["explanation"] is None


def test_mentioning_dropouts_elsewhere_does_not_excuse_an_unexplained_difference():
    sections, flow = flow_for("We recruited 120 students. Some dropouts are common in school studies.", "A total of 96 students were analysed.")
    apply_figure_conflicts(flow, sections)
    explanation = flow[0]["explanation"]
    assert explanation["highlighted_inconsistencies"] and explanation["severity"] == "Minor"


def test_more_people_analysed_than_recruited_is_flagged_as_more_serious():
    sections, flow = flow_for("We recruited 96 students.", "A total of 120 students were analysed.")
    apply_figure_conflicts(flow, sections)
    assert flow[0]["explanation"]["severity"] == "Moderate"


def test_figures_from_different_stages_need_context_and_do_not_cost_points():
    sections, flow = flow_for("At baseline, 120 students were enrolled.", "At follow-up, 96 students were tested.")
    apply_figure_conflicts(flow, sections)
    explanation = flow[0]["explanation"]
    assert explanation["highlighted_inconsistencies"] == [] and explanation["recommendations"] == []
    assert explanation["unverified"][0]["reason"] == "needs_context" and explanation.get("severity") in (None, "None")
    assert figure_verdict("", "At baseline, 120 students.", 120, "", "At follow-up, 96 students.", 96) == "needs_context"


def test_the_ai_can_mark_a_numeric_item_as_needing_verification_without_scoring_it():
    raw = {"severity": "Moderate", "highlighted_inconsistencies": [
        {"section": "A", "quote": "We enrolled 96 students.", "what": "Different count", "why": "Not clear which group.", "confidence": "needs_verification"}]}
    result = explain(raw)
    assert result["severity"] == "None" and result["highlighted_inconsistencies"] == []
    assert result["unverified"][0]["reason"] == "needs_context" and result["unverified"][0]["quote"] == "We enrolled 96 students."


# ---- 8 and 9. exact quotes only
def test_a_quote_is_accepted_only_when_it_is_the_manuscripts_own_words_in_the_named_section():
    good = {"severity": "Moderate", "highlighted_inconsistencies": [{"section": "A", "quote": "We enrolled 96 students.", "what": "w", "why": "y"}]}
    assert explain(good)["highlighted_inconsistencies"][0]["quote"] == "We enrolled 96 students."
    for bad in ("we enrolled 96 students.",             # capitalization changed
                "We enrolled ninety-six students.",    # paraphrase
                "The group did not improve."):         # real sentence, but from the other section
        result = explain({"severity": "Moderate", "highlighted_inconsistencies": [{"section": "A", "quote": bad, "what": "w", "why": "y"}]})
        assert result["highlighted_inconsistencies"] == [] and result["severity"] == "None", bad
        assert result["unverified"][0]["reason"] == "quote_not_found"


# ---- 11 and 12. recommendations stay grounded
def test_an_edit_whose_original_sentence_is_not_in_the_manuscript_is_dropped():
    raw = {"severity": "Moderate", "highlighted_inconsistencies": [{"section": "A", "quote": "We enrolled 96 students.", "what": "w", "why": "y"}],
           "recommendations": [{"section": "A", "original_sentence": "We enrolled 100 students.", "issue": "i", "recommended_revision": "We enrolled 96.", "category": "numeric"}]}
    assert explain(raw)["recommendations"] == []


def test_a_bridge_that_invents_a_number_is_dropped_and_one_that_reuses_the_papers_numbers_is_kept():
    def bridge(sentence):
        raw = {"severity": "None", "bridges": [{"section": "A", "placement": "end", "sentence": sentence}]}
        scores = dict(SCORES, overall_coherence=0.5)
        return v7_explain.normalize_explanation(raw, scores, "A", "A text. We enrolled 96 students.", "B", "B text about scores.", adjacent=True)["bridges"]
    assert bridge("The 250 students enrolled here are tested in the next measure of reading scores.") == []
    assert len(bridge("The 96 students enrolled here are the ones whose reading scores are compared next.")) == 1


# ---- 13. malformed answers
@pytest.mark.parametrize("answer", [["not", "an", "object"], {}, {"unrelated": 1}, {"severity": "Catastrophic"}, {"severity": None}])
def test_malformed_answers_are_never_taken_as_a_clean_review(answer):
    assert v7_explain.valid_answer(answer) is False


def test_well_formed_answers_are_accepted():
    assert v7_explain.valid_answer({"severity": "None"}) and v7_explain.valid_answer({"severity": " moderate "})
    assert v7_explain.valid_answer({"highlighted_inconsistencies": []})


def test_the_prompt_asks_for_context_aware_numbers_exact_quotes_and_no_invented_facts():
    rules = v7_explain.STRICT_RULES
    for needle in ("same population", "needs_verification", "same capitalization", "never proof", "instead of inventing it", "Critical = a major"):
        assert needle in rules, needle


# ---- 15 to 19. scoring behaviour
def pair(order, severity=None, dimension=None, adjacent=True, rating=0.9, quote=None):
    p = {"order": order, "section_a": f"S{order}", "section_b": f"S{order + 1}", "adjacent": adjacent, "severity": severity,
         "failed_dimension": dimension, "numeric_applicable": False,
         "scores": {k: rating for k in ("overall_coherence", "objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support")}}
    if quote:
        p["explanation"] = {"highlighted_inconsistencies": [{"section": "S", "quote": quote}]}
    return p


def test_adequate_checks_with_no_findings_score_high_but_a_category_without_checks_is_not_perfect():
    score, breakdown = manuscript_score([pair(1, "None", "none"), pair(2, "None", "none"), pair(3, "None", "none", adjacent=False)])
    assert breakdown["evidence_consistency"]["score"] == "N/A"          # not assessed, not a perfect score
    assert score == 100.0 and all(e["score"] == 100.0 for k, e in breakdown.items() if k != "evidence_consistency")


def test_real_critical_inconsistencies_pull_the_score_down():
    clean, _ = manuscript_score([pair(n, "None", "none") for n in (1, 2, 3)] + [pair(n, "None", "none", adjacent=False) for n in (4, 5, 6)])
    hurt, breakdown = manuscript_score([pair(1, "Critical", "objectives_alignment", quote="q1"), pair(2, "Critical", "objectives_alignment", quote="q2"),
                                        pair(3, "None", "none")] + [pair(n, "None", "none", adjacent=False) for n in (4, 5, 6)])
    assert hurt < clean and breakdown["context_alignment"]["issues"] == 2


def test_the_same_verified_sentence_found_by_three_pairs_costs_once_and_is_listed_once():
    pairs = [pair(n, "Moderate", "claim_support", quote="Same sentence.") for n in (1, 2, 3)]
    _, breakdown = manuscript_score(pairs)
    assert breakdown["logical_support"]["items"] == 1 and breakdown["logical_support"]["issues"] == 1


def test_the_displayed_numbers_come_from_the_same_points_as_the_score():
    """What the interface shows (points per group and the equation) is exactly what the score is made of."""
    pairs = [pair(1, "None", "none"), pair(2, "Minor", "claim_support"), pair(3, "None", "none"),
             pair(4, "None", "none", adjacent=False), pair(5, "Moderate", "claim_support", adjacent=False), pair(6, "None", "none", adjacent=False)]
    _, breakdown = manuscript_score(pairs)
    entry = breakdown["logical_support"]
    flow_rate, cross_rate = entry["flow"]["credit"] / entry["flow"]["items"], entry["cross"]["credit"] / entry["cross"]["items"]
    assert entry["score"] == round((flow_rate + cross_rate) / 2 * 100, 2)
    assert entry["points"]["earned"] == round(entry["flow"]["credit"] + entry["cross"]["credit"], 2)


# ---- 14. Gemini unavailable
def test_when_the_ai_is_unavailable_no_pair_is_marked_clean(monkeypatch):
    async def blocked(*_, **__):
        raise gemini.GeminiQuotaExhausted("limit reached")

    monkeypatch.setattr(v7_explain, "_generate", blocked)
    single = asyncio.run(v7_explain.explain_pair("A", "A text.", "B", "B text.", SCORES, [], []))
    assert single["severity"] is None and single["failed_dimension"] is None
    assert asyncio.run(v7_explain.explain_batch([{"id": 1, "role_a": "A", "text_a": "A text.", "role_b": "B", "text_b": "B text.",
                                                  "scores": SCORES, "culprits_a": [], "culprits_b": [], "adjacent": True}])) == {}
