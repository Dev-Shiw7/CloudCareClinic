# Design Decisions

Each entry: what was decided, what else was considered, why, and what it costs.

---

## 1. The voice agent holds no state

**Decision.** Conversational state lives in a `call_sessions.draft` JSONB
column, written on every turn. The LLM is told what to ask next by the
server, on every tool response.

**Alternative.** The usual approach: let the model accumulate all sixteen
fields in its context window and POST once at the end.

**Why.** Four separate requirements collapse into this one choice:
a dropped call loses nothing; validation cannot be skipped by a confused
model; "start over" is a single `DELETE`; and every call leaves an audit
trail. It also stops long calls from degrading — the model never has to
remember what it already asked.

**Cost.** One database write per conversational turn (~15-25 per call)
instead of one. Irrelevant at phone-call concurrency; at thousands of
concurrent calls this would want a write-behind cache. Also a slightly
chattier tool protocol, which is a latency cost of roughly 100-200 ms per
turn against a Postgres in the same region.

---

## 2. Postgres, not SQLite

**Decision.** Managed Postgres (Supabase).

**Alternative.** SQLite, which the brief explicitly blesses as a reasonable
shortcut.

**Why.** The brief also requires the system to be *live at review time* and
for data to survive restarts. On the ephemeral filesystems used by most free
hosting tiers, a SQLite file does not survive a spin-down or a redeploy —
and that failure is invisible during development, because the service is
never idle while you are working on it. Managed Postgres decouples data
lifetime from container lifetime. Secondarily, this design writes on every
turn, and SQLite's database-level write lock is a poor fit for concurrent
calls.

Postgres also lets the schema express things SQLite cannot: a partial unique
index (`UNIQUE(phone_number) WHERE deleted_at IS NULL`), regex `CHECK`
constraints, native `UUID`, `JSONB`, and `TIMESTAMPTZ`.

**Cost.** A network hop per query, and a second managed service to keep
alive. The data layer is SQLAlchemy, so the engine is a connection-string
change.

---

## 3. Validators return spoken re-prompts

**Decision.** A failed validation carries three renderings of the same
problem: `reason` (machine code), `reprompt` (a sentence the agent speaks),
and `api_message` (for the REST 422 body).

**Alternative.** Return error codes and let the prompt decide what to say.

**Why.** It puts the *content* of a correction under version control and
under test, while leaving *delivery* to the model — which is the split the
two systems are each good at. "I only caught 3 digits. Could you give me the
full 10-digit phone number, starting with the area code?" is a better
caller experience than anything a model reliably improvises from
`value_error.phone`.

**Cost.** Re-prompt strings are English-only, so the multi-language bonus
would need them externalised. Noted in the README's Next Steps.

---

## 4. Resume-after-drop keys on caller ID alone

**Decision.** If a caller redials within 30 minutes and has an in-progress
draft, it is adopted automatically with no verification.

**Alternative.** Confirm one known field ("can you confirm your date of
birth?") before resuming.

**Why.** Chosen for demonstration smoothness — the reviewer can hang up
mid-call, redial, and be picked up exactly where they left off.

**Cost — and this is a real one.** Caller ID is spoofable, and shared
household or office lines exist. In production this would leak one caller's
partial demographics to another. A production build should confirm a known
field before adopting a draft; the 30-minute window limits but does not
close the exposure. This is a deliberate assessment-scope trade-off, not an
oversight.

---

## 5. One service layer, two adapters

**Decision.** `services/` holds all business logic. `adapters/rest.py` and
`adapters/voice.py` are HTTP shells containing no SQL and no validation.

**Alternative.** Let the voice endpoints talk to the ORM directly, since
they have different shapes from the REST resources.

**Why.** The brief warns against relying on the agent for validation. Making
both paths traverse the same functions means a voice registration and a
`POST /patients` cannot diverge. `tests/test_voice_flow.py::
test_voice_and_rest_produce_identical_records` asserts exactly that.

**Cost.** The voice adapter does some shape translation that would be
unnecessary if it owned its own queries.

---

## 6. `create_all()` instead of Alembic

**Decision.** Tables are created at startup from the ORM metadata.

**Alternative.** Alembic migrations.

**Why.** Time budget. The schema has one version and no production data to
preserve.

**Cost.** No migration path. Changing a column means dropping the table.
This is the first thing to fix if the project continued.

---

## 7. Idempotent finalize

**Decision.** `save_registration` records the resulting `patient_id` on the
call session; a second call returns `already_saved` with the same id.

**Why.** Vapi retries tool calls that time out. Without this, a slow
database response produces two patients for one caller — and the partial
unique index would surface it as a confusing 409 mid-call rather than a
clean no-op.

**Cost.** None meaningful.

---

## 8. Chunked confirmation

**Decision.** The read-back is returned as three or four short groups
(identity, contact, address, extras), each spoken as its own sentence.

**Alternative.** One recital of all collected fields.

**Why.** Sixteen fields in one breath is unusable on a phone; the caller
cannot hold it in working memory long enough to spot an error, which defeats
the point of confirming. Chunking also lets a correction re-read one group
instead of everything.

**Cost.** Slightly more tool-response structure for the model to follow.

---

## 9. The opening line is fixed, not model-generated

**Decision.** `firstMessageMode: assistant-speaks-first` with a hardcoded
`firstMessage`. The model's first turn happens only after `start_call`
returns.

**Alternative.** Let the model generate the greeting
(`assistant-speaks-first-with-model-generated-message`), which reads as more
flexible.

**Why.** It was measurably worse. Because the model must call `start_call`
before it can know whether this is a new caller, a returning patient, or a
resumed draft, a model-generated opening improvises filler to cover the tool
latency — real transcripts from this number opened with *"This'll just take a
sec."* and *"1 moment."* before the greeting. No prompt instruction reliably
suppressed it, because the model is being asked to hold a turn open with
nothing to say. Pinning the greeting removes the choice: Vapi speaks it
instantly at pickup and `start_call` runs behind that audio, so the latency is
hidden by speech rather than papered over with a stall.

**Cost.** The greeting no longer adapts to a returning caller — everyone hears
the same first sentence, and the recognition ("we already have a record for
you") lands one turn later instead. Worth it: the first three seconds set
whether the caller believes they are talking to a person.

---

## 10. Idle nudges are interchangeable; the goodbye is not

**Decision.** `idleMessages` holds three neutral, order-independent nudges.
The actual sign-off lives in `silenceTimeoutMessage`.

**Why.** Vapi selects from `idleMessages` at **random**, not in order. A
three-step escalation written into that array produces exactly the wrong
call: a live transcript from this number played *"I can't hear anything on
the line — I'll let you go for now"* and then *"Are you still there?"*,
which sounds broken. Anything order-dependent cannot live there.
`silenceTimeoutMessage` is the only line guaranteed to be last, so it owns
the goodbye — and it tells the caller their details are saved, which is true,
because the draft is already in Postgres.

**Cost.** No gradual escalation in tone; the nudges are all equally gentle.

