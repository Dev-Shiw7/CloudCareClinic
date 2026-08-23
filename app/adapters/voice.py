"""Vapi tool endpoints.

Deliberately thin. This adapter translates Vapi's tool-call envelope into
service calls and back - nothing else. There is no SQL and no validation
here; if there were, the "one service layer, two adapters" claim would be
false. Compare the size of this file to services/session_service.py.

Vapi's contract: a tool call arrives as
    {"message": {"toolCalls": [{"id": ..., "function": {"name", "arguments"}}],
                 "call": {"id": ..., "customer": {"number": ...}}}}
and must be answered with
    {"results": [{"toolCallId": <same id>, "result": <string or object>}]}

Everything below funnels through _reply() so that envelope is produced in
exactly one place.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from dataclasses import dataclass
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


@dataclass
class ToolCall:
    """A Vapi tool invocation, normalised."""

    tool_call_id: str | None
    name: str
    arguments: dict[str, Any]
    call_id: str | None
    caller_phone: str | None


async def _unwrap(request: Request) -> ToolCall:
    """Parse a Vapi tool-call request.

    Also accepts a flat ``{"call_id": ..., "arguments": {...}}`` body so the
    endpoints are testable with curl and in the test suite.
    """
    body = await request.json()
    message = body.get("message", body) or {}

    call = message.get("call") or body.get("call") or {}
    call_id = call.get("id") or body.get("call_id") or message.get("callId")
    caller_phone = (
        (call.get("customer") or {}).get("number") or body.get("caller_phone")
    )

    tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
    if tool_calls:
        # Known assumption: one tool call per request. Vapi sends them one at
        # a time for the sequencing this design depends on (each response
        # names the next field), so a batch would mean the model ran ahead of
        # the state machine. Handling only the first is the safe read - a
        # second would be answered with a toolCallId Vapi did not ask about.
        entry = tool_calls[0]
        fn = entry.get("function", {})
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        return ToolCall(
            tool_call_id=entry.get("id"),
            name=fn.get("name", ""),
            arguments=args if isinstance(args, dict) else {},
            call_id=call_id,
            caller_phone=caller_phone,
        )

    return ToolCall(
        tool_call_id=body.get("toolCallId"),
        name=body.get("tool", ""),
        arguments=body.get("arguments") or {},
        call_id=call_id,
        caller_phone=caller_phone,
    )


def _reply(tool: ToolCall, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a service result in the envelope Vapi expects.

    The result is serialised to a JSON string: Vapi feeds it back to the
    model as tool output, and a string is what the model reads most
    reliably. When there is no toolCallId (curl / tests) the bare payload is
    returned so responses stay easy to inspect.
    """
    if tool.tool_call_id is None:
        return payload
    return {
        "results": [
            {"toolCallId": tool.tool_call_id, "result": json.dumps(payload)}
        ]
    }


def _require_call_id(tool: ToolCall) -> str:
    if not tool.call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing call id in request.",
        )
    return tool.call_id


@router.post("/start", dependencies=[Depends(verify_vapi_secret)])
async def start_call(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Called once at the top of the call.

    Does two useful things before the caller has said a word:
      1. Looks for an interrupted draft from this number (resume-after-drop).
      2. Looks for an existing patient with this number (duplicate detection).
    """
    tool = await _unwrap(request)
    call_id = _require_call_id(tool)

    call = session_service.get_or_create_session(
        session, call_id, tool.caller_phone
    )
    result: dict[str, Any] = {"call_id": call_id}

    # A phone number given as a tool argument wins over caller ID - the
    # caller may be registering from a different line than they want on file.
    lookup_phone = tool.arguments.get("phone_number") or tool.caller_phone

    if lookup_phone:
        previous = session_service.find_resumable_draft(
            session, lookup_phone, exclude_call_id=call_id
        )
        if previous is not None:
            call = session_service.adopt_draft(session, call, previous)
            result["resumed"] = True
            result["resume_hint"] = (
                "This caller was disconnected mid-registration. Acknowledge it "
                "briefly, summarise what you already have, and continue from "
                "the next missing field. Do not start over."
            )

        existing = patient_service.find_active_by_phone(session, lookup_phone)
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
    return _reply(tool, result)


@router.post("/lookup", dependencies=[Depends(verify_vapi_secret)])
async def lookup(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Look up an existing patient by phone number.

    Separate from /start because the caller may give a different number than
    the one they are calling from.
    """
    tool = await _unwrap(request)
    phone = tool.arguments.get("phone_number") or tool.caller_phone or ""

    existing = patient_service.find_active_by_phone(session, phone)
    if existing is None:
        return _reply(
            tool,
            {
                "found": False,
                "message": "No existing record for that number. Continue with "
                           "a new registration.",
            },
        )

    if tool.call_id:
        call = session_service.get_or_create_session(
            session, tool.call_id, tool.caller_phone
        )
        call.matched_patient_id = existing.patient_id
        session.commit()

    return _reply(
        tool,
        {
            "found": True,
            "patient_id": str(existing.patient_id),
            "first_name": existing.first_name,
            "last_name": existing.last_name,
            "message": (
                "We already have a record for {} {}. Ask whether they would "
                "like to update it instead of creating a new one.".format(
                    existing.first_name, existing.last_name
                )
            ),
        },
    )


@router.post("/capture", dependencies=[Depends(verify_vapi_secret)])
async def capture(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Validate and persist one or more fields. Called every turn or two."""
    tool = await _unwrap(request)
    call_id = _require_call_id(tool)

    call = session_service.get_or_create_session(
        session, call_id, tool.caller_phone
    )
    args = tool.arguments
    fields = args.get("fields") if isinstance(args.get("fields"), dict) else args
    return _reply(tool, session_service.capture_fields(session, call, fields or {}))


@router.post("/confirm", dependencies=[Depends(verify_vapi_secret)])
async def confirm(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Return the chunked read-back script."""
    tool = await _unwrap(request)
    call_id = _require_call_id(tool)
    call = session_service.get_or_create_session(
        session, call_id, tool.caller_phone
    )
    return _reply(tool, session_service.confirmation_script(call))


@router.post("/restart", dependencies=[Depends(verify_vapi_secret)])
async def restart(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Caller asked to start over. Clears the draft, keeps the session."""
    tool = await _unwrap(request)
    call_id = _require_call_id(tool)
    call = session_service.get_or_create_session(
        session, call_id, tool.caller_phone
    )
    result = session_service.clear_draft(session, call)
    result["message"] = "Cleared. Starting fresh."
    return _reply(tool, result)


@router.post("/finalize", dependencies=[Depends(verify_vapi_secret)])
async def finalize(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Promote the draft to a patient record. Idempotent on retry."""
    tool = await _unwrap(request)
    call_id = _require_call_id(tool)
    call = session_service.get_or_create_session(
        session, call_id, tool.caller_phone
    )

    # A model that ignored capture_fields and passed everything at the end
    # still works: fold any supplied fields into the draft first.
    args = tool.arguments
    trailing = args.get("fields") if isinstance(args.get("fields"), dict) else None
    if trailing:
        session_service.capture_fields(session, call, trailing)
        session.refresh(call)

    mode = args.get("mode", "auto")
    return _reply(tool, session_service.finalize(session, call, mode=mode))


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
        transcript = message.get("transcript") or (
            message.get("artifact") or {}
        ).get("transcript")
        if call is not None and transcript:
            call.transcript = (
                transcript
                if isinstance(transcript, list)
                else [{"text": transcript}]
            )
            session.commit()

        if event_type == "end-of-call-report":
            session_service.mark_abandoned(
                session,
                call_id,
                note="Call ended without completing registration",
            )

    return {"ok": True}
