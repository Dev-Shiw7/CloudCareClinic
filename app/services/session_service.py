"""Call session lifecycle - the server-side conversational state machine.

This is the inversion at the heart of the design. The LLM does not decide
what to ask next and does not hold the collected data; it renders whatever
this module tells it. Each captured field is validated and persisted the
moment it is spoken, so:

  * a dropped call loses nothing (the draft survives, keyed on caller ID),
  * validation cannot be skipped by a confused model,
  * "start over" is a single delete,
  * and every call leaves an auditable trail.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import CallSession, Patient
from app.domain.validators import (
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    normalize_phone,
    spoken_digits,
)
from app.services import patient_service

logger = logging.getLogger(__name__)

# A draft older than this is not offered for resume - a caller returning the
# next day should start fresh rather than inherit a stale half-record.
RESUME_WINDOW = timedelta(minutes=30)

# Order the agent should collect required fields in. Grouped so the
# conversation flows naturally (identity -> contact -> address).
COLLECTION_ORDER = [
    "first_name", "last_name", "date_of_birth", "sex", "phone_number",
    "address_line_1", "city", "state", "zip_code",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_session(
    session: Session, call_id: str, caller_phone: str | None = None
) -> CallSession:
    """Fetch the session for this call, creating it on first contact."""
    call = session.get(CallSession, call_id)
    if call is not None:
        return call

    call = CallSession(
        call_id=call_id,
        caller_phone=normalize_phone(caller_phone or "") if caller_phone else None,
        draft={},
        status="in_progress",
        transcript=[],
    )
    session.add(call)
    session.commit()
    session.refresh(call)
    logger.info(
        "call.started",
        extra={"call_id": call_id, "caller_phone": call.caller_phone},
    )
    return call


def find_resumable_draft(
    session: Session, caller_phone: str, exclude_call_id: str
) -> Optional[CallSession]:
    """Most recent in-progress draft for this caller ID, within the window.

    Identity is caller ID alone - a deliberate trade-off for demo smoothness,
    documented in docs/DECISIONS.md. Caller ID is spoofable and shared lines
    exist, so a production build would confirm one known field before
    resuming.
    """
    normalized = normalize_phone(caller_phone or "")
    if not normalized:
        return None

    cutoff = _now() - RESUME_WINDOW
    stmt = (
        select(CallSession)
        .where(
            CallSession.caller_phone == normalized,
            CallSession.status == "in_progress",
            CallSession.call_id != exclude_call_id,
            CallSession.updated_at >= cutoff,
        )
        .order_by(CallSession.updated_at.desc())
        .limit(1)
    )
    candidate = session.execute(stmt).scalar_one_or_none()
    # Only worth resuming if something was actually captured.
    if candidate is not None and candidate.draft:
        return candidate
    return None


def adopt_draft(
    session: Session, call: CallSession, previous: CallSession
) -> CallSession:
    """Carry a previous call's draft into the current call."""
    merged = dict(previous.draft or {})
    merged.update(call.draft or {})
    call.draft = merged
    previous.status = "abandoned"
    previous.outcome_note = "Superseded by call {}".format(call.call_id)
    session.commit()
    session.refresh(call)
    logger.info(
        "call.resumed",
        extra={
            "call_id": call.call_id,
            "resumed_from": previous.call_id,
            "fields": sorted(merged.keys()),
        },
    )
    return call


def capture_fields(
    session: Session, call: CallSession, fields: dict[str, Any]
) -> dict[str, Any]:
    """Validate and persist a batch of fields, then report state back.

    The return value is the agent's entire instruction set for the next turn:
    what was accepted, what was rejected (with a speakable re-prompt), and
    what still needs collecting.
    """
    cleaned, failures = patient_service.validate_payload(
        fields, require_all=False
    )

    if cleaned:
        draft = dict(call.draft or {})
        draft.update(cleaned)
        call.draft = draft
        call.updated_at = _now()
        session.commit()
        session.refresh(call)

    logger.info(
        "call.captured",
        extra={
            "call_id": call.call_id,
            "accepted": sorted(cleaned.keys()),
            "rejected": [f.reason for f in failures],
        },
    )

    return {
        "accepted": sorted(cleaned.keys()),
        "rejected": [
            {
                "field": f.field,
                "reason": f.reason,
                "reprompt": f.reprompt,
            }
            for f in failures
        ],
        **progress(call),
    }


def progress(call: CallSession) -> dict[str, Any]:
    """What the agent still needs, and what to ask for next."""
    draft = call.draft or {}
    missing = [f for f in COLLECTION_ORDER if draft.get(f) in (None, "")]
    optional_missing = [f for f in OPTIONAL_FIELDS if draft.get(f) in (None, "")]

    return {
        "still_needed": missing,
        "next_field": missing[0] if missing else None,
        "ready_to_confirm": not missing,
        "optional_not_yet_offered": optional_missing,
        "progress": "{} of {} required fields captured".format(
            len(REQUIRED_FIELDS) - len(missing), len(REQUIRED_FIELDS)
        ),
    }


def clear_draft(session: Session, call: CallSession) -> dict[str, Any]:
    """Caller asked to start over."""
    call.draft = {}
    call.updated_at = _now()
    session.commit()
    session.refresh(call)
    logger.info("call.restarted", extra={"call_id": call.call_id})
    return progress(call)


def confirmation_script(call: CallSession) -> dict[str, Any]:
    """Build a chunked read-back.

    Deliberately NOT one long recital. Three short groups with a pause
    between each is far easier to follow on a phone and lets the caller
    correct one group without re-hearing everything.
    """
    d = call.draft or {}

    def spoken_dob(value: str | None) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value).date()
        except (ValueError, TypeError):
            return str(value)
        return "{} {}, {}".format(parsed.strftime("%B"), parsed.day, parsed.year)

    def spoken_phone(value: str | None) -> str:
        if not value:
            return ""
        digits = value.replace("+1", "")
        return "{}, {}, {}".format(
            spoken_digits(digits[:3]),
            spoken_digits(digits[3:6]),
            spoken_digits(digits[6:]),
        )

    chunks = []

    identity = "{} {}, born {}".format(
        d.get("first_name", ""), d.get("last_name", ""),
        spoken_dob(d.get("date_of_birth")),
    ).strip()
    if d.get("sex"):
        identity += ", {}".format(d["sex"].lower())
    chunks.append({"group": "identity", "text": identity})

    contact = "Phone number {}".format(spoken_phone(d.get("phone_number")))
    if d.get("email"):
        contact += ", email {}".format(d["email"])
    chunks.append({"group": "contact", "text": contact})

    address_parts = [d.get("address_line_1", "")]
    if d.get("address_line_2"):
        address_parts.append(d["address_line_2"])
    address_parts.append(
        "{}, {} {}".format(
            d.get("city", ""), d.get("state", ""),
            spoken_digits(d.get("zip_code", "")),
        )
    )
    chunks.append(
        {"group": "address", "text": ", ".join(p for p in address_parts if p)}
    )

    extras = []
    if d.get("insurance_provider"):
        extra = "Insurance {}".format(d["insurance_provider"])
        if d.get("insurance_member_id"):
            extra += ", member ID {}".format(
                spoken_digits(d["insurance_member_id"])
            )
        extras.append(extra)
    if d.get("emergency_contact_name"):
        extras.append(
            "Emergency contact {}".format(d["emergency_contact_name"])
        )
    if d.get("preferred_language") and d["preferred_language"] != "English":
        extras.append("Preferred language {}".format(d["preferred_language"]))
    if extras:
        chunks.append({"group": "extras", "text": ". ".join(extras)})

    return {
        "chunks": chunks,
        "instruction": (
            "Read each chunk as a separate sentence, pausing briefly between "
            "them. After the last chunk ask: 'Does all of that sound right?'"
        ),
        **progress(call),
    }


def finalize(
    session: Session, call: CallSession, mode: str = "auto"
) -> dict[str, Any]:
    """Promote the draft to a patient record.

    Idempotent: Vapi retries tool calls on timeout, and a retry must not
    create a second patient. If this session already produced a patient we
    return that same record.
    """
    if call.patient_id is not None:
        patient = session.get(Patient, call.patient_id)
        if patient is not None:
            return {
                "status": "already_saved",
                "patient_id": str(patient.patient_id),
                "first_name": patient.first_name,
                "message": "This registration was already saved.",
            }

    draft = dict(call.draft or {})
    state = progress(call)
    if state["still_needed"]:
        return {
            "status": "incomplete",
            "still_needed": state["still_needed"],
            "next_field": state["next_field"],
            "message": (
                "I still need a few details before I can save this."
            ),
        }

    existing = patient_service.find_active_by_phone(
        session, draft.get("phone_number", "")
    )

    try:
        if existing is not None and mode in ("auto", "update"):
            patient = patient_service.update_patient(
                session, existing.patient_id, draft, source="voice"
            )
            action = "updated"
        else:
            patient = patient_service.create_patient(
                session, draft, source="voice"
            )
            action = "created"
    except patient_service.ValidationError as exc:
        # Should be unreachable - everything in the draft already passed the
        # same validators - but a caller must never hear silence.
        logger.exception("call.finalize_validation_failed",
                         extra={"call_id": call.call_id})
        return {
            "status": "invalid",
            "rejected": [
                {"field": f.field, "reason": f.reason, "reprompt": f.reprompt}
                for f in exc.failures
            ],
            "message": "A couple of details need fixing before I can save.",
        }
    except Exception:
        logger.exception("call.finalize_failed",
                         extra={"call_id": call.call_id})
        return {
            "status": "error",
            "message": (
                "I'm having trouble saving that to our system right now. Your "
                "details are safe - someone from our office will follow up to "
                "finish your registration."
            ),
        }

    call.patient_id = patient.patient_id
    call.status = "completed"
    call.outcome_note = "Patient {} via voice".format(action)
    session.commit()

    logger.info(
        "call.completed",
        extra={
            "call_id": call.call_id,
            "patient_id": str(patient.patient_id),
            "action": action,
            "payload": patient_service.to_dict(patient),
        },
    )

    return {
        "status": action,
        "patient_id": str(patient.patient_id),
        "first_name": patient.first_name,
        "message": "You're all set, {}.".format(patient.first_name),
    }


def append_transcript(
    session: Session, call: CallSession, role: str, text: str
) -> None:
    """Append one turn to the stored transcript."""
    entries = list(call.transcript or [])
    entries.append({"role": role, "text": text, "at": _now().isoformat()})
    call.transcript = entries
    session.commit()


def mark_abandoned(session: Session, call_id: str, note: str = "") -> None:
    """Called on end-of-call when no patient was created."""
    call = session.get(CallSession, call_id)
    if call is None or call.status != "in_progress":
        return
    call.status = "abandoned"
    call.outcome_note = note or "Call ended before registration completed"
    session.commit()
    logger.info(
        "call.abandoned",
        extra={"call_id": call_id, "captured": sorted((call.draft or {}).keys())},
    )
