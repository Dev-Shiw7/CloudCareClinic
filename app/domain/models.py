"""SQLAlchemy ORM models.

Two tables, and the second one is the architectural centrepiece:

  * ``patients``      - the durable demographic record from the spec.
  * ``call_sessions`` - a per-call *draft* that is written incrementally,
                        turn by turn, as the caller speaks.

Keeping the in-progress draft in the database (rather than in the LLM's
context window) is what makes the system resilient to dropped calls, makes
server-side validation structurally unavoidable, and gives us a free
transcript trail linked to the resulting patient record.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.validators import SEX_VALUES, US_STATES


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Patient(Base):
    """A registered patient. Soft-deleted only - never hard-deleted."""

    __tablename__ = "patients"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # --- Required demographics -------------------------------------------
    first_name: Mapped[str] = mapped_column(String(50), nullable=False)
    last_name: Mapped[str] = mapped_column(String(50), nullable=False)
    # A birth date has no time component; Date keeps the CHECK against
    # CURRENT_DATE honest and the API serialisation unambiguous.
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[str] = mapped_column(String(20), nullable=False)
    # Stored E.164 (+1XXXXXXXXXX) so duplicate detection is an index hit.
    phone_number: Mapped[str] = mapped_column(String(16), nullable=False)
    address_line_1: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(2), nullable=False)
    zip_code: Mapped[str] = mapped_column(String(10), nullable=False)

    # --- Optional demographics -------------------------------------------
    email: Mapped[str | None] = mapped_column(String(254))
    address_line_2: Mapped[str | None] = mapped_column(String(100))
    insurance_provider: Mapped[str | None] = mapped_column(String(100))
    insurance_member_id: Mapped[str | None] = mapped_column(String(50))
    preferred_language: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="English"
    )
    emergency_contact_name: Mapped[str | None] = mapped_column(String(100))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(16))

    # --- Lifecycle --------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=_utcnow,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list["CallSession"]] = relationship(back_populates="patient")

    __table_args__ = (
        CheckConstraint(
            "sex IN ({})".format(
                ", ".join("'{}'".format(v) for v in sorted(SEX_VALUES))
            ),
            name="ck_patients_sex",
        ),
        CheckConstraint(
            "state IN ({})".format(
                ", ".join("'{}'".format(s) for s in sorted(US_STATES))
            ),
            name="ck_patients_state",
        ),
        CheckConstraint(
            r"phone_number ~ '^\+1[2-9][0-9]{2}[2-9][0-9]{6}$'",
            name="ck_patients_phone_e164",
        ),
        CheckConstraint(
            r"zip_code ~ '^[0-9]{5}(-[0-9]{4})?$'", name="ck_patients_zip"
        ),
        CheckConstraint(
            "date_of_birth <= CURRENT_DATE", name="ck_patients_dob_not_future"
        ),
        # One active record per phone number. Soft-deleted rows are excluded,
        # so a number can be re-registered after deletion. This partial index
        # is what makes duplicate detection a single indexed lookup.
        Index(
            "uq_patients_active_phone",
            "phone_number",
            unique=True,
            postgresql_where=deleted_at.is_(None),
        ),
        Index("ix_patients_last_name", "last_name"),
        Index("ix_patients_dob", "date_of_birth"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Patient {} {} {}>".format(
            self.patient_id, self.first_name, self.last_name
        )


class CallSession(Base):
    """An in-progress or completed registration call.

    ``draft`` holds only *validated* field values. A value reaches this
    column having already passed the same validators the REST API uses, so
    promoting a draft to a patient row cannot introduce invalid data.
    """

    __tablename__ = "call_sessions"

    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    caller_phone: Mapped[str | None] = mapped_column(String(16))

    draft: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="in_progress"
    )

    # Append-only list of {role, text, at} entries.
    transcript: Mapped[list | None] = mapped_column(JSONB)

    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("patients.patient_id")
    )
    # Set when the caller is recognised as an existing patient.
    matched_patient_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True)
    )
    outcome_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=_utcnow,
    )

    patient: Mapped[Patient | None] = relationship(back_populates="sessions")

    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'abandoned')",
            name="ck_call_sessions_status",
        ),
        # Resume-after-drop looks up the most recent in-progress session for
        # a caller ID, so index that access path.
        Index("ix_call_sessions_caller_phone", "caller_phone", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<CallSession {} {}>".format(self.call_id, self.status)
