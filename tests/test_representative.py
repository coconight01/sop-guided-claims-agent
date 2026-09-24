"""Authorized representative flow: listed representative + policyholder's 3 PII + policyholder consent.

Uses representatives.json (who may act for whom) and consent_scenarios.json (the simulated consent record).
"""
import json
import unittest

from engine import Session, respond, summary
from llm import ModelClient

DAVID = ("I'm David Chen, Margaret Chen's son. I'm calling about my mother's denied claim from January. "
         "Her DOB is 1985-03-15, SSN last four 4472.")


class RepresentativeTests(unittest.TestCase):
    def setUp(self):
        self.model = ModelClient()
        self.model.enabled = False

    def chat(self, scenario="default"):
        session = Session()
        session.consent_scenario = scenario
        return session, lambda text: respond(session, text, self.model)

    def test_default_scenario_opens_claim_only_after_consent(self):
        s, say = self.chat()
        first = say(DAVID)
        self.assertEqual(s.consent_status, "pending")
        self.assertEqual(s.phase, "VERIFY_ID")
        self.assertIsNone(s.public()["claim"])
        self.assertNotIn("pathology", first)
        self.assertIn("consent", first)
        second = say("any update?")
        self.assertEqual(s.consent_status, "approved")
        self.assertEqual(s.case_id, "CL-2048")
        self.assertIn("her healthcare claim", second)
        self.assertIn("pathology report", second)
        self.assertEqual(s.public()["representative"], "David Chen")

    def test_timeout_scenario_never_discloses_and_hands_off(self):
        s, say = self.chat("timeout")
        say(DAVID)
        for text in ("any update?", "hello?", "why do I need her consent?", "still there?"):
            answer = say(text)
            self.assertNotIn("pathology", answer)
            self.assertFalse(s.holder_id)
        self.assertEqual(s.consent_status, "timeout")
        self.assertTrue(s.human_transfer)

    def test_consent_reason_is_explained_while_pending(self):
        s, say = self.chat("timeout")
        say(DAVID)
        answer = say("why do I need her consent?")
        self.assertIn("her own approval", answer)

    def test_name_first_then_policyholder_details(self):
        s, say = self.chat()
        self.assertIn("full name", say("I'm calling for my mom, Margaret Chen"))
        self.assertIn("listed as an authorized representative", say("David Chen"))
        say("her DOB is 1985-03-15")
        self.assertEqual(s.consent_status, "")
        say("last four 4472")
        self.assertEqual(s.consent_status, "pending")
        self.assertNotIn("name", [k for k in s.fields if s.fields[k] == "David Chen"])

    def test_representatives_own_name_is_not_a_policyholder_field(self):
        s, say = self.chat()
        say("I'm David Chen, Margaret's son. DOB 1985-03-15, SSN 4472")
        self.assertNotEqual(s.fields.get("name"), "David Chen")
        self.assertFalse(s.consent_status)

    def test_unlisted_person_is_handed_off(self):
        s, say = self.chat()
        say("I'm Margaret Chen's son, can you tell me about her claim?")
        say("Tom Chen")
        self.assertTrue(s.human_transfer)
        self.assertFalse(s.holder_id)

    def test_relationship_must_match_record(self):
        s, say = self.chat()
        say("I'm David Chen, Margaret Chen's husband. DOB 1985-03-15, SSN 4472")
        self.assertTrue(s.human_transfer)
        self.assertEqual(s.consent_status, "")

    def test_representative_cannot_act_for_another_policyholder(self):
        s, say = self.chat()
        answer = say("I'm David Chen, Ava Lopez's son. Her DOB is 1990-08-21, SSN last four 9180, email ava.lopez@email.com")
        self.assertTrue(s.human_transfer)
        self.assertFalse(s.holder_id)
        self.assertEqual(s.consent_status, "")

    def test_after_consent_rep_can_talk_about_her_claim_but_not_become_someone_else(self):
        s, say = self.chat()
        say(DAVID)
        say("ok")
        self.assertIn("pathology report", say("what documents does her claim still need?"))
        self.assertTrue(s.holder_id)
        say("Actually my name is John Smith")
        self.assertFalse(s.holder_id)
        self.assertTrue(s.human_transfer)

    def test_summary_names_representative_and_goes_to_policyholder(self):
        s, say = self.chat()
        say(DAVID)
        say("ok")
        say("that's all")
        sent = say("send it")
        self.assertIn("margaret@email.com", sent)
        self.assertIn("David Chen, listed as the policyholder's son", summary(s))

    def test_policyholder_mentioning_rep_still_verifies_as_herself(self):
        s, say = self.chat()
        say("Margaret Chen DOB 1985-03-15 SSN last four 4472. I spoke to David Chen about my denied healthcare January claim.")
        self.assertEqual(s.holder_id, "P9")
        self.assertEqual(s.rep_name, "")

    def test_consent_fixture_drives_the_simulation(self):
        from engine import CONSENT
        self.assertEqual(CONSENT["default"]["status_sequence"][-1], "approved")
        self.assertNotIn("approved", CONSENT["timeout"]["status_sequence"])
        self.assertEqual(json.loads(json.dumps(Session().public()))["consent_status"], "")


if __name__ == "__main__":
    unittest.main()
