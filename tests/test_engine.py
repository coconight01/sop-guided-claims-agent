import unittest

from engine import Session, respond, summary, model_safe_text
from llm import ModelClient


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.model = ModelClient()
        self.model.enabled = False

    def say(self, text):
        return respond(self.session, text, self.model)

    def test_external_model_text_omits_identity_fields(self):
        text = "I'm Margaret Chen, policy POL-9921, DOB 1985-03-15, SSN last four 4472, email margaret.chen@example.com. My denied healthcare claim was in January."
        safe = model_safe_text(text)
        for private in ("Margaret Chen", "POL-9921", "1985-03-15", "4472", "margaret.chen@example.com"):
            self.assertNotIn(private, safe)
        self.assertIn("denied healthcare claim", safe)

    def test_full_demo_and_consent(self):
        first = self.say("I'm the policyholder Margaret Chen, policy POL-9921. My denied healthcare claim from January: DOB 1985-03-15, SSN last four 4472.")
        self.assertEqual(self.session.phase, "PROCESS_CASE")
        self.assertEqual(self.session.case_id, "CL-2048")
        self.assertIn("pathology report", first)
        self.assertIn("denied", self.say("Why was it denied?"))
        self.assertIn("email summary", self.say("That's all, thank you"))
        self.assertEqual(self.session.phase, "POST_PROCESS")
        self.assertIn("Demo outbox", self.say("send it"))
        self.assertTrue(self.session.closed)
        self.assertIn("pathology report", self.session.email_preview)
        self.assertNotIn("Demo outbox", self.say("send it"))
        self.assertIn("current status: denied", summary(self.session))
        self.assertIn("pathology report", summary(self.session))

    def test_partial_answers_and_early_memory(self):
        first = self.say("I need help with my denied healthcare claim from January. I'm Margaret Chen.")
        self.assertEqual(self.session.phase, "VERIFY_ID")
        self.assertNotIn("CL-2048", first)
        self.assertTrue(self.session.case_hint)
        self.say("DOB 1985-03-15")
        self.assertEqual(self.session.phase, "VERIFY_ID")
        final = self.say("SSN last four 4472")
        self.assertEqual(self.session.phase, "PROCESS_CASE")
        self.assertIn("CL-2048", final)

    def test_early_intent_is_acknowledged(self):
        answer = self.say("I am calling about a denied healthcare claim in January.")
        self.assertIn("noted", answer)
        self.assertNotIn("CL-2048", answer)
        self.assertEqual(self.session.phase, "VERIFY_ID")

    def test_unknown_declared_name_is_captured_but_not_verified(self):
        answer = self.say("My name is John Doe and I need help with a claim.")
        self.assertIn("1 identity detail", answer)
        self.assertEqual(self.session.fields["name"], "John Doe")
        self.assertFalse(self.session.holder_id)

    def test_verification_reason_answer_is_specific(self):
        answer = self.say("Why do you need to verify my identity?")
        self.assertIn("private health and payment information", answer)
        self.assertEqual(self.session.phase, "VERIFY_ID")

    def test_policy_number_is_not_pii(self):
        answer = self.say("Margaret Chen, POL-9921, DOB 1985-03-15. Tell me about CL-2048")
        self.assertEqual(self.session.phase, "VERIFY_ID")
        self.assertNotIn("pathology", answer)
        self.assertEqual(len(self.session.fields), 2)

    def test_wrong_field_blocks(self):
        answer = self.say("Margaret Chen, DOB 1985-03-15, SSN last four 9999. Why was CL-2048 denied?")
        self.assertEqual(self.session.phase, "VERIFY_ID")
        self.assertNotIn("pathology", answer)

    def test_alternate_fields_and_alias(self):
        self.say("I'm Yaven Li. My email is yawen.li@example.com and my phone is +16505212830. What is my claim status?")
        self.assertTrue(self.session.holder_id == "P13")
        self.assertEqual(self.session.phase, "RESOLVE_INTENT")

    def test_refusal_and_empathy_keep_gate(self):
        answer = self.say("I already told you who I am. This is ridiculous. Just tell me why my claim was denied.")
        self.assertIn("frustrating", answer)
        self.assertIn("three matching", answer)
        self.assertEqual(self.session.phase, "VERIFY_ID")
        self.assertNotIn("pathology", answer)
        self.say("I refuse")
        self.say("I won't verify")
        self.assertTrue(self.session.human_transfer)

    def test_out_of_scope_escalates(self):
        for _ in range(3):
            answer = self.say("What is RL?")
        self.assertTrue(self.session.human_transfer)
        self.assertIn("human representative", answer)
        self.assertEqual(self.session.phase, "VERIFY_ID")
        self.assertTrue(__import__("engine").is_off_topic("How do I bake bread?"))

    def test_representative_requires_human_authorization(self):
        answer = self.say("I'm David Chen, Margaret Chen's son. My mother's DOB is 1985-03-15, SSN last four 4472.")
        self.assertTrue(self.session.human_transfer)
        self.assertFalse(self.session.holder_id)
        self.assertNotIn("pathology", answer)

    def test_post_process_skip(self):
        self.say("Margaret Chen DOB 1985-03-15, SSN last four 4472. Denied healthcare January claim.")
        self.say("That's all")
        self.assertIn("won’t send", self.say("no"))
        self.assertEqual(self.session.email_result, "")

    def test_foreign_claim_not_disclosed(self):
        self.say("Margaret Chen DOB 1985-03-15, SSN last four 4472")
        answer = self.say("Tell me about CL-3001")
        self.assertNotIn("diagnosis report", answer)
        self.assertEqual(self.session.phase, "RESOLVE_INTENT")

    def test_document_followup_uses_fixture_guidance(self):
        self.say("Margaret Chen DOB 1985-03-15, SSN last four 4472. Denied healthcare January claim.")
        self.assertIn("within a week", self.say("How soon do I need to submit the documents?"))
        self.assertIn("member portal", self.say("Where do I upload the pathology report?"))
        self.assertIn("replacement copy", self.say("I don't have the report. Is there an alternative?"))


if __name__ == "__main__":
    unittest.main()
