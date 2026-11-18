"""Uploading proof of payment for a pending credit purchase."""

import asyncio

import pytest
from fastapi import HTTPException

import main
from services import credits
from services.db_service import DatabasePersistenceService


class Table:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.row, self.change = db, name, {}, None, None

    def select(self, *_):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def update(self, change):
        self.change = change
        return self

    def execute(self):
        rows = self.db.rows[self.name]
        matches = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        for row in matches:
            row.update(self.change or {})
        return type("R", (), {"data": matches})()


class DB:
    def __init__(self):
        self.rows = {"pymt_txn": [{"pymt_txn_id": "txn1", "user_id": "me", "pymt_txn_provider": "simulated"}]}
        self.storage = self
        self.uploads = []
        self.removed = []

    def from_(self, bucket):
        assert bucket == "payment-proofs"
        return self

    def upload(self, path, data, options):
        self.uploads.append((path, data, options))

    def remove(self, paths):
        self.removed.extend(paths)

    def table(self, name):
        return Table(self, name)


def request(**changes):
    values = dict(pymt_txn_id="txn1", method="gcash", reference_number="1234 567 890", payer_name="Synthetic Tester",
                  file_name="receipt.png", content_type="image/png", proof_data="aGVsbG8=")
    return main.PaymentProofRequest(**{**values, **changes})


@pytest.fixture
def db(monkeypatch):
    database = DB()
    monkeypatch.setattr(credits.DatabasePersistenceService, "get_client", lambda: database)
    return database


def upload(body, user="me"):
    return asyncio.run(main.upload_payment_proof(body, x_user_id=user, authenticated_user_id=user))


def test_proof_is_saved_with_the_transaction_and_marks_the_method(db):
    response = upload(request())
    assert response.status == "received"
    import uuid
    uuid.UUID(response.proof_id)
    path = f"me/txn1/{response.proof_id}.png"
    assert db.uploads == [(path, b"hello", {"content-type": "image/png", "upsert": "true"})]
    saved = db.rows["pymt_txn"][0]
    assert {k: saved[k] for k in ("pymt_txn_proof_method", "pymt_txn_proof_reference",
                                   "pymt_txn_proof_payer_name", "pymt_txn_proof_file_path")} == {
        "pymt_txn_proof_method": "gcash", "pymt_txn_proof_reference": "1234 567 890",
        "pymt_txn_proof_payer_name": "Synthetic Tester", "pymt_txn_proof_file_path": path}
    assert saved["pymt_txn_proof_uploaded_at"]
    assert saved["pymt_txn_provider"] == "gcash"
    upload(request(proof_data="d29ybGQ="))
    assert len(db.rows["pymt_txn"]) == 1 and db.uploads[-1][1] == b"world"
    upload(request(content_type="application/pdf", file_name="receipt.pdf"))
    assert db.uploads[-1][0].startswith("me/txn1/") and db.uploads[-1][0].endswith(".pdf")
    assert len({item[0] for item in db.uploads}) == 3 and db.removed == []


def test_someone_elses_transaction_is_not_found(db):
    with pytest.raises(HTTPException) as error:
        upload(request(), user="other")
    assert error.value.status_code == 403 or error.value.status_code == 404
    assert db.uploads == []


def test_only_pictures_and_pdfs_are_accepted(db):
    with pytest.raises(HTTPException) as error:
        upload(request(content_type="application/zip"))
    assert error.value.status_code == 422


def test_a_proof_over_two_megabytes_is_refused(db):
    with pytest.raises(HTTPException) as error:
        upload(request(proof_data="A" * (3 * 1024 * 1024)))
    assert error.value.status_code == 413
    assert db.uploads == []


def test_invalid_base64_is_refused_before_upload(db):
    with pytest.raises(HTTPException) as error:
        upload(request(proof_data="not-base64!"))
    assert error.value.status_code == 422
    assert db.uploads == []


def test_proof_client_requires_service_role(monkeypatch):
    monkeypatch.setattr(DatabasePersistenceService, "_client", None)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-only")
    with pytest.raises(ValueError, match="SUPABASE_SERVICE_ROLE_KEY"):
        DatabasePersistenceService.get_client()


def test_a_reference_number_with_odd_characters_is_refused():
    with pytest.raises(Exception):
        request(reference_number="<script>")
    with pytest.raises(Exception):
        request(reference_number="12")
    with pytest.raises(Exception):
        request(method="paypal")
