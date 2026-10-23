import json
import pytest

from services.ml_client import MLClient
from services.scoring import (
    ROLE_PAIR_WEIGHTS,
    aggregate_role_texts,
    compute_cross_chapter_coherence,
    compute_structural_completeness,
)


def test_local_and_modal_batches_have_identical_scan_findings(monkeypatch):
    import modal_app

    base = (
        "The study measures how guided reading changes comprehension in students. "
        "Forty students completed a reading assessment before the lessons. "
        "Teachers delivered guided reading lessons over six weeks. "
        "The students completed the same assessment after the lessons. "
        "Scores improved and the study discusses the learning outcomes."
    )
    sections = {
        "Objectives": base,
        "Methodology": base,
        "Results": base.replace("Scores improved", "Assessment scores improved"),
        "Discussion": base.replace("Scores improved", "Reading scores improved"),
    }
    roles = {heading: heading.lower() for heading in sections}
    role_texts = aggregate_role_texts(sections, roles)
    role_pairs = [[a, b] for a, b, _ in ROLE_PAIR_WEIGHTS]

    local = MLClient("local")
    remote = MLClient("modal")
    calls = []

    def in_process_modal(method, *args):
        calls.append(method)
        body = getattr(modal_app, method)
        return json.loads(json.dumps(body(*json.loads(json.dumps(args)))))

    monkeypatch.setattr(remote, "_call", in_process_modal)

    def scan(client):
        ml = client.analyze_batch(sections, role_texts, role_pairs)
        structural = compute_structural_completeness(sections, roles)
        cross = compute_cross_chapter_coherence(ml["coherence"]["embeddings"], roles, sections)
        pair = "objectives::methodology"
        assert ml["v6_dimensions"][pair]["overall_coherence"] > 0.80
        evidence = client.explain_batch([[pair, role_texts["objectives"], role_texts["methodology"]]])
        findings = [{
            "pair": pair,
            "score": round(ml["v6_dimensions"][pair]["overall_coherence"] * 100, 2),
            "evidence_a": [item["sentence"] for item in evidence[pair]["evidence_a"]["top_supporting"]],
            "evidence_b": [item["sentence"] for item in evidence[pair]["evidence_b"]["top_supporting"]],
        }]
        return {
            "scores": (ml["coherence"]["overall_score"], structural.score, cross.score, ml["ai_text"]["overall_score"]),
            "v6_dimensions": ml["v6_dimensions"],
            "findings": findings,
            "evidence": evidence,
        }

    assert scan(local) == scan(remote)
    assert calls == ["analyze_batch", "explain_batch"]


def test_modal_error_is_visible(monkeypatch):
    import modal

    def fail(*args):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(modal.Cls, "from_name", fail)
    with pytest.raises(RuntimeError, match="Modal ML analyze_batch failed: unreachable"):
        MLClient("modal").analyze_batch({}, {}, [])
