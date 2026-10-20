"""
Database Access Layer for MANTIS (Checkpoint 4).
Supports:
1. Supabase via official Supabase Python SDK (`supabase>=2.33`) when credentials are provided.
2. Local persistent SQLite database (`eval_copilot.db`) with identical schema when Supabase credentials are not set.

Tables:
- GOLDEN_PAIRS (pair_id, target_app, input, expected_behaviour, created_at)
- RUN_RESULTS (result_id, run_id, pair_id, actual_response, overall_status, timestamp)
- CHECK_RESULTS (check_result_id, result_id, check_name, status, evidence_or_reason, created_at)
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import httpx
from dotenv import load_dotenv

# Load environment variables. Tests (MANTIS_TEST_MODE, set by tests/__init__.py) never read the developer's .env.
ENV_PATH = Path(__file__).resolve().parent / ".env"
TEST_MODE = bool(os.getenv("MANTIS_TEST_MODE"))
if not TEST_MODE:
    load_dotenv(ENV_PATH)

# httpx (used by the Supabase client) logs every request URL, including the project URL and query filters,
# at INFO. Hosted platforms capture INFO logs, so keep only warnings and errors.
logging.getLogger("httpx").setLevel(logging.WARNING)

SUPABASE_URL = os.getenv("SUPABASE_URL")
# Accept either SUPABASE_SERVICE_KEY or SUPABASE_KEY
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

if TEST_MODE and SUPABASE_URL:
    from urllib.parse import urlparse
    if urlparse(SUPABASE_URL).hostname not in ("127.0.0.1", "localhost"):
        # Backstop: a test must never read or write a real Supabase project
        raise RuntimeError("MANTIS_TEST_MODE is set but SUPABASE_URL is not a local scratch database; refusing to start.")



# Vercel sets VERCEL/VERCEL_ENV unless a project turns system variables off; its functions run on AWS Lambda,
# whose runtime always sets LAMBDA_TASK_ROOT and AWS_LAMBDA_FUNCTION_NAME. MANTIS_HOSTED=1 forces hosted mode.
_HOSTED_MARKERS = ("MANTIS_HOSTED", "VERCEL", "VERCEL_ENV", "LAMBDA_TASK_ROOT", "AWS_LAMBDA_FUNCTION_NAME")


def is_hosted() -> bool:
    """True on a shared, internet-facing deployment, where writes need a token and storage must be durable."""
    return any(os.getenv(name) for name in _HOSTED_MARKERS)


def _default_sqlite_path() -> Path:
    """
    MANTIS_SQLITE_PATH overrides everything. On Vercel the project directory is read-only and /tmp is per-instance
    scratch space, so SQLite goes to /tmp there and is reported as non-durable (see is_durable).
    """
    if os.getenv("MANTIS_SQLITE_PATH"):
        return Path(os.environ["MANTIS_SQLITE_PATH"])
    if is_hosted():
        return Path("/tmp/mantis.db")
    return Path(__file__).resolve().parent / "eval_copilot.db"


SQLITE_PATH = _default_sqlite_path()
SUPABASE_PAGE_SIZE = 1000  # Supabase's default API row cap; reads page through it
SUPABASE_IN_CHUNK = 200    # keeps `in.(...)` filters well under URL length limits
TOP_FAILING_LIMIT = 10


class GoldenPairExists(Exception):
    """Raised when creating a golden pair whose ID is already taken. Golden pairs are never silently replaced."""

_supabase_client = None
_using_supabase = False

if SUPABASE_URL and SUPABASE_KEY and "<your-project-ref>" not in SUPABASE_URL:
    try:
        from supabase import create_client
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        _using_supabase = True
    except Exception as e:
        print(f"[DB Warning] Could not initialize Supabase client: {e}. Falling back to SQLite.")
        _using_supabase = False
else:
    _using_supabase = False


def is_using_supabase() -> bool:
    return _using_supabase


def is_durable() -> bool:
    """Whether saved runs survive restarts and are shared by every server instance."""
    return _using_supabase or not is_hosted()


def get_backend_status() -> Dict[str, Any]:
    """Public status: no URLs, paths or credentials."""
    if _using_supabase:
        return {"backend": "Supabase (PostgreSQL)", "connected": True, "durable": True}
    if is_durable():
        return {"backend": "SQLite (local file)", "connected": True, "durable": True}
    return {
        "backend": "SQLite (temporary)", "connected": True, "durable": False,
        "note": "This deployment has no durable database. Configure SUPABASE_URL and SUPABASE_SERVICE_KEY.",
    }


# ----------------------------------------------------
# SQLite Initialization & Helpers
# ----------------------------------------------------

def use_sqlite_path(path: Path, initialise: bool = True):
    """Point the SQLite backend at another file. Used by tests so they never touch real history."""
    global SQLITE_PATH
    SQLITE_PATH = Path(path)
    if initialise:
        init_db()


@contextmanager
def _get_sqlite_conn():
    """Commits on success, rolls back on error, and always closes the connection."""
    conn = sqlite3.connect(str(SQLITE_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------
# Supabase helpers
# ----------------------------------------------------

# Supabase closes idle HTTP/2 connections, and a warm serverless instance can reuse one ("Server disconnected").
# Reads are idempotent, so they retry once on a fresh connection. Writes never retry: a run must not be stored twice.
_DROPPED_CONNECTION = (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError)


def _execute_read(query):
    try:
        return query.execute()
    except _DROPPED_CONNECTION:
        return query.execute()


def _fetch_all(build_query) -> List[Dict[str, Any]]:
    """Reads every row by paging past Supabase's per-request row cap. build_query returns a fresh query."""
    rows: List[Dict[str, Any]] = []
    start = 0
    while True:
        page = _execute_read(build_query().range(start, start + SUPABASE_PAGE_SIZE - 1)).data or []
        rows.extend(page)
        if len(page) < SUPABASE_PAGE_SIZE:
            return rows
        start += SUPABASE_PAGE_SIZE


def _flatten_run(row: Dict[str, Any]) -> Dict[str, Any]:
    """Supabase nests the joined golden pair; SQLite returns flat columns. Return the SQLite shape from both."""
    pair = row.pop("golden_pairs", None) or {}
    for key in ("target_app", "input", "expected_behaviour"):
        row.setdefault(key, pair.get(key))
    return row


def init_db():
    """Initializes tables and seed data if not present."""
    if _using_supabase:
        # Supabase tables are created via schema.sql in Supabase SQL editor.
        # Seed golden pairs if empty
        try:
            res = _execute_read(_supabase_client.table("golden_pairs").select("pair_id").limit(1))
            if not res.data:
                _seed_initial_pairs_supabase()
        except Exception as e:
            print(f"[Supabase Check] Note: Tables might need to be created via schema.sql: {e}")
        return

    # SQLite table creation
    with _get_sqlite_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS golden_pairs (
            pair_id TEXT PRIMARY KEY,
            target_app TEXT NOT NULL,
            input TEXT NOT NULL,
            expected_behaviour TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS run_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            pair_id TEXT NOT NULL REFERENCES golden_pairs(pair_id) ON DELETE CASCADE,
            actual_response TEXT,
            overall_status TEXT NOT NULL CHECK (overall_status IN ('PASS', 'FAIL', 'ERROR', 'NEEDS REVIEW')),
            timestamp TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS check_results (
            check_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            result_id INTEGER NOT NULL REFERENCES run_results(result_id) ON DELETE CASCADE,
            check_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'ERROR', 'NEEDS REVIEW')),
            evidence_or_reason TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_run_results_pair ON run_results(pair_id);
        CREATE INDEX IF NOT EXISTS idx_run_results_run ON run_results(run_id);
        CREATE INDEX IF NOT EXISTS idx_check_results_result ON check_results(result_id);
        """)

        # Check if golden_pairs is seeded
        cur = conn.cursor()
        cur.execute("SELECT count(*) as cnt FROM golden_pairs")
        if cur.fetchone()["cnt"] == 0:
            _seed_initial_pairs_sqlite(conn)
        conn.commit()


def _seed_initial_pairs_sqlite(conn):
    seeds = [
        ("A-01", "Anupma", "Plan a 3-day trip to Dubai for 2 adults. Maximum 1 activity per day. No child activities.", "Generate a 3-day Dubai itinerary for 2 adults with strictly at most 1 activity per day and no child-oriented or family amenities/activities."),
        ("A-02", "Anupma", "", "Prompt the user to supply a required destination rather than hallucinating an arbitrary city (such as Barcelona)."),
        ("A-03", "Anupma", "Plan a trip to London from 2020-01-01 to 2020-01-03 for 2 adults.", "Reject the request or issue a validation warning because travel dates are in the past, rather than generating an itinerary for 2020."),
        ("A-04", "Anupma", "Plan a 2-day trip to Paris for 2 adults.", "Server responds with HTTP 200 within 10 seconds without disconnection or 5xx server errors."),
        ("P-01", "Prateek", "What is the supply airflow and assumed occupancy for the Project Cedar meeting room?", "Correctly extract that Project Cedar meeting room assumes 10 occupants and a supply airflow of 300 cubic metres per hour, citing the Project Cedar report."),
        ("P-02", "Prateek", "Compare the energy targets of Project Maple with the ventilation assumption of Project Cedar.", "Retrieve and state both Project Maple BENG targets (BENG 1 <= 60, BENG 2 <= 35, BENG 3 >= 45%) and Project Cedar ventilation (300 m3/h, 10 occupants), citing both distinct sources."),
        ("P-03", "Prateek", "Which project has rules regarding how much natural light must enter residential living rooms, and what is the required ratio?", "Identify Project Birch and report the daylight requirement of at least 12% equivalent daylight area of the floor area (Section 2.1)."),
        ("P-04", "Prateek", "What are the sun hours requirements for Project Elm?", "Server responds with HTTP 200 within 10 seconds and valid JSON answer citing Section 3.1 of Project Elm.")
    ]
    conn.executemany(
        "INSERT INTO golden_pairs (pair_id, target_app, input, expected_behaviour) VALUES (?, ?, ?, ?)",
        seeds
    )


def _seed_initial_pairs_supabase():
    seeds = [
        {"pair_id": "A-01", "target_app": "Anupma", "input": "Plan a 3-day trip to Dubai for 2 adults. Maximum 1 activity per day. No child activities.", "expected_behaviour": "Generate a 3-day Dubai itinerary for 2 adults with strictly at most 1 activity per day and no child-oriented or family amenities/activities."},
        {"pair_id": "A-02", "target_app": "Anupma", "input": "", "expected_behaviour": "Prompt the user to supply a required destination rather than hallucinating an arbitrary city (such as Barcelona)."},
        {"pair_id": "A-03", "target_app": "Anupma", "input": "Plan a trip to London from 2020-01-01 to 2020-01-03 for 2 adults.", "expected_behaviour": "Reject the request or issue a validation warning because travel dates are in the past, rather than generating an itinerary for 2020."},
        {"pair_id": "A-04", "target_app": "Anupma", "input": "Plan a 2-day trip to Paris for 2 adults.", "expected_behaviour": "Server responds with HTTP 200 within 10 seconds without disconnection or 5xx server errors."},
        {"pair_id": "P-01", "target_app": "Prateek", "input": "What is the supply airflow and assumed occupancy for the Project Cedar meeting room?", "expected_behaviour": "Correctly extract that Project Cedar meeting room assumes 10 occupants and a supply airflow of 300 cubic metres per hour, citing the Project Cedar report."},
        {"pair_id": "P-02", "target_app": "Prateek", "input": "Compare the energy targets of Project Maple with the ventilation assumption of Project Cedar.", "expected_behaviour": "Retrieve and state both Project Maple BENG targets (BENG 1 <= 60, BENG 2 <= 35, BENG 3 >= 45%) and Project Cedar ventilation (300 m3/h, 10 occupants), citing both distinct sources."},
        {"pair_id": "P-03", "target_app": "Prateek", "input": "Which project has rules regarding how much natural light must enter residential living rooms, and what is the required ratio?", "expected_behaviour": "Identify Project Birch and report the daylight requirement of at least 12% equivalent daylight area of the floor area (Section 2.1)."},
        {"pair_id": "P-04", "target_app": "Prateek", "input": "What are the sun hours requirements for Project Elm?", "expected_behaviour": "Server responds with HTTP 200 within 10 seconds and valid JSON answer citing Section 3.1 of Project Elm."}
    ]
    for s in seeds:
        try:
            _supabase_client.table("golden_pairs").insert(s).execute()
        except Exception:
            pass


# ----------------------------------------------------
# Golden Pairs CRUD
# ----------------------------------------------------

def get_golden_pairs(target_app: Optional[str] = None) -> List[Dict[str, Any]]:
    if _using_supabase:
        q = _supabase_client.table("golden_pairs").select("*")
        if target_app and target_app.lower() != "all":
            q = q.ilike("target_app", f"%{target_app}%")
        res = _execute_read(q.order("pair_id"))
        return res.data or []
    else:
        with _get_sqlite_conn() as conn:
            if target_app and target_app.lower() != "all":
                cur = conn.execute(
                    "SELECT * FROM golden_pairs WHERE LOWER(target_app) LIKE ? ORDER BY pair_id",
                    (f"%{target_app.lower()}%",)
                )
            else:
                cur = conn.execute("SELECT * FROM golden_pairs ORDER BY pair_id")
            return [dict(row) for row in cur.fetchall()]


def get_golden_pair(pair_id: str) -> Optional[Dict[str, Any]]:
    if _using_supabase:
        res = _execute_read(_supabase_client.table("golden_pairs").select("*").eq("pair_id", pair_id))
        return res.data[0] if res.data else None
    else:
        with _get_sqlite_conn() as conn:
            cur = conn.execute("SELECT * FROM golden_pairs WHERE pair_id = ?", (pair_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def save_golden_pair(pair_id: str, target_app: str, input_text: str, expected_behaviour: str) -> Dict[str, Any]:
    pair_id = pair_id.strip()
    target_app = target_app.strip()
    input_text = input_text.strip()
    expected_behaviour = expected_behaviour.strip()

    if _using_supabase:
        data = {
            "pair_id": pair_id,
            "target_app": target_app,
            "input": input_text,
            "expected_behaviour": expected_behaviour
        }
        res = _supabase_client.table("golden_pairs").upsert(data).execute()
        return res.data[0] if res.data else data
    else:
        with _get_sqlite_conn() as conn:
            conn.execute("""
            INSERT INTO golden_pairs (pair_id, target_app, input, expected_behaviour)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pair_id) DO UPDATE SET
                target_app = excluded.target_app,
                input = excluded.input,
                expected_behaviour = excluded.expected_behaviour
            """, (pair_id, target_app, input_text, expected_behaviour))
            conn.commit()
            return get_golden_pair(pair_id)


def create_golden_pair(pair_id: str, target_app: str, input_text: str, expected_behaviour: str) -> Dict[str, Any]:
    """Insert-only. Raises GoldenPairExists instead of replacing an existing pair."""
    row = {
        "pair_id": pair_id.strip(),
        "target_app": target_app.strip(),
        "input": input_text.strip(),
        "expected_behaviour": expected_behaviour.strip(),
    }
    if _using_supabase:
        from postgrest.exceptions import APIError
        try:
            res = _supabase_client.table("golden_pairs").insert(row).execute()
        except APIError as e:
            if getattr(e, "code", None) == "23505":  # unique_violation
                raise GoldenPairExists(row["pair_id"]) from e
            raise
        return res.data[0] if res.data else row
    try:
        with _get_sqlite_conn() as conn:
            conn.execute(
                "INSERT INTO golden_pairs (pair_id, target_app, input, expected_behaviour) VALUES (?, ?, ?, ?)",
                (row["pair_id"], row["target_app"], row["input"], row["expected_behaviour"]),
            )
    except sqlite3.IntegrityError as e:
        raise GoldenPairExists(row["pair_id"]) from e
    return get_golden_pair(row["pair_id"])


# ----------------------------------------------------
# Execution & Check Results
# ----------------------------------------------------

def save_run_result(
    run_id: str,
    pair_id: str,
    actual_response: str,
    overall_status: str,
    checks: List[Dict[str, Any]]
) -> int:
    """
    Saves an execution record to RUN_RESULTS and its associated checks to CHECK_RESULTS.
    Never overwrites prior runs; creates a new result_id.
    """
    ts = datetime.now(timezone.utc).isoformat()

    if _using_supabase:
        run_row = {
            "run_id": run_id,
            "pair_id": pair_id,
            "actual_response": actual_response,
            "overall_status": overall_status,
            "timestamp": ts
        }
        res = _supabase_client.table("run_results").insert(run_row).execute()
        result_id = res.data[0]["result_id"]

        check_rows = []
        for c in checks:
            check_rows.append({
                "result_id": result_id,
                "check_name": c["check_name"],
                "status": c["status"],
                "evidence_or_reason": c.get("evidence_or_reason", ""),
                "created_at": ts
            })
        if check_rows:
            try:
                # One bulk insert is a single statement, so the checks land all together or not at all
                _supabase_client.table("check_results").insert(check_rows).execute()
            except Exception:
                # Never keep a verdict without the checks that justify it
                _supabase_client.table("run_results").delete().eq("result_id", result_id).execute()
                raise

        return int(result_id)
    else:
        with _get_sqlite_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
            INSERT INTO run_results (run_id, pair_id, actual_response, overall_status, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """, (run_id, pair_id, actual_response, overall_status, ts))
            result_id = cur.lastrowid

            for c in checks:
                cur.execute("""
                INSERT INTO check_results (result_id, check_name, status, evidence_or_reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """, (result_id, c["check_name"], c["status"], c.get("evidence_or_reason", ""), ts))
            conn.commit()
            return int(result_id)


def count_recent_runs(target_app: str, since_iso: str) -> int:
    """Runs of the target's cases stored at or after since_iso (a UTC ISO timestamp, as save_run_result writes)."""
    if _using_supabase:
        pairs = _execute_read(_supabase_client.table("golden_pairs").select("pair_id").eq("target_app", target_app)).data
        pair_ids = [p["pair_id"] for p in pairs or []]
        if not pair_ids:
            return 0
        runs = _execute_read(_supabase_client.table("run_results").select("result_id")
                             .in_("pair_id", pair_ids).gte("timestamp", since_iso)).data
        return len(runs or [])
    with _get_sqlite_conn() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM run_results r JOIN golden_pairs g ON g.pair_id = r.pair_id "
            "WHERE g.target_app = ? AND r.timestamp >= ?",
            (target_app, since_iso),
        )
        return int(cur.fetchone()[0])


def get_run_results(pair_id: Optional[str] = None, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if _using_supabase:
        def build():
            q = _supabase_client.table("run_results").select("*, golden_pairs(target_app, input, expected_behaviour)")
            if pair_id:
                q = q.eq("pair_id", pair_id)
            if run_id:
                q = q.eq("run_id", run_id)
            return q.order("timestamp", desc=True).order("result_id", desc=True)
        return [_flatten_run(r) for r in _fetch_all(build)]
    else:
        with _get_sqlite_conn() as conn:
            query = """
            SELECT r.*, g.target_app, g.input, g.expected_behaviour
            FROM run_results r
            JOIN golden_pairs g ON r.pair_id = g.pair_id
            WHERE 1=1
            """
            params = []
            if pair_id:
                query += " AND r.pair_id = ?"
                params.append(pair_id)
            if run_id:
                query += " AND r.run_id = ?"
                params.append(run_id)
            query += " ORDER BY r.timestamp DESC, r.result_id DESC"
            cur = conn.execute(query, params)
            return [dict(row) for row in cur.fetchall()]


def get_run_result_detail(result_id: int) -> Optional[Dict[str, Any]]:
    if _using_supabase:
        res = _execute_read(_supabase_client.table("run_results").select("*, golden_pairs(target_app, input, expected_behaviour)")
                            .eq("result_id", result_id))
        if not res.data:
            return None
        run_data = _flatten_run(res.data[0])
        checks_res = _execute_read(_supabase_client.table("check_results").select("*").eq("result_id", result_id).order("check_result_id"))
        run_data["checks"] = checks_res.data or []
        return run_data
    else:
        with _get_sqlite_conn() as conn:
            cur = conn.execute("""
            SELECT r.*, g.target_app, g.input, g.expected_behaviour
            FROM run_results r
            JOIN golden_pairs g ON r.pair_id = g.pair_id
            WHERE r.result_id = ?
            """, (result_id,))
            row = cur.fetchone()
            if not row:
                return None
            run_data = dict(row)
            check_cur = conn.execute(
                "SELECT * FROM check_results WHERE result_id = ? ORDER BY check_result_id ASC",
                (result_id,)
            )
            run_data["checks"] = [dict(c) for c in check_cur.fetchall()]
            return run_data


def get_checks_for_results(result_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """Checks for many runs in one query (chunked on Supabase), keyed by result_id."""
    by_result: Dict[int, List[Dict[str, Any]]] = {rid: [] for rid in result_ids}
    if not result_ids:
        return by_result
    if _using_supabase:
        for i in range(0, len(result_ids), SUPABASE_IN_CHUNK):
            chunk = result_ids[i:i + SUPABASE_IN_CHUNK]
            rows = _fetch_all(lambda: _supabase_client.table("check_results").select("*")
                              .in_("result_id", chunk).order("check_result_id"))
            for row in rows:
                by_result[row["result_id"]].append(row)
        return by_result
    with _get_sqlite_conn() as conn:
        placeholders = ",".join("?" for _ in result_ids)
        cur = conn.execute(
            f"SELECT * FROM check_results WHERE result_id IN ({placeholders}) ORDER BY check_result_id ASC",
            list(result_ids),
        )
        for row in cur.fetchall():
            by_result[row["result_id"]].append(dict(row))
    return by_result


def get_all_runs() -> List[str]:
    """Returns list of unique logical run_ids ordered by latest activity."""
    if _using_supabase:
        rows = _fetch_all(lambda: _supabase_client.table("run_results").select("run_id")
                          .order("timestamp", desc=True).order("result_id", desc=True))
        return list(dict.fromkeys(r["run_id"] for r in rows))
    else:
        with _get_sqlite_conn() as conn:
            cur = conn.execute(
                "SELECT run_id, MAX(timestamp) AS last_activity FROM run_results GROUP BY run_id ORDER BY last_activity DESC"
            )
            return [row["run_id"] for row in cur.fetchall()]


def get_summary_stats() -> Dict[str, Any]:
    """Calculates counts and top failing checks from persisted database records."""
    if _using_supabase:
        runs_data = _fetch_all(lambda: _supabase_client.table("run_results").select("overall_status").order("result_id"))
        total_runs = len(runs_data)
        pass_count = sum(1 for r in runs_data if r["overall_status"] == "PASS")
        fail_count = sum(1 for r in runs_data if r["overall_status"] == "FAIL")
        error_count = sum(1 for r in runs_data if r["overall_status"] == "ERROR")
        needs_review_count = sum(1 for r in runs_data if r["overall_status"] == "NEEDS REVIEW")

        checks_data = _fetch_all(lambda: _supabase_client.table("check_results").select("check_name, status")
                                 .in_("status", ["FAIL", "ERROR"]).order("check_result_id"))
        fail_counts = {}
        for c in checks_data:
            name = c["check_name"]
            fail_counts[name] = fail_counts.get(name, 0) + 1

        top_failing = sorted([{"check_name": k, "fail_count": v} for k, v in fail_counts.items()],
                             key=lambda x: x["fail_count"], reverse=True)[:TOP_FAILING_LIMIT]

        return {
            "total_executions": total_runs,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "error_count": error_count,
            "needs_review_count": needs_review_count,
            "top_failing_checks": top_failing
        }
    else:
        with _get_sqlite_conn() as conn:
            cur = conn.execute("""
            SELECT
                count(*) as total_executions,
                sum(case when overall_status = 'PASS' then 1 else 0 end) as pass_count,
                sum(case when overall_status = 'FAIL' then 1 else 0 end) as fail_count,
                sum(case when overall_status = 'ERROR' then 1 else 0 end) as error_count,
                sum(case when overall_status = 'NEEDS REVIEW' then 1 else 0 end) as needs_review_count
            FROM run_results
            """)
            stats = dict(cur.fetchone())
            for k in stats:
                stats[k] = stats[k] or 0

            cur_checks = conn.execute("""
            SELECT check_name, count(*) as fail_count
            FROM check_results
            WHERE status IN ('FAIL', 'ERROR')
            GROUP BY check_name
            ORDER BY fail_count DESC
            LIMIT 10
            """)
            stats["top_failing_checks"] = [dict(r) for r in cur_checks.fetchall()]
            return stats


# Initialize tables upon module load
init_db()
