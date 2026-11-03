"""Contract checks for the schema cleanup paths that handle ownership and credits."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main
from services import credits
from services.scan_access import locked_scan_view


def test_saved_view_uses_database_unlock_flag_even_for_old_rows():
    report = {"analysis_run_id": "run", "manuscript_sections": [
        {"name": "First", "text": "Visible."}, {"name": "Second", "text": "Secret."}],
        "recommendations": [], "citations": [], "inconsistencies": [], "manuscript_flow": [],
        "ai_synthesis": {"summary": "Private"},
        "access": {"unlocked": False, "unlock_cost": 1}}
    expected = deepcopy(report)
    assert main._saved_scan_view(report, False) == locked_scan_view(expected)
    expected["access"]["unlocked"] = True
    assert main._saved_scan_view(report, True) == locked_scan_view(expected)
    assert report["access"]["unlocked"] is False
    old = deepcopy(report)
    old.pop("access")
    assert main._saved_scan_view(old, False)["manuscript_sections"][1]["text"] is None
    assert main._saved_scan_view(old, True)["manuscript_sections"][1]["text"] == "Secret."


def test_locked_old_preview_without_saved_sections_does_not_fetch_document(monkeypatch):
    class DB:
        def table(self, *_): return self
        def select(self, *_): return self
        def eq(self, *_): return self
        def execute(self):
            return SimpleNamespace(data=[{"user_id": "owner", "analysis_run_is_unlocked": False,
                                          "analysis_run_result_json": {"doc_url": "https://docs.google.com/document/d/id/edit"}}])

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    assert asyncio.run(main.get_manuscript_preview("scan", authenticated_user_id="owner")) == {
        "available": False, "reason": "locked"}


def test_feedback_updates_the_owned_issue(monkeypatch):
    written = []

    class DB:
        table_name = None

        def table(self, name):
            self.table_name = name
            return self

        def select(self, *_): return self
        def eq(self, *_): return self
        def single(self): return self

        def update(self, payload):
            written.append((self.table_name, payload))
            return self

        def execute(self):
            if self.table_name == "inconsistency":
                return SimpleNamespace(data=[{"analysis_run_id": "run"}])
            return SimpleNamespace(data={"user_id": "owner"})

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    result = asyncio.run(main.submit_issue_feedback("issue", main.IssueFeedbackRequest(helpful=True), "owner", "owner"))
    asyncio.run(main.submit_issue_feedback("issue", main.IssueFeedbackRequest(helpful=False), "owner", "owner"))
    assert result.status == "created" and result.feedback_id
    assert written == [("inconsistency", {"inconsistency_is_helpful": True}),
                       ("inconsistency", {"inconsistency_is_helpful": False})]


def test_device_registration_moves_an_existing_player_and_delete_checks_owner(monkeypatch):
    rows = {}

    class DB:
        def __init__(self):
            self.operation = None
            self.filters = {}

        def table(self, name):
            assert name == "user_device_token"
            self.operation = None
            self.filters = {}
            return self

        def upsert(self, payload, on_conflict):
            assert on_conflict == "user_device_token_player_id"
            self.operation = ("upsert", payload)
            return self

        def delete(self):
            self.operation = ("delete", None)
            return self

        def eq(self, column, value):
            self.filters[column] = value
            return self

        def execute(self):
            if self.operation[0] == "upsert":
                row = self.operation[1]
                rows[row["user_device_token_player_id"]] = row
                return SimpleNamespace(data=[row])
            player = self.filters["user_device_token_player_id"]
            owner = self.filters["user_id"]
            row = rows.get(player)
            if row and row["user_id"] == owner:
                del rows[player]
                return SimpleNamespace(data=[row])
            return SimpleNamespace(data=[])

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    body = main.DeviceRegistration(player_id="phone", platform="android")
    assert asyncio.run(main.register_device(body, "owner")) == {"registered": True}
    assert asyncio.run(main.register_device(body, "owner")) == {"registered": True}
    assert len(rows) == 1 and rows["phone"]["user_id"] == "owner"
    assert asyncio.run(main.register_device(main.DeviceRegistration(player_id="phone", platform="ios"), "other")) == {"registered": True}
    assert rows["phone"]["user_id"] == "other" and rows["phone"]["user_device_token_platform"] == "ios"
    with pytest.raises(HTTPException) as denied:
        asyncio.run(main.remove_device("phone", "owner"))
    assert denied.value.status_code == 404 and rows["phone"]["user_id"] == "other"
    assert asyncio.run(main.remove_device("phone", "other")) == {"removed": True}
    assert rows == {}


def test_balance_uses_ledger_rpc_and_checkout_inserts_user_id(monkeypatch):
    calls = []

    class DB:
        def rpc(self, name, args):
            calls.append((name, args))
            return self

        def table(self, name):
            assert name == "pymt_txn"
            return self

        def insert(self, payload):
            calls.append(("insert", payload))
            return self

        def execute(self):
            return SimpleNamespace(data=7)

    monkeypatch.setattr(credits.DatabasePersistenceService, "get_client", DB)
    monkeypatch.setattr(credits, "CREDITS_ENABLED", True)
    assert asyncio.run(credits.CreditsService.get_balance("owner")) == 7
    result = asyncio.run(credits.CreditsService.create_checkout("owner", 2))
    assert calls[0] == ("get_credit_balance", {"p_user_id": "owner"})
    assert calls[1][1]["user_id"] == "owner" and "subscription_id" not in calls[1][1]
    assert result["amount"] == 50.0 and result["credit_amount"] == 2
