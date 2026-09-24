# Northstar Claims — SOP-guided conversational agent

A dependency-free Python web app with a responsive test UI. The supplied starter fixtures are retained under `apps/insurance_claims/fixtures` and are the demo's only claim records.

**Public workflow preview:** <https://coconight01.github.io/sop-guided-claims-agent/>. **Model-backed demo:** <https://sop-guided-claims-agent.onrender.com/>. The GitHub Pages version runs the same Python SOP engine in the browser via Pyodide, using only synthetic starter fixtures. It makes no model API calls and does not accept an API key. Because static-site data can be inspected by visitors, this is a workflow demonstration, not a real identity or privacy boundary. The Python server below enforces the gate before returning claim data and can use a model token stored on the server.

## Run

Python 3.11 or newer:

```bash
python server.py
```

Open <http://localhost:8080>. The demo works without a model token using deterministic fallback responses. To enable AI interpretation and phrasing of case answers, set `AI_API_TOKEN` in your environment before starting. `AI_BASE_URL` (OpenAI-compatible chat completions endpoint base), `AI_MODEL`, and optional `AI_FALLBACK_MODEL` are configurable. The token stays server-side.

In PowerShell, for example: `$env:AI_API_TOKEN="YOUR_TOKEN"; python server.py`. See `.env.example` for every optional variable. No package installation is required.

### Try a free model

A Google AI Studio Gemini API key can be used with the existing OpenAI-compatible adapter. Check the [current free-tier quota](https://ai.google.dev/gemini-api/docs/pricing) for your region and model. Free-tier inputs may be used to improve Google's products, so use only the synthetic sample claims here.

In PowerShell, set these variables **in the terminal running the backend**, then start the server:

```powershell
$env:AI_API_TOKEN = "your-test-key"
$env:AI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
$env:AI_MODEL = "gemini-3.5-flash-lite"
$env:AI_FALLBACK_MODEL = "gemini-3.1-flash-lite"
python server.py
```

Create a project key in [Google AI Studio](https://aistudio.google.com/apikey). Never put the key in the chat UI, the public GitHub Pages demo, a commit, or the submission ZIP. The public Pages URL remains a no-token workflow preview; hosting the model-backed backend needs a server with the key set as a secret. The adapter also accepts other OpenAI-compatible chat completion APIs.

### Deploy the model-backed demo

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/coconight01/sop-guided-claims-agent)

The `render.yaml` Blueprint creates a Free Python web service, runs the tests during build, and prompts for `AI_API_TOKEN` as a secret. Sign in to Render, review the Free plan, and enter a **new** Gemini key in that prompt. Keep the key out of GitHub and the browser. The Free service can spin down after inactivity, so the first visit may take longer; sessions are in memory and may reset. See [Render's Free service limits](https://render.com/docs/free). The GitHub Pages link above remains the token-free preview.

Docker:

```bash
docker build -t northstar-claims .
docker run --rm -p 8080:8080 -e AI_API_TOKEN=YOUR_TOKEN northstar-claims
```

Optional real email delivery requires `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_FROM`. Without SMTP, explicit consent creates a clearly labeled demo outbox result and sends nothing externally. The email always goes to the verified policyholder's recorded address; the caller cannot redirect it.

## Test flow

Paste this in the UI:

> I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.

The agent verifies name, DOB, and last four, resolves the remembered January denial to `CL-2048`, and gives the fixture-backed denial reason. Ask “What documents do I need?”, “How do I submit them?”, or “How long after submission?” Then say “That's all” and choose **Send email summary** or **Skip email**.

Other useful tests: state only name and DOB plus policy number (verification stays locked); give details naturally across messages (“I'm Margaret Chen”, “born March 15, 1985”, “4472”); refuse one field (“I'm not giving you my SSN”) and use phone or email instead; say “I already told you who I am. This is ridiculous”; ask “What is RL?” three times; after verification ask “what's going on with my car accident claim?”, “what do I do now?”, “will it be approved?”, or “how much will I get?”; ask about another person's `CL-3001`; or claim to be representative David Chen.

## SOP design

| Phase | Code-controlled invariant | Flexible behavior |
| --- | --- | --- |
| `VERIFY_ID` | Match at least three distinct PII fields to one holder. Policy number is only a lookup hint. No claim record or claim details are exposed. After three different non-matching combinations the chat stops checking and hands off to a person. | Partial answers across messages, corrections, natural dates (“March 15, 1985”, “03/15/1985”), a bare “4472” reply, alternate fields when one is refused, empathy that says which details are already held, and memory of early claim hints (a birth month is never mistaken for a claim date). |
| `RESOLVE_INTENT` | Only consider claims owned by the verified party. Resolve an explicit ID or bounded type/status/date clues. Ask when ambiguous. | Everyday words (“car accident”, “dentist”), narrowing a list (“the healthcare one”, then “the recent one” or “the second one”), and answering the question that was asked before the claim was chosen. The model can choose among a bounded candidate list. |
| `PROCESS_CASE` | Claim data and guidance fixtures provide all factual content. A model draft is shown only if every number, date, and case ID in it appears in the verified claim record, it names the required facts, and it makes no promise or claimed action; otherwise code composes the answer from the record. | Natural questions about denial, status, next steps, likely outcome (never promised), appeal deadline (compared with today's date), documents, submission, receipt, timing, and payment. |
| `POST_PROCESS` | Offer an email summary and require an affirmative send choice; skip is equally available. A question during the choice never counts as consent. | Casual answers (“sure”, “nah I'm good”) work; the caller can ask another claim question and return to processing. The summary lists topics discussed, status, denial reason, and next steps per claim. |

**Callers other than the policyholder.** The assignment requires three matching PII fields; it does not say who may call. The starter data adds `representatives.json` (David Chen, Margaret Chen's son) and `consent_scenarios.json` (a consent record that goes `pending → approved`, or stays `pending` on timeout). The agent therefore accepts a representative only when all of these hold: the caller names themself as a representative listed for that policyholder, any stated relationship matches the record, the **policyholder's** three details match, and the simulated consent record reaches `approved`. Until then nothing about the claim is shared; the agent explains why consent is needed; a timeout, an unlisted person, a mismatched relationship, or another policyholder's details all go to a human. Set `CONSENT_SCENARIO=timeout` to demo the timeout path. Try: `I'm David Chen, Margaret Chen's son, calling about my mother's denied claim from January. Her DOB is 1985-03-15, SSN last four 4472.`, then `any update?`.

**Model freedom by phase.** `VERIFY_ID`: the model may only label an unclear message `claim` or `unrelated`; code writes every reply and decides verification. `RESOLVE_INTENT`: the model may pick one ID from the verified caller's own candidate claims; anything else is discarded. `PROCESS_CASE`: the model may label topics and emotion and draft wording, which code checks against the record. `POST_PROCESS`: no model call; consent, recipient, and summary are code only. `tests/test_phase_permissions.py` feeds hostile model output into every phase to prove these limits.

In `PROCESS_CASE` the model receives the caller's redacted message, the previous reply, and a fact sheet for the verified claim only (status, denial reason, documents, deadlines, amounts, and matching guidance; no name, contact details, or other claims). It returns JSON such as
`{"scope":"claim","topics":["denial_reason"],"emotion":"frustrated","reply":"..."}`. Code validates every label. The `reply` is a draft, not an answer: `grounded()` in `engine.py` rejects it if it contains a number, date, or case ID absent from the record, omits a required fact (for example the missing documents or the paid amount), promises approval or payment, claims receipt, or says the agent took an action. A rejected or missing draft falls back to the fixture-based template. Preferred names are conversation preferences, separate from the verified policyholder identity.

For a resolved case, the backend makes at most **one model request per ordinary user turn**. Before a case is open, the model is called only for a message that local rules cannot place (for example “any good laptops?”); it returns a single word, `claim` or `unrelated`, from a redacted message. The model never decides whether identity is verified, whether email consent was given, or which facts are true. Identity details, clear workflow commands, and obvious unrelated requests use no model call. Every caller message is redacted before it leaves the server: names (including the first name the agent used in its reply), email, phone, dates of birth, ID digits, and policy numbers. This keeps the Gemini 3.5 Flash-Lite demo within its project quota more comfortably. If the primary model is rate-limited, times out, or returns a server error, the same call tries Gemini 3.1 Flash-Lite. Authentication and request errors do not trigger a second model call. If both models are unavailable, the local bounded interpreter continues the SOP. These are separate per-model quotas, not extra requests against the primary quota. A conflicting identity claim pauses disclosure and routes to a representative; a preferred name request changes only how the caller is addressed.

Out-of-scope prompts receive a polite boundary; repeated attempts trigger a human handoff notice. Repeated refusal or an explicit human request also triggers handoff. Representatives are routed to a human because the fixtures do not provide an authorization grant. The demo never claims that a document was submitted or a claim decision changed.

Sessions are memory-only, expire after four hours, and are never written to disk. The UI displays the verified claim only after the gate. This is a demo identity check against synthetic fixture data, not production authentication; production use would require a secure identity provider, audit logging, access controls, and a configured delivery service.

## Tests

See [Assessment verification](ASSESSMENT.md) for prompt-by-prompt checks, authorization boundaries, and demo limits.

```bash
python -m unittest discover -s tests -v
```

The test suite (147 tests) covers the supplied Margaret scenario, early memory, three-field gating, wrong fields and lockout, natural date and name formats, aliases, refusal of individual fields, emotional recovery, escalation, representative authorization, claim narrowing and switching, grounded follow-ups, model-draft rejection and redaction, recipient restrictions, casual consent, session isolation, skip, and cross-policy access. `tests/test_conversation.py` holds the natural-language cases, including every adversarial prompt that exposed a problem on the hosted demo.

## Rebuild the public demo

Run `python build_pages.py` after changing the engine, fixtures, or web UI. Commit `docs/` and publish the `main` branch's `/docs` folder through GitHub Pages. The generated demo uses a pinned Pyodide runtime from jsDelivr. No credentials belong in `docs/` or the repository.
