"""Deterministic SOP gates and fixture-backed insurance claim workflow."""
from __future__ import annotations

import json
import re
import smtplib
import ssl
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from llm import ModelClient

FIXTURES = Path(__file__).parent / "apps" / "insurance_claims" / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


HOLDERS = fixture("policyholders.json")
CLAIMS = fixture("claims.json")
REPS = fixture("representatives.json")
GUIDE = fixture("required_document_guideline.json")
PII = ("name", "dob", "phone", "email", "id_last4")
PHASES = ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS")


@dataclass
class Session:
    phase: str = "VERIFY_ID"
    fields: dict[str, str] = field(default_factory=dict)
    policy_hint: str = ""
    case_hint: str = ""
    intent_hint: str = ""
    holder_id: str = ""
    case_id: str = ""
    intent: str = ""
    emotion: str = ""
    refusal_count: int = 0
    off_topic_count: int = 0
    human_transfer: bool = False
    closed: bool = False
    email_offered: bool = False
    email_result: str = ""
    email_preview: str = ""
    turns: list[dict[str, str]] = field(default_factory=list)
    updated_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())

    def public(self) -> dict[str, Any]:
        verified = bool(self.holder_id)
        return {
            "phase": self.phase,
            "phases": PHASES,
            "verified": verified,
            "verified_fields": sorted(self.fields) if verified else [],
            "collected_fields": sorted(self.fields) if not verified else [],
            "memory_saved": bool(self.case_hint or self.intent_hint),
            "claim": next((safe_claim(c) for c in CLAIMS if verified and c["case_id"] == self.case_id), None),
            "human_transfer": self.human_transfer,
            "closed": self.closed,
            "email_result": self.email_result,
            "email_preview": self.email_preview if verified else "",
            "turns": self.turns,
        }


def safe_claim(c: dict) -> dict:
    return {k: c[k] for k in ("case_id", "case_type", "created_at", "status", "summary")}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def exact_word(text: str, value: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(value.lower()) + r"(?!\w)", text.lower()))


def extract_fields(text: str, session: Session) -> None:
    """Only record explicit caller statements; matching is checked separately."""
    low = norm(text)
    for holder in HOLDERS:
        for name in [holder["name"], *holder.get("name_aliases", [])]:
            if exact_word(low, name) and not re.search(r"\b(?:not|isn't|is not)\s+" + re.escape(name.lower()), low):
                session.fields["name"] = name
        for email in [holder["email"], *holder.get("email_aliases", [])]:
            if email.lower() in low:
                session.fields["email"] = email
        for phone in [holder["phone"], *holder.get("phone_aliases", [])]:
            if len(digits(phone)) >= 10 and digits(phone)[-10:] in digits(text):
                session.fields["phone"] = phone
    declared_name = re.search(r"\b(?:my name is|i am|i'm)\s+([a-z][a-z' -]{2,60})", text, re.I)
    if declared_name:
        candidate = re.split(r"[,.;!?]|\s+(?:and|with|calling|about|the policyholder|a policyholder)\b", declared_name.group(1), 1, flags=re.I)[0].strip()
        if len(candidate.split()) in (2, 3) and not candidate.lower().startswith(("the ", "a ")):
            session.fields["name"] = candidate
    declared_email = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.I)
    if declared_email:
        session.fields["email"] = declared_email.group(0)
    phone_match = re.search(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)", text)
    if phone_match:
        session.fields["phone"] = phone_match.group(0)
    dates = re.findall(r"\b(?:19|20)\d\d[-/]\d\d?[-/]\d\d?\b", text)
    if dates:
        raw = dates[-1].replace("/", "-")
        pieces = raw.split("-")
        try:
            session.fields["dob"] = f"{int(pieces[0]):04d}-{int(pieces[1]):02d}-{int(pieces[2]):02d}"
        except ValueError:
            pass
    ssn = re.search(r"\b(?:ssn|social security|national id|id)\b.{0,30}?\b(\d{4})\b|\b(?:last four|last 4|last4)\b.{0,15}?\b(\d{4})\b", low)
    if ssn:
        session.fields["id_last4"] = ssn.group(1) or ssn.group(2)
    policy = re.search(r"\bPOL[-\s]?(\d{4})\b", text, re.I)
    if policy:
        session.policy_hint = "POL-" + policy.group(1)
    case = re.search(r"\bCL[-\s]?(\d{4})\b", text, re.I)
    if case:
        session.case_hint = "CL-" + case.group(1)


def matches(holder: dict, key: str, value: str) -> bool:
    if key == "name":
        return norm(value) in [norm(holder["name"]), *map(norm, holder.get("name_aliases", []))]
    if key == "email":
        return norm(value) in [norm(holder["email"]), *map(norm, holder.get("email_aliases", []))]
    if key == "phone":
        return digits(value)[-10:] in [digits(x)[-10:] for x in [holder["phone"], *holder.get("phone_aliases", [])]]
    return value == holder[key]


def verified_holder(session: Session) -> dict | None:
    if len(session.fields) < 3:
        return None
    candidates = [h for h in HOLDERS if all(matches(h, k, v) for k, v in session.fields.items())]
    if session.policy_hint:
        candidates = [h for h in candidates if h["policy_number"] == session.policy_hint]
    return candidates[0] if len(candidates) == 1 else None


def empathy(text: str) -> str:
    low = norm(text)
    if any(x in low for x in ("ridiculous", "angry", "furious", "unacceptable", "frustrat", "already told")):
        return "I understand why this is frustrating. "
    if any(x in low for x in ("worried", "anxious", "scared", "stressed", "afraid")):
        return "I can hear that this is worrying. "
    if any(x in low for x in ("confused", "don't understand", "unclear")):
        return "I can help make this clearer. "
    return ""


def is_off_topic(text: str) -> bool:
    low = norm(text)
    if any(x in low for x in ("reinforcement learning", "what is rl", "weather", "recipe", "bitcoin", "stock market", "tell me a joke", "capital of", "write code", "football score")):
        return True
    if low in ("hello", "hi", "how are you", "thanks", "thank you", "why?", "how?", "what next?"):
        return False
    scope = r"\b(?:claim|insurance|policy|denial|denied|appeal|document|report|payment|paid|verification|verify|identity|status|coverage|covered|deductible|reimbursement|submit|upload|adjuster|representative|pathology|portal|case|email|summary|next|missing|that|this|it|them|those)\b|office note|net pay|net fee|you need|need from me|help me"
    if re.match(r"^(?:what|who|where|when|why|how|can you|could you|please explain|please tell me|tell me about|explain)\b", low) and not re.search(scope, low):
        return True
    return False


def detect_hint(text: str, session: Session) -> None:
    low = norm(text)
    if any(x in low for x in ("claim", "denied", "appeal", "reimbursement", "covered", "documents", "status")):
        session.intent_hint = text[:500]
    if not session.case_hint:
        # Preserve natural date/type hints without disclosing or resolving a claim yet.
        if any(x in low for x in ("january", "february", "march", "healthcare", "dental", "auto")):
            session.case_hint = text[:500]


def choose_claim(text: str, session: Session, model: ModelClient) -> tuple[dict | None, list[dict]]:
    own = [c for c in CLAIMS if c["party_id"] == session.holder_id]
    full = norm(" ".join((session.case_hint, session.intent_hint, text)))
    explicit = re.findall(r"\bCL[-\s]?(\d{4})\b", full, re.I)
    if explicit:
        own = [c for c in own if c["case_id"] == "CL-" + explicit[-1]]
        return (own[0] if len(own) == 1 else None), own
    filtered = own
    for typ in ("healthcare", "medical", "dental", "auto"):
        if exact_word(full, typ):
            normalized = "healthcare" if typ == "medical" else typ
            filtered = [c for c in filtered if c["case_type"] == normalized]
            break
    if "denied" in full or "denial" in full:
        filtered = [c for c in filtered if c["status"] == "denied"]
    for month, number in (("january", "01"), ("february", "02"), ("march", "03"), ("november", "11")):
        if month in full:
            filtered = [c for c in filtered if c["created_at"][5:7] == number]
            break
    year = re.search(r"\b20\d{2}\b", full)
    if year:
        filtered = [c for c in filtered if c["created_at"].startswith(year.group())]
    if len(filtered) == 1:
        return filtered[0], filtered
    if len(filtered) > 1 and model.enabled:
        choice = model.select_claim(full, [safe_claim(c) for c in filtered])
        if choice:
            match = next((c for c in filtered if c["case_id"] == choice), None)
            if match:
                return match, filtered
    return None, filtered


def detect_intent(text: str, model: ModelClient) -> str:
    low = norm(text)
    if model.enabled:
        result = model.classify_intent(text)
        if result:
            return result
    if any(x in low for x in ("why", "denied", "denial", "reason")):
        return "denial_question"
    if any(x in low for x in ("document", "upload", "submit", "send", "paperwork", "report")):
        return "document_submission"
    if any(x in low for x in ("status", "progress", "where is", "update")):
        return "status_inquiry"
    if any(x in low for x in ("next", "appeal", "what do i do")):
        return "next_steps"
    return "general_claim_question"


def grounded_answer(claim: dict, text: str, intent: str, model: ModelClient) -> str:
    low = norm(text)
    cid = claim["case_id"]
    docs = claim.get("documents_needed", [])
    doc_list = ", ".join(docs)
    parts = []
    if any(x in low for x in ("why", "reason", "denied", "denial")) and claim["status"] == "denied":
        parts.append(f"{cid} was denied because {claim['denial_reason']}.")
    if any(x in low for x in ("status", "progress", "outcome", "update")) or not parts and intent in ("status_inquiry", "general_claim_question"):
        parts.append(f"{cid} is currently {claim['status']}.")
    if docs and any(x in low for x in ("need", "document", "next", "appeal", "submit", "send", "report", "what do i do")):
        parts.append(f"The file needs {doc_list}.")
    if "deadline" in low or "appeal" in low:
        if claim.get("appeal_deadline"):
            parts.append(f"The recorded appeal deadline was {claim['appeal_deadline']}. Please ask a human representative about current options if you have not already submitted an appeal.")
    if any(x in low for x in ("paid", "payment", "reimburse", "amount", "dollar", "money")):
        parts.append(f"The recorded net payment is ${claim['net_pay']} and the expected reimbursement is ${claim['expected_reimbursement_amount']}.")
    if docs and any(x in low for x in ("how do i submit", "where do i", "upload", "portal", "fax", "mail")):
        parts.append(GUIDE["default_guidance"]["en"])
    if docs and any(x in low for x in ("how long", "processing time", "once i submit", "after i submit", "after i send")):
        avg = GUIDE["claim_followup_settings"]["average_processing_time_after_submission"]["en"]
        parts.append(f"After the missing files are received, review usually takes {avg}; intake or another review cycle may take longer.")
    if docs and any(x in low for x in ("how soon do i need to submit", "how soon do i need to send", "when do i need to submit", "when should i send", "when should i submit")):
        parts.append(f"The fixture guidance asks for {doc_list} within a week. If the recorded appeal deadline has passed, a human representative should review current options.")
    if docs and any(x in low for x in ("don't have", "do not have", "can't get", "cannot get", "alternative", "substitute", "instead of", "missing")):
        parts.append(GUIDE["document_alternative_guidance"]["default"]["en"])
    if docs:
        detailed = GUIDE["document_guidance"]
        for key, value in detailed.items():
            if any(word in low for word in key.split() if len(word) > 5) and ("pathology" in key and "pathology" in doc_list or "office note" in key and "office note" in doc_list):
                parts.append(value["en"])
    if not parts:
        parts = [f"{cid} is currently {claim['status']}."]
        if docs:
            parts.append(f"The file needs {doc_list}. What would you like to know about this claim?")
    answer = " ".join(dict.fromkeys(parts))
    if model.enabled:
        rewritten = model.rephrase(text, answer)
        if rewritten:
            answer = rewritten
    return answer


def summary(session: Session) -> str:
    claim = next(c for c in CLAIMS if c["case_id"] == session.case_id)
    lines = [f"Conversation summary for {claim['case_id']}", f"We discussed your {claim['case_type']} claim and its current status: {claim['status']}."]
    conversation = " ".join(turn["text"].lower() for turn in session.turns if turn["role"] == "user")
    topics = []
    for label, hints in (("the denial reason", ("why", "denied", "denial")),
                         ("required documents", ("document", "report", "office note")),
                         ("document submission", ("upload", "submit", "send the documents")),
                         ("review timing", ("how long", "how soon", "processing time")),
                         ("payment", ("paid", "payment", "reimburse", "amount"))):
        if any(hint in conversation for hint in hints):
            topics.append(label)
    if topics:
        lines.append("Topics discussed: " + ", ".join(topics) + ".")
    if claim.get("denial_reason"):
        lines.append(f"The recorded denial reason is that {claim['denial_reason']}.")
    if claim.get("documents_needed"):
        lines.append("Next step: obtain and submit " + ", ".join(claim["documents_needed"]) + " through the member portal or claim upload link. If online upload is unavailable, contact support for fax or mail options.")
    if claim.get("appeal_deadline"):
        lines.append(f"The recorded appeal deadline was {claim['appeal_deadline']}; a human representative can discuss current options.")
    lines.append("This summary reflects the demo claim record and does not confirm a new claim decision or submission.")
    return "\n\n".join(lines)


def send_summary(session: Session) -> str:
    holder = next(h for h in HOLDERS if h["party_id"] == session.holder_id)
    recipient = holder["email"]
    body = summary(session)
    session.email_preview = body
    host = os.getenv("SMTP_HOST")
    if not host:
        session.email_result = f"Demo outbox: summary recorded for {recipient}; no external email was sent."
        return session.email_result + "\n\n" + body
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM", "claims@example.test")
    msg["To"] = recipient
    msg["Subject"] = f"Claim conversation summary — {session.case_id}"
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            user = os.getenv("SMTP_USER")
            if user:
                smtp.login(user, os.getenv("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
    except Exception:
        session.email_result = "Email delivery failed. No confirmation was recorded; please ask a representative to help."
        return session.email_result
    session.email_result = f"Summary sent to {recipient}."
    return session.email_result


def respond(session: Session, text: str, model: ModelClient) -> str:
    text = text.strip()[:2000]
    if not text:
        return "Please type a message so I can help."
    session.updated_at = datetime.now(timezone.utc).timestamp()
    session.turns.append({"role": "user", "text": text})
    answer = _respond(session, text, model)
    session.turns.append({"role": "assistant", "text": answer})
    session.turns = session.turns[-80:]
    return answer


def _respond(s: Session, text: str, model: ModelClient) -> str:
    low = norm(text)
    prefix = empathy(text)
    if s.human_transfer:
        return prefix + "I’ve marked this conversation for a human representative. In this demo, please contact the claims support team directly."
    if any(x in low for x in ("human representative", "talk to a person", "speak to a person", "real person", "human agent", "live agent")):
        s.human_transfer = True
        return prefix + "I understand. I’ve marked this for a human representative. In this demo, please contact the claims support team directly."
    if is_off_topic(text):
        s.off_topic_count += 1
        if s.off_topic_count >= 3:
            s.human_transfer = True
            return "I can only help with insurance claims here. Since this has come up several times, I’ve marked the conversation for a human representative."
        return "I can help with insurance claims and this service workflow. Please ask a claim-related question; a human representative is available if you prefer."
    s.off_topic_count = 0
    if s.phase == "VERIFY_ID" and any(exact_word(text, rep["rep_name"]) for rep in REPS):
        s.human_transfer = True
        return prefix + "I can help route a representative request, but this demo cannot establish a third party's authority to access a policyholder's claim. I’ve marked this for a human representative to verify authorization safely."
    extract_fields(text, s)
    detect_hint(text, s)
    if s.phase == "VERIFY_ID":
        if ("why" in low or "what for" in low) and any(x in low for x in ("verify", "verification", "identity", "details", "information")):
            return prefix + "Claim records can contain private health and payment information. I need three matching identity details before opening one. You may choose your full name, date of birth, phone, email, or ID last four digits; I can also route you to a human representative."
        if any(x in low for x in ("can i use", "other way to verify", "different id", "different detail")):
            return prefix + "Yes. Any three matching details from full name, date of birth, phone, email, and ID last four digits work. You can provide them across messages, and your policy number helps locate the record but does not count as one of the three."
        if any(x in low for x in ("refuse", "won't", "will not", "not giving", "don't want to", "skip verification", "just tell me")):
            s.refusal_count += 1
            if s.refusal_count >= 3:
                s.human_transfer = True
                return prefix + "I respect that. I cannot share claim details without verification, so I’ve marked this for a human representative to discuss safe options."
            return prefix + "I need to protect claim details by verifying three matching details first. You can choose from your full name, date of birth, phone, email, or the last four digits of your SSN or national ID. A human representative can help if you prefer."
        holder = verified_holder(s)
        if holder:
            s.holder_id = holder["party_id"]
            s.phase = "RESOLVE_INTENT"
            note = "Thanks, your identity is verified. "
            if s.case_hint or s.intent_hint:
                return note + resolve(s, text, model, remembered=True)
            return note + "What can I help you with regarding your claim?"
        if len(s.fields) >= 3:
            return prefix + "Those details did not match one policyholder record. I cannot open claim details yet. Please check the information, use the sample caller in this demo, or ask for a human representative."
        remaining = [x for x in ("full name", "date of birth", "phone", "email", "SSN or national ID last four digits") if {"full name":"name", "date of birth":"dob", "phone":"phone", "email":"email", "SSN or national ID last four digits":"id_last4"}[x] not in s.fields]
        count = 3 - len(s.fields)
        if s.fields:
            lead = "I’ve saved your reason for calling and will return to it after verification. " if s.case_hint or s.intent_hint else ""
            return prefix + lead + f"I have {len(s.fields)} identity detail{'s' if len(s.fields)>1 else ''}. Please provide {count} more from: " + ", ".join(remaining) + ". Your policy number helps locate a record but does not count as one of the three details."
        if s.case_hint or s.intent_hint:
            return prefix + "I’ve noted what you’re calling about, so you won’t need to repeat it. To protect the claim, please share any three matching details: full name, date of birth, phone, email, or ID last four digits."
        if len(s.turns) > 1:
            return prefix + "I can help with insurance claims. To open a personal claim, I still need three matching identity details. You may give full name, date of birth, phone, email, or ID last four digits in any order; a human representative can help if you prefer."
        return prefix + "Before I can discuss a claim, please provide any three matching details: full name, date of birth, phone, email, or SSN or national ID last four digits. You can share them across messages."
    if s.phase == "RESOLVE_INTENT":
        return prefix + resolve(s, text, model)
    if s.phase == "PROCESS_CASE":
        if any(x in low for x in ("that's all", "that is all", "all done", "i'm done", "thank you", "thanks, bye", "goodbye", "no more questions", "email summary", "send me a summary")):
            s.phase = "POST_PROCESS"
            s.email_offered = True
            if any(x in low for x in ("email summary", "send me a summary")):
                s.closed = True
                return prefix + "I can email a summary of what we discussed, the claim status, and next steps. " + send_summary(s)
            return prefix + "Before we finish, would you like an email summary of what we discussed, the claim status, and next steps? You can say “send it” or “skip”."
        claim = next(c for c in CLAIMS if c["case_id"] == s.case_id)
        return prefix + grounded_answer(claim, text, detect_intent(text, model), model) + " Is there anything else about this claim?"
    if s.phase == "POST_PROCESS":
        if s.closed:
            return "This conversation is complete. You can start a new chat for another claim."
        if low == "no" or any(x in low for x in ("skip", "no thanks", "don't send", "do not send", "no email", "no,", "not now")):
            s.closed = True
            return "Understood. I won’t send an email summary. Thank you for contacting claims support."
        if any(x in low for x in ("send", "yes", "email", "please do")):
            s.closed = True
            return send_summary(s) + " Thank you for contacting claims support."
        if any(x in low for x in ("claim", "why", "how", "what", "when", "document", "status")):
            s.phase = "PROCESS_CASE"
            claim = next(c for c in CLAIMS if c["case_id"] == s.case_id)
            return prefix + grounded_answer(claim, text, detect_intent(text, model), model) + " Anything else about the claim?"
        return "Would you like me to send the email summary, or skip it?"
    return "I can help with your insurance claim."


def resolve(s: Session, text: str, model: ModelClient, remembered: bool = False) -> str:
    source = s.intent_hint or text
    selected, candidates = choose_claim(text, s, model)
    if not selected:
        if not candidates:
            return "I could not find a claim matching that description in your record. Could you provide the claim ID or another date or claim type?"
        options = "; ".join(f"{c['case_id']} ({c['case_type']}, {c['created_at']}, {c['status']})" for c in candidates)
        return "Which claim do you mean? I can see: " + options + "."
    s.case_id = selected["case_id"]
    s.intent = detect_intent(source, model)
    s.phase = "PROCESS_CASE"
    intro = "I used the claim details you mentioned earlier. " if remembered else "I found the matching claim. "
    return intro + grounded_answer(selected, source, s.intent, model) + " What else would you like to know?"
