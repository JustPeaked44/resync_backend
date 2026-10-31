"""Shared Gemini transport, rate limiting and source-quote verification for v7 explanations."""

import asyncio
import collections
import contextvars
import math
import os
import re
import logging
import time
import httpx

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
RATE_WINDOW_SECONDS = 60.0
# A single Gemini call never waits longer than this in total across limiter waits and retry delays.
RETRY_CAP_SECONDS = 180.0
BUSY_MESSAGE = "The AI is busy right now, so your scan may take a little longer than usual."
QUOTA_RECHECK_SECONDS = 600
TRANSPORT_ATTEMPTS = 3  # a dropped connection or timeout is retried this many times in all


class GeminiQuotaExhausted(Exception):
    """A confirmed per-day provider quota cannot be fixed by retrying now."""


_quota_blocked_until = 0.0


def quota_blocked():
    return time.monotonic() < _quota_blocked_until

# Set per scan job (see main._run_scan_job) so the limiter can report status to that scan's poller.
scan_context = contextvars.ContextVar("gemini_scan_id", default=None)

_WHITESPACE = re.compile(r"\s+")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def verify_quote(quote, text):
    if not quote:
        return "", True
    if not text:
        return "", False
    normalized = lambda value: _WHITESPACE.sub(" ", value.translate(_QUOTES)).strip().lower()
    return (quote, True) if normalized(quote) in normalized(text) else ("", False)


def truncate_tail(text, max_chars=3500):
    """The END of a section, starting on a whole word: the part that connects to whatever follows."""
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[-max_chars:]
    return "[...] " + cut[cut.find(" ") + 1:]


def truncate_text(text, max_chars=6000):
    cleaned = text.strip()
    return cleaned if len(cleaned) <= max_chars else cleaned[:max_chars] + "... [truncated]"


def rpm():
    """Gemini requests allowed per rolling minute (free tier default)."""
    try:
        return max(1, int(os.getenv("GEMINI_RPM", "14")))
    except ValueError:
        return 14


class CallLimiter:
    """Process-wide rolling-window limit on Gemini requests. Every request, retries included, takes a slot."""

    def __init__(self):
        self.clock = time.monotonic
        self.sleep = asyncio.sleep
        self.sent = collections.deque()
        self.waits = {}          # scan id -> end times of the waits in progress
        self.over_budget = set()  # scan ids whose remaining calls cannot fit in one window
        self.retrying_429 = set()
        self.rate_limited = set()

    def _prune(self, now):
        while self.sent and now - self.sent[0] >= RATE_WINDOW_SECONDS:
            self.sent.popleft()

    def seconds_until_slot(self):
        now = self.clock()
        self._prune(now)
        if len(self.sent) < rpm():
            return 0.0
        return self.sent[0] + RATE_WINDOW_SECONDS - now

    async def pause(self, scan_id, seconds):
        end = self.clock() + seconds
        self.waits.setdefault(scan_id, []).append(end)
        try:
            await self.sleep(seconds)
        finally:
            self.waits[scan_id].remove(end)
            if not self.waits[scan_id]:
                del self.waits[scan_id]

    async def acquire(self, scan_id, spent):
        """Waits for a free rolling-window slot and takes it. Returns the total seconds spent, or None past the cap."""
        while True:
            wait = self.seconds_until_slot()
            if not wait:
                self.sent.append(self.clock())
                return spent
            if spent + wait > RETRY_CAP_SECONDS:
                return None
            await self.pause(scan_id, wait)
            spent += wait

    def status(self, scan_id):
        now = self.clock()
        if scan_id in self.retrying_429:
            return "wait", BUSY_MESSAGE
        ends = [end for end in self.waits.get(scan_id, []) if end > now]
        if ends:
            seconds = math.ceil(max(ends) - now)
            return "wait", f"Waiting for the AI to be ready again (about {seconds} second{'' if seconds == 1 else 's'})."
        self._prune(now)
        if scan_id is not None and (scan_id in self.over_budget or len(self.sent) >= 0.8 * rpm()):
            return "info", BUSY_MESSAGE
        return None, None


LIMITER = CallLimiter()


def expect_calls(count):
    """Flags the current scan while it needs more Gemini calls than one rolling window allows. Zero clears it."""
    scan_id = scan_context.get()
    if count > rpm():
        LIMITER.over_budget.add(scan_id)
    else:
        LIMITER.over_budget.discard(scan_id)
        if count == 0:
            LIMITER.rate_limited.discard(scan_id)


def rate_limited():
    return scan_context.get() in LIMITER.rate_limited


def scan_status_message(scan_id):
    """(status_message_level, status_message) for a scan, or (None, None) when the AI is back to normal."""
    return LIMITER.status(scan_id)


def _retry_delay(response):
    """Server-suggested wait in seconds from a RetryInfo detail or Retry-After header, else None."""
    try:
        for detail in response.json().get("error", {}).get("details", []):
            if str(detail.get("@type", "")).endswith("RetryInfo") and detail.get("retryDelay"):
                return float(str(detail["retryDelay"]).rstrip("s"))
    except (ValueError, AttributeError):
        pass
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, TypeError, ValueError):
        return None


def _is_retryable(response):
    return response.status_code == 429 or (response.status_code == 503 and "overloaded" in response.text.lower())


def _is_daily_quota(response):
    try:
        details = response.json().get("error", {}).get("details", [])
        return any(
            str(detail.get("@type", "")).endswith("QuotaFailure")
            and any(re.search(r"per.?day|daily", " ".join(str(violation.get(field, ""))
                    for field in ("quotaId", "quotaMetric", "description")), re.I)
                    for violation in detail.get("violations", []))
            for detail in details
        )
    except (ValueError, AttributeError, TypeError):
        return False


async def _send(payload, key):
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.post(GEMINI_URL, json=payload,
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})


async def post_gemini_with_retry(payload):
    global _quota_blocked_until
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    scan_id = scan_context.get()
    if quota_blocked():  # already known to be out of quota: fail at once instead of sending more doomed requests
        LIMITER.rate_limited.add(scan_id)
        raise GeminiQuotaExhausted("The AI's temporary limit was reached")
    spent = 0.0
    response = None
    transport_failures = 0
    try:
        while True:
            spent = await LIMITER.acquire(scan_id, spent)
            if spent is None:
                break
            try:
                response = await _send(payload, key)
            except httpx.TransportError:  # timeout or dropped connection
                transport_failures += 1
                if transport_failures >= TRANSPORT_ATTEMPTS:
                    raise
                await LIMITER.pause(scan_id, 2.0 * transport_failures)
                spent += 2.0 * transport_failures
                continue
            if response.status_code == 429:
                LIMITER.rate_limited.add(scan_id)
                LIMITER.retrying_429.add(scan_id)
                if _is_daily_quota(response):
                    _quota_blocked_until = time.monotonic() + QUOTA_RECHECK_SECONDS
                    logging.getLogger("resync.gemini").warning("Gemini quota exhausted; pausing AI reviews for %ss", QUOTA_RECHECK_SECONDS)
                    raise GeminiQuotaExhausted("The AI's temporary limit was reached")
            if not _is_retryable(response):
                return response
            logging.getLogger("resync.gemini").warning("Gemini returned %s", response.status_code)
            delay = _retry_delay(response) or max(1.0, LIMITER.seconds_until_slot())
            if spent + delay > RETRY_CAP_SECONDS:
                break
            await LIMITER.pause(scan_id, delay)
            spent += delay
    finally:
        LIMITER.retrying_429.discard(scan_id)
    # Out of budget: hand back the last failure so callers fall back exactly as they do for any error.
    return response or httpx.Response(429, request=httpx.Request("POST", GEMINI_URL))
