"""Uploading proof of payment for a pending credit purchase."""

import asyncio

import pytest
from fastapi import HTTPException

import main
from services import credits


class Table:
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

    def execute(self):
        rows = self.db.rows[self.name]
        if self.row is not None:
            rows.append(self.row)
            return type("R", (), {"data": [self.row]})()
        matches = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        for row in matches:
            row.update(self.change or {})
        return type("R", (), {"data": matches})()


class DB:
    def __init__(self):
        self.rows = {"pymt_txn": [{"pymt_txn_id": "txn1", "user_id": "me", "pymt_txn_provider": "simulated"}], "payment_proof": []}

    def table(self, name):
        return Table(self, name)


def request(**changes):
    values = dict(pymt_txn_id="txn1", method="gcash", reference_number="1234 567 890", payer_name="Sheena",
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
    saved = db.rows["payment_proof"][0]
    assert saved["reference_number"] == "1234 567 890" and saved["proof_data"] == "data:image/png;base64,aGVsbG8="
    assert db.rows["pymt_txn"][0]["pymt_txn_provider"] == "gcash"


def test_someone_elses_transaction_is_not_found(db):
    with pytest.raises(HTTPException) as error:
        upload(request(), user="other")
    assert error.value.status_code == 403 or error.value.status_code == 404


def test_only_pictures_and_pdfs_are_accepted(db):
    with pytest.raises(HTTPException) as error:
        upload(request(content_type="application/zip"))
    assert error.value.status_code == 422


def test_a_proof_over_two_megabytes_is_refused(db):
    with pytest.raises(HTTPException) as error:
        upload(request(proof_data="A" * (3 * 1024 * 1024)))
    assert error.value.status_code == 413


def test_a_reference_number_with_odd_characters_is_refused():
    with pytest.raises(Exception):
        request(reference_number="<script>")
    with pytest.raises(Exception):
        request(reference_number="12")
    with pytest.raises(Exception):
        request(method="paypal")
