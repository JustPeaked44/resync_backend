import asyncio
import json
import threading
import time
import pytest
from fastapi import HTTPException

import main
from services import credits


def test_two_concurrent_unlocks_charge_once(monkeypatch):
    state = {"owner": "user", "balance": 1, "unlocked": False, "charges": 0}
    lock = threading.Lock()

    class Request:
        def __init__(self, args):
            self.args = args

        def execute(self):
            with lock:
                time.sleep(0.01)
                if self.args["p_user_id"] != state["owner"]:
                    raise RuntimeError("SCAN_NOT_FOUND")
                if state["unlocked"]:
                    value = {"unlocked": True, "charged": False, "credits_remaining": state["balance"]}
                elif self.args["p_charge"] and state["balance"] == 0:
                    raise RuntimeError("INSUFFICIENT_CREDITS")
                else:
                    state["charges"] += int(self.args["p_charge"])
                    state["balance"] -= int(self.args["p_charge"])
                    state["unlocked"] = True
                    value = {"unlocked": True, "charged": bool(self.args["p_charge"]),
                             "credits_remaining": state["balance"]}
                return type("Response", (), {"data": value})()

    class DB:
        def rpc(self, name, args):
            assert name == "unlock_scan_report" and args["p_analysis_run_id"] == "scan"
            return Request(args)

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    monkeypatch.setattr(credits, "CREDITS_ENABLED", True)

    async def pair():
        return await asyncio.gather(main.unlock_scan("scan", "user"), main.unlock_scan("scan", "user"))

    responses = asyncio.run(pair())
    assert sorted(response["charged"] for response in responses) == [False, True]
    assert [response["credits_remaining"] for response in responses] == [0, 0]
    assert state == {"owner": "user", "balance": 0, "unlocked": True, "charges": 1}
    assert asyncio.run(main.unlock_scan("scan", "user")) == {
        "unlocked": True, "charged": False, "credits_remaining": 0}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.unlock_scan("scan", "other"))
    assert exc.value.status_code == 404

    state["unlocked"] = False
    denied = asyncio.run(main.unlock_scan("scan", "user"))
    assert denied.status_code == 402
    assert json.loads(denied.body) == {
        "detail": "Not enough credits to unlock this report.", "credits_remaining": 0}
    assert state["charges"] == 1


def test_unlock_kill_switch_does_not_charge(monkeypatch):
    called = {}

    class Request:
        def execute(self):
            return type("Response", (), {"data": {"unlocked": True, "charged": False,
                                                   "credits_remaining": 0}})()

    class DB:
        def rpc(self, name, args):
            called.update(args)
            return Request()

    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", DB)
    monkeypatch.setattr(credits, "CREDITS_ENABLED", False)
    assert asyncio.run(main.unlock_scan("scan", "user")) == {
        "unlocked": True, "charged": False, "credits_remaining": 0}
    assert called == {"p_user_id": "user", "p_analysis_run_id": "scan", "p_charge": False}


def _failing_db(message):
    class Request:
        def execute(self):
            raise RuntimeError(message)

    class DB:
        def rpc(self, name, args):
            return Request()

    return DB


def test_unlocking_a_scan_that_is_still_running_is_a_409(monkeypatch):
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", _failing_db("SCAN_NOT_READY"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.unlock_scan("scan", "user"))
    assert exc.value.status_code == 409


def test_an_unexpected_database_error_is_a_503_not_a_charge(monkeypatch):
    monkeypatch.setattr(main.DatabasePersistenceService, "get_client", _failing_db("connection reset"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.unlock_scan("scan", "user"))
    assert exc.value.status_code == 503


# ---- buying credits: a checkout that is never confirmed adds nothing ----------------------------------------------

class _Table:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.row, self.change = db, name, {}, None, None

    def select(self, *_):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def insert(self, row):
        self.row = row
        return self

    def update(self, change):
        self.change = change
        return self

    def order(self, *_, **__):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.name, [])
        if self.row is not None:
            rows.append(dict(self.row))
            return type("R", (), {"data": [self.row]})()
        found = [row for row in rows if all(row.get(k) == v for k, v in self.filters.items())]
        for row in found:
            row.update(self.change or {})
        return type("R", (), {"data": found})()


class _Wallet:
    def __init__(self):
        self.tables, self.balance, self.purchases = {}, 0, []

    def table(self, name):
        return _Table(self, name)

    def rpc(self, name, args):
        wallet = self

        class Call:
            def execute(self_inner):
                assert name == "purchase_credits"
                key = args["p_idempotency_key"]
                if key not in wallet.purchases:
                    wallet.purchases.append(key)
                    wallet.balance += args["p_amount"]
                return type("R", (), {"data": wallet.balance})()
        return Call()


@pytest.fixture
def wallet(monkeypatch):
    db = _Wallet()
    monkeypatch.setattr(credits.DatabasePersistenceService, "get_client", lambda: db)
    monkeypatch.setattr(credits, "CREDITS_ENABLED", True)
    return db


def test_a_checkout_that_is_never_confirmed_adds_no_credits(wallet):
    checkout = asyncio.run(credits.credits_service.create_checkout("user", 5, unit_price=20.0))
    assert checkout["amount"] == 100.0 and wallet.balance == 0
    assert wallet.tables["pymt_txn"][0]["pymt_txn_status"] == "pending"


def test_confirming_credits_the_wallet_once_even_if_asked_twice(wallet):
    checkout = asyncio.run(credits.credits_service.create_checkout("user", 5))
    asyncio.run(credits.credits_service.confirm_checkout("user", checkout["pymt_txn_id"]))
    asyncio.run(credits.credits_service.confirm_checkout("user", checkout["pymt_txn_id"]))
    assert wallet.balance == 5 and len(wallet.purchases) == 1
    assert wallet.tables["pymt_txn"][0]["pymt_txn_status"] == "paid"


def test_someone_else_cannot_confirm_my_checkout(wallet):
    checkout = asyncio.run(credits.credits_service.create_checkout("user", 5))
    with pytest.raises(ValueError):
        asyncio.run(credits.credits_service.confirm_checkout("intruder", checkout["pymt_txn_id"]))
    assert wallet.balance == 0


# ---- the one-step purchase: the server sets the price and the wallet really grows ------------------------------

def test_a_purchase_records_the_transaction_and_credits_the_wallet(wallet):
    result = asyncio.run(credits.credits_service.purchase("user", 20, "card"))
    assert result["amount"] == 450.0 and result["balance"] == 20 and result["checkout_reference"].startswith("RS-")
    assert wallet.tables["pymt_txn"][0]["pymt_txn_status"] == "paid" and wallet.tables["pymt_txn"][0]["pymt_txn_credits"] == 20


def test_the_price_cannot_be_chosen_by_the_buyer(wallet):
    with pytest.raises(ValueError):
        asyncio.run(credits.credits_service.purchase("user", 7, "card"))   # not a package
    assert wallet.balance == 0


def test_a_purchase_that_fails_midway_adds_nothing_and_is_marked_failed(wallet, monkeypatch):
    def broken_rpc(name, args):
        raise RuntimeError("database down")
    monkeypatch.setattr(wallet, "rpc", broken_rpc)
    with pytest.raises(RuntimeError):
        asyncio.run(credits.credits_service.purchase("user", 5, "gcash"))
    assert wallet.balance == 0 and wallet.tables["pymt_txn"][0]["pymt_txn_status"] == "failed"


def test_a_purchase_still_works_when_the_table_has_no_user_column(wallet, monkeypatch):
    real_table = wallet.table

    def table(name):
        handle = real_table(name)
        original = handle.insert

        def insert(row):
            if name == "pymt_txn" and "user_id" in row:
                raise RuntimeError("Could not find the 'user_id' column of 'pymt_txn' in the schema cache")
            return original(row)
        handle.insert = insert
        return handle
    monkeypatch.setattr(wallet, "table", table)
    assert asyncio.run(credits.credits_service.purchase("user", 5, "card"))["balance"] == 5


def test_a_purchase_with_proof_saves_the_proof_first(wallet):
    proof = {"reference_number": "1234 567", "payer_name": "Sheena", "file_name": "r.png", "content_type": "image/png",
             "proof_data": "data:image/png;base64,aGk="}
    asyncio.run(credits.credits_service.purchase("user", 5, "gcash", proof))
    assert wallet.tables["payment_proof"][0]["reference_number"] == "1234 567" and wallet.balance == 5


def test_the_endpoint_refuses_someone_elses_account(wallet):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.purchase_credits(main.PurchaseRequest(credit_amount=5), x_user_id="victim", authenticated_user_id="me"))
    assert exc.value.status_code in (401, 403)


def test_a_purchase_is_filed_under_the_buyers_subscription(wallet):
    wallet.tables["subscription"] = [{"subscription_id": "sub-1", "user_id": "user"}]
    asyncio.run(credits.credits_service.purchase("user", 5, "card"))
    row = wallet.tables["pymt_txn"][0]
    assert row["subscription_id"] == "sub-1" and row["pymt_txn_created_at"]


def test_the_old_credits_enabled_setting_no_longer_turns_credits_off(monkeypatch):
    import importlib
    monkeypatch.setenv("CREDITS_ENABLED", "false")
    monkeypatch.delenv("CREDITS_UNLIMITED_FOR_TESTING", raising=False)
    assert importlib.reload(credits).CREDITS_ENABLED is True
    monkeypatch.setenv("CREDITS_UNLIMITED_FOR_TESTING", "true")
    assert importlib.reload(credits).CREDITS_ENABLED is False
    monkeypatch.delenv("CREDITS_UNLIMITED_FOR_TESTING")
    importlib.reload(credits)


def test_a_purchase_goes_through_even_if_the_proof_cannot_be_stored(wallet, monkeypatch):
    real_table = wallet.table

    def table(name):
        handle = real_table(name)
        if name == "payment_proof":
            def broken(row):
                raise RuntimeError("Could not find the table 'public.payment_proof' in the schema cache")
            handle.insert = broken
        return handle
    monkeypatch.setattr(wallet, "table", table)
    proof = {"reference_number": "TEST", "payer_name": None, "file_name": "r.png", "content_type": "image/png",
             "proof_data": "data:image/png;base64,aGk="}
    assert asyncio.run(credits.credits_service.purchase("user", 5, "gcash", proof))["balance"] == 5
