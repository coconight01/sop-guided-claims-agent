const phases = ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"];
const labels = ["Verify identity", "Find your claim", "Review your claim", "Finish up"];
const $ = id => document.getElementById(id);
let sessionId = sessionStorage.getItem("northstar-session") || "";
let busy = false;
let current = null;
const fieldLabels = {name: "Full name", dob: "Date of birth", phone: "Phone", email: "Email", id_last4: "ID last four"};
const demoCaller = "I’m the policyholder. My name is Margaret Chen, policy POL-9921. I’m calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.";

function setTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("themeToggle").replaceChildren(dark ? "☀" : "☾", node("span", "", dark ? " Light mode" : " Dark mode"));
  $("themeToggle").setAttribute("aria-pressed", String(dark));
  $("themeToggle").setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
  document.querySelector('meta[name="theme-color"]').content = dark ? "#25343b" : "#f4f7f3";
  try { localStorage.setItem("northstar-theme", theme); } catch {}
}

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

let savedTheme = "light";
try { savedTheme = localStorage.getItem("northstar-theme") || "light"; } catch {}
setTheme(savedTheme);
$("themeToggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

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

function chips(id, items) {
  $(id).replaceChildren(...items.map(item => node("span", "chip", item)));
}

function quickActions(items) {
  const actions = $("quickActions");
  actions.replaceChildren(...items.map(([label, message]) => {
    const button = node("button", "", label);
    button.type = "button";
    button.addEventListener("click", () => message ? send(message) : $("newChat").click());
    return button;
  }));
}

function render(state) {
  const accessRevoked = current?.verified && !state.verified && state.human_transfer;
  current = state;
  if (accessRevoked) {
    $("messages").replaceChildren();
    const handoff = state.turns.at(-1);
    if (handoff?.role === "assistant") addMessage("assistant", handoff.text);
  }
  const phaseIndex = phases.indexOf(state.phase);
  $("steps").replaceChildren(...phases.map((phase, i) => {
    const li = node("li", i < phaseIndex ? "done" : phase === state.phase ? "active" : "");
    li.append(node("span", "step-number", i < phaseIndex ? "✓" : String(i + 1)));
    li.append(node("span", "", labels[i]));
    return li;
  }));
  $("phasePill").textContent = state.human_transfer ? "Representative requested" : ["Identity check", "Finding your claim", "Claim review", "Summary choice"][Math.max(phaseIndex, 0)];
  $("identitySignal").textContent = state.verified ? "Verified" : `${Math.min(state.collected_fields.length, 3)} of 3 details shared`;
  chips("identityChips", (state.verified ? state.verified_fields : state.collected_fields).map(key => fieldLabels[key] || key));
  $("memorySignal").textContent = state.claim ? state.claim.case_id : state.memory_saved ? "Noted for after verification" : "Waiting for details";
  chips("memoryChips", state.claim ? [] : state.memory_tags || []);
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
  if (state.human_transfer || state.closed) quickActions([["Start a new conversation", ""]]);
  else if (state.phase === "VERIFY_ID") quickActions([["Try sample conversation", demoCaller], ["Why verify?", "Why do you need to verify my identity?"]]);
  else if (state.phase === "RESOLVE_INTENT") quickActions([["Show my claims", "Which claims do I have?"]]);
  else if (state.phase === "PROCESS_CASE") quickActions([
    ...(state.claim?.status === "denied" ? [["Why was it denied?", "Why was it denied?"], ["What should I do next?", "What should I do next?"]] : [["What's the status?", "What's the status?"], ["Payment details", "How much was paid?"]]),
    ["That's all", "That's all, thanks."],
  ]);
  else if (state.phase === "POST_PROCESS") quickActions([["Send email summary", "send it"], ["Skip email", "skip"]]);
  else quickActions([]);
}

async function loadSession() {
  try {
    const res = await fetch(`/api/session?id=${encodeURIComponent(sessionId)}`);
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
  const typing = node("div", "message assistant typing");
  typing.append(node("div", "small-avatar", "N"), node("div", "bubble", "Typing…"));
  typing.setAttribute("aria-label", "Claims support is typing");
  $("messages").append(typing);
  $("messages").scrollTop = $("messages").scrollHeight;
  try {
    let res = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({session_id: sessionId, message})});
    if (res.status === 404) {
      const fresh = await (await fetch("/api/session")).json();
      sessionId = fresh.session_id;
      sessionStorage.setItem("northstar-session", sessionId);
      res = await fetch("/api/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({session_id: sessionId, message})});
    }
    const data = await res.json();
    typing.remove();
    if (!res.ok) throw new Error(data.error || "Something went wrong");
    addMessage("assistant", data.reply);
    render(data.session);
  } catch (error) {
    typing.remove();
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
    const res = await fetch("/api/reset", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({session_id: sessionId})});
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
