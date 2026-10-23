from services.xai_utils import explain_pair


def test_sentence_explanations():
    result = explain_pair(
        "The study surveyed two hundred participants. The team collected responses over six weeks. The researchers analyzed responses using regression.",
        "The survey reported a strong relationship. The sample included two hundred participants. Regression identified a significant association.",
    )
    assert set(result) == {"baseline", "evidence_a", "evidence_b"}
    assert isinstance(result["baseline"]["overall_coherence"], float)
    for side in ("evidence_a", "evidence_b"):
        assert set(result[side]) == {"top_supporting", "top_undermining"}
        for category in ("top_supporting", "top_undermining"):
            assert len(result[side][category]) <= 3
            for entry in result[side][category]:
                assert set(entry) == {"sentence", "delta"}
                assert isinstance(entry["sentence"], str)
                assert isinstance(entry["delta"], float)


def test_single_sentence_and_empty_side():
    text = "The survey included two hundred students and measured their learning outcomes."
    result = explain_pair(text, text)
    assert result["evidence_a"]["top_supporting"] == []
    empty = explain_pair("", text)
    assert empty["baseline"]["overall_coherence"] is None
    assert empty["evidence_b"]["top_undermining"] == []
