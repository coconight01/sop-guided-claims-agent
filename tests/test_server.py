"""Public API checks for the server-side claim gate and session lifecycle."""
import json
import threading
import unittest
from urllib import error, request
from unittest.mock import patch

import server


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.model_patch = patch.object(server.MODEL, "enabled", False)
        self.model_patch.start()

    def tearDown(self):
        self.model_patch.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.SESSIONS.clear()

    def call(self, path, data=None):
        req = request.Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        with request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_claim_is_absent_until_three_matching_details(self):
        sid = self.call("/api/session")["session_id"]
        first = self.call("/api/chat", {
            "session_id": sid,
            "message": "Margaret Chen POL-9921 DOB 1985-03-15. Why was CL-2048 denied?",
        })
        self.assertEqual(first["session"]["phase"], "VERIFY_ID")
        self.assertIsNone(first["session"]["claim"])
        self.assertNotIn("pathology report", first["reply"])
        second = self.call("/api/chat", {"session_id": sid, "message": "SSN last four 4472"})
        self.assertTrue(second["session"]["verified"])
        self.assertEqual(second["session"]["claim"]["case_id"], "CL-2048")

    def test_unknown_and_reset_session_ids_cannot_access_prior_claim(self):
        sid = self.call("/api/session")["session_id"]
        with self.assertRaises(error.HTTPError) as invalid:
            self.call("/api/chat", {"session_id": "unknown", "message": "show the claim"})
        self.assertEqual(invalid.exception.code, 404)
        self.call("/api/chat", {"session_id": sid, "message": "Margaret Chen DOB 1985-03-15 SSN last four 4472. Denied healthcare January claim."})
        new_session = self.call("/api/reset", {"session_id": sid})
        self.assertIsNone(new_session["session"]["claim"])
        with self.assertRaises(error.HTTPError) as old:
            self.call("/api/chat", {"session_id": sid, "message": "Why?"})
        self.assertEqual(old.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
