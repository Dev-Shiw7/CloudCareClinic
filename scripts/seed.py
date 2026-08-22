"""Insert two demonstration patients.

Idempotent - safe to run repeatedly. Uses the service layer rather than raw
SQL so seeded rows go through exactly the same validation as everything else.

    python -m scripts.seed
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from app.infra.db import init_db, session_scope  # noqa: E402
from app.services import patient_service  # noqa: E402

SEEDS = [
    {
        "first_name": "Jane",
        "last_name": "Doe",
        "date_of_birth": "1985-03-04",
        "sex": "Female",
        "phone_number": "4155550147",
        "email": "jane.doe@example.com",
        "address_line_1": "22 Market Street",
        "address_line_2": "Apt 4B",
        "city": "San Francisco",
        "state": "CA",
        "zip_code": "94103",
        "insurance_provider": "Blue Cross",
        "insurance_member_id": "BC123456789",
        "preferred_language": "English",
        "emergency_contact_name": "John Doe",
        "emergency_contact_phone": "4155550148",
    },
    {
        "first_name": "Miguel",
        "last_name": "Santos",
        "date_of_birth": "1972-11-19",
        "sex": "Male",
        "phone_number": "2125550183",
        "address_line_1": "915 Amsterdam Avenue",
        "city": "New York",
        "state": "NY",
        "zip_code": "10025",
        "preferred_language": "Spanish",
    },
]


def main() -> None:
    init_db()
    with session_scope() as session:
        for payload in SEEDS:
            existing = patient_service.find_active_by_phone(
                session, payload["phone_number"]
            )
            if existing is not None:
                print("skip   {} {} (already present)".format(
                    payload["first_name"], payload["last_name"]))
                continue
            patient = patient_service.create_patient(
                session, payload, source="seed"
            )
            print("insert {} {} -> {}".format(
                patient.first_name, patient.last_name, patient.patient_id))


if __name__ == "__main__":
    main()
