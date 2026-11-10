import asyncio
import json

import httpx
import pytest

from services import gemini
EXPLAIN_LIMIT = 15  # any number of expected calls works here


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.waits = []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.delenv("GEMINI_RPM", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-primary")
    monkeypatch.delenv("GEMINI_API_KEY_FALLBACK", raising=False)
    fake = FakeClock()
    limiter = gemini.CallLimiter()
    limiter.clock, limiter.sleep = fake.now, fake.sleep
    monkeypatch.setattr(gemini, "LIMITER", limiter)
    return fake


REQUEST = httpx.Request("POST", gemini.GEMINI_URL)


def ok_response():
    return httpx.Response(200, json={"candidates": []}, request=REQUEST)


def retry_response(delay="7s"):
    body = {"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}}
    return httpx.Response(429, json=body, request=REQUEST)


def script_sends(monkeypatch, clock, responses, sent_at=None):
    queue = list(responses)

    async def fake_send(payload, key):
        if sent_at is not None:
            sent_at.append(clock.t)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(gemini, "_send", fake_send)


def test_twenty_five_calls_never_exceed_fourteen_per_rolling_minute(monkeypatch, clock):
    sent_at = []
    script_sends(monkeypatch, clock, [ok_response()], sent_at)

    async def run():
        await asyncio.gather(*(gemini.post_gemini_with_retry({}) for _ in range(25)))

    asyncio.run(run())
    assert len(sent_at) == 25
    for start in sent_at:
        assert sum(start <= t < start + 60 for t in sent_at) <= 14
    assert sum(clock.waits) > 0  # the 15th to 25th calls really waited


def test_rpm_default_and_env_override(monkeypatch):
    monkeypatch.delenv("GEMINI_RPM", raising=False)
    assert gemini.rpm() == 14
    monkeypatch.setenv("GEMINI_RPM", "9")
    assert gemini.rpm() == 9


def test_429_with_retry_delay_waits_seven_seconds_then_succeeds(monkeypatch, clock):
    script_sends(monkeypatch, clock, [retry_response("7s"), ok_response()])

    response = asyncio.run(gemini.post_gemini_with_retry({}))
    assert response.status_code == 200
    assert clock.waits == [7.0]


def test_repeated_429_beyond_cap_returns_failure_for_fallback(monkeypatch, clock):
    sent_at = []
    script_sends(monkeypatch, clock, [retry_response("60s")], sent_at)

    response = asyncio.run(gemini.post_gemini_with_retry({}))
    assert response.status_code == 429
    with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()
    assert len(sent_at) == 4  # 60 + 60 + 60 seconds of retries fit the cap, the fourth wait does not
    assert clock.t == pytest.approx(180.0)


def test_status_message_is_wait_during_retry_and_cleared_after(monkeypatch, clock):
    seen = []

    async def fake_send(payload, key):
        if not seen:
            return retry_response("7s")
        return ok_response()

    async def sleep_and_record(seconds):
        seen.append(gemini.scan_status_message("scan-1"))
        clock.t += seconds

    monkeypatch.setattr(gemini, "_send", fake_send)
    gemini.LIMITER.sleep = sleep_and_record

    async def run():
        gemini.scan_context.set("scan-1")
        return await gemini.post_gemini_with_retry({})

    asyncio.run(run())
    assert seen == [("wait", gemini.BUSY_MESSAGE)]
    assert gemini.scan_status_message("scan-1") == (None, None)


def test_status_message_info_when_window_is_80_percent_full_then_clears(monkeypatch, clock):
    script_sends(monkeypatch, clock, [ok_response()])

    async def run():
        gemini.scan_context.set("scan-2")
        for _ in range(12):
            await gemini.post_gemini_with_retry({})
        return gemini.scan_status_message("scan-2")

    level, message = asyncio.run(run())
    assert level == "info"
    assert message == gemini.BUSY_MESSAGE
    clock.t += 61
    assert gemini.scan_status_message("scan-2") == (None, None)


def test_status_message_info_when_scan_needs_more_calls_than_window(monkeypatch, clock):
    gemini.scan_context.set("scan-3")
    gemini.expect_calls(EXPLAIN_LIMIT + 1)
    assert gemini.scan_status_message("scan-3") == ("info", gemini.BUSY_MESSAGE)
    gemini.expect_calls(0)
    assert gemini.scan_status_message("scan-3") == (None, None)
    gemini.scan_context.set(None)


def test_wait_message_uses_singular_for_one_second(monkeypatch, clock):
    gemini.scan_context.set("scan-4")
    gemini.LIMITER.waits["scan-4"] = [clock.t + 1]
    assert gemini.scan_status_message("scan-4") == ("wait", "Waiting for the AI to be ready again (about 1 second).")
    gemini.LIMITER.waits.clear()
    gemini.scan_context.set(None)


def test_retry_after_header_is_used_when_no_retry_info(monkeypatch, clock):
    script_sends(monkeypatch, clock, [httpx.Response(429, headers={"Retry-After": "3"}), ok_response()])

    response = asyncio.run(gemini.post_gemini_with_retry({}))
    assert response.status_code == 200
    assert clock.waits == [3.0]


def test_overloaded_503_is_retried(monkeypatch, clock):
    script_sends(monkeypatch, clock, [httpx.Response(503, text='{"error":"model overloaded"}'), ok_response()])

    response = asyncio.run(gemini.post_gemini_with_retry({}))
    assert response.status_code == 200
    assert clock.waits == [1.0]


def test_each_key_has_its_own_window_and_uses_free_key(monkeypatch, clock):
    monkeypatch.setenv("GEMINI_API_KEY_FALLBACK", "test-fallback")
    calls = []

    async def send(_, key):
        calls.append((key == "test-primary", clock.t))
        return ok_response()

    monkeypatch.setattr(gemini, "_send", send)

    async def run():
        await asyncio.gather(*(gemini.post_gemini_with_retry({}) for _ in range(29)))

    asyncio.run(run())
    assert sum(primary for primary, _ in calls[:28]) == 14
    assert all(t == 0 for _, t in calls[:28])
    assert calls[28][1] == 60
    for primary in (True, False):
        times = [t for is_primary, t in calls if is_primary == primary]
        assert all(sum(t <= other < t + 60 for other in times) <= 14 for t in times)


def test_waiting_requests_are_served_fifo_and_cancelled_waiter_is_removed(monkeypatch, clock):
    monkeypatch.setenv("GEMINI_RPM", "1")
    order = []
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def sleep(seconds):
        entered.set()
        await gate.wait()
        clock.t += seconds
        await asyncio.sleep(0)

    async def send(payload, _):
        order.append(payload["name"])
        return ok_response()

    gemini.LIMITER.sleep = sleep
    monkeypatch.setattr(gemini, "_send", send)

    async def run():
        await gemini.post_gemini_with_retry({"name": "initial"})
        first = asyncio.create_task(gemini.post_gemini_with_retry({"name": "first"}))
        await entered.wait()
        cancelled = asyncio.create_task(gemini.post_gemini_with_retry({"name": "cancelled"}))
        await asyncio.sleep(0)
        second = asyncio.create_task(gemini.post_gemini_with_retry({"name": "second"}))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        gate.set()
        await asyncio.gather(first, second)

    asyncio.run(run())
    assert order == ["initial", "first", "second"]


def test_minute_429_keeps_same_key_even_when_other_key_is_free(monkeypatch, clock):
    monkeypatch.setenv("GEMINI_API_KEY_FALLBACK", "test-fallback")
    monkeypatch.setenv("GEMINI_RPM", "1")
    calls = []

    async def send(_, key):
        calls.append(key == "test-primary")
        return retry_response("1s") if len(calls) == 1 else ok_response()

    monkeypatch.setattr(gemini, "_send", send)
    response = asyncio.run(gemini.post_gemini_with_retry({}))
    assert response.status_code == 200
    assert calls == [True, True]
    assert clock.t == 60
