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

    def test_open_ended_verification_question_is_in_scope(self):
        answer = self.say("What can I do?")
        self.assertIn("matching details", answer)
        self.assertEqual(self.session.off_topic_count, 0)

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

    def test_claim_date_and_third_party_name_are_not_identity_fields(self):
        self.say("I'm calling about Margaret Chen's claim filed 1985-03-15. My phone is 650-521-2830.")
        self.assertNotIn("name", self.session.fields)
        self.assertNotIn("dob", self.session.fields)

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

    def test_preferred_name_and_submission_dispute_keep_claim_context(self):
        self.say("Margaret Chen DOB 1985-03-15, SSN last four 4472. Denied healthcare January claim.")
        reply = self.say("i am yuhan! call my name!")
        self.assertIn("Yuhan", reply)
        self.assertEqual(self.session.case_id, "CL-2048")
        self.assertEqual(self.session.phase, "PROCESS_CASE")
        reply = self.say("why!!!!!!! i summited all i have!")
        self.assertIn("frustrating", reply)
        self.assertIn("can't confirm", reply)
        self.assertNotIn("claim-related question", reply)
        self.assertEqual(self.session.off_topic_count, 0)

    def test_preferred_name_is_used_in_next_case_reply(self):
        self.say("Margaret Chen DOB 1985-03-15, SSN last four 4472. Denied healthcare January claim.")
        self.assertIn("Alex", self.say("Please call me Alex"))
        reply = self.say("I submitted everything. Why is it still denied?")
        self.assertIn("Alex", reply)
        self.assertIn("can't confirm", reply)
        self.assertEqual(self.session.phase, "PROCESS_CASE")

    def test_model_routes_once_per_case_turn_and_never_before_verification(self):
        calls = []
        self.model.enabled = True
        self.model._ask = lambda system, user: calls.append((system, user)) or (
            '{"scope":"claim","topics":["denial_reason"],"emotion":"neutral"}'
        )
        self.say("I'm Margaret Chen and calling about a denied healthcare claim in January.")
        self.assertEqual(len(calls), 0)
        self.say("DOB 1985-03-15, SSN last four 4472")
        self.assertEqual(len(calls), 1)
        self.say("Call me Yuhan")
        self.assertEqual(len(calls), 1)
        self.say("why!!!! I summited everything!")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("Yuhan", calls[-1][1])
        self.say("That's all")
        self.assertEqual(len(calls), 2)

    def test_email_requires_clear_consent(self):
        self.say("Margaret Chen DOB 1985-03-15, SSN last four 4472. Denied healthcare January claim.")
        self.say("That's all")
        self.assertIn("send the email summary, or skip", self.say("email"))
        self.assertFalse(self.session.closed)

    def test_document_followup_uses_fixture_guidance(self):
        self.say("Margaret Chen DOB 1985-03-15, SSN last four 4472. Denied healthcare January claim.")
        self.assertIn("within a week", self.say("How soon do I need to submit the documents?"))
        self.assertIn("member portal", self.say("Where do I upload the pathology report?"))
        self.assertIn("replacement copy", self.say("I don't have the report. Is there an alternative?"))


if __name__ == "__main__":
    unittest.main()
