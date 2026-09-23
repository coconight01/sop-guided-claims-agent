# Northstar Claims — SOP-guided conversational agent

A dependency-free Python web app with a responsive test UI. The supplied starter fixtures are retained under `apps/insurance_claims/fixtures` and are the demo's only claim records.

**Public demo:** <https://coconight01.github.io/sop-guided-claims-agent/>. The GitHub Pages version runs the same Python SOP engine in the browser via Pyodide, using only synthetic starter fixtures. It makes no model API calls and does not accept an API key. Because static-site data can be inspected by visitors, this is a workflow demonstration, not a real identity or privacy boundary. The Python server below enforces the gate before returning claim data and can use a model token stored on the server.

## Run

Python 3.11 or newer:

```bash
python server.py
```

Open <http://localhost:8080>. The demo works without a model token using deterministic fallback responses. To enable AI interpretation and phrasing, set `AI_API_TOKEN` in your environment before starting. `AI_BASE_URL` (OpenAI-compatible chat completions endpoint base) and `AI_MODEL` are configurable. The token stays server-side.

In PowerShell, for example: `$env:AI_API_TOKEN="YOUR_TOKEN"; python server.py`. See `.env.example` for every optional variable. No package installation is required.

### Try a free model

A Google AI Studio Gemini API key can be used with the existing OpenAI-compatible adapter. Check the [current free-tier quota](https://ai.google.dev/gemini-api/docs/pricing) for your region and model. Free-tier inputs may be used to improve Google's products, so use only the synthetic sample claims here.

In PowerShell, set these variables **in the terminal running the backend**, then start the server:

```powershell
$env:AI_API_TOKEN = "your-test-key"
$env:AI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
$env:AI_MODEL = "gemini-3.8-flash"
python server.py
```

Create a project key in [Google AI Studio](https://aistudio.google.com/apikey). Never put the key in the chat UI, the public GitHub Pages demo, a commit, or the submission ZIP. The public Pages URL remains a no-token workflow preview; hosting the model-backed backend needs a server with the key set as a secret. The adapter also accepts other OpenAI-compatible chat completion APIs.

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

Other useful tests: state only name and DOB plus policy number (verification stays locked); provide phone or email as the third detail; say “I already told you who I am. This is ridiculous”; ask “What is RL?” three times; ask about another person's `CL-3001`; or claim to be representative David Chen.

## SOP design

| Phase | Code-controlled invariant | Flexible behavior |
| --- | --- | --- |
| `VERIFY_ID` | Match at least three distinct PII fields to one holder. Policy number is only a lookup hint. No claim record or claim details are exposed. | Partial answers, alternate fields, empathy, refusal handling, and memory of early claim hints. |
| `RESOLVE_INTENT` | Only consider claims owned by the verified party. Resolve an explicit ID or bounded type/status/date clues. Ask when ambiguous. | Optional model intent classification and selection among the bounded candidate list. |
| `PROCESS_CASE` | Claim data and guidance fixtures provide all factual content. A model can rephrase the grounded answer, subject to validation; errors fall back to deterministic wording. | Natural questions about denial, status, documents, submission, timing, and payment. |
| `POST_PROCESS` | Offer an email summary and require an affirmative send choice; skip is equally available. | The caller can ask another claim question and return to processing. |

Out-of-scope prompts receive a polite boundary; repeated attempts trigger a human handoff notice. Repeated refusal or an explicit human request also triggers handoff. Representatives are routed to a human because the fixtures do not provide an authorization grant. The demo never claims that a document was submitted or a claim decision changed.

Sessions are memory-only, expire after four hours, and are never written to disk. The UI displays the verified claim only after the gate. This is a demo identity check against synthetic fixture data, not production authentication; production use would require a secure identity provider, audit logging, access controls, and a configured delivery service.

## Tests

```bash
python -m unittest discover -s tests -v
```

The test suite covers the supplied Margaret scenario, early memory, three-field gating, wrong fields, aliases, refusal, escalation, representative authorization, skip, and cross-policy access.

## Rebuild the public demo

Run `python build_pages.py` after changing the engine, fixtures, or web UI. Commit `docs/` and publish the `main` branch's `/docs` folder through GitHub Pages. The generated demo uses a pinned Pyodide runtime from jsDelivr. No credentials belong in `docs/` or the repository.
