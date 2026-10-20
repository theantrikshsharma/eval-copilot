"""
Opt-in live contract tests for Prateek's /api/ans_fact request validation, against the documented OpenAPI schema
(Question: {"text": string}, required). Skipped unless MANTIS_LIVE=1.

The target is a shared production server: two requests in total, sent one after the other with a pause, no
retries. Neither request reaches the retriever, because validation rejects it first.

    MANTIS_LIVE=1 python -m unittest tests/test_prateek_contract_live.py
"""

import os
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

import requests

from adapters import PrateekAdapter

PAUSE_SECONDS = 3


@unittest.skipUnless(os.getenv("MANTIS_LIVE") == "1", "live contract tests make real requests; set MANTIS_LIVE=1")
class TestPrateekRequestValidation(unittest.TestCase):

    def post(self, body):
        time.sleep(PAUSE_SECONDS)
        return requests.post(PrateekAdapter.ANS_FACT_URL, json=body, timeout=20)

    def assertValidationError(self, resp, error_type):
        self.assertEqual(resp.status_code, 422, resp.text[:300])
        self.assertTrue(resp.headers.get("content-type", "").startswith("application/json"))
        detail = resp.json()["detail"]
        self.assertEqual(len(detail), 1, detail)
        self.assertEqual(detail[0]["type"], error_type)
        self.assertEqual(detail[0]["loc"], ["body", "text"])

    def test_missing_text_field_is_rejected(self):
        self.assertValidationError(self.post({}), "missing")

    def test_non_string_text_field_is_rejected(self):
        self.assertValidationError(self.post({"text": 123}), "string_type")


if __name__ == "__main__":
    unittest.main()
