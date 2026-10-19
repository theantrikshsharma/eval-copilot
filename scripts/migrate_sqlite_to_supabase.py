"""
Copy MANTIS history from the local SQLite file into Supabase, safely and repeatably.

    python scripts/migrate_sqlite_to_supabase.py                           # dry run: backup + plan
    python scripts/migrate_sqlite_to_supabase.py --exclude-pair TEST-01    # leave a pair out of the plan
    python scripts/migrate_sqlite_to_supabase.py --apply [...]             # copy, repair, then verify

Guarantees:
- The SQLite file is opened read-only and never modified. A JSON backup of all three tables is written first.
- Everything is planned, read-only, before the first write. Any of these stops the run with nothing written:
  * an existing Supabase golden pair that differs from its local counterpart (never overwritten),
  * an excluded pair that still has runs or checks (excluding it would orphan them),
  * a Supabase run whose identity matches a local run but whose response, status or checks conflict,
  * a missing or unreadable timestamp (one is never invented).
- Runs are matched on (pair_id, run_id, timestamp instant). Re-running never duplicates them.
- An interrupted earlier run is repaired: if a migrated run is missing trailing checks and the ones present
  match exactly, only the missing checks are added.
- Supabase assigns new result IDs; each check is attached to its run's new ID.
- Verification compares every migrated run's response and status and every check's name, status and evidence.

Exit codes: 0 success (or dry run with no conflicts), 1 verification failed, 2 refused (nothing written).
Needs SUPABASE_URL and SUPABASE_SERVICE_KEY (in .env or the environment), with schema.sql already applied.
"""

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
PAGE = 1000
IN_CHUNK = 200
PAIR_FIELDS = ("target_app", "input", "expected_behaviour")
EXIT_OK, EXIT_VERIFY_FAILED, EXIT_REFUSED = 0, 1, 2

RunKey = Tuple[str, str, datetime]


class MigrationRefused(Exception):
    """Migration is unsafe as planned. Raised before anything is written."""


# ----------------------------------------------------
# Timestamps
# ----------------------------------------------------

def parse_timestamp(value: Any, where: str) -> datetime:
    """
    Parses SQLite and Postgres timestamps into an aware datetime. Zone-less values are UTC (SQLite's
    datetime('now') convention); explicit offsets, positive or negative, and 'Z' are honoured.
    A missing or unreadable value is refused rather than replaced.
    """
    if value is None or not str(value).strip():
        raise MigrationRefused(f"{where}: timestamp is missing; refusing to invent one.")
    text = str(value).strip().replace(" ", "T", 1)
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise MigrationRefused(f"{where}: unreadable timestamp {value!r}.") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


# ----------------------------------------------------
# Planning (pure: no I/O)
# ----------------------------------------------------

@dataclass
class Plan:
    excluded: Dict[str, Tuple[int, int]] = field(default_factory=dict)       # pair_id -> (runs, checks)
    pairs_to_add: List[dict] = field(default_factory=list)
    pairs_present: List[str] = field(default_factory=list)
    pair_conflicts: Dict[str, List[str]] = field(default_factory=dict)       # pair_id -> differing fields
    runs_to_add: List[dict] = field(default_factory=list)
    runs_complete: List[dict] = field(default_factory=list)
    runs_to_repair: List[Tuple[dict, int, List[dict]]] = field(default_factory=list)  # local, remote id, missing
    run_conflicts: List[str] = field(default_factory=list)
    checks_by_run: Dict[int, List[dict]] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.pair_conflicts or self.run_conflicts)

    def checks_to_add(self) -> int:
        return sum(len(self.checks_by_run.get(r["result_id"], [])) for r in self.runs_to_add) + \
            sum(len(missing) for _, _, missing in self.runs_to_repair)


def check_signature(check: dict) -> Tuple[str, str, str]:
    return check["check_name"], check["status"], check.get("evidence_or_reason") or ""


def run_key(run: dict, where: str) -> RunKey:
    return run["pair_id"], run["run_id"], parse_timestamp(run["timestamp"], where).astimezone(timezone.utc)


def build_plan(source: dict, remote_pairs: Dict[str, dict], remote_runs: List[dict],
               remote_checks: Dict[int, List[dict]], exclude: List[str]) -> Plan:
    """
    Decides exactly what to write. remote_checks holds Supabase checks for remote runs, ordered by
    check_result_id. Raises MigrationRefused for problems that make any write unsafe.
    """
    plan = Plan()
    local_pairs = {p["pair_id"]: p for p in source["golden_pairs"]}
    for c in source["check_results"]:
        plan.checks_by_run.setdefault(c["result_id"], []).append(c)

    for pair_id in exclude:
        if pair_id not in local_pairs:
            raise MigrationRefused(f"--exclude-pair {pair_id}: no such golden pair in the local database.")
        runs = [r for r in source["run_results"] if r["pair_id"] == pair_id]
        checks = sum(len(plan.checks_by_run.get(r["result_id"], [])) for r in runs)
        if runs or checks:
            raise MigrationRefused(f"--exclude-pair {pair_id}: it has {len(runs)} runs and {checks} checks; "
                                   f"excluding it would leave them behind.")
        plan.excluded[pair_id] = (0, 0)

    pairs = [p for p in source["golden_pairs"] if p["pair_id"] not in plan.excluded]
    runs = [r for r in source["run_results"] if r["pair_id"] not in plan.excluded]

    # Every timestamp that will be written or matched must be readable, before anything is written
    problems = []
    for p in pairs:
        try:
            parse_timestamp(p["created_at"], f"golden pair {p['pair_id']} created_at")
        except MigrationRefused as e:
            problems.append(str(e))
    for r in runs:
        try:
            parse_timestamp(r["timestamp"], f"local run {r['result_id']} timestamp")
        except MigrationRefused as e:
            problems.append(str(e))
        for c in plan.checks_by_run.get(r["result_id"], []):
            try:
                parse_timestamp(c["created_at"], f"local check {c['check_result_id']} created_at")
            except MigrationRefused as e:
                problems.append(str(e))
    if problems:
        raise MigrationRefused("; ".join(problems))

    for p in pairs:
        existing = remote_pairs.get(p["pair_id"])
        if existing is None:
            plan.pairs_to_add.append(p)
            continue
        differing = [f for f in PAIR_FIELDS if existing.get(f) != p[f]]
        if differing:
            plan.pair_conflicts[p["pair_id"]] = differing
        else:
            plan.pairs_present.append(p["pair_id"])

    remote_by_key: Dict[RunKey, List[dict]] = {}
    for r in remote_runs:
        remote_by_key.setdefault(run_key(r, f"Supabase run {r['result_id']} timestamp"), []).append(r)

    for run in runs:
        matches = remote_by_key.get(run_key(run, f"local run {run['result_id']} timestamp"), [])
        label = f"local run {run['result_id']} ({run['pair_id']})"
        if not matches:
            plan.runs_to_add.append(run)
            continue
        if len(matches) > 1:
            plan.run_conflicts.append(f"{label}: matches {len(matches)} Supabase runs "
                                      f"({', '.join(str(m['result_id']) for m in matches)}); can't tell which is the copy.")
            continue
        remote = matches[0]
        differing = [f for f in ("overall_status", "actual_response") if (remote.get(f) or "") != (run.get(f) or "")]
        if differing:
            plan.run_conflicts.append(f"{label}: Supabase run {remote['result_id']} has a different {', '.join(differing)}.")
            continue
        local_checks = plan.checks_by_run.get(run["result_id"], [])
        local_sigs = [check_signature(c) for c in local_checks]
        remote_sigs = [check_signature(c) for c in remote_checks.get(remote["result_id"], [])]
        if remote_sigs == local_sigs:
            plan.runs_complete.append(run)
        elif len(remote_sigs) < len(local_sigs) and local_sigs[:len(remote_sigs)] == remote_sigs:
            plan.runs_to_repair.append((run, remote["result_id"], local_checks[len(remote_sigs):]))
        else:
            plan.run_conflicts.append(f"{label}: Supabase run {remote['result_id']} has checks that don't match "
                                      f"the local ones ({len(remote_sigs)} vs {len(local_sigs)}); not repairing automatically.")
    return plan


def describe_plan(plan: Plan) -> List[str]:
    included_pairs = len(plan.pairs_to_add) + len(plan.pairs_present) + len(plan.pair_conflicts)
    considered_runs = len(plan.runs_to_add) + len(plan.runs_complete) + len(plan.runs_to_repair) + len(plan.run_conflicts)
    lines = [
        f"Golden pairs: {len(plan.pairs_to_add)} to add, {len(plan.pairs_present)} already present and identical "
        f"(of {included_pairs} included).",
    ]
    for pair_id, (runs, checks) in plan.excluded.items():
        lines.append(f"Excluded: {pair_id} ({runs} runs, {checks} checks). Not copied; left untouched locally.")
    lines.append(
        f"Runs: {len(plan.runs_to_add)} to add, {len(plan.runs_complete)} already complete, "
        f"{len(plan.runs_to_repair)} to repair (of {considered_runs} included); {plan.checks_to_add()} checks to write."
    )
    for pair_id, fields in plan.pair_conflicts.items():
        lines.append(f"MISMATCH golden pair {pair_id}: {', '.join(fields)} differ in Supabase. Never overwritten.")
    lines.extend(f"CONFLICT {c}" for c in plan.run_conflicts)
    return lines


# ----------------------------------------------------
# Supabase I/O
# ----------------------------------------------------

def read_sqlite(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {
            table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            for table, order in (("golden_pairs", "pair_id"), ("run_results", "result_id"), ("check_results", "check_result_id"))
        }
    finally:
        conn.close()


def fetch_all(build_query) -> list:
    rows, start = [], 0
    while True:
        page = build_query().range(start, start + PAGE - 1).execute().data or []
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        start += PAGE


def fetch_remote(client) -> Tuple[Dict[str, dict], List[dict], Dict[int, List[dict]]]:
    pairs = {p["pair_id"]: p for p in fetch_all(
        lambda: client.table("golden_pairs").select("pair_id,target_app,input,expected_behaviour").order("pair_id"))}
    runs = fetch_all(lambda: client.table("run_results")
                     .select("result_id,pair_id,run_id,timestamp,overall_status,actual_response").order("result_id"))
    checks: Dict[int, List[dict]] = {}
    ids = [r["result_id"] for r in runs]
    for i in range(0, len(ids), IN_CHUNK):
        chunk = ids[i:i + IN_CHUNK]
        for c in fetch_all(lambda: client.table("check_results")
                           .select("check_result_id,result_id,check_name,status,evidence_or_reason")
                           .in_("result_id", chunk).order("check_result_id")):
            checks.setdefault(c["result_id"], []).append(c)
    return pairs, runs, checks


def check_rows(result_id: int, checks: List[dict]) -> List[dict]:
    return [{"result_id": result_id, "check_name": c["check_name"], "status": c["status"],
             "evidence_or_reason": c["evidence_or_reason"],
             "created_at": utc_iso(parse_timestamp(c["created_at"], f"local check {c['check_result_id']}"))}
            for c in checks]


def apply_plan(client, plan: Plan):
    if plan.blocked:
        raise MigrationRefused("plan has conflicts")
    for p in plan.pairs_to_add:
        client.table("golden_pairs").insert({
            **{k: p[k] for k in ("pair_id", *PAIR_FIELDS)},
            "created_at": utc_iso(parse_timestamp(p["created_at"], f"golden pair {p['pair_id']}")),
        }).execute()
    for run in plan.runs_to_add:
        res = client.table("run_results").insert({
            "run_id": run["run_id"], "pair_id": run["pair_id"], "actual_response": run["actual_response"],
            "overall_status": run["overall_status"],
            "timestamp": utc_iso(parse_timestamp(run["timestamp"], f"local run {run['result_id']}")),
        }).execute()
        new_id = res.data[0]["result_id"]
        rows = check_rows(new_id, plan.checks_by_run.get(run["result_id"], []))
        if rows:
            try:
                client.table("check_results").insert(rows).execute()  # one statement: all or nothing
            except Exception:
                client.table("run_results").delete().eq("result_id", new_id).execute()
                raise
    for run, remote_id, missing in plan.runs_to_repair:
        # Appends only the verified-missing tail; the existing run and its matching checks are left as they are
        client.table("check_results").insert(check_rows(remote_id, missing)).execute()


def verify(client, source: dict, excluded: List[str]) -> List[str]:
    """Every included pair and run must exist exactly once with identical content and identical checks."""
    pairs, runs, checks = fetch_remote(client)
    by_key: Dict[RunKey, List[dict]] = {}
    for r in runs:
        by_key.setdefault(run_key(r, f"Supabase run {r['result_id']}"), []).append(r)
    local_checks: Dict[int, List[dict]] = {}
    for c in source["check_results"]:
        local_checks.setdefault(c["result_id"], []).append(c)
    problems = []
    for p in source["golden_pairs"]:
        if p["pair_id"] in excluded:
            continue
        remote = pairs.get(p["pair_id"])
        if not remote or any(remote[f] != p[f] for f in PAIR_FIELDS):
            problems.append(f"golden pair {p['pair_id']} missing or different")
    for run in source["run_results"]:
        if run["pair_id"] in excluded:
            continue
        matches = by_key.get(run_key(run, f"local run {run['result_id']}"), [])
        label = f"local run {run['result_id']} ({run['pair_id']})"
        if len(matches) != 1:
            problems.append(f"{label}: found {len(matches)} copies in Supabase")
            continue
        remote = matches[0]
        for f in ("overall_status", "actual_response"):
            if (remote.get(f) or "") != (run.get(f) or ""):
                problems.append(f"{label}: {f} differs")
        local_sigs = [check_signature(c) for c in local_checks.get(run["result_id"], [])]
        remote_sigs = [check_signature(c) for c in checks.get(remote["result_id"], [])]
        if local_sigs != remote_sigs:
            problems.append(f"{label}: checks differ (names, statuses or evidence)")
    return problems


# ----------------------------------------------------
# CLI
# ----------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write to Supabase (default is a dry run)")
    parser.add_argument("--sqlite", type=Path, default=ROOT / "eval_copilot.db", help="source SQLite file")
    parser.add_argument("--exclude-pair", action="append", default=[], metavar="PAIR_ID",
                        help="leave this golden pair out (it must have no runs); repeatable")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "backups", help="where the JSON backup goes")
    args = parser.parse_args(argv)

    if not os.getenv("MANTIS_TEST_MODE"):  # tests never read the developer's .env
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (values are never printed).", file=sys.stderr)
        return EXIT_REFUSED
    from supabase import create_client
    client = create_client(url, key)

    source = read_sqlite(args.sqlite)
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    backup = args.backup_dir / f"mantis-sqlite-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    backup.write_text(json.dumps(source, indent=1, ensure_ascii=False))
    print(f"Backup written: {backup} ({len(source['golden_pairs'])} pairs, "
          f"{len(source['run_results'])} runs, {len(source['check_results'])} checks)")

    try:
        plan = build_plan(source, *fetch_remote(client), exclude=args.exclude_pair)
    except MigrationRefused as e:
        print(f"Refused, nothing written: {e}")
        return EXIT_REFUSED
    print("Plan:")
    for line in describe_plan(plan):
        print(f"  {line}")
    if plan.blocked:
        print("Refused, nothing written: resolve the mismatches above first." if args.apply
              else "Dry run: nothing written. --apply will refuse until the mismatches above are resolved.")
        return EXIT_REFUSED
    if not args.apply:
        print("Dry run: nothing written. Re-run with --apply to copy.")
        return EXIT_OK

    apply_plan(client, plan)
    print(f"Wrote {len(plan.pairs_to_add)} golden pairs, {len(plan.runs_to_add)} runs and "
          f"{plan.checks_to_add()} checks ({len(plan.runs_to_repair)} runs repaired).")
    problems = verify(client, source, args.exclude_pair)
    included = sum(1 for r in source["run_results"] if r["pair_id"] not in args.exclude_pair)
    failed_runs = {p.split(":")[0] for p in problems if p.startswith("local run")}
    for problem in problems:
        print(f"VERIFY FAILED: {problem}")
    print(f"Verified {included - len(failed_runs)}/{included} runs: "
          f"responses, statuses and every check's name, status and evidence.")
    return EXIT_VERIFY_FAILED if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
