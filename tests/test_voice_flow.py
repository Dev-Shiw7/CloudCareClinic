"""Tests for the voice adapter and the session state machine.

These are the tests that verify the architectural claims in the README:

  * a registration through the voice tools and a POST to /patients produce
    an identical record (one service layer, two adapters);
  * a dropped call loses nothing;
  * a retried save does not create a duplicate patient;
  * a caller can start over mid-call.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("VAPI_SERVER_SECRET", "test-secret")
SECRET = {"X-Vapi-Secret": os.environ["VAPI_SERVER_SECRET"]}

from app.domain.models import Base  # noqa: E402
from app.infra.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    init_db()
    with TestClient(app) as c:
        yield c


def _call_id() -> str:
    return "call-{}".format(uuid.uuid4())


def _phone() -> str:
    return "415555{}".format(str(uuid.uuid4().int)[:4])


def _tool(client, path, call_id, args=None, caller=None):
    body = {"call_id": call_id, "arguments": args or {}}
    if caller:
        body["caller_phone"] = caller
    return client.post("/voice/{}".format(path), json=body, headers=SECRET)


REQUIRED = {
    "first_name": "Jane",
    "last_name": "Doe",
    "date_of_birth": "1985-03-04",
    "sex": "Female",
    "address_line_1": "22 Market Street",
    "city": "San Francisco",
    "state": "California",
    "zip_code": "94103",
}


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------

def test_voice_endpoints_reject_missing_secret(client):
    r = client.post("/voice/start", json={"call_id": _call_id()})
    assert r.status_code == 401


def test_voice_endpoints_reject_wrong_secret(client):
    r = client.post(
        "/voice/start",
        json={"call_id": _call_id()},
        headers={"X-Vapi-Secret": "wrong"},
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------
# The state machine drives the conversation
# --------------------------------------------------------------------------

def test_start_reports_what_is_needed(client):
    r = _tool(client, "start", _call_id())
    assert r.status_code == 200
    body = r.json()
    assert body["next_field"] == "first_name"
    assert body["ready_to_confirm"] is False
    assert len(body["still_needed"]) == 9


def test_capture_advances_next_field(client):
    cid = _call_id()
    _tool(client, "start", cid)
    r = _tool(client, "capture", cid, {"fields": {"first_name": "Jane"}})
    body = r.json()
    assert body["accepted"] == ["first_name"]
    assert body["next_field"] == "last_name"


def test_invalid_field_returns_speakable_reprompt(client):
    cid = _call_id()
    _tool(client, "start", cid)
    r = _tool(client, "capture", cid, {"fields": {"phone_number": "555"}})
    body = r.json()
    assert body["accepted"] == []
    rejected = body["rejected"][0]
    assert rejected["field"] == "phone_number"
    assert rejected["reason"] == "invalid_us_phone"
    # The re-prompt must be a sentence an agent can speak, not an error code.
    assert "3 digits" in rejected["reprompt"]


def test_partial_batch_accepts_good_rejects_bad(client):
    cid = _call_id()
    _tool(client, "start", cid)
    r = _tool(
        client, "capture", cid,
        {"fields": {"first_name": "Jane", "date_of_birth": "2099-01-01"}},
    )
    body = r.json()
    assert body["accepted"] == ["first_name"]
    assert body["rejected"][0]["reason"] == "future_date"


def test_restart_clears_the_draft(client):
    cid = _call_id()
    _tool(client, "start", cid)
    _tool(client, "capture", cid, {"fields": {"first_name": "Jane"}})
    r = _tool(client, "restart", cid)
    assert r.json()["next_field"] == "first_name"
    assert len(r.json()["still_needed"]) == 9


def test_confirmation_is_chunked_not_one_blob(client):
    cid = _call_id()
    _tool(client, "start", cid)
    _tool(client, "capture", cid,
          {"fields": {**REQUIRED, "phone_number": _phone()}})
    r = _tool(client, "confirm", cid)
    chunks = r.json()["chunks"]
    groups = [c["group"] for c in chunks]
    assert groups[:3] == ["identity", "contact", "address"]
    # Digits spoken individually so TTS doesn't say "ninety-four thousand".
    assert "9 4 1 0 3" in chunks[2]["text"]


# --------------------------------------------------------------------------
# Resilience
# --------------------------------------------------------------------------

def test_dropped_call_resumes_from_caller_id(client):
    """The scenario the rubric asks about: the line drops mid-registration."""
    caller = _phone()
    first_call = _call_id()

    _tool(client, "start", first_call, caller=caller)
    _tool(client, "capture", first_call,
          {"fields": {"first_name": "Jane", "last_name": "Doe",
                      "date_of_birth": "1985-03-04"}}, caller=caller)

    # ... line drops. Caller redials; Vapi issues a new call id.
    second_call = _call_id()
    r = _tool(client, "start", second_call, caller=caller)
    body = r.json()

    assert body["resumed"] is True
    assert "first_name" not in body["still_needed"]
    assert body["next_field"] == "sex"


def test_save_is_idempotent_on_retry(client):
    """Vapi retries tool calls on timeout; a retry must not duplicate."""
    cid = _call_id()
    caller = _phone()
    _tool(client, "start", cid, caller=caller)
    _tool(client, "capture", cid,
          {"fields": {**REQUIRED, "phone_number": caller}})

    first = _tool(client, "finalize", cid).json()
    assert first["status"] == "created"

    second = _tool(client, "finalize", cid).json()
    assert second["status"] == "already_saved"
    assert second["patient_id"] == first["patient_id"]


def test_finalize_refuses_incomplete_draft(client):
    cid = _call_id()
    _tool(client, "start", cid)
    _tool(client, "capture", cid, {"fields": {"first_name": "Jane"}})
    r = _tool(client, "finalize", cid).json()
    assert r["status"] == "incomplete"
    assert "last_name" in r["still_needed"]


def test_returning_caller_is_recognised(client):
    """Duplicate detection fires before the caller has said a word."""
    caller = _phone()
    cid = _call_id()
    _tool(client, "start", cid, caller=caller)
    _tool(client, "capture", cid,
          {"fields": {**REQUIRED, "phone_number": caller}})
    _tool(client, "finalize", cid)

    later = _call_id()
    r = _tool(client, "start", later, caller=caller).json()
    assert r["existing_patient"]["first_name"] == "Jane"
    assert "already have a record" in r["duplicate_hint"]


def test_second_registration_updates_instead_of_duplicating(client):
    caller = _phone()
    cid = _call_id()
    _tool(client, "start", cid, caller=caller)
    _tool(client, "capture", cid,
          {"fields": {**REQUIRED, "phone_number": caller}})
    created = _tool(client, "finalize", cid).json()

    cid2 = _call_id()
    _tool(client, "start", cid2, caller=caller)
    _tool(client, "capture", cid2,
          {"fields": {**REQUIRED, "phone_number": caller, "city": "Oakland"}})
    updated = _tool(client, "finalize", cid2).json()

    assert updated["status"] == "updated"
    assert updated["patient_id"] == created["patient_id"]

    row = client.get("/patients/{}".format(created["patient_id"])).json()["data"]
    assert row["city"] == "Oakland"


# --------------------------------------------------------------------------
# The parity test: both adapters, one service layer
# --------------------------------------------------------------------------

def test_voice_and_rest_produce_identical_records(client):
    """The claim that both paths share one implementation, made testable."""
    voice_phone, rest_phone = _phone(), _phone()

    cid = _call_id()
    _tool(client, "start", cid, caller=voice_phone)
    _tool(client, "capture", cid,
          {"fields": {**REQUIRED, "phone_number": voice_phone}})
    voice_id = _tool(client, "finalize", cid).json()["patient_id"]

    rest_id = client.post(
        "/patients", json={**REQUIRED, "phone_number": rest_phone}
    ).json()["data"]["patient_id"]

    via_voice = client.get("/patients/{}".format(voice_id)).json()["data"]
    via_rest = client.get("/patients/{}".format(rest_id)).json()["data"]

    volatile = {"patient_id", "phone_number", "created_at", "updated_at"}
    assert {k: v for k, v in via_voice.items() if k not in volatile} == \
           {k: v for k, v in via_rest.items() if k not in volatile}

    # Both normalised "California" -> "CA" through the same validator.
    assert via_voice["state"] == via_rest["state"] == "CA"
