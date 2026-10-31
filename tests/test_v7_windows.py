import re

import torch

from services import v7_logic
from services.v7_logic import head_window, tail_window

SCORES = {key: 0.9 for key in v7_logic.SCORE_KEYS}


def numbered(count):
    return " ".join(f"Sentence number {i} has exactly eight words in it." for i in range(1, count + 1))


def test_head_and_tail_windows_use_whole_sentences_from_the_right_end():
    text = numbered(60)
    head, tail = head_window(text, 40), tail_window(text, 40)
    assert head.startswith("Sentence number 1 ") and "number 60" not in head and head.endswith("in it.")
    assert tail.endswith("Sentence number 60 has exactly eight words in it.") and tail.startswith("Sentence number ")
    assert len(head.split()) <= 40 and len(tail.split()) <= 40


def test_short_sections_are_used_whole_and_a_huge_sentence_keeps_its_end():
    short = "One short section. It has two sentences."
    assert head_window(short) == short and tail_window(short) == short
    huge = " ".join(f"w{i}" for i in range(500)) + "."
    assert tail_window(huge, 50).split()[-1] == "w499." and len(tail_window(huge, 50).split()) == 50


def stub_model(monkeypatch, embedded=None):
    """Replaces the neural model: a text's encoding is its section number, so a pair rates lower the further apart it is."""
    embedded = embedded if embedded is not None else []

    def fake_embed(model, texts):
        embedded.append(list(texts))
        return torch.tensor([[float(re.search(r"S(\d+)", text).group(1)) if re.search(r"S(\d+)", text) else 0.0] for text in texts])

    def fake_scores(model, embed_a, embed_b):
        return [dict(SCORES, overall_coherence=0.9 - 0.1 * abs(float(a[0]) - float(b[0]))) for a, b in zip(embed_a, embed_b)]

    monkeypatch.setattr(v7_logic, "get_model", lambda: object())
    monkeypatch.setattr(v7_logic, "_embed", fake_embed)
    monkeypatch.setattr(v7_logic, "_score_embeddings", fake_scores)
    return embedded


def sections(roles):
    return [{"name": f"S{i}", "text": f"S{i} " + numbered(5), "role": role} for i, role in enumerate(roles)]


def test_every_pair_is_rated_and_neighbors_use_the_end_of_the_first_section_and_the_start_of_the_next(monkeypatch):
    embedded = stub_model(monkeypatch)
    two = [{"name": "S1", "text": "S1 " + numbered(60)}, {"name": "S2", "text": "S2 " + numbered(60).replace("Sentence", "Clause")}]
    result = v7_logic.analyze_manuscript(two)
    heads, tails = embedded
    assert heads[0].startswith("S1 Sentence number 1 ") and "number 60" not in heads[0]
    assert tails[0].endswith("Sentence number 60 has exactly eight words in it.") and "number 1 " not in tails[0]
    assert heads[1].startswith("S2 Clause number 1 ")
    assert result["pairs_considered"] == 1 and result["pairs"][0]["adjacent"] is True
    assert (result["pairs"][0]["index_a"], result["pairs"][0]["index_b"]) == (0, 1)


def test_the_balanced_selection_keeps_weak_transitions_and_cross_chapter_pairs_that_should_agree(monkeypatch):
    stub_model(monkeypatch)
    monkeypatch.setattr(v7_logic, "HOLISTIC_PAIR_LIMIT", 4)
    monkeypatch.setattr(v7_logic, "NEIGHBOR_SLOTS", 2)
    monkeypatch.setattr(v7_logic, "HOLISTIC_SELECTION", "balanced")
    paper = sections(["objectives", "methodology", "methodology", "results", "conclusion"])
    kept = v7_logic.analyze_manuscript(paper)
    assert kept["pairs_considered"] == 10                                    # itertools.combinations of 5 sections
    # 2 neighbor transitions, then the 2 lowest-rated cross-chapter pairs (objectives vs results / conclusion)
    assert [(p["index_a"], p["index_b"]) for p in kept["pairs"]] == [(0, 1), (0, 3), (0, 4), (1, 2)]
    assert [p["adjacent"] for p in kept["pairs"]] == [True, False, False, True]


def test_the_lowest_setting_keeps_the_lowest_rated_pairs_overall_as_the_adviser_specified(monkeypatch):
    stub_model(monkeypatch)
    monkeypatch.setattr(v7_logic, "HOLISTIC_PAIR_LIMIT", 4)
    monkeypatch.setattr(v7_logic, "HOLISTIC_SELECTION", "lowest")
    kept = v7_logic.analyze_manuscript(sections(["objectives", "methodology", "methodology", "results", "conclusion"]))
    ratings = sorted(p["scores"]["overall_coherence"] for p in kept["pairs"])
    assert len(kept["pairs"]) == 4 and ratings == sorted(ratings)[:4] and max(ratings) <= 0.7 + 1e-9
    assert not any(p["adjacent"] for p in kept["pairs"])                     # neighbors rate higher, so none are kept


def test_without_section_roles_every_far_pair_is_a_candidate_and_short_sections_are_skipped(monkeypatch):
    stub_model(monkeypatch)
    monkeypatch.setattr(v7_logic, "HOLISTIC_PAIR_LIMIT", 3)
    monkeypatch.setattr(v7_logic, "NEIGHBOR_SLOTS", 1)
    paper = sections([None, None, None, None]) + [{"name": "S9", "text": "too short"}]
    kept = v7_logic.analyze_manuscript(paper)
    assert kept["pairs_considered"] == 6 and len(kept["pairs"]) == 3          # the 5th section has under 5 words


def test_transition_averages_cover_every_neighbor_pair_and_count_figures_only_where_both_sections_have_them(monkeypatch):
    stub_model(monkeypatch)
    monkeypatch.setattr(v7_logic, "HOLISTIC_PAIR_LIMIT", 1)     # only one pair is kept, but the average uses all neighbors
    plain = lambda i: {"name": f"S{i}", "text": f"S{i} alpha beta gamma delta epsilon zeta.", "role": None}
    figures = lambda i: {"name": f"S{i}", "text": f"S{i} The sample was 120 students and 45 percent passed.", "role": None}
    result = v7_logic.analyze_manuscript([plain(0), plain(1), figures(2), figures(3)])
    averages = result["transition_averages"]
    assert len(result["pairs"]) == 1 and result["pairs_considered"] == 6
    assert abs(averages["overall_coherence"] - 0.8) < 1e-6        # three neighbor transitions, each rated 0.8
    assert abs(averages["numeric_consistency"] - 0.9) < 1e-6      # only S2->S3 has figures on both sides (the stub rates it 0.9)
    only_plain = v7_logic.analyze_manuscript([plain(0), plain(1), plain(2)])["transition_averages"]
    assert only_plain["numeric_consistency"] is None              # no figures anywhere: nothing to measure
