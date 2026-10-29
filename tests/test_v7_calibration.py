import pytest

from services.v7_scan import calibrate, final_score


def pair(overall):
    return {"scores": {"overall_coherence": overall, "objectives_alignment": overall, "methodology_fit": overall,
                       "numeric_consistency": overall, "claim_support": overall}, "numeric_applicable": True}


def test_calibration_curve_anchors_and_monotonic():
    assert calibrate(0.0) == 0.0 and calibrate(0.15) == 0.0 and calibrate(0.9) == 1.0
    assert calibrate(0.45) == pytest.approx(0.60) and calibrate(0.65) == pytest.approx(0.86)
    values = [calibrate(x / 100) for x in range(0, 101)]
    assert values == sorted(values)


def test_typical_writing_scores_well_and_unrelated_text_does_not():
    assert final_score([pair(0.65)] * 5)[0] == 86.0
    assert final_score([pair(0.84)] * 5)[0] == 100.0
    assert final_score([pair(0.45)] * 5)[0] == 60.0
    assert final_score([pair(0.1)] * 5)[0] == 0.0
