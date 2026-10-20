"""
FastAPI Backend for MANTIS.
Provides clean REST API endpoints for:
- Managing Golden Pairs
- Executing Evaluations against Target Apps
- Persisting Run and Check Results
- Fetching and Comparing Runs
- Aggregating Metrics & Summaries
"""

import hmac
import os
import time
from collections import Counter
from typing import Optional, List, Dict, Any, Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

import db
from adapters import invoke_target
from evaluator import effective_status, evaluate_response, is_unavailable_capability

# Hosted deployments hide the interactive docs and refuse writes without a token (see db.is_hosted).
HOSTED = db.is_hosted()

app = FastAPI(
    title="MANTIS API",
    description=(
        "MANTIS (Machine-Assisted Network Testing & Intelligent Scrutiny) runs golden test cases against live "
        "AI applications and scores every response with evidence. Hunt failures. Prove reliability."
    ),
    version="1.0.0",
    docs_url=None if HOSTED else "/docs",
    redoc_url=None if HOSTED else "/redoc",
    openapi_url=None if HOSTED else "/openapi.json",
)

# Scripts only from this origin; styles allow inline attributes (used for animation offsets) and Google Fonts.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if not request.url.path.startswith("/classic"):  # the legacy Gradio view ships its own scripts
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


# ----------------------------------------------------
# Write access
# ----------------------------------------------------

# Runs against these targets need no token (owner's decision): anyone with the URL can run their cases.
# Creating cases, and running every other target, still requires write access.
OPEN_EXECUTE_TARGETS = ["Prateek"]


def require_write_access(authorization: Optional[str] = Header(default=None)):
    """
    Guards every endpoint that changes data or calls a target app.
    - MANTIS_ADMIN_TOKEN set: requires `Authorization: Bearer <token>`.
    - Not set: allowed locally, refused on a hosted deployment.
    - Storage not durable: refused, so a run is never accepted and then silently lost.
    """
    _require_token(authorization)
    _require_durable_storage()


def _require_token(authorization: Optional[str]):
    token = os.getenv("MANTIS_ADMIN_TOKEN", "")
    if token:
        supplied = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="An access token is required to change data.",
                                headers={"WWW-Authenticate": "Bearer"})
    elif db.is_hosted():
        raise HTTPException(status_code=403,
                            detail="Writes are disabled on this deployment until MANTIS_ADMIN_TOKEN is configured.")


def _require_durable_storage():
    if not db.is_durable():
        raise HTTPException(status_code=503,
                            detail="This deployment has no durable database, so it can't save runs or test cases. "
                                   "Configure Supabase first.")


# ----------------------------------------------------
# Pydantic Request / Response Models
# ----------------------------------------------------

PAIR_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$"
MAX_TEXT_CHARS = 20_000


class GoldenPairIn(BaseModel):
    pair_id: str = Field(..., pattern=PAIR_ID_PATTERN, description="Unique test case identifier (e.g. A-01, P-01)")
    target_app: Literal["Anupma", "Prateek"] = Field(..., description="Target application")
    input: str = Field(..., max_length=MAX_TEXT_CHARS, description="Exact test input sent to the target application")
    expected_behaviour: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS,
                                    description="Expected behavioural outcome or constraint")


class ExecuteRequest(BaseModel):
    pair_id: str = Field(..., max_length=64)
    run_id: Optional[str] = Field(default=None, max_length=80)
    timeout: float = Field(default=20.0, ge=1, le=60)


# ----------------------------------------------------
# Display verdicts
# Stored history is never rewritten. Each run also gets a display `verdict`: the same precedence over the
# checks that actually ran. It differs from the stored `overall_status` only for older runs whose sole open
# item was an optional check that couldn't run (e.g. Content Safety during a provider quota outage).
# ----------------------------------------------------

STATUS_LABELS = {"PASS": "Pass", "FAIL": "Fail", "ERROR": "Error", "NEEDS REVIEW": "Needs review"}


def annotate_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    checks = detail.get("checks") or []
    for c in checks:
        c["unavailable"] = is_unavailable_capability(c["check_name"], c["status"], c.get("evidence_or_reason") or "")
    skipped = [c["check_name"] for c in checks if c["unavailable"]]
    stored = detail["overall_status"]
    verdict = effective_status(checks) if skipped else stored
    detail["verdict"] = verdict
    detail["verdict_note"] = (
        f"Recorded as {STATUS_LABELS.get(stored, stored)}. {', '.join(skipped)} could not run, "
        f"so it does not count toward the verdict." if verdict != stored else None
    )
    return detail


def annotate_results(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only stored NEEDS REVIEW runs can be reinterpreted, so only their checks are loaded, in one batch."""
    review_ids = [row["result_id"] for row in rows if row["overall_status"] == "NEEDS REVIEW"]
    checks_by_result = db.get_checks_for_results(review_ids) if review_ids else {}
    for row in rows:
        if row["overall_status"] == "NEEDS REVIEW":
            detail = annotate_detail({"overall_status": row["overall_status"], "checks": checks_by_result[row["result_id"]]})
            row["verdict"], row["verdict_note"] = detail["verdict"], detail["verdict_note"]
        else:
            row["verdict"], row["verdict_note"] = row["overall_status"], None
    return rows


# ----------------------------------------------------
# Endpoints
# ----------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "database": db.get_backend_status(),
        "writes_require_token": bool(os.getenv("MANTIS_ADMIN_TOKEN")),
        "open_execute_targets": OPEN_EXECUTE_TARGETS,
        "targets": {
            "Anupma": "https://04-gradio-client.vercel.app/gradio_api/run/predict",
            "Prateek": "https://knowledge-retriever-mvp.vercel.app/api/ans_fact"
        }
    }


@app.get("/api/golden-pairs")
def list_golden_pairs(target_app: Optional[str] = None):
    return db.get_golden_pairs(target_app)


@app.get("/api/golden-pairs/{pair_id}")
def get_golden_pair(pair_id: str):
    pair = db.get_golden_pair(pair_id)
    if not pair:
        raise HTTPException(status_code=404, detail=f"Golden pair '{pair_id}' not found.")
    return pair


@app.post("/api/golden-pairs", dependencies=[Depends(require_write_access)])
def create_golden_pair(pair_in: GoldenPairIn):
    """Creates a golden pair. Existing IDs are rejected with 409 rather than overwritten."""
    try:
        return db.create_golden_pair(
            pair_id=pair_in.pair_id,
            target_app=pair_in.target_app,
            input_text=pair_in.input,
            expected_behaviour=pair_in.expected_behaviour
        )
    except db.GoldenPairExists:
        raise HTTPException(status_code=409,
                            detail=f"Golden pair '{pair_in.pair_id}' already exists. Choose another ID.")


@app.post("/api/execute")
def execute_test(req: ExecuteRequest, authorization: Optional[str] = Header(default=None)):
    """
    Executes a golden test case against its target application:
    1. Loads the golden pair
    2. Invokes target adapter
    3. Runs evaluation checks
    4. Persists execution to RUN_RESULTS and checks to CHECK_RESULTS
    5. Returns full results
    """
    pair = db.get_golden_pair(req.pair_id)
    if not pair:
        raise HTTPException(status_code=404, detail=f"Golden pair '{req.pair_id}' not found.")
    # Write access, checked before any target call; open targets skip only the token
    if pair["target_app"] not in OPEN_EXECUTE_TARGETS:
        _require_token(authorization)
    _require_durable_storage()

    # Generate default run_id if not provided
    run_id = req.run_id or f"run_{time.strftime('%Y%m%d_%H%M%S')}"

    # 1. Invoke Target Application
    adapter_resp = invoke_target(
        target_app=pair["target_app"],
        input_text=pair["input"],
        timeout=req.timeout
    )

    # 2. Evaluate Response
    eval_result = evaluate_response(
        pair_id=pair["pair_id"],
        target_app=pair["target_app"],
        test_input=pair["input"],
        expected_behaviour=pair["expected_behaviour"],
        resp=adapter_resp
    )

    # 3. Persist to Database (creates a new result row, never overwrites)
    checks_dict_list = [c.model_dump() for c in eval_result.checks]
    result_id = db.save_run_result(
        run_id=run_id,
        pair_id=pair["pair_id"],
        actual_response=adapter_resp.raw_response,
        overall_status=eval_result.overall_status,
        checks=checks_dict_list
    )

    # 4. Return Full Result
    full_detail = annotate_detail(db.get_run_result_detail(result_id))
    full_detail["why_failed"] = eval_result.why_failed
    return full_detail


@app.get("/api/runs")
def list_runs():
    return db.get_all_runs()


@app.get("/api/results")
def list_results(pair_id: Optional[str] = None, run_id: Optional[str] = None):
    return annotate_results(db.get_run_results(pair_id=pair_id, run_id=run_id))


@app.get("/api/results/{result_id}")
def get_result(result_id: int):
    detail = db.get_run_result_detail(result_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Result ID {result_id} not found.")
    return annotate_detail(detail)


@app.get("/api/compare")
def compare_executions(result_id_a: int = Query(...), result_id_b: int = Query(...)):
    """
    Compares two saved executions of the same test case or run.
    Identifies:
    - Overall status in each execution
    - Actual response in each
    - Checks that failed in A but passed in B (resolved)
    - Checks that still fail in B (remaining)
    - Checks that passed in A but failed in B (newly introduced)
    """
    run_a = db.get_run_result_detail(result_id_a)
    run_b = db.get_run_result_detail(result_id_b)

    if not run_a:
        raise HTTPException(status_code=404, detail=f"Execution result_id_a={result_id_a} not found.")
    if not run_b:
        raise HTTPException(status_code=404, detail=f"Execution result_id_b={result_id_b} not found.")
    run_a, run_b = annotate_detail(run_a), annotate_detail(run_b)

    checks_a = {c["check_name"]: c for c in run_a.get("checks", [])}
    checks_b = {c["check_name"]: c for c in run_b.get("checks", [])}

    all_check_names = list(dict.fromkeys(list(checks_a.keys()) + list(checks_b.keys())))

    resolved = []
    remaining = []
    new_failures = []
    check_comparisons = []

    for name in all_check_names:
        ca = checks_a.get(name)
        cb = checks_b.get(name)

        status_a = ca["status"] if ca else "NOT RUN"
        status_b = cb["status"] if cb else "NOT RUN"

        reason_a = ca.get("evidence_or_reason", "") if ca else "Not run in this execution"
        reason_b = cb.get("evidence_or_reason", "") if cb else "Not run in this execution"

        comp_item = {
            "check_name": name,
            "status_a": status_a,
            "status_b": status_b,
            "evidence_a": reason_a,
            "evidence_b": reason_b,
            "unavailable_a": bool(ca and ca["unavailable"]),
            "unavailable_b": bool(cb and cb["unavailable"]),
        }
        check_comparisons.append(comp_item)

        # Categorize
        if status_a in ("FAIL", "ERROR") and status_b == "PASS":
            resolved.append(name)
        elif status_a in ("FAIL", "ERROR") and status_b in ("FAIL", "ERROR"):
            remaining.append(name)
        elif status_a == "PASS" and status_b in ("FAIL", "ERROR"):
            new_failures.append(name)

    return {
        "execution_a": {
            "result_id": run_a["result_id"],
            "run_id": run_a["run_id"],
            "pair_id": run_a["pair_id"],
            "timestamp": run_a["timestamp"],
            "overall_status": run_a["overall_status"],
            "verdict": run_a["verdict"],
            "actual_response": run_a["actual_response"]
        },
        "execution_b": {
            "result_id": run_b["result_id"],
            "run_id": run_b["run_id"],
            "pair_id": run_b["pair_id"],
            "timestamp": run_b["timestamp"],
            "overall_status": run_b["overall_status"],
            "verdict": run_b["verdict"],
            "actual_response": run_b["actual_response"]
        },
        "summary": {
            "status_a": run_a["overall_status"],
            "status_b": run_b["overall_status"],
            "status_transition": f"{run_a['overall_status']} -> {run_b['overall_status']}",
            "failures_resolved": resolved,
            "failures_remaining": remaining,
            "new_failures": new_failures
        },
        "individual_checks": check_comparisons
    }


@app.get("/api/summary")
def get_summary():
    """Run counts per display verdict. Stored statuses and the failing-check ranking are unchanged."""
    stats = db.get_summary_stats()
    rows = annotate_results(db.get_run_results())
    verdicts = Counter(r["verdict"] for r in rows)
    stats.update(
        pass_count=verdicts["PASS"],
        fail_count=verdicts["FAIL"],
        error_count=verdicts["ERROR"],
        needs_review_count=verdicts["NEEDS REVIEW"],
        reclassified_runs=sum(1 for r in rows if r["verdict_note"]),
    )
    return stats
