"""
Regression tests for the audit findings. Every test that writes uses a temporary SQLite database,
so the real eval_copilot.db is never touched. No network calls.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

import api
import db
from adapters import AdapterResponse
from evaluator import evaluate_response

NO_MODELS = {k: v for k, v in os.environ.items()
             if k not in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "MANTIS_SEMANTIC_SAFETY", "MANTIS_ADMIN_TOKEN", "VERCEL",
                          "VERCEL_ENV", "LAMBDA_TASK_ROOT", "AWS_LAMBDA_FUNCTION_NAME", "MANTIS_HOSTED", "MANTIS_SQLITE_PATH")}
REAL_DB = PROJECT_ROOT / "eval_copilot.db"
_real_db_fingerprint = None


def _fingerprint(path: Path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def setUpModule():
    global _real_db_fingerprint
    _real_db_fingerprint = _fingerprint(REAL_DB)


def tearDownModule():
    # Guard: nothing in this module may ever write to the real history database
    assert _fingerprint(REAL_DB) == _real_db_fingerprint, "tests modified the real eval_copilot.db"


class TempDatabaseCase(unittest.TestCase):
    """Points db at a fresh temporary SQLite file for each test, then restores the real path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = db.SQLITE_PATH
        db.use_sqlite_path(Path(self.tmp.name) / "test.db")
        env = patch.dict(os.environ, NO_MODELS, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(api.app)

    def tearDown(self):
        db.use_sqlite_path(self.original_path, initialise=False)
        self.tmp.cleanup()


# ----------------------------------------------------
# H2: golden pairs are never silently overwritten
# ----------------------------------------------------

class GoldenPairsAreNotOverwritten(TempDatabaseCase):

    NEW = {"pair_id": "X-01", "target_app": "Prateek", "input": "q", "expected_behaviour": "e"}

    def test_creating_a_new_pair_succeeds(self):
        resp = self.client.post("/api/golden-pairs", json=self.NEW)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(db.get_golden_pair("X-01")["input"], "q")

    def test_existing_id_is_rejected_and_left_unchanged(self):
        before = db.get_golden_pair("A-01")
        resp = self.client.post("/api/golden-pairs", json={**self.NEW, "pair_id": "A-01", "input": "overwritten"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already exists", resp.json()["detail"])
        self.assertEqual(db.get_golden_pair("A-01"), before)


# ----------------------------------------------------
# H1 + C1: write access and durable storage
# ----------------------------------------------------

class WriteAccess(TempDatabaseCase):

    PAIR = {"pair_id": "X-02", "target_app": "Anupma", "input": "q", "expected_behaviour": "e"}

    def post(self, headers=None):
        return self.client.post("/api/golden-pairs", json=self.PAIR, headers=headers or {})

    def test_local_development_needs_no_token(self):
        self.assertEqual(self.post().status_code, 200)

    @patch.dict(os.environ, {"MANTIS_ADMIN_TOKEN": "correct-horse"})
    def test_configured_token_is_required_for_writes(self):
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.post({"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.post({"Authorization": "Bearer correct-horse"}).status_code, 200)

    @patch.dict(os.environ, {"MANTIS_ADMIN_TOKEN": "correct-horse"})
    def test_execute_is_also_protected_and_makes_no_target_call(self):
        with patch("api.invoke_target") as invoke:
            resp = self.client.post("/api/execute", json={"pair_id": "A-01"})
        self.assertEqual(resp.status_code, 401)
        invoke.assert_not_called()

    @patch.dict(os.environ, {"MANTIS_ADMIN_TOKEN": "correct-horse"})
    def test_reads_stay_public(self):
        for path in ("/api/golden-pairs", "/api/results", "/api/summary", "/api/health"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    @patch.dict(os.environ, {"VERCEL": "1"})
    def test_hosted_deployment_without_a_token_refuses_writes(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 403)
        self.assertIn("MANTIS_ADMIN_TOKEN", resp.json()["detail"])

    @patch.dict(os.environ, {"VERCEL": "1", "MANTIS_ADMIN_TOKEN": "correct-horse"})
    def test_non_durable_storage_refuses_writes_instead_of_losing_them(self):
        with patch("db.is_durable", return_value=False):
            resp = self.post({"Authorization": "Bearer correct-horse"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("durable", resp.json()["detail"])
        self.assertIsNone(db.get_golden_pair("X-02"))

    def test_health_reports_capabilities_without_paths_or_urls(self):
        body = self.client.get("/api/health").json()
        self.assertIn("durable", body["database"])
        self.assertIn("writes_require_token", body)
        flat = str(body)
        self.assertNotIn(str(PROJECT_ROOT), flat)
        self.assertNotIn("supabase.co", flat)


# ----------------------------------------------------
# M2: bounded request inputs
# ----------------------------------------------------

class RequestValidation(TempDatabaseCase):

    def test_execute_rejects_out_of_range_timeouts(self):
        with patch("api.invoke_target") as invoke:
            for timeout in (0, -5, 600):
                with self.subTest(timeout=timeout):
                    self.assertEqual(self.client.post("/api/execute", json={"pair_id": "A-01", "timeout": timeout}).status_code, 422)
        invoke.assert_not_called()

    def test_golden_pair_fields_are_validated(self):
        base = {"pair_id": "X-03", "target_app": "Prateek", "input": "q", "expected_behaviour": "e"}
        for bad in ({"pair_id": "has spaces"}, {"pair_id": ""}, {"target_app": "Unknown"},
                    {"input": "x" * 20_001}, {"expected_behaviour": ""}):
            with self.subTest(bad=list(bad)):
                self.assertEqual(self.client.post("/api/golden-pairs", json={**base, **bad}).status_code, 422)

    def test_run_label_length_is_bounded(self):
        resp = self.client.post("/api/execute", json={"pair_id": "A-01", "run_id": "r" * 200})
        self.assertEqual(resp.status_code, 422)


# ----------------------------------------------------
# History integrity: append-only runs and correct relationships
# ----------------------------------------------------

class HistoryIntegrity(TempDatabaseCase):

    def run_once(self, raw, pair_id="P-01"):
        resp = AdapterResponse(success=True, status_code=200, latency_ms=10.0, raw_response=raw)
        with patch("api.invoke_target", return_value=resp):
            return self.client.post("/api/execute", json={"pair_id": pair_id}).json()

    def test_reruns_append_and_never_overwrite(self):
        first = self.run_once("Project Cedar: 10 occupants, 300 cubic metres per hour.")
        second = self.run_once("No reliable answer found in the fictional document collection.")
        self.assertNotEqual(first["result_id"], second["result_id"])
        stored_first = db.get_run_result_detail(first["result_id"])
        self.assertEqual(stored_first["actual_response"], "Project Cedar: 10 occupants, 300 cubic metres per hour.")
        self.assertEqual(stored_first["overall_status"], first["overall_status"])

    def test_results_and_checks_belong_to_the_right_parents(self):
        run = self.run_once("Project Cedar: 10 occupants, 300 cubic metres per hour.")
        detail = db.get_run_result_detail(run["result_id"])
        self.assertEqual(detail["pair_id"], "P-01")
        self.assertEqual(detail["target_app"], "Prateek")
        self.assertTrue(detail["checks"])
        self.assertTrue(all(c["result_id"] == run["result_id"] for c in detail["checks"]))

    def test_summary_loads_checks_in_one_batch(self):
        # No quoted phrases and no judge: these runs are genuinely NEEDS REVIEW, the only kind that needs checks
        db.create_golden_pair("X-10", "Prateek", "q", "Should describe the project accurately.")
        runs = [self.run_once("Some answer.", pair_id="X-10") for _ in range(3)]
        self.assertTrue(all(r["overall_status"] == "NEEDS REVIEW" for r in runs))
        with patch("db.get_run_result_detail", wraps=db.get_run_result_detail) as per_run, \
                patch("db.get_checks_for_results", wraps=db.get_checks_for_results) as batch:
            self.client.get("/api/summary")
        per_run.assert_not_called()
        self.assertEqual(batch.call_count, 1)

    def test_sqlite_connections_are_closed(self):
        with db._get_sqlite_conn() as conn:
            conn.execute("SELECT 1")
        with self.assertRaises(Exception):
            conn.execute("SELECT 1")


# ----------------------------------------------------
# H4 + M1: false PASS and false FAIL in existing checks
# ----------------------------------------------------

class CheckAccuracy(unittest.TestCase):

    def setUp(self):
        env = patch.dict(os.environ, NO_MODELS, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def statuses(self, pair_id, raw, expected="expected"):
        target = "Prateek" if pair_id.startswith("P") else "Anupma"
        resp = AdapterResponse(success=True, status_code=200, latency_ms=10.0, raw_response=raw)
        return {c.check_name: c.status for c in evaluate_response(pair_id, target, "q", expected, resp).checks}

    def test_p01_numbers_must_match_exactly(self):
        wrong = self.statuses("P-01", "Project Cedar (built 2010) holds 100 people and 3000 m3/h.")
        self.assertEqual(wrong["Occupancy Fact (10 occupants)"], "FAIL")
        self.assertEqual(wrong["Supply Airflow Fact (300 m³/h)"], "FAIL")
        right = self.statuses("P-01", "Project Cedar assumes 10 occupants and 300 cubic metres per hour.")
        self.assertEqual(right["Occupancy Fact (10 occupants)"], "PASS")
        self.assertEqual(right["Supply Airflow Fact (300 m³/h)"], "PASS")

    def test_p04_needs_elm_and_the_figure_not_just_sun(self):
        vague = self.statuses("P-04", "The sun is important for daylight in homes.")
        self.assertNotEqual(vague["Valid Retrieval Content"], "PASS")
        right = self.statuses("P-04", "Project Elm requires at least 2.5 hours of direct sun on 21 March (Section 3.1).")
        self.assertEqual(right["Valid Retrieval Content"], "PASS")
        self.assertEqual(right["Section 3.1 Cited"], "PASS")

    def test_apostrophes_are_not_required_phrases(self):
        expected = 'Don\'t invent the user\'s city. Must include "Lisbon".'
        result = self.statuses("X-01", "Your Lisbon itinerary.", expected)
        self.assertEqual(result["Required Expected Phrases"], "PASS")

    def test_curly_double_quotes_still_count(self):
        result = self.statuses("X-01", "Your Lisbon itinerary.", "Must include “Porto”.")
        self.assertEqual(result["Required Expected Phrases"], "FAIL")


# ----------------------------------------------------
# Security headers, docs, schema RLS, deploy entry
# ----------------------------------------------------

class SecurityAndDeployment(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from main import app
        cls.client = TestClient(app)

    def test_security_headers_on_ui_and_api(self):
        for path in ("/", "/api/health"):
            headers = self.client.get(path).headers
            self.assertEqual(headers["x-content-type-options"], "nosniff", path)
            self.assertEqual(headers["x-frame-options"], "DENY", path)
            self.assertIn("frame-ancestors 'none'", headers["content-security-policy"], path)
            self.assertIn("script-src 'self'", headers["content-security-policy"], path)

    def test_vercel_config_routes_to_main_and_disables_git_deployments(self):
        import json
        config = json.loads((PROJECT_ROOT / "vercel.json").read_text())
        self.assertEqual(config["git"], {"deploymentEnabled": False}, "pushes must never deploy automatically")
        self.assertEqual(config["builds"][0]["src"], "main.py")
        self.assertEqual(config["routes"], [{"src": "/(.*)", "dest": "main.py"}])
        ignored = (PROJECT_ROOT / ".vercelignore").read_text().split()
        for entry in (".env", "*.db", ".venv/", "tests/", ".vscode/"):
            self.assertIn(entry, ignored)

    def test_schema_enables_row_level_security_on_every_table(self):
        schema = (PROJECT_ROOT / "schema.sql").read_text().lower()
        for table in ("golden_pairs", "run_results", "check_results"):
            self.assertIn(f"alter table {table} enable row level security", schema)

    def run_isolated(self, code, isolate_db=True, **env):
        """Runs code in a fresh interpreter. It gets a temporary database unless a test only reads the default path."""
        full_env = {**NO_MODELS, **env, "PYTHONPATH": str(PROJECT_ROOT)}
        with tempfile.TemporaryDirectory() as tmp:
            if isolate_db:
                full_env["MANTIS_SQLITE_PATH"] = str(Path(tmp) / "isolated.db")
            return subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=full_env,
                                  capture_output=True, text=True, timeout=120)

    def test_hosted_mode_hides_docs_and_skips_gradio(self):
        code = (
            "import sys, tempfile, os\n"
            "os.environ['MANTIS_SQLITE_PATH'] = os.path.join(tempfile.mkdtemp(), 'h.db')\n"
            "from fastapi.testclient import TestClient\n"
            "from main import app\n"
            "c = TestClient(app)\n"
            "print(c.get('/docs').status_code, c.get('/openapi.json').status_code, c.get('/').status_code,\n"
            "      c.get('/api/health').status_code, c.get('/classic/').status_code, 'gradio' in sys.modules)\n"
        )
        out = self.run_isolated(code, VERCEL="1", MANTIS_CLASSIC_UI="0")
        self.assertEqual(out.returncode, 0, out.stderr[-800:])
        self.assertEqual(out.stdout.split(), ["404", "404", "200", "200", "404", "False"])

    def test_serverless_runtime_counts_as_hosted_even_without_the_vercel_variable(self):
        # Vercel lets projects stop exposing VERCEL=1; the Lambda runtime variables are always present
        code = (
            "from fastapi.testclient import TestClient\n"
            "import db\n"
            "from main import app\n"
            "r = TestClient(app).post('/api/golden-pairs', json={'pair_id': 'H-01', 'target_app': 'Anupma', 'input': 'q', 'expected_behaviour': 'e'})\n"
            "print(db.is_hosted(), db.is_durable(), r.status_code)\n"
        )
        for marker in ({"LAMBDA_TASK_ROOT": "/var/task"}, {"AWS_LAMBDA_FUNCTION_NAME": "mantis"}, {"MANTIS_HOSTED": "1"}):
            with self.subTest(marker=list(marker)):
                out = self.run_isolated(code, MANTIS_CLASSIC_UI="0", **marker)
                self.assertEqual(out.returncode, 0, out.stderr[-800:])
                self.assertEqual(out.stdout.split()[-3:], ["True", "False", "403"])

    def test_classic_view_is_never_mounted_where_writes_need_protection(self):
        # /classic calls the evaluator and database directly, so it can't honour the access token
        code = (
            "import os, sys, tempfile\n"
            "os.environ['MANTIS_SQLITE_PATH'] = os.path.join(tempfile.mkdtemp(), 'c.db')\n"
            "from fastapi.testclient import TestClient\n"
            "from main import app\n"
            "print(TestClient(app).get('/classic/').status_code)\n"
        )
        protected = self.run_isolated(code, MANTIS_ADMIN_TOKEN="t")
        self.assertEqual(protected.returncode, 0, protected.stderr[-800:])
        self.assertEqual(protected.stdout.split()[-1], "404")
        open_local = self.run_isolated(code)
        self.assertEqual(open_local.stdout.split()[-1], "200", "local development without a token keeps /classic")

    def test_hosted_sqlite_never_writes_into_the_project_directory(self):
        # Reads the default path only (no writes), so it runs without a temporary database override
        code = "import db; print(db.SQLITE_PATH, db.is_durable())"
        for marker in ({"VERCEL": "1"}, {"LAMBDA_TASK_ROOT": "/var/task"}):
            with self.subTest(marker=list(marker)):
                out = self.run_isolated(code, isolate_db=False, **marker)
                self.assertEqual(out.returncode, 0, out.stderr[-800:])
                path, durable = out.stdout.split()[-2:]
                self.assertTrue(path.startswith("/tmp/"), path)
                self.assertEqual(durable, "False")


if __name__ == "__main__":
    unittest.main()
