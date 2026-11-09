"""Daily-quota fail-fast and dropped-connection retries, layered on the rate limiter tested in test_gemini_rate.py."""

import asyncio
import json
import uuid

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
    monkeypatch.setattr(gemini, "LIMITER", gemini.CallLimiter())
    monkeypatch.setattr(gemini.LIMITER, "sleep", instant)
    monkeypatch.setattr(gemini, "_quota_blocked_until", {})
    monkeypatch.setenv("GEMINI_API_KEY", "test-primary")
    monkeypatch.delenv("GEMINI_API_KEY_FALLBACK", raising=False)
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


def configure_two_keys(monkeypatch):
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    monkeypatch.setenv("GEMINI_API_KEY", first)
    monkeypatch.setenv("GEMINI_API_KEY_FALLBACK", second)
    return first, second


def test_daily_limit_switches_once_without_flagging_scan_or_logging_credentials(monkeypatch):
    first, second = configure_two_keys(monkeypatch)
    calls, safe_logs, same_payload = [], [], []
    payload = {"contents": []}
    async def send(request, credential):
        same_payload.append(request is payload)
        calls.append(0 if credential == first else 1 if credential == second else 2)
        return QuotaResponse(429) if len(calls) == 1 else Response(200)
    class Logger:
        def warning(self, message, *args):
            rendered = message % args
            safe_logs.append(first not in rendered and second not in rendered and "second" in rendered)
    monkeypatch.setattr(gemini, "_send", send)
    monkeypatch.setattr(gemini.logging.getLogger("resync.gemini"), "warning", Logger().warning)
    async def run_calls():
        token = gemini.scan_context.set("switch-test")
        try:
            one = await gemini.post_gemini_with_retry(payload)
            two = await gemini.post_gemini_with_retry(payload)
            return one.status_code, two.status_code, gemini.rate_limited(), gemini.scan_status_message("switch-test")
        finally:
            gemini.scan_context.reset(token)
    first_status, second_status, flagged, status = asyncio.run(run_calls())
    assert (first_status, second_status) == (200, 200)
    assert calls == [0, 1, 1] and all(same_payload)
    assert not flagged and status == (None, None)
    assert safe_logs == [True]
    assert not gemini.quota_blocked()


def test_both_daily_quotas_exhaust_then_first_recovers(monkeypatch):
    first, second = configure_two_keys(monkeypatch)
    calls = []
    now = [100.0]
    monkeypatch.setattr(gemini.time, "monotonic", lambda: now[0])
    async def send(payload, credential):
        calls.append(0 if credential == first else 1 if credential == second else 2)
        return QuotaResponse(429) if len(calls) <= 2 else Response(200)
    monkeypatch.setattr(gemini, "_send", send)
    async def run_calls():
        token = gemini.scan_context.set("exhaust-test")
        try:
            with pytest.raises(gemini.GeminiQuotaExhausted):
                await gemini.post_gemini_with_retry({})
            assert gemini.quota_blocked() and gemini.rate_limited()
            with pytest.raises(gemini.GeminiQuotaExhausted):
                await gemini.post_gemini_with_retry({})
            now[0] += gemini.QUOTA_RECHECK_SECONDS
            assert not gemini.quota_blocked()
            return await gemini.post_gemini_with_retry({})
        finally:
            gemini.scan_context.reset(token)
    response = asyncio.run(run_calls())
    assert response.status_code == 200
    assert calls == [0, 1, 0]


def test_minute_limit_retries_same_key(monkeypatch):
    first, second = configure_two_keys(monkeypatch)
    calls = []
    async def send(payload, credential):
        calls.append(0 if credential == first else 1 if credential == second else 2)
        return httpx.Response(429, headers={"Retry-After": "1"}) if len(calls) == 1 else Response(200)
    monkeypatch.setattr(gemini, "_send", send)
    response = asyncio.run(gemini.post_gemini_with_retry({}))
    assert response.status_code == 200
    assert calls == [0, 0]
    assert gemini.rate_limited()
    assert not gemini.quota_blocked()
