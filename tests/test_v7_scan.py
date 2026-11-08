import asyncio
from services import v7_explain, gemini
from services.v7_scan import (manuscript_score, assemble_flow, band, four_part_score, explain_flow, merge_repeated_sections,
                              pair_pointer, select_worst_pairs)
import pytest
from services.v7_scan import apply_figure_conflicts, flatten_explanations
from services import v7_scan

@pytest.fixture(autouse=True)
def identity_calibration(monkeypatch):
    """These tests check the scoring structure on raw values; the real curve is tested in test_v7_calibration."""
    monkeypatch.setattr(v7_scan, "CALIBRATION", ((0.0, 0.0), (1.0, 1.0)))


@pytest.fixture(autouse=True)
def reviews_fall_back_to_single_pairs(monkeypatch):
    """Most tests fake the one-pair review. The batch calls answer nothing here, so every pair falls back to that fake;
    the batching tests at the end of this file set their own."""
    async def nothing(*_, **__):
        return {}
    monkeypatch.setattr(v7_explain, "explain_batch", nothing)
    monkeypatch.setattr(v7_explain, "suggest_bridges_batch", nothing)



def test_merge_literal_repeats_in_first_occurrence_order():
    sections = {"Related Studies": "first", "Limitations": "one", "Related Studies (2)": "second",
                "Background": "other", "Limitations (3)": "", "Rationale": "distinct"}
    assert merge_repeated_sections(sections, list(sections)) == [
        {"name": "Related Studies", "text": "first\nsecond"},
        {"name": "Limitations", "text": "one"},
        {"name": "Background", "text": "other"},
        {"name": "Rationale", "text": "distinct"},
    ]
    assert merge_repeated_sections({"Studies (2)": "a", "Studies": "b"}, ["Studies (2)", "Studies"]) == [
        {"name": "Studies (2)", "text": "a\nb"}]


def test_flow_score_dimensions_and_worst_twelve():
    sections = [{"name": str(i), "text": "words"} for i in range(11)]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": i / 10,
                       "objectives_alignment": i / 10, "methodology_fit": i / 10,
                       "numeric_consistency": i / 10, "claim_support": i / 10}}
           for i in range(10)]
    raw[0] = {"skipped": "too short"}
    flow = assemble_flow(sections, raw)
    assert flow[0]["scores"]["overall_coherence"] is None
    assert flow[0]["pair_weight"] is None
    assert flow[0]["skipped_reason"] == "too short"
    assert [p["order"] for p in select_worst_pairs(flow)] == list(range(2, 11))
    assert four_part_score(flow)[0] == 50.0
    assert four_part_score(flow)[1]["claim_support"] == 0.5
    assert pair_pointer(flow[1])["score"] == 10.0
    assert band(85) == "Strong" and band(70) == "Solid" and band(69.99) == "Needs Revision"
    assert four_part_score(assemble_flow(sections[:2], [{"skipped": "too short"}]))[0] == 0.0


def test_worst_pair_selection_at_boundaries():
    sections = [{"name": str(i), "text": "enough words for a valid section"} for i in range(15)]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": i / 10,
                       "objectives_alignment": 0.5, "methodology_fit": 0.5,
                       "numeric_consistency": 0.5, "claim_support": 0.5}} for i in range(14)]
    for n in (0, 1, 8, 9, 12, 13):
        flow = assemble_flow(sections[:n + 1], raw[:n])
        assert len(select_worst_pairs(flow)) == min(n, 12)


def test_pair_score_drives_ranking_and_skipped_pair_has_no_score():
    sections = [{"name": str(i), "text": "The result was 78% for n = 120."} for i in range(4)]
    raw = [
        {"scores": {"pair_weight": 0.9, "overall_coherence": 0.1,
                    "objectives_alignment": 0.8, "methodology_fit": 0.8,
                    "numeric_consistency": 0.8, "claim_support": 0.8}},
        {"scores": {"pair_weight": 0.1, "overall_coherence": 0.9,
                    "objectives_alignment": 0.2, "methodology_fit": 0.2,
                    "numeric_consistency": 0.2, "claim_support": 0.2}},
        {"skipped": "too short"},
    ]
    flow = assemble_flow(sections, raw)
    assert [p["order"] for p in select_worst_pairs(flow)] == [2, 1]
    assert flow[0]["pair_score"] == 0.8 and flow[0]["numeric_applicable"] is True
    assert flow[2]["pair_score"] is None and flow[2]["numeric_applicable"] is False
    assert pair_pointer(flow[1]) == {"order": 2, "section_a": "1", "section_b": "2",
                                     "overall_coherence": 0.9, "score": 20.0}


def test_explanation_cap_and_single_summary(monkeypatch):
    sections = [{"name": str(i), "text": "enough words for a valid section"} for i in range(31)]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": i / 40,
                       "objectives_alignment": i / 40, "methodology_fit": i / 40,
                       "numeric_consistency": i / 40, "claim_support": i / 40}} for i in range(30)]
    flow = assemble_flow(sections, raw)
    state = {"active": 0, "peak": 0, "summary_calls": 0}

    async def fake_pair(*_, **__):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return {"severity": "None", "failed_dimension": "none", "coherence_scores": {}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": []}

    async def fake_summary(*_):
        state["summary_calls"] += 1
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", fake_pair)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    findings, recommendations, synthesis = asyncio.run(explain_flow(sections, flow))
    # 30 weak pairs and no cap: every one is reviewed
    assert [p["order"] for p in flow if p["explanation"] is not None] == list(range(1, 31))
    assert 4 < state["peak"] <= v7_scan.EXPLAIN_CONCURRENCY and state["summary_calls"] == 1   # single reviews run side by side, within the limit
    assert findings == []
    assert recommendations == []  # no generic advice is invented for pairs Gemini found nothing in
    assert synthesis["summary"] is None and synthesis["strongest_pair"]["order"] == 30


def test_unfinished_rate_limited_review_is_partial(monkeypatch):
    sections = [{"name": "A", "text": "enough words for a valid section"},
                {"name": "B", "text": "enough words for a valid section"}]
    flow = assemble_flow(sections, [{"scores": {"pair_weight": 1, "overall_coherence": 0.2,
        "objectives_alignment": 0.2, "methodology_fit": 0.2,
        "numeric_consistency": 0.2, "claim_support": 0.2}}])

    async def unavailable(*_, **__):
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", unavailable)
    monkeypatch.setattr(v7_explain, "explain_summary", unavailable)
    monkeypatch.setattr(gemini, "rate_limited", lambda: True)
    _, _, synthesis = asyncio.run(explain_flow(sections, flow))
    status = synthesis["review_status"]
    assert (status["status"], status["unreviewed"], status["reason"]) == ("partial", 1, "rate_limited")
    assert status["message"] == "Some AI reviews could not finish because the AI's temporary limit was reached. This score is provisional; try scanning again later."
    # nothing was reviewed, so the score is an ML-only estimate and says so
    assert (status["analysis"], status["basis"], status["reviewed"], status["needs_review"]) == ("incomplete", "ml_estimate", 0, 1)
    assert status["unverified"] == 0


def pair_for_score(severity=None, dimension=None, numeric=True, order=1, overall=0.5):
    return {"order": order, "section_a": f"S{order}", "section_b": f"S{order + 1}", "severity": severity, "failed_dimension": dimension,
            "numeric_applicable": numeric, "scores": {key: overall for key in ("objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support", "overall_coherence")}}


def test_checks_are_counted_per_pair_and_category_and_each_category_is_a_percentage_of_checks_passed():
    pairs = [pair_for_score("Critical", "methodology_fit", order=1), pair_for_score("Moderate", "methodology_fit", order=2),
             pair_for_score("Minor", "claim_support", order=3), pair_for_score("None", "none", order=4), pair_for_score(None, None, order=5)]
    score, breakdown = manuscript_score(pairs)
    assert {name: entry["score"] for name, entry in breakdown.items()} == {
        "context_alignment": 100.0, "operational_alignment": 65.0, "evidence_consistency": 100.0, "logical_support": 90.0}
    assert score == 89.0   # (100 + 65 + 100 + 90) / 4 = 88.75, rounded up
    operational = breakdown["operational_alignment"]
    assert (operational["items"], operational["passed"], operational["issues"]) == (5, 3, 2)
    assert [(f["reason"], f["severity"]) for f in operational["failures"]] == [("contradiction", "Critical"), ("contradiction", "Moderate")]   # traceable
    assert manuscript_score([])[0] == 0.0


def test_the_overall_is_the_average_of_the_category_percentages_rounded_up():
    pairs = [pair_for_score("Critical", "objectives_alignment", order=1), pair_for_score("None", "none", order=2), pair_for_score("None", "none", order=3)]
    score, breakdown = manuscript_score(pairs)
    assert breakdown["context_alignment"]["score"] == 66.67 and breakdown["context_alignment"]["passed"] == 2
    assert score == 92.0   # (66.67 + 100 + 100 + 100) / 4 = 91.67, rounded up
    assert manuscript_score([pair_for_score("None", "none", order=1)])[0] == 100.0   # exactly 100 stays 100


def test_a_category_with_no_applicable_pair_is_na_and_left_out_of_the_average():
    pairs = [pair_for_score("Moderate", "claim_support", numeric=False, order=1), pair_for_score("None", "none", numeric=False, order=2)]
    score, breakdown = manuscript_score(pairs)
    assert breakdown["evidence_consistency"]["score"] == "N/A" and breakdown["evidence_consistency"]["failures"] == []
    assert breakdown["logical_support"]["score"] == 62.5
    assert score == 88.0   # (100 + 100 + 62.5) / 3 = 87.5, rounded up: figures are left out


def test_neighbors_fail_on_a_loose_link_but_far_apart_pairs_need_a_quoted_contradiction():
    neighbor = pair_for_score("None", "none", order=1, overall=0.4)
    far = dict(pair_for_score("None", "none", order=2, overall=0.4), adjacent=False)
    far_unreviewed = dict(pair_for_score(None, None, order=3, overall=0.4), adjacent=False)
    _, breakdown = manuscript_score([neighbor, far, far_unreviewed])
    context = breakdown["context_alignment"]
    assert (context["items"], context["passed"]) == (3, 1)   # only the AI-cleared far-apart pair passes
    assert [(f["order"], f["reason"]) for f in context["failures"]] == [(1, "loose_link"), (3, "loose_link")]
    assert context["failures"][0]["rating"] == 0.4 and context["issues"] == 0


def test_the_ai_reviews_own_grade_decides_the_contradiction_checks(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABCDE"]
    overall = [0.2, 0.5, 0.8, 0.3]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": o, "objectives_alignment": o, "methodology_fit": o,
                       "numeric_consistency": o, "claim_support": o}} for o in overall]
    flow = assemble_flow(sections, raw)
    verdicts = {"A": "Moderate", "B": "Critical", "C": "Minor", "D": "None"}

    async def fake_pair(role_a, *_, **__):
        return {"severity": verdicts[role_a], "failed_dimension": "none" if verdicts[role_a] == "None" else "claim_support",
                "coherence_scores": {}, "highlighted_inconsistencies": [], "recommendations": [], "reference_list": [], "bridges": []}

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", fake_pair)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    asyncio.run(explain_flow(sections, flow))
    assert [p["severity"] for p in flow] == ["Moderate", "Critical", "Minor", "None"]   # the review's grade stands
    score, breakdown = manuscript_score(flow)
    logical = breakdown["logical_support"]
    assert (logical["items"], logical["passed"], logical["issues"]) == (4, 0, 3)   # three contradictions and one loose link (rated 0.3)
    assert breakdown["context_alignment"]["score"] == 62.5   # the two low-rated neighbors (0.2 and 0.3) are loose links and keep only their ratings
    assert score == 51.0   # failed checks keep part of their credit; numeric is N/A for these texts


def test_failed_reviews_get_a_second_one_at_a_time_pass(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABC"]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": 0.5, "objectives_alignment": 0.5, "methodology_fit": 0.5,
                       "numeric_consistency": 0.5, "claim_support": 0.5}}] * 2
    flow = assemble_flow(sections, raw)
    calls = {"A": 0, "B": 0}

    async def flaky(role_a, *_, **__):
        calls[role_a] += 1
        failed = role_a == "A" and calls["A"] == 1     # pair A fails the first time only
        return {"severity": None if failed else "None", "failed_dimension": None if failed else "none",
                "coherence_scores": {}, "highlighted_inconsistencies": [], "recommendations": [], "reference_list": [], "bridges": []}

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", flaky)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    asyncio.run(explain_flow(sections, flow))
    assert calls == {"A": 2, "B": 1}                   # only the failed pair is retried
    assert [p["severity"] for p in flow] == ["None", "None"]


def test_a_pair_that_takes_too_long_is_left_unreviewed_without_holding_the_others(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABC"]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": 0.5, "objectives_alignment": 0.5, "methodology_fit": 0.5,
                       "numeric_consistency": 0.5, "claim_support": 0.5}}] * 2
    flow = assemble_flow(sections, raw)

    async def slow_for_a(role_a, *_, **__):
        if role_a == "A":
            await asyncio.sleep(5)
        return {"severity": "None", "failed_dimension": "none", "coherence_scores": {}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": [], "bridges": []}

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_scan, "EXPLAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(v7_explain, "explain_pair", slow_for_a)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    asyncio.run(explain_flow(sections, flow))
    assert [p["severity"] for p in flow] == [None, "None"]   # A unreviewed, B reviewed


def test_a_loosely_linked_neighbor_pair_gets_linking_sentences_even_when_the_main_review_wrote_none(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABCD"]
    ratings = [0.4, 0.8, 0.5]   # A-B loose, B-C well linked (never reviewed), C-D loose
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": o, "objectives_alignment": o, "methodology_fit": o,
                       "numeric_consistency": o, "claim_support": o}} for o in ratings]
    flow = assemble_flow(sections, raw)
    asked = []

    async def cleared_without_bridges(role_a, *_, **__):
        return {"severity": "None", "failed_dimension": "none", "coherence_scores": {}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": [], "bridges": []}

    async def fake_bridges(items):
        asked.append([(item["role_a"], item["role_b"]) for item in items])
        return {item["id"]: [{"section": item["role_a"], "placement": "end", "sentence": f"{item['role_a']} sets up what {item['role_b']} applies."},
                             {"section": item["role_b"], "placement": "start", "sentence": f"{item['role_b']} applies what {item['role_a']} sets up."}]
                for item in items}

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_explain, "explain_pair", cleared_without_bridges)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    monkeypatch.setattr(v7_explain, "suggest_bridges_batch", fake_bridges)
    _, recommendations, _ = asyncio.run(explain_flow(sections, flow))
    assert asked == [[("A", "B"), ("C", "D")]]                      # ONE call for both loose neighbor pairs, nothing for B-C
    assert [(r["pair_order"], r["kind"]) for r in recommendations] == [(1, "add"), (1, "add"), (3, "add"), (3, "add")]
    assert [p["severity"] for p in flow] == ["None", "None", "None"]   # a suggestion is never a penalty


def pair_with(overall, order=1):
    return {"order": order, "scores": {"overall_coherence": overall}}


def test_weak_pairs_are_grouped_by_severity_weakest_first():
    pairs = [pair_with(0.7, 1), pair_with(0.2, 2), pair_with(0.5, 3), pair_with(0.39, 4), pair_with(0.84, 5), pair_with(0.65, 6), pair_with(0.4, 7)]
    batches = v7_scan.severity_batches(pairs)
    assert [(tier, [p["order"] for p in group]) for tier, group in batches] == [
        ("Critical", [2, 4]), ("Moderate", [7, 3]), ("Minor", [6, 1, 5])]     # 0.40 is Moderate, 0.65 is Minor
    assert v7_scan.severity_batches([]) == []


def test_a_large_group_is_split_so_one_answer_never_grows_too_long(monkeypatch):
    monkeypatch.setattr(v7_scan, "BATCH_MAX", 3)
    batches = v7_scan.severity_batches([pair_with(0.1 + i / 100, i) for i in range(7)])
    assert [(tier, len(group)) for tier, group in batches] == [("Critical", 3), ("Critical", 3), ("Critical", 1)]


def test_one_gemini_call_per_severity_group_and_pairs_a_batch_skips_are_reviewed_alone(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABCDEFG"]
    ratings = [0.2, 0.3, 0.5, 0.55, 0.7, 0.75]   # two Critical, two Moderate, two Minor
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": o, "objectives_alignment": o, "methodology_fit": o,
                       "numeric_consistency": o, "claim_support": o}} for o in ratings]
    flow = assemble_flow(sections, raw)
    batch_calls, single_calls = [], []

    def explanation(severity):
        return {"severity": severity, "failed_dimension": "none", "coherence_scores": {}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": [], "bridges": [{"section": "x", "placement": "end", "sentence": "x"}] * 2}

    async def fake_batch(items):
        batch_calls.append([item["id"] for item in items])
        answered = [item for item in items if item["id"] != 4]     # the Gemini answer leaves pair 4 out
        return {item["id"]: explanation("None") for item in answered}

    async def fake_single(role_a, *_, **__):
        single_calls.append(role_a)
        return explanation("None")

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_explain, "explain_batch", fake_batch)
    monkeypatch.setattr(v7_explain, "explain_pair", fake_single)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    asyncio.run(explain_flow(sections, flow))
    assert batch_calls == [[1, 2], [3, 4], [5, 6]]       # three calls for six pairs: one per severity group
    assert single_calls == ["D"]                          # only pair 4 (D -> E) needed a call of its own
    assert all(p["explanation"] is not None and p["severity"] == "None" for p in flow)


def test_a_failed_batch_falls_back_to_reviewing_each_pair(monkeypatch):
    sections = [{"name": n, "text": "enough words for a valid section"} for n in "ABC"]
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": 0.5, "objectives_alignment": 0.5, "methodology_fit": 0.5,
                       "numeric_consistency": 0.5, "claim_support": 0.5}}] * 2
    flow = assemble_flow(sections, raw)
    calls = []

    async def failing_batch(items):
        raise RuntimeError("Gemini is down")

    async def fake_single(role_a, *_, **__):
        calls.append(role_a)
        return {"severity": "None", "failed_dimension": "none", "coherence_scores": {}, "highlighted_inconsistencies": [],
                "recommendations": [], "reference_list": [], "bridges": [{"section": "x", "placement": "end", "sentence": "x"}] * 2}

    async def fake_summary(*_):
        return None

    monkeypatch.setattr(v7_explain, "explain_batch", failing_batch)
    monkeypatch.setattr(v7_explain, "explain_pair", fake_single)
    monkeypatch.setattr(v7_explain, "explain_summary", fake_summary)
    asyncio.run(explain_flow(sections, flow))
    assert sorted(calls) == ["A", "B"] and [p["severity"] for p in flow] == ["None", "None"]


def test_a_cap_applies_only_when_one_is_set(monkeypatch):
    pairs = [{"order": n, "pair_score": n / 30, "scores": {"overall_coherence": n / 40}} for n in range(1, 31)]
    assert [p["order"] for p in v7_scan.select_pairs_to_explain(pairs)] == list(range(1, 31))   # all weak pairs, weakest first
    monkeypatch.setattr(v7_scan, "EXPLAIN_LIMIT", 5)
    assert [p["order"] for p in v7_scan.select_pairs_to_explain(pairs)] == [1, 2, 3, 4, 5]
    strong = [{"order": 99, "pair_score": 0.99, "scores": {"overall_coherence": 0.95}}]
    assert v7_scan.select_pairs_to_explain(strong) == []                                         # strong pairs are still never sent


def test_many_easy_far_apart_pairs_cannot_hide_weak_flow_between_neighbors():
    weak_flow = [pair_for_score("None", "none", order=n, overall=0.4) for n in (1, 2, 3)]                    # three loose neighbor links
    easy_far = [dict(pair_for_score("None", "none", order=n, overall=0.6), adjacent=False) for n in range(4, 14)]   # ten far pairs that pass
    score, breakdown = manuscript_score(weak_flow + easy_far)
    context = breakdown["context_alignment"]
    assert (context["flow"], context["cross"]) == ({"items": 3, "passed": 0, "credit": 1.2}, {"items": 10, "passed": 10, "credit": 10.0})
    assert context["halves"] is True and (context["items"], context["passed"]) == (13, 10)
    assert context["score"] == 70.0 and score == 70.0      # flow keeps only its weak ratings and cross is 100%; the two halves are averaged, not pooled
    only_cross = [dict(pair_for_score("None", "none", order=1, overall=0.6), adjacent=False)]
    assert manuscript_score(only_cross)[1]["context_alignment"]["score"] == 100.0                    # a single kind of check stands alone


def test_a_kind_with_too_few_checks_stays_balanced_but_is_marked_limited():
    """Changed rule: a kind with fewer than MIN_HALF_CHECKS is no longer pooled (pooling let the many chapter checks hide weak
    flow). It still counts for half, and the category says its evidence is limited."""
    one_miss_of_two = [pair_for_score("None", "none", order=1, overall=0.4), pair_for_score("None", "none", order=2, overall=0.6)]
    far = [dict(pair_for_score("None", "none", order=n, overall=0.6), adjacent=False) for n in (3, 4, 5)]
    category = manuscript_score(one_miss_of_two + far)[1]["context_alignment"]
    assert (category["flow"]["items"], category["cross"]["items"]) == (2, 3) and category["halves"] is True
    assert category["limited"] is True and category["basis"] == "balanced"
    assert category["score"] == 85.0      # flow: (0.4 + 1) / 2 = 0.7, chapters: 3 / 3 = 1; the average of the two rates


def test_a_category_with_only_one_kind_of_check_uses_that_kind_alone():
    flow_only = [pair_for_score("None", "none", order=n, overall=0.9) for n in (1, 2)]
    category = manuscript_score(flow_only)[1]["context_alignment"]
    assert category["basis"] == "flow_only" and category["halves"] is False and category["score"] == 100.0
    chapters_only = [dict(pair_for_score("None", "none", order=n, overall=0.9), adjacent=False) for n in (1, 2, 3)]
    assert manuscript_score(chapters_only)[1]["context_alignment"]["basis"] == "chapters_only"


def test_a_category_with_no_checks_is_not_applicable_and_the_overall_ignores_it():
    score, breakdown = manuscript_score([])
    assert score == 0.0 and all(entry["score"] == "N/A" for entry in breakdown.values())


def test_the_same_quoted_sentence_in_two_pairs_is_scored_once():
    def pair(order, quote):
        p = pair_for_score("Moderate", "objectives_alignment", order=order, overall=0.9)
        p["explanation"] = {"highlighted_inconsistencies": [{"section": "b", "quote": quote, "what": "w", "why": "y"}]}
        return p
    twice = manuscript_score([pair(1, "B says Y."), pair(2, "B  says y."), pair(3, "Another sentence.")])[1]["context_alignment"]
    assert twice["items"] == 2 and twice["issues"] == 2   # the repeated sentence is one issue; the other sentence is a second one
    once = manuscript_score([pair(1, "B says Y.")])[1]["context_alignment"]
    assert once["issues"] == 1


def test_the_overall_power_mean_lets_a_weak_category_pull_the_overall_down():
    from services.v7_scan import overall_from
    values = [87.86, 34.95, 81.25, 87.91]
    assert overall_from(values, 1) == 73.0                      # plain average: the strong categories hide the weak one
    assert overall_from(values, 0) < 73.0 and overall_from(values, -1) < overall_from(values, 0)
    assert overall_from([60.0, 60.0, 60.0, 60.0], -1) == 60.0   # equal categories: every rule agrees
    assert overall_from([80.0, 0.0], -1) == 0.0 and overall_from([], -1) == 0.0


# ---- context-aware headcounts, merged duplicates ----------------------------------------------------------------------
def _headcount_flow(text_a, text_b):
    sections = [{"name": "methodology", "text": text_a}, {"name": "results", "text": text_b}]
    flow = assemble_flow(sections, [{"scores": {"overall_coherence": 0.7, "objectives_alignment": 0.7, "methodology_fit": 0.7,
                                                 "numeric_consistency": 0.7, "claim_support": 0.7, "pair_weight": 1}}])
    return sections, flow


def test_a_smaller_count_explained_by_dropouts_is_not_flagged():
    sections, flow = _headcount_flow("We recruited 120 students. ", "Because of dropouts, 96 students were analysed.")
    apply_figure_conflicts(flow, sections)
    assert flow[0]["explanation"] is None


def test_an_unexplained_or_impossible_count_is_still_flagged():
    sections, flow = _headcount_flow("We recruited 96 students.", "A total of 120 students were analysed, with dropouts.")
    apply_figure_conflicts(flow, sections)
    assert flow[0]["explanation"]["highlighted_inconsistencies"]


def test_the_same_sentence_reported_by_two_pairs_gives_one_finding_and_one_recommendation():
    sections = [{"name": "a", "text": "A says X."}, {"name": "b", "text": "B says Y."}, {"name": "c", "text": "C says Z."}]
    scores = {"overall_coherence": 0.3, "objectives_alignment": 0.3, "methodology_fit": 0.3, "numeric_consistency": 0.3,
              "claim_support": 0.3, "pair_weight": 1}
    flow = assemble_flow(sections, [{"scores": dict(scores)}, {"scores": dict(scores)}])
    for pair, other in zip(flow, ("a", "c")):
        pair["explanation"] = {
            "severity": "Moderate", "failed_dimension": "claim_support", "coherence_scores": {}, "link_summary": "", "comparison": {},
            "highlighted_inconsistencies": [{"section": "b", "quote": "B says Y.", "what": "w", "why": "y"}],
            "recommendations": [{"section": "b", "original_sentence": "B says Y.", "recommended_revision": "B says Y, carefully.",
                                 "issue": "i", "category": "claims"}],
            "reference_list": [{"section": other, "sentence": f"{other.upper()} says."}], "bridges": []}
    findings, recommendations = flatten_explanations(flow, sections)
    assert len(findings) == 1 and len(recommendations) == 1
    assert len(findings[0]["evidence_quotes"]) >= 2


def test_the_points_shown_add_up_to_the_score():
    """Every check is worth one point; a pass earns 1 and a failed check earns its credit. The score is made of exactly those points."""
    def pair(order, adjacent, severity, rating):
        scores = {k: rating for k in ("overall_coherence", "objectives_alignment", "methodology_fit", "numeric_consistency", "claim_support")}
        return {"order": order, "section_a": "a", "section_b": "b", "adjacent": adjacent, "numeric_applicable": False, "scores": scores,
                "severity": severity, "failed_dimension": "objectives_alignment" if severity else None}
    flow = [pair(1, True, None, 0.9), pair(2, True, "Critical", 0.9), pair(3, True, "Minor", 0.9), pair(4, True, "Moderate", 0.9),
            pair(5, False, None, 0.9), pair(6, False, None, 0.9), pair(7, False, "Minor", 0.9)]
    _, breakdown = manuscript_score(flow)
    entry = breakdown["context_alignment"]
    assert entry["points"] == {"perfect": 3, "partial": 3, "zero": 1, "earned": 1.75 + 2.5, "total": 7}
    assert entry["flow"]["credit"] == 1 + 0 + 0.5 + 0.25 and entry["cross"]["credit"] == 2.5
    # two halves of 4 and 3 checks: the score is the average of the two rates, not points / checks
    assert entry["halves"] is True and entry["score"] == round((1.75 / 4 + 2.5 / 3) / 2 * 100, 2)


# ---- how complete the analysis was --------------------------------------------------------------------------------------
def test_analysis_quality_tells_complete_degraded_and_incomplete_scans_apart():
    from services.v7_scan import analysis_quality
    done = {"explanation": {"severity": "None"}}
    missing = {"explanation": None}
    assert analysis_quality([done, done], 0, True)["analysis"] == "complete"
    assert analysis_quality([done, done, missing], 1, True)["analysis"] == "degraded"
    incomplete = analysis_quality([done, missing, missing], 2, False)
    assert incomplete["analysis"] == "incomplete" and incomplete["basis"] == "ml_estimate" and incomplete["whole_paper_check"] == "skipped"
    assert analysis_quality([], 0, True)["analysis"] == "complete"


def test_an_unverified_finding_is_kept_apart_and_never_lowers_the_score():
    from services import v7_explain
    raw = {"severity": "Critical", "highlighted_inconsistencies": [
        {"section": "B", "quote": "text that is not in the paper", "what": "Conflict", "why": "They disagree."}]}
    scores = {"overall_coherence": 0.4, "objectives_alignment": 0.8, "methodology_fit": 0.7, "numeric_consistency": 0.6, "claim_support": 0.5}
    result = v7_explain.normalize_explanation(raw, scores, "A", "A text.", "B", "B text.")
    assert result["severity"] == "None" and result["highlighted_inconsistencies"] == []
    assert [item["what"] for item in result["unverified"]] == ["Conflict"]
