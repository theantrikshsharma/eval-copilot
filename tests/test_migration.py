"""
Tests for scripts/migrate_sqlite_to_supabase.py.

- Planning and timestamps: offline, with hand-built data.
- End to end: against a scratch Postgres + PostgREST only. Skipped unless MANTIS_SCRATCH_SUPABASE_URL,
  MANTIS_SCRATCH_SUPABASE_KEY and MANTIS_SCRATCH_PGDATABASE are set, and refused unless the URL is localhost.
  The source is always a temporary copy of eval_copilot.db, never the real file.
"""

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

SCRIPT = PROJECT_ROOT / "scripts" / "migrate_sqlite_to_supabase.py"
_spec = importlib.util.spec_from_file_location("migrate", SCRIPT)
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)
Refused = migrate.MigrationRefused


# ----------------------------------------------------
# Timestamps
# ----------------------------------------------------

class Timestamps(unittest.TestCase):

    def instant(self, value):
        return migrate.parse_timestamp(value, "t").astimezone(timezone.utc)

    def test_zone_less_values_are_utc(self):
        self.assertEqual(self.instant("2026-10-19 15:41:32"), datetime(2026, 10, 19, 15, 41, 32, tzinfo=timezone.utc))

    def test_z_positive_and_negative_offsets_are_honoured(self):
        expected = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)
        for value in ("2026-10-19T15:00:00Z", "2026-10-19T15:00:00+00:00",
                      "2026-10-19T20:30:00+05:30", "2026-10-19T10:00:00-05:00"):
            with self.subTest(value=value):
                self.assertEqual(self.instant(value), expected)

    def test_fractional_seconds_survive(self):
        self.assertEqual(self.instant("2026-10-19T15:43:43.385739+00:00").microsecond, 385739)

    def test_missing_and_unreadable_values_are_refused(self):
        for value in (None, "", "   "):
            with self.subTest(value=value), self.assertRaisesRegex(Refused, "missing; refusing to invent"):
                migrate.parse_timestamp(value, "run 7")
        with self.assertRaisesRegex(Refused, "unreadable"):
            migrate.parse_timestamp("yesterday", "run 7")


# ----------------------------------------------------
# Planning
# ----------------------------------------------------

def pair(pid, **over):
    return {"pair_id": pid, "target_app": "Prateek", "input": f"q {pid}", "expected_behaviour": f"e {pid}",
            "created_at": "2026-10-19 15:41:32", **over}


def run(rid, pid, ts="2026-10-19T16:00:00+00:00", **over):
    return {"result_id": rid, "run_id": "r", "pair_id": pid, "actual_response": f"answer {rid}",
            "overall_status": "PASS", "timestamp": ts, **over}


def check(cid, rid, name, **over):
    return {"check_result_id": cid, "result_id": rid, "check_name": name, "status": "PASS",
            "evidence_or_reason": f"evidence {name}", "created_at": "2026-10-19T16:00:00+00:00", **over}


def source(pairs, runs, checks):
    return {"golden_pairs": pairs, "run_results": runs, "check_results": checks}


def remote_of(src, result_id_offset=100):
    """Supabase state equal to a perfect earlier migration of src (new IDs, same content)."""
    pairs = {p["pair_id"]: p for p in src["golden_pairs"]}
    runs = [{**r, "result_id": r["result_id"] + result_id_offset} for r in src["run_results"]]
    checks = {}
    for c in src["check_results"]:
        checks.setdefault(c["result_id"] + result_id_offset, []).append({**c, "result_id": c["result_id"] + result_id_offset})
    return pairs, runs, checks


LOCAL = source(
    [pair("A-01"), pair("P-01"), pair("TEST-01")],
    [run(1, "A-01"), run(2, "P-01", ts="2026-10-19T17:00:00+00:00")],
    [check(1, 1, "c1"), check(2, 1, "c2"), check(3, 1, "c3"), check(4, 2, "c1")],
)


class Planning(unittest.TestCase):

    def test_fresh_target_copies_everything(self):
        plan = migrate.build_plan(LOCAL, {}, [], {}, exclude=[])
        self.assertEqual([p["pair_id"] for p in plan.pairs_to_add], ["A-01", "P-01", "TEST-01"])
        self.assertEqual(len(plan.runs_to_add), 2)
        self.assertEqual(plan.checks_to_add(), 4)
        self.assertFalse(plan.blocked)

    def test_exclusion_needs_zero_runs_and_is_reported(self):
        plan = migrate.build_plan(LOCAL, {}, [], {}, exclude=["TEST-01"])
        self.assertEqual(plan.excluded, {"TEST-01": (0, 0)})
        self.assertNotIn("TEST-01", [p["pair_id"] for p in plan.pairs_to_add])
        self.assertIn("Excluded: TEST-01 (0 runs, 0 checks). Not copied; left untouched locally.", migrate.describe_plan(plan))

    def test_excluding_a_pair_with_runs_is_refused(self):
        with self.assertRaisesRegex(Refused, "P-01: it has 1 runs and 1 checks"):
            migrate.build_plan(LOCAL, {}, [], {}, exclude=["P-01"])

    def test_excluding_an_unknown_pair_is_refused(self):
        with self.assertRaisesRegex(Refused, "no such golden pair"):
            migrate.build_plan(LOCAL, {}, [], {}, exclude=["NOPE"])

    def test_identical_existing_pairs_are_present_and_mismatches_block_with_fields(self):
        remote_pairs = {"A-01": pair("A-01"), "P-01": pair("P-01", input="changed", target_app="Anupma")}
        plan = migrate.build_plan(LOCAL, remote_pairs, [], {}, exclude=["TEST-01"])
        self.assertEqual(plan.pairs_present, ["A-01"])
        self.assertEqual(plan.pair_conflicts, {"P-01": ["target_app", "input"]})
        self.assertTrue(plan.blocked)
        self.assertIn("MISMATCH golden pair P-01: target_app, input differ in Supabase. Never overwritten.",
                      migrate.describe_plan(plan))

    def test_a_perfect_earlier_migration_is_a_no_op(self):
        plan = migrate.build_plan(LOCAL, *remote_of(LOCAL), exclude=[])
        self.assertEqual((len(plan.pairs_to_add), len(plan.runs_to_add), len(plan.runs_complete)), (0, 0, 2))
        self.assertEqual(plan.checks_to_add(), 0)

    def test_run_with_no_checks_is_repaired_with_all_of_them(self):
        pairs, runs, checks = remote_of(LOCAL)
        del checks[101]
        plan = migrate.build_plan(LOCAL, pairs, runs, checks, exclude=[])
        (local_run, remote_id, missing), = plan.runs_to_repair
        self.assertEqual((local_run["result_id"], remote_id, [c["check_name"] for c in missing]), (1, 101, ["c1", "c2", "c3"]))

    def test_run_with_a_matching_prefix_gets_only_the_missing_tail(self):
        pairs, runs, checks = remote_of(LOCAL)
        checks[101] = checks[101][:1]
        plan = migrate.build_plan(LOCAL, pairs, runs, checks, exclude=[])
        (_, _, missing), = plan.runs_to_repair
        self.assertEqual([c["check_name"] for c in missing], ["c2", "c3"])
        self.assertEqual(plan.checks_to_add(), 2)

    def test_conflicting_checks_block_instead_of_being_repaired(self):
        for mutate in (lambda cs: cs[0].update(evidence_or_reason="edited"),   # same count, different evidence
                       lambda cs: cs.append({**cs[0], "check_result_id": 999}),  # extra check
                       lambda cs: cs.reverse()):                                 # same checks, wrong order
            with self.subTest(mutate=mutate):
                pairs, runs, checks = remote_of(LOCAL)
                mutate(checks[101])
                plan = migrate.build_plan(LOCAL, pairs, runs, checks, exclude=[])
                self.assertTrue(plan.blocked)
                self.assertFalse(plan.runs_to_repair)
                self.assertIn("not repairing automatically", plan.run_conflicts[0])

    def test_run_identity_matches_but_content_differs(self):
        pairs, runs, checks = remote_of(LOCAL)
        runs[0]["actual_response"] = "something else"
        plan = migrate.build_plan(LOCAL, pairs, runs, checks, exclude=[])
        self.assertIn("different actual_response", plan.run_conflicts[0])

    def test_ambiguous_duplicate_copies_block(self):
        pairs, runs, checks = remote_of(LOCAL)
        runs.append({**runs[0], "result_id": 555})
        plan = migrate.build_plan(LOCAL, pairs, runs, checks, exclude=[])
        self.assertIn("matches 2 Supabase runs", plan.run_conflicts[0])

    def test_offsets_identify_the_same_instant(self):
        local = source([pair("A-01")], [run(1, "A-01", ts="2026-10-19T11:00:00-05:00")], [check(1, 1, "c1")])
        pairs, runs, checks = remote_of(local)
        runs[0]["timestamp"] = "2026-10-19T16:00:00+00:00"
        plan = migrate.build_plan(local, pairs, runs, checks, exclude=[])
        self.assertEqual(len(plan.runs_complete), 1)

    def test_missing_timestamps_refuse_before_anything_is_written(self):
        for bad in (source([pair("A-01")], [run(1, "A-01", ts=None)], []),
                    source([pair("A-01")], [run(1, "A-01")], [check(1, 1, "c1", created_at=None)]),
                    source([pair("A-01", created_at="")], [], [])):
            with self.subTest(bad=bad), self.assertRaisesRegex(Refused, "missing; refusing to invent"):
                migrate.build_plan(bad, {}, [], {}, exclude=[])

    def test_a_blocked_plan_writes_nothing(self):
        client = MagicMock()
        plan = migrate.build_plan(LOCAL, {"A-01": pair("A-01", input="x")}, [], {}, exclude=[])
        with self.assertRaises(Refused):
            migrate.apply_plan(client, plan)
        client.table.assert_not_called()


# ----------------------------------------------------
# End to end against scratch Postgres + PostgREST
# ----------------------------------------------------

SCRATCH_URL = os.getenv("MANTIS_SCRATCH_SUPABASE_URL")
SCRATCH_KEY = os.getenv("MANTIS_SCRATCH_SUPABASE_KEY")
SCRATCH_DB = os.getenv("MANTIS_SCRATCH_PGDATABASE")
SCRATCH_READY = bool(SCRATCH_URL and SCRATCH_KEY and SCRATCH_DB)


@unittest.skipUnless(SCRATCH_READY, "scratch Postgres + PostgREST not configured")
class EndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if urlparse(SCRATCH_URL).hostname not in ("127.0.0.1", "localhost") or not SCRATCH_DB.startswith("mantis_"):
            raise RuntimeError("scratch settings must point at a local mantis_* database; refusing")
        seed = (PROJECT_ROOT / "schema.sql").read_text()
        cls.seed_sql = seed[seed.index("insert into golden_pairs"):seed.index("on conflict (pair_id) do nothing;") + 33]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Path(self.tmp.name) / "source.db"
        shutil.copy(PROJECT_ROOT / "eval_copilot.db", self.fixture)  # never the real file
        self.psql("TRUNCATE check_results, run_results, golden_pairs RESTART IDENTITY CASCADE;" + self.seed_sql)

    def psql(self, sql):
        return subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-Atq", "-d", SCRATCH_DB, "-c", sql],
                              capture_output=True, text=True, check=True).stdout.strip()

    def counts(self):
        return tuple(int(self.psql(f"select count(*) from {t}")) for t in ("golden_pairs", "run_results", "check_results"))

    def migrate(self, *args):
        env = {**os.environ, "MANTIS_TEST_MODE": "1", "SUPABASE_URL": SCRATCH_URL, "SUPABASE_SERVICE_KEY": SCRATCH_KEY}
        out = subprocess.run([sys.executable, str(SCRIPT), "--sqlite", str(self.fixture),
                              "--backup-dir", self.tmp.name, *args],
                             cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300)
        return out.returncode, out.stdout + out.stderr

    def local_counts(self, exclude="TEST-01"):
        conn = sqlite3.connect(f"file:{self.fixture}?mode=ro", uri=True)
        pairs = conn.execute("select count(*) from golden_pairs where pair_id != ?", (exclude,)).fetchone()[0]
        runs = conn.execute("select count(*) from run_results").fetchone()[0]
        checks = conn.execute("select count(*) from check_results").fetchone()[0]
        conn.close()
        return pairs, runs, checks

    def test_dry_run_excludes_test_01_and_writes_nothing(self):
        code, out = self.migrate("--exclude-pair", "TEST-01")
        self.assertEqual(code, 0, out)
        self.assertIn("Golden pairs: 0 to add, 8 already present and identical (of 8 included).", out)
        self.assertIn("Excluded: TEST-01 (0 runs, 0 checks).", out)
        self.assertIn("Runs: 35 to add, 0 already complete, 0 to repair (of 35 included); 157 checks to write.", out)
        self.assertEqual(self.counts(), (8, 0, 0))

    def test_apply_then_repeat_apply_is_exact_and_idempotent(self):
        code, out = self.migrate("--exclude-pair", "TEST-01", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("Verified 35/35 runs", out)
        self.assertEqual(self.counts(), self.local_counts())
        self.assertEqual(self.psql("select count(*) from golden_pairs where pair_id = 'TEST-01'"), "0")
        code, out = self.migrate("--exclude-pair", "TEST-01", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("Runs: 0 to add, 35 already complete, 0 to repair", out)
        self.assertEqual(self.counts(), self.local_counts())

    def test_mismatched_existing_pair_refuses_apply(self):
        self.psql("update golden_pairs set input = 'edited in Supabase' where pair_id = 'A-01'")
        code, out = self.migrate("--exclude-pair", "TEST-01", "--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("MISMATCH golden pair A-01: input differ", out)
        self.assertEqual(self.counts(), (8, 0, 0))
        self.assertEqual(self.psql("select input from golden_pairs where pair_id = 'A-01'"), "edited in Supabase")

    def test_interrupted_migration_is_repaired_without_duplicates(self):
        self.assertEqual(self.migrate("--exclude-pair", "TEST-01", "--apply")[0], 0)
        first, second = [int(x) for x in self.psql("select result_id from run_results order by result_id limit 2").split()]
        self.psql(f"delete from check_results where result_id = {first}")
        self.psql(f"delete from check_results where check_result_id in "
                  f"(select check_result_id from check_results where result_id = {second} order by check_result_id desc limit 2)")
        code, out = self.migrate("--exclude-pair", "TEST-01", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("2 to repair", out)
        self.assertIn("Verified 35/35 runs", out)
        self.assertEqual(self.counts(), self.local_counts())

    def test_conflicting_migrated_check_refuses_and_is_left_alone(self):
        self.assertEqual(self.migrate("--exclude-pair", "TEST-01", "--apply")[0], 0)
        self.psql("update check_results set evidence_or_reason = 'tampered' where check_result_id = 1")
        code, out = self.migrate("--exclude-pair", "TEST-01", "--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("not repairing automatically", out)
        self.assertEqual(self.psql("select evidence_or_reason from check_results where check_result_id = 1"), "tampered")
        self.assertEqual(self.counts(), self.local_counts())

    def test_excluding_a_pair_with_runs_is_refused(self):
        code, out = self.migrate("--exclude-pair", "P-01", "--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("--exclude-pair P-01: it has", out)
        self.assertEqual(self.counts(), (8, 0, 0))

    def test_missing_timestamp_refuses_with_nothing_written(self):
        conn = sqlite3.connect(self.fixture)  # the temporary copy only
        conn.execute("update run_results set timestamp = NULL where result_id = (select min(result_id) from run_results)")
        conn.commit()
        conn.close()
        code, out = self.migrate("--exclude-pair", "TEST-01", "--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("timestamp is missing; refusing to invent one", out)
        self.assertEqual(self.counts(), (8, 0, 0))

    def test_negative_offset_is_stored_as_the_same_instant(self):
        conn = sqlite3.connect(self.fixture)
        rid = conn.execute("select min(result_id) from run_results").fetchone()[0]
        conn.execute("update run_results set timestamp = '2026-10-19T10:43:43.385739-05:00' where result_id = ?", (rid,))
        conn.commit()
        conn.close()
        self.assertEqual(self.migrate("--exclude-pair", "TEST-01", "--apply")[0], 0)
        stored = self.psql("select to_char(timestamp at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') from run_results "
                           "where actual_response = (select actual_response from run_results order by result_id limit 1) "
                           "order by result_id limit 1")
        self.assertEqual(stored, "2026-10-19 15:43:43.385739")
        code, out = self.migrate("--exclude-pair", "TEST-01", "--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("0 to add, 35 already complete", out)


if __name__ == "__main__":
    unittest.main()
