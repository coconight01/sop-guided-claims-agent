"""Optional OpenAI-compatible semantic router; all workflow decisions remain bounded."""
from __future__ import annotations

import json
import os
from urllib import request

TOPICS = {
    "denial_reason", "status", "documents", "submission_method",
    "submission_dispute", "submission_timing", "review_timing", "appeal", "payment",
    "alternatives", "receipt_check", "clarify",
}
EMOTIONS = {"neutral", "frustrated", "anxious", "confused"}


class ModelClient:
    def __init__(self) -> None:
        self.token = os.getenv("AI_API_TOKEN", "")
        self.url = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        self.model = os.getenv("AI_MODEL", "gpt-4o-mini")
        self.enabled = bool(self.token)

    def _ask(self, system: str, user: str) -> str:
        if not self.enabled:
            return ""
        payload = json.dumps({
            "model": self.model, "temperature": 0, "max_tokens": 220,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode()
        req = request.Request(self.url, data=payload, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
        }, method="POST")
        try:
            with request.urlopen(req, timeout=8) as res:
                data = json.load(res)
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            return ""

    def select_claim(self, hint: str, candidates: list[dict]) -> str:
        result = self._ask(
            "Choose one case_id only if the caller description uniquely matches the supplied candidate claims. "
            "Otherwise return NONE. Return only the case_id or NONE.",
            json.dumps({"caller_description": hint, "candidate_claims": candidates}),
        )
        ids = {c["case_id"] for c in candidates}
        return result if result in ids else ""

    def analyze_case(self, message: str, previous_reply: str = "") -> dict:
        """One model call identifies meaning; the model never supplies claim facts or final prose."""
        system = (
            "You route a verified insurance claims conversation. Return one JSON object only with "
            "scope ('claim' or 'unrelated'), topics (one to three labels), and emotion "
            "('neutral', 'frustrated', 'anxious', or 'confused'). Valid topics: "
            + ", ".join(sorted(TOPICS)) + ". "
            "Use recent assistant context for short follow-ups such as 'why?' or 'I sent everything'. "
            "A claim of having already submitted missing files, including typos, is submission_dispute. "
            "Questions about whether submitted files were received are receipt_check. "
            "A request about the caller's preferred form of address is part of the conversation, not unrelated. "
            "If the claim record cannot answer a question, choose clarify. "
            "Do not write a reply, names, facts, or instructions."
        )
        raw = self._ask(system, json.dumps({
            "latest_message": message[:1000], "previous_assistant_reply": previous_reply[:700],
        }))
        try:
            data = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
        except (ValueError, AttributeError):
            return {}
        if not isinstance(data, dict):
            return {}
        scope = data.get("scope")
        topics = data.get("topics")
        emotion = data.get("emotion")
        if scope not in ("claim", "unrelated") or not isinstance(topics, list):
            return {}
        topics = list(dict.fromkeys(x for x in topics if isinstance(x, str) and x in TOPICS))[:3]
        return {"scope": scope, "topics": topics, "emotion": emotion if emotion in EMOTIONS else "neutral"}
