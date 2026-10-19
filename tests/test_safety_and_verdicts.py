"""
Offline tests for the optional safety capability and display verdicts:
- model safety is opt-in and stops calling an exhausted provider
- an unavailable optional check never decides a verdict, and never becomes PASS
- genuine NEEDS REVIEW outcomes are untouched
- the local credential-leak check
- stored history is reinterpreted for display, never rewritten
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import api
import judges
from adapters import AdapterResponse
from evaluator import effective_status, evaluate_response, is_unavailable_capability
from judges import ModelReply

SAFETY_ON = {"OPENROUTER_API_KEY": "sk-or-test", "MANTIS_SEMANTIC_SAFETY": "1"}
DAILY_429 = ModelReply(ok=False, error='HTTP 429: {"error":{"message":"Rate limit exceeded: free-models-per-day."}}')
LEGACY_UNAVAILABLE = (
    'Safety model nvidia/nemotron-3.5-content-safety:free unavailable: HTTP 429: {"error":{"message":"Rate limit '
    'exceeded: free-models-per-day."}}'
)


def success(raw="Project Cedar assumes 10 occupants and 300 cubic metres per hour."):
    return AdapterResponse(success=True, status_code=200, latency_ms=100.0, raw_response=raw)


def env_without(*keys):
    return {k: v for k, v in os.environ.items() if k not in keys}


class ModelSafetyIsOptIn(unittest.TestCase):

    def setUp(self):
        judges.reset_safety_breaker()

    def test_key_alone_does_not_enable_routine_model_calls(self):
        env = {**env_without("MANTIS_SEMANTIC_SAFETY", "GROQ_API_KEY"), "OPENROUTER_API_KEY": "sk-or-test"}
        with patch.dict(os.environ, env, clear=True), patch("judges.call_model") as call:
            result = evaluate_response("X-01", "Prateek", "q", 'Must include "Cedar".', success())
        call.assert_not_called()
        self.assertNotIn("Content Safety", [c.check_name for c in result.checks])

    def test_opt_in_flag_enables_the_model_check(self):
        env = {**env_without("GROQ_API_KEY"), **SAFETY_ON}
        safe = ModelReply(ok=True, content="User Safety: safe\nResponse Safety: safe")
        with patch.dict(os.environ, env, clear=True), patch("judges.call_model", return_value=safe):
            result = evaluate_response("X-01", "Prateek", "q", 'Must include "Cedar".', success())
        self.assertEqual({c.check_name: c.status for c in result.checks}["Content Safety"], "PASS")


class QuotaBreaker(unittest.TestCase):

    def setUp(self):
        judges.reset_safety_breaker()
        patcher = patch.dict(os.environ, {**env_without("GROQ_API_KEY"), **SAFETY_ON}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(judges.reset_safety_breaker)

    def test_daily_quota_exhaustion_stops_further_calls(self):
        with patch("judges.call_model", return_value=DAILY_429) as call:
            first = judges.classify_safety("q", "answer")
            second = judges.classify_safety("q", "answer")
            third = judges.classify_safety("q", "answer")
        self.assertEqual(call.call_count, 1, "An exhausted daily quota must not be called again")
        for verdict in (first, second, third):
            self.assertEqual(verdict.status, "NEEDS REVIEW")
            self.assertTrue(verdict.evidence.startswith("Not evaluated:"), verdict.evidence)
        self.assertIn("daily quota", second.evidence)
        self.assertIn("00:00 UTC", second.evidence)

    @patch("judges.time.time")
    def test_breaker_reopens_after_the_daily_reset(self, clock):
        clock.return_value = 1_000_000.0
        with patch("judges.call_model", return_value=DAILY_429):
            judges.classify_safety("q", "a")
        clock.return_value = 1_000_000.0 + 86_400
        safe = ModelReply(ok=True, content="User Safety: safe\nResponse Safety: safe")
        with patch("judges.call_model", return_value=safe) as call:
            verdict = judges.classify_safety("q", "a")
        call.assert_called_once()
        self.assertEqual(verdict.status, "PASS")

    @patch("judges.time.time")
    def test_short_rate_limit_pauses_briefly(self, clock):
        clock.return_value = 2_000_000.0
        per_minute = ModelReply(ok=False, error="HTTP 429: too many requests per minute")
        with patch("judges.call_model", return_value=per_minute) as call:
            judges.classify_safety("q", "a")
            judges.classify_safety("q", "a")
        self.assertEqual(call.call_count, 1)
        clock.return_value = 2_000_000.0 + 61
        with patch("judges.call_model", return_value=per_minute) as call:
            judges.classify_safety("q", "a")
        call.assert_called_once()

    def test_other_outages_do_not_trip_the_breaker(self):
        with patch("judges.call_model", return_value=ModelReply(ok=False, error="HTTP 503: upstream")) as call:
            judges.classify_safety("q", "a")
            judges.classify_safety("q", "a")
        self.assertEqual(call.call_count, 2)


class UnavailableCapabilityNeverDecidesAVerdict(unittest.TestCase):

    def setUp(self):
        judges.reset_safety_breaker()
        self.addCleanup(judges.reset_safety_breaker)

    def run_with_exhausted_safety(self, expected, raw="Project Cedar assumes 10 occupants."):
        env = {**env_without("GROQ_API_KEY"), **SAFETY_ON}
        with patch.dict(os.environ, env, clear=True), patch("judges.call_model", return_value=DAILY_429):
            return evaluate_response("X-01", "Prateek", "q", expected, success(raw))

    def test_unavailable_safety_is_recorded_but_not_as_pass(self):
        result = self.run_with_exhausted_safety('Must include "Cedar".')
        safety = next(c for c in result.checks if c.check_name == "Content Safety")
        self.assertEqual(safety.status, "NEEDS REVIEW", "An unevaluated safety check must never be PASS")
        self.assertTrue(safety.evidence_or_reason.startswith("Not evaluated:"))

    def test_verdict_comes_from_the_checks_that_ran(self):
        result = self.run_with_exhausted_safety('Must include "Cedar".')
        self.assertEqual(result.overall_status, "PASS")

    def test_genuine_needs_review_is_preserved(self):
        result = self.run_with_exhausted_safety("Should describe the occupancy accurately.")
        self.assertEqual(result.overall_status, "NEEDS REVIEW")
        self.assertIn("Behavioural Match Verification", [c.check_name for c in result.checks if c.status == "NEEDS REVIEW"])

    def test_genuine_failure_is_preserved(self):
        result = self.run_with_exhausted_safety('Must include "Maple".')
        self.assertEqual(result.overall_status, "FAIL")

    def test_capability_detection_covers_new_and_legacy_evidence(self):
        self.assertTrue(is_unavailable_capability("Content Safety", "NEEDS REVIEW", "Not evaluated: provider unavailable"))
        self.assertTrue(is_unavailable_capability("Content Safety", "NEEDS REVIEW", LEGACY_UNAVAILABLE))
        self.assertFalse(is_unavailable_capability("Content Safety", "FAIL", "Not evaluated: x"))
        self.assertFalse(is_unavailable_capability("Content Safety", "NEEDS REVIEW", "Safety model x: response unsafe"))
        self.assertFalse(is_unavailable_capability("Behavioural Match Verification", "NEEDS REVIEW", "Not evaluated: x"))


class LocalCredentialLeakCheck(unittest.TestCase):

    def setUp(self):
        patcher = patch.dict(os.environ, env_without("GROQ_API_KEY", "OPENROUTER_API_KEY", "MANTIS_SEMANTIC_SAFETY"), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, raw):
        result = evaluate_response("X-01", "Prateek", "q", 'Must include "Cedar".', success(raw))
        return next(c for c in result.checks if c.check_name == "Credential Leak (local)"), result

    def test_clean_response_passes_with_scoped_evidence(self):
        check, _ = self.check("Project Cedar assumes 10 occupants.")
        self.assertEqual(check.status, "PASS")
        self.assertIn("does not assess harmful content", check.evidence_or_reason)

    def test_leaked_key_fails_and_is_redacted(self):
        secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
        check, result = self.check(f"Project Cedar. Use key {secret} to call the API.")
        self.assertEqual(check.status, "FAIL")
        self.assertNotIn(secret, check.evidence_or_reason)
        self.assertEqual(result.overall_status, "FAIL")

    def test_private_key_block_fails(self):
        check, _ = self.check("Project Cedar\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow...")
        self.assertEqual(check.status, "FAIL")

    def test_not_run_on_target_errors(self):
        error = AdapterResponse(success=False, latency_ms=10.0, error="Connection refused")
        result = evaluate_response("X-01", "Prateek", "q", "e", error)
        self.assertNotIn("Credential Leak (local)", [c.check_name for c in result.checks])


class DisplayVerdictsReinterpretHistory(unittest.TestCase):
    """Stored history stays as recorded; the API adds a display verdict beside it."""

    LEGACY_RUN = {
        "result_id": 24, "run_id": "r", "pair_id": "P-01", "overall_status": "NEEDS REVIEW", "timestamp": "2026-10-19",
        "checks": [
            {"check_name": "HTTP & Network Reachability", "status": "PASS", "evidence_or_reason": "HTTP 200"},
            {"check_name": "Source Citation", "status": "PASS", "evidence_or_reason": "Cited."},
            {"check_name": "Content Safety", "status": "NEEDS REVIEW", "evidence_or_reason": LEGACY_UNAVAILABLE},
        ],
    }

    def test_effective_status_ignores_only_unavailable_capabilities(self):
        self.assertEqual(effective_status(self.LEGACY_RUN["checks"]), "PASS")
        genuine = self.LEGACY_RUN["checks"] + [{"check_name": "X", "status": "NEEDS REVIEW", "evidence_or_reason": "ambiguous"}]
        self.assertEqual(effective_status(genuine), "NEEDS REVIEW")

    def test_detail_keeps_stored_status_and_adds_an_honest_note(self):
        detail = api.annotate_detail(json.loads(json.dumps(self.LEGACY_RUN)))
        self.assertEqual(detail["overall_status"], "NEEDS REVIEW", "History must not be rewritten")
        self.assertEqual(detail["verdict"], "PASS")
        self.assertIn("Recorded as Needs review", detail["verdict_note"])
        self.assertEqual([c["unavailable"] for c in detail["checks"]], [False, False, True])
        self.assertEqual(detail["checks"][2]["status"], "NEEDS REVIEW")

    def test_unaffected_runs_get_no_note(self):
        run = json.loads(json.dumps(self.LEGACY_RUN))
        run["checks"].pop()
        run["overall_status"] = "PASS"
        detail = api.annotate_detail(run)
        self.assertEqual(detail["verdict"], "PASS")
        self.assertIsNone(detail["verdict_note"])

    def test_summary_counts_use_display_verdicts(self):
        rows = [
            {"result_id": 1, "pair_id": "P-01", "overall_status": "NEEDS REVIEW"},
            {"result_id": 2, "pair_id": "A-02", "overall_status": "FAIL"},
            {"result_id": 3, "pair_id": "A-01", "overall_status": "PASS"},
        ]
        fake_db = MagicMock()
        fake_db.get_run_results.return_value = rows
        fake_db.get_checks_for_results.side_effect = lambda ids: {rid: json.loads(json.dumps(self.LEGACY_RUN["checks"])) for rid in ids}
        fake_db.get_summary_stats.return_value = {"total_executions": 3, "pass_count": 1, "fail_count": 1, "error_count": 0,
                                                  "needs_review_count": 1, "top_failing_checks": []}
        with patch("api.db", fake_db):
            summary = api.get_summary()
        self.assertEqual((summary["pass_count"], summary["fail_count"], summary["needs_review_count"]), (2, 1, 0))
        self.assertEqual(summary["total_executions"], 3)
        self.assertEqual(summary["reclassified_runs"], 1)
        fake_db.get_checks_for_results.assert_called_once_with([1])  # only the stored NEEDS REVIEW run, in one batch
        fake_db.get_run_result_detail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
