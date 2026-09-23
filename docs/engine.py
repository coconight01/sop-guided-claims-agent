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
    discussed_case_ids: list[str] = field(default_factory=list)
    intent: str = ""
    emotion: str = ""
    preferred_name: str = ""
    preferred_name_pending: bool = False
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
            self_intro = re.search(
                r"\b(?:my name is|i am|i'm|this is)(?:\s+the policyholder)?\s+" + re.escape(name) + r"(?!\w)",
                text, re.I,
            )
            starts_with_name = re.match(r"^\s*" + re.escape(name) + r"(?!\w)", text, re.I)
            if (self_intro or starts_with_name) and not re.search(
                r"\b(?:not|isn't|is not)\s+" + re.escape(name), text, re.I
            ):
                session.fields["name"] = name
    declared_name = re.search(r"\b(?:my name is|i am|i'm)\s+([a-z][a-z' -]{2,60})", text, re.I)
    if declared_name and "name" not in session.fields:
        candidate = re.split(r"[,.;!?]|\s+(?:and|with|calling|about|the policyholder|a policyholder)\b", declared_name.group(1), 1, flags=re.I)[0].strip()
        if len(candidate.split()) in (2, 3) and not candidate.lower().startswith(("the ", "a ")):
            session.fields["name"] = candidate
    declared_email = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.I)
    if declared_email:
        session.fields["email"] = declared_email.group(0)
    phone_match = re.search(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)", text)
    if phone_match:
        session.fields["phone"] = phone_match.group(0)
    dob = re.search(
        r"\b(?:dob|date of birth|birthdate|born on|birthday)\b[^\d]{0,20}"
        r"((?:19|20)\d\d[-/]\d\d?[-/]\d\d?)\b", text, re.I,
    )
    bare_date = re.fullmatch(r"\s*((?:19|20)\d\d[-/]\d\d?[-/]\d\d?)\s*", text)
    if dob or bare_date:
        pieces = (dob or bare_date).group(1).replace("/", "-").split("-")
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
    if any(x in low for x in ("ridiculous", "angry", "furious", "unacceptable", "frustrat", "already told", "upset", "annoyed")):
        return "I understand why this is frustrating. "
    if any(x in low for x in ("worried", "anxious", "scared", "stressed", "afraid", "overwhelmed")):
        return "I can hear that this is worrying. "
    if any(x in low for x in ("confused", "don't understand", "unclear")):
        return "I can help make this clearer. "
    return ""


def clearly_off_topic(text: str) -> bool:
    low = norm(text)
    return any(x in low for x in (
        "reinforcement learning", "what is rl", "weather", "recipe", "bitcoin",
        "stock market", "tell me a joke", "capital of", "write code", "football score",
    ))


def is_off_topic(text: str) -> bool:
    low = norm(text)
    if clearly_off_topic(text):
        return True
    if low in ("hello", "hi", "how are you", "thanks", "thank you", "why?", "how?", "what next?"):
        return False
    scope = r"\bcl[- ]?\d{4}\b|\b(?:claim|insurance|policy|denial|denied|appeal|document|report|payment|paid|verification|verify|identity|status|coverage|covered|deductible|reimbursement|submit|submitted|summited|upload|adjuster|representative|pathology|portal|case|email|summary|next|missing|that|this|it|them|those)\b|office note|net pay|net fee|you need|need from me|help me"
    if re.match(r"^(?:what is|what are|what's|who is|who's|who won|tell me about|explain|how do i|can you (?:explain|tell me|write|solve|translate))\b", low) and not re.search(scope, low):
        return True
    return False


def third_party_declaration(text: str) -> bool:
    low = norm(text)
    return bool(
        re.search(r"\b(?:calling|speaking)\s+on behalf of\b", low)
        or re.search(r"\b(?:calling|speaking)\s+for\s+my\s+(?:mother|father|wife|husband|son|daughter)\b", low)
        or re.search(r"\bmy\s+(?:mother|father|wife|husband|son|daughter|client|patient)(?:'s)?\s+(?:claim|policy|dob|date of birth|ssn)\b", low)
        or re.search(r"\b(?:his|her|their)\s+(?:claim|dob|date of birth|ssn|policy)\b", low)
        or re.search(r"\b(?:i am|i'm|this is)\s+[^.!?]{0,50}?'s\s+(?:son|daughter|spouse|wife|husband|caregiver|representative)\b", low)
        or re.search(r"\b(?:i am|i'm|this is)\s+(?:his|her|their)\s+(?:son|daughter|spouse|wife|husband|caregiver|representative)\b", low)
        or re.search(r"\b(?:i am|i'm)\s+(?:a|the)\s+(?:son|daughter|spouse|wife|husband|caregiver|representative)\s+(?:for|of)\b", low)
        or "power of attorney" in low
    )


def different_identity(text: str, holder: dict) -> bool:
    low = norm(text)
    if any(x in low for x in ("call me", "call my name", "use my name")):
        return False
    match = re.search(r"\bmy name is\s+([a-z][a-z'-]*(?:\s+[a-z][a-z'-]*)?)", text, re.I)
    if not match:
        match = re.search(r"\b(?i:i am|i'm|this is)\s+([A-Z][a-z'-]+\s+[A-Z][a-z'-]+)\b", text)
    if not match:
        return False
    candidate = norm(match.group(1))
    if candidate.startswith(("the ", "a ")):
        return False
    allowed = [norm(x) for x in [holder["name"], *holder.get("name_aliases", [])]]
    return candidate not in allowed and candidate not in {x.split()[0] for x in allowed}


def representative_self_intro(text: str) -> bool:
    return any(
        re.search(r"\b(?:my name is|i am|i'm|this is)\s+" + re.escape(rep["rep_name"]) + r"(?!\w)", text, re.I)
        for rep in REPS
    )


def detect_hint(text: str, session: Session) -> None:
    low = norm(text)
    if any(x in low for x in ("claim", "denied", "appeal", "reimbursement", "covered", "documents", "status")):
        session.intent_hint = text[:500]
    if not session.case_hint:
        # Preserve natural date/type hints without disclosing or resolving a claim yet.
        if any(x in low for x in ("january", "february", "march", "healthcare", "dental", "auto")):
            session.case_hint = text[:500]


def model_safe_text(text: str, extra_names: tuple[str, ...] = ()) -> str:
    """Remove identity fields before sending a caller utterance to an external model."""
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]", text, flags=re.I)
    text = re.sub(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)", "[phone]", text)
    text = re.sub(r"\bPOL[-\s]?\d{4}\b", "[policy number]", text, flags=re.I)
    text = re.sub(r"\b(?:ssn|social security|national id|id|last four|last 4|last4)\b.{0,30}?\b\d{4}\b", "[ID detail]", text, flags=re.I)
    for holder in HOLDERS:
        for name in [holder["name"], *holder.get("name_aliases", [])]:
            text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "[name]", text, flags=re.I)
        text = text.replace(holder["dob"], "[date of birth]")
        text = text.replace(holder["id_last4"], "[ID digits]")
    for name in extra_names:
        if name:
            text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "[name]", text, flags=re.I)
    text = re.sub(r"\b(?:19|20)\d\d[-/]\d\d?[-/]\d\d?\b", "[date]", text)
    return text


def choose_claim(text: str, session: Session, model: ModelClient) -> tuple[dict | None, list[dict]]:
    own = [c for c in CLAIMS if c["party_id"] == session.holder_id]
    latest = norm(text)
    strong_new_hint = (
        re.search(r"\b(?:healthcare|medical|dental|auto|denied|denial|january|february|march|november)\b", latest)
        or re.search(r"\b20\d{2}\b", latest)
    )
    full = latest if strong_new_hint else norm(" ".join((session.case_hint, session.intent_hint, text)))
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
        choice = model.select_claim(model_safe_text(full), [safe_claim(c) for c in filtered])
        if choice:
            match = next((c for c in filtered if c["case_id"] == choice), None)
            if match:
                return match, filtered
    return None, filtered


def local_topics(text: str) -> list[str]:
    """Conservative fallback when the model is unavailable or returns invalid JSON."""
    low = norm(text)
    if re.search(r"\b(?:submitted|summited|sent|uploaded|gave)\b", low) and any(
        word in low for word in ("all", "already", "everything", "have")
    ):
        return ["submission_dispute"]
    topics = []
    if any(x in low for x in ("format", "pdf", "scan", "legible", "readable", "file type", "what should", "what needs to")) and any(
        x in low for x in ("document", "report", "note", "file", "upload", "pathology")
    ):
        topics.append("document_detail")
    if any(x in low for x in ("why", "denied", "denial", "reason")):
        topics.append("denial_reason")
    if any(x in low for x in ("document", "paperwork", "report", "office note", "what do i need")):
        topics.append("documents")
    if any(x in low for x in ("upload", "portal", "fax", "mail", "where do i send", "where should i send", "how do i submit")):
        topics.append("submission_method")
    if any(x in low for x in ("got it", "received", "receipt", "confirmation", "attached")):
        topics.append("receipt_check")
    if any(x in low for x in ("how soon do i need to", "when should i submit", "when do i need to submit")):
        topics.append("submission_timing")
    elif any(x in low for x in ("how long", "processing time", "review time", "after i submit", "once i submit")):
        topics.append("review_timing")
    if any(x in low for x in ("appeal", "deadline")):
        topics.append("appeal")
    if any(x in low for x in ("paid", "payment", "reimburse", "amount", "dollar", "money")):
        topics.append("payment")
    if any(x in low for x in ("don't have", "do not have", "can't get", "cannot get", "alternative", "substitute")):
        topics.append("alternatives")
    if any(x in low for x in ("status", "progress", "outcome", "update")):
        topics.append("status")
    return topics[:3] or ["clarify"]


def matching_guidance(claim: dict, text: str, category: str) -> list[str]:
    guidance = GUIDE[category]
    requested = [
        doc for doc in claim.get("documents_needed", [])
        if any(part in norm(text) for part in doc.split() if len(part) > 4)
    ]
    docs = requested or claim.get("documents_needed", [])
    selected = []
    for doc in docs:
        key = next((name for name in guidance if doc in name or name in doc), "")
        if key:
            selected.append(guidance[key]["en"])
    return selected


def topic_answer(claim: dict, topics: list[str], text: str = "") -> str:
    cid = claim["case_id"]
    docs = claim.get("documents_needed", [])
    doc_list = ", ".join(docs)
    reason = claim.get("denial_reason")
    if "submission_dispute" in topics:
        if reason:
            return (f"{cid} was denied because {reason} at the time of review. "
                    "I can't confirm from this record whether documents you sent later were received. "
                    "If you have a submission confirmation or approximate date, a representative can check "
                    "whether the files were attached and review the next step with you.")
        return ("I hear that you've already sent the materials. This record does not show receipt details, "
                "so a representative can check the intake record with you.")
    parts = []
    for topic in topics:
        if topic == "denial_reason" and reason:
            parts.append(f"{cid} was denied because {reason}.")
        elif topic == "status":
            parts.append(f"{cid} is currently {claim['status']}.")
        elif topic == "documents" and docs:
            parts.append(f"The record lists {doc_list} as the documents requested for review.")
        elif topic == "submission_method" and docs:
            parts.append(GUIDE["default_guidance"]["en"])
        elif topic == "receipt_check":
            parts.append("This record does not confirm whether a later upload was received. "
                         "A representative can check whether the files were attached to the claim.")
        elif topic == "submission_timing" and docs:
            parts.append(f"The guidance asks for {doc_list} within a week. "
                         "If the recorded appeal deadline has passed, a representative should review current options.")
        elif topic == "review_timing" and docs:
            average = GUIDE["claim_followup_settings"]["average_processing_time_after_submission"]["en"]
            parts.append(f"After the requested files are received, review usually takes {average}; "
                         "intake or another review cycle may take longer.")
        elif topic == "appeal" and claim.get("appeal_deadline"):
            parts.append(f"The recorded appeal deadline was {claim['appeal_deadline']}. "
                         "A human representative can discuss your current options.")
        elif topic == "payment":
            parts.append(f"The recorded net payment is ${claim['net_pay']} and the expected reimbursement is "
                         f"${claim['expected_reimbursement_amount']}.")
        elif topic == "document_detail" and docs:
            parts.append(GUIDE["default_guidance"]["en"])
            parts.extend(matching_guidance(claim, text, "document_guidance"))
        elif topic == "alternatives" and docs:
            parts.append(GUIDE["document_alternative_guidance"]["default"]["en"])
            parts.extend(matching_guidance(claim, text, "document_alternative_guidance"))
    if not parts:
        return ("I don't see enough in this claim record to answer that reliably. "
                "I can help with its status, denial reason, documents, submission, timing, or payment, "
                "or connect you with a representative.")
    return " ".join(dict.fromkeys(parts))


def case_response(session: Session, claim: dict, text: str, model: ModelClient) -> tuple[str, bool]:
    previous = session.turns[-2]["text"] if len(session.turns) > 1 else ""
    route = model.analyze_case(model_safe_text(text, (session.preferred_name,)),
                               model_safe_text(previous, (session.preferred_name,))) if model.enabled else {}
    fallback = local_topics(text)
    # A clear statement that files were already sent takes priority over a generic model label.
    if fallback == ["submission_dispute"]:
        topics = fallback
        unrelated = False
    else:
        topics = route.get("topics") or fallback
        unrelated = route.get("scope") == "unrelated"
    if unrelated:
        return "", True
    if "document_detail" in topics and "documents" in topics:
        topics = [topic for topic in topics if topic != "documents"]
    emotion = route.get("emotion", "neutral")
    session.emotion = emotion
    session.intent = topics[0]
    if emotion == "frustrated" or text.count("!") >= 3:
        opening = "I hear how frustrating this is. "
    elif emotion == "anxious":
        opening = "I know this is worrying. "
    elif emotion == "confused":
        opening = "Let me make this clearer. "
    else:
        opening = empathy(text)
    if session.preferred_name_pending:
        opening = f"{session.preferred_name}, " + opening
        session.preferred_name_pending = False
    return opening + topic_answer(claim, topics, text), False


def requested_name(text: str) -> str:
    low = norm(text)
    if not any(x in low for x in ("call me", "call my name", "use my name", "my name is")):
        return ""
    match = re.search(r"\b(?:call me|my name is|i am|i'm)\s+([a-z][a-z'-]{1,29})\b", text, re.I)
    if not match or match.group(1).lower() in {"the", "a", "an", "your", "policyholder"}:
        return ""
    return match.group(1).title()


def off_topic_reply(session: Session) -> str:
    session.off_topic_count += 1
    if session.off_topic_count >= 3:
        session.human_transfer = True
        return ("I can only help with insurance claims here. Since this has come up several times, "
                "I've marked the conversation for a human representative.")
    return ("I can help with insurance claims and related account questions. "
            "For something else, a human representative is available if you prefer.")


def summary(session: Session) -> str:
    case_ids = list(dict.fromkeys([*session.discussed_case_ids, session.case_id]))
    claims = [next(c for c in CLAIMS if c["case_id"] == cid) for cid in case_ids if cid]
    title = claims[0]["case_id"] if len(claims) == 1 else "multiple claims"
    lines = [f"Conversation summary for {title}"]
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
    for claim in claims:
        lines.append(f"{claim['case_id']}: We discussed your {claim['case_type']} claim and its current status: {claim['status']}.")
        if claim.get("denial_reason"):
            lines.append(f"The recorded denial reason for {claim['case_id']} is that {claim['denial_reason']}.")
        if claim.get("documents_needed"):
            lines.append(f"Next step for {claim['case_id']}: obtain and submit " + ", ".join(claim["documents_needed"]) + " through the member portal or claim upload link. If online upload is unavailable, contact support for fax or mail options.")
        if claim.get("appeal_deadline"):
            lines.append(f"The recorded appeal deadline for {claim['case_id']} was {claim['appeal_deadline']}; a human representative can discuss current options.")
    lines.append("This summary reflects the demo claim record and does not confirm a new claim decision or submission.")
    return "\n\n".join(lines)


def requested_other_email(session: Session, text: str) -> bool:
    holder = next(h for h in HOLDERS if h["party_id"] == session.holder_id)
    addresses = re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.I)
    redirect = re.search(
        r"\b(?:to|at)\s+(?:my\s+)?(?:other|work|new|different|another)\s+(?:email|address)\b"
        r"|\bto\s+my\s+(?:wife|husband|son|daughter|representative)\b",
        text, re.I,
    )
    return bool(redirect or any(address.lower() != holder["email"].lower() for address in addresses))


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
    msg["Subject"] = "Claim conversation summary"
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
    holder = next((h for h in HOLDERS if h["party_id"] == s.holder_id), None)
    identity_denial = bool(holder and (
        re.search(r"\b(?:i am|i'm)\s+(?:not the (?:policyholder|claimant)|a different person)\b", low)
        or re.search(r"\b(?:i am|i'm)\s+not\s+" + re.escape(holder["name"].lower()) + r"\b", low)
    ))
    if holder and (third_party_declaration(text) or identity_denial or different_identity(text, holder)):
        s.human_transfer = True
        s.holder_id = ""
        s.case_id = ""
        s.discussed_case_ids.clear()
        s.fields.clear()
        s.email_preview = ""
        s.turns.clear()
        return ("Thanks for clarifying. I can't continue discussing the verified policyholder's claim "
                "with a different caller. A representative can check your authorization safely.")
    if s.holder_id:
        name = requested_name(text)
        if name:
            s.preferred_name = name
            s.preferred_name_pending = True
            s.off_topic_count = 0
            return f"Of course, I’ll call you {name}. What would you like to know about the claim?"
    if is_off_topic(text):
        return off_topic_reply(s)
    if s.phase == "VERIFY_ID" and (third_party_declaration(text) or representative_self_intro(text)):
        s.human_transfer = True
        return prefix + "I can’t establish a third party’s authority to access the policyholder’s claim in this demo. I’ve marked this for a human representative to check authorization safely."
    if s.phase == "VERIFY_ID":
        extract_fields(text, s)
        detect_hint(text, s)
        s.off_topic_count = 0
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
        detect_hint(text, s)
        return prefix + resolve(s, text, model)
    if s.phase == "PROCESS_CASE":
        if any(x in low for x in ("that's all", "that is all", "all done", "i'm done", "thank you", "thanks, bye", "goodbye", "no more questions", "email summary", "send me a summary")):
            s.phase = "POST_PROCESS"
            s.email_offered = True
            if any(x in low for x in ("email summary", "send me a summary")):
                if requested_other_email(s, text):
                    return "For privacy, I can only send the summary to the email on the verified policyholder's record. Would you like me to send it there or skip?"
                s.closed = True
                return prefix + "I can email a summary of what we discussed, the claim status, and next steps. " + send_summary(s)
            return prefix + "Before we finish, would you like an email summary of what we discussed, the claim status, and next steps? You can say “send it” or “skip”."
        other_ids = re.findall(r"\bCL[-\s]?(\d{4})\b", text, re.I)
        requested_id = "CL-" + other_ids[-1] if other_ids else ""
        type_request = re.search(r"\b(?:my|another|other|different)\s+(healthcare|medical|dental|auto)\s+claim\b", low)
        if requested_id and requested_id != s.case_id:
            target = next((c for c in CLAIMS if c["case_id"] == requested_id and c["party_id"] == s.holder_id), None)
            if not target:
                return "I can't access that claim under the verified policyholder's record. A representative can check authorization or help locate the right claim."
            s.case_id = requested_id
            if requested_id not in s.discussed_case_ids:
                s.discussed_case_ids.append(requested_id)
            answer, unrelated = case_response(s, target, text, model)
            return "I found the claim you asked about. " + answer
        if type_request:
            kind = "healthcare" if type_request.group(1) == "medical" else type_request.group(1)
            current = next(c for c in CLAIMS if c["case_id"] == s.case_id)
            if kind != current["case_type"]:
                options = [c for c in CLAIMS if c["party_id"] == s.holder_id and c["case_type"] == kind]
                if len(options) == 1:
                    target = options[0]
                    s.case_id = target["case_id"]
                    if s.case_id not in s.discussed_case_ids:
                        s.discussed_case_ids.append(s.case_id)
                    if local_topics(text) == ["clarify"]:
                        return f"I found {target['case_id']}, your {kind} claim. It is currently {target['status']}. What would you like to know about it?"
                    answer, unrelated = case_response(s, target, text, model)
                    return "I found the claim you asked about. " + answer
                if not options:
                    return "I couldn't find that claim type under the verified policyholder's record. Do you have a claim ID or another date?"
                s.phase = "RESOLVE_INTENT"
                return "Which claim do you mean? I can see: " + "; ".join(f"{c['case_id']} ({c['created_at']})" for c in options) + "."
        claim = next(c for c in CLAIMS if c["case_id"] == s.case_id)
        answer, unrelated = case_response(s, claim, text, model)
        if unrelated:
            return off_topic_reply(s)
        s.off_topic_count = 0
        return answer
    if s.phase == "POST_PROCESS":
        if s.closed:
            return "This conversation is complete. You can start a new chat for another claim."
        if low in {"no", "skip", "no thanks", "not now", "no email"} or any(
            x in low for x in ("don't send", "do not send", "skip email")
        ):
            s.closed = True
            return "Understood. I won’t send an email summary. Thank you for contacting claims support."
        if requested_other_email(s, text):
            return "For privacy, I can only send the summary to the email on the verified policyholder's record. Would you like me to send it there or skip?"
        if low in {"yes", "yes please", "send it", "please do", "send email", "send the email"} or any(
            x in low for x in ("send the summary", "send me the summary", "email me the summary")
        ):
            s.closed = True
            return send_summary(s) + " Thank you for contacting claims support."
        if any(x in low for x in ("claim", "why", "how", "what", "when", "document", "status", "submitted", "summited")):
            s.phase = "PROCESS_CASE"
            claim = next(c for c in CLAIMS if c["case_id"] == s.case_id)
            answer, unrelated = case_response(s, claim, text, model)
            if unrelated:
                return off_topic_reply(s)
            s.off_topic_count = 0
            return answer
        return "Would you like me to send the email summary, or skip it?"
    return "I can help with your insurance claim."


def resolve(s: Session, text: str, model: ModelClient, remembered: bool = False) -> str:
    source = (s.intent_hint or text) if remembered else text
    selected, candidates = choose_claim(text, s, model)
    if not selected:
        if not candidates:
            return "I could not find a claim matching that description in your record. Could you provide the claim ID or another date or claim type?"
        options = "; ".join(f"{c['case_id']} ({c['case_type']}, {c['created_at']}, {c['status']})" for c in candidates)
        return "Which claim do you mean? I can see: " + options + "."
    s.case_id = selected["case_id"]
    if s.case_id not in s.discussed_case_ids:
        s.discussed_case_ids.append(s.case_id)
    s.phase = "PROCESS_CASE"
    if local_topics(source) == ["clarify"]:
        answer = f"{selected['case_id']} is currently {selected['status']}."
    else:
        answer, _ = case_response(s, selected, source, model)
    intro = (f"I used the details you mentioned earlier to find {selected['case_id']}. " if remembered
             else f"I found {selected['case_id']}. ")
    return intro + answer + " What else would you like to know?"
