"""
Evaluation Engine for Eval Co-pilot.
Implements deterministic checks first, separates ERROR from FAIL from PASS from NEEDS REVIEW,
and compiles actionable evidence and concise failure explanations.
"""

import json
import re
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from adapters import AdapterResponse
import judges


class SingleCheckResult(BaseModel):
    check_name: str
    status: str  # 'PASS' | 'FAIL' | 'ERROR' | 'NEEDS REVIEW'
    evidence_or_reason: str


class EvaluationResult(BaseModel):
    overall_status: str  # 'PASS' | 'FAIL' | 'ERROR' | 'NEEDS REVIEW'
    why_failed: Optional[str] = None
    checks: List[SingleCheckResult]


def evaluate_response(
    pair_id: str,
    target_app: str,
    test_input: str,
    expected_behaviour: str,
    resp: AdapterResponse
) -> EvaluationResult:
    """
    Evaluates target application response against expected behaviour.
    Rules for overall status:
    - ERROR: Request failed, connection timed out, HTTP status != 200, or response empty due to technical failure.
    - FAIL: Execution technically succeeded, but one or more behavioral/deterministic checks failed.
    - PASS: All required checks completed successfully with positive evidence.
    - NEEDS REVIEW: Evaluation criteria cannot be verified deterministically without ambiguous evidence.
    """
    checks: List[SingleCheckResult] = []

    # 1. Technical Health Check (First Gate)
    if not resp.success:
        err_msg = resp.error or "Target application did not return a successful response."
        checks.append(SingleCheckResult(
            check_name="HTTP & Network Reachability",
            status="ERROR",
            evidence_or_reason=f"Execution error: {err_msg} (Latency: {resp.latency_ms}ms)"
        ))
        return EvaluationResult(
            overall_status="ERROR",
            why_failed=f"Execution error: Could not reach target or received error ({err_msg}).",
            checks=checks
        )

    # Technical success check
    checks.append(SingleCheckResult(
        check_name="HTTP & Network Reachability",
        status="PASS",
        evidence_or_reason=f"HTTP {resp.status_code} received in {resp.latency_ms}ms"
    ))

    raw_text = resp.raw_response or ""
    parsed = resp.parsed_data or {}
    target_clean = target_app.strip().lower()

    # Route evaluation based on known test pair IDs or target app
    if pair_id == "A-01":
        _eval_A01(parsed, raw_text, checks)
    elif pair_id == "A-02":
        _eval_A02(parsed, raw_text, checks)
    elif pair_id == "A-03":
        _eval_A03(parsed, raw_text, checks)
    elif pair_id == "A-04":
        _eval_A04(resp, checks)
    elif pair_id == "P-01":
        _eval_P01(raw_text, checks)
    elif pair_id == "P-02":
        _eval_P02(raw_text, checks)
    elif pair_id == "P-03":
        _eval_P03(raw_text, checks)
    elif pair_id == "P-04":
        _eval_P04(resp, raw_text, checks)
    else:
        # Generic deterministic check
        _eval_generic(test_input, expected_behaviour, raw_text, parsed, checks)

    # Model-backed checks run after the deterministic ones, and only when their API key is set
    if judges.is_judge_enabled():
        _escalate_to_judge(test_input, expected_behaviour, raw_text, checks)
    if judges.is_safety_enabled() and raw_text.strip():
        safety = judges.classify_safety(test_input, raw_text)
        checks.append(SingleCheckResult(
            check_name="Content Safety",
            status=safety.status,
            evidence_or_reason=safety.evidence
        ))

    # Determine overall status from checks
    overall_status = "PASS"
    fail_reasons = []

    for c in checks:
        if c.status == "ERROR":
            overall_status = "ERROR"
            fail_reasons.append(f"[{c.check_name}] {c.evidence_or_reason}")
            break
        elif c.status == "FAIL":
            overall_status = "FAIL"
            fail_reasons.append(f"[{c.check_name}] {c.evidence_or_reason}")
        elif c.status == "NEEDS REVIEW" and overall_status != "FAIL":
            overall_status = "NEEDS REVIEW"

    why_failed = None
    if overall_status in ("FAIL", "ERROR"):
        why_failed = "; ".join(fail_reasons)
    elif overall_status == "NEEDS REVIEW":
        why_failed = "Some behavioural checks could not be fully resolved deterministically and require manual review."

    return EvaluationResult(
        overall_status=overall_status,
        why_failed=why_failed,
        checks=checks
    )


def _escalate_to_judge(
    test_input: str,
    expected_behaviour: str,
    raw_text: str,
    checks: List[SingleCheckResult]
):
    """
    Sends each NEEDS REVIEW check to the LLM judge. Deterministic PASS and FAIL results are never touched.
    The check keeps its name, and its evidence records both the deterministic reason and the judge's verdict.
    """
    for i, check in enumerate(checks):
        if check.status != "NEEDS REVIEW":
            continue
        verdict = judges.judge_behaviour(check.check_name, test_input, expected_behaviour, raw_text)
        checks[i] = SingleCheckResult(
            check_name=check.check_name,
            status=verdict.status,
            evidence_or_reason=f"Deterministic: {check.evidence_or_reason} | {verdict.evidence}"
        )


# ----------------------------------------------------
# Target A (Anupma) Evaluators
# ----------------------------------------------------

def _eval_A01(parsed: Dict[str, Any], raw_text: str, checks: List[SingleCheckResult]):
    """
    A-01: Dubai 3-day itinerary, 2 adults, max 1 activity per day, no child activities.
    """
    # Check JSON structure
    if parsed and isinstance(parsed, dict) and "days" in parsed:
        checks.append(SingleCheckResult(
            check_name="Response Structure",
            status="PASS",
            evidence_or_reason="Returned valid JSON containing structured 'days' itinerary."
        ))
    else:
        checks.append(SingleCheckResult(
            check_name="Response Structure",
            status="FAIL",
            evidence_or_reason="Output is not valid JSON or lacks 'days' array."
        ))

    # Check 1: Max 1 activity per day
    day_violations = []
    if parsed and "days" in parsed and isinstance(parsed["days"], list):
        for d in parsed["days"]:
            day_num = d.get("day", "?")
            acts = d.get("activities", [])
            if isinstance(acts, list) and len(acts) > 1:
                day_violations.append(f"Day {day_num} has {len(acts)} activities")
    if day_violations:
        checks.append(SingleCheckResult(
            check_name="Activity Count Constraint (Max 1/day)",
            status="FAIL",
            evidence_or_reason=f"Exceeded maximum 1 activity per day: {', '.join(day_violations)}."
        ))
    else:
        checks.append(SingleCheckResult(
            check_name="Activity Count Constraint (Max 1/day)",
            status="PASS",
            evidence_or_reason="At most 1 activity scheduled per day across the itinerary."
        ))

    # Check 2: No child-oriented activities
    child_keywords = ["carousel", "playground", "kid", "child", "children", "puppet theater", "family room", "kids' menu", "family-friendly"]
    found_child_terms = []
    text_lower = raw_text.lower()
    for kw in child_keywords:
        if re.search(r'\b' + re.escape(kw) + r'\b', text_lower):
            found_child_terms.append(kw)

    if found_child_terms:
        checks.append(SingleCheckResult(
            check_name="Traveler Preference (No Child Activities)",
            status="FAIL",
            evidence_or_reason=f"Itinerary for 2 adults contains child-oriented keywords: {', '.join(set(found_child_terms))}."
        ))
    else:
        checks.append(SingleCheckResult(
            check_name="Traveler Preference (No Child Activities)",
            status="PASS",
            evidence_or_reason="No child-oriented activities or amenities detected in adult itinerary."
        ))


def _eval_A02(parsed: Dict[str, Any], raw_text: str, checks: List[SingleCheckResult]):
    """
    A-02: Missing destination.
    Expected: Prompt user for destination, do not invent Barcelona.
    """
    text_lower = raw_text.lower()
    has_barcelona = "barcelona" in text_lower
    asked_for_dest = any(phrase in text_lower for phrase in [
        "destination is required", "please provide a destination", "where would you like to go",
        "missing destination", "which city", "enter destination"
    ])

    if has_barcelona:
        checks.append(SingleCheckResult(
            check_name="Missing Destination Handling",
            status="FAIL",
            evidence_or_reason="App hallucinated 'Barcelona, Spain' itinerary instead of asking the user to provide a destination."
        ))
    elif asked_for_dest:
        checks.append(SingleCheckResult(
            check_name="Missing Destination Handling",
            status="PASS",
            evidence_or_reason="App correctly requested a destination before generating an itinerary."
        ))
    else:
        # Check if it generated another arbitrary city
        dest = parsed.get("destination", "") if parsed else ""
        if dest:
            checks.append(SingleCheckResult(
                check_name="Missing Destination Handling",
                status="FAIL",
                evidence_or_reason=f"App generated an itinerary for '{dest}' despite blank destination input."
            ))
        else:
            checks.append(SingleCheckResult(
                check_name="Missing Destination Handling",
                status="NEEDS REVIEW",
                evidence_or_reason="Response did not mention Barcelona, but clarity on destination validation is ambiguous."
            ))


def _eval_A03(parsed: Dict[str, Any], raw_text: str, checks: List[SingleCheckResult]):
    """
    A-03: Past travel date (2020-01-01 to 2020-01-03).
    Expected: Reject or warn about past dates.
    """
    text_lower = raw_text.lower()
    warned_past = any(phrase in text_lower for phrase in [
        "past date", "invalid date", "dates in the past", "cannot travel in the past",
        "date error", "future date", "select upcoming dates"
    ])
    accepted_2020 = "2020" in text_lower

    if accepted_2020 and not warned_past:
        checks.append(SingleCheckResult(
            check_name="Past Date Validation",
            status="FAIL",
            evidence_or_reason="App accepted past travel dates (2020-01-01 to 2020-01-03) and generated an itinerary without warning or rejection."
        ))
    elif warned_past:
        checks.append(SingleCheckResult(
            check_name="Past Date Validation",
            status="PASS",
            evidence_or_reason="App identified and rejected the past travel dates."
        ))
    else:
        checks.append(SingleCheckResult(
            check_name="Past Date Validation",
            status="NEEDS REVIEW",
            evidence_or_reason="Could not definitively confirm if past date was validated."
        ))


def _eval_A04(resp: AdapterResponse, checks: List[SingleCheckResult]):
    """
    A-04: Server stability check.
    """
    checks.append(SingleCheckResult(
        check_name="Response Latency (<10s)",
        status="PASS" if resp.latency_ms < 10000 else "FAIL",
        evidence_or_reason=f"Target responded in {resp.latency_ms}ms (threshold: 10,000ms)."
    ))
    checks.append(SingleCheckResult(
        check_name="Non-Empty Response Body",
        status="PASS" if len(resp.raw_response.strip()) > 20 else "FAIL",
        evidence_or_reason=f"Payload received ({len(resp.raw_response)} characters)."
    ))
    checks.append(SingleCheckResult(
        check_name="Stability Scope Note",
        status="PASS",
        evidence_or_reason="Single request completed without disconnection. Note: A single successful request does not prove overall server stability."
    ))


# ----------------------------------------------------
# Target B (Prateek) Evaluators
# ----------------------------------------------------

def _eval_P01(raw_text: str, checks: List[SingleCheckResult]):
    """
    P-01: Project Cedar meeting room supply airflow (300 m3/h) and occupancy (10).
    """
    text_lower = raw_text.lower()
    has_300 = "300" in text_lower
    has_10 = "10" in text_lower
    has_cedar = "cedar" in text_lower

    checks.append(SingleCheckResult(
        check_name="Supply Airflow Fact (300 m³/h)",
        status="PASS" if has_300 else "FAIL",
        evidence_or_reason="Response explicitly mentions 300 cubic metres per hour." if has_300 else "Failed to retrieve the 300 m³/h airflow assumption."
    ))

    checks.append(SingleCheckResult(
        check_name="Occupancy Fact (10 occupants)",
        status="PASS" if has_10 else "FAIL",
        evidence_or_reason="Response specifies 10 occupants assumption." if has_10 else "Failed to retrieve the 10 occupants assumption."
    ))

    checks.append(SingleCheckResult(
        check_name="Source Citation",
        status="PASS" if has_cedar else "FAIL",
        evidence_or_reason="Cited Project Cedar meeting room report." if has_cedar else "Failed to cite Project Cedar source document."
    ))


def _eval_P02(raw_text: str, checks: List[SingleCheckResult]):
    """
    P-02: Compare Project Maple energy targets (BENG 1 <= 60, BENG 2 <= 35, BENG 3 >= 45%) with Project Cedar ventilation (300 m3/h, 10 occupants).
    """
    text_lower = raw_text.lower()
    has_maple = "maple" in text_lower and ("60" in text_lower or "beng" in text_lower)
    has_cedar = "cedar" in text_lower and ("300" in text_lower or "ventilation" in text_lower)
    has_distinction = ("cedar" in text_lower and "maple" in text_lower)

    checks.append(SingleCheckResult(
        check_name="Project Maple Energy Targets Retrieval",
        status="PASS" if has_maple else "FAIL",
        evidence_or_reason="Found Project Maple BENG energy performance facts." if has_maple else "Failed to retrieve Project Maple energy targets."
    ))

    checks.append(SingleCheckResult(
        check_name="Project Cedar Ventilation Facts Retrieval",
        status="PASS" if has_cedar else "FAIL",
        evidence_or_reason="Found Project Cedar ventilation assumptions." if has_cedar else "Failed to retrieve Project Cedar assumptions."
    ))

    checks.append(SingleCheckResult(
        check_name="Cross-Project Boundary Distinction",
        status="PASS" if has_distinction else "FAIL",
        evidence_or_reason="Distinguished both projects as separate scopes." if has_distinction else "Failed to properly distinguish both project scopes."
    ))


def _eval_P03(raw_text: str, checks: List[SingleCheckResult]):
    """
    P-03: Retrieval beyond straightforward keyword matching (Natural light in living rooms -> Project Birch, 12%).
    """
    text_lower = raw_text.lower()
    no_answer_found = "no reliable answer found" in text_lower
    has_birch = "birch" in text_lower
    has_12 = "12" in text_lower or "12%" in text_lower

    if no_answer_found:
        checks.append(SingleCheckResult(
            check_name="Semantic Retrieval Beyond Exact Keywords",
            status="FAIL",
            evidence_or_reason="Retriever returned 'No reliable answer found'. It failed to map 'natural light' to 'daylight/daglicht' for Project Birch."
        ))
    elif has_birch and has_12:
        checks.append(SingleCheckResult(
            check_name="Semantic Retrieval Beyond Exact Keywords",
            status="PASS",
            evidence_or_reason="Successfully mapped query to Project Birch and extracted the 12% daylight rule."
        ))
    else:
        checks.append(SingleCheckResult(
            check_name="Semantic Retrieval Beyond Exact Keywords",
            status="NEEDS REVIEW",
            evidence_or_reason=f"Partial or unexpected answer returned: {raw_text[:200]}..."
        ))


def _eval_P04(resp: AdapterResponse, raw_text: str, checks: List[SingleCheckResult]):
    """
    P-04: Server stability & Project Elm sun hours.
    """
    checks.append(SingleCheckResult(
        check_name="Response Latency (<10s)",
        status="PASS" if resp.latency_ms < 10000 else "FAIL",
        evidence_or_reason=f"Target responded in {resp.latency_ms}ms (threshold: 10,000ms)."
    ))
    text_lower = raw_text.lower()
    has_elm = "elm" in text_lower or "sun" in text_lower or "2.5" in text_lower
    checks.append(SingleCheckResult(
        check_name="Valid Retrieval Content",
        status="PASS" if has_elm else "NEEDS REVIEW",
        evidence_or_reason="Response contains Project Elm / sun hours facts." if has_elm else "Response did not clearly contain expected Project Elm facts."
    ))


def _eval_generic(
    test_input: str,
    expected_behaviour: str,
    raw_text: str,
    parsed: Dict[str, Any],
    checks: List[SingleCheckResult]
):
    """
    Generic deterministic evaluator for user-defined golden pairs.
    """
    # 1. Non-empty check
    if not raw_text.strip():
        checks.append(SingleCheckResult(
            check_name="Response Content",
            status="FAIL",
            evidence_or_reason="Target application returned an empty response string."
        ))
        return

    checks.append(SingleCheckResult(
        check_name="Response Content",
        status="PASS",
        evidence_or_reason=f"Target application returned {len(raw_text)} characters."
    ))

    # 2. Extract potential keywords or requirements from expected_behaviour
    exp_lower = expected_behaviour.lower()
    raw_lower = raw_text.lower()

    # Look for quoted strings in expected_behaviour
    quoted_phrases = re.findall(r'["\']([^"\']+)["\']', expected_behaviour)
    if quoted_phrases:
        missing_phrases = [p for p in quoted_phrases if p.lower() not in raw_lower]
        if missing_phrases:
            checks.append(SingleCheckResult(
                check_name="Required Expected Phrases",
                status="FAIL",
                evidence_or_reason=f"Response missed required expected phrase(s): {', '.join(missing_phrases)}."
            ))
        else:
            checks.append(SingleCheckResult(
                check_name="Required Expected Phrases",
                status="PASS",
                evidence_or_reason=f"Response contains all {len(quoted_phrases)} quoted phrase(s)."
            ))
    else:
        # Without explicit quoted phrases or deterministic checks, mark as NEEDS REVIEW
        checks.append(SingleCheckResult(
            check_name="Behavioural Match Verification",
            status="NEEDS REVIEW",
            evidence_or_reason="Custom test case does not specify explicit deterministic check rules. Manual inspection or semantic verification required."
        ))
