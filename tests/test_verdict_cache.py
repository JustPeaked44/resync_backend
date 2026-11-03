"""The AI's verdict on a pair is remembered, so scanning the same text again always gives the same score."""

import asyncio
from types import SimpleNamespace

import pytest

from services import v7_explain, v7_scan, verdict_cache
from services.v7_scan import assemble_flow, explain_flow, manuscript_score
from services.verdict_cache import VerdictCache


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("VERDICT_CACHE", "on")


class FakeTable:
    def __init__(self, rows):
        self.rows, self.wanted, self.written = rows, None, None

    def select(self, *_):
        return self

    def in_(self, column, values):
        self.wanted = (column, set(values))
        return self

    def upsert(self, rows, on_conflict=None):
        self.written = (rows, on_conflict)
        return self

    def execute(self):
        if self.written:
            rows, key = self.written
            kept = {row[key]: row for row in self.rows}
            kept.update({row[key]: row for row in rows})
            self.rows[:] = list(kept.values())
            return SimpleNamespace(data=rows)
        column, values = self.wanted
        return SimpleNamespace(data=[row for row in self.rows if row[column] in values])


class FakeClient:
    def __init__(self):
        self.rows = []

    def table(self, name):
        assert name == verdict_cache.TABLE
        return FakeTable(self.rows)


def verdict(severity="None", **extra):
    return {"severity": severity, "failed_dimension": "none" if severity == "None" else "claim_support", "coherence_scores": {},
            "link_summary": "", "comparison": {"a_says": "", "b_says": ""}, "highlighted_inconsistencies": [],
            "recommendations": [], "reference_list": [], "bridges": [], **extra}


# ---- the cache on its own ---------------------------------------------------------------------------------------

def test_the_fingerprint_is_the_same_for_the_same_input_and_changes_with_any_part_of_it():
    base = VerdictCache.fingerprint("A", "B", True, "text a", "text b", {"overall": 0.5})
    assert base == VerdictCache.fingerprint("A", "B", True, "text a", "text b", {"overall": 0.5})
    for changed in (("A", "B", True, "text a!", "text b", {"overall": 0.5}), ("A", "B", False, "text a", "text b", {"overall": 0.5}),
                    ("A", "B", True, "text a", "text b", {"overall": 0.51}), ("B", "A", True, "text a", "text b", {"overall": 0.5})):
        assert VerdictCache.fingerprint(*changed) != base


def test_a_new_prompt_version_makes_old_verdicts_stop_matching(monkeypatch):
    before = VerdictCache.fingerprint("A", "B", True, "a", "b")
    monkeypatch.setattr(verdict_cache, "PROMPT_VERSION", "next-version")
    assert VerdictCache.fingerprint("A", "B", True, "a", "b") != before


def test_saved_verdicts_come_back_from_the_database_and_incomplete_ones_are_never_saved(monkeypatch, on):
    client = FakeClient()
    monkeypatch.setattr(verdict_cache.DatabasePersistenceService, "get_client", lambda: client)
    asyncio.run(VerdictCache.save_many({"k1": verdict("Moderate"), "k2": verdict(None), "k3": verdict("None")}))
    assert sorted(row["cache_key"] for row in client.rows) == ["k1", "k3"]          # k2 was a pair the AI could not judge
    VerdictCache.forget_everything_in_memory()                                      # a restarted server: only the database remembers
    found = asyncio.run(VerdictCache.get_many(["k1", "k2", "k3", "k4"]))
    assert set(found) == {"k1", "k3"} and found["k1"]["severity"] == "Moderate"


def test_without_a_database_verdicts_are_still_remembered_by_the_running_server(monkeypatch, on):
    def broken():
        raise RuntimeError("relation pair_verdict_cache does not exist")
    monkeypatch.setattr(verdict_cache.DatabasePersistenceService, "get_client", broken)
    asyncio.run(VerdictCache.save_many({"k": verdict("Minor")}))
    assert asyncio.run(VerdictCache.get_many(["k"]))["k"]["severity"] == "Minor"
    found = asyncio.run(VerdictCache.get_many(["k"]))["k"]
    found["severity"] = "Critical"                                                    # changing a copy never changes what is remembered
    assert asyncio.run(VerdictCache.get_many(["k"]))["k"]["severity"] == "Minor"


def test_the_switch_turns_remembering_off(monkeypatch):
    monkeypatch.setenv("VERDICT_CACHE", "off")
    asyncio.run(VerdictCache.save_many({"k": verdict("Minor")}))
    assert asyncio.run(VerdictCache.get_many(["k"])) == {}


# ---- the review uses it -----------------------------------------------------------------------------------------

def paper(texts):
    sections = [{"name": name, "text": text} for name, text in zip("ABCD", texts)]
    ratings = [0.3, 0.55, 0.7]       # one Critical, one Moderate, one Minor pair
    raw = [{"scores": {"pair_weight": 1, "overall_coherence": o, "objectives_alignment": o, "methodology_fit": o,
                       "numeric_consistency": o, "claim_support": o}} for o in ratings]
    return sections, assemble_flow(sections, raw)


@pytest.fixture
def ai(monkeypatch, on):
    """A fake AI that records which pairs it was asked about. Its verdict depends only on the text it is shown."""
    monkeypatch.setattr(verdict_cache.DatabasePersistenceService, "get_client", lambda: FakeClient())
    asked = []

    async def batch(items):
        asked.append(sorted(item["id"] for item in items))
        return {item["id"]: verdict("Moderate" if "bad" in item["text_a"] + item["text_b"] else "None",
                                    bridges=[{"section": "x", "placement": "end", "sentence": "x"}] * 2) for item in items}

    async def none(*_, **__):
        return {}

    async def summary(*_):
        return None

    monkeypatch.setattr(v7_explain, "explain_batch", batch)
    monkeypatch.setattr(v7_explain, "suggest_bridges_batch", none)
    monkeypatch.setattr(v7_explain, "explain_summary", summary)
    return SimpleNamespace(asked=asked, batch=batch)


def severities(flow):
    return [p["severity"] for p in flow]


def test_scanning_the_same_text_again_asks_the_ai_nothing_and_gives_the_same_score(ai):
    texts = ["alpha words here", "beta bad words here", "gamma words here", "delta words here"]
    sections, flow = paper(texts)
    asyncio.run(explain_flow(sections, flow))
    first = (severities(flow), manuscript_score(flow)[0])
    assert first[0] == ["Moderate", "Moderate", "None"]       # B holds the bad text, so both pairs that include it are judged Moderate
    assert ai.asked == [[1], [2], [3], [1], [2]]               # one call per severity group, then a second look at the two flagged pairs
    sections2, flow2 = paper(texts)
    asyncio.run(explain_flow(sections2, flow2))
    assert len(ai.asked) == 5                                   # nothing was asked a second time
    assert (severities(flow2), manuscript_score(flow2)[0]) == first


def test_only_the_pair_whose_text_changed_is_asked_again(ai):
    sections, flow = paper(["alpha words here", "beta words here", "gamma words here", "delta words here"])
    asyncio.run(explain_flow(sections, flow))
    ai.asked.clear()
    # section C changes: it belongs to pairs 2 (B-C) and 3 (C-D); pair 1 (A-B) is untouched and keeps its verdict
    sections2, flow2 = paper(["alpha words here", "beta words here", "gamma bad words here", "delta words here"])
    asyncio.run(explain_flow(sections2, flow2))
    assert set(order for call in ai.asked for order in call) == {2, 3}   # pair 1 (A-B) keeps its saved verdict
    assert severities(flow2) == ["None", "Moderate", "Moderate"]


def test_a_pair_the_ai_could_not_judge_is_asked_again_next_time(monkeypatch, ai):
    texts = ["alpha words here", "beta words here", "gamma words here", "delta words here"]

    async def nothing(items):
        return {}

    async def cannot_judge(*_, **__):
        return verdict(None)                                   # the AI failed on every single review too

    monkeypatch.setattr(v7_explain, "explain_batch", nothing)
    monkeypatch.setattr(v7_explain, "explain_pair", cannot_judge)
    sections, flow = paper(texts)
    asyncio.run(explain_flow(sections, flow))
    assert all(severity is None for severity in severities(flow))
    monkeypatch.setattr(v7_explain, "explain_batch", ai.batch)   # the AI works again
    sections2, flow2 = paper(texts)
    asyncio.run(explain_flow(sections2, flow2))
    assert ai.asked == [[1], [2], [3]] and severities(flow2) == ["None", "None", "None"]   # nothing unreviewed was remembered


def test_a_loosely_linked_neighbor_pair_with_only_one_linking_sentence_is_completed_to_two(monkeypatch, ai):
    sections, flow = paper(["alpha words here", "beta words here", "gamma words here", "delta words here"])
    asked_for = []

    async def one_bridge(items):
        return {item["id"]: verdict("None", bridges=[{"section": item["role_a"], "placement": "end", "sentence": "only one"}]) for item in items}

    async def completes(items):
        asked_for.extend(item["id"] for item in items)
        return {item["id"]: [{"section": item["role_a"], "placement": "end", "sentence": "first"},
                             {"section": item["role_b"], "placement": "start", "sentence": "second"}] for item in items}

    monkeypatch.setattr(v7_explain, "explain_batch", one_bridge)
    monkeypatch.setattr(v7_explain, "suggest_bridges_batch", completes)
    asyncio.run(explain_flow(sections, flow))
    assert sorted(asked_for) == [1, 2]                         # the two pairs rated under 0.65; the 0.7 pair needs none
    assert [len(p["explanation"]["bridges"]) for p in flow[:2]] == [2, 2]


def test_a_contradiction_only_counts_when_a_second_review_agrees(monkeypatch, ai):
    sections, flow = paper(["alpha words here", "beta words here", "gamma words here", "delta words here"])
    calls = {"n": 0}

    async def flips(items):
        calls["n"] += 1
        first_look = calls["n"] <= 3                            # the three severity groups; later calls are second looks
        return {item["id"]: verdict("Moderate" if first_look or item["id"] == 2 else "None",
                                    bridges=[{"section": "x", "placement": "end", "sentence": "x"}] * 2) for item in items}

    monkeypatch.setattr(v7_explain, "explain_batch", flips)
    asyncio.run(explain_flow(sections, flow))
    # all three pairs were flagged first; the second look confirmed only pair 2
    assert severities(flow) == ["None", "Moderate", "None"]


def test_when_the_second_review_cannot_run_the_first_verdict_stands(monkeypatch, ai):
    sections, flow = paper(["alpha words here", "beta words here", "gamma words here", "delta words here"])
    calls = {"n": 0}

    async def first_only(items):
        calls["n"] += 1
        return {item["id"]: verdict("Minor") for item in items} if calls["n"] <= 3 else {}

    monkeypatch.setattr(v7_explain, "explain_batch", first_only)
    asyncio.run(explain_flow(sections, flow))
    assert severities(flow) == ["Minor", "Minor", "Minor"]       # unconfirmed is not denied


def test_pairs_missing_linking_sentences_are_asked_again_until_they_have_both(monkeypatch, ai):
    sections, flow = paper(["alpha words here", "beta words here", "gamma words here", "delta words here"])
    rounds = []

    async def no_bridges(items):
        return {item["id"]: verdict("None") for item in items}

    async def partial(items):
        rounds.append(sorted(item["id"] for item in items))
        done = {} if len(rounds) == 1 else {item["id"]: [{"section": item["role_a"], "placement": "end", "sentence": "first"},
                                                         {"section": item["role_b"], "placement": "start", "sentence": "second"}] for item in items}
        return dict(list(done.items())[:1]) if len(rounds) == 2 and len(done) > 1 else done   # round 2 only covers one pair

    monkeypatch.setattr(v7_explain, "explain_batch", no_bridges)
    monkeypatch.setattr(v7_explain, "suggest_bridges_batch", partial)
    asyncio.run(explain_flow(sections, flow))
    assert rounds == [[1, 2], [1, 2], [2]]                     # each round asks only for the pairs still short
    assert [len(p["explanation"]["bridges"]) for p in flow[:2]] == [2, 2]
