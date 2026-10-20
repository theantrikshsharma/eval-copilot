"""
End-to-end judge pipeline through evaluate_response, with the provider mocked: when the judge runs, which
provider/model/auth it uses, and that only a genuine, quote-backed judge verdict becomes PASS or FAIL.
Evidence must lead with the judge's outcome so the UI's two-line preview shows it. No network.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

import requests

import judges
from adapters import AdapterResponse
from evaluator import evaluate_response
from judges import ModelReply

FAKE_KEY = "gsk_" + "f" * 40
JUDGE_ON = {"GROQ_API_KEY": FAKE_KEY}
NAME = "Behavioural Match Verification"
INPUT = "What is the BENG values of a project ?"
EXPECTED = "Give BENG 1, BENG 2, BENG 3 values in kWh/m2, project name"  # no quoted phrases: semantic only
ANSWER = ("**Answer**\n- BENG 1: maximum of 60 kWh/m2 per year\n- BENG 2: maximum of 35 kWh/m2 per year\n"
          "- BENG 3: minimum of 45 percent\n\n**Source**\n- FICTIONAL training rules: BENG for Project Maple, Section 1.1")


def run(env, reply=None, expected=EXPECTED, answer=ANSWER):
    """Evaluate a generic Prateek case with the given env and (optionally) a stubbed judge reply."""
    resp = AdapterResponse(success=True, status_code=200, latency_ms=900.0, raw_response=answer)
    base = {k: v for k, v in os.environ.items() if k not in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MANTIS_SEMANTIC_SAFETY")}
    with patch.dict(os.environ, {**base, **env}, clear=True), \
            patch("judges.call_model", return_value=reply) as call:
        result = evaluate_response("X-20", "Prateek", INPUT, expected, resp)
    check = next(c for c in result.checks if c.check_name == NAME) if any(c.check_name == NAME for c in result.checks) else None
    return result, check, call


def verdict(v, quote="BENG 1: maximum of 60 kWh/m2 per year", reason="States all three BENG values."):
    return ModelReply(ok=True, content=json.dumps({"verdict": v, "evidence_quote": quote, "reason": reason}))


class TestJudgeVerdicts(unittest.TestCase):

    def test_genuine_pass_is_recorded_as_pass_and_leads_the_evidence(self):
        result, check, call = run(JUDGE_ON, verdict("PASS"))
        self.assertEqual(call.call_count, 1)
        self.assertEqual((check.status, result.overall_status), ("PASS", "PASS"))
        self.assertTrue(check.evidence_or_reason.startswith("Judge openai/gpt-oss-120b"), check.evidence_or_reason)
        self.assertIn("PASS", check.evidence_or_reason.split("|")[0])
        self.assertIn("Deterministic:", check.evidence_or_reason)

    def test_genuine_fail_is_recorded_as_fail(self):
        result, check, _ = run(JUDGE_ON, verdict("FAIL", reason="Omits the project name."))
        self.assertEqual((check.status, result.overall_status), ("FAIL", "FAIL"))
        self.assertTrue(check.evidence_or_reason.startswith("Judge openai/gpt-oss-120b"))

    def test_provider_errors_never_become_pass(self):
        errors = {
            "HTTP 500": ModelReply(ok=False, error="HTTP 500: upstream error"),
            "rate limit": ModelReply(ok=False, error="HTTP 429: Rate limit reached"),
            "timeout": ModelReply(ok=False, error="Model request timed out after 30.0s"),
            "malformed": ModelReply(ok=False, error="Malformed provider response"),
        }
        for label, reply in errors.items():
            with self.subTest(error=label):
                result, check, _ = run(JUDGE_ON, reply)
                self.assertEqual((check.status, result.overall_status), ("NEEDS REVIEW", "NEEDS REVIEW"))
                self.assertTrue(check.evidence_or_reason.startswith("Judge openai/gpt-oss-120b [judge-v2] unavailable"))

    def test_unusable_verdicts_never_become_pass(self):
        cases = {
            "unsure": verdict("UNSURE", quote=""),
            "invented quote": verdict("PASS", quote="BENG 1 is 999 kWh"),
            "not json": ModelReply(ok=True, content="PASS"),
        }
        for label, reply in cases.items():
            with self.subTest(case=label):
                result, check, _ = run(JUDGE_ON, reply)
                self.assertEqual((check.status, result.overall_status), ("NEEDS REVIEW", "NEEDS REVIEW"))
                self.assertTrue(check.evidence_or_reason.startswith("Judge openai/gpt-oss-120b"))


class TestJudgeNotRun(unittest.TestCase):

    def test_missing_key_is_stated_and_never_passes(self):
        result, check, call = run({})
        call.assert_not_called()
        self.assertEqual((check.status, result.overall_status), ("NEEDS REVIEW", "NEEDS REVIEW"))
        self.assertTrue(check.evidence_or_reason.startswith("Judge not run: GROQ_API_KEY is not configured"),
                        check.evidence_or_reason)
        self.assertIn("Deterministic:", check.evidence_or_reason)

    def test_deterministic_rules_decide_without_the_judge(self):
        for answer, status in ((ANSWER, "PASS"), ("BENG 1 is 60.", "FAIL")):
            with self.subTest(status=status):
                result, _, call = run(JUDGE_ON, verdict("FAIL"), expected='Must include "Project Maple".', answer=answer)
                call.assert_not_called()
                self.assertEqual(result.overall_status, status)

    def test_target_errors_short_circuit_before_the_judge(self):
        resp = AdapterResponse(success=False, latency_ms=20000.0, error="Request timed out after 20s")
        with patch.dict(os.environ, JUDGE_ON), patch("judges.call_model") as call:
            result = evaluate_response("X-20", "Prateek", INPUT, EXPECTED, resp)
        call.assert_not_called()
        self.assertEqual(result.overall_status, "ERROR")


class TestJudgeProvider(unittest.TestCase):

    def test_request_goes_to_groq_with_the_judge_model_and_server_side_key(self):
        provider = MagicMock(status_code=200)
        provider.json.return_value = {"choices": [{"message": {"content": verdict("PASS").content}}]}
        with patch.dict(os.environ, JUDGE_ON), patch("judges.requests.post", return_value=provider) as post:
            result = evaluate_response("X-20", "Prateek", INPUT, EXPECTED,
                                       AdapterResponse(success=True, status_code=200, latency_ms=1.0, raw_response=ANSWER))
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "openai/gpt-oss-120b")
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(result.overall_status, "PASS")
        self.assertNotIn(FAKE_KEY, json.dumps([c.model_dump() for c in result.checks]))

    def test_provider_error_body_echoing_the_key_is_scrubbed(self):
        provider = MagicMock(status_code=401, text=f"invalid api key {FAKE_KEY}")
        with patch.dict(os.environ, JUDGE_ON), patch("judges.requests.post", return_value=provider):
            result = evaluate_response("X-20", "Prateek", INPUT, EXPECTED,
                                       AdapterResponse(success=True, status_code=200, latency_ms=1.0, raw_response=ANSWER))
        self.assertEqual(result.overall_status, "NEEDS REVIEW")
        self.assertNotIn(FAKE_KEY, json.dumps([c.model_dump() for c in result.checks]))

    def test_network_failures_are_reported_not_raised(self):
        with patch.dict(os.environ, JUDGE_ON), patch("judges.requests.post", side_effect=requests.exceptions.ConnectionError()), \
                patch("judges.time.sleep"):
            _, check, _ = run(JUDGE_ON, judges._post_once(judges.GROQ_URL, FAKE_KEY, {})[0])
        self.assertEqual(check.status, "NEEDS REVIEW")
        self.assertIn("ConnectionError", check.evidence_or_reason)


class TestCredentialLeakStillWorks(unittest.TestCase):

    def test_a_leaked_key_in_the_target_response_fails_the_run(self):
        result, _, _ = run(JUDGE_ON, verdict("PASS"), answer=ANSWER + f"\nDebug: GROQ_API_KEY={FAKE_KEY}")
        leak = next(c for c in result.checks if c.check_name == "Credential Leak (local)")
        self.assertEqual(leak.status, "FAIL")
        self.assertNotIn(FAKE_KEY, leak.evidence_or_reason)
        self.assertEqual(result.overall_status, "FAIL")


if __name__ == "__main__":
    unittest.main()
