import pytest


@pytest.fixture(autouse=True)
def verdicts_are_not_remembered_between_tests(monkeypatch):
    """Tests that fake the AI must always reach the fake, so saved verdicts are switched off; test_verdict_cache turns them on."""
    from services.verdict_cache import VerdictCache
    monkeypatch.setenv("VERDICT_CACHE", "off")
    VerdictCache.forget_everything_in_memory()
    yield
    VerdictCache.forget_everything_in_memory()
