"""
Universal App Testing: SSRF guards, request/response mapping, verdict semantics and the admin-only endpoints.
DNS and HTTP are mocked and writes go to a temporary SQLite file: no network, no Supabase.
"""

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

import requests
from fastapi.testclient import TestClient

import api
import db
import universal

SECRET = "sk-test-" + "z" * 24
CONFIG = {"config_id": "demo", "name": "Demo bot", "url": "https://api.example.com/chat", "method": "POST",
          "headers": {"Authorization": f"Bearer {SECRET}"}, "body_template": '{"question": "{{input}}"}',
          "response_path": "data.answer"}
NO_MODELS = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MANTIS_SEMANTIC_SAFETY")}


def resolves_to(*addresses):
    return patch("universal.socket.getaddrinfo", return_value=[(socket.AF_INET, 1, 6, "", (a, 443)) for a in addresses])


def http_reply(status=200, body=b"", encoding="utf-8"):
    reply = MagicMock(status_code=status, encoding=encoding)
    reply.iter_content.return_value = [body]
    reply.__enter__ = lambda s: s
    reply.__exit__ = lambda s, *a: False
    return reply


class TestSsrfGuards(unittest.TestCase):

    def test_internal_addresses_are_blocked(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1",
                        "fd00::1", "::ffff:127.0.0.1", "224.0.0.1"):
            with self.subTest(address=address), resolves_to(address):
                with self.assertRaises(universal.UnsafeTarget):
                    universal.check_url("https://looks-public.example.com/")

    def test_one_internal_address_among_public_ones_is_blocked(self):
        with resolves_to("93.184.216.34", "10.1.2.3"), self.assertRaises(universal.UnsafeTarget):
            universal.check_url("https://mixed.example.com/")

    def test_scheme_userinfo_and_unresolvable_hosts_are_rejected(self):
        for url in ("file:///etc/passwd", "ftp://example.com/", "https://user:pw@example.com/", "http:///nohost"):
            with self.subTest(url=url), self.assertRaises(universal.UnsafeTarget):
                universal.check_url(url)
        with patch("universal.socket.getaddrinfo", side_effect=socket.gaierror()), self.assertRaises(universal.UnsafeTarget):
            universal.check_url("https://does-not-exist.invalid/")

    def test_public_address_is_allowed(self):
        with resolves_to("93.184.216.34"):
            self.assertEqual(universal.check_url("https://example.com/x"), ["93.184.216.34"])

    def test_blocked_target_is_never_requested(self):
        with resolves_to("127.0.0.1"), patch("universal.requests.request") as req:
            resp = universal.invoke(CONFIG, "hi")
        req.assert_not_called()
        self.assertFalse(resp.success)
        self.assertIn("Blocked", resp.error)


class TestInvoke(unittest.TestCase):

    def call(self, reply, config=CONFIG, test_input='Say "hi"'):
        with resolves_to("93.184.216.34"), patch("universal.requests.request", return_value=reply) as req:
            return universal.invoke(config, test_input), req

    def test_maps_the_response_and_escapes_the_input_into_valid_json(self):
        resp, req = self.call(http_reply(body=json.dumps({"data": {"answer": "Hello"}}).encode()))
        self.assertTrue(resp.success)
        self.assertEqual(resp.raw_response, "Hello")
        kwargs = req.call_args.kwargs
        self.assertEqual(json.loads(kwargs["data"]), {"question": 'Say "hi"'})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"][0], universal.CONNECT_TIMEOUT_S)

    def test_redirects_are_not_followed(self):
        resp, _ = self.call(http_reply(status=302))
        self.assertFalse(resp.success)
        self.assertIn("Redirects are not followed", resp.error)

    def test_oversized_responses_are_cut_off(self):
        resp, _ = self.call(http_reply(body=b"x" * (universal.MAX_RESPONSE_BYTES + 1)))
        self.assertIn("exceeded", resp.error)

    def test_missing_mapping_is_a_contract_error(self):
        resp, _ = self.call(http_reply(body=b'{"other": 1}'))
        self.assertTrue(resp.contract_error)

    def test_header_secrets_are_redacted_from_errors_and_answers(self):
        resp, _ = self.call(http_reply(status=401, body=f"bad key {SECRET}".encode()))
        self.assertNotIn(SECRET, resp.error)
        resp, _ = self.call(http_reply(body=json.dumps({"data": {"answer": f"echo {SECRET}"}}).encode()))
        self.assertNotIn(SECRET, resp.raw_response)

    def test_timeouts_never_raise(self):
        with resolves_to("93.184.216.34"), patch("universal.requests.request", side_effect=requests.exceptions.Timeout()):
            resp = universal.invoke(CONFIG, "x", timeout=99)
        self.assertIn("timed out after 30.0s", resp.error)


class TestEndpoints(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = db.SQLITE_PATH
        db.use_sqlite_path(Path(self.tmp.name) / "u.db")
        env = patch.dict(os.environ, {**NO_MODELS, "MANTIS_ADMIN_TOKEN": "admin"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(api.app)
        self.auth = {"Authorization": "Bearer admin"}

    def tearDown(self):
        db.use_sqlite_path(self.original, initialise=False)
        self.tmp.cleanup()

    def create(self, **overrides):
        with resolves_to("93.184.216.34"):
            return self.client.post("/api/universal/configs", json={**CONFIG, **overrides}, headers=self.auth)

    def test_everything_that_writes_or_calls_out_needs_the_admin_token(self):
        self.assertEqual(self.client.post("/api/universal/configs", json=CONFIG).status_code, 401)
        self.assertEqual(self.create().status_code, 200)
        self.assertEqual(self.client.post("/api/universal/configs/demo/cases", json={"cases": [{"input": "a", "expected_behaviour": "b"}]}).status_code, 401)
        self.assertEqual(self.client.post("/api/universal/configs/demo/run").status_code, 401)

    def test_header_values_never_leave_the_server(self):
        created = self.create().json()
        listed = self.client.get("/api/universal/configs").json()
        self.assertEqual(created["header_names"], ["Authorization"])
        self.assertNotIn(SECRET, json.dumps([created, listed]))

    def test_internal_urls_are_refused_at_save(self):
        with resolves_to("169.254.169.254"):
            resp = self.client.post("/api/universal/configs", json={**CONFIG, "url": "http://metadata.example/"}, headers=self.auth)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("non-public", resp.json()["detail"])

    def test_invalid_template_and_bad_golden_pairs_are_rejected(self):
        self.assertEqual(self.create(body_template="{not json {{input}}").status_code, 400)
        self.create()
        for bad in ({"cases": []}, {"cases": [{"input": "a"}]}, {"cases": [{"input": "", "expected_behaviour": "b"}]}, [1, 2]):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post("/api/universal/configs/demo/cases", json=bad, headers=self.auth).status_code, 422)

    def test_upload_run_and_inspect_end_to_end(self):
        self.create()
        cases = {"cases": [{"input": "Capital of France?", "expected_behaviour": 'Must include "Paris".'},
                           {"input": "Capital of Spain?", "expected_behaviour": 'Must include "Madrid".'},
                           {"input": "Say anything", "expected_behaviour": "Be polite."}]}
        self.assertEqual(self.client.post("/api/universal/configs/demo/cases", json=cases, headers=self.auth).json()["created"],
                         ["U-demo-01", "U-demo-02", "U-demo-03"])
        reply = lambda *a, **k: http_reply(body=json.dumps({"data": {"answer": "It is Paris."}}).encode())
        with resolves_to("93.184.216.34"), patch("universal.requests.request", side_effect=reply):
            out = self.client.post("/api/universal/configs/demo/run", headers=self.auth).json()
        verdicts = {r["pair_id"]: r["verdict"] for r in out["results"]}
        # HTTP 200 alone is not PASS: unmet quoted phrase FAILs, and an unquoted expectation without a judge needs review
        self.assertEqual(verdicts, {"U-demo-01": "PASS", "U-demo-02": "FAIL", "U-demo-03": "NEEDS REVIEW"})
        detail = self.client.get(f"/api/results/{out['results'][1]['result_id']}").json()
        self.assertEqual((detail["pair_id"], detail["target_app"], detail["actual_response"]), ("U-demo-02", "Universal:demo", "It is Paris."))
        self.assertIn("Madrid", json.dumps(detail["checks"]))

    def test_a_universal_case_runs_through_the_existing_execute_endpoint_with_the_token(self):
        self.create()
        self.client.post("/api/universal/configs/demo/cases", json={"cases": [{"input": "x", "expected_behaviour": 'Must include "ok".'}]}, headers=self.auth)
        self.assertEqual(self.client.post("/api/execute", json={"pair_id": "U-demo-01"}).status_code, 401)
        with resolves_to("93.184.216.34"), patch("universal.requests.request", return_value=http_reply(body=b'{"data": {"answer": "ok"}}')):
            resp = self.client.post("/api/execute", json={"pair_id": "U-demo-01"}, headers=self.auth)
        self.assertEqual(resp.json()["overall_status"], "PASS")

    def test_target_errors_are_error_not_fail(self):
        self.create()
        self.client.post("/api/universal/configs/demo/cases", json={"cases": [{"input": "x", "expected_behaviour": 'Must include "ok".'}]}, headers=self.auth)
        with resolves_to("93.184.216.34"), patch("universal.requests.request", return_value=http_reply(status=500, body=b"boom")):
            out = self.client.post("/api/universal/configs/demo/run", headers=self.auth).json()
        self.assertEqual(out["results"][0]["verdict"], "ERROR")


if __name__ == "__main__":
    unittest.main()
