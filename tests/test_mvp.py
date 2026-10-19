"""
End-to-End Acceptance Test for MANTIS (Checkpoint 4 MVP).
Verifies the complete 9-step workflow specified in Section 3 and Section 11:
1. Create or load a golden test case.
2. Execute it against a target application.
3. Capture the actual response or execution error.
4. Run applicable checks.
5. Save the execution and check results to database.
6. Reload the saved results.
7. Execute the same test again.
8. Compare the two executions without overwriting either.
9. Show evidence explaining individual check outcomes.
"""

import sys
import os
import unittest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

import db
from adapters import invoke_target
from evaluator import evaluate_response


class TestEvalCopilotMVP(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # A fresh temporary database (seeded with the golden pairs), so tests never write to real history
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls._real_path = db.SQLITE_PATH
        db.use_sqlite_path(Path(cls._tmp.name) / "mvp_test.db")

    @classmethod
    def tearDownClass(cls):
        db.use_sqlite_path(cls._real_path, initialise=False)
        cls._tmp.cleanup()

    def test_01_load_golden_pairs(self):
        """1. Verify initial golden pairs are loaded and accessible."""
        pairs = db.get_golden_pairs()
        self.assertGreaterEqual(len(pairs), 8, "Expected at least 8 seeded golden pairs")
        
        pair_ids = [p["pair_id"] for p in pairs]
        for expected_id in ["A-01", "A-02", "A-03", "A-04", "P-01", "P-02", "P-03", "P-04"]:
            self.assertIn(expected_id, pair_ids, f"Expected golden pair {expected_id} to exist")

    def test_02_create_custom_golden_pair(self):
        """1b. Verify a new golden pair can be created and persisted."""
        saved = db.save_golden_pair(
            pair_id="TEST-01",
            target_app="Prateek",
            input_text="What is the mock daylight rule for Project Birch?",
            expected_behaviour="Extract daylight requirement of at least 12 percent equivalent daylight area."
        )
        self.assertEqual(saved["pair_id"], "TEST-01")
        retrieved = db.get_golden_pair("TEST-01")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["target_app"], "Prateek")

    def test_03_execute_target_b_real_request(self):
        """2-5. Execute against Target B (Prateek), evaluate, and save to DB."""
        pair = db.get_golden_pair("P-01")
        self.assertIsNotNone(pair)

        # 2. Invoke Target B
        resp = invoke_target(pair["target_app"], pair["input"], timeout=15.0)
        self.assertTrue(resp.success, f"Expected successful request to Target B: {resp.error}")
        self.assertIn("300", resp.raw_response, "Expected '300' in Prateek response")

        # 4. Evaluate checks
        eval_res = evaluate_response(
            pair_id=pair["pair_id"],
            target_app=pair["target_app"],
            test_input=pair["input"],
            expected_behaviour=pair["expected_behaviour"],
            resp=resp
        )
        self.assertIn(eval_res.overall_status, ["PASS", "FAIL", "ERROR", "NEEDS REVIEW"])
        self.assertGreater(len(eval_res.checks), 0, "Expected at least 1 check result")

        # 5. Save to database
        checks_data = [c.model_dump() for c in eval_res.checks]
        result_id_1 = db.save_run_result(
            run_id="batch_test_1",
            pair_id="P-01",
            actual_response=resp.raw_response,
            overall_status=eval_res.overall_status,
            checks=checks_data
        )
        self.assertGreater(result_id_1, 0)

        # 6. Reload saved results from database
        reloaded = db.get_run_result_detail(result_id_1)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["pair_id"], "P-01")
        self.assertEqual(reloaded["overall_status"], eval_res.overall_status)
        self.assertEqual(len(reloaded["checks"]), len(eval_res.checks))

        # 7. Execute same test again (rerun)
        resp_2 = invoke_target(pair["target_app"], pair["input"], timeout=15.0)
        eval_res_2 = evaluate_response(
            pair_id=pair["pair_id"],
            target_app=pair["target_app"],
            test_input=pair["input"],
            expected_behaviour=pair["expected_behaviour"],
            resp=resp_2
        )
        checks_data_2 = [c.model_dump() for c in eval_res_2.checks]
        result_id_2 = db.save_run_result(
            run_id="batch_test_2",
            pair_id="P-01",
            actual_response=resp_2.raw_response,
            overall_status=eval_res_2.overall_status,
            checks=checks_data_2
        )
        self.assertNotEqual(result_id_1, result_id_2, "Rerun must create a new record without overwriting the first")

        # 8. Compare the two executions
        reloaded_1 = db.get_run_result_detail(result_id_1)
        reloaded_2 = db.get_run_result_detail(result_id_2)
        self.assertIsNotNone(reloaded_1)
        self.assertIsNotNone(reloaded_2)

        # 9. Verify evidence explaining individual check outcomes
        for chk in reloaded_1["checks"]:
            self.assertTrue(len(chk["evidence_or_reason"]) > 0, f"Check {chk['check_name']} must have evidence")

    def test_04_execute_target_a_real_request(self):
        """Verify real execution against Target A (Anupma) and failure detection."""
        pair = db.get_golden_pair("A-01")
        self.assertIsNotNone(pair)

        resp = invoke_target(pair["target_app"], pair["input"], timeout=15.0)
        self.assertTrue(resp.success, f"Expected successful request to Target A: {resp.error}")
        self.assertGreater(len(resp.raw_response), 0)

        eval_res = evaluate_response(
            pair_id=pair["pair_id"],
            target_app=pair["target_app"],
            test_input=pair["input"],
            expected_behaviour=pair["expected_behaviour"],
            resp=resp
        )
        self.assertIn(eval_res.overall_status, ["PASS", "FAIL", "ERROR", "NEEDS REVIEW"])

        # Save result
        result_id = db.save_run_result(
            run_id="batch_anupma_1",
            pair_id="A-01",
            actual_response=resp.raw_response,
            overall_status=eval_res.overall_status,
            checks=[c.model_dump() for c in eval_res.checks]
        )
        detail = db.get_run_result_detail(result_id)
        self.assertEqual(detail["pair_id"], "A-01")
        self.assertTrue(len(detail["checks"]) > 0)

    def test_05_error_vs_fail_distinction(self):
        """Verify the evaluation engine distinguishes ERROR (technical failure) from FAIL (behavioral failure)."""
        from adapters import AdapterResponse
        
        # Simulated Technical Connection Error
        error_resp = AdapterResponse(
            success=False,
            status_code=None,
            latency_ms=5000.0,
            raw_response="",
            error="Connection refused"
        )
        eval_err = evaluate_response("P-01", "Prateek", "query", "expected", error_resp)
        self.assertEqual(eval_err.overall_status, "ERROR")
        self.assertIn("Execution error", eval_err.why_failed)

        # Simulated Behavioral Failure (HTTP 200 succeeded, but wrong answer)
        fail_resp = AdapterResponse(
            success=True,
            status_code=200,
            latency_ms=250.0,
            raw_response="No reliable answer found in the fictional document collection.",
            parsed_data={"answer": "No reliable answer found in the fictional document collection."}
        )
        eval_fail = evaluate_response("P-01", "Prateek", "query", "expected", fail_resp)
        self.assertEqual(eval_fail.overall_status, "FAIL")
        self.assertIsNotNone(eval_fail.why_failed)

    def test_06_summary_metrics(self):
        """Verify summary metrics reflect persisted data correctly."""
        stats = db.get_summary_stats()
        self.assertGreaterEqual(stats["total_executions"], 1)
        self.assertIn("pass_count", stats)
        self.assertIn("fail_count", stats)
        self.assertIn("error_count", stats)
        self.assertIn("needs_review_count", stats)
        self.assertIn("top_failing_checks", stats)


if __name__ == "__main__":
    unittest.main()
