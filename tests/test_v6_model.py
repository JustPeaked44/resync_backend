from services.embedding import predict_coherence


KEYS = {
    "objectives_alignment", "methodology_fit", "numeric_consistency",
    "claim_support", "overall_coherence",
}


def test_real_v6_prediction():
    result = predict_coherence(
        "The study surveys two hundred university students about sleep quality.",
        "The questionnaire measures reported sleep quality among university students.",
    )
    assert set(result) == KEYS
    assert all(isinstance(value, float) and 0.0 <= value <= 1.0 for value in result.values())


def test_near_empty_input():
    assert predict_coherence("four words are here", "This side has plenty of meaningful words.") == dict.fromkeys(KEYS)
    assert predict_coherence("", "This side has plenty of meaningful words.") == dict.fromkeys(KEYS)


def test_long_input_truncates():
    result = predict_coherence("The study measures learning outcomes. " * 150, "The outcomes improved during the study. " * 150)
    assert all(isinstance(value, float) for value in result.values())
