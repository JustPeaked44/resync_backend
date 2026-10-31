"""Daily-quota fail-fast and dropped-connection retries, layered on the rate limiter tested in test_gemini_rate.py."""

import asyncio
import json

import httpx
import pytest

from services import gemini


class Response:
    text = ""
    headers = {}

    def __init__(self, status):
        self.status_code = status


class QuotaResponse(Response):
    text = '{"error": {"details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}'

    def json(self):
        return json.loads(self.text)


def fake_client(outcomes, calls):
    class Client:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, *_, **__):
            calls.append(1)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
    return Client


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(gemini.LIMITER, "sleep", instant)
    monkeypatch.setattr(gemini, "_quota_blocked_until", 0.0)
    gemini.LIMITER.sent.clear()
    yield
    gemini.LIMITER.sent.clear()


def run(outcomes, monkeypatch):
    calls = []
    monkeypatch.setattr(gemini.httpx, "AsyncClient", fake_client(outcomes, calls))
    return asyncio.run(gemini.post_gemini_with_retry({})), calls


def test_a_permanent_error_is_returned_without_retrying(monkeypatch):
    response, calls = run([Response(403)], monkeypatch)
    assert response.status_code == 403 and len(calls) == 1


def test_timeouts_are_retried_and_finally_raised(monkeypatch):
    ok, calls = run([httpx.ReadTimeout("slow"), Response(200)], monkeypatch)
    assert ok.status_code == 200 and len(calls) == 2
    with pytest.raises(httpx.ReadTimeout):
        run([httpx.ReadTimeout("slow")], monkeypatch)


def test_a_daily_quota_stops_immediately_and_later_calls_do_not_even_try(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini.httpx, "AsyncClient", fake_client([QuotaResponse(429)], calls))
    with pytest.raises(gemini.GeminiQuotaExhausted):
        asyncio.run(gemini.post_gemini_with_retry({}))
    assert len(calls) == 1 and gemini.quota_blocked()          # no waiting for a retry that cannot succeed
    with pytest.raises(gemini.GeminiQuotaExhausted):
        asyncio.run(gemini.post_gemini_with_retry({}))
    assert len(calls) == 1                                     # the second call never reached the network


def test_unstructured_daily_word_does_not_stop_minute_retry(monkeypatch):
    response = httpx.Response(429, json={"error": {"message": "PerDay is mentioned here", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}]}})
    result, calls = run([response, Response(200)], monkeypatch)
    assert result.status_code == 200 and len(calls) == 2
    assert not gemini.quota_blocked()


def test_truncate_tail_keeps_the_end_on_a_whole_word():
    text = " ".join(f"word{i}" for i in range(2000))
    cut = gemini.truncate_tail(text, 100)
    assert cut.startswith("[...] word") and cut.endswith("word1999") and len(cut) <= 110
    assert gemini.truncate_tail("short text", 100) == "short text"
