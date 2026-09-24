"""Deterministic SOP gates and fixture-backed insurance claim workflow."""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
CONSENT = fixture("consent_scenarios.json")
GUIDE_WORDS = set(re.findall(r"[a-z]+", json.dumps(GUIDE).lower()))
PII = ("name", "dob", "phone", "email", "id_last4")
PHASES = ("VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS")
FIELD_LABELS = {"name": "full name", "dob": "date of birth", "phone": "phone number",
                "email": "email", "id_last4": "SSN or national ID last four"}
MAX_VERIFY_FAILURES = 3
# The SOP decides which actions each phase may take; code checks every action against this table.
PHASE_ACTIONS = {
    "VERIFY_ID": {"collect_identity", "remember_for_later", "explain_verification", "request_consent", "handoff"},
    "RESOLVE_INTENT": {"list_own_claims", "select_own_claim", "handoff"},
    "PROCESS_CASE": {"read_own_claim", "read_guidance", "select_own_claim", "list_own_claims", "handoff"},
    "POST_PROCESS": {"read_own_claim", "send_summary", "skip_summary", "handoff"},
}
ACTION_LABELS = {
    "collect_identity": "check identity details", "remember_for_later": "remember your request for later",
    "explain_verification": "explain why verification is needed", "request_consent": "request policyholder consent",
    "handoff": "hand off to a person", "list_own_claims": "list your claims", "select_own_claim": "open one of your claims",
    "read_own_claim": "answer from your claim record", "read_guidance": "share document guidance",
    "send_summary": "email a summary (with your consent)", "skip_summary": "skip the email",
}
CLAIM_ACTIONS = {"list_own_claims", "select_own_claim", "read_own_claim", "read_guidance", "send_summary"}


def allow(session: "Session", action: str, claim: dict | None = None) -> None:
    """Raise if the SOP phase, verification state, or claim ownership does not permit this action."""
    if action not in PHASE_ACTIONS[session.phase]:
        raise PermissionError(f"{action} is not allowed in {session.phase}")
    if action in CLAIM_ACTIONS and not session.holder_id:
        raise PermissionError(f"{action} requires verification")
    if claim is not None and claim["party_id"] != session.holder_id:
        raise PermissionError("claim belongs to another policyholder")
MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december")
MONTH_RE = r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")
DOB_CUE = r"\b(?:dob|d\.o\.b\.?|date of birth|birth ?date|born(?: on| in)?|birthday(?: is)?)\b"
REFERENCE_RE = re.compile(r"\b(?:CL|POL)[-\s]?\d{4}\b", re.I)
NAME_STOP = set(
    "and with calling about from here the a an policyholder really very so just not also still sorry trying "
    "looking having worried frustrated angry upset confused anxious scared fine good okay ok done back ready sure "
    "afraid tired waiting asking wondering in on at for to of my your is was dob date birth phone email ssn social "
    "security policy claim please thanks thank but because i im i'm do you what calling hello hi hey there dear yes "
    "no skip help sure bye".split()
)
TYPE_CLUES = (("auto", r"auto|car|vehicle|accident|collision|crash"),
              ("dental", r"dental|dentist|teeth|tooth"),
              ("healthcare", r"health ?care|health|medical|hospital|doctor|surgery|biopsy|clinic"),
              ("unsupported", r"life insurance|life claim|homeowners?|home insurance|property|renters?|travel insurance|pet insurance|vision|disability"))
STATUS_CLUES = (("denied", r"denied|denial|deny|rejected|declined|turned down|said no|not approved|refused|won'?t pay|didn'?t pay"),
                ("open", r"still open|open one|open claim|in progress|pending|ongoing|being processed"),
                ("closed", r"closed|settled|completed|finished"))
CLAIM_WORDS = (r"\bcl[- ]?\d{4}\b|\b(?:claims?|insurance|insured|policy|denial|denied|appeal|documents?|report|payment|paid|"
               r"verification|verify|identity|status|coverage|covered|deductible|reimburse\w*|submit\w*|summited|upload\w*|"
               r"adjuster|representative|pathology|portal|case|email|summary|missing|office note|net pay|net fee|"
               r"dob|ssn|birth|phone|name|last four|last 4|social|pii|national id|car|auto|vehicle|accident|dental|dentist|medical|hospital|doctor|"
               r"deadline|refund|money|bill|office|contact)\b")
WEAK_REFERENCES = r"\b(?:that|this|it|them|those|next|you need|need from me|help me|what now|why)\b"
OFF_TOPIC_RE = re.compile(
    r"reinforcement learning|\bwhat(?:'s| is) rl\b|^rl\??$|machine learning|\bweather\b|\brecipes?\b|\bbake\b|\bcook(?:ing)?\b|"
    r"bitcoin|crypto|stock market|\bstocks?\b|\bjokes?\b|\bpoems?\b|\bsongs?\b|\blyrics\b|\bstory\b|capital of|"
    r"write (?:me )?(?:some |a |an )?(?:code|program|script|essay)|\bfootball\b|\bbasketball\b|\bsoccer\b|\bnba\b|"
    r"\bnfl\b|\belection\b|\bpresident\b|\bmovies?\b|\bhomework\b|\bpython\b|\bjavascript\b|meaning of life|"
    r"\btranslate\b|\bmath\b|\bsolve\b|\bvacation\b|\brestaurant\b|\b\d{1,3}\s*[+*x]\s*\d{1,3}\b|\b\d{1,3}\s+[-/]\s+\d{1,3}\b", re.I)
HUMAN_RE = re.compile(
    r"\b(?:human (?:representative|agent|being)|real person|live (?:agent|person)|"
    r"(?:talk|speak|chat)(?: to| with) (?:a |an |some|your )?(?:one|someone|person|agent|representative|rep|human|supervisor|manager)|"
    r"transfer me|connect me (?:to|with) (?:a |an )?(?:person|agent|representative|human)|"
    r"(?:get|give|put) me (?:to |through to )?(?:your |a |the )?(?:manager|supervisor|human|person|someone|representative|agent)|"
    r"(?:want|need) (?:your |a |the )?(?:manager|supervisor))\b"
    r"|^(?:a )?(?:human|representative|agent|rep|person)(?: please)?[.!]?$")
INJECTION_RE = re.compile(r"\bsystem\s*:|identity_verified|verified\s*=\s*true|phase\s*=|ignore (?:all |the |your )?"
                          r"(?:previous|prior|above) instructions|developer mode|you are now|admin mode", re.I)
STAFF_RE = re.compile(r"\boverride(?: code)?\b|\binternal\b|\bi'?m (?:staff|an employee|from northstar|an adjuster|a manager)\b|"
                      r"\badjuster\b|verification doesn'?t apply|\bemployee id\b|\bstaff\b", re.I)
ALREADY_VERIFIED_RE = re.compile(r"already verified|verified (?:me )?(?:yesterday|before|earlier|last time)|check your notes|"
                                 r"your colleague|already did (?:this|that)|verified with", re.I)
DECEASED_RE = re.compile(r"passed away|\bdied\b|\bdeceased\b|\bdeath of\b|\bwidow\w*|\bfuneral\b|lost my (?:wife|husband|"
                         r"mother|mom|father|dad|son|daughter|partner)", re.I)
REFUSAL_RE = re.compile(
    r"\b(?:i refuse|refuse to|won'?t (?:give|share|tell|provide|verify)|will not (?:give|share|tell|provide|verify)|"
    r"not (?:giving|going to give|sharing|telling|providing)|don'?t want to (?:give|share|verify|tell|provide)|"
    r"rather not|skip (?:the )?verification|none of your business|why should i|just tell me|already told you|"
    r"forget it|not doing this|skip (?:it|this)|^skip)\b")
OPENINGS = {
    "frustrated": ("I understand why this is frustrating.", "I hear you, and I'm sorry this has been such a hassle.",
                   "I know this is taking longer than you'd like."),
    "anxious": ("I can hear that this is worrying, and I'll help you through it.",
                "I understand this is stressful. Let's take it one step at a time."),
    "confused": ("Let me make this clearer.", "Good question. Here's how it works."),
}
TOPIC_LABELS = {
    "denial_reason": "why the claim was denied", "status": "the claim status", "documents": "the documents requested",
    "submission_method": "how to submit documents", "submission_dispute": "documents you said were already sent",
    "submission_timing": "when to submit documents", "review_timing": "review timing",
    "appeal": "the appeal deadline", "payment": "payment amounts", "alternatives": "alternatives for hard-to-get documents",
    "receipt_check": "whether uploads were received", "document_detail": "document requirements",
    "next_steps": "next steps", "outcome": "what to expect from the review", "contact": "how to reach support",
    "how_to_get_documents": "how to get the missing documents", "other_documents": "other records you have",
    "account_change": "updating contact details", "status_meaning": "what the claim status means",
}


def today() -> date:
    return date.today()


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
    candidate_ids: list[str] = field(default_factory=list)
    pending_question: str = ""
    topics_discussed: list[str] = field(default_factory=list)
    intent: str = ""
    emotion: str = ""
    emotion_streak: int = 0
    preferred_name: str = ""
    preferred_name_pending: bool = False
    refusal_count: int = 0
    refused_fields: list[str] = field(default_factory=list)
    name_parts: dict[str, str] = field(default_factory=dict)
    last_topics: list[str] = field(default_factory=list)
    email_requested: bool = False
    stalled: int = 0
    human_offered: bool = False
    # Authorized representative flow: listed rep + policyholder's 3 PII + policyholder consent.
    rep_name: str = ""
    rep_party: str = ""
    awaiting_rep_name: bool = False
    consent_status: str = ""
    consent_polls: int = 0
    consent_scenario: str = field(default_factory=lambda: os.getenv("CONSENT_SCENARIO", "default"))
    verify_failures: int = 0
    failed_snapshot: str = ""
    hint_acknowledged: bool = False
    policy_note_given: bool = False
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
            "memory_tags": memory_tags(self),
            "representative": self.rep_name if verified or self.consent_status else "",
            "allowed_actions": [ACTION_LABELS[a] for a in sorted(PHASE_ACTIONS[self.phase])
                                if a != "request_consent" or self.rep_name],
            "candidates": [f"{c['case_id']} · {c['case_type']} · {c['status']}" for c in CLAIMS
                           if verified and self.phase == "RESOLVE_INTENT" and c["case_id"] in self.candidate_ids],
            "consent_status": self.consent_status,
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


def join_words(items: list[str], last: str = "and") -> str:
    items = list(items)
    if len(items) < 3:
        return f" {last} ".join(items)
    return ", ".join(items[:-1]) + f", {last} " + items[-1]


def fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


def doc_phrase(claim: dict) -> str:
    return join_words([f"the {doc}" for doc in claim.get("documents_needed", [])])


def deadline_passed(claim: dict) -> bool:
    return bool(claim.get("appeal_deadline")) and date.fromisoformat(claim["appeal_deadline"]) < today()


# ---------------------------------------------------------------- identity capture

def parse_date(s: str) -> str:
    patterns = (
        (r"\b((?:19|20)\d\d)[-/.](\d{1,2})[-/.](\d{1,2})\b", "ymd"),
        (r"\b(\d{1,2})[-/.](\d{1,2})[-/.]((?:19|20)?\d\d)\b", "mdy"),
        (MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+'?((?:19|20)?\d\d)\b", "Mdy"),
        (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + MONTH_RE + r"\.?,?\s+'?((?:19|20)?\d\d)\b", "dMy"),
    )
    for pattern, order in patterns:
        m = re.search(pattern, s, re.I)
        if not m:
            continue
        a, b, c = m.groups()
        try:
            if order == "ymd":
                y, mo, d = int(a), int(b), int(c)
            elif order == "mdy":
                mo, d, y = int(a), int(b), int(c)
                if mo > 12:
                    mo, d = d, mo
            elif order == "Mdy":
                mo, d, y = MONTH_NAMES.index(next(n for n in MONTH_NAMES if n.startswith(a[:3].lower()))) + 1, int(b), int(c)
            else:
                d, mo, y = int(a), MONTH_NAMES.index(next(n for n in MONTH_NAMES if n.startswith(b[:3].lower()))) + 1, int(c)
            if y < 100:
                y += 1900 if y > today().year % 100 else 2000
            return date(y, mo, d).isoformat()
        except (ValueError, StopIteration):
            continue
    return ""


def declared_name(text: str) -> str:
    for holder in HOLDERS:
        for name in [holder["name"], *holder.get("name_aliases", [])]:
            n = re.escape(name) + r"(?!\w)(?!'s)"
            m = re.search(r"(?<!\w)" + n, text, re.I)
            if not m or re.search(r"\b(?:not|isn't|is not)\s+" + re.escape(name), text, re.I):
                continue
            before = text[max(0, m.start() - 30):m.start()].lower()
            if not re.search(r"\b(?:my|her|his|their)\s+\w+\s*,?\s*$|\b(?:for|about|of|with|to|from|called|spoke|and|named)\s*$",
                             before):
                return name
    patterns = (
        re.compile(r"\b(?:my (?:full )?name is|name's)\s+([A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){1,3})", re.I),
        re.compile(r"(?:^|[.,;!]\s*)(?:full )?name\s*[:\-]?\s+([A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){1,3})", re.I),
        re.compile(r"\b(?i:i am|i'm|this is|it's)\s+([A-Z][a-zA-Z'.-]+(?:\s+[A-Z][a-zA-Z'.-]+){1,2})\b"),
    )
    for pattern in patterns:
        m = pattern.search(text)
        if not m:
            continue
        words = []
        for word in m.group(1).split():
            if word.lower().strip(".,'") in NAME_STOP:
                break
            words.append(word.strip(".,"))
        if 2 <= len(words) <= 3 and not words[-1].lower().endswith("'s"):
            return " ".join(w[:1].upper() + w[1:] for w in words)
    return ""


def extract_fields(text: str, session: Session) -> list[str]:
    """Record explicit caller statements; matching is checked separately. Returns handling notes."""
    notes = []
    scrubbed = REFERENCE_RE.sub(" ", text)
    low = norm(scrubbed)
    name = declared_name(text)
    if name:
        session.fields["name"] = name
    for part, pattern in (("first", r"\b(?:first|given) name(?: is|:)?\s+([a-z][a-z'-]+)"),
                          ("last", r"\b(?:last|family|sur) ?name(?: is|:)?\s+([a-z][a-z'-]+)")):
        m = re.search(pattern, low)
        if m and m.group(1) not in NAME_STOP:
            session.name_parts[part] = m.group(1).title()
    if not name and session.name_parts:
        if len(session.name_parts) == 2:
            session.fields["name"] = f"{session.name_parts['first']} {session.name_parts['last']}"
        else:
            notes.append("name_part")
    email = EMAIL_RE.search(text)
    if email and not INJECTION_RE.search(text) and not re.search(
            r"\b(?:to|send|cc|forward|at|update|change)\b[^.@]{0,15}$", text[:email.start()], re.I):
        session.fields["email"] = email.group(0)
    phone = PHONE_RE.search(scrubbed)
    if phone:
        session.fields["phone"] = phone.group(0)
    partial_phone = re.search(r"\b(?:phone|cell|mobile|number)\b[^.,;]{0,25}?\b(?:ends? in|ending in|ending with|last (?:four|4)"
                              r"(?: digits)?(?: (?:is|are))?)\s*:?\s*\d{3,4}\b", low)
    if partial_phone and not phone:
        notes.append("partial_phone")
    if partial_phone:
        low = low.replace(partial_phone.group(0), " ")
    cue = re.search(DOB_CUE, scrubbed, re.I)
    dob = parse_date(scrubbed[cue.end():cue.end() + 40]) if cue else ""
    if not dob and len(scrubbed.strip()) <= 32 and not re.search(r"claim|filed|since|from", low):
        dob = parse_date(scrubbed)
    if not dob and "dob" not in session.fields:
        for m in re.finditer(r"\b(?:(?:19|20)\d\d[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.](?:19|20)?\d\d)\b", scrubbed):
            if not re.search(r"filed|claim|since|from|dated|\bon\s*$", scrubbed[max(0, m.start() - 20):m.start()], re.I):
                candidate = parse_date(m.group())
                if candidate and 1900 <= int(candidate[:4]) <= today().year - 16:
                    dob = candidate
                    break
    if dob and 1900 <= int(dob[:4]) <= today().year - 16:
        session.fields["dob"] = dob
    full_ssn = re.search(r"(?<!\d)\d{3}-\d{2}-(\d{4})(?!\d)", scrubbed)
    ssn = re.search(
        r"\b(?:ssn|social(?: security)?(?: number)?|national id|id number|id)\b.{0,30}?(?<!\d)(\d{4})(?![\d/.-]\d)"
        r"|\b(?:last four|last 4|last4|ending in|ends in)\b.{0,30}?(?<!\d)(\d{4})(?!\d)", low)
    bare = re.fullmatch(r"(?:it'?s|its|it is|that'?s|ssn|last four|last 4)?\s*(?:is)?\s*:?\s*(\d{4})\s*[.!]?", low)
    if full_ssn:
        session.fields["id_last4"] = full_ssn.group(1)
        notes.append("full_ssn")
    elif ssn or bare:
        session.fields["id_last4"] = (ssn.group(1) or ssn.group(2)) if ssn else bare.group(1)
    elif "id_last4" not in session.fields:
        rest = PHONE_RE.sub(" ", re.sub(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", " ", low))
        loose = [d for d in re.findall(r"(?<![\d/.:$,-])(\d{4})(?![\d/.:,-])", rest) if not re.fullmatch(r"(?:19|20)\d\d", d)]
        if len(loose) == 1:
            session.fields["id_last4"] = loose[0]
    policy = re.search(r"\bPOL[-\s]?(\d{4})\b", text, re.I)
    if policy:
        session.policy_hint = "POL-" + policy.group(1)
    case = re.search(r"\bCL[-\s]?(\d{4})\b", text, re.I)
    if case:
        session.case_hint = "CL-" + case.group(1)
    return notes


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


# ---------------------------------------------------------------- conversation signals

def shouting(text: str) -> bool:
    """Mostly upper-case prose, not a couple of acronyms such as SSN or SYSTEM OVERRIDE."""
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.7


def emotion_of(text: str) -> str:
    low = norm(text)
    if (re.search(r"ridiculous|angry|furious|unacceptable|frustrat|already told|upset|annoyed|useless|waste of (?:my )?time|"
                  r"\bstupid\b|terrible|awful|fed up|sick of|\bwtf\b|\bdamn\b|\bpissed\b|not satisfied|still waiting|"
                  r"what'?s the point|why bother|no point|give up|hopeless", low)
            or text.count("!") >= 3 or shouting(text)):
        return "frustrated"
    if re.search(r"worried|anxious|scared|stress\w*|afraid|overwhelmed|panic|can'?t afford|cannot afford|nervous|desperate|"
                 r"terrified|lose (?:my )?(?:house|home|job|apartment)|can'?t (?:sleep|breathe|cope)|please,? (?:just )?help|"
                 r"urgent|emergency|critical condition|life is at stake|dying|crying", low):
        return "anxious"
    if re.search(r"confused|don'?t understand|do not understand|unclear|makes no sense|what does that mean|i'?m lost", low):
        return "confused"
    return "neutral"


def opening(session: Session, emotion: str) -> str:
    """An empathetic lead-in that varies instead of repeating the same sentence every turn."""
    session.emotion_streak = session.emotion_streak + 1 if emotion == session.emotion else 0
    session.emotion = emotion
    options = OPENINGS.get(emotion)
    return options[session.emotion_streak % len(options)] + " " if options else ""


def empathy(text: str) -> str:
    options = OPENINGS.get(emotion_of(text))
    return options[0] + " " if options else ""


def scope_state(text: str) -> str:
    """'claim', 'unrelated', or 'unsure'/'unsure_question' for wording a model may classify."""
    low = norm(text)
    claim_words = re.search(CLAIM_WORDS, low)
    if re.search(r"reinforcement learning|\bwhat(?:'s| is) rl\b", low) or (OFF_TOPIC_RE.search(low) and not claim_words):
        return "unrelated"
    if (claim_words or re.search(WEAK_REFERENCES, low) or smalltalk(text) or emotion_of(text) != "neutral"
            or REFUSAL_RE.search(low) or re.search(r"\d{4}|\d[-/.]\d|@", low) or declared_name(text)
            or re.fullmatch(r"[A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+){0,2}[.!]?", text.strip())):
        return "claim"
    if re.match(r"^(?:what is|what are|what's|who is|who's|who won|where is|when did|tell me about|explain|how do i|how to|"
                r"can you (?:explain|tell me|write|solve|translate|recommend|help me with)|write|recommend|give me)\b", low):
        return "unsure_question"
    return "unsure"


def is_off_topic(text: str) -> bool:
    return scope_state(text) in ("unrelated", "unsure_question")


def smalltalk(text: str) -> str:
    low = norm(text).strip(" .!?")
    if re.fullmatch(r"(?:hi|hello|hey|hiya|good (?:morning|afternoon|evening))(?: there)?", low):
        return "greeting"
    if re.fullmatch(r"(?:hi |hello |hey )?(?:how are you(?: doing)?|how's it going)(?: today)?", low):
        return "how_are_you"
    if re.fullmatch(r"(?:ok|okay|got it|i see|alright|cool|great)", low):
        return "ack"
    return ""


RELATIVE = r"(?:mother|father|mom|dad|mum|wife|husband|spouse|partner|son|daughter|parent|grandma|grandmother|grandpa|grandfather|sister|brother|aunt|uncle|friend|client|patient|boss)"
ROLE = r"(?:son|daughter|spouse|wife|husband|partner|caregiver|representative|lawyer|attorney|assistant|agent)"


def caller_is_third_party(text: str) -> bool:
    """The person typing says they are not the policyholder."""
    low = norm(text)
    return bool(
        re.search(r"\b(?:calling|speaking|writing|typing|asking)\s+(?:on behalf of|for (?:her|him|them)\b)", low)
        or re.search(r"\b(?:calling|speaking|writing|asking|here)\s+for\s+my\s+" + RELATIVE, low)
        or re.search(r"\b(?:helping|assisting)\s+(?:out\s+)?my\s+" + RELATIVE, low)
        or re.search(r"\b(?:i am|i'm|this is)\s+[^.!?]{0,50}?'s\s+" + ROLE + r"\b", low)
        or re.search(r"\b(?:i am|i'm|this is)\s+(?:his|her|their)\s+" + ROLE + r"\b", low)
        or re.search(r"\b(?:i am|i'm)\s+(?:a|the)\s+" + ROLE + r"\s+(?:for|of)\b", low)
        or re.search(r"\b(?:she|he|they)(?:'s| is| are|'re) (?:right )?(?:here|next to me|with me|beside me)\b", low)
        or re.search(r"\bhanded me the phone\b|\bon (?:her|his|their) behalf\b|\bpower of attorney\b", low)
    )


def about_other_person(text: str) -> bool:
    """The request concerns someone else's identity details or claim."""
    low = norm(text)
    return bool(
        re.search(r"\bmy\s+" + RELATIVE + r"(?:'s)?\s+(?:claim|policy|case|dob|date of birth|ssn|birthday)\b", low)
        or re.search(r"\b(?:his|her|their)\s+(?:\w+\s+)?(?:claim|dob|date of birth|ssn|policy|birthday|insurance)\b", low)
        or re.search(r"\b(?:his|her|their) name (?:is|was)\b", low)
        or re.search(r"\bthat'?s my\s+" + RELATIVE + r"'?s\b", low)
    )


def third_party_declaration(text: str) -> bool:
    return caller_is_third_party(text) or about_other_person(text)


def other_holder_named(text: str, holder: dict) -> bool:
    own = {norm(n) for n in [holder["name"], *holder.get("name_aliases", [])]}
    return any(re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text, re.I)
               for h in HOLDERS for n in [h["name"], *h.get("name_aliases", [])] if norm(n) not in own)


def different_identity(text: str, holder: dict, also_allowed: tuple[str, ...] = ()) -> bool:
    low = norm(text)
    if any(x in low for x in ("call me", "call my name", "use my name")):
        return False
    match = re.search(r"\bmy name is\s+([a-z][a-z'-]*(?:\s+[a-z][a-z'-]*)?)", text, re.I)
    if not match:
        match = re.search(r"\b(?i:i am|i'm|this is)\s+([A-Z][a-z'-]+\s+[A-Z][a-z'-]+)\b", text)
    if not match:
        return False
    candidate = norm(match.group(1))
    if candidate.startswith(("the ", "a ")) or candidate.split()[0] in NAME_STOP:
        return False
    allowed = [norm(x) for x in [holder["name"], *holder.get("name_aliases", []), *also_allowed] if x]
    return candidate not in allowed and candidate not in {x.split()[0] for x in allowed}


def listed_representative(text: str, third_party_context: bool) -> dict | None:
    """A representative from the fixture who introduces themself (or is named while speaking for someone)."""
    for rep in REPS:
        name = re.escape(rep["rep_name"]) + r"(?!\w)"
        if re.search(r"\b(?:my name is|i am|i'm|this is|it's)\s+" + name, text, re.I) or (
                third_party_context and re.search(r"(?<!\w)" + name, text, re.I)):
            return rep
    return None


def relationship_ok(text: str, rep: dict) -> bool:
    """A stated relationship must agree with the representative record (e.g. a listed son, not a husband)."""
    low = norm(text)
    stated = re.search(r"'s\s+(son|daughter|wife|husband|spouse|caregiver|lawyer|attorney|friend)\b"
                       r"|\b(?:her|his|their)\s+(son|daughter|wife|husband|spouse|caregiver|lawyer|attorney|friend)\b", low)
    if stated:
        return (stated.group(1) or stated.group(2)) == rep["relationship"]
    mine = re.search(r"\bmy\s+(mother|mom|mum|father|dad|wife|husband|son|daughter)\b", low)
    if mine:
        implied = {"mother": ("son", "daughter"), "mom": ("son", "daughter"), "mum": ("son", "daughter"),
                   "father": ("son", "daughter"), "dad": ("son", "daughter"), "wife": ("husband", "spouse"),
                   "husband": ("wife", "spouse"), "son": ("mother", "father", "parent"),
                   "daughter": ("mother", "father", "parent")}[mine.group(1)]
        return rep["relationship"] in implied
    return True


def representative_self_intro(text: str) -> bool:
    return any(
        re.search(r"\b(?:my name is|i am|i'm|this is)\s+" + re.escape(rep["rep_name"]) + r"(?!\w)", text, re.I)
        for rep in REPS
    )


def refused_fields(text: str) -> list[str]:
    low = norm(text)
    if not re.search(r"\b(?:not|won'?t|don'?t|refuse|rather not|no way|never)\b", low):
        return []
    return [key for key, pattern in (("id_last4", r"ssn|social|last four|last 4|national id"), ("dob", r"birth|dob|birthday"),
                                     ("phone", r"phone"), ("email", r"email")) if re.search(pattern, low)]


def without_identity(text: str) -> str:
    """Drop date-of-birth wording so a birth month is never mistaken for a claim clue."""
    return re.sub(DOB_CUE + r"[^\w]{0,5}(?:is\s+|was\s+)?(?:the\s+)?(?:" + MONTH_RE + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+'?\d{2,4}"
                  r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + MONTH_RE + r"\.?,?\s+'?\d{2,4}|\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4})",
                  " ", text, flags=re.I)


def remember_for_later(text: str, s: Session) -> list[str]:
    """Keep requests that belong to a later phase (name to use, email summary wish) without acting on them yet."""
    notes = []
    name = re.search(r"\b(?:please\s+)?call me\s+([A-Za-z][a-z'-]{1,29})\b", text, re.I)
    if name and name.group(1).lower() not in NAME_STOP:
        s.preferred_name, s.preferred_name_pending = name.group(1).title(), True
        notes.append("name")
    if s.phase != "POST_PROCESS" and re.search(
            r"\b(?:e-?mail)\b[^.?!]{0,30}\b(?:summary|recap|copy)\b|\bsend me (?:a |the )?(?:summary|recap)\b|\bemail me\b", norm(text)):
        s.email_requested = True
        notes.append("email")
    return notes


def detect_hint(text: str, session: Session) -> None:
    clean = without_identity(text)
    low = norm(clean)
    if re.search(r"claim|denied|denial|appeal|reimburse|covered|documents?|status|payment|rejected|report|upload\w*|"
                 r"submitted|summited|paperwork|office note", low):
        session.intent_hint = clean[:500]
    if not session.case_hint and claim_clues(clean):
        session.case_hint = clean[:500]


def claim_clues(text: str) -> dict[str, str]:
    low = norm(text)
    clues = {}
    for kind, pattern in TYPE_CLUES:
        if re.search(r"\b(?:" + pattern + r")\b", low):
            clues["type"] = kind
            break
    for status, pattern in STATUS_CLUES:
        if re.search(r"\b(?:" + pattern + r")\b", low):
            clues["status"] = status
            break
    month = next((m for m in MONTH_NAMES if re.search(r"\b" + m + r"\b", low)), "")
    if month:
        clues["month"] = f"{MONTH_NAMES.index(month) + 1:02d}"
    year = re.search(r"\b20\d{2}\b", low)
    if year:
        clues["year"] = year.group()
    if re.search(r"\b(?:latest|most recent|newest|newer|recent|last one|current one|new one)\b", low):
        clues["order"] = "newest"
    elif re.search(r"\b(?:oldest|older|earliest|old one|previous one)\b", low):
        clues["order"] = "oldest"
    return clues


def memory_tags(session: Session) -> list[str]:
    """Caller-provided clues shown in the UI; never claim data before verification."""
    clues = claim_clues(" ".join((session.case_hint, session.intent_hint)))
    tags = [clues[k] for k in ("status", "type") if clues.get(k, "unsupported") != "unsupported"]
    if "month" in clues:
        tags.append(MONTH_NAMES[int(clues["month"]) - 1].title())
    case = re.search(r"\bCL-\d{4}\b", session.case_hint)
    return tags + [case.group()] if case else tags


def model_safe_text(text: str, extra_names: tuple[str, ...] = (), session: Session | None = None) -> str:
    """Remove identity fields before sending a caller utterance to an external model."""
    names = [*extra_names, *(r["rep_name"] for r in REPS)]
    for holder in HOLDERS:
        names += [holder["name"], *holder.get("name_aliases", [])]
    if session:
        # First names too: replies such as "Thank you, Margaret" become model context on the next turn.
        names += [session.fields.get("name", ""), session.preferred_name, *session.fields.get("name", "").split()]
    declared = declared_name(text)
    if declared:
        names.append(declared)
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "[name]", text, flags=re.I)
    text = EMAIL_RE.sub("[email]", text)
    text = PHONE_RE.sub("[phone]", text)
    text = re.sub(r"\bPOL[-\s]?\d{4}\b", "[policy number]", text, flags=re.I)
    text = re.sub(r"\b(?:ssn|social security|national id|id|last four|last 4|last4)\b.{0,30}?\b\d{4}\b", "[ID detail]", text, flags=re.I)
    text = re.sub(DOB_CUE + r"[^\d]{0,5}(?:" + MONTH_RE + r"\.?\s+)?\d[\d/\-., ]{2,10}\d", "[date of birth]", text, flags=re.I)
    for holder in HOLDERS:
        text = text.replace(holder["dob"], "[date of birth]").replace(holder["id_last4"], "[ID digits]")
    text = re.sub(r"\b(?:19|20)\d\d[-/]\d\d?[-/]\d\d?\b|\b\d\d?/\d\d?/(?:19|20)\d\d\b", "[date]", text)
    return re.sub(r"(?<![-$\d])\d{3}-?\d{2}-?(\d{4})(?!\d)|(?<![-$.\d])(?!(?:19|20)\d\d)\d{4}(?![.\d])", "[digits]", text)


# ---------------------------------------------------------------- claim resolution

def choose_claim(text: str, session: Session, model: ModelClient) -> tuple[dict | None, list[dict]]:
    own = [c for c in CLAIMS if c["party_id"] == session.holder_id]
    text = without_identity(text)
    clues = claim_clues(text)
    explicit = re.findall(r"\bCL[-\s]?(\d{4})\b", text, re.I) or (
        [] if clues or session.candidate_ids else re.findall(r"\bCL[-\s]?(\d{4})\b", session.case_hint, re.I))
    if explicit:
        own = [c for c in own if c["case_id"] == "CL-" + explicit[-1]]
        return (own[0] if len(own) == 1 else None), own
    pool = [c for c in own if c["case_id"] in session.candidate_ids] or own
    ordinal = re.search(r"\b(?:the\s+)?(first|second|third|fourth|1st|2nd|3rd|4th)(?:\s+one)?\s*[.!?]*$"
                        r"|\b(?:the\s+)(first|second|third|fourth|1st|2nd|3rd|4th)\s+(?:one|claim)\b"
                        r"|\b(?:number|option|#)\s*([1-4])\b", norm(text))
    if ordinal and session.candidate_ids and not clues:
        word = ordinal.group(1) or ordinal.group(2)
        index = int(ordinal.group(3)) - 1 if ordinal.group(3) else (
            ["first", "second", "third", "fourth"].index(word) if word.isalpha() else int(word[0]) - 1)
        if index < len(pool):
            return pool[index], [pool[index]]
    if not clues and not session.candidate_ids:
        clues = claim_clues(" ".join((session.case_hint, session.intent_hint, text)))

    def narrow(claims: list[dict]) -> list[dict]:
        if "type" in clues:
            claims = [c for c in claims if c["case_type"] == clues["type"]]
        if "status" in clues:
            claims = [c for c in claims if c["status"] == clues["status"]]
        if "month" in clues:
            claims = [c for c in claims if c["created_at"][5:7] == clues["month"]]
        if "year" in clues:
            claims = [c for c in claims if c["created_at"].startswith(clues["year"])]
        if clues.get("order") and claims:
            pick = max if clues["order"] == "newest" else min
            claims = [pick(claims, key=lambda c: c["created_at"])]
        return claims

    filtered = narrow(pool)
    if not filtered and pool is not own:
        filtered = narrow(own)
    if not clues:
        filtered = pool
    if len(filtered) == 1:
        return filtered[0], filtered
    if len(filtered) > 1:
        # Remembered hints count too: "I already uploaded the pathology report" points at the claim that needs it.
        low = norm(text if clues else " ".join((session.case_hint, session.intent_hint, text)))
        doc_hit = [c for c in filtered if any(w in low for d in c.get("documents_needed", []) for w in d.split() if len(w) > 4)]
        active = [c for c in filtered if c["status"] != "closed"]
        if len(doc_hit) == 1:
            return doc_hit[0], doc_hit
        if len(active) == 1 and set(local_topics(text)) & ACTIVE_TOPICS:
            return active[0], active
    if len(filtered) > 1 and model.enabled and not clues:
        choice = model.select_claim(model_safe_text(text, session=session), [safe_claim(c) for c in filtered])
        match = next((c for c in filtered if c["case_id"] == choice), None)
        if match:
            return match, filtered
    return None, filtered


ACTIVE_TOPICS = {"denial_reason", "documents", "document_detail", "alternatives", "appeal", "how_to_get_documents",
                 "submission_method", "submission_dispute", "receipt_check", "review_timing", "next_steps", "outcome",
                 "submission_timing", "provider_unresponsive", "other_documents"}


NO_CLAIMS = ("I don't see any claims on file for your policy. If you expected to see one, a human representative can "
             "look into it; just say \"representative\". Is there anything else I can help with?")


def claim_list(claims: list[dict]) -> str:
    return "; ".join(f"{c['case_id']} ({c['case_type']}, filed {fmt_date(c['created_at'])}, {c['status']})" for c in claims)


def select_case(s: Session, claim: dict) -> None:
    allow(s, "select_own_claim", claim)
    s.case_id = claim["case_id"]
    s.candidate_ids = []
    if s.case_id not in s.discussed_case_ids:
        s.discussed_case_ids.append(s.case_id)
    s.phase = "PROCESS_CASE"


def resolve(s: Session, text: str, model: ModelClient, remembered: bool = False) -> str:
    source = (s.intent_hint or s.case_hint or text) if remembered else (
        text if local_topics(text) != ["clarify"] else s.pending_question or text)
    selected, candidates = choose_claim(text, s, model)
    own = [c for c in CLAIMS if c["party_id"] == s.holder_id]
    if not own:
        return NO_CLAIMS
    if not selected:
        if local_topics(text) != ["clarify"]:
            s.pending_question = text
        if not candidates and remembered:
            s.candidate_ids = [c["case_id"] for c in own]
            return f"I see {len(own)} claims on the policy: " + claim_list(own) + ". Which one would you like to discuss?"
        if not candidates:
            s.candidate_ids = [c["case_id"] for c in own]
            return ("I don't see a claim matching that description on the policy. Here is what I can see: "
                    + claim_list(own) + ". Which one would you like to discuss?")
        s.candidate_ids = [c["case_id"] for c in candidates]
        previous = next((t["text"] for t in reversed(s.turns) if t["role"] == "assistant"), "")
        if len(candidates) == 2 and previous.startswith(("Just to open the right one", "No problem")):
            a, b = sorted(candidates, key=lambda c: c["created_at"], reverse=True)
            return (f"No problem. Is it the newer one from {a['created_at'][:4]} that is {a['status']}, or the older one "
                    f"from {b['created_at'][:4]} that is {b['status']}? You can say \"the newer one\", \"the older one\", "
                    f"or the claim ID ({a['case_id']} or {b['case_id']}).")
        if len(candidates) == 2:
            a, b = candidates
            return (f"Just to open the right one: do you mean {a['case_id']} ({a['case_type']}, filed "
                    f"{fmt_date(a['created_at'])}, {a['status']}) or {b['case_id']} ({b['case_type']}, filed "
                    f"{fmt_date(b['created_at'])}, {b['status']})?")
        lead = f"I see {len(candidates)} claims that could match: " if len(candidates) < len(own) else \
            f"I see {len(candidates)} claims on the policy: "
        return lead + claim_list(candidates) + ". Which one would you like to discuss?"
    select_case(s, selected)
    s.pending_question = ""
    if local_topics(source) == ["clarify"] and (remembered or not model.enabled):
        answer = status_sentence(selected)
    else:
        answer, _ = case_response(s, selected, source, model)
    intro = (f"I used what you mentioned earlier to find {selected['case_id']}, "
             f"{'her' if s.rep_name else 'your'} {selected['case_type']} claim "
             f"filed {fmt_date(selected['created_at'])}. " if remembered else f"I found {selected['case_id']}. ")
    return intro + answer + " What else would you like to know?"


# ---------------------------------------------------------------- grounded case answers

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
    if re.search(r"\b(?:why|denied|denial|reason|rejected)\b", low):
        topics.append("denial_reason")
    if any(x in low for x in ("document", "paperwork", "report", "office note", "what do i need", "what do you need")) or re.search(
            r"\bwhat (?:should|do|can|must) i (?:send|submit|upload|provide|give)\b|\bwhat else do you need\b", low):
        topics.append("documents")
    if re.search(r"\b(?:upload|portal|fax|mail|mailing|where do i send|where should i send|how do i submit|how do i send)\b", low):
        topics.append("submission_method")
    if any(x in low for x in ("got it", "received", "receipt", "confirmation", "attached")):
        topics.append("receipt_check")
    if any(x in low for x in ("how soon do i need to", "when should i submit", "when do i need to submit")):
        topics.append("submission_timing")
    elif "how long do i have" not in low and any(x in low for x in ("how long", "processing time", "review time", "after i submit", "once i submit", "when will i hear", "when will it")):
        topics.append("review_timing")
    if re.search(r"\b(?:appeal|deadline|too late)\b|how (?:long|much time) do i have|when is it due", low):
        topics.append("appeal")
    if re.search(r"\b(?:will (?:it|they|you) (?:be )?(?:approve|approved|accept|cover|covered|pay)|chances?|overturn|get approved|be approved)\b", low):
        topics.append("outcome")
        if "how much" in low:
            topics.append("payment")
    elif any(x in low for x in ("paid", "payment", "reimburse", "amount", "dollar", "money", "how much", "owe", "$")):
        topics.append("payment")
    if any(x in low for x in ("don't have", "do not have", "can't get", "cannot get", "alternative", "substitute")) or re.search(
            r"\bonly have\b|\bbut not (?:the|a|my)\b|\bmissing (?:the|my)\b|\blost (?:the|my)\b", low) or (
            "instead" in low and re.search(r"document|report|note|file|photo|estimate", low)):
        topics.append("alternatives")
    if re.search(r"\b(?:what (?:do|should|can) i do|what now|next steps?|what happens (?:now|next)|how (?:do|can) i fix|what can be done|"
                 r"what'?s the point|why bother|no point|give up)\b", low):
        topics.append("next_steps")
    if any(x in low for x in ("status", "progress", "outcome", "update", "what's going on", "what is going on", "where is my", "where's my")):
        topics.append("status")
    if re.search(r"\b(?:phone number|contact|call (?:you|someone|the office)|reach (?:you|someone|a person)|office hours|"
                 r"mailing address|fax number)\b", low) and "submission_method" not in topics:
        topics.append("contact")
    if re.search(r"\bwaive\w*|\bexception\b|\bskip (?:the )?(?:documents?|requirement)|\bwithout (?:the )?(?:documents?|report|note)|"
                 r"\boverride\b|\bbend the rules?\b", low):
        topics.insert(0, "exception")
    if re.search(r"\bwhat(?:'s| is| even is| exactly is)\s+(?:a|an|the)\s+(?:pathology|office note|diagnosis|repair estimate|report|note)"
                 r"|\bwhat (?:does|do)\s+(?:a |an |the )?(?:pathology|office note|diagnosis|report|note)\b[\w ]{0,20}\bmean\b", low):
        topics.insert(0, "document_detail")
    if re.search(r"\b(?:i have|i've got|i got|but i have|i do have|i only have)\b[^.?!]{0,40}\b(?:payment|receipt|bill|invoice|"
                 r"statement|proof)\b", low):
        topics = ["other_documents"] + [t for t in topics if t not in ("payment", "other_documents")]
    if re.search(r"\b(?:update|change|new)\b[^.?!]{0,30}\b(?:email|address|phone|number|name)\b(?:[^.?!]{0,20}\bon file\b)?", low) \
            and re.search(r"\b(?:update|change|please|my new|got a new)\b", low):
        return ["account_change"]
    if re.search(r"\bmeans?\b[^.?!]{0,20}\b(?:delayed|pending|waiting)\b|(?:does|is) [\w' ]{0,12}(?:denied|closed|open)['\"]? mean|"
                 r"\b(?:denied|closed|open)['\"]? (?:is )?(?:good|bad)\b|good or bad|what does (?:denied|closed|open) mean", low):
        topics.append("status_meaning")
    if re.search(r"\bcan (?:my|a|the) (?:husband|wife|spouse|partner|son|daughter|mom|mother|dad|father|family|kids?|"
                 r"someone|friend)\b[^.?!]{0,30}\b(?:see|access|view|look at|call|talk|help|manage|handle)", low):
        return ["access_request"]
    if re.search(r"never (?:answer|respond|call)|not (?:answering|responding)|won'?t (?:answer|respond|give|send)|"
                 r"can'?t (?:reach|get hold of|get through)|cannot reach|refus\w* to (?:give|send)|"
                 r"didn'?t (?:give|send)|won'?t release|no one (?:answers|picks up)", low):
        topics = ["provider_unresponsive"] + [t for t in topics if t not in ("contact", "how_to_get_documents")]
    if re.search(r"\bhow (?:do|can|should) i (?:get|obtain|request|ask for)\b|\bwhere (?:do|can) i get\b", low):
        topics.insert(0, "how_to_get_documents")
    if "alternatives" in topics and "documents" in topics:
        topics.remove("documents")
    return topics[:3] or ["clarify"]


def missing_doc(claim: dict, text: str) -> str:
    """'I only have the office note but not the pathology report' -> 'pathology report'."""
    low = norm(text)
    for doc in claim.get("documents_needed", []):
        words = [w for w in doc.split() if len(w) > 4] or doc.split()
        if re.search(r"\b(?:not|no|without|missing|don'?t have|do not have|can'?t get|cannot get|lost)\s+(?:the |a |an |my |any )?"
                     r"(?:\w+\s+){0,2}?(?:" + "|".join(map(re.escape, words)) + r")", low):
            return doc
    return ""


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


def status_sentence(claim: dict) -> str:
    cid, status = claim["case_id"], claim["status"]
    if status == "open":
        return f"{cid} is open and still in progress. The record doesn't show a decision date yet."
    if status == "closed":
        return f"{cid} is closed, with ${claim['net_pay']} paid."
    return f"{cid} is currently {status}."


def appeal_sentence(claim: dict) -> str:
    deadline = fmt_date(claim["appeal_deadline"])
    if deadline_passed(claim):
        return (f"The recorded appeal deadline was {deadline}, and that date has passed. I can't confirm whether a late "
                "appeal or reconsideration is still possible, so a human representative should review your options.")
    return f"The recorded appeal deadline is {deadline}. A representative can help you prepare the appeal."


def topic_answer(claim: dict, topics: list[str], text: str = "") -> str:
    cid = claim["case_id"]
    docs = claim.get("documents_needed", [])
    reason = claim.get("denial_reason")
    average = GUIDE["claim_followup_settings"]["average_processing_time_after_submission"]["en"]
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
        elif topic == "denial_reason" or topic == "status":
            parts.append(status_sentence(claim))
        elif topic == "documents":
            parts.append(f"To review it again, the claims team needs {doc_phrase(claim)}." if docs
                         else f"The record doesn't list any outstanding documents for {cid}.")
        elif topic == "submission_method" and docs:
            parts.append(GUIDE["default_guidance"]["en"])
        elif topic == "receipt_check":
            parts.append("This record does not confirm whether a later upload was received. "
                         "A representative can check whether the files were attached to the claim.")
        elif topic == "submission_timing" and docs:
            parts.append(f"The guidance asks for {doc_phrase(claim)} within a week.")
            if deadline_passed(claim):
                parts.append(appeal_sentence(claim))
        elif topic == "review_timing":
            parts.append(f"After the requested files are received, the average review time is {average}; "
                         "intake or another review cycle may take longer." if docs else
                         f"The record doesn't give a completion date for {cid}. A representative can check its timing.")
        elif topic == "appeal":
            parts.append(appeal_sentence(claim) if claim.get("appeal_deadline")
                         else f"The record doesn't show an appeal deadline for {cid}.")
        elif topic == "payment":
            if claim["status"] == "closed":
                parts.append(f"{cid} paid ${claim['net_pay']}; the allowed maximum was ${claim['allowed_max_amount']}.")
            elif claim["status"] == "denied":
                parts.append(f"Nothing has been paid on {cid} (${claim['net_pay']}), and the expected reimbursement is "
                             f"${claim['expected_reimbursement_amount']} while it is denied. The allowed maximum for the "
                             f"service is ${claim['allowed_max_amount']}, but I can't predict what a new review would pay.")
            else:
                parts.append(f"So far ${claim['net_pay']} has been paid on {cid}. The expected reimbursement is "
                             f"${claim['expected_reimbursement_amount']} of an allowed maximum of "
                             f"${claim['allowed_max_amount']}; the final amount depends on the review.")
        elif topic == "outcome":
            if docs:
                parts.append(f"I can't promise the outcome. Once {doc_phrase(claim)} are received, the claim goes back "
                             f"into review (the average review time is {average}), and the review team makes the decision.")
            else:
                parts.append(status_sentence(claim) + " I can't predict a future decision from this record.")
        elif topic == "next_steps":
            if docs:
                parts.append(f"The next step is to get {doc_phrase(claim)} and upload them through the member portal "
                             "or claim upload link.")
                if deadline_passed(claim):
                    parts.append(appeal_sentence(claim))
            elif claim["status"] == "open":
                parts.append(f"The record doesn't list anything you need to send for {cid}; it is still in progress.")
            else:
                parts.append(f"{cid} is {claim['status']}, and the record shows nothing outstanding.")
        elif topic == "account_change":
            parts.append("I can't change contact details in this chat. A human representative can update your email or "
                         "phone after verifying you; until then, any summary can only go to the email already on your "
                         "policy record.")
        elif topic == "status_meaning":
            meaning = {"denied": f"Not quite. Denied means {cid} was reviewed and not paid as submitted, because {reason}. "
                                 "It isn't just waiting: once the missing files are received, the review restarts.",
                       "closed": f"Closed means the review of {cid} is finished; the record shows ${claim['net_pay']} paid.",
                       "open": f"Open means {cid} is still being reviewed and no decision has been made yet."}
            parts.append(meaning[claim["status"]])
        elif topic == "access_request":
            parts.append("Right now I can only discuss this claim with you. Someone else can be helped here only if "
                         "they're listed as an authorized representative on your policy and you approve their access; a "
                         "human representative can help set that up.")
        elif topic == "provider_unresponsive" and docs:
            parts.append("If the doctor's office isn't responding, you can also ask the hospital or lab that ran the test "
                         "to resend the report directly, and ask the clinic for a visit summary or discharge paperwork "
                         "in place of the full office note. If none of those can be obtained, a human representative "
                         "should review manual options with you; just say \"representative\".")
        elif topic == "how_to_get_documents" and docs:
            parts.append(f"Contact the hospital, lab, or treating provider and ask for a replacement copy of "
                         f"{doc_phrase(claim)}, or ask them to send it directly; if the clinic can fax or upload the office "
                         "note itself, that is often the cleanest option. Once you have the files, upload them through the "
                         "member portal or claim upload link. If online upload isn't possible, support can arrange fax or mail.")
        elif topic == "other_documents" and docs:
            parts.append(f"You can include what you have, but it doesn't replace {doc_phrase(claim)}, which the record "
                         "lists as needed for the review. If one of them is still pending, submit what you have with a "
                         "short note explaining what's missing.")
        elif topic == "exception":
            parts.append("I can't waive or change the claim requirements from this chat. " + (
                f"The record lists {doc_phrase(claim)} as needed; if you can't get them, a representative can review "
                "manual options with you." if docs else "A representative can review the claim with you."))
        elif topic == "contact":
            parts.append("I don't have contact details for the claims office in this chat, so I won't guess a number "
                         "or address. If you'd like, I can mark this conversation for a human representative.")
        elif topic == "document_detail" and docs:
            specific = matching_guidance(claim, text, "document_guidance")
            parts.extend(specific if len(specific) < len(docs) else [
                GUIDE["default_guidance"]["en"], f"Tell me which document you're preparing ({join_words(docs, 'or')}) "
                "and I'll share what it should include."])
        elif topic == "alternatives" and docs:
            specific = matching_guidance(claim, missing_doc(claim, text) or text, "document_alternative_guidance")
            parts.extend(specific if len(specific) < len(docs) else [
                GUIDE["document_alternative_guidance"]["default"]["en"],
                f"If you tell me which one is hard to get ({join_words(docs, 'or')}), I can share specific options."])
    if not parts:
        options = ("its status, denial reason, documents, how to submit them, timing, or payment, "
                   "or connect you with a representative")
        if "?" not in text and emotion_of(text) != "neutral":
            return f"I'm here to help sort this out. For {cid}, I can explain {options}. What would help most?"
        return f"I don't see enough in the record for {cid} to answer that reliably. I can explain {options}."
    return " ".join(dict.fromkeys(parts))


def case_facts(claim: dict) -> dict:
    """The only claim information a model may phrase: no identity fields and no other claims."""
    facts = {k: claim[k] for k in ("case_id", "case_type", "status", "summary") if k in claim}
    facts["filed_on"] = fmt_date(claim["created_at"])
    facts["payment"] = {k: claim[k] for k in ("net_pay", "expected_reimbursement_amount", "allowed_max_amount")}
    for key in ("denial_reason", "documents_needed"):
        if claim.get(key):
            facts[key] = claim[key]
    if claim.get("appeal_deadline"):
        facts["appeal_deadline"] = fmt_date(claim["appeal_deadline"])
    if claim.get("appeal_deadline"):
        facts["appeal_deadline_has_passed"] = deadline_passed(claim)
    if claim.get("documents_needed"):
        docs = claim["documents_needed"]
        facts["guidance"] = {
            "how_to_submit": GUIDE["default_guidance"]["en"],
            "submit_within": "a week",
            "review_after_receipt": GUIDE["claim_followup_settings"]["average_processing_time_after_submission"]["en"],
            "document_requirements": matching_guidance(claim, " ".join(docs), "document_guidance"),
            "if_a_document_is_unavailable": matching_guidance(claim, " ".join(docs), "document_alternative_guidance"),
        }
    facts["record_cannot_confirm"] = ["whether documents sent after the decision were received",
                                      "any future decision or payment"]
    return facts


# Words a grounded reply may use besides the claim record and the guidance fixture: conversation, not facts.
CONVERSATION_WORDS = set("""
understand understanding frustrating frustration stressful stress worrying worried sorry help helpful happy glad please
thank thanks welcome know like would could should want wanted need needs needed sure able unable cannot still also however
additionally simply currently right today first next then once after before while until again only just more other
another anything something else further directly instead available record records recorded system shows show check
checked checking review reviewed reviewing reviews representative representatives human person people connect speak talk
contact reach hand over question questions details detail information options option remains remain possible whether
confirm confirmed promise predict decision decisions approve approved approval outcome payment payments paid pay amount
dollars zero total expected maximum allowed status open closed denied denial reason reasons because missing include
included including submit submitted submitting send sent upload uploaded uploading portal link copy copies provider
providers doctor office clinic hospital lab deadline appeal appeals passed period timeframe time week weeks days business
usually average longer claim claims case policy policyholder healthcare medical dental auto filed date dated access
authorized authorization consent spouse family member privacy private protect protected their these those this that there
here with from into your yours about what which when where have been being will were they them than each every some
make made take takes taking getting obtain request requested ask asking tell explain share give keep look looking find
found work works prepare preparing ready receive received receipt based regarding related assist assistance support team
might must current currently recently difficult away explore exploring note notes documents document file files report
reports pathology requested requirements required requires process processing processed started start resolve resolved
submission submissions mail fax online steps step completed complete readable legible clear clearly ensure sure want
hard really phone number call calls discuss discussed someone able course depends late reconsideration verified
verification identity email summary earlier mentioned says said
""".split()) | set(MONTH_NAMES)


def ungrounded_words(sentence: str, vocabulary: set[str]) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", sentence.lower()) if len(w) >= 4 and w not in vocabulary]


def numbers_in(text: str) -> set[float]:
    return {float(n.replace(",", "")) for n in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", text)}


def grounded(reply: str, claim: dict, topics: list[str], facts: dict, missing: str = "", asked_amount: bool = False) -> bool:
    """Accept model wording only when every fact in it is traceable to the claim record."""
    if not 20 <= len(reply) <= 900 or re.search(r"[\[\]{}<>@*#]|https?://", reply):
        return False
    if {c.upper() for c in re.findall(r"\bCL-\d{4}\b", reply, re.I)} - {claim["case_id"]}:
        return False
    if not numbers_in(reply) <= numbers_in(json.dumps(facts)):
        return False
    for sentence in re.split(r"(?<=[.!?])\s+", reply.lower()):
        hedged = re.search(r"\b(?:not|no|cannot|unable|whether)\b|n't", sentence)
        if not hedged and (
            re.search(r"\b(?:guarantee\w*|promise\w*|definitely|certainly)\b", sentence)
            or re.search(r"\b(?:will|would|should) (?:be |get )?(?:approved|paid|covered|reimbursed|overturned|reversed)\b", sentence)
            or re.search(r"\b(?:has|have|was|were|is|are)(?: been| now)? (?:received|approved|reopened|overturned|escalated|resubmitted)\b", sentence)
        ):
            return False
        if re.search(r"\bi(?:'ve| have|'ll| will| just)? (?:sent|submitted|escalated|filed|updated|approved|scheduled|"
                     r"transferred|forwarded|emailed|reopened)\b", sentence):
            return False
    # Every sentence (except a short empathy lead) must be built from record, guidance, or conversation words.
    vocabulary = CONVERSATION_WORDS | set(re.findall(r"[a-z]+", json.dumps(facts).lower())) | GUIDE_WORDS
    for sentence in re.split(r"(?<=[.!?])\s+", reply):
        if not EMPATHY_LEAD.fullmatch(sentence.strip() + " ") and len(ungrounded_words(sentence, vocabulary)) >= 2:
            return False
    if deadline_passed(claim):
        deadline = date.fromisoformat(claim["appeal_deadline"])
        for sentence in re.split(r"(?<=[.!?])\s+", reply.lower()):
            names_deadline = {float(deadline.year), float(deadline.day)} <= numbers_in(sentence)
            still_open = re.search(r"\b(?:can|could|may|still|able to)\b[^.]{0,30}\bappeal\b|\bappeal by\b", sentence)
            if (names_deadline or still_open) and not re.search(r"passed|expired|\bpast\b|\bwas\b|ended|missed|no longer|n't|not", sentence):
                return False
    low = reply.lower()
    docs = claim.get("documents_needed", [])
    if "denial_reason" in topics and docs and not all(doc in low for doc in docs):
        return False
    if "documents" in topics and docs and not any(doc in low for doc in docs):
        return False
    if re.search(r"\b(?:final|permanent|irreversible|closed for good)\b", low) and claim["status"] != "closed":
        return False
    if re.search(r"\b(?:cannot|can't|can not|won't|will not) (?:be )?(?:appeal|submit|file|reopen|reconsider)\w*|"
                 r"no longer (?:possible|eligible|able)|not eligible|no (?:further )?options?\b", low):
        return False
    if {"next_steps", "outcome"} & set(topics) and docs and not any(doc in low for doc in docs):
        return False
    if missing and missing not in low:
        return False
    if asked_amount and not numbers_in(reply):
        return False
    if "payment" in topics and float(claim["net_pay"]) not in numbers_in(reply):
        return False
    if {"review_timing", "submission_timing"} & set(topics) and docs and "week" not in low:
        return False
    return True


EMPATHY_LEAD = re.compile(r"^\s*(?:I (?:completely |totally |really |truly )?(?:understand|hear|know|can hear|can see)|"
                          r"I'm (?:so |really )?sorry|That sounds)[^.!?]*[.!?]\s*", re.I)


def support_prompt(claim: dict) -> str:
    if claim.get("documents_needed"):
        return (f"The most useful next step is still getting {doc_phrase(claim)} to the claims team. A human "
                "representative can also review what options remain; just say \"representative\" if you'd like that.")
    return "I can explain the status or payment, or connect you with a human representative. What would help most?"


def without_repeats(body: str, previous: str, keep_deadline: bool, deadline_heard: bool = False,
                    asked_why: bool = False) -> str:
    """Drop sentences the caller just heard (same fact, or the deadline again) unless nothing else is left."""
    heard = {re.sub(r"\W+", " ", x.lower()).strip() for x in re.split(r"(?<=[.!?])\s+", previous)}
    heard_deadline = deadline_heard or "deadline" in previous.lower()
    sentences = re.split(r"(?<=[.!?])\s+", body)
    kept = [x for x in sentences
            if re.sub(r"\W+", " ", x.lower()).strip() not in heard
            and not (heard_deadline and not keep_deadline and re.search(r"deadline|late appeal", x.lower()))
            and not (not asked_why and re.search(r"denied because", x) and re.search(r"denied because", previous))]
    kept = [x for i, x in enumerate(kept) if not any(
        # "the claims team needs the pathology report..." after "doesn't replace the pathology report..."
        re.search(r"needs the ", x) and "doesn't replace" in y for y in kept[:i])]
    if any(not OPENING_RE.fullmatch(x.strip()) for x in kept):
        return " ".join(kept)
    # The same question twice: repeat the answer, but still not the deadline the caller just heard.
    fresh = [x for x in sentences if keep_deadline or not (heard_deadline and re.search(r"deadline|late appeal", x.lower()))]
    return " ".join(fresh) if any(not OPENING_RE.fullmatch(x.strip()) for x in fresh) else body


OPENING_RE = re.compile("|".join(re.escape(o) for group in OPENINGS.values() for o in group))


CODE_TOPICS = {"submission_dispute", "access_request", "account_change", "status_meaning"}


def followup_topics(text: str, last: list[str], claim: dict) -> list[str]:
    """A bare 'how...' / 'and then?' continues the previous answer instead of asking the caller to repeat."""
    low = norm(text).strip(" .?!\u2026")
    if not last or len(low.split()) > 4 or local_topics(text) != ["clarify"]:
        return []
    if low in ("", "huh", "what", "meaning", "so", "and") and "status" in last and claim["status"] == "denied":
        return ["denial_reason"]
    if not re.fullmatch(r"(?:how|how so|how do i|how do i do that|how\.*|and|and then|then what|what then|so|ok and|"
                        r"what now|what next|next|then|huh|what|)", low):
        return []
    if not claim.get("documents_needed"):
        return ["next_steps"]
    if {"alternatives", "other_documents", "denial_reason", "documents"} & set(last):
        return ["how_to_get_documents"]
    if "how_to_get_documents" in last:
        return ["review_timing"]
    if {"next_steps", "document_detail"} & set(last):
        return ["submission_method"]
    if "submission_method" in last:
        return ["review_timing"]
    return ["next_steps"]


def case_response(session: Session, claim: dict, text: str, model: ModelClient) -> tuple[str, bool]:
    allow(session, "read_own_claim", claim)
    previous = next((t["text"] for t in reversed(session.turns) if t["role"] == "assistant"), "")
    facts = case_facts(claim)
    followup = followup_topics(text, session.last_topics, claim) or (
        local_topics(text) if local_topics(text)[0] in CODE_TOPICS else [])
    route = {} if followup else model.analyze_case(model_safe_text(text, (session.preferred_name,), session),
                               model_safe_text(previous, (session.preferred_name,), session), facts) if model.enabled else {}
    fallback = followup or local_topics(text)
    # A clear statement that files were already sent takes priority over a generic model label.
    if fallback == ["submission_dispute"]:
        topics, unrelated, from_model = fallback, False, "submission_dispute" in route.get("topics", [])
    else:
        model_topics = [t for t in route.get("topics", []) if t != "clarify"]
        topics = model_topics or fallback
        unrelated = route.get("scope") == "unrelated" and not re.search(CLAIM_WORDS, norm(text))
        from_model = bool(model_topics) and not (route.get("scope") == "unrelated")
    if unrelated:
        return "", True
    if re.search(r"\b(?:deadline|appeal)\b", norm(text)) and "appeal" not in topics:
        topics, from_model = ["appeal", *topics], False
    if "document_detail" in topics and "documents" in topics:
        topics = [topic for topic in topics if topic != "documents"]
    if claim["status"] == "denied" and "denial_reason" in topics and "status" in topics:
        topics = [topic for topic in topics if topic != "status"]
    emotion = route.get("emotion") if route.get("emotion") not in (None, "neutral") else emotion_of(text)
    lead = opening(session, emotion)
    session.intent = topics[0]
    session.topics_discussed = list(dict.fromkeys([*session.topics_discussed, *(t for t in topics if t != "clarify")]))
    session.last_topics = topics
    reply = route.get("reply", "")
    accepted = from_model and reply and grounded(reply, claim, topics, facts, missing_doc(claim, text), "how much" in norm(text))
    if not accepted and fallback != ["clarify"]:
        # A rejected draft falls back to the record, using the topics the local rules recognized.
        topics = fallback
        if claim["status"] == "denied" and "denial_reason" in topics and "status" in topics:
            topics = [topic for topic in topics if topic != "status"]
    body = reply if accepted else lead + topic_answer(claim, topics, text)
    history = " ".join(t["text"] for t in session.turns if t["role"] == "assistant")
    body = without_repeats(body, previous, keep_deadline="appeal" in topics or bool(re.search(r"deadline|appeal", norm(text))),
                           deadline_heard="deadline" in history.lower(),
                           asked_why=bool(re.search(r"\bwhy\b|\breason\b", norm(text))))
    sentences = re.split(r"(?<=[.!?])\s+", body)
    if (len(sentences) > 1 and emotion_of(text) == "neutral" and EMPATHY_LEAD.match(sentences[0])
            and EMPATHY_LEAD.match(previous)):
        body = " ".join(sentences[1:])
    if len(EMPATHY_LEAD.sub("", body).strip()) < 25:
        body = body.rstrip() + " " + support_prompt(claim)
    if emotion == "frustrated" and not session.human_offered and (
            session.emotion_streak >= 1 or shouting(text) or text.count("!") >= 3):
        session.human_offered = True
        body += " If you'd rather talk this through with a person, just say \"representative\" and I'll hand this over."
    if session.preferred_name_pending:
        body = f"{session.preferred_name}, " + body[:1].lower() + body[1:] if body[:2] != "I " else f"{session.preferred_name}, " + body
        session.preferred_name_pending = False
    return body, False


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
        return ("I'm only able to help with insurance claims here. Since this has come up several times, "
                "I've marked the conversation for a human representative who can point you in the right direction.")
    back = {"VERIFY_ID": "Whenever you're ready, I can verify your identity and look at your claim.",
            "RESOLVE_INTENT": "Which of your claims can I help with?",
            "PROCESS_CASE": "Is there anything else about your claim I can help with?",
            "POST_PROCESS": "Would you like the email summary, or should I skip it?"}[session.phase]
    lead = ("Sorry, that's outside what I can help with. " if session.off_topic_count == 1 else
            "I'm sorry, I still can't help with that topic; if you need something outside claims, a human "
            "representative can help. ")
    return lead + "I can help with insurance claims and related account questions. " + back


# ---------------------------------------------------------------- post-process

def summary(session: Session) -> str:
    case_ids = list(dict.fromkeys([*session.discussed_case_ids, session.case_id]))
    claims = [next(c for c in CLAIMS if c["case_id"] == cid) for cid in case_ids if cid]
    title = claims[0]["case_id"] if len(claims) == 1 else "multiple claims"
    lines = [f"Conversation summary for {title}"]
    if session.rep_name:
        rep = next(r for r in REPS if r["rep_name"] == session.rep_name)
        lines.append(f"This conversation was with {rep['rep_name']}, listed as the policyholder's {rep['relationship']}, "
                     "after the policyholder's details matched and the policyholder approved access.")
    topics = [TOPIC_LABELS[t] for t in session.topics_discussed if t in TOPIC_LABELS]
    if not topics:
        conversation = " ".join(turn["text"].lower() for turn in session.turns if turn["role"] == "user")
        topics = [label for label, hints in (("why the claim was denied", ("why", "denied", "denial")),
                                             ("the documents requested", ("document", "report", "office note")),
                                             ("payment amounts", ("paid", "payment", "reimburse", "amount")))
                  if any(hint in conversation for hint in hints)]
    if topics:
        lines.append("What we discussed: " + join_words(topics) + ".")
    average = GUIDE["claim_followup_settings"]["average_processing_time_after_submission"]["en"]
    for claim in claims:
        lines.append(f"{claim['case_id']}: your {claim['case_type']} claim filed {fmt_date(claim['created_at'])}, "
                     f"current status: {claim['status']}.")
        if claim.get("denial_reason"):
            lines.append(f"The recorded denial reason for {claim['case_id']} is that {claim['denial_reason']}.")
        steps = []
        if claim.get("documents_needed"):
            steps.append("Obtain and submit " + join_words(claim["documents_needed"]) + " through the member portal "
                         "or claim upload link. If online upload is unavailable, contact support for fax or mail options.")
            steps.append(f"After the files are received, the average review time is {average}.")
        if claim.get("appeal_deadline"):
            steps.append(appeal_sentence(claim))
        if not steps:
            steps.append("No action from you is listed in the record." if claim["status"] == "open"
                         else "Nothing is outstanding on this claim.")
        lines.append(f"Next steps for {claim['case_id']}:\n" + "\n".join("- " + step for step in steps))
    lines.append("This summary reflects the demo claim record and does not confirm a new claim decision or submission.")
    return "\n\n".join(lines)


def requested_other_email(session: Session, text: str) -> bool:
    holder = next(h for h in HOLDERS if h["party_id"] == session.holder_id)
    addresses = EMAIL_RE.findall(text)
    redirect = re.search(
        r"\b(?:to|at)\s+(?:my\s+)?(?:other|work|new|different|another|personal|second)\s+(?:email|address)\b"
        r"|\bto\s+my\s+(?:wife|husband|son|daughter|representative|mom|dad|lawyer|friend)\b"
        r"|\bmy\s+(?:gmail|yahoo|hotmail|outlook|icloud|proton\w*|personal|work|office|other|new)(?:\s+(?:email|address|account|inbox))?\b"
        r"|\b(?:cc|bcc|copy|forward|also send|share)\b.{0,25}\b(?:my\s+\w+|him|her|them|someone|anyone|others?)\b"
        r"|\b(?:the )?(?:new|other|different|updated|second) (?:one|email|address)\b",
        text, re.I,
    )
    return bool(redirect or any(address.lower() != holder["email"].lower() for address in addresses))


def consent_decision(text: str) -> str:
    # A change of mind ("don't send it... actually yes send it") is decided by the last clause.
    low = re.split(r"\b(?:actually|on second thought|wait|never ?mind|changed my mind)\b", norm(text))[-1].strip(" .!,")
    negative = re.search(r"\b(?:no|nope|nah|skip|don'?t|do not|not now|no need|not necessary|i'?m good|all set|pass)\b", low)
    positive = re.search(r"\b(?:yes|yeah|yep|yup|sure|ok|okay|please|go ahead|do it|send|email it|email me|fine|"
                         r"that works|on file|that one)\b", low)
    if negative and not positive:
        return "skip"
    if positive and not negative:
        return "send"
    if negative and re.search(r"\b(?:don'?t|do not) send\b|\bskip\b|\bno need\b", low):
        return "skip"
    return ""


def is_closing(text: str) -> bool:
    low = norm(text)
    closing = re.search(r"\b(?:that'?s all|that is all|that'?s it|all done|i'?m done|we'?re done|no more questions|"
                        r"nothing else|goodbye|bye|thank you|thanks|appreciate it)\b", low) or re.fullmatch(
        r"(?:no|nope|no thanks|no thank you|thank u|thanks u|thx|ty|i'?m good|i'?m all set|all set|that'?s everything|skip|"
        r"ok bye|bye bye)[.! ]*", low)
    asking = re.search(r"\b(?:what|how|where|when|why|which|can i|should i|do i)\b", low)
    return bool(closing) and not asking and "?" not in text and local_topics(text) == ["clarify"]


def send_summary(session: Session) -> str:
    allow(session, "send_summary")
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


# ---------------------------------------------------------------- turn handling

def respond(session: Session, text: str, model: ModelClient) -> str:
    text = text.strip()[:2000].replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    if not text:
        return "Please type a message so I can help."
    session.updated_at = datetime.now(timezone.utc).timestamp()
    session.turns.append({"role": "user", "text": text})
    answer = _respond(session, text, model)
    session.turns.append({"role": "assistant", "text": answer})
    session.turns = session.turns[-80:]
    return answer


def transfer(s: Session, text: str) -> str:
    s.human_transfer = True
    return text


def needed_details(s: Session) -> str:
    options = [FIELD_LABELS[k] for k in PII if k not in s.fields and k not in s.refused_fields]
    count = {1: "one more detail", 2: "two more details", 3: "three matching details"}[3 - len(s.fields)]
    return f"{count}: your {options[0]}" if len(options) == 1 else f"{count}: any of your {join_words(options, 'or')}"


def ask_for_details(s: Session) -> str:
    return f"I need {needed_details(s)}."


def held_details(s: Session) -> str:
    return join_words([FIELD_LABELS[k] for k in PII if k in s.fields])


def extract_rep_fields(text: str, s: Session) -> list[str]:
    """In the representative flow every identity field belongs to the policyholder, never to the caller."""
    notes = extract_fields(text, s)
    if norm(s.fields.get("name", "")) in {norm(r["rep_name"]) for r in REPS}:
        del s.fields["name"]
    for holder in HOLDERS:
        for name in [holder["name"], *holder.get("name_aliases", [])]:
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I):
                s.fields["name"] = name
    return notes


def consent_poll(s: Session) -> tuple[str, bool]:
    """Advance the simulated consent record; returns (status, sequence_exhausted)."""
    sequence = CONSENT.get(s.consent_scenario, CONSENT["default"])["status_sequence"]
    status = sequence[min(s.consent_polls, len(sequence) - 1)]
    s.consent_polls += 1
    return status, s.consent_polls >= len(sequence)


def representative_turn(s: Session, text: str, model: ModelClient, new_rep: bool = False) -> str:
    low = norm(text)
    first = s.rep_name.split()[0]
    lead = opening(s, emotion_of(text))
    why = ("Claim records hold the policyholder's private health and payment information, so a representative needs "
           "both a match on her details and her own approval before I share anything. ")
    asked_why = re.search(r"\bwhy\b", low) and re.search(r"consent|permission|approv|authoriz|wait|need", low)
    if s.consent_status == "pending":
        status, exhausted = consent_poll(s)
        if status == "approved":
            return (("Because it's her private health information, she has to approve access herself. "
                     if asked_why else "") + approve_representative(s, text, model))
        if exhausted:
            s.consent_status = "timeout"
            return transfer(s, lead + (why if asked_why else "") + "The policyholder hasn't approved the request yet, "
                            "so I still can't share claim details. I've marked this for a human representative who can "
                            "follow up on the authorization with you.")
        return (lead + (why if asked_why else "") + "The consent request is still pending, so I can't share claim details "
                "yet. You can check again in a moment, or ask for a human representative.")
    before = dict(s.fields)
    notes = extract_rep_fields(text, s)
    new = [k for k in PII if k in s.fields and before.get(k) != s.fields[k]]
    detect_hint(text, s)
    holder = verified_holder(s)
    if holder and holder["party_id"] != s.rep_party:
        return transfer(s, "Those details belong to a policyholder you aren't listed for, so I can't continue. I've marked "
                        "this for a human representative who can check authorization.")
    if holder:
        allow(s, "request_consent")
        s.consent_status, _ = consent_poll(s)
        if s.consent_status == "approved":
            return approve_representative(s, text, model)
        return (lead + f"Thank you, {first}. Those details match the policy record, and you're listed as the "
                f"policyholder's {next(r['relationship'] for r in REPS if r['rep_name'] == s.rep_name)}. " + why +
                "I've requested the policyholder's consent through the contact on her record (a simulated consent check "
                "in this demo). It's pending right now; send any message to check again.")
    if len(s.fields) >= 3:
        s.verify_failures += 1
        if s.verify_failures >= MAX_VERIFY_FAILURES:
            return transfer(s, "For security, I can't keep checking details after several attempts that didn't match. "
                            "I've marked this for a human representative.")
        return (lead + "Those details don't match the policyholder's record together, so I can't continue yet. If "
                "something was mistyped, send the corrected value, or ask for a human representative.")
    parts = [lead.strip(), f"Thank you, {first}, I see you listed as an authorized representative." if new_rep else "",
             f"I've got the policyholder's {join_words([FIELD_LABELS[k] for k in new])}." if new else ""]
    if asked_why:
        parts.append(why)
    if "full_ssn" in notes:
        parts.append("Only the last four digits of the SSN are needed; please don't share the full number.")
    parts.append(("To continue, I need three matching details for the policyholder, then the policyholder's consent. "
                  if new_rep else "") + ask_for_details(s).replace("any of your", "any of the policyholder's"))
    return " ".join(x for x in parts if x)


def approve_representative(s: Session, text: str, model: ModelClient) -> str:
    holder = next(h for h in HOLDERS if h["party_id"] == s.rep_party)
    s.consent_status = "approved"
    s.holder_id = holder["party_id"]
    s.phase = "RESOLVE_INTENT"
    s.refusal_count = 0
    note = (f"Good news: {holder['name']} approved your access (simulated consent record). Thank you, "
            f"{s.rep_name.split()[0]}; I can help with her claims now. ")
    if s.case_hint or s.intent_hint:
        return note + resolve(s, text, model, remembered=True)
    return note + "Which of her claims can I help with?"


def verify_turn(s: Session, text: str, model: ModelClient) -> str:
    low = norm(text)
    if DECEASED_RE.search(text):
        return transfer(s, "I'm so sorry for your loss. When a policyholder has passed away, their claims need to be "
                        "handled by a human representative who can explain what's needed and help you through it, so "
                        "I've marked this conversation for them. Please take your time.")
    third = third_party_declaration(text)
    if not s.rep_name and (third or s.awaiting_rep_name or representative_self_intro(text)):
        rep = listed_representative(text, third or s.awaiting_rep_name)
        if not rep or not relationship_ok(text, rep):
            if s.awaiting_rep_name or rep:
                return transfer(s, empathy(text) + "Thanks. I don't see you listed as an authorized representative for "
                                "this policyholder, so I can't share claim details here. I've marked this for a human "
                                "representative who can check authorization safely.")
            s.awaiting_rep_name = True
            extract_rep_fields(text, s)
            detect_hint(text, s)
            return (empathy(text) + "Thanks for letting me know. I can only help someone other than the policyholder if "
                    "they are listed as an authorized representative on the policy, and only with the policyholder's "
                    "consent. What is your full name?")
        s.rep_name, s.rep_party, s.awaiting_rep_name = rep["rep_name"], rep["buyer_party_id"], False
        return representative_turn(s, text, model, new_rep=True)
    if s.rep_name:
        return representative_turn(s, text, model)
    allow(s, "collect_identity")
    later = remember_for_later(text, s)
    if re.fullmatch(r"(?:ok,?\s*)?(?:let'?s\s+)?(?:start over|clear (?:it|them|that|everything)|reset|restart)[.!]?", low):
        s.fields.clear()
        s.name_parts.clear()
        s.failed_snapshot = ""
        return "Okay, I've cleared the details. Please share any three matching details again: full name, date of birth, phone, email, or SSN last four."
    before = dict(s.fields)
    notes = extract_fields(text, s)
    bare = re.fullmatch(r"(?:sorry,?\s+|it'?s\s+|this is\s+|i'?m\s+)?([a-z][a-z'.-]+(?:\s+[a-z][a-z'.-]+){1,2})[.!]?", text.strip(), re.I)
    if ("name" not in s.fields and bare and not re.search(CLAIM_WORDS, low)
            and not any(w.lower().strip(".") in NAME_STOP for w in bare.group(1).split())):
        s.fields["name"] = " ".join(w[:1].upper() + w[1:] for w in bare.group(1).split())
    new = [k for k in PII if k in s.fields and before.get(k) != s.fields[k]]
    s.stalled = 0 if new else s.stalled + 1
    had_hint = bool(s.case_hint or s.intent_hint)
    detect_hint(text, s)
    holder = verified_holder(s)
    if holder:
        s.holder_id = holder["party_id"]
        s.phase = "RESOLVE_INTENT"
        s.refusal_count = 0
        first = s.preferred_name or (s.fields.get("name", "").split()[0] if "name" in s.fields else "")
        s.preferred_name_pending = False
        note = f"Thank you{', ' + first if first else ''}, you're verified. "
        if s.case_hint or s.intent_hint:
            return note + resolve(s, text, model, remembered=True)
        return note + "What can I help you with today? For example, a claim's status, a denial, documents, or a payment."
    lead = opening(s, emotion_of(text))
    s.off_topic_count = 0
    refused = [k for k in refused_fields(text) if k not in s.fields]
    if refused:
        s.refused_fields = list(dict.fromkeys([*s.refused_fields, *refused]))
        unwilling = re.search(r"\b(?:won'?t|don'?t want|do not want|refuse|rather not|no way|never|not giving|not sharing)\b", low)
        labels = join_words([FIELD_LABELS[k] for k in refused])
        if len([k for k in PII if k not in s.refused_fields]) < 3:
            if unwilling:
                return transfer(s, lead + "I understand. Without three matching details I can't open the claim in this "
                                "chat, so I've marked this for a human representative who can discuss other ways to verify you.")
            s.refused_fields = [k for k in s.refused_fields if k not in refused]
            return (lead + f"No problem if you don't have your {labels} right now. I still need {needed_details(s)}. "
                    "If you can find one of them, just send it; otherwise say \"representative\" and a person can verify "
                    "you another way.")
        if not unwilling:
            s.refused_fields = [k for k in s.refused_fields if k not in refused]
            return lead + f"No problem if you're not sure of your {labels}. " + ask_for_details(s)
        return lead + f"That's okay, you don't have to share your {labels}. " + ask_for_details(s)
    if REFUSAL_RE.search(low):
        s.refusal_count += 1
        if s.refusal_count >= 3:
            return transfer(s, lead + "I respect that. I can't share claim details without verification, so I've "
                            "marked this for a human representative to discuss safe options with you.")
        so_far = (f"So far I have your {held_details(s)}, so I just need {needed_details(s)}."
                  if s.fields else "I don't have any of those details from this chat yet. " + ask_for_details(s))
        if s.refusal_count == 1:
            return (lead + "I do want to get you an answer. Claim records hold private medical and payment details, so I "
                    "need three matching details before I open one; it protects you if someone else calls about your "
                    "policy. " + so_far + " If you'd prefer, a human representative can help instead.")
        return (lead + "I can't skip this step, but I'm glad to keep it quick. " + so_far +
                " Or just say \"representative\" and I'll hand this to a person.")
    if re.search(r"\bwhat for\b|\bwhy (?:do|does|should|would|must|is|are|the)\b[^.?!]{0,40}(?:verif|identity|details|"
                 r"information|need (?:that|this|it)|ask)|\bwhy verif", low):
        return (lead + "Claim records can contain private health and payment information. I need three matching "
                "identity details before opening one, which keeps anyone else from accessing your claim. You may choose "
                "your full name, date of birth, phone, email, or ID last four digits; I can also route you to a human "
                "representative.")
    if re.search(r"\b(?:can i use|other way to verify|another way|different (?:id|detail)|instead of|don'?t have my)\b", low):
        return (lead + "Yes. Any three matching details from full name, date of birth, phone, email, and ID last four "
                "digits work. You can provide them across messages, and your policy number helps locate the record but "
                "does not count as one of the three.")
    if len(s.fields) >= 3:
        snapshot = json.dumps([sorted(s.fields.items()), s.policy_hint])
        if snapshot != s.failed_snapshot:
            s.failed_snapshot = snapshot
            s.verify_failures += 1
        if s.verify_failures >= MAX_VERIFY_FAILURES:
            return transfer(s, "For your security, I can't keep checking details in this chat after several attempts "
                            "that didn't match. I've marked this for a human representative who can verify you another way.")
        policy = " and policy number" if s.policy_hint else ""
        if not new:
            return (lead + "I still can't match those details to one record. You can correct any one of them (for "
                    "example, \"my email is ...\"), say \"start over\" to clear them, or say \"representative\".")
        if s.verify_failures == 2:
            return (lead + f"That still doesn't match one policyholder record. I have your {held_details(s)}{policy}. "
                    "Please double-check one of them, or say \"representative\" and a person can help.")
        return (lead + f"Thanks. Those details don't match a single policyholder record together, so I can't open a claim "
                f"yet. I have your {held_details(s)}{policy}. If something was mistyped, just send the corrected value "
                "(for example, \"my DOB is ...\"), or I can connect you with a human representative.")
    field_question = re.search(r"\bwhat(?:'s| is| are| does)\s+(?:a |an |the |my )?(last four|last 4|ssn|social|national id|"
                               r"id last four|dob|pii)\b", low)
    if field_question:
        term = field_question.group(1)
        meaning = ("your date of birth" if term == "dob" else "personal details such as your name, date of birth, phone, "
                   "or email" if term == "pii" else "the last four digits of your Social Security number (or national "
                   "ID number)")
        return (lead + f"Good question. That means {meaning}. If you'd rather not share it, your phone number or email "
                "works too. " + (ask_for_details(s) if s.fields else "I need any three of: full name, date of birth, "
                "phone, email, or those last four digits."))
    if emotion_of(text) == "confused" and not s.fields:
        return (lead + "No problem. Before I can look at your claim, I just need to confirm it's really you. The easiest "
                "way is to tell me three things, for example your full name, your date of birth, and your phone "
                "number. You can send them one at a time.")
    parts = [lead.strip()]
    kind = smalltalk(text) or ("greeting" if re.match(r"^(?:hi|hello|hey|good (?:morning|afternoon|evening))\b", low) else "")
    if kind == "greeting":
        parts.append("Hi, thanks for reaching out.")
    elif kind == "how_are_you":
        parts.append("I'm doing well, thanks for asking.")
    if "name" in later:
        parts.append(f"Sure, I'll call you {s.preferred_name}.")
    if "email" in later:
        parts.append("I'll remember that you'd like an email summary at the end.")
    if new:
        parts.append(f"Thanks, I've got your {join_words([FIELD_LABELS[k] for k in new])}.")
    if "full_ssn" in notes:
        parts.append("For your safety, I only need the last four digits of your SSN; please don't share the full number.")
    if INJECTION_RE.search(text):
        parts.append("I can't accept instructions or status changes typed into the chat; verification only happens by "
                     "matching your details.")
    if STAFF_RE.search(text):
        parts.append("I can't accept staff claims or override codes in this customer chat. If you're the policyholder, "
                     "three matching details will do it.")
    if ALREADY_VERIFIED_RE.search(text):
        parts.append("Each chat is verified on its own, so I can't carry over a check from another conversation, but it "
                     "only takes three details.")
    if re.search(r"(?:ssn|social)[^.?!]{0,15}\bPOL[-\s]?\d{4}|\b(?:number )?on my (?:insurance )?card\b", text, re.I):
        parts.append("The number on your insurance card (like POL-9921) is your policy number. The SSN last four means "
                     "the last four digits of your Social Security number.")
    if "partial_phone" in notes:
        parts.append("For phone, I need the full number on your record, not just the last digits.")
    if "name_part" in notes:
        parts.append(f"Could you also share your {'last' if 'first' in s.name_parts else 'first'} name?")
    asking = "?" in text or re.search(r"\btell me\b|\bexplain\b|^(?:why|what|how|when|was|did|is|can|will)\b", low)
    if asking and (local_topics(text) != ["clarify"] or re.search(r"\b(?:was it|did (?:they|you)|is it|yes or no)\b", low)):
        previous = next((t["text"] for t in reversed(s.turns[:-1]) if t["role"] == "assistant"), "")
        if "can't share or confirm" not in previous:
            parts.append("I can't share or confirm any claim details until you're verified.")
        else:
            parts.append("I'll answer that as soon as you're verified.")
    if (s.case_hint or s.intent_hint) and (not s.hint_acknowledged or not had_hint):
        s.hint_acknowledged = True
        parts.append("I've noted what you're calling about, so you won't need to repeat it after verification.")
    if s.policy_hint and (not s.policy_note_given or re.search(r"\b(?:three|3) (?:details|things|pieces)\b|that'?s (?:three|3)", low)):
        s.policy_note_given = True
        parts.append("Your policy number helps me find the record, but it doesn't count toward the three details.")
    if s.fields and s.stalled >= 3:
        parts.append(f"Still {needed_details(s).split(':')[0]} to go ({needed_details(s).split(': any of your ')[1]}), "
                     "or say \"representative\".")
    elif s.fields and s.stalled >= 2:
        parts.append(f"We're almost there: I just need {needed_details(s)}. If you're not sure what's on file, say "
                     "\"representative\" and a person can help.")
    elif not s.fields and s.stalled >= 2:
        parts.append("Whenever you're ready, three details will do it, for example your full name, date of birth, and "
                     "phone number. If you'd rather talk to a person, just say \"representative\".")
    elif not s.fields:
        parts.append("To protect your claim information, I first need to verify your identity. Please share any three "
                     "matching details: full name, date of birth, phone, email, or SSN or national ID last four digits. "
                     "You can send them in any order, across messages if you like.")
    else:
        parts.append(ask_for_details(s))
    return " ".join(p for p in parts if p)


def case_turn(s: Session, text: str, model: ModelClient) -> str:
    low = norm(text)
    if re.search(r"\b(?:at the end|later|when we'?re done|afterwards)\b", low):
        remember_for_later(text, s)
    wants_email = re.search(r"\b(?:email (?:me )?(?:a |the )?summary|send (?:me )?(?:a |the )?summary|email (?:it|that) to me)\b", low)
    if wants_email or is_closing(text):
        s.phase = "POST_PROCESS"
        s.email_offered = True
        if wants_email:
            if requested_other_email(s, text):
                return ("For privacy, I can only send the summary to the email on the verified policyholder's record. "
                        "Would you like me to send it there or skip?")
            s.closed = True
            return "I can email a summary of what we discussed, the claim status, and next steps. " + send_summary(s)
        if s.email_requested:
            return ("Glad I could help. Earlier you asked for an email summary of what we discussed, the claim status, "
                    "and next steps. Shall I send it now to the email on your policy record? You can say \"send it\" "
                    "or \"skip\".")
        return ("Glad I could help. Before we finish, would you like an email summary of what we discussed, the claim "
                "status, and next steps? It goes to the email on your policy record. You can say \"send it\" or \"skip\".")
    own = [c for c in CLAIMS if c["party_id"] == s.holder_id]
    if re.search(r"\b(?:other|all|list|any more|more)\b.*\bclaims\b|\bclaims do i have\b", low):
        s.phase = "RESOLVE_INTENT"
        s.candidate_ids = [c["case_id"] for c in own]
        meaning = ("Closed means a claim's review is finished; open means it's still in progress; denied means it was "
                   "reviewed and not paid as submitted. " if re.search(r"good or bad|mean", low) else "")
        return meaning + f"Your policy has {len(own)} claims: " + claim_list(own) + ". Which one would you like to discuss?"
    ids = re.findall(r"\bCL[-\s]?(\d{4})\b", text, re.I)
    requested_id = "CL-" + ids[-1] if ids else ""
    if requested_id and requested_id != s.case_id:
        target = next((c for c in own if c["case_id"] == requested_id), None)
        if not target:
            return ("I can't access that claim under the verified policyholder's record. A representative can check "
                    "authorization or help locate the right claim.")
        select_case(s, target)
        answer, _ = case_response(s, target, text, model)
        return "I found the claim you asked about. " + answer
    current = next(c for c in CLAIMS if c["case_id"] == s.case_id)
    clues = claim_clues(without_identity(text))
    if clues and (re.search(r"\bone\b|\bwhat about\b|\bhow about\b|\band the\b|\bswitch\b|\bother\b|\binstead\b", low)
                  or ("type" in clues and re.search(r"\bclaim\b", low))):
        s.candidate_ids = []
        target, options = choose_claim(text, s, model)
        if target and target["case_id"] != s.case_id:
            select_case(s, target)
            if local_topics(text) == ["clarify"]:
                return (f"Switching to {target['case_id']}, your {target['case_type']} claim filed "
                        f"{fmt_date(target['created_at'])}. {status_sentence(target)} What would you like to know about it?")
            answer, _ = case_response(s, target, text, model)
            return f"Switching to {target['case_id']}. " + answer
        if not target and not options:
            return "I couldn't find a claim like that on your policy. Your claims are: " + claim_list(own) + "."
        if not target and s.case_id not in [c["case_id"] for c in options]:
            s.phase = "RESOLVE_INTENT"
            s.candidate_ids = [c["case_id"] for c in options]
            return "Which one do you mean? I can see: " + claim_list(options) + "."
    if smalltalk(text) == "ack":
        return f"Is there anything else about {s.case_id} I can help with, or shall we wrap up?"
    if scope_state(text) == "unsure_question" and not model.enabled:
        return off_topic_reply(s)
    if OFF_TOPIC_RE.search(low) and local_topics(text) == ["clarify"]:
        # An unrelated request mixed with claim feelings: decline that part without counting it as off-topic.
        return (opening(s, emotion_of(text)) + f"That part is outside what I can do here, but I'm here to help with "
                f"the claim itself. For {s.case_id}, I can explain what's needed next, how to submit documents, "
                "timing, or payment, or connect you with a representative. What would help most?")
    answer, unrelated = case_response(s, current, text, model)
    if unrelated:
        return off_topic_reply(s)
    s.off_topic_count = 0
    return answer


def _respond(s: Session, text: str, model: ModelClient) -> str:
    low = norm(text)
    if s.human_transfer:
        previous = next((t["text"] for t in reversed(s.turns[:-1]) if t["role"] == "assistant"), "")
        if "I've marked this conversation" in previous or "already marked" in previous:
            return (empathy(text) + "I'm sorry I can't do more in this chat. This is already marked for a human "
                    "representative, who can pick it up with you; you can also start a new conversation at any time.")
        return empathy(text) + ("I've marked this conversation for a human representative. In this demo, please "
                                "contact the claims support team directly, or start a new conversation.")
    if HUMAN_RE.search(low):
        return transfer(s, empathy(text) + "Of course. I've marked this for a human representative. In this demo, "
                        "please contact the claims support team directly.")
    holder = next((h for h in HOLDERS if h["party_id"] == s.holder_id), None)
    identity_denial = bool(holder and (
        re.search(r"\b(?:i am|i'm)\s+(?:not the (?:policyholder|claimant)|a different person)\b", low)
        or re.search(r"\b(?:i am|i'm)\s+not\s+" + re.escape(holder["name"].lower()) + r"\b", low)
    ))
    if holder and s.rep_name:
        identity_denial = False
    typist_changed = bool(re.search(
        r"\bthis is (?:her|his|their)\b|handed me the phone|\btyping for\b|\bon (?:her|his|their) behalf\b|"
        r"\b(?:i am|i'm) (?:her|his|their) (?:son|daughter|spouse|wife|husband|caregiver)\b|"
        r"\b(?:i am|i'm) [a-z ]{2,40}'s (?:son|daughter|spouse|wife|husband|caregiver)\b", low))
    asks_for_other = holder and ((about_other_person(text) and not s.rep_name) or other_holder_named(text, holder)
                                 or re.search(r"\b(?:helping|for) (?:my )?(?:friend|neighbor|coworker)\b", low)
                                 or (not s.rep_name and re.search(r"\b(?:she|he|they)(?:'s| is| are) (?:right )?(?:here|next to me|"
                                                                  r"with me)\b|\bsays it'?s (?:fine|ok|okay)\b", low)))
    if holder and asks_for_other and not typist_changed and not identity_denial:
        return ("I can only discuss claims on your own policy record, so I can't share or look up someone else's claim, "
                "even with their permission in this chat. They can contact us and verify themselves, or a representative "
                "can check authorization. Is there anything else about your own claims I can help with?")
    if holder and ((typist_changed and not s.rep_name) or identity_denial
                   or different_identity(text, holder, (s.rep_name,))):
        s.human_transfer = True
        s.phase = "VERIFY_ID"
        s.holder_id = ""
        s.case_id = ""
        s.discussed_case_ids.clear()
        s.candidate_ids.clear()
        s.topics_discussed.clear()
        s.fields.clear()
        s.policy_hint = s.case_hint = s.intent_hint = s.pending_question = ""
        s.preferred_name = ""
        s.preferred_name_pending = False
        s.rep_name = s.rep_party = s.consent_status = ""
        s.email_preview = ""
        s.turns.clear()
        return ("Thanks for clarifying. I can't continue discussing the verified policyholder's claim "
                "with a different caller. A representative can check your authorization safely.")
    if holder and ((about_other_person(text) and not s.rep_name) or other_holder_named(text, holder)):
        return ("I can only discuss claims on your own policy record, so I can't share or look up someone else's claim. "
                "The other policyholder can contact us directly, or a representative can check authorization. "
                "Is there anything else about your own claims I can help with?")
    if s.holder_id:
        name = requested_name(text)
        if name:
            s.preferred_name = name
            s.preferred_name_pending = True
            s.off_topic_count = 0
            return f"Of course, I'll call you {name}. What would you like to know about the claim?"
    if sum(ord(c) > 127 for c in text) > len(text) / 2:
        note = "I can only chat in English here, but I'm glad to help. "
        return note + (verify_turn(s, text, model) if s.phase == "VERIFY_ID" else
                       "Please describe your question in English, or say \"representative\" for a person.")
    state = scope_state(text)
    if state == "unrelated":
        return off_topic_reply(s)
    if state.startswith("unsure") and s.phase in ("VERIFY_ID", "RESOLVE_INTENT"):
        label = model.classify_scope(model_safe_text(text, session=s)) if model.enabled else ""
        if label == "unrelated" or (not label and state == "unsure_question"):
            return off_topic_reply(s)
    if s.phase == "VERIFY_ID":
        return verify_turn(s, text, model)
    if s.phase == "RESOLVE_INTENT":
        if s.closed:
            return "This conversation is complete. You can start a new conversation for another claim."
        s.off_topic_count = 0
        lead = opening(s, emotion_of(text))
        own = [c for c in CLAIMS if c["party_id"] == s.holder_id]
        if not own:
            previous = next((t["text"] for t in reversed(s.turns[:-1]) if t["role"] == "assistant"), "")
            if previous.startswith("I don't see any claims"):
                return (lead + "Since nothing is on file, the best next step is a human representative who can check "
                        "whether your claim was received. Just say \"representative\" and I'll hand this over.")
            return lead + NO_CLAIMS
        if re.search(r"\b(?:all|list|which|what)\b.*\bclaims\b|\bclaims do i have\b", low):
            s.candidate_ids = [c["case_id"] for c in own]
            return lead + f"Your policy has {len(own)} claims: " + claim_list(own) + ". Which one would you like to discuss?"
        if smalltalk(text):
            return "Which claim can I help you with? You can describe it, for example \"my denied claim\" or \"the auto claim\"."
        if (is_closing(text) or re.search(r"\b(?:nothing|never ?mind)\b.*\b(?:bye|that'?s all|thanks)\b", low)) \
                and s.discussed_case_ids:
            s.phase, s.email_offered = "POST_PROCESS", True
            return ("Glad I could help. Before we finish, would you like an email summary of what we discussed, the claim "
                    "status, and next steps? It goes to the email on your policy record. You can say \"send it\" or \"skip\".")
        if is_closing(text) or re.search(r"\b(?:nothing|never ?mind)\b.*\b(?:bye|that'?s all|thanks)\b", low):
            # No claim was opened, so there is nothing to summarize and no email to offer.
            s.closed = True
            return ("No problem. Since we didn't open a claim today, there's nothing to summarize. "
                    "Thank you for contacting claims support.")
        return lead + resolve(s, text, model)
    if s.phase == "PROCESS_CASE":
        return case_turn(s, text, model)
    if s.phase == "POST_PROCESS":
        if s.closed:
            return "This conversation is complete. You can start a new conversation for another claim."
        if re.search(r"\bwhy\b", low) and re.search(r"\b(?:permission|consent|ask(?:ing)?|agree|email|summary|send)\b", low):
            return ("The summary includes your claim status, the denial reason, and health-related next steps, so I only "
                    "send it when you say yes, and only to the email on your policy record. That keeps private "
                    "information from going anywhere you didn't choose. Would you like me to send it, or skip it?")
        if re.search(r"\b(?:go|goes|going|sent|send|copy)\b[^.?!]{0,20}\b(?:doctor|provider|clinic|hospital|anyone else|"
                     r"someone else|else)\b|\bwho (?:gets|receives|sees)\b", low) and "?" in text:
            return ("No, it goes only to the email on your policy record; nobody else, including your doctor, receives "
                    "it. Would you like me to send it, or skip it?")
        if requested_other_email(s, text):
            return ("For privacy, I can only send the summary to the email on the verified policyholder's record. "
                    "Would you like me to send it there or skip?")
        question = local_topics(text) != ["clarify"] or re.search(r"\bCL[-\s]?\d{4}\b|\bclaims?\b", text, re.I)
        decision = "" if question else consent_decision(text)
        if decision == "skip":
            allow(s, "skip_summary")
            s.closed = True
            return "Understood. I won't send an email summary. Thank you for contacting claims support."
        if decision == "send":
            s.closed = True
            return send_summary(s) + " Thank you for contacting claims support."
        if question or "?" in text:
            s.phase = "PROCESS_CASE"
            return case_turn(s, text, model)
        return "Would you like me to send the email summary, or skip it?"
    return "I can help with your insurance claim."
