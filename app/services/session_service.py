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

from app.domain import validators
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

# The only fields worth asking for in one breath. A human intake coordinator
# says "and what city and state?" but never strings five questions together,
# so pairing is an explicit whitelist rather than left to the model's
# judgement - given a list of everything still outstanding, an LLM reliably
# front-loads it and the call stops sounding like a conversation.
FIELD_PAIRS = {
    "first_name": ["first_name", "last_name"],
    "city": ["city", "state"],
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_session(
    session: Session, call_id: str, caller_phone: str | None = None
) -> CallSession:
    """Fetch the session for this call, creating it on first contact.

    ``populate_existing`` forces a re-read rather than trusting whatever the
    identity map holds. The engine uses ``expire_on_commit=False`` over a
    connection pool, so a worker that handled an earlier turn can otherwise
    hand back a cached row whose ``draft`` predates the fields captured
    since - which made finalize see an empty draft and tell the caller it
    still needed everything they had just given.
    """
    call = session.get(CallSession, call_id, populate_existing=True)
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

    # Cross-field checks need the whole address, not just this turn's batch.
    # The caller says the ZIP on its own turn, so validate_payload above sees
    # no `state` to compare it against and cannot fire. Re-run the check
    # against the draft as it would look after this batch, so a ZIP that
    # cannot belong to the state is caught during the address conversation
    # rather than surfacing at the save step - after "saving that now" is far
    # too late to be asking which value was misheard.
    if cleaned:
        prospective = {**(call.draft or {}), **cleaned}
        mismatch = validators.cross_check_address(prospective)
        if mismatch is not None and not any(
            f.field == mismatch.field for f in failures
        ):
            failures.append(mismatch)
            # Keep the state, drop the ZIP: a misheard 5-digit number is far
            # more likely than a misheard state name, and re-asking both at
            # once is what the re-prompt already does.
            cleaned.pop("zip_code", None)

    if cleaned:
        draft = dict(call.draft or {})
        draft.update(cleaned)
        call.draft = draft
        call.updated_at = _now()
        session.commit()
        # No refresh: `draft` is the value we just committed, and every extra
        # round-trip is audible. The database is not necessarily co-located
        # with the app, so each query costs real conversational latency.

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
        **progress(call, rejected_fields=[f.field for f in failures]),
    }


def progress(
    call: CallSession, rejected_fields: list[str] | None = None
) -> dict[str, Any]:
    """What the agent still needs, and what to ask for next.

    ``rejected_fields`` are fields the caller just answered with a value that
    failed validation. They take priority in ``next_field``: the spec requires
    that invalid input be re-prompted *for that specific field*, so the field
    the caller must fix has to stay in front of the agent rather than being
    silently deferred behind the next uncollected one.
    """
    draft = call.draft or {}
    missing = [f for f in COLLECTION_ORDER if draft.get(f) in (None, "")]
    optional_missing = [f for f in OPTIONAL_FIELDS if draft.get(f) in (None, "")]

    # A rejected *required* field is still "missing", so it is already in
    # `missing` - promote it rather than appending a duplicate. A rejected
    # *optional* field (a garbled email, say) is not in `missing` at all, so
    # surface it via retry_field only: it must not block ready_to_confirm,
    # because the caller is always free to skip an optional value.
    retry = [f for f in (rejected_fields or []) if f in missing]
    retry_optional = [
        f for f in (rejected_fields or [])
        if f not in missing and f in OPTIONAL_FIELDS
    ]
    next_field = retry[0] if retry else (missing[0] if missing else None)
    retry_field = (retry + retry_optional)[0] if (retry or retry_optional) else None

    # Ask for one field, or one sanctioned pair - and only if the partner is
    # also still outstanding, so a correction late in the call re-asks just
    # the field that was wrong.
    ask_now = [next_field] if next_field else []
    if next_field and not retry:
        ask_now = [f for f in FIELD_PAIRS.get(next_field, [next_field])
                   if f in missing]

    state: dict[str, Any] = {
        "next_field": next_field,
        "ask_now": ask_now,
        "retry_field": retry_field,
        "ready_to_confirm": not missing,
        "fields_remaining": len(missing),
        "progress": "{} of {} required fields captured".format(
            len(REQUIRED_FIELDS) - len(missing), len(REQUIRED_FIELDS)
        ),
    }

    # `still_needed` is the full outstanding list. It is deliberately withheld
    # while collection is in flight: handed the whole form, the model asks for
    # all of it at once. It only appears once nothing is outstanding (so a
    # failed save can name what is missing) - see docs/DECISIONS.md.
    if not missing:
        state["still_needed"] = missing
        # Optional extras are offered once, at the end, per the spec's
        # opt-in note - so they stay hidden until the required set is done.
        state["optional_not_yet_offered"] = optional_missing

    return state


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
        extra = "Emergency contact {}".format(d["emergency_contact_name"])
        # The spec requires reading back *all* collected information, so an
        # emergency number the caller gave has to be spoken too - otherwise
        # they never get the chance to correct a misheard digit.
        if d.get("emergency_contact_phone"):
            extra += " at {}".format(spoken_phone(d["emergency_contact_phone"]))
        extras.append(extra)
    elif d.get("emergency_contact_phone"):
        extras.append(
            "Emergency contact number {}".format(
                spoken_phone(d["emergency_contact_phone"])
            )
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

    # Re-read the row before judging completeness. A stale draft here is
    # catastrophic for the caller: they hear "I still need a few details"
    # about fields they already gave, and the whole intake starts again.
    session.refresh(call)
    draft = dict(call.draft or {})
    state = progress(call)
    if not state["ready_to_confirm"]:
        # A failed save is the one moment the agent needs the whole outstanding
        # list, so name it explicitly rather than relying on progress().
        return {
            "status": "incomplete",
            "still_needed": [
                f for f in COLLECTION_ORDER
                if (call.draft or {}).get(f) in (None, "")
            ],
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
        # Reachable by design, despite every field having passed validation
        # individually: the cross-field checks (a ZIP that cannot belong to
        # the given state) can only run against a complete payload, so they
        # fire here rather than at capture time.
        #
        # `message` carries the first re-prompt verbatim rather than a
        # generic "some details need fixing". The model has to say something
        # immediately after "saving that now", and a vague status invites it
        # to improvise a stall ("let me just double check one thing") and go
        # quiet - which is exactly what a caller must never hear at the save
        # step. Handing it the sentence removes the choice.
        logger.warning(
            "call.finalize_validation_failed",
            extra={
                "call_id": call.call_id,
                "rejected": [f.reason for f in exc.failures],
            },
        )
        # Drop the offending values so the state machine asks for them again
        # instead of re-rejecting the same draft on the next save attempt.
        draft_after = dict(call.draft or {})
        for failure in exc.failures:
            draft_after.pop(failure.field, None)
        call.draft = draft_after
        call.updated_at = _now()
        session.commit()

        return {
            "status": "invalid",
            "rejected": [
                {"field": f.field, "reason": f.reason, "reprompt": f.reprompt}
                for f in exc.failures
            ],
            "message": exc.failures[0].reprompt,
            "next_field": exc.failures[0].field,
            "retry_field": exc.failures[0].field,
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
