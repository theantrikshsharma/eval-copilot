"""
Guards that keep every test away from the developer's real credentials and real Supabase project.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

import db
import judges


class TestIsolation(unittest.TestCase):

    def test_test_mode_is_on_and_credentials_are_absent(self):
        self.assertEqual(os.environ.get("MANTIS_TEST_MODE"), "1")
        for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY", "GROQ_API_KEY", "MANTIS_ADMIN_TOKEN"):
            self.assertNotIn(name, os.environ, f"{name} leaked into the test environment")

    def test_modules_did_not_read_the_env_file(self):
        self.assertTrue(db.TEST_MODE)
        self.assertFalse(db.is_using_supabase(), "tests must run on SQLite")
        self.assertIsNone(db.SUPABASE_URL)
        self.assertFalse(judges.is_judge_enabled())

    def test_a_real_supabase_url_is_refused_in_test_mode(self):
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT),
               "SUPABASE_URL": "https://abcdefghijklmnopqrst.supabase.co", "SUPABASE_SERVICE_KEY": "x.y.z"}
        out = subprocess.run([sys.executable, "-c", "import db"], cwd=PROJECT_ROOT, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("refusing to start", out.stderr)


if __name__ == "__main__":
    unittest.main()
