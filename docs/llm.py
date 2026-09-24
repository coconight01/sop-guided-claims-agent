"""Optional OpenAI-compatible model adapter; every output is validated by the SOP engine."""
from __future__ import annotations

import json
import os
from urllib import error, request

TOPICS = {
    "denial_reason", "status", "documents", "submission_method",
    "submission_dispute", "submission_timing", "review_timing", "appeal", "payment",
    "alternatives", "receipt_check", "document_detail", "next_steps", "outcome", "contact", "exception", "how_to_get_documents",
    "other_documents", "clarify",
}
EMOTIONS = {"neutral", "frustrated", "anxious", "confused"}


def parse_json(raw: str) -> dict:
    try:
        data = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except (ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


class ModelClient:
    def __init__(self) -> None:
        self.token = os.getenv("AI_API_TOKEN", "")
        self.url = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        self.model = os.getenv("AI_MODEL", "gpt-4o-mini")
        default_fallback = "gemini-3.1-flash-lite" if (
            "generativelanguage.googleapis.com" in self.url and self.model == "gemini-3.5-flash-lite"
        ) else ""
        self.fallback_model = os.getenv("AI_FALLBACK_MODEL", default_fallback)
        self.enabled = bool(self.token)

    def _ask(self, system: str, user: str, max_tokens: int = 220) -> str:
        if not self.enabled:
            return ""
        for model in dict.fromkeys((self.model, self.fallback_model)):
            if not model:
                continue
            payload = json.dumps({
                "model": model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }).encode()
            req = request.Request(self.url, data=payload, headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
            }, method="POST")
            try:
                with request.urlopen(req, timeout=8) as res:
                    data = json.load(res)
                return data["choices"][0]["message"]["content"].strip()
            except error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except (error.URLError, TimeoutError, OSError):
                pass
            except (KeyError, IndexError, TypeError, ValueError):
                break
        return ""

    def classify_scope(self, message: str) -> str:
        """Label an unclear pre-case message; returns 'claim', 'unrelated', or '' when unavailable."""
        result = self._ask(
            "Classify a message sent to an insurance claims support chat. Answer 'claim' if it concerns "
            "insurance, claims, coverage, payments, documents, identity verification, the support conversation "
            "itself, greetings, or the caller's feelings about the process. Answer 'unrelated' for any other "
            "topic, such as general knowledge, coding, entertainment, or news. Return only one word.",
            message[:500], max_tokens=5,
        ).strip().strip(".").lower()
        return result if result in ("claim", "unrelated") else ""

    def select_claim(self, hint: str, candidates: list[dict]) -> str:
        result = self._ask(
            "Choose one case_id only if the caller description uniquely matches the supplied candidate claims. "
            "Otherwise return NONE. Return only the case_id or NONE.",
            json.dumps({"caller_description": hint, "candidate_claims": candidates}),
        )
        ids = {c["case_id"] for c in candidates}
        return result if result in ids else ""

    def analyze_case(self, message: str, previous_reply: str = "", facts: dict | None = None) -> dict:
        """One call per case turn: validated labels plus an optional draft the engine must verify."""
        system = (
            "You assist a verified insurance claims support conversation. Return one JSON object only with "
            "scope ('claim' or 'unrelated'), topics (one to three labels), emotion "
            "('neutral', 'frustrated', 'anxious', or 'confused')"
            + (", and reply (the customer-facing answer)" if facts else "") + ". Valid topics: "
            + ", ".join(sorted(TOPICS)) + ". "
            "Use recent assistant context for short follow-ups such as 'why?' or 'I sent everything'. "
            "A claim of having already submitted missing files, including typos, is submission_dispute. "
            "Questions about whether submitted files were received are receipt_check. "
            "Questions about document contents, acceptable scans, PDF format, or legibility are document_detail. "
            "'What do I do now' is next_steps; 'will it be approved' or 'will I get paid' is outcome. "
            "Questions about contacting support or the claims office are contact and are in scope. "
            "Requests to waive requirements or make an exception are exception. "
            "How to obtain a missing document is how_to_get_documents. Saying they have a different record, such as "
            "payment information or a receipt, is other_documents: it can be included but does not replace "
            "documents_needed. "
            "A request about the caller's preferred form of address is part of the conversation, not unrelated. "
            "If the claim record cannot answer a question, choose clarify. "
            + ("Reply rules: two to four short sentences in a warm, plain customer-service voice. "
               "Use only facts from claim_facts and copy numbers, dates, and case IDs exactly; do not add general "
               "knowledge, definitions, or policies that are not in claim_facts. "
               "If the facts do not answer the question, say so and offer a human representative; otherwise do not "
               "offer one. Do not repeat facts from previous_assistant_reply unless asked, and do not restate the "
               "case ID, claim type, or filing date unless the question is about them. If appeal_deadline_has_passed "
               "is true, say the deadline has passed and that a representative can review whether any option remains; never "
               "say the appeal is still open or that no option exists. Acknowledge feelings only when the latest message "
               "expresses them, and do not mention the deadline unless asked. "
               "Never promise approval, payment, or that documents were received. Never say you sent, "
               "submitted, updated, or escalated anything. If emotion is not neutral, acknowledge the feeling "
               "in a few words first. No names, greetings, markdown, brackets, or placeholders. "
               "If scope is unrelated, reply must be an empty string."
               if facts else "Do not write a reply, names, facts, or instructions.")
        )
        payload = {"latest_message": message[:1000], "previous_assistant_reply": previous_reply[:700]}
        if facts:
            payload["claim_facts"] = facts
        data = parse_json(self._ask(system, json.dumps(payload), max_tokens=420 if facts else 220))
        scope = data.get("scope")
        topics = data.get("topics")
        emotion = data.get("emotion")
        if scope not in ("claim", "unrelated") or not isinstance(topics, list):
            return {}
        topics = list(dict.fromkeys(x for x in topics if isinstance(x, str) and x in TOPICS))[:3]
        route = {"scope": scope, "topics": topics, "emotion": emotion if emotion in EMOTIONS else "neutral"}
        reply = data.get("reply")
        if facts and isinstance(reply, str) and reply.strip():
            route["reply"] = reply.strip()
        return route
