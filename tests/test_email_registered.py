"""Forgot password can tell an unregistered email from a registered one."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main


def ask(email, host="1.2.3.4"):
    request = SimpleNamespace(client=SimpleNamespace(host=host))
    return asyncio.run(main.email_registered(main.EmailCheckRequest(email=email), request))


class Rpc:
    def __init__(self, answer):
        self.answer = answer

    def execute(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(data=self.answer)


class Client:
    def __init__(self, answer, users=None):
        self.answer, self.users = answer, users or []
        self.auth = SimpleNamespace(admin=SimpleNamespace(list_users=lambda page=1, per_page=200: self.users if page == 1 else []))

    def rpc(self, name, args):
        assert name == "email_registered"
        return Rpc(self.answer)


@pytest.fixture(autouse=True)
def fresh():
    main._email_checks.clear()


def test_a_registered_email_is_found_and_the_case_does_not_matter(monkeypatch):
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: Client(True))
    assert ask("Someone@Gmail.com").registered is True


def test_an_unregistered_email_is_not_found(monkeypatch):
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: Client(False))
    assert ask("nobody@gmail.com").registered is False


def test_without_the_sql_function_the_user_list_is_searched(monkeypatch):
    users = [SimpleNamespace(email="Real@Gmail.com")]
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: Client(RuntimeError("no function"), users))
    assert ask("real@gmail.com").registered is True
    assert ask("other@gmail.com").registered is False


def test_too_many_checks_in_a_minute_are_refused(monkeypatch):
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: Client(False))
    for _ in range(main.EMAIL_CHECKS_PER_MINUTE):
        ask("a@gmail.com")
    with pytest.raises(HTTPException) as error:
        ask("a@gmail.com")
    assert error.value.status_code == 429


def test_a_badly_formed_email_is_refused():
    with pytest.raises(Exception):
        main.EmailCheckRequest(email="not-an-email")
