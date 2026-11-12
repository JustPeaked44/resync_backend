import asyncio
from collections import Counter, defaultdict
import hashlib
import json
import random
import time
from types import SimpleNamespace

import httpx
import pytest

from services import full_check, gemini
from tests.test_full_check import _flow, METHOD_SENTENCE, CONCLUSION_SENTENCE

_real_sleep = asyncio.sleep


async def _no_pause(*args):
    await _real_sleep(0)


def _sections():
    sections = []
    for i in range(4):
        paragraphs = []
        for j in range(2):
            first = METHOD_SENTENCE if (i, j) == (0, 0) else CONCLUSION_SENTENCE if (i, j) == (1, 0) else f"Section {i} part {j} describes a distinct research finding."
            paragraphs.append(first + " " + (f"Supporting detail for section {i} part {j} appears here. " * 130))
        sections.append({"name": f"section {i}", "text": "\n".join(paragraphs)})
    return sections


def _run(monkeypatch, rate=0, seed=0, malformed=False, first_503=False, permanent=False):
    cache, attempts, prompts = {}, defaultdict(int), []
    async def get_many(keys):
        return {k: cache[k] for k in keys if k in cache}
    async def save_many(values):
        cache.update(values)
    monkeypatch.setattr(full_check.VerdictCache, "get_many", get_many)
    monkeypatch.setattr(full_check.VerdictCache, "save_many", save_many)
    monkeypatch.setattr(gemini, "quota_blocked", lambda: False)
    monkeypatch.setattr(gemini, "expect_calls", lambda *args: None)
    monkeypatch.setattr(full_check.asyncio, "sleep", _no_pause)
    monkeypatch.setattr(full_check, "time", SimpleNamespace(monotonic=time.monotonic))
    monkeypatch.setattr(full_check, "_jitter", SimpleNamespace(uniform=lambda *args: 1.0))
    monkeypatch.setenv("FULL_CHECK", "on")

    async def post(payload):
        prompt = payload["contents"][0]["parts"][0]["text"]
        digest = hashlib.sha1(prompt.encode()).hexdigest()
        attempts[digest] += 1
        attempt = attempts[digest]
        prompts.append(prompt)
        read = "ONE part of a student's thesis" in prompt
        text = prompt.split("TEXT:\n", 1)[1] if read else ""
        target = read and METHOD_SENTENCE in text and len(text) > 2500
        fail = random.Random(f"{seed}:{digest}:{attempt}").random() < rate
        if (first_503 and target and attempt == 1) or (permanent and target) or fail:
            return httpx.Response(503, text="Service Unavailable", request=httpx.Request("POST", "https://example.invalid"))
        if malformed and target and len(text) > 5000:
            answer = "invalid JSON"
        elif read:
            sentence = text.splitlines()[0].split(". ", 1)[0].rstrip(".") + "."
            answer = json.dumps({"facts": [{"kind": "method", "statement": sentence, "quote": sentence}]})
        elif "STATEMENTS:" in prompt:
            lines = [line.split(" | ") for line in prompt.split("STATEMENTS:", 1)[1].strip().splitlines()]
            a = next((x[0] for x in lines if METHOD_SENTENCE in x[3]), None)
            b = next((x[0] for x in lines if CONCLUSION_SENTENCE in x[3]), None)
            answer = json.dumps({"conflicts": [{"fact_a": a, "fact_b": b, "fix_id": b, "severity": "Moderate", "category": "methodology", "what": "Different models", "why": "The development models disagree."}] if a and b else []})
        else:
            answer = json.dumps({"results": [{"id": 1, "genuine": True, "severity": "Moderate", "fixed_sentence": METHOD_SENTENCE}]})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": answer}]}}]}, request=httpx.Request("POST", "https://example.invalid"))
    monkeypatch.setattr(gemini, "post_gemini_with_retry", post)
    sections = _sections()
    flow, status = _flow(sections), {}
    count = asyncio.run(full_check.apply_full_check(flow, sections, status=status))
    return json.dumps([flow, count], sort_keys=True), status, prompts, count


@pytest.mark.parametrize("rate", [0.1, 0.3, 0.5])
def test_fault_injection_preserves_ran_result(monkeypatch, rate):
    baseline, status, _, count = _run(monkeypatch)
    assert status["state"] == "ran" and status["chunks_total"] >= 8 and count == 1
    totals = Counter()
    for seed in range(40):
        result, status, _, _ = _run(monkeypatch, rate, seed)
        state = status["state"]
        totals[state] += 1
        assert state in ("ran", "partial", "failed")
        if state == "ran":
            assert result == baseline, f"rate={rate} seed={seed}"
    print(f"rate={rate} ran={totals['ran']} partial={totals['partial']} failed={totals['failed']}")


def test_malformed_halves_are_deterministic(monkeypatch):
    baseline, status, prompts, count = _run(monkeypatch, malformed=True)
    assert status["state"] == "ran" and count == 1
    assert any(METHOD_SENTENCE in p and len(p.split("TEXT:\n")[-1]) < 4000 for p in prompts)
    result, status, _, _ = _run(monkeypatch, rate=0.3, seed=3, malformed=True)
    assert status["state"] == "ran" and result == baseline


def test_transient_long_chunk_never_uses_halves(monkeypatch):
    baseline, _, _, _ = _run(monkeypatch)
    result, status, prompts, _ = _run(monkeypatch, first_503=True)
    assert status["state"] == "ran" and result == baseline
    assert not any(METHOD_SENTENCE in p and "TEXT:\n" in p and len(p.split("TEXT:\n")[-1]) < 4000 for p in prompts)





def test_retry_after_is_logged_and_bounded(monkeypatch, caplog):
    waits = []
    async def sleep(delay):
        waits.append(delay)
    async def post(payload):
        return httpx.Response(503, text="Service Unavailable", headers={"Retry-After": "50"}, request=httpx.Request("POST", "https://example.invalid"))
    monkeypatch.setattr(gemini, "post_gemini_with_retry", post)
    monkeypatch.setattr(full_check.asyncio, "sleep", sleep)
    assert asyncio.run(full_check._ask("prompt", "read")) is None
    assert waits == [] and "Retry-After: 50" in caplog.text and "Service Unavailable" in caplog.text


def test_extra_pass_budget_preserves_previous_result(monkeypatch):
    monkeypatch.setattr(gemini, "quota_blocked", lambda: False)
    monkeypatch.setenv("FULL_CHECK", "on")
    result = full_check.Prepared(([], {}))
    result.stats = {"state": "failed"}
    calls = []
    async def prepare(*args, **kwargs):
        calls.append(1)
        return result
    monkeypatch.setattr(full_check, "_prepare", prepare)
    times = iter([0, 80])
    monkeypatch.setattr(full_check, "time", SimpleNamespace(monotonic=lambda: next(times)))
    status = {}
    assert asyncio.run(full_check.apply_full_check([], _sections(), status=status)) == 0
    assert calls == [1] and status["state"] == "failed"


def test_permanent_transient_failure_is_not_ran(monkeypatch):
    _, status, prompts, _ = _run(monkeypatch, permanent=True)
    assert status["state"] in ("partial", "failed")
    assert not any(METHOD_SENTENCE in p and "TEXT:`n" in p and len(p.split("TEXT:`n")[-1]) < 4000 for p in prompts)
