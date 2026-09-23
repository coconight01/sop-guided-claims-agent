"""Optional OpenAI-compatible chat API. All model decisions are bounded by code."""
from __future__ import annotations

import json
import os
import re
from urllib import request

INTENTS = {"denial_question", "document_submission", "status_inquiry", "next_steps", "general_claim_question"}


class ModelClient:
    def __init__(self) -> None:
        self.token = os.getenv("AI_API_TOKEN", "")
        self.url = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        self.model = os.getenv("AI_MODEL", "gpt-4o-mini")
        self.enabled = bool(self.token)

    def _ask(self, system: str, user: str) -> str:
        if not self.enabled:
            return ""
        payload = json.dumps({"model": self.model, "temperature": 0, "max_tokens": 250,
                              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        req = request.Request(self.url, data=payload, headers={"Authorization": "Bearer " + self.token,
                             "Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=8) as res:
                data = json.load(res)
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            return ""

    def classify_intent(self, text: str) -> str:
        result = self._ask("Classify an insurance customer service request. Return exactly one label from: " +
                           ", ".join(sorted(INTENTS)) + ". No explanation.", text)
        return result if result in INTENTS else ""

    def select_claim(self, hint: str, candidates: list[dict]) -> str:
        result = self._ask("Select one claim ID only if the caller's description unambiguously matches it. "
                           "Otherwise return NONE. Never invent an ID.",
                           json.dumps({"caller_description": hint, "candidate_claims": candidates}))
        ids = {c["case_id"] for c in candidates}
        return result if result in ids else ""

    def rephrase(self, question: str, grounded_answer: str) -> str:
        system = ("You are a concise, empathetic insurance claims representative. Rewrite the supplied answer naturally "
                  "for the customer's question. The supplied answer is the complete and only source of facts. "
                  "Do not add a promise, instruction, deadline, policy detail, dollar amount, claim ID, or outcome. "
                  "Do not answer outside insurance customer service. Return only the rewritten answer.")
        draft = self._ask(system, json.dumps({"question": question, "grounded_answer": grounded_answer}))
        if not draft or len(draft) > 1300:
            return ""
        # Hard check machine-checkable facts. Unsupported prose is limited by the narrow prompt.
        facts = re.findall(r"CL-\d+|\d{4}-\d\d-\d\d|\$\d+(?:\.\d+)?", draft, re.I)
        if any(f.lower() not in grounded_answer.lower() for f in facts):
            return ""
        required = re.findall(r"CL-\d+|\d{4}-\d\d-\d\d|\$\d+(?:\.\d+)?|pathology report|office note|diagnosis report", grounded_answer, re.I)
        if any(f.lower() not in draft.lower() for f in required):
            return ""
        if re.search(r"\b(?:guarantee|approved now|will be approved|i submitted|i filed|i changed your claim)\b", draft, re.I):
            return ""
        return draft
