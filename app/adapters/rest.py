"""Public REST API.

A thin HTTP shell over ``patient_service``. Contains no business logic and
no SQL - every route delegates. All responses use the envelope the spec
requires: ``{"data": ..., "error": ...}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.infra.db import get_session
from app.services import patient_service

router = APIRouter(prefix="/patients", tags=["patients"])


def ok(data: Any) -> dict[str, Any]:
    return {"data": data, "error": None}


def _failures_payload(exc: patient_service.ValidationError) -> dict[str, Any]:
    return {
        "code": "validation_error",
        "message": "One or more fields are invalid.",
        "fields": [
            {"field": f.field, "reason": f.reason, "message": f.api_message}
            for f in exc.failures
        ],
    }


@router.get("")
def list_patients(
    response: Response,
    last_name: str | None = Query(None, max_length=50),
    date_of_birth: str | None = Query(None, max_length=20),
    phone_number: str | None = Query(None, max_length=20),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """List active patients, with the optional filters the spec requires."""
    try:
        patients = patient_service.list_patients(
            session,
            last_name=last_name,
            date_of_birth=date_of_birth,
            phone_number=phone_number,
            limit=limit,
            offset=offset,
        )
    except patient_service.ValidationError as exc:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return {"data": None, "error": _failures_payload(exc)}

    return ok([patient_service.to_dict(p) for p in patients])


@router.get("/{patient_id}")
def get_patient(
    patient_id: str,
    response: Response,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        patient = patient_service.get_patient(session, patient_id)
    except patient_service.NotFoundError as exc:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"data": None, "error": {"code": "not_found", "message": str(exc)}}
    return ok(patient_service.to_dict(patient))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_patient(
    payload: dict[str, Any],
    response: Response,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        patient = patient_service.create_patient(session, payload, source="api")
    except patient_service.ValidationError as exc:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return {"data": None, "error": _failures_payload(exc)}
    except patient_service.DuplicateError as exc:
        response.status_code = status.HTTP_409_CONFLICT
        return {
            "data": None,
            "error": {
                "code": "duplicate_patient",
                "message": str(exc),
                "existing_patient_id": str(exc.existing.patient_id),
            },
        }
    return ok(patient_service.to_dict(patient))


@router.put("/{patient_id}")
def update_patient(
    patient_id: str,
    payload: dict[str, Any],
    response: Response,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Partial update - only the supplied fields change."""
    try:
        patient = patient_service.update_patient(
            session, patient_id, payload, source="api"
        )
    except patient_service.NotFoundError as exc:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"data": None, "error": {"code": "not_found", "message": str(exc)}}
    except patient_service.ValidationError as exc:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return {"data": None, "error": _failures_payload(exc)}
    except patient_service.DuplicateError as exc:
        response.status_code = status.HTTP_409_CONFLICT
        return {
            "data": None,
            "error": {
                "code": "duplicate_patient",
                "message": str(exc),
                "existing_patient_id": str(exc.existing.patient_id),
            },
        }
    return ok(patient_service.to_dict(patient))


@router.delete("/{patient_id}")
def delete_patient(
    patient_id: str,
    response: Response,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Soft delete - sets deleted_at, never removes the row."""
    try:
        patient = patient_service.soft_delete_patient(
            session, patient_id, source="api"
        )
    except patient_service.NotFoundError as exc:
        response.status_code = status.HTTP_404_NOT_FOUND
        return {"data": None, "error": {"code": "not_found", "message": str(exc)}}
    return ok(
        {
            "patient_id": str(patient.patient_id),
            "deleted_at": patient.deleted_at.isoformat()
            if patient.deleted_at
            else None,
        }
    )
