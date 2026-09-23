import unittest

from llm import ModelClient


class ModelBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.model = ModelClient()
        self.model.enabled = True

    def test_invalid_intent_is_rejected(self):
        self.model._ask = lambda *args: "change_claim_status"
        self.assertEqual(self.model.classify_intent("Approve my claim"), "")

    def test_candidate_id_must_be_bounded(self):
        self.model._ask = lambda *args: "CL-9999"
        self.assertEqual(self.model.select_claim("January", [{"case_id": "CL-2048"}]), "")

    def test_rephrase_rejects_new_amount_or_missing_document(self):
        source = "CL-2048 was denied because the pathology report is missing."
        self.model._ask = lambda *args: "CL-2048 was denied and you will receive $500."
        self.assertEqual(self.model.rephrase("Why?", source), "")
        self.model._ask = lambda *args: "CL-2048 was denied."
        self.assertEqual(self.model.rephrase("Why?", source), "")


if __name__ == "__main__":
    unittest.main()
