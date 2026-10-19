"""
Copy MANTIS history from the local SQLite file into Supabase, safely and repeatably.

    python scripts/migrate_sqlite_to_supabase.py            # dry run: backup + plan, writes nothing
    python scripts/migrate_sqlite_to_supabase.py --apply    # copy what's missing, then verify

Guarantees:
- The SQLite file is opened read-only and never modified.
- A JSON backup of all three tables is written first (backups/, gitignored).
- Golden pairs are insert-only: an existing pair with different content is reported, never overwritten.
- Runs are matched on (pair_id, run_id, timestamp), so re-running never duplicates them.
- Supabase assigns new result IDs; every check is attached to its run's new ID, so relationships hold.
- Stored statuses and evidence are copied exactly. Nothing is re-scored.

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY (in .env or the environment), with schema.sql already applied.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
PAGE = 1000


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


def as_utc(ts: str) -> str:
    """SQLite's datetime('now') has no zone; it is UTC. Make that explicit for Postgres."""
    return ts if ts and ("+" in ts[10:] or ts.endswith("Z")) else f"{ts.replace(' ', 'T')}+00:00"


def fetch_all(client, table: str, columns: str) -> list:
    rows, start = [], 0
    while True:
        page = client.table(table).select(columns).order(columns.split(",")[0]).range(start, start + PAGE - 1).execute().data or []
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        start += PAGE


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write to Supabase (default is a dry run)")
    parser.add_argument("--sqlite", type=Path, default=ROOT / "eval_copilot.db", help="source SQLite file")
    args = parser.parse_args()

    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (values are never printed).")
    from supabase import create_client
    client = create_client(url, key)

    source = read_sqlite(args.sqlite)
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"mantis-sqlite-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    backup.write_text(json.dumps(source, indent=1, ensure_ascii=False))
    print(f"Backup written: {backup.relative_to(ROOT)} "
          f"({len(source['golden_pairs'])} pairs, {len(source['run_results'])} runs, {len(source['check_results'])} checks)")

    remote_pairs = {p["pair_id"]: p for p in fetch_all(client, "golden_pairs", "pair_id,target_app,input,expected_behaviour")}
    remote_runs = {(r["pair_id"], r["run_id"], r["timestamp"]): r["result_id"]
                   for r in fetch_all(client, "run_results", "result_id,pair_id,run_id,timestamp")}

    def remote_key(run):
        # Postgres normalises timestamps, so compare on the parsed instant
        return run["pair_id"], run["run_id"], datetime.fromisoformat(as_utc(run["timestamp"])).astimezone(timezone.utc)

    remote_instants = {(p, r, datetime.fromisoformat(t).astimezone(timezone.utc)): rid for (p, r, t), rid in remote_runs.items()}
    checks_by_run = {}
    for c in source["check_results"]:
        checks_by_run.setdefault(c["result_id"], []).append(c)

    new_pairs, conflicts = [], []
    for p in source["golden_pairs"]:
        existing = remote_pairs.get(p["pair_id"])
        if not existing:
            new_pairs.append(p)
        elif any(existing[k] != p[k] for k in ("target_app", "input", "expected_behaviour")):
            conflicts.append(p["pair_id"])
    new_runs = [r for r in source["run_results"] if remote_key(r) not in remote_instants]

    print(f"Plan: {len(new_pairs)} golden pairs to add, {len(source['golden_pairs']) - len(new_pairs)} already present; "
          f"{len(new_runs)} runs to add, {len(source['run_results']) - len(new_runs)} already present.")
    if conflicts:
        print(f"Not overwritten (Supabase has different content): {', '.join(conflicts)}")
    if not args.apply:
        print("Dry run: nothing written. Re-run with --apply to copy.")
        return

    for p in new_pairs:
        client.table("golden_pairs").insert({**{k: p[k] for k in ("pair_id", "target_app", "input", "expected_behaviour")},
                                             "created_at": as_utc(p["created_at"])}).execute()
    for run in new_runs:
        res = client.table("run_results").insert({
            "run_id": run["run_id"], "pair_id": run["pair_id"], "actual_response": run["actual_response"],
            "overall_status": run["overall_status"], "timestamp": as_utc(run["timestamp"]),
        }).execute()
        new_id = res.data[0]["result_id"]
        rows = [{"result_id": new_id, "check_name": c["check_name"], "status": c["status"],
                 "evidence_or_reason": c["evidence_or_reason"], "created_at": as_utc(c["created_at"])}
                for c in checks_by_run.get(run["result_id"], [])]
        if rows:
            try:
                client.table("check_results").insert(rows).execute()
            except Exception:
                client.table("run_results").delete().eq("result_id", new_id).execute()
                raise
    print(f"Copied {len(new_pairs)} golden pairs and {len(new_runs)} runs.")

    # Verify: every source run exists remotely with the same status and the same checks
    remote_runs = {(r["pair_id"], r["run_id"], datetime.fromisoformat(r["timestamp"]).astimezone(timezone.utc)): r
                   for r in fetch_all(client, "run_results", "result_id,pair_id,run_id,timestamp,overall_status")}
    remote_checks = {}
    for c in fetch_all(client, "check_results", "check_result_id,result_id,check_name,status"):
        remote_checks.setdefault(c["result_id"], []).append((c["check_name"], c["status"]))
    problems = 0
    for run in source["run_results"]:
        remote = remote_runs.get(remote_key(run))
        local_checks = [(c["check_name"], c["status"]) for c in checks_by_run.get(run["result_id"], [])]
        if not remote or remote["overall_status"] != run["overall_status"] or remote_checks.get(remote["result_id"], []) != local_checks:
            problems += 1
            print(f"MISMATCH: local run {run['result_id']} ({run['pair_id']})")
    print(f"Verified {len(source['run_results']) - problems}/{len(source['run_results'])} runs match, including their checks.")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
