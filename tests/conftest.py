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
