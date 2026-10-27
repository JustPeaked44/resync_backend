from services.v7_explain import STRICT_CORRELATION_PROMPT, normalize_explanation

SCORES = {
    "objectives_alignment": 0.8, "methodology_fit": 0.7, "numeric_consistency": 0.6,
    "claim_support": 0.5, "overall_coherence": 0.4, "pair_weight": 0.9,
}


def test_normalize_keeps_exactly_four_keys_and_model_scores():
    raw = {
        "coherence_scores": {"overall": 0.99},
        "highlighted_inconsistencies": ["Quote"],
        "recommendations": "Fix it",
        "reference_list": ["Intro: sentence"],
        "research_type": "extra key",
    }
    result = normalize_explanation(raw, SCORES)
    assert set(result) == {"coherence_scores", "highlighted_inconsistencies", "recommendations", "reference_list"}
    assert result["coherence_scores"] == {
        "overall": 0.4, "objectives": 0.8, "methodology": 0.7, "numeric": 0.6, "claims": 0.5,
    }
    assert result["recommendations"] == ["Fix it"]


def test_normalize_tolerates_garbage():
    result = normalize_explanation("not a dict", SCORES)
    assert result["highlighted_inconsistencies"] == [] and result["reference_list"] == []


def test_prompt_formats_without_leftover_placeholders():
    text = STRICT_CORRELATION_PROMPT.format(
        role_a="A", text_a="a", role_b="B", text_b="b", score_overall=0.4, score_obj=0.8,
        score_meth=0.7, score_num=0.6, score_claim=0.5, culprits_a="None", culprits_b="None",
    )
    assert '"overall": 0.4' in text and "{score" not in text
