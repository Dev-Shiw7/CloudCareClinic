# Voice AI Patient Registration

A caller dials a US phone number, speaks to an intake coordinator named
Shiwani, and is registered as a patient. Every field is validated and
persisted server-side *as it is spoken*, so a dropped call loses nothing and
the voice agent can never write invalid data.

| | |
|---|---|
| **Phone number** | **+1 (661) 215-8249** |
| **API base URL** | `https://cloudcareclinic.onrender.com` |
| **Interactive API docs** | `<API base URL>/docs` |
| **Health check** | `<API base URL>/health` |

---

## A note on the time limit

The brief sets a 3-hour limit. **This repository's commit history spans longer
than that** — roughly 18:50 on 22 Aug to 18:45 on 23 Aug, in two sittings —
and I would rather say so than have you find it in `git log`.

What existed at the 3-hour mark (through commit `f380866`) was the working
system: the server-driven state machine, the validators, the full REST API,
the Vapi assistant with its prompt and tool schemas, and a live phone number
completing registrations end to end.

What came after was hardening, not new scope:

| Commit | What it fixed |
|---|---|
| `f0a428a` | 400 for malformed bodies; keep every error inside the envelope |
| `2d0f116` | Re-prompt the field that actually failed; read back every collected field |
| `e3ee973` | Stale-draft bug on save (pooled-connection identity map); stop the test suite wiping the deployed DB |
| `62e60ff` | Cut per-turn latency; only speak a filler when a call is genuinely slow |
| `e2859ea` | Reject a ZIP that cannot belong to the stated state |

Every one of those came from actually calling the number and hearing something
wrong. If you want to assess only the 3-hour deliverable, `git checkout
f380866` is that boundary — it is callable and passes its tests. I have left
the later fixes in because a reviewer dialling the number should reach the
version that works properly, and because the fixes are the more honest signal
about how I work.

---

## Architecture

```
   ┌─────────┐   PSTN    ┌──────────────────┐
   │ Caller  │◄─────────►│       Vapi       │
   └─────────┘           │  STT · TTS ·     │
                         │  turn-taking     │
                         └────────┬─────────┘
                                  │ tool calls (HTTPS + shared secret)
                                  ▼
        ┌──────────────────────────────────────────────┐
        │                  FastAPI                     │
        │                                              │
        │   adapters/voice.py        adapters/rest.py  │
        │   /voice/*                 /patients         │
        │   (thin: shape only)       (thin: HTTP only) │
        │            └───────┬───────────┘             │
        │                    ▼                         │
        │          services/  ← ALL business logic     │
        │            patient_service                   │
        │            session_service  ← state machine  │
        │                    ▼                         │
        │          domain/validators.py                │
        │            ← single source of truth          │
        └────────────────────┬─────────────────────────┘
                             ▼
                   ┌───────────────────┐
                   │    PostgreSQL     │
                   │  patients         │
                   │  call_sessions    │
                   └───────────────────┘
```

### The one idea worth reading

**The LLM is a rendering layer over a server-driven state machine.**

Most voice agents keep the collected data in the model's context window and
write it once at the end. This one does the opposite. Every tool response
tells the agent what was accepted, what was rejected (and the exact sentence
to say about it), and which field to ask for next:

```jsonc
// POST /voice/capture  →  {"fields": {"date_of_birth": "03/04/2027"}}
{
  "accepted": [],
  "rejected": [{
    "field": "date_of_birth",
    "reason": "future_date",
    "reprompt": "I heard March 4, 2027, but that's in the future. What year were you born?"
  }],
  "still_needed": ["date_of_birth", "sex", "phone_number", "..."],
  "next_field": "date_of_birth",   // stays on the rejected field, not the next one
  "retry_field": "date_of_birth",
  "ready_to_confirm": false,
  "progress": "2 of 9 required fields captured"
}
```

The agent never decides what to ask next and never judges whether a value is
valid. It only decides *how to say things*. That inversion is what makes the
dropped-call, invalid-input, and start-over cases fall out for free rather
than needing prompt instructions to cover them.

Full reasoning, including the costs, is in [docs/DECISIONS.md](docs/DECISIONS.md).

---

## Tech stack, and why

| Layer | Choice | Reason |
|---|---|---|
| Telephony / STT / TTS | **Vapi** | Audio transport, endpointing, and barge-in are solved commodity problems with no design decisions in them. Treated as a swappable I/O adapter — the agent holds no business logic. |
| LLM | **Claude Sonnet 5** | Strong instruction-following on tool-call sequencing, which matters because the server drives the conversation. |
| Backend | **FastAPI** | Pydantic makes the three-way validation result (`reason` / `reprompt` / `api_message`) natural to express, and the generated OpenAPI docs are a free deliverable. |
| Database | **PostgreSQL** (Supabase) | Survives restarts on ephemeral hosting, handles per-turn concurrent writes, and can express a partial unique index and regex `CHECK`s. See decision 2. |
| Hosting | **Render** | Deployed from this repo's `Procfile`; `/health` doubles as the platform health check. A durable hostname matters here because Vapi's tool URLs are baked into the assistant at provision time — an ephemeral tunnel would silently break every call after a restart. |

---

## Running it

### Local

```bash
git clone <repo> && cd voice-intake
python -m venv .venv && source .venv/Scripts/activate   # Windows
pip install -r requirements.txt

cp .env.example .env        # then fill in DATABASE_URL + VAPI_SERVER_SECRET
python -m scripts.seed      # optional: two demo patients

uvicorn app.main:app --reload
```

Open <http://localhost:8000/docs>.

### Exposing it to Vapi

```bash
ngrok http 8000                       # copy the https URL
# set PUBLIC_BASE_URL in .env, then:
python -m vapi.provision              # creates/updates the assistant
python -m vapi.provision --list-numbers
python -m vapi.provision --attach <phone_number_id>
```

The assistant is defined by [`vapi/assistant.json`](vapi/assistant.json) and
[`prompts/intake_agent.v1.md`](prompts/intake_agent.v1.md) — not by clicking
around the dashboard — so the prompt and tool schemas are reviewable here and
the deployment is reproducible.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection string |
| `VAPI_SERVER_SECRET` | yes | Shared secret; every `/voice/*` call must present it as `X-Vapi-Secret`. The endpoints **fail closed** if it is unset. |
| `VAPI_API_KEY` | provisioning only | Used by `vapi/provision.py` |
| `PUBLIC_BASE_URL` | provisioning only | Where Vapi should send tool calls |
| `LOG_LEVEL` | no | Defaults to `INFO` |

No secrets are committed; `.env` is gitignored and `assistant.json` carries
placeholders that `provision.py` fills at deploy time.

---

## API

All responses use the envelope `{"data": ..., "error": ...}`.

| Method | Endpoint | Notes |
|---|---|---|
| `GET` | `/patients` | Filters: `?last_name=`, `?date_of_birth=`, `?phone_number=`. Also `limit` / `offset`. Phone and DOB filters accept any format. |
| `GET` | `/patients/{id}` | 404 if missing or soft-deleted |
| `POST` | `/patients` | 201 on success, 422 on validation, 409 on duplicate active phone |
| `PUT` | `/patients/{id}` | Partial update; validates the same way |
| `DELETE` | `/patients/{id}` | Soft delete — sets `deleted_at`, never removes the row |

```bash
curl -X POST $API/patients -H 'content-type: application/json' -d '{
  "first_name":"Jane","last_name":"Doe","date_of_birth":"03/04/1985",
  "sex":"Female","phone_number":"(415) 555-0147",
  "address_line_1":"22 Market St","city":"San Francisco",
  "state":"California","zip_code":"94103"}'
```

Note that `"California"` is stored as `CA` and `"(415) 555-0147"` as
`+14155550147` — the REST API and the voice agent share one normalisation
path.

### Voice tool endpoints

`POST /voice/{start,lookup,capture,confirm,restart,finalize,event}` — internal, not
part of the public contract, secret-protected. Documented in
[`app/adapters/voice.py`](app/adapters/voice.py).

---

## Edge cases

The brief names four. Each is handled structurally rather than by prompt
instruction:

**Invalid date of birth.** `validators.py` rejects unparseable dates, future
dates, and ages over 130, returning a specific spoken re-prompt naming what
was wrong. The rejected field also stays in `next_field` (and is named in
`retry_field`), so the agent — which is instructed to follow `next_field` —
re-asks for that field instead of advancing past it. The database
independently enforces `CHECK (date_of_birth <= CURRENT_DATE)`.

**Connection drops mid-call.** The draft is already in Postgres. On redial,
`/voice/start` finds the in-progress session by caller ID (within 30
minutes), adopts it, and returns `resumed: true` with a hint telling the
agent to acknowledge the disconnection and continue from `next_field`. The
caller does not repeat themselves. *Try this — hang up halfway and call
back.*

**Database write fails.** `finalize()` catches, logs with a stack trace, and
returns `status: "error"` with a caller-facing sentence the agent speaks:
the caller hears an apology and a follow-up promise, never silence. Retries
are idempotent — a timed-out tool call that Vapi re-sends returns
`already_saved` rather than creating a second patient.

**Caller wants to start over.** `restart_registration` clears the draft and
keeps the session; the state machine resets to `first_name`.

Also handled: several fields given in one breath (batched into one capture);
answering a question that wasn't asked (accepted, then the outstanding one
is re-asked); returning callers recognised by caller ID before they speak;
`"California"`/`"CA"`, `"prefer not to say"`, and `"jane at gmail dot com"`
all normalised.

---

## Tests

The suite calls `drop_all()`, so `tests/conftest.py` **refuses to run against
a remote database** — pointed at the deployed Postgres, that would be a
production wipe. Give it a throwaway local database explicitly:

```bash
# any local Postgres; the guard requires a localhost/127.0.0.1 host
TEST_DATABASE_URL=postgresql://postgres:pw@localhost:5432/intake_test pytest -v
```

Running a bare `pytest` against the deployed `DATABASE_URL` exits with a
`REFUSING TO RUN` message. That is the guard working, not a broken suite.

`tests/test_api.py` covers the REST contract — status codes, envelope
shape, filters, partial updates, and that a soft-deleted patient disappears
from reads while the row survives with `deleted_at` set.

`tests/test_voice_flow.py` covers the claims this design is built on:

- `test_dropped_call_resumes_from_caller_id` — the resilience story
- `test_save_is_idempotent_on_retry` — no duplicate on a Vapi retry
- `test_voice_and_rest_produce_identical_records` — proves both adapters
  share one service layer, rather than just asserting it in prose
- `test_invalid_field_returns_speakable_reprompt` — validation returns
  something a human can say
- `test_rejected_required_field_stays_in_next_field` and
  `test_three_digit_phone_reprompts_for_phone` — the spec's two named
  invalid-input examples re-prompt for *that* field rather than moving on
- `test_rejected_optional_field_does_not_block_confirmation` — a garbled
  email cannot hold the call hostage
- `test_readback_includes_every_collected_field` — the confirmation step
  speaks all collected data, including the emergency contact number

---

## Observability

Structured JSON to stdout, one object per line. Key events: `call.started`,
`call.resumed`, `call.captured` (accepted and rejected field names),
`call.restarted`, `call.completed` (with the full saved payload, per the
brief), `call.abandoned`, `patient.created/updated/deleted`.

Transcripts arrive on Vapi's `end-of-call-report` webhook and are stored on
`call_sessions.transcript`, linked to the patient record.

---

## Known limitations

- **Resume keys on caller ID alone**, with no verification. Spoofable, and
  shared lines would collide. A deliberate demo-smoothness trade-off — see
  decision 4. Production should confirm one known field first.
- **No migrations.** `create_all()` at startup; a schema change means
  dropping the table.
- **Re-prompt strings are English-only.** The multi-language bonus would
  need them externalised into a message catalogue.
- **No rate limiting** on the public API, and no auth on `/patients` — the
  brief scoped this to demographics-only with no real patient data.
- **`preferred_language` is recorded but does not switch the agent's
  language** mid-call.
- **Address is not verified** against a postal database; a valid-looking
  ZIP that doesn't match the city is accepted.
- **Transcript storage is best-effort** — it depends on Vapi's webhook
  arriving.
- **Hosted on Render's free tier**, which spins the service down after
  ~15 minutes of inactivity. The first request after an idle period pays a
  cold start of roughly 30-50 seconds, so the very first call of the day may
  hit a slow first tool response. Subsequent calls are warm. Hitting
  `/health` shortly before a review call avoids this entirely.
- **Connection pool is sized for a demo** (`pool_size=5, max_overflow=5`).
  Because the design writes once per conversational turn, several truly
  concurrent calls plus the test suite can exhaust Supabase's pooler. Fine
  for review-scale traffic; a real deployment wants pgbouncer sizing tuned
  to expected concurrency.

---

## Next steps

1. Alembic migrations.
2. Confirm one known field before adopting a resumed draft.
3. Externalise re-prompts into a message catalogue, then enable Spanish —
   the state machine is already language-agnostic, only the strings are not.
4. Appointment scheduling after registration (mock slots).
5. A read-only dashboard over `GET /patients`.
6. USPS or Smarty address verification on the city/state/ZIP triple.
7. Load-test concurrent calls; add a write-behind cache for `draft` if
   per-turn writes become a bottleneck.
