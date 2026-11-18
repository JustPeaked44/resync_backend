import asyncio

import pytest
from fastapi import HTTPException

import main
from services.db_service import DatabasePersistenceService, DISCARDED_MESSAGE


class Table:
    def __init__(self, db, name):
        self.db, self.name, self.mode, self.payload, self.filters = db, name, None, None, {}

    def select(self, *_):
        self.mode = "select"
        return self

    def delete(self):
        self.mode = "delete"
        return self

    def update(self, payload):
        self.mode, self.payload = "update", payload
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def execute(self):
        self.db.calls.append((self.name, self.mode, dict(self.filters), self.payload))
        if (self.name, self.mode) == self.db.failure:
            raise RuntimeError("permission denied")
        rows = [{"user_id": self.db.owner, "manuscript_id": "m-1"}] if self.mode == "select" else []
        return type("R", (), {"data": rows})()


class DB:
    def __init__(self, owner="user-1"):
        self.owner, self.calls, self.failure = owner, [], None

    def table(self, name):
        return Table(self, name)


@pytest.fixture
def db(monkeypatch):
    fake = DB()
    monkeypatch.setattr(DatabasePersistenceService, "get_client", classmethod(lambda cls: fake))
    return fake


def test_leaving_a_report_deletes_everything_it_holds_of_the_manuscript(db):
    assert asyncio.run(main.discard_scan("scan-1", "user-1")) == {"discarded": True}
    deleted = {name for name, mode, _, _ in db.calls if mode == "delete"}
    assert deleted == set(DatabasePersistenceService.SCAN_CONTENT_TABLES)
    update = next(payload for name, mode, _, payload in db.calls if name == "analysis_run" and mode == "update")
    assert update == {"analysis_run_result_json": None, "analysis_run_status": "failed", "analysis_run_error_message": DISCARDED_MESSAGE}
    manuscript = next(payload for name, mode, _, payload in db.calls if name == "manuscript" and mode == "update")
    assert manuscript == dict.fromkeys(("manuscript_content_hash", "manuscript_structure", "manuscript_parsed_sections", "manuscript_raw_text"))
    assert db.calls[1][:3] == ("analysis_run", "update", {"analysis_run_id": "scan-1", "user_id": "user-1"})


@pytest.mark.parametrize("failed_table, mode", [("citation", "delete"), ("manuscript", "update")])
def test_content_delete_failure_does_not_stop_discard(db, caplog, failed_table, mode):
    db.failure = (failed_table, mode)
    assert asyncio.run(DatabasePersistenceService.discard_scan("user-1", "scan-1")) is True
    assert db.calls[1][1] == "update"
    assert {call[0] for call in db.calls if call[1] == "delete"} == set(DatabasePersistenceService.SCAN_CONTENT_TABLES)
    assert failed_table in caplog.text


def test_update_failure_stops_deletions(db):
    db.failure = ("analysis_run", "update")
    with pytest.raises(RuntimeError, match="permission denied"):
        asyncio.run(DatabasePersistenceService.discard_scan("user-1", "scan-1"))
    assert not [call for call in db.calls if call[1] == "delete"]
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.discard_scan("scan-1", "user-1"))
    assert caught.value.status_code == 503


def test_someone_else_cannot_delete_a_report(db):
    assert asyncio.run(DatabasePersistenceService.discard_scan("intruder", "scan-1")) is False
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.discard_scan("scan-1", "intruder"))
    assert caught.value.status_code == 404
    assert not [call for call in db.calls if call[1] in ("delete", "update")]


def test_a_database_problem_is_reported_not_hidden(monkeypatch):
    class Broken:
        def table(self, _name):
            raise RuntimeError("database down")

    monkeypatch.setattr(DatabasePersistenceService, "get_client", classmethod(lambda cls: Broken()))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.discard_scan("scan-1", "user-1"))
    assert caught.value.status_code == 503
