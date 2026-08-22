"""Vapi tool endpoints.

Deliberately thin. This adapter translates Vapi's tool-call envelope into
service calls and back - nothing else. There is no SQL and no validation
here; if there were, the "one service layer, two adapters" claim would be
false. Compare the size of this file to services/session_service.py.

Every response is shaped as guidance for the LLM's next turn: what was
accepted, what to re-prompt for, and what remains.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.domain.models import CallSession
from app.infra.db import get_session
from app.services import patient_service, session_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


def verify_vapi_secret(
    x_vapi_secret: str | None = Header(default=None),
) -> None:
    """Shared-secret check on every tool call.

    Vapi sends this header on server requests. Without it these endpoints
    would let anyone write to the patient database.
    """
    expected = os.environ.get("VAPI_SERVER_SECRET")
    if not expected:
        # Fail closed rather than silently accepting unauthenticated writes.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="VAPI_SERVER_SECRET is not configured on the server.",
        )
    if not x_vapi_secret or not hmac.compare_digest(x_vapi_secret, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Vapi-Secret header.",
        )


async def _unwrap(request: Request) -> tuple[str, dict[str, Any], str | None, str | None]:
    """Pull (tool_name, arguments, call_id, caller_phone) from a Vapi request.

    Vapi nests tool calls under message.toolCalls[]. We also accept a flat
    body so the endpoints are trivially testable with curl.
    """
    body = await request.json()
    message = body.get("message", body) or {}

    call = message.get("call") or body.get("call") or {}
    call_id = call.get("id") or body.get("call_id") or message.get("callId")
    caller_phone = (
        (call.get("customer") or {}).get("number")
        or body.get("caller_phone")
    )

    tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
    if tool_calls:
        fn = tool_calls[0].get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            import json

            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        return name, args, call_id, caller_phone

    return body.get("tool", ""), body.get("arguments", body), call_id, caller_phone


def _require_call_id(call_id: str | None) -> str:
    if not call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing call id in request.",
        )
    return call_id


@router.post("/start", dependencies=[Depends(verify_vapi_secret)])
async def start_call(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Called once at the top of the call.

    Does two useful things before the caller has said a word:
      1. Looks for an interrupted draft from this number (resume-after-drop).
      2. Looks for an existing patient with this number (duplicate detection).
    """
    _, _, call_id, caller_phone = await _unwrap(request)
    call_id = _require_call_id(call_id)

    call = session_service.get_or_create_session(session, call_id, caller_phone)

    result: dict[str, Any] = {"call_id": call_id}

    if caller_phone:
        previous = session_service.find_resumable_draft(
            session, caller_phone, exclude_call_id=call_id
        )
        if previous is not None:
            call = session_service.adopt_draft(session, call, previous)
            result["resumed"] = True
            result["resume_hint"] = (
                "This caller was disconnected mid-registration. Acknowledge it "
                "briefly, summarise what you already have, and continue from "
                "the next missing field. Do not start over."
            )

        existing = patient_service.find_active_by_phone(session, caller_phone)
        if existing is not None:
            call.matched_patient_id = existing.patient_id
            session.commit()
            result["existing_patient"] = {
                "patient_id": str(existing.patient_id),
                "first_name": existing.first_name,
                "last_name": existing.last_name,
            }
            result["duplicate_hint"] = (
                "We already have a record for {} {}. Ask whether they would "
                "like to update it instead of creating a new one.".format(
                    existing.first_name, existing.last_name
                )
            )

    result.update(session_service.progress(call))
    return result


@router.post("/capture", dependencies=[Depends(verify_vapi_secret)])
async def capture(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Validate and persist one or more fields. Called every turn or two."""
    _, args, call_id, caller_phone = await _unwrap(request)
    call_id = _require_call_id(call_id)

    call = session_service.get_or_create_session(session, call_id, caller_phone)
    fields = args.get("fields") if isinstance(args.get("fields"), dict) else args
    return session_service.capture_fields(session, call, fields or {})


@router.post("/confirm", dependencies=[Depends(verify_vapi_secret)])
async def confirm(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Return the chunked read-back script."""
    _, _, call_id, caller_phone = await _unwrap(request)
    call_id = _require_call_id(call_id)
    call = session_service.get_or_create_session(session, call_id, caller_phone)
    return session_service.confirmation_script(call)


@router.post("/restart", dependencies=[Depends(verify_vapi_secret)])
async def restart(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Caller asked to start over. Clears the draft, keeps the session."""
    _, _, call_id, caller_phone = await _unwrap(request)
    call_id = _require_call_id(call_id)
    call = session_service.get_or_create_session(session, call_id, caller_phone)
    result = session_service.clear_draft(session, call)
    result["message"] = "Cleared. Starting fresh."
    return result


@router.post("/finalize", dependencies=[Depends(verify_vapi_secret)])
async def finalize(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Promote the draft to a patient record. Idempotent on retry."""
    _, args, call_id, caller_phone = await _unwrap(request)
    call_id = _require_call_id(call_id)
    call = session_service.get_or_create_session(session, call_id, caller_phone)
    mode = args.get("mode", "auto") if isinstance(args, dict) else "auto"
    return session_service.finalize(session, call, mode=mode)


@router.post("/event", dependencies=[Depends(verify_vapi_secret)])
async def event(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """End-of-call webhook: store the transcript, mark abandoned calls."""
    body = await request.json()
    message = body.get("message", body) or {}
    event_type = message.get("type", "")
    call_id = (message.get("call") or {}).get("id") or body.get("call_id")

    if not call_id:
        return {"ok": True}

    if event_type in ("end-of-call-report", "status-update"):
        call = session.get(CallSession, call_id)
        transcript = message.get("transcript") or message.get("artifact", {}).get(
            "transcript"
        )
        if call is not None and transcript:
            call.transcript = (
                transcript if isinstance(transcript, list) else [{"text": transcript}]
            )
            session.commit()

        if event_type == "end-of-call-report":
            session_service.mark_abandoned(
                session, call_id, note="Call ended without completing registration"
            )

    return {"ok": True}
