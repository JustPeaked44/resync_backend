import pytest
from fastapi.testclient import TestClient
import main


def _payment_stubs(monkeypatch):
    async def user():
        return "user-b"
    async def purchase(*args):
        return {"pymt_txn_id": "own-transaction", "checkout_reference": "fake", "amount": 125,
                "credit_amount": 5, "balance": 5, "method": "card"}
    async def confirm(*args):
        return 5
    main.app.dependency_overrides[main.get_authenticated_user_id] = user
    monkeypatch.setattr(main.credits_service, "purchase", purchase)
    monkeypatch.setattr(main.credits_service, "confirm_checkout", confirm)


@pytest.mark.parametrize("path,payload", [
    ("/api/credits/purchase", {"credit_amount": 5}),
    ("/api/credits/confirm", {"pymt_txn_id": "own-transaction"}),
])
def test_simulated_payments_can_be_switched_off(monkeypatch, path, payload):
    _payment_stubs(monkeypatch)
    monkeypatch.setenv("SIMULATED_PAYMENTS_ENABLED", "false")
    try:
        response = TestClient(main.app).post(path, json=payload, headers={"X-User-Id": "user-b"})
        assert response.status_code == 503
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("path,payload", [
    ("/api/credits/purchase", {"credit_amount": 5}),
    ("/api/credits/confirm", {"pymt_txn_id": "own-transaction"}),
])
def test_simulated_payments_stay_on_by_default(monkeypatch, path, payload):
    _payment_stubs(monkeypatch)
    monkeypatch.delenv("SIMULATED_PAYMENTS_ENABLED", raising=False)
    try:
        response = TestClient(main.app).post(path, json=payload, headers={"X-User-Id": "user-b"})
        assert response.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_foreign_manuscript_cannot_be_used_to_start_scan(monkeypatch):
    from types import SimpleNamespace
    class Query:
        def __init__(self, table):
            self.table, self.filters = table, {}
        def select(self, *args): return self
        def eq(self, key, value):
            self.filters[key] = value
            return self
        def insert(self, row): return self
        def execute(self):
            if self.table == "user": return SimpleNamespace(data=[{"user_id": "user-b"}])
            if self.table == "manuscript":
                return SimpleNamespace(data=[{"manuscript_id": "a-manuscript"}] if self.filters.get("user_id", "user-a") == "user-a" else [])
            return SimpleNamespace(data=[])
    class DB:
        def table(self, name): return Query(name)
    async def user(): return "user-b"
    async def balance(*args): return 0
    async def rate(*args): return None
    main.app.dependency_overrides[main.get_authenticated_user_id] = user
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", lambda: DB())
    monkeypatch.setattr(main.credits_service, "get_balance", balance)
    monkeypatch.setattr(main, "_check_scan_rate_limit", rate)
    monkeypatch.setattr(main, "_enqueue_scan", lambda *args: None)
    try:
        response = TestClient(main.app).post("/api/scans/start", json={"user_id": "user-b", "manuscript_id": "a-manuscript"})
        assert response.status_code == 404
    finally:
        main.app.dependency_overrides.clear()
        main._scan_reservations.clear()
        main._scan_reservations_by_id.clear()


def test_debug_routes_disabled_when_flag_unset(monkeypatch):
    import os
    import subprocess
    import sys
    env = os.environ.copy()
    env.pop("ENABLE_TEST_ENDPOINTS", None)
    code = '''
from fastapi.testclient import TestClient
import main
async def fake(url): return "public text"
main.google_doc_text = fake
response = TestClient(main.app).get("/api/test-ingestion", params={"doc_url": "example"})
assert response.status_code == 404, response.status_code
'''
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
