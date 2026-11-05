"""A dropped database connection is retried instead of failing the scan."""

import httpx
import pytest

from services.db_service import DatabasePersistenceService as DB


class FakeSession:
    def __init__(self, failures, error=None):
        self.calls, self.failures, self.error = 0, failures, error or httpx.RemoteProtocolError("Server disconnected")

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "ok"


def wrap(session):
    client = type("Client", (), {"postgrest": type("P", (), {"session": session})()})()
    return DB._make_resilient(client), session


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("services.db_service.time.sleep", lambda *_: None)


def test_a_dropped_connection_is_retried_and_then_succeeds():
    client, session = wrap(FakeSession(failures=2))
    assert client.postgrest.session.request("GET", "/x") == "ok" and session.calls == 3


def test_a_connection_that_keeps_dropping_still_fails_after_three_tries():
    client, session = wrap(FakeSession(failures=9))
    with pytest.raises(httpx.RemoteProtocolError):
        client.postgrest.session.request("GET", "/x")
    assert session.calls == 3


def test_other_errors_are_not_retried():
    client, session = wrap(FakeSession(failures=9, error=ValueError("bad query")))
    with pytest.raises(ValueError):
        client.postgrest.session.request("GET", "/x")
    assert session.calls == 1
