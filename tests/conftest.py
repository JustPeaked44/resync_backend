import pytest


@pytest.fixture(autouse=True)
def verdicts_are_not_remembered_between_tests(monkeypatch):
    """Tests that fake the AI must always reach the fake, so saved verdicts are switched off; test_verdict_cache turns them on."""
    from services.verdict_cache import VerdictCache
    monkeypatch.setenv("VERDICT_CACHE", "off")
    monkeypatch.setenv("GDOC_AS_DOCX", "off")   # tests that fake the text export must reach it (test_manuscript_outline covers the Word route)
    monkeypatch.setenv("FULL_CHECK", "off")   # the whole-manuscript check has its own tests (test_full_check)
    VerdictCache.forget_everything_in_memory()
    yield
    VerdictCache.forget_everything_in_memory()


@pytest.fixture(autouse=True)
def the_ai_quota_state_is_not_shared_between_tests():
    """A scan now stops when the AI limit is reached, so a quota or rate-limit mark left by one test would stop the next one."""
    from services import gemini
    gemini._quota_blocked_until.clear()
    gemini.LIMITER.rate_limited.clear()
    gemini._requests.clear()
    yield
    gemini._quota_blocked_until.clear()
    gemini.LIMITER.rate_limited.clear()
    gemini._requests.clear()


@pytest.fixture(autouse=True)
def tests_never_see_the_real_gemini_keys(monkeypatch):
    """The developer's .env holds real keys. A test that forgets to fake the AI would send real requests, spend the quota and wait
    through Google's retries, so no test sees a key unless it sets one itself (test_gemini_retry does)."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY_FALLBACK", "GEMINI_API_KEY_FALLBACK_2"):
        monkeypatch.delenv(name, raising=False)

