import unittest
import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

from llm import ModelClient


class ModelBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.model = ModelClient()
        self.model.enabled = True

    def test_claim_choice_is_bounded(self):
        self.model._ask = lambda *args: "CL-9999"
        self.assertEqual(self.model.select_claim("January", [{"case_id": "CL-2048"}]), "")

    def test_structured_route_rejects_unrecognized_values(self):
        self.model._ask = lambda *args: '{"scope":"claim","topics":["approve_claim","denial_reason"],"emotion":"angry"}'
        self.assertEqual(
            self.model.analyze_case("Why was it denied?"),
            {"scope": "claim", "topics": ["denial_reason"], "emotion": "neutral"},
        )

    def test_invalid_route_falls_back(self):
        self.model._ask = lambda *args: "I think the claim should be approved"
        self.assertEqual(self.model.analyze_case("Why?"), {})

    def test_gemini_default_backup_does_not_need_new_render_setting(self):
        with patch.dict("os.environ", {
            "AI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
            "AI_MODEL": "gemini-3.5-flash-lite",
        }, clear=True):
            self.assertEqual(ModelClient().fallback_model, "gemini-3.1-flash-lite")

    def test_rate_limit_tries_backup_model(self):
        self.model.token = "test-token"
        self.model.model = "gemini-3.5-flash-lite"
        self.model.fallback_model = "gemini-3.1-flash-lite"
        called = []

        def fake_open(req, timeout):
            model = json.loads(req.data)["model"]
            called.append(model)
            if len(called) == 1:
                raise HTTPError(req.full_url, 429, "rate limit", {}, None)
            return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')

        with patch("llm.request.urlopen", side_effect=fake_open):
            self.assertEqual(self.model._ask("route", "question"), "ok")
        self.assertEqual(called, [self.model.model, self.model.fallback_model])

    def test_auth_error_does_not_try_backup(self):
        self.model.token = "test-token"
        self.model.fallback_model = "gemini-3.1-flash-lite"
        called = []

        def fake_open(req, timeout):
            called.append(json.loads(req.data)["model"])
            raise HTTPError(req.full_url, 401, "unauthorized", {}, None)

        with patch("llm.request.urlopen", side_effect=fake_open):
            self.assertEqual(self.model._ask("route", "question"), "")
        self.assertEqual(called, [self.model.model])


if __name__ == "__main__":
    unittest.main()
