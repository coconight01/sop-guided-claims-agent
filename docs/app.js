const phases = ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"];
const labels = ["Verify identity", "Find your claim", "Review your claim", "Finish up"];
const $ = id => document.getElementById(id);
let sessionId = sessionStorage.getItem("northstar-session") || "";
let busy = false;
let current = null;
const demoCaller = "I’m the policyholder. My name is Margaret Chen, policy POL-9921. I’m calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.";

function setTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("themeToggle").textContent = dark ? "☀ Light mode" : "☾ Dark mode";
  $("themeToggle").setAttribute("aria-pressed", String(dark));
  $("themeToggle").setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
  document.querySelector('meta[name="theme-color"]').content = dark ? "#25343b" : "#f4f7f3";
  try { localStorage.setItem("northstar-theme", theme); } catch {}
}
let savedTheme = "light";
try { savedTheme = localStorage.getItem("northstar-theme") || "light"; } catch {}
setTheme(savedTheme);
$("themeToggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function addMessage(role, text) {
  const wrapper = node("div", `message ${role}`);
  wrapper.append(node("div", "small-avatar", role === "assistant" ? "N" : "Y"));
  const content = node("div", "message-content");
  content.append(node("div", "bubble", text));
  content.append(node("div", "message-meta", role === "assistant" ? "Claims support" : "You"));
  wrapper.append(content);
  $("messages").append(wrapper);
  $("messages").scrollTop = $("messages").scrollHeight;
}

function render(state) {
  current = state;
  const phaseIndex = phases.indexOf(state.phase);
  $("steps").replaceChildren(...phases.map((phase, i) => {
    const li = node("li", i < phaseIndex ? "done" : phase === state.phase ? "active" : "");
    li.append(node("span", "step-number", i < phaseIndex ? "✓" : String(i + 1)));
    li.append(node("span", "", labels[i]));
    return li;
  }));
  $("phasePill").textContent = state.human_transfer ? "Representative requested" : ["Identity check", "Finding your claim", "Claim review", "Summary choice"][Math.max(phaseIndex, 0)];
  $("identitySignal").textContent = state.verified ? "Verified" : `${state.collected_fields.length} of 3 details shared`;
  $("memorySignal").textContent = state.memory_saved ? "Noted" : "Waiting for details";
  const card = $("caseCard");
  card.replaceChildren();
  if (state.claim) {
    card.append(node("div", "panel-kicker", "YOUR CLAIM"));
    card.append(node("h2", "", state.claim.case_id));
    card.append(node("p", "", state.claim.summary));
    const data = node("div", "case-data");
    [["Type", state.claim.case_type], ["Filed", state.claim.created_at], ["Status", state.claim.status]].forEach(([key, value]) => {
      const box = node("div"); box.append(node("small", "", key)); box.append(node("b", key === "Status" ? "status" : "", value)); data.append(box);
    });
    card.append(data);
  } else {
    card.append(node("div", "case-lock", "♙"));
    card.append(node("strong", "", "Claim details are protected"));
    card.append(node("p", "", "Share three matching identity details to open your record."));
  }
  const actions = $("quickActions");
  actions.replaceChildren();
  if (state.phase === "VERIFY_ID" && !state.human_transfer) {
    [["Try sample conversation", demoCaller], ["Why verify?", "Why do you need to verify my identity?"]].forEach(([label, message]) => {
      const button = node("button", "", label);
      button.type = "button";
      button.addEventListener("click", () => send(message));
      actions.append(button);
    });
  }
  if (state.phase === "POST_PROCESS" && !state.closed && !state.human_transfer) {
    [["Send email summary", "send it"], ["Skip email", "skip"]].forEach(([label, message]) => {
      const button = node("button", "", label);
      button.type = "button";
      button.addEventListener("click", () => send(message));
      actions.append(button);
    });
  }
}

async function loadSession() {
  try {
    const res = await demoFetch(`/api/session?id=${encodeURIComponent(sessionId)}`);
    const data = await res.json();
    sessionId = data.session_id;
    sessionStorage.setItem("northstar-session", sessionId);
    $("messages").replaceChildren();
    if (data.session.turns.length) data.session.turns.forEach(turn => addMessage(turn.role, turn.text));
    else addMessage("assistant", "Hello, I’m your claims support agent. I can help with a claim, and I’ll first verify your identity to protect your information. Tell me what brings you in, and share any three of your full name, date of birth, phone, email, or ID last four digits when you’re ready.");
    render(data.session);
  } catch (error) {
    console.error("Could not initialize claims chat", error);
    addMessage("assistant", "I couldn’t connect right now. Please refresh the page.");
  }
}

async function send(message) {
  if (busy || !message.trim()) return;
  busy = true;
  $("sendButton").disabled = true;
  $("messageInput").value = "";
  addMessage("user", message.trim());
  try {
    let res = await demoFetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({session_id: sessionId, message})});
    if (res.status === 404) {
      const fresh = await (await demoFetch("/api/session")).json();
      sessionId = fresh.session_id;
      sessionStorage.setItem("northstar-session", sessionId);
      res = await demoFetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({session_id: sessionId, message})});
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Something went wrong");
    addMessage("assistant", data.reply);
    render(data.session);
  } catch (error) {
    addMessage("assistant", `${error.message}. Try starting a new conversation.`);
  } finally {
    busy = false;
    $("sendButton").disabled = false;
    $("messageInput").focus();
  }
}

$("chatForm").addEventListener("submit", event => {event.preventDefault(); send($("messageInput").value);});
$("messageInput").addEventListener("keydown", event => {if (event.key === "Enter" && !event.shiftKey) {event.preventDefault(); send($("messageInput").value);}});
$("newChat").addEventListener("click", async () => {
  if (busy) return;
  try {
    const res = await demoFetch("/api/reset", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({session_id: sessionId})});
    const data = await res.json();
    sessionId = data.session_id;
    sessionStorage.setItem("northstar-session", sessionId);
    $("messages").replaceChildren();
    addMessage("assistant", "New conversation started. How can I help with your insurance claim? I’ll need three matching identity details before opening the record.");
    render(data.session);
  } catch { addMessage("assistant", "I couldn’t reset the conversation. Please refresh the page."); }
});
document.querySelectorAll(".scenario").forEach(button => button.addEventListener("click", () => {$("messageInput").value = button.dataset.text; $("messageInput").focus();}));
loadSession();
