"""Integration tests for the REST adapter.

Run against a throwaway Postgres:
    docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=pw \
        --name intake-test postgres:16-alpine
    DATABASE_URL=postgresql://postgres:pw@localhost:55432/postgres pytest
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("VAPI_SERVER_SECRET", "test-secret")

from app.infra.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.domain.models import Base  # noqa: E402


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    init_db()
    with TestClient(app) as c:
        yield c


def _valid_patient(**overrides):
    # Unique phone per call so the active-phone unique index doesn't collide.
    tail = str(uuid.uuid4().int)[:4]
    payload = {
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": "1985-03-04",
        "sex": "Female",
        "phone_number": "415555{}".format(tail),
        "address_line_1": "22 Market Street",
        "city": "San Francisco",
        "state": "CA",
        "zip_code": "94103",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Envelope + status codes
# --------------------------------------------------------------------------

def test_create_returns_201_and_envelope(client):
    r = client.post("/patients", json=_valid_patient())
    assert r.status_code == 201
    body = r.json()
    assert body["error"] is None
    assert body["data"]["first_name"] == "Jane"
    assert uuid.UUID(body["data"]["patient_id"])
    assert body["data"]["created_at"] is not None
    # Default applied server-side.
    assert body["data"]["preferred_language"] == "English"


def test_phone_is_normalised_to_e164(client):
    r = client.post(
        "/patients", json=_valid_patient(phone_number="(415) 555-0188")
    )
    assert r.status_code == 201
    assert r.json()["data"]["phone_number"] == "+14155550188"


def test_missing_required_field_returns_422(client):
    payload = _valid_patient()
    del payload["last_name"]
    r = client.post("/patients", json=payload)
    assert r.status_code == 422
    fields = [f["field"] for f in r.json()["error"]["fields"]]
    assert "last_name" in fields


def test_future_dob_rejected_server_side(client):
    r = client.post("/patients", json=_valid_patient(date_of_birth="2099-01-01"))
    assert r.status_code == 422
    reasons = [f["reason"] for f in r.json()["error"]["fields"]]
    assert "future_date" in reasons


def test_short_phone_rejected_server_side(client):
    r = client.post("/patients", json=_valid_patient(phone_number="555"))
    assert r.status_code == 422
    reasons = [f["reason"] for f in r.json()["error"]["fields"]]
    assert "invalid_us_phone" in reasons


def test_invalid_state_rejected(client):
    r = client.post("/patients", json=_valid_patient(state="ZZ"))
    assert r.status_code == 422


def test_unknown_field_is_rejected_not_ignored(client):
    r = client.post("/patients", json=_valid_patient(ssn="123-45-6789"))
    assert r.status_code == 422
    fields = [f["field"] for f in r.json()["error"]["fields"]]
    assert "ssn" in fields


def test_duplicate_active_phone_returns_409(client):
    payload = _valid_patient(phone_number="4155550199")
    assert client.post("/patients", json=payload).status_code == 201
    r = client.post("/patients", json=payload)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_patient"


def test_get_missing_patient_returns_404(client):
    r = client.get("/patients/{}".format(uuid.uuid4()))
    assert r.status_code == 404
    assert r.json()["data"] is None


def test_malformed_uuid_returns_404_not_500(client):
    r = client.get("/patients/not-a-uuid")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Query filters
# --------------------------------------------------------------------------

def test_filter_by_last_name(client):
    client.post("/patients", json=_valid_patient(last_name="Kowalski"))
    r = client.get("/patients", params={"last_name": "kowalski"})
    assert r.status_code == 200
    assert all(p["last_name"] == "Kowalski" for p in r.json()["data"])
    assert len(r.json()["data"]) >= 1


def test_filter_by_phone_accepts_any_format(client):
    client.post("/patients", json=_valid_patient(phone_number="4155550177"))
    r = client.get("/patients", params={"phone_number": "(415) 555-0177"})
    assert len(r.json()["data"]) == 1


def test_filter_by_dob(client):
    client.post("/patients", json=_valid_patient(date_of_birth="1970-12-25"))
    r = client.get("/patients", params={"date_of_birth": "12/25/1970"})
    assert len(r.json()["data"]) >= 1


# --------------------------------------------------------------------------
# Update + soft delete
# --------------------------------------------------------------------------

def test_partial_update_only_changes_supplied_fields(client):
    created = client.post("/patients", json=_valid_patient()).json()["data"]
    r = client.put(
        "/patients/{}".format(created["patient_id"]), json={"city": "Oakland"}
    )
    assert r.status_code == 200
    assert r.json()["data"]["city"] == "Oakland"
    assert r.json()["data"]["last_name"] == created["last_name"]


def test_update_validates_too(client):
    created = client.post("/patients", json=_valid_patient()).json()["data"]
    r = client.put(
        "/patients/{}".format(created["patient_id"]),
        json={"date_of_birth": "2099-01-01"},
    )
    assert r.status_code == 422


def test_soft_delete_hides_from_list_but_keeps_row(client):
    created = client.post("/patients", json=_valid_patient()).json()["data"]
    pid = created["patient_id"]

    assert client.delete("/patients/{}".format(pid)).status_code == 200
    assert client.get("/patients/{}".format(pid)).status_code == 404

    # The row still exists, with deleted_at set.
    from sqlalchemy import select
    from app.domain.models import Patient
    from app.infra.db import SessionLocal

    with SessionLocal() as s:
        row = s.execute(
            select(Patient).where(Patient.patient_id == uuid.UUID(pid))
        ).scalar_one()
        assert row.deleted_at is not None


def test_phone_reusable_after_soft_delete(client):
    """The partial unique index excludes soft-deleted rows."""
    payload = _valid_patient(phone_number="4155550166")
    first = client.post("/patients", json=payload).json()["data"]
    client.delete("/patients/{}".format(first["patient_id"]))
    r = client.post("/patients", json=payload)
    assert r.status_code == 201


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["data"]["database"] is True


# --------------------------------------------------------------------------
# Malformed input: 400, distinct from field-level 422
# --------------------------------------------------------------------------

def test_malformed_json_returns_400_in_envelope(client):
    r = client.post(
        "/patients",
        content=b'{"first_name": broken',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["data"] is None
    assert r.json()["error"]["code"] == "bad_request"


def test_bad_query_param_type_returns_400(client):
    r = client.get("/patients", params={"limit": "abc"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_unknown_route_404_keeps_envelope(client):
    r = client.get("/does-not-exist")
    assert r.status_code == 404
    assert r.json()["data"] is None
    assert "error" in r.json()


def test_field_errors_are_still_422_not_400(client):
    """A parseable body with bad values is 422, not 400."""
    r = client.post("/patients", json={"first_name": "A"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
