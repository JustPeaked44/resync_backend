from services.v7_scan import four_part_score, has_figures, numeric_applicable


def pair(objectives, methodology, claims, numeric, applicable, overall=0.1, weight=0.9):
    return {"scores": {"objectives_alignment": objectives, "methodology_fit": methodology,
                       "claim_support": claims, "numeric_consistency": numeric,
                       "overall_coherence": overall}, "numeric_applicable": applicable,
            "pair_weight": weight}


def test_four_equal_parts_at_full_score():
    score, averages, weights = four_part_score([pair(1, 1, 1, 1, True)])
    assert score == 100.0
    assert averages == dict.fromkeys(averages, 1.0)
    assert set(weights.values()) == {0.25}


def test_numeric_dropped_and_mixed_values():
    score, averages, weights = four_part_score([pair(0.9, 0.6, 0.3, 0.1, False)])
    assert score == 60.0  # (0.9 + 0.6 + 0.3) / 3 * 100
    assert averages["numeric_consistency"] is None
    assert weights["numeric_consistency"] == 0.0
    assert all(weights[key] == 1 / 3 for key in ("objectives_alignment", "methodology_fit", "claim_support"))

    score, averages, weights = four_part_score([
        pair(1, 0.8, 0.6, 0.4, True), pair(0.4, 0.2, 0.8, 0.6, False)])
    assert averages == {"objectives_alignment": 0.7, "methodology_fit": 0.5,
                        "numeric_consistency": 0.4, "claim_support": 0.7}
    assert set(weights.values()) == {0.25}
    assert score == 57.5  # (0.7 + 0.5 + 0.4 + 0.7) / 4 * 100


def test_no_numeric_pairs_and_no_scored_pairs():
    assert four_part_score([pair(1, 0, 0.5, 1, False)])[1]["numeric_consistency"] is None
    score, averages, weights = four_part_score([{"scores": {"overall_coherence": None}}])
    assert score == 0.0
    assert all(value is None for value in averages.values())
    assert set(weights.values()) == {0.0}


def test_numeric_text_rule_and_non_figures():
    labels = "(Smith, 2024) et al., 2019 Figure 2 Table 20.1 Chapter 3 Section 4 page 12\n1. item\n23\nhttps://example.com/2024/45 doi:10.1234/5678"
    assert not has_figures(labels)
    assert has_figures("The sample had n = 120 participants.")
    assert has_figures("Accuracy reached 78%.")
    assert has_figures("The value was 20.1.")
    assert not numeric_applicable("n = 120", "No measured values.", "both")
    assert numeric_applicable("n = 120", "No measured values.", "either")
