/* GitHub Pages adapter: the original Python engine runs in WebAssembly.
   No customer data or API keys are sent to a model or Goaly from this demo. */
const ASSET_VERSION = "c4ad0f5218";
let pythonRuntime;
const demoReady = (async () => {
  if (typeof loadPyodide !== "function") throw new Error("Python demo runtime could not load");
  const py = await loadPyodide({indexURL: "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/"});
  py.FS.mkdirTree("/app/apps/insurance_claims/fixtures");
  const files = ["engine.py", "llm.py", ...[
    "claims.json", "claim_schema.json", "consent_scenarios.json", "policyholders.json",
    "representatives.json", "required_document_guideline.json"
  ].map(name => `apps/insurance_claims/fixtures/${name}`)];
  await Promise.all(files.map(async path => {
    const res = await fetch(`${path}?v=${ASSET_VERSION}`);
    if (!res.ok) throw new Error(`Could not load ${path}`);
    py.FS.writeFile(`/app/${path}`, new Uint8Array(await res.arrayBuffer()));
  }));
  py.runPython(`
import json, os, sys
sys.path.insert(0, '/app')
os.environ.pop('AI_API_TOKEN', None)
os.environ.pop('SMTP_HOST', None)
from engine import Session, respond, ModelClient
_session = Session()
_model = ModelClient()
_model.enabled = False
def _state():
    return json.dumps(_session.public())
def _chat(message):
    reply = respond(_session, message, _model)
    return json.dumps({'reply': reply, 'session': _session.public()})
def _reset():
    global _session
    _session = Session()
    return json.dumps(_session.public())
`);
  pythonRuntime = py;
})();

async function demoFetch(url, options = {}) {
  await demoReady;
  const path = new URL(url, location.href).pathname;
  if (path.endsWith("/api/session")) {
    return {ok: true, status: 200, json: async () => ({session_id: "browser-demo", session: JSON.parse(pythonRuntime.runPython("_state()")), model_enabled: false})};
  }
  if (path.endsWith("/api/reset")) {
    return {ok: true, status: 200, json: async () => ({session_id: "browser-demo", session: JSON.parse(pythonRuntime.runPython("_reset()"))})};
  }
  if (path.endsWith("/api/chat")) {
    const {message} = JSON.parse(options.body || "{}");
    pythonRuntime.globals.set("_browser_message", message);
    const answer = JSON.parse(pythonRuntime.runPython("_chat(_browser_message)"));
    return {ok: true, status: 200, json: async () => answer};
  }
  return {ok: false, status: 404, json: async () => ({error: "Unknown demo route"})};
}
