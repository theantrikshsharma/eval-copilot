"""
Offline tests for how main.py serves the front end: the new UI at /, Gradio at /classic, the API at /api.
"""

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

from main import app


class TestWebRoutes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_root_serves_the_new_ui(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn('<script type="module" src="/assets/app.js">', resp.text)

    def test_assets_are_served_with_correct_types(self):
        for path, content_type in [
            ("/assets/app.js", "javascript"),
            ("/assets/lib.js", "javascript"),
            ("/assets/styles.css", "text/css"),
        ]:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertIn(content_type, resp.headers["content-type"], path)

    def test_test_files_are_not_published(self):
        self.assertEqual(self.client.get("/assets/lib.test.mjs").status_code, 404)

    def test_classic_gradio_ui_is_still_reachable(self):
        resp = self.client.get("/classic/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("gradio", resp.text.lower())

    def test_api_routes_are_unaffected(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "healthy")

    def test_ui_is_branded_mantis(self):
        page = self.client.get("/").text
        self.assertIn("<title>MANTIS</title>", page)
        self.assertIn('aria-label="MANTIS"', page)
        for path in ("/", "/assets/app.js", "/assets/lib.js", "/assets/styles.css"):
            self.assertNotIn("Eval Co-pilot", self.client.get(path).text, path)

    def test_api_and_classic_ui_carry_the_new_name(self):
        self.assertEqual(self.client.get("/openapi.json").json()["info"]["title"], "MANTIS API")
        self.assertIn("MANTIS", self.client.get("/classic/").text)

    def test_ui_never_contains_secrets(self):
        page = self.client.get("/").text + self.client.get("/assets/app.js").text
        for marker in ("gsk_", "sk-or-", "SUPABASE_SERVICE_KEY", "GROQ_API_KEY"):
            self.assertNotIn(marker, page)


if __name__ == "__main__":
    unittest.main()
