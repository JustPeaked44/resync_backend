import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from supabase_auth.errors import AuthApiError

import main


def _client(outcomes):
    calls = []

    def get_user(token):
        calls.append(token)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return SimpleNamespace(auth=SimpleNamespace(get_user=get_user)), calls


def _check(monkeypatch, outcomes):
    client, calls = _client(outcomes)
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: client)
    return calls


def test_a_dropped_connection_to_supabase_is_retried_and_does_not_end_the_session(monkeypatch):
    calls = _check(monkeypatch, [httpx.RemoteProtocolError("Server disconnected"), SimpleNamespace(user=SimpleNamespace(id="owner"))])
    assert asyncio.run(main.get_authenticated_user_id("Bearer token")) == "owner"
    assert len(calls) == 2


def test_supabase_that_cannot_be_reached_is_a_503_not_an_expired_session(monkeypatch):
    calls = _check(monkeypatch, [httpx.RemoteProtocolError("Server disconnected")])
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main.get_authenticated_user_id("Bearer token"))
    assert refused.value.status_code == 503 and len(calls) == 2   # tried twice, never reported as a 401


def test_a_token_supabase_rejects_is_still_a_401_without_a_retry(monkeypatch):
    calls = _check(monkeypatch, [AuthApiError("invalid JWT", 401, "bad_jwt")])
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main.get_authenticated_user_id("Bearer token"))
    assert refused.value.status_code == 401 and refused.value.detail == "Invalid or expired session." and len(calls) == 1
    calls = _check(monkeypatch, [SimpleNamespace(user=None)])   # an answer with no user is also a rejection
    with pytest.raises(HTTPException) as refused:
        asyncio.run(main.get_authenticated_user_id("Bearer token"))
    assert refused.value.status_code == 401


def test_the_app_loggers_have_a_handler_at_info_so_the_scan_summaries_reach_the_container_log():
    import logging
    app_log = logging.getLogger("resync")
    assert app_log.level == logging.INFO and any(isinstance(handler, logging.StreamHandler) for handler in app_log.handlers)
    assert logging.getLogger("resync.gemini").isEnabledFor(logging.INFO)
