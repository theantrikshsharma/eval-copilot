"""
FastAPI Backend for Eval Co-pilot.
Provides clean REST API endpoints for:
- Managing Golden Pairs
- Executing Evaluations against Target Apps
- Persisting Run and Check Results
- Fetching and Comparing Runs
- Aggregating Metrics & Summaries
"""

import time
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

import db
from adapters import invoke_target
from evaluator import evaluate_response


app = FastAPI(
    title="Eval Co-pilot API",
    description="Evaluation co-pilot for checking live AI applications against golden test cases.",
    version="1.0.0"
)


# ----------------------------------------------------
# Pydantic Request / Response Models
# ----------------------------------------------------

class GoldenPairIn(BaseModel):
    pair_id: str = Field(..., description="Unique test case identifier (e.g. A-01, P-01)")
    target_app: str = Field(..., description="Target application name ('Anupma' or 'Prateek')")
    input: str = Field(..., description="Exact test input sent to the target application")
    expected_behaviour: str = Field(..., description="Expected behavioural outcome or constraint")


class ExecuteRequest(BaseModel):
    pair_id: str
    run_id: Optional[str] = None
    timeout: float = 20.0


# ----------------------------------------------------
# Endpoints
# ----------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "database": db.get_backend_status(),
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


@app.post("/api/golden-pairs")
def create_or_update_golden_pair(pair_in: GoldenPairIn):
    return db.save_golden_pair(
        pair_id=pair_in.pair_id,
        target_app=pair_in.target_app,
        input_text=pair_in.input,
        expected_behaviour=pair_in.expected_behaviour
    )


@app.post("/api/execute")
def execute_test(req: ExecuteRequest):
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
    full_detail = db.get_run_result_detail(result_id)
    full_detail["why_failed"] = eval_result.why_failed
    return full_detail


@app.get("/api/runs")
def list_runs():
    return db.get_all_runs()


@app.get("/api/results")
def list_results(pair_id: Optional[str] = None, run_id: Optional[str] = None):
    return db.get_run_results(pair_id=pair_id, run_id=run_id)


@app.get("/api/results/{result_id}")
def get_result(result_id: int):
    detail = db.get_run_result_detail(result_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Result ID {result_id} not found.")
    return detail


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
            "evidence_b": reason_b
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
            "actual_response": run_a["actual_response"]
        },
        "execution_b": {
            "result_id": run_b["result_id"],
            "run_id": run_b["run_id"],
            "pair_id": run_b["pair_id"],
            "timestamp": run_b["timestamp"],
            "overall_status": run_b["overall_status"],
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
    return db.get_summary_stats()
