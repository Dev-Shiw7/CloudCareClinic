"""Field-level validation for patient demographics.

DESIGN NOTE - the core idea of this module:
A failed validation returns THREE representations of the same problem:

  * ``reason``      - stable machine code, for logs and metrics
  * ``reprompt``    - a sentence the voice agent can literally speak
  * ``api_message`` - a precise message for the REST 422 envelope

The server therefore owns the *content* of a correction; the LLM owns only
its *delivery*. This keeps validation authority server-side (the spec
explicitly warns: "do not rely solely on the voice agent for validation")
while still producing a natural phone experience.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Callable, Optional

from pydantic import BaseModel

# US state + territory abbreviations, used for the `state` field.
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "AS", "GU", "MP", "PR", "VI",
}

SEX_VALUES = {"Male", "Female", "Other", "Decline to Answer"}

# Spoken forms callers actually use, mapped to the canonical enum.
SEX_SYNONYMS = {
    "m": "Male", "male": "Male", "man": "Male", "boy": "Male",
    "f": "Female", "female": "Female", "woman": "Female", "girl": "Female",
    "other": "Other", "non-binary": "Other", "nonbinary": "Other",
    "nb": "Other", "x": "Other",
    "decline": "Decline to Answer", "decline to answer": "Decline to Answer",
    "prefer not to say": "Decline to Answer",
    "prefer not to answer": "Decline to Answer",
    "rather not say": "Decline to Answer", "skip": "Decline to Answer",
    "no answer": "Decline to Answer",
}

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "puerto rico": "PR",
    "guam": "GU", "american samoa": "AS",
}

# Names: letters (incl. accented), spaces, hyphens, apostrophes, periods.
NAME_RE = re.compile(r"^[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'\-. ]{0,49}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[A-Za-z]{2,}$")
ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

# Oldest verified human age was 122; 130 is a safe upper bound.
MAX_AGE_YEARS = 130


class ValidationFailure(BaseModel):
    """One rejected field, rendered for three different consumers."""

    field: str
    reason: str
    reprompt: str
    api_message: str


class FieldResult(BaseModel):
    """Outcome of validating a single field."""

    field: str
    ok: bool
    value: Optional[object] = None
    failure: Optional[ValidationFailure] = None

    @classmethod
    def accept(cls, field: str, value: object) -> "FieldResult":
        return cls(field=field, ok=True, value=value)

    @classmethod
    def reject(
        cls, field: str, reason: str, reprompt: str, api_message: str
    ) -> "FieldResult":
        return cls(
            field=field,
            ok=False,
            failure=ValidationFailure(
                field=field,
                reason=reason,
                reprompt=reprompt,
                api_message=api_message,
            ),
        )


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------

def normalize_phone(raw: str) -> Optional[str]:
    """Return a 10-digit US number as E.164 (+1XXXXXXXXXX), or None.

    Accepts the many shapes speech-to-text produces: "(415) 555-0147",
    "415.555.0147", "1 415 555 0147", "+14155550147".
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    # US area codes and exchange codes cannot begin with 0 or 1.
    if digits[0] in "01" or digits[3] in "01":
        return None
    return "+1" + digits


def spoken_digits(value: str) -> str:
    """Render digits for TTS one at a time: '94103' -> '9 4 1 0 3'."""
    return " ".join(ch for ch in (value or "") if ch.isalnum())


def _spoken_date(value: date) -> str:
    """'March 4, 1985' with no zero padding, for natural TTS."""
    return "{} {}, {}".format(value.strftime("%B"), value.day, value.year)


# --------------------------------------------------------------------------
# Per-field validators
# --------------------------------------------------------------------------

def _validate_name(field: str, raw: object, label: str) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.reject(
            field, "missing",
            "Sorry, I didn't catch your {}. Could you say it again?".format(label),
            "{} is required.".format(field),
        )
    if len(text) > 50:
        return FieldResult.reject(
            field, "too_long",
            "That came through longer than I can store. Could you repeat just "
            "your {}?".format(label),
            "{} must be 50 characters or fewer.".format(field),
        )
    if not NAME_RE.match(text):
        return FieldResult.reject(
            field, "invalid_characters",
            "I may have misheard your {}. Could you spell it for me, letter by "
            "letter?".format(label),
            "{} may contain only letters, spaces, hyphens, and "
            "apostrophes.".format(field),
        )
    return FieldResult.accept(field, " ".join(text.split()))


def validate_first_name(raw: object) -> FieldResult:
    return _validate_name("first_name", raw, "first name")


def validate_last_name(raw: object) -> FieldResult:
    return _validate_name("last_name", raw, "last name")


def validate_date_of_birth(raw: object) -> FieldResult:
    """Accept ISO (YYYY-MM-DD) or US (MM/DD/YYYY); reject future / implausible."""
    text = str(raw or "").strip()
    if not text:
        return FieldResult.reject(
            "date_of_birth", "missing",
            "I didn't get your date of birth. What is it?",
            "date_of_birth is required.",
        )

    parsed: Optional[date] = None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d %Y", "%b %d %Y",
                "%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue

    if parsed is None:
        return FieldResult.reject(
            "date_of_birth", "unparseable",
            "I didn't quite get that date. Could you give me your date of birth "
            "as month, day, and year?",
            "date_of_birth must be a valid date in MM/DD/YYYY or YYYY-MM-DD "
            "format.",
        )

    today = date.today()
    if parsed > today:
        return FieldResult.reject(
            "date_of_birth", "future_date",
            "I heard {}, but that's in the future. What year were you "
            "born?".format(_spoken_date(parsed)),
            "date_of_birth cannot be in the future.",
        )

    age = today.year - parsed.year - (
        (today.month, today.day) < (parsed.month, parsed.day)
    )
    if age > MAX_AGE_YEARS:
        return FieldResult.reject(
            "date_of_birth", "implausible_age",
            "That would make you over {} years old, so I think I misheard. What "
            "year were you born?".format(MAX_AGE_YEARS),
            "date_of_birth implies an age greater than {} "
            "years.".format(MAX_AGE_YEARS),
        )

    return FieldResult.accept("date_of_birth", parsed.isoformat())


def validate_sex(raw: object) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.reject(
            "sex", "missing",
            "And what sex should I record - male, female, other, or would you "
            "rather not say?",
            "sex is required.",
        )
    if text in SEX_VALUES:
        return FieldResult.accept("sex", text)
    mapped = SEX_SYNONYMS.get(text.lower())
    if mapped:
        return FieldResult.accept("sex", mapped)
    return FieldResult.reject(
        "sex", "invalid_enum",
        "I can record that as male, female, other, or decline to answer. Which "
        "of those fits best?",
        "sex must be one of: Male, Female, Other, Decline to Answer.",
    )


def _validate_us_phone(
    field: str, raw: object, label: str, required: bool
) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        if required:
            return FieldResult.reject(
                field, "missing",
                "What's the best {} to reach you at?".format(label),
                "{} is required.".format(field),
            )
        return FieldResult.accept(field, None)

    normalized = normalize_phone(text)
    if normalized is None:
        digits = re.sub(r"\D", "", text)
        if digits and len(digits) < 10:
            lead = "I only caught {} digits".format(len(digits))
        else:
            lead = "That didn't sound like a complete number"
        return FieldResult.reject(
            field, "invalid_us_phone",
            "{}. Could you give me the full 10-digit {}, starting with the area "
            "code?".format(lead, label),
            "{} must be a valid 10-digit US phone number.".format(field),
        )
    return FieldResult.accept(field, normalized)


def validate_phone_number(raw: object) -> FieldResult:
    return _validate_us_phone("phone_number", raw, "phone number", required=True)


def validate_emergency_contact_phone(raw: object) -> FieldResult:
    return _validate_us_phone(
        "emergency_contact_phone", raw, "phone number for them", required=False
    )


def validate_email(raw: object) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.accept("email", None)
    # STT often renders "at" / "dot" as words.
    text = re.sub(r"\s+at\s+", "@", text, flags=re.I)
    text = re.sub(r"\s+dot\s+", ".", text, flags=re.I)
    text = text.replace(" ", "")
    if not EMAIL_RE.match(text) or len(text) > 254:
        return FieldResult.reject(
            "email", "invalid_email",
            "I don't think I got that email quite right. Could you spell it out "
            "for me, including what comes after the at sign?",
            "email must be a valid email address.",
        )
    return FieldResult.accept("email", text.lower())


def validate_address_line_1(raw: object) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.reject(
            "address_line_1", "missing",
            "What's your street address?",
            "address_line_1 is required.",
        )
    if len(text) > 200:
        return FieldResult.reject(
            "address_line_1", "too_long",
            "That address came through a bit garbled. Could you give me just the "
            "street number and street name?",
            "address_line_1 must be 200 characters or fewer.",
        )
    return FieldResult.accept("address_line_1", text)


def validate_address_line_2(raw: object) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.accept("address_line_2", None)
    if len(text) > 100:
        return FieldResult.reject(
            "address_line_2", "too_long",
            "Could you give me just the apartment or unit number?",
            "address_line_2 must be 100 characters or fewer.",
        )
    return FieldResult.accept("address_line_2", text)


def validate_city(raw: object) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.reject(
            "city", "missing", "And what city is that in?", "city is required."
        )
    if len(text) > 100:
        return FieldResult.reject(
            "city", "too_long",
            "I didn't catch the city clearly. Could you say just the city name?",
            "city must be 100 characters or fewer.",
        )
    return FieldResult.accept("city", text)


def validate_state(raw: object) -> FieldResult:
    """Accept a 2-letter abbreviation or a spelled-out state name."""
    text = str(raw or "").strip()
    if not text:
        return FieldResult.reject(
            "state", "missing", "Which state?", "state is required."
        )
    upper = text.upper()
    if upper in US_STATES:
        return FieldResult.accept("state", upper)
    mapped = STATE_NAMES.get(text.lower().replace(".", "").strip())
    if mapped:
        return FieldResult.accept("state", mapped)
    return FieldResult.reject(
        "state", "invalid_state",
        "I didn't recognise that as a US state. Could you say the state name "
        "again?",
        "state must be a valid 2-letter US state abbreviation.",
    )


def validate_zip_code(raw: object) -> FieldResult:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        return FieldResult.reject(
            "zip_code", "missing", "And the ZIP code?", "zip_code is required."
        )
    # "941031234" -> "94103-1234"
    if re.fullmatch(r"\d{9}", text):
        text = "{}-{}".format(text[:5], text[5:])
    if not ZIP_RE.match(text):
        return FieldResult.reject(
            "zip_code", "invalid_zip",
            "That didn't sound like a complete ZIP code. Could you give me the "
            "five digits?",
            "zip_code must be a 5-digit or ZIP+4 US postal code.",
        )
    return FieldResult.accept("zip_code", text)


def _validate_optional_text(
    field: str, raw: object, max_len: int, label: str
) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.accept(field, None)
    if len(text) > max_len:
        return FieldResult.reject(
            field, "too_long",
            "I didn't catch the {} clearly. Could you repeat it?".format(label),
            "{} must be {} characters or fewer.".format(field, max_len),
        )
    return FieldResult.accept(field, text)


def validate_insurance_provider(raw: object) -> FieldResult:
    return _validate_optional_text(
        "insurance_provider", raw, 100, "insurance provider"
    )


def validate_insurance_member_id(raw: object) -> FieldResult:
    text = str(raw or "").strip().replace(" ", "").upper()
    if not text:
        return FieldResult.accept("insurance_member_id", None)
    if not re.fullmatch(r"[A-Z0-9\-]{2,50}", text):
        return FieldResult.reject(
            "insurance_member_id", "invalid_member_id",
            "Could you read me the member ID one character at a time?",
            "insurance_member_id must be 2-50 alphanumeric characters.",
        )
    return FieldResult.accept("insurance_member_id", text)


def validate_preferred_language(raw: object) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.accept("preferred_language", "English")
    if len(text) > 50:
        return FieldResult.reject(
            "preferred_language", "too_long",
            "Which language would you prefer?",
            "preferred_language must be 50 characters or fewer.",
        )
    return FieldResult.accept("preferred_language", text.title())


def validate_emergency_contact_name(raw: object) -> FieldResult:
    text = str(raw or "").strip()
    if not text:
        return FieldResult.accept("emergency_contact_name", None)
    if len(text) > 100:
        return FieldResult.reject(
            "emergency_contact_name", "too_long",
            "Could you give me just their first and last name?",
            "emergency_contact_name must be 100 characters or fewer.",
        )
    return FieldResult.accept("emergency_contact_name", text)


# --------------------------------------------------------------------------
# ZIP-to-state cross-check
#
# The first three digits of a US ZIP (the "sectional centre facility") sit
# inside ranges assigned to one state, so a ZIP and a state are jointly
# checkable facts rather than two independent free-text fields. Speech
# recognition mishears city and state names constantly ("Sydney" for
# "Sidney", a state abbreviation heard as a different one), and a value that
# is individually well-formed can still be impossible in combination.
#
# Ranges below are inclusive 3-digit prefixes. Kept as data rather than a
# dependency: this is a stable published mapping, and a 3-hour build should
# not take a package for it.
# --------------------------------------------------------------------------
ZIP_PREFIX_RANGES: dict[str, list[tuple[int, int]]] = {
    "AL": [(350, 369)], "AK": [(995, 999)], "AZ": [(850, 865)],
    "AR": [(716, 729), (755, 755)], "CA": [(900, 961)], "CO": [(800, 816)],
    "CT": [(60, 69)], "DE": [(197, 199)], "DC": [(200, 205), (569, 569)],
    "FL": [(320, 349)], "GA": [(300, 319), (398, 399)], "HI": [(967, 968)],
    "ID": [(832, 838)], "IL": [(600, 629)], "IN": [(460, 479)],
    "IA": [(500, 528)], "KS": [(660, 679)], "KY": [(400, 427)],
    "LA": [(700, 714)], "ME": [(39, 49)], "MD": [(206, 219)],
    "MA": [(10, 27), (55, 55)], "MI": [(480, 499)], "MN": [(550, 567)],
    "MS": [(386, 397)], "MO": [(630, 658)], "MT": [(590, 599)],
    "NE": [(680, 693)], "NV": [(889, 898)], "NH": [(30, 38)],
    "NJ": [(70, 89)], "NM": [(870, 884)],
    "NY": [(90, 149), (5, 5), (63, 63)], "NC": [(269, 289)],
    "ND": [(580, 588)], "OH": [(430, 459)], "OK": [(730, 749)],
    "OR": [(970, 979)], "PA": [(150, 196)], "RI": [(28, 29)],
    "SC": [(290, 299)], "SD": [(570, 577)], "TN": [(370, 385)],
    "TX": [(750, 799), (885, 885)], "UT": [(840, 847)], "VT": [(50, 59)],
    "VA": [(220, 246)], "WA": [(980, 994)], "WV": [(247, 268)],
    "WI": [(530, 549)], "WY": [(820, 831)],
    "PR": [(6, 9)], "VI": [(8, 8)], "GU": [(969, 969)],
    "AS": [(96, 96)], "MP": [(969, 969)],
}


def zip_matches_state(zip_code: str, state: str) -> bool:
    """True if a 5-digit ZIP's prefix falls in the state's assigned ranges.

    Unknown states pass: the caller is not made to argue with a gap in this
    table. Only a definite contradiction is reported.
    """
    ranges = ZIP_PREFIX_RANGES.get((state or "").upper())
    if not ranges or not zip_code:
        return True
    digits = re.sub(r"\D", "", zip_code)[:5]
    if len(digits) < 5:
        return True
    prefix = int(digits[:3])
    return any(lo <= prefix <= hi for lo, hi in ranges)


def cross_check_address(cleaned: dict) -> Optional[ValidationFailure]:
    """Report a ZIP that cannot belong to the given state.

    Called once a payload is otherwise valid, because it is the *combination*
    that is wrong - neither field is individually faulty, so neither can be
    blamed on its own. The re-prompt names both values back to the caller so
    they can tell us which one we misheard.
    """
    zip_code, state = cleaned.get("zip_code"), cleaned.get("state")
    if not zip_code or not state or zip_matches_state(zip_code, state):
        return None
    return ValidationFailure(
        field="zip_code",
        reason="zip_state_mismatch",
        reprompt=(
            "I have {} as the state but {} as the ZIP, and those don't go "
            "together — so I've misheard one of them. What's the city, state, "
            "and ZIP again?".format(state, spoken_digits(str(zip_code)[:5]))
        ),
        api_message=(
            "zip_code {} is not valid for state {}.".format(zip_code, state)
        ),
    )


# Single dispatch table: field name -> validator.
VALIDATORS: dict[str, Callable[[object], FieldResult]] = {
    "first_name": validate_first_name,
    "last_name": validate_last_name,
    "date_of_birth": validate_date_of_birth,
    "sex": validate_sex,
    "phone_number": validate_phone_number,
    "email": validate_email,
    "address_line_1": validate_address_line_1,
    "address_line_2": validate_address_line_2,
    "city": validate_city,
    "state": validate_state,
    "zip_code": validate_zip_code,
    "insurance_provider": validate_insurance_provider,
    "insurance_member_id": validate_insurance_member_id,
    "preferred_language": validate_preferred_language,
    "emergency_contact_name": validate_emergency_contact_name,
    "emergency_contact_phone": validate_emergency_contact_phone,
}

REQUIRED_FIELDS = [
    "first_name", "last_name", "date_of_birth", "sex", "phone_number",
    "address_line_1", "city", "state", "zip_code",
]

OPTIONAL_FIELDS = [
    "email", "address_line_2", "insurance_provider", "insurance_member_id",
    "preferred_language", "emergency_contact_name", "emergency_contact_phone",
]

ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS


def validate_field(field: str, value: object) -> FieldResult:
    """Validate one field by name. Unknown fields are rejected loudly."""
    validator = VALIDATORS.get(field)
    if validator is None:
        return FieldResult.reject(
            field, "unknown_field",
            "Sorry, I'm not sure I can record that. Let's move on.",
            "'{}' is not a recognised patient field.".format(field),
        )
    return validator(value)
