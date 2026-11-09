"""A user's first scan is free; later scans cost a credit to unlock. Decided on the server."""
import asyncio

import main
from services import credits
from services.credits import CreditsService


class Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def neq(self, *_):
        return self

    def order(self, *_):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class DB:
    def __init__(self, oldest_scan_id, calls):
        self.oldest, self.calls = oldest_scan_id, calls

    def table(self, _name):
        return Query([{"analysis_run_id": self.oldest}] if self.oldest else [])

    def rpc(self, name, args):
        self.calls.append(args)
        return type("Q", (), {"execute": lambda _s: type("R", (), {"data": {"unlocked": True, "charged": args["p_charge"], "credits_remaining": 0}})()})()


def service(monkeypatch, oldest):
    calls = []
    monkeypatch.setattr(CreditsService, "_client", staticmethod(lambda: DB(oldest, calls)))
    return calls


def test_the_oldest_scan_of_a_user_is_the_free_one(monkeypatch):
    service(monkeypatch, "scan-1")
    assert asyncio.run(credits.credits_service.is_first_scan("user", "scan-1")) is True
    assert asyncio.run(credits.credits_service.is_first_scan("user", "scan-2")) is False


def test_a_lookup_problem_never_grants_the_free_scan(monkeypatch):
    class Broken:
        def table(self, _name):
            raise RuntimeError("database down")

    monkeypatch.setattr(CreditsService, "_client", staticmethod(lambda: Broken()))
    assert asyncio.run(credits.credits_service.is_first_scan("user", "scan-1")) is False


def test_unlocking_the_first_scan_does_not_charge_and_a_later_one_does(monkeypatch):
    calls = service(monkeypatch, "scan-1")
    first = asyncio.run(main.unlock_scan("scan-1", "user"))
    second = asyncio.run(main.unlock_scan("scan-2", "user"))
    assert [c["p_charge"] for c in calls] == [False, True]
    assert first["free"] is True and "free" not in second
