"""
Supabase drops idle HTTP/2 connections; a warm Vercel instance can reuse one and get
httpx.RemoteProtocolError("Server disconnected"), which surfaced as HTTP 500 on /api/results after a run.
Reads retry once on a fresh connection; writes never retry, so a run is never stored twice. No network.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

import httpx

import db


def query(*outcomes):
    """A query whose execute() raises or returns each outcome in turn."""
    q = MagicMock()
    q.execute.side_effect = [o if isinstance(o, Exception) else MagicMock(data=o) for o in outcomes]
    return q


class TestReadRetry(unittest.TestCase):

    def test_stale_connection_is_retried_once(self):
        q = query(httpx.RemoteProtocolError("Server disconnected"), [{"pair_id": "P-01"}])
        self.assertEqual(db._execute_read(q).data, [{"pair_id": "P-01"}])
        self.assertEqual(q.execute.call_count, 2)

    def test_other_transport_drops_are_retried(self):
        for error in (httpx.ReadError("reset"), httpx.ConnectError("refused")):
            with self.subTest(error=type(error).__name__):
                q = query(error, [])
                db._execute_read(q)
                self.assertEqual(q.execute.call_count, 2)

    def test_a_second_failure_is_raised_not_retried_forever(self):
        q = query(httpx.RemoteProtocolError("a"), httpx.RemoteProtocolError("b"))
        with self.assertRaises(httpx.RemoteProtocolError):
            db._execute_read(q)
        self.assertEqual(q.execute.call_count, 2)

    def test_non_transport_errors_are_not_retried(self):
        q = query(ValueError("bad query"))
        with self.assertRaises(ValueError):
            db._execute_read(q)
        self.assertEqual(q.execute.call_count, 1)

    def test_paged_reads_retry_the_failed_page(self):
        first = query(httpx.RemoteProtocolError("Server disconnected"), [{"result_id": 1}])
        with patch.object(db, "SUPABASE_PAGE_SIZE", 1000):
            rows = db._fetch_all(lambda: MagicMock(range=lambda a, b: first))
        self.assertEqual(rows, [{"result_id": 1}])

    def test_single_row_reads_retry(self):
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value = query(
            httpx.RemoteProtocolError("Server disconnected"), [{"pair_id": "P-01", "target_app": "Prateek"}])
        with patch.object(db, "_using_supabase", True), patch.object(db, "_supabase_client", client):
            self.assertEqual(db.get_golden_pair("P-01")["target_app"], "Prateek")


class TestWritesNeverRetry(unittest.TestCase):

    def test_a_dropped_run_insert_is_raised_once_and_not_repeated(self):
        client = MagicMock()
        insert = client.table.return_value.insert.return_value
        insert.execute.side_effect = httpx.RemoteProtocolError("Server disconnected")
        with patch.object(db, "_using_supabase", True), patch.object(db, "_supabase_client", client):
            with self.assertRaises(httpx.RemoteProtocolError):
                db.save_run_result("r", "P-01", "answer", "PASS", [])
        self.assertEqual(insert.execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
