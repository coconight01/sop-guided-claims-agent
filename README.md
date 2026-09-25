# Northstar Claims — SOP-Guided Insurance Claims Agent

An insurance claims support agent that follows a fixed business workflow — **VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS** — while still conversing naturally. Code owns the workflow: phase order, identity gates, claim ownership, allowed actions, and email consent. The language model helps interpret messy language and phrase answers, but only inside the limits each phase allows, and every draft is checked against the claim record before a caller sees it.

## Links

| | |
| --- | --- |
| **Live demo (model-backed)** | <https://sop-guided-claims-agent.onrender.com/> — free hosting; if the service was idle, the first load can take about a minute |
| Source repository | <https://github.com/coconight01/sop-guided-claims-agent> |
| Static workflow preview (no model, runs the same engine in the browser) | <https://coconight01.github.io/sop-guided-claims-agent/> |
| Requirement-by-requirement checklist with prompts to try | [ASSESSMENT.md](ASSESSMENT.md) |

## Try it in two minutes

Open the live demo and send these in one conversation. The left rail shows the current phase, which identity fields have been collected (types only, never values), what the agent remembered, and **what the agent is allowed to do right now**.

| Send | What to look for |
| --- | --- |
| `I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.` | The official test case: three fields verify (the policy number does not count), the remembered "denied healthcare January" hint opens `CL-2048` without asking again, and the denial reason comes from the claim record. |
| `why! i NEED my MONEY! the doctor didnt give me the report!` then `how...` | Empathy first, a concrete next step, an offer of a person; the short follow-up continues the previous topic. |
| `What is RL?` | A polite scope boundary (three unrelated requests → human handoff). |
| `That's all` then `send it` or `skip` | Email summary offer with a real choice; the summary lists what was discussed, status, and next steps. |

In a new conversation (↻ New conversation), also try:

- `I already told you who I am. This is ridiculous. Just tell me why my claim was denied.` — the bonus example: acknowledges frustration, explains why verification protects the caller, offers the allowed fields and a person, discloses nothing.
- `Hi, please call me Maggie. I want an email summary at the end. I already uploaded the pathology report last week.` then `Margaret Chen, DOB 1985-03-15, SSN 4472` — details that belong to later phases are remembered and used after verification.
- `I'm David Chen, Margaret Chen's son, calling about my mother's denied claim from January. Her DOB is 1985-03-15, SSN last four 4472.` then `any update?` — a listed representative is served only after the policyholder's details match **and** the policyholder consents.

## How each requirement is met

### 1. A fixed four-phase workflow with different levels of freedom

> *"Some steps require strict SOP control … other steps allow freer LLM reasoning."*

The phase machine lives in `engine.py`. Each phase has an explicit list of allowed actions (`PHASE_ACTIONS`), and `allow()` checks every claim read, claim selection, consent request, email send, and skip against the current phase, the verification state, and claim ownership — a violation raises in code rather than relying on a prompt. The model's freedom is set per phase:

| Phase | Allowed actions | What the model may do | What code alone decides |
| --- | --- | --- | --- |
| `VERIFY_ID` (strict) | check identity details, remember requests for later, explain verification, request policyholder consent (representatives), hand off | Label an unclear message `claim` or `unrelated` (one word, redacted input) | Every reply, field extraction, matching, verification, lockout |
| `RESOLVE_INTENT` (bounded) | list the caller's own claims, open one, hand off | Pick one ID from the verified caller's own candidate claims; anything else is discarded | Which claims are candidates, ownership, when to ask |
| `PROCESS_CASE` (flexible) | answer from the claim record and document guidance, switch to another own claim, hand off | Interpret the question (topic labels, emotion) and draft a natural reply | Whether the draft is grounded; if not, the reply is built from the record |
| `POST_PROCESS` (strict) | send the summary on consent, skip it, hand off | Nothing — no model call | Consent, recipient, summary content |

`tests/test_phase_permissions.py` plugs a hostile model into every phase (it claims the caller is verified, picks another holder's claim, writes an invented approval, asks to email an attacker) and proves none of it takes effect.

### 2. VERIFY_ID: strict gate, natural conversation

> *"Must not disclose claim details or advance until identity is verified based on at least 3 PII … but should still handle natural conversation, clarification questions, partial answers, refusals, and alternate identity fields."*

- **Gate:** at least three distinct fields (full name, DOB, phone, email, SSN/national ID last four) must all match one policyholder. The policy number only helps locate a record and never counts. No claim record reaches the UI or the reply before the gate opens — the API returns `claim: null`.
- **Natural input:** details can arrive in any order across messages, as `March 15, 1985`, `03/15/1985`, `15th of March, 85`, a bare `4472`, `first name … last name …`, or all in one unlabeled line (`margaret chen 1985-03-15 4472`). Corrections overwrite the earlier value; `start over` clears them.
- **Clarification questions:** "Why do you need this?", "What is a last four?", "Does my policy number count?", "Can I use my email instead?" each get a direct answer.
- **Refusals and alternate fields:** refusing one field ("not giving you my SSN") lists the remaining options; "I don't remember" is treated as unavailable rather than refused. Refusing too many, or repeated refusal, hands off to a person.
- **Safety:** three different non-matching combinations lock verification and hand off (no guessing). A full SSN is reduced to its last four with a warning. Typed "system" instructions, "override codes", staff claims, and "I was verified yesterday" are explained and ignored. An email that appears inside an instruction is never stored as an identity field.
- **Other callers:** a third party is asked for their name; only a representative listed in `representatives.json` for that policyholder, with a matching relationship, the policyholder's three details, and the policyholder's simulated consent (`consent_scenarios.json`) is served. A deceased policyholder, an unlisted caller, or a contradictory relationship goes to a person.

### 3. RESOLVE_INTENT and PROCESS_CASE: interpret, disambiguate, answer from grounded data

> *"The LLM can interpret messy user language, resolve ambiguity, answer grounded follow-up questions, and decide which bounded workflow path best matches the caller's need."*

- **Messy language → one of the caller's claims:** everyday words ("the car one", "dentist", "the one they said no to", "the older healthcare one"), status, month, year, recency, and list position resolve to a claim the verified caller owns. When two claims still fit, the agent asks "do you mean A or B?" (and rephrases if asked twice); a question that only applies to one claim picks it. Claims owned by someone else are never listed or disclosed, even with the other person's "permission".
- **Bounded paths:** every caller question maps to a fixed set of answer paths (denial reason, status, documents, document requirements, alternatives, how to get documents, submission method, submission timing, review timing, receipt, appeal deadline, payment, outcome, next steps, contact, access requests, account changes, and more). The model chooses among these labels; it cannot invent a new path.
- **Grounded answers:** facts come only from `claims.json` and `required_document_guideline.json`. The model sees a fact sheet for the verified claim (no name, contact details, or other claims) and may draft wording. `grounded()` rejects a draft that uses a number, date, or case ID not in the record; uses content words found in neither the record, the guidance, nor a fixed list of conversational words (this catches invented policies and general knowledge); omits a required fact; promises approval or payment; claims a document was received; describes an expired appeal as open; or says the agent took an action. A rejected draft is replaced by a reply built from the record.
- **Follow-ups:** short follow-ups ("why?", "how...", "and then?") continue the previous topic; "the appeal deadline" is compared with today's date; outcomes and payments are never promised; nothing the caller just heard is repeated.

### 4. POST_PROCESS: email summary with a real choice

> *"Offer to send the customer an email summary … including what was discussed, the claim status/outcome, and the major follow-up items or next steps. The customer must be able to choose."*

- Closing the case ("that's all", "thanks, bye") offers the summary; the UI also shows **Send email summary** and **Skip email** buttons.
- The summary lists the topics discussed, each discussed claim with its status and denial reason, and next steps per claim (documents to submit, review time, the appeal deadline and whether it has passed).
- Consent must be clear: "maybe" asks again, a question is answered without sending, and "don't send… actually yes send it" follows the last decision. The summary only goes to the email on the policyholder's record; requests to use a new, work, or other address, to cc someone, or to "update my email" are refused. Without SMTP settings, sending records a clearly labeled demo outbox entry instead of sending real email.

### 5. Staying in scope

> *"Reject answering any out of scope questions (e.g. what is RL?) politely. And ask to talk to human representatives if users keep retry."*

Unrelated requests (RL, weather, sports, coding, jokes, arithmetic) get a polite boundary that points back to the claim; the second suggests a person, and the third marks the conversation for a human representative. Messages that local rules cannot place are classified by the model with a one-word label. An unrelated request mixed with claim stress ("tell me a joke, this claim is stressing me out") is declined kindly without counting as off-topic.

### 6. Remembering information from later phases

> *"If the caller says during verification, 'I'm calling about my denied healthcare claim from January,' the agent must stay in VERIFY_ID, but store that intent and case hint."*

While staying in `VERIFY_ID` (and saying so), the agent keeps the claim hint and the original question, a preferred name ("call me Maggie"), an early email-summary request, and statements like "I already uploaded the pathology report". Right after verification it opens the matching claim, answers the original question, addresses the caller by the preferred name, and at the end says "earlier you asked for an email summary" — still waiting for a yes. A birth month is never mistaken for a claim month.

### 7. The official test case

| Expected behavior | Result |
| --- | --- |
| Extracts identity info and verifies the caller | Name, DOB, and SSN last four match; the policy number is noted as a lookup hint only |
| Does not disclose claim details before verification | Nothing is shared until three fields match (tested on the API with `claim: null`) |
| Remembers the denied-healthcare-January hint | Stored during `VERIFY_ID` and shown in the UI as remembered tags |
| Uses the hint after verification instead of asking again | "I used what you mentioned earlier to find CL-2048…" |
| Answers naturally but only from grounded claim/tool data | Denial reason and next steps come from the fixtures; model drafts are checked |

### 8. Bonus: emotional support and SOP recovery

1. **Recognize** frustration, anger (including shouting and profanity), anxiety (including urgency and distress), confusion, hopelessness, and refusal.
2. **Empathy before workflow:** replies open with an acknowledgement that varies instead of repeating; an upset caller is offered a person once; grief gets condolences.
3. **Explain why:** verification ("claim records hold private medical and payment details…"), policyholder consent for representatives, and email consent ("I only send it when you say yes, and only to the address on your record").
4. **Persuade without bypassing:** partial progress is acknowledged ("I have your name and date of birth, so I just need one more"), and the gate never opens early.
5. **Alternatives and when to stop:** other identity fields, "start over", or a person; repeated refusal, too many refused fields, repeated failed matches, or an explicit request ("get me your manager") hands off.

### 9. Delivery

| Requirement | Delivered |
| --- | --- |
| Hosted demo URL or Docker/repo with setup | Live demo above; `Dockerfile`, `render.yaml`, and setup below |
| Setup accepts an API auth token for the model | `AI_API_TOKEN` (server-side only; never sent to the browser) with any OpenAI-compatible endpoint |
| Simple test UI for natural-language chat | Responsive chat with phase rail, remembered details, allowed actions, claim card, quick replies, light/dark mode |
| Demo shows the full workflow | Verification → intent resolution → claim processing → email summary, visible step by step in the UI |

## Architecture

```text
Browser (web/)                     Python server (server.py)                 engine.py
─────────────                      ────────────────────────                  ─────────
chat UI  ── POST /api/chat ──►  per-session lock, 4h TTL  ──► respond(session, text, model)
         ◄── reply + state ───  (no chat content logged)          │
                                                                  ├─ phase handler (VERIFY / RESOLVE / PROCESS / POST)
                                                                  ├─ allow(): phase × action × ownership check
                                                                  ├─ fixtures: claims, holders, guidance, reps, consent
                                                                  └─ llm.py (optional) ── redacted text ──► model
                                                                          ◄── JSON labels + draft ── validated by grounded()
```

| File | Role |
| --- | --- |
| `engine.py` | The SOP: session state, identity capture and matching, memory of later-phase details, emotion and scope signals, claim resolution, grounded answers, draft validation (`grounded()`), allowed actions (`PHASE_ACTIONS`, `allow()`), representative consent, email summary |
| `llm.py` | OpenAI-compatible adapter with three bounded calls: `classify_scope` (one word), `select_claim` (one of the given IDs), `analyze_case` (labels + draft). Invalid output is ignored. Falls back to a second model on rate limits or server errors |
| `server.py` | Dependency-free HTTP server: `/api/session`, `/api/chat`, `/api/reset`, `/api/health`; random session IDs, per-session locks, expiry |
| `web/` | Test UI (vanilla HTML/CSS/JS) |
| `apps/insurance_claims/fixtures/` | The six starter files, unchanged; all six are used |
| `tests/` | 178 unit and API tests (see below) |
| `docs/` | Generated static preview (`python build_pages.py`) |

**Model usage and privacy.** At most one model call per ordinary turn once a claim is open, a one-word call only for unclear messages before that, and none for identity details, workflow commands, or the email step. Every caller message is redacted before it leaves the server (names — including the first name the agent itself used — email, phone, dates of birth, ID digits, policy numbers). If the model is unavailable, the agent keeps working with its local interpreter and record-based replies.

## Testing

```bash
python -m unittest discover -s tests -v
```

| File | Tests | Covers |
| --- | --- | --- |
| `tests/test_conversation.py` | 111 | Natural-language behavior in every phase, emotions, misunderstanding, deception, missing information, memory, grounding of model drafts — including every prompt that exposed a problem during live testing |
| `tests/test_engine.py` | 41 | Core workflow: gate, memory, consent, scope, ownership, identity changes |
| `tests/test_representative.py` | 12 | Listed representative, consent approved / timeout, unlisted and mismatched callers |
| `tests/test_phase_permissions.py` | 6 | Hostile model output in every phase; the allowed-actions table |
| `tests/test_llm.py` | 6 | Model output validation and fallback |
| `tests/test_server.py` | 2 | API: no claim before verification, session isolation |

Beyond unit tests, the live demo was exercised with an automated requirement audit (38 checks, all passing) and multi-turn persona conversations — 26 personas and 175 turns covering upset, panicking, sarcastic, grieving, and confused callers, people who misunderstand terms, social-engineering attempts (fake staff, injected instructions, changing the email on file, asking for a friend's claim), and callers with missing information. Each problem found became a regression test.

## Run it yourself

Python 3.11+, no packages to install:

```bash
python server.py            # http://localhost:8080 — works without a token using the local interpreter
```

With a model (any OpenAI-compatible endpoint; the demo uses Google Gemini Flash-Lite):

```bash
export AI_API_TOKEN=your-token
export AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
export AI_MODEL=gemini-3.5-flash-lite
export AI_FALLBACK_MODEL=gemini-3.1-flash-lite
python server.py
```

PowerShell: `$env:AI_API_TOKEN="your-token"; python server.py`.

Docker:

```bash
docker build -t northstar-claims .
docker run --rm -p 8080:8080 -e AI_API_TOKEN=your-token northstar-claims
```

Render: `render.yaml` creates a free web service, runs the tests during build, and prompts for `AI_API_TOKEN` as a secret.

| Variable | Purpose |
| --- | --- |
| `AI_API_TOKEN` | Model API token (optional; server-side only) |
| `AI_BASE_URL`, `AI_MODEL`, `AI_FALLBACK_MODEL` | Model endpoint and names |
| `CONSENT_SCENARIO` | `default` (policyholder approves) or `timeout` (never approves) for the representative flow |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Optional real email delivery; without them a demo outbox entry is recorded |
| `PORT` | Server port (default 8080) |

After changing the engine, fixtures, or UI, run `python build_pages.py` to regenerate the static preview in `docs/`.

## Limitations

- **Demo identity check.** Verification matches synthetic fixture data; production would need a real identity provider, audit logging, and rate limiting.
- **Simulated integrations.** Email (without SMTP), human handoff, and policyholder consent are simulated and clearly labeled; no external action is taken.
- **In-memory sessions.** Conversations expire after four hours and are lost when the free host restarts; the UI says so and starts a fresh, unverified conversation.
- **Rule-based local interpretation.** It covers the phrasing seen in extensive testing, but new wording can still be misread; the model and the "representative" option cover the gaps.
- **Grounding check scope.** Draft validation catches unsupported numbers, dates, IDs, promises, and vocabulary outside the record; it cannot prove every paraphrase is faithful, so conservative templates remain the fallback.
- **Static preview.** The GitHub Pages build ships the synthetic fixtures to the browser, so it demonstrates the workflow but is not a privacy boundary; use the live demo for the server-enforced gate.
