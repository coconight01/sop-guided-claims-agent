import unittest

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


if __name__ == "__main__":
    unittest.main()
