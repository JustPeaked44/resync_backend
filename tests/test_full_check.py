import asyncio

from services import full_check, v7_explain
from services.v7_scan import assemble_flow

_real_sleep = asyncio.sleep


async def _no_pause(*_):
    await _real_sleep(0)


FILLER = "The students answered the questionnaire during class hours and handed it back the same day. " * 60
METHOD_SENTENCE = "The team developed the system using the Waterfall model with fixed phases."
CONCLUSION_SENTENCE = "The team developed the system using the Agile model with short sprints."


def _sections():
    return [
        {"name": "introduction", "text": "This study builds a manuscript checker for capstone students. " * 20},
        # the statement sits in the MIDDLE of a long section, where the pair review never looks
        {"name": "methodology", "text": FILLER + METHOD_SENTENCE + " " + FILLER},
        {"name": "conclusion", "text": "The checker works well for the tested papers. " * 10 + CONCLUSION_SENTENCE},
    ]


def _fake_ai(monkeypatch, genuine=True, misnamed=False):
    prompts = []

    async def generate(prompt, max_tokens=None):
        prompts.append(prompt)
        if "ONE part of a student's thesis" in prompt:
            if "Waterfall" in prompt:
                return {"facts": [{"kind": "method", "statement": "built with the Waterfall model", "quote": METHOD_SENTENCE},
                                  {"kind": "claim", "statement": "invented", "quote": "A sentence that is not in the text at all."}]}
            if "Agile" in prompt:
                return {"facts": [{"kind": "method", "statement": "built with the Agile model", "quote": CONCLUSION_SENTENCE}]}
            return {"facts": []}
        if "STATEMENTS:" in prompt:
            ids = {line.split(" | ")[3]: line.split(" | ")[0] for line in prompt.split("STATEMENTS:")[1].strip().splitlines()}
            if misnamed:   # the AI named its title field wrongly and left the reason out
                return {"conflicts": [{"fact_a": ids["built with the Waterfall model"], "fact_b": ids["built with the Agile model"],
                                       "severity": "Moderate", "category": "methodology", "claims": "Different development model"}]}
            return {"conflicts": [{"fact_a": ids["built with the Waterfall model"], "fact_b": ids["built with the Agile model"],
                                   "severity": "Moderate", "category": "methodology", "what": "Different development model",
                                   "why": f"Statement {ids['built with the Waterfall model']} says Waterfall but statement {ids['built with the Agile model']} says Agile.", "fix_id": ids["built with the Agile model"]}]}
        return {"results": [{"id": 1, "genuine": genuine, "severity": "Moderate",
                             "fixed_sentence": "The team developed the system using the Waterfall model with fixed phases."}]}

    monkeypatch.setattr(v7_explain, "_generate", generate)
    return prompts


def _flow(sections):
    scores = {"objectives_alignment": 0.9, "methodology_fit": 0.9, "numeric_consistency": 0.9, "claim_support": 0.9,
              "overall_coherence": 0.9, "pair_weight": 1.0}
    return assemble_flow(sections, [{"index_a": 0, "index_b": 1, "scores": dict(scores), "culprits_a": [], "culprits_b": []}])


def test_a_statement_buried_in_a_long_section_is_compared_with_a_far_section(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    prompts = _fake_ai(monkeypatch)
    sections, flow = _sections(), None
    flow = _flow(sections)
    added = asyncio.run(full_check.apply_full_check(flow, sections))
    assert added == 1
    pair = next(p for p in flow if (p["index_a"], p["index_b"]) == (1, 2))   # the pair the model had not kept was created
    explanation = pair["explanation"]
    assert explanation["severity"] == "Moderate" and explanation["failed_dimension"] == "methodology_fit"
    quotes = {item["quote"] for item in explanation["highlighted_inconsistencies"]}
    assert quotes == {METHOD_SENTENCE, CONCLUSION_SENTENCE}               # both sides are real sentences from the paper
    why = explanation["highlighted_inconsistencies"][0]["why"]
    assert "F1" not in why and "F2" not in why and "methodology section" in why   # section names, never ids
    fix = explanation["recommendations"][0]
    assert fix["original_sentence"] == CONCLUSION_SENTENCE and "Waterfall" in fix["recommended_revision"]
    assert [p["order"] for p in flow] == [1, 2]                           # still in reading order
    assert any("Waterfall" in prompt for prompt in prompts)               # the whole long section was read, not just its ends


def test_an_unconfirmed_finding_is_not_reported(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    _fake_ai(monkeypatch, genuine=False)
    sections = _sections()
    flow = _flow(sections)
    assert asyncio.run(full_check.apply_full_check(flow, sections)) == 0
    assert len(flow) == 1


def test_a_failing_ai_never_breaks_the_scan(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")

    async def broken(prompt, max_tokens=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(v7_explain, "_generate", broken)
    monkeypatch.setattr(full_check.asyncio, "sleep", _no_pause)
    sections = _sections()
    flow = _flow(sections)
    assert asyncio.run(full_check.apply_full_check(flow, sections)) == 0


def test_long_sections_are_read_in_whole_pieces():
    text = "\n".join(f"Paragraph {n}. " + "word " * 400 for n in range(30))
    pieces = full_check.chunk_text(text, size=12000)
    assert len(pieces) > 1 and all(len(piece) <= 12500 for piece in pieces)
    assert " ".join(pieces).split() == text.split()


def test_a_piece_whose_answer_comes_back_broken_is_read_again_in_halves(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    sizes = []

    async def generate(prompt, max_tokens=None):
        text = prompt.split("TEXT:\n")[1]
        sizes.append(len(text))
        if len(text) > 7000:
            raise ValueError("broken JSON")
        return {"facts": []}

    monkeypatch.setattr(v7_explain, "_generate", generate)
    monkeypatch.setattr(full_check.asyncio, "sleep", _no_pause)
    sections = [{"name": "one", "text": "Alpha beta gamma delta. " * 400}, {"name": "two", "text": "Alpha beta gamma delta. " * 400}]
    asyncio.run(full_check.apply_full_check([], sections))
    assert any(size <= 7000 for size in sizes)     # the halves were asked after the whole piece failed


def test_a_conflict_with_a_misnamed_or_missing_title_is_still_checked_and_reported(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    _fake_ai(monkeypatch, misnamed=True)
    sections = _sections()
    flow = _flow(sections)
    assert asyncio.run(full_check.apply_full_check(flow, sections)) == 1


def test_a_check_started_early_gives_the_same_report_as_one_run_in_place(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    _fake_ai(monkeypatch)
    sections = _sections()
    flow_early, flow_late = _flow(sections), _flow(sections)

    async def early():
        task = full_check.start(sections)          # begun while other work is still going on
        await asyncio.sleep(0)
        return await full_check.apply_full_check(flow_early, sections, prepared=task)

    assert asyncio.run(early()) == asyncio.run(full_check.apply_full_check(flow_late, sections)) == 1
    assert flow_early[-1]["explanation"] == flow_late[-1]["explanation"]


def test_finding_fact_ids_keep_word_boundaries(monkeypatch):
    async def extract(name, text):
        return [{"statement": name, "quote": text}] * (2 if name == "first" else 1)

    async def cross_check(facts):
        return [{"fact_a": "F1", "fact_b": "F3", "what": "whereas F3 says", "why": "Statement F3 says the statement F3 (F3). F3. the  the F1."}]

    async def confirm(candidates):
        return {}

    monkeypatch.setattr(full_check, "_extract", extract)
    monkeypatch.setattr(full_check, "_cross_check", cross_check)
    monkeypatch.setattr(full_check, "_confirm", confirm)
    sections = [{"name": name, "text": ("word " * 100).strip()} for name in ("first", "second")]
    candidates, _ = asyncio.run(full_check._prepare(sections))
    assert candidates[0]["what"] == "whereas the second section says"
    assert candidates[0]["why"] == "the second section says the second section (the second section). the second section. the first section."


def test_compare_failure_reports_failed_without_changing_flow(monkeypatch):
    import copy
    monkeypatch.setenv("FULL_CHECK", "on")
    _fake_ai(monkeypatch)
    generate = v7_explain._generate

    async def fail_compare(prompt, max_tokens=None):
        if "STATEMENTS:" in prompt:
            raise RuntimeError("unavailable")
        return await generate(prompt, max_tokens=max_tokens)

    monkeypatch.setattr(v7_explain, "_generate", fail_compare)
    monkeypatch.setattr(full_check.asyncio, "sleep", _no_pause)
    sections = _sections()
    flow = _flow(sections)
    before = copy.deepcopy(flow)
    status = {}
    assert asyncio.run(full_check.apply_full_check(flow, sections, status=status)) == 0
    assert flow == before and status["state"] == status["compare"] == "failed"


def test_failed_piece_recovers_on_second_pass(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    _fake_ai(monkeypatch)
    generate = v7_explain._generate
    calls = []

    async def intermittent(prompt, max_tokens=None):
        if "ONE part of a student's thesis" in prompt and "Agile" in prompt:
            calls.append(1)
            if len(calls) <= 3:
                raise RuntimeError("unavailable")
        return await generate(prompt, max_tokens=max_tokens)

    monkeypatch.setattr(v7_explain, "_generate", intermittent)
    monkeypatch.setattr(full_check.asyncio, "sleep", _no_pause)
    sections = _sections()
    status = {}
    assert asyncio.run(full_check.apply_full_check(_flow(sections), sections, status=status)) == 1
    assert status["state"] == "ran" and len(calls) == 4


def test_lost_piece_with_two_fact_sections_is_partial(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    _fake_ai(monkeypatch)
    generate = v7_explain._generate

    async def fail_intro(prompt, max_tokens=None):
        if 'called "introduction"' in prompt:
            raise RuntimeError("unavailable")
        return await generate(prompt, max_tokens=max_tokens)

    monkeypatch.setattr(v7_explain, "_generate", fail_intro)
    monkeypatch.setattr(full_check.asyncio, "sleep", _no_pause)
    status = {}
    sections = _sections()
    assert asyncio.run(full_check.apply_full_check(_flow(sections), sections, status=status)) == 1
    assert status["state"] == "partial" and status["chunks_read"] == status["chunks_total"] - 1
    assert status["compare"] == "ok"


def test_healthy_whole_check_reports_complete_stats(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    _fake_ai(monkeypatch)
    sections = _sections()
    status = {}
    assert asyncio.run(full_check.apply_full_check(_flow(sections), sections, status=status)) == 1
    assert status["state"] == "ran" and status["chunks_read"] == status["chunks_total"]
    assert status["compare"] == status["confirm"] == "ok"


def test_no_statements_is_a_successful_check(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    async def empty(*args, **kwargs):
        return {"facts": []}
    monkeypatch.setattr(v7_explain, "_generate", empty)
    sections = [{"name": str(i), "text": "word " * 20} for i in range(2)]
    status = {}
    assert asyncio.run(full_check.apply_full_check([], sections, status=status)) == 0
    assert status["state"] == "ran" and status["compare"] == "skipped"


def test_confirm_failure_reports_failed(monkeypatch):
    monkeypatch.setenv("FULL_CHECK", "on")
    _fake_ai(monkeypatch)
    generate = v7_explain._generate
    async def fail_confirm(prompt, max_tokens=None):
        if "PAIR 1" in prompt:
            raise RuntimeError("unavailable")
        return await generate(prompt, max_tokens=max_tokens)
    monkeypatch.setattr(v7_explain, "_generate", fail_confirm)
    monkeypatch.setattr(full_check.asyncio, "sleep", _no_pause)
    sections = _sections()
    status = {}
    assert asyncio.run(full_check.apply_full_check(_flow(sections), sections, status=status)) == 0
    assert status["state"] == status["confirm"] == "failed"


def test_cancelled_read_propagates(monkeypatch):
    async def cancelled(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(full_check, "_extract", cancelled)
    sections = [{"name": str(i), "text": "word " * 20} for i in range(2)]
    import pytest
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(full_check._prepare(sections))


def test_chunks_over_safety_limit_are_unread(monkeypatch):
    monkeypatch.setattr(full_check, "MAX_CHUNK_CALLS", 1)
    async def empty(*args):
        return []
    monkeypatch.setattr(full_check, "_extract", empty)
    sections = [{"name": str(i), "text": "word " * 20} for i in range(2)]
    result = asyncio.run(full_check._prepare(sections))
    assert result.stats == {"state": "failed", "chunks_total": 2, "chunks_read": 1,
                            "compare": "skipped", "confirm": "none"}


def test_split_ledger_without_comparable_group_is_skipped(monkeypatch):
    monkeypatch.setattr(full_check, "LEDGER_SPLIT_CHARS", 1)
    async def extract(name, text):
        return [{"kind": "number" if name == "first" else "claim", "statement": name, "quote": text}]
    monkeypatch.setattr(full_check, "_extract", extract)
    sections = [{"name": name, "text": "word " * 20} for name in ("first", "second")]
    result = asyncio.run(full_check._prepare(sections))
    assert result.stats["state"] == "ran" and result.stats["compare"] == "skipped"
