"""Patient business logic.

This module is the single implementation of every patient operation. Both
adapters - the public REST API and the voice-agent tool endpoints - call
into here. Neither adapter contains business logic or SQL, which is what
makes the "one service layer, two adapters" claim verifiable rather than
aspirational (see test_voice_and_rest_produce_identical_records in
tests/test_voice_flow.py).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import Patient
from app.domain.validators import (
    ALL_FIELDS,
    REQUIRED_FIELDS,
    ValidationFailure,
    normalize_phone,
    validate_field,
)

logger = logging.getLogger(__name__)


class ServiceError(Exception):
    """Base class for expected, caller-facing service failures."""


class NotFoundError(ServiceError):
    pass


class ValidationError(ServiceError):
    def __init__(self, failures: list[ValidationFailure]) -> None:
        self.failures = failures
        super().__init__("; ".join(f.api_message for f in failures))


class DuplicateError(ServiceError):
    def __init__(self, existing: Patient) -> None:
        self.existing = existing
        super().__init__(
            "An active patient already exists with phone number {}.".format(
                existing.phone_number
            )
        )


def validate_payload(
    payload: dict[str, Any], *, require_all: bool
) -> tuple[dict[str, Any], list[ValidationFailure]]:
    """Validate a partial or complete patient payload.

    Returns ``(cleaned, failures)``. Unknown keys are reported as failures
    rather than silently dropped - a typo in a tool call should be loud.
    """
    cleaned: dict[str, Any] = {}
    failures: list[ValidationFailure] = []

    for key, raw in (payload or {}).items():
        if key not in ALL_FIELDS:
            failures.append(
                ValidationFailure(
                    field=key,
                    reason="unknown_field",
                    reprompt="Sorry, I can't record that one.",
                    api_message="'{}' is not a recognised patient field.".format(
                        key
                    ),
                )
            )
            continue
        result = validate_field(key, raw)
        if result.ok:
            cleaned[key] = result.value
        elif result.failure:
            failures.append(result.failure)

    if require_all:
        for field in REQUIRED_FIELDS:
            if cleaned.get(field) in (None, ""):
                already = any(f.field == field for f in failures)
                if not already:
                    result = validate_field(field, None)
                    if result.failure:
                        failures.append(result.failure)

    return cleaned, failures


def find_active_by_phone(session: Session, phone: str) -> Optional[Patient]:
    """Look up a non-deleted patient by phone number (accepts any format)."""
    normalized = normalize_phone(phone) if phone else None
    if not normalized:
        return None
    stmt = select(Patient).where(
        Patient.phone_number == normalized, Patient.deleted_at.is_(None)
    )
    return session.execute(stmt).scalar_one_or_none()


def get_patient(session: Session, patient_id: str | uuid.UUID) -> Patient:
    try:
        pid = uuid.UUID(str(patient_id))
    except (ValueError, AttributeError, TypeError):
        raise NotFoundError("No patient with id '{}'.".format(patient_id))

    stmt = select(Patient).where(
        Patient.patient_id == pid, Patient.deleted_at.is_(None)
    )
    patient = session.execute(stmt).scalar_one_or_none()
    if patient is None:
        raise NotFoundError("No patient with id '{}'.".format(patient_id))
    return patient


def list_patients(
    session: Session,
    *,
    last_name: str | None = None,
    date_of_birth: str | None = None,
    phone_number: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Patient]:
    """List active patients with the optional filters the spec requires."""
    stmt = select(Patient).where(Patient.deleted_at.is_(None))

    if last_name:
        stmt = stmt.where(Patient.last_name.ilike(last_name.strip()))
    if date_of_birth:
        result = validate_field("date_of_birth", date_of_birth)
        if not result.ok:
            raise ValidationError([result.failure])  # type: ignore[list-item]
        stmt = stmt.where(
            Patient.date_of_birth == date.fromisoformat(str(result.value))
        )
    if phone_number:
        normalized = normalize_phone(phone_number)
        if not normalized:
            result = validate_field("phone_number", phone_number)
            raise ValidationError([result.failure])  # type: ignore[list-item]
        stmt = stmt.where(Patient.phone_number == normalized)

    stmt = stmt.order_by(Patient.created_at.desc()).limit(limit).offset(offset)
    return list(session.execute(stmt).scalars().all())


def create_patient(
    session: Session, payload: dict[str, Any], *, source: str = "api"
) -> Patient:
    """Create a patient. Raises ValidationError / DuplicateError."""
    cleaned, failures = validate_payload(payload, require_all=True)
    if failures:
        raise ValidationError(failures)

    existing = find_active_by_phone(session, cleaned["phone_number"])
    if existing is not None:
        raise DuplicateError(existing)

    patient = Patient(
        patient_id=uuid.uuid4(),
        date_of_birth=date.fromisoformat(cleaned.pop("date_of_birth")),
        **cleaned,
    )
    session.add(patient)
    session.commit()
    session.refresh(patient)

    # Observability requirement: log the final collected payload.
    logger.info(
        "patient.created",
        extra={
            "patient_id": str(patient.patient_id),
            "source": source,
            "fields_provided": sorted(payload.keys()),
        },
    )
    return patient


def update_patient(
    session: Session,
    patient_id: str | uuid.UUID,
    payload: dict[str, Any],
    *,
    source: str = "api",
) -> Patient:
    """Partial update of an existing patient."""
    patient = get_patient(session, patient_id)

    cleaned, failures = validate_payload(payload, require_all=False)
    if failures:
        raise ValidationError(failures)
    if not cleaned:
        return patient

    # Changing phone must not collide with another active record.
    new_phone = cleaned.get("phone_number")
    if new_phone and new_phone != patient.phone_number:
        clash = find_active_by_phone(session, new_phone)
        if clash is not None and clash.patient_id != patient.patient_id:
            raise DuplicateError(clash)

    for key, value in cleaned.items():
        if key == "date_of_birth":
            value = date.fromisoformat(str(value))
        setattr(patient, key, value)

    patient.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(patient)

    logger.info(
        "patient.updated",
        extra={
            "patient_id": str(patient.patient_id),
            "source": source,
            "fields_changed": sorted(cleaned.keys()),
        },
    )
    return patient


def soft_delete_patient(
    session: Session, patient_id: str | uuid.UUID, *, source: str = "api"
) -> Patient:
    """Set ``deleted_at``. The row is never removed."""
    patient = get_patient(session, patient_id)
    patient.deleted_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(patient)

    logger.info(
        "patient.deleted",
        extra={"patient_id": str(patient.patient_id), "source": source},
    )
    return patient


def to_dict(patient: Patient) -> dict[str, Any]:
    """Serialise a patient for API responses."""
    return {
        "patient_id": str(patient.patient_id),
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "date_of_birth": patient.date_of_birth.isoformat()
        if patient.date_of_birth
        else None,
        "sex": patient.sex,
        "phone_number": patient.phone_number,
        "email": patient.email,
        "address_line_1": patient.address_line_1,
        "address_line_2": patient.address_line_2,
        "city": patient.city,
        "state": patient.state,
        "zip_code": patient.zip_code,
        "insurance_provider": patient.insurance_provider,
        "insurance_member_id": patient.insurance_member_id,
        "preferred_language": patient.preferred_language,
        "emergency_contact_name": patient.emergency_contact_name,
        "emergency_contact_phone": patient.emergency_contact_phone,
        "created_at": patient.created_at.isoformat()
        if patient.created_at
        else None,
        "updated_at": patient.updated_at.isoformat()
        if patient.updated_at
        else None,
        "deleted_at": patient.deleted_at.isoformat()
        if patient.deleted_at
        else None,
    }
