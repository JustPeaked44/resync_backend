"""Two sections that report different headcounts are flagged with a quote and a one-sentence fix."""

import asyncio

from services import v7_explain
from services.v7_scan import assemble_flow, explain_flow, figure_conflict, manuscript_score


def test_different_headcounts_are_a_conflict():
    found = figure_conflict("Methods. In total, 120 respondents answered the survey.",
                            "Results. Of the 300 respondents who answered, most agreed.")
    assert found[0] == "respondents" and (found[1], found[3]) == (120, 300)


def test_the_same_headcount_and_percentages_are_not():
    assert figure_conflict("We surveyed 120 respondents.", "All 120 respondents answered.") is None
    assert figure_conflict("We surveyed 120 respondents.", "Of 95% of respondents who agreed.") is None


def test_a_section_with_several_counts_is_left_alone():
    assert figure_conflict("We piloted with 20 respondents and surveyed 300 respondents.", "A total of 300 respondents.") is None


def test_a_pilot_group_is_not_the_main_sample():
    assert figure_conflict("The pilot survey involved 33 respondents.", "In total, 120 respondents answered the survey.") is None


def test_different_kinds_of_people_do_not_clash():
    assert figure_conflict("We interviewed 12 experts.", "We surveyed 300 respondents.") is None


def test_years_and_small_numbers_are_ignored():
    assert figure_conflict("In 2024 users liked it. 5 users tested it.", "In 2025 users liked it. 7 users tested it.") is None


def make_flow(texts):
    sections = [{"name": name, "text": text, "role": role} for name, text, role in zip(
        ("research methodology", "results", "conclusion"), texts, ("methodology", "results", "conclusion"))]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": 0.9, "objectives_alignment": 0.9, "methodology_fit": 0.9,
                       "numeric_consistency": 0.9, "claim_support": 0.9}}] * 2
    return sections, assemble_flow(sections, raw)


def test_a_planted_headcount_conflict_becomes_a_finding_a_fix_and_a_lower_evidence_score(monkeypatch):
    async def nothing(*_, **__):
        return {}

    async def no_summary(*_):
        return None
    monkeypatch.setattr(v7_explain, "explain_batch", nothing)
    monkeypatch.setattr(v7_explain, "suggest_bridges_batch", nothing)
    monkeypatch.setattr(v7_explain, "explain_summary", no_summary)
    sections, flow = make_flow(["We chose a survey design. In total, 120 respondents answered the survey.",
                                "Of the 300 respondents who answered the survey, most agreed. Scores were high.",
                                "The study is done."])
    before = manuscript_score(flow)[1]["evidence_consistency"]["score"]
    findings, recs, _ = asyncio.run(explain_flow(sections, flow))
    assert any("respondents" in f["explanation_what"] for f in findings)
    quote = next(f["evidence_b"] for f in findings if "respondents" in f["explanation_what"])
    assert quote in sections[1]["text"]
    fix = next(r for r in recs if r["kind"] == "edit")
    assert fix["original_sentence"] in sections[1]["text"]
    assert "120 respondents" in fix["recommended_revision"] and "300" not in fix["recommended_revision"]
    after = manuscript_score(flow)[1]["evidence_consistency"]["score"]
    assert after is not None and (before is None or after < before)
