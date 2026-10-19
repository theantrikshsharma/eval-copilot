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
    elif CASE_INPUTS.get(pair_id) == test_input.strip():
        # Per-case checks apply only to the exact registered input, so a reused ID never inherits them
        _CASE_CHECKS[pair_id](parsed, raw_text, checks)
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
# Shared helpers for the per-case checks (A-05 onwards, P-05 onwards)
# ----------------------------------------------------

EXCERPT_CHARS = 180


def _add(checks: List[SingleCheckResult], name: str, status: str, evidence: str):
    checks.append(SingleCheckResult(check_name=name, status=status, evidence_or_reason=evidence))


def _needs_judgment(checks: List[SingleCheckResult], name: str, criterion: str):
    """A check only contextual judgment can decide. The LLM judge resolves it when configured; otherwise a human does."""
    _add(checks, name, "NEEDS REVIEW", f"Requires contextual judgment: {criterion}")


def _excerpt(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS].rstrip() + "…"


def _strings(value: Any) -> List[str]:
    """Every string value inside a JSON structure, without its keys."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [str(value)] if value is not None else []


# ----------------------------------------------------
# Target A (Anupma) per-case checks
# ----------------------------------------------------

REQUIRED_ITINERARY_FIELDS = ("destination", "travelers", "days")
_NUMBER_WORDS = {r"\bone\b": "1", r"\btwo\b": "2", r"\bthree\b": "3", r"\bfour\b": "4", r"\bcouple\b": "2 adults"}
_NEGATION = re.compile(r"\b(no|not|skip|skipping|avoid|avoiding|without|instead of|rather than)\b", re.I)
_FLAG_TERMS = (
    r"invalid|not (a )?valid|does ?n[o']t exist|not exist|please (provide|clarify|confirm|specify)|could you|"
    r"can you (confirm|clarify)|conflict|inconsistent|mismatch|is ?n[o']t possible|not possible|impossible|"
    r"infeasible|not feasible|unrealistic|not enough"
)


def _itinerary_days(parsed: Any) -> Optional[list]:
    days = parsed.get("days") if isinstance(parsed, dict) else None
    return days if isinstance(days, list) and days else None


def _is_empty_template(parsed: Any) -> bool:
    """A 'days' list whose every entry, and the destination, is blank: seen when the app gets no usable input."""
    return bool(_itinerary_days(parsed)) and not parsed.get("destination") and not _trip_lines(parsed)


def _prose(parsed: Any, raw_text: str) -> str:
    """The response's text without JSON keys, so a key like "destination" never reads as a question."""
    # evaluate_response passes {} for non-JSON replies, so an empty structure means "use the raw text"
    return " ".join(_strings(parsed)) if parsed and isinstance(parsed, (dict, list)) else raw_text


def _trip_lines(parsed: Any, extra_fields=()) -> List[str]:
    """One line per day base and activity (plus selected top-level fields), for context-aware term scans."""
    lines = []
    for day in _itinerary_days(parsed) or []:
        if not isinstance(day, dict):
            continue
        lines += _strings(day.get("base"))
        lines += [" ".join(_strings(a)) for a in day.get("activities") or []]
    for field in extra_fields:
        lines += _strings(parsed.get(field)) if isinstance(parsed, dict) else []
    return [line for line in lines if line.strip()]


def _scan_for_terms(checks, name: str, lines: List[str], pattern: str, what: str, is_ambiguous=None):
    """FAIL on a clear match, NEEDS REVIEW when the matching line's context may exclude it, PASS on none."""
    is_ambiguous = is_ambiguous or (lambda line: bool(_NEGATION.search(line)))
    hits = [(line, m.group(0)) for line in lines for m in [re.search(pattern, line, re.I)] if m]
    clear = [(line, term) for line, term in hits if not is_ambiguous(line)]
    if clear:
        found = "; ".join(f"'{_excerpt(line)}'" for line, _ in clear[:2])
        _add(checks, name, "FAIL", f"Found {what} ({clear[0][1]}): {found}")
    elif hits:
        line, term = hits[0]
        _add(checks, name, "NEEDS REVIEW", f"Mentions '{term}' in a context that may exclude it: '{_excerpt(line)}'")
    else:
        _add(checks, name, "PASS", f"No {what} found across {len(lines)} itinerary entries.")


def _check_flagged(checks, parsed, raw_text: str, problem: str, extra_terms: str):
    """For requests that can't be planned as stated: an itinerary without a flag is a FAIL."""
    name = "Invalid Request Flagged"
    days = _itinerary_days(parsed)
    prose = _prose(parsed, raw_text)
    flag = re.search(f"{_FLAG_TERMS}|{extra_terms}", prose, re.I)
    if _is_empty_template(parsed):
        _add(checks, name, "FAIL",
             f"Returned an empty itinerary template ({len(days)} days, every field blank) instead of flagging that {problem}.")
    elif days and not flag:
        _add(checks, name, "FAIL",
             f"Generated a {len(days)}-day itinerary for '{parsed.get('destination', 'unknown')}' "
             f"(travellers: '{parsed.get('travelers', 'unknown')}') without flagging that {problem}.")
    elif days:
        context = prose[max(0, flag.start() - 60): flag.end() + 60]
        _add(checks, name, "NEEDS REVIEW",
             f"Generated a {len(days)}-day itinerary but also says '{_excerpt(context)}'. "
             f"Decide whether that adequately flags that {problem}.")
    elif flag or "?" in prose:
        _add(checks, name, "PASS", f"Did not generate an itinerary; flagged or asked instead: '{_excerpt(prose)}'")
    else:
        _add(checks, name, "NEEDS REVIEW",
             f"Neither generated an itinerary nor clearly flagged that {problem}: '{_excerpt(prose)}'")


def _check_itinerary_shape(checks, parsed, raw_text: str, destination: str, n_days: int) -> bool:
    """Required fields, day count and destination for a complete request. Returns False when there is no itinerary."""
    days = _itinerary_days(parsed)
    if not days:
        _add(checks, "Required Itinerary Fields", "FAIL",
             f"Expected an itinerary for this complete request, but the response has no 'days' list: '{_excerpt(raw_text)}'")
        return False
    if _is_empty_template(parsed):
        _add(checks, "Required Itinerary Fields", "FAIL",
             f"Returned an empty itinerary template ({len(days)} days, every field blank) for a complete request.")
        return False
    missing = [f for f in REQUIRED_ITINERARY_FIELDS if not parsed.get(f)]
    _add(checks, "Required Itinerary Fields", "FAIL" if missing else "PASS",
         f"Missing field(s): {', '.join(missing)}." if missing
         else f"Itinerary includes {', '.join(REQUIRED_ITINERARY_FIELDS)}.")

    numbers = [d.get("day") for d in days if isinstance(d, dict)]
    expected = list(range(1, n_days + 1))
    if len(days) != n_days:
        _add(checks, "Day Count Matches Request", "FAIL",
             f"Requested {n_days} days; the itinerary has {len(days)} (day numbers {numbers}).")
    elif all(isinstance(n, int) for n in numbers) and numbers != expected:
        _add(checks, "Day Count Matches Request", "FAIL",
             f"Has {n_days} entries as requested, but the day numbers are {numbers} instead of {expected}.")
    else:
        _add(checks, "Day Count Matches Request", "PASS", f"Itinerary covers the requested {n_days} days.")

    actual = str(parsed.get("destination") or "")
    if actual:
        matches = destination.lower() in actual.lower()
        _add(checks, "Destination Matches Request", "PASS" if matches else "FAIL",
             f"Requested {destination}; destination field says '{actual}'.")
    return True


def _check_travellers(checks, parsed, request: str, required: List[str], forbidden: List[tuple]):
    """FAIL when the travellers field contradicts the request, PASS when it clearly matches, NEEDS REVIEW otherwise."""
    raw = parsed.get("travelers") if isinstance(parsed, dict) else None
    if not raw:
        return
    actual = str(raw)
    normalised = actual.lower()
    for pattern, replacement in _NUMBER_WORDS.items():
        normalised = re.sub(pattern, replacement, normalised)
    for pattern, reason in forbidden:
        if re.search(pattern, normalised):
            _add(checks, "Travellers Match Request", "FAIL",
                 f"Request was for {request}; travellers field says '{actual}' ({reason}).")
            return
    if all(re.search(p, normalised) for p in required):
        _add(checks, "Travellers Match Request", "PASS", f"Travellers field '{actual}' matches {request}.")
    else:
        _add(checks, "Travellers Match Request", "NEEDS REVIEW",
             f"Couldn't confirm that travellers field '{actual}' matches {request}.")


TWO_ADULTS = ([r"\b2\b", r"adult"], [(r"child|kid|famil|toddler|infant|baby|teen", "travellers that were not requested")])
_MEAT_OR_FISH = (
    r"\b(meat|jam[oó]n|ham|chorizo|chicken|beef|pork|lamb|veal|steak|bacon|sausages?|butifarra|seafood|fish|"
    r"prawns?|shrimps?|octopus|squid|calamari|anchov(y|ies)|tuna|salmon|cod|bacalao|mussels?|clams?)\b"
)
_FOOD_CONTEXT = re.compile(r"lunch|dinner|breakfast|brunch|\beat|meal|tapas|food|dish|tasting|restaurant|caf[eé]|snack|\btry\b", re.I)
_VEG_MARKER = re.compile(r"vegetarian|vegan|plant-based|meat-free", re.I)


def _eval_A05(parsed, raw_text, checks):
    """A-05: return date before departure date."""
    _check_flagged(checks, parsed, raw_text, "the return date (2026-12-05) is before the departure date (2026-12-10)",
                   r"before (the |your )?departure|earlier than|after (the |your )?return")


def _eval_A06(parsed, raw_text, checks):
    """A-06: requested duration conflicts with the dates."""
    _check_flagged(checks, parsed, raw_text, "a 5-day trip doesn't fit dates that span 3 days",
                   r"only (3|three) days|(3|three) days (between|from)|dates (only )?(cover|span|allow)|does ?n[o']t match")


def _eval_A07(parsed, raw_text, checks):
    """A-07: nonexistent date (2027 is not a leap year)."""
    _check_flagged(checks, parsed, raw_text, "2027-02-29 does not exist",
                   r"leap|february 29|29 february|feb 29")
    uses_date = bool(_itinerary_days(parsed)) and "2027-02-29" in _prose(parsed, raw_text)
    _add(checks, "Nonexistent Date Not Used", "FAIL" if uses_date else "PASS",
         f"Itinerary is scheduled on 2027-02-29, which doesn't exist (dates: '{parsed.get('dates')}')." if uses_date
         else "The response does not schedule anything on 2027-02-29.")


def _eval_A08(parsed, raw_text, checks):
    """A-08: 4-day Kyoto itinerary for 2 adults. Structure and defaults."""
    if _check_itinerary_shape(checks, parsed, raw_text, "Kyoto", 4):
        _check_travellers(checks, parsed, "2 adults", *TWO_ADULTS)


def _eval_A09(parsed, raw_text, checks):
    """A-09: 3-day Rome itinerary with no museums."""
    if not _check_itinerary_shape(checks, parsed, raw_text, "Rome", 3):
        return
    _check_travellers(checks, parsed, "2 adults", *TWO_ADULTS)
    _scan_for_terms(checks, "Excluded Activity Absent (museums)", _trip_lines(parsed),
                    r"\b(museums?|musei|museo|galler(y|ies)|sistine chapel|pinacoteca)\b", "a museum or gallery visit")


def _eval_A10(parsed, raw_text, checks):
    """A-10: 2-day Barcelona itinerary for strict vegetarians."""
    if not _check_itinerary_shape(checks, parsed, raw_text, "Barcelona", 2):
        return
    _check_travellers(checks, parsed, "2 adults", *TWO_ADULTS)
    diet = str(parsed.get("diet") or "")
    if _VEG_MARKER.search(diet):
        _add(checks, "Vegetarian Requirement Recorded", "PASS", f"Diet field records the requirement: '{diet}'.")
    elif re.search(r"no (dietary )?restrictions?|\bnone\b|\bany\b|omnivor", diet, re.I):
        _add(checks, "Vegetarian Requirement Recorded", "FAIL",
             f"User is strictly vegetarian, but the diet field defaults to '{diet}'.")
    else:
        _add(checks, "Vegetarian Requirement Recorded", "NEEDS REVIEW",
             f"Diet field doesn't clearly record the vegetarian requirement: '{diet or '(missing)'}'.")
    _scan_for_terms(checks, "No Meat or Fish Suggested", _trip_lines(parsed, ("food",)), _MEAT_OR_FISH,
                    "a meat or fish suggestion",
                    is_ambiguous=lambda line: bool(_VEG_MARKER.search(line) or _NEGATION.search(line)
                                                   or not _FOOD_CONTEXT.search(line)))


def _eval_A11(parsed, raw_text, checks):
    """A-11: 2-day Lisbon itinerary for 1 wheelchair user, step-free only."""
    if not _check_itinerary_shape(checks, parsed, raw_text, "Lisbon", 2):
        return
    _check_travellers(checks, parsed, "1 adult wheelchair user",
                      [r"\b1\b|solo|single", r"adult|travell?er|person|wheelchair"],
                      [(r"\b2 adults|\b[2-9] (people|persons|travell?ers)|famil|child", "more travellers than requested")])
    lines = _trip_lines(parsed, ("transport",))
    barriers = r"\b(stairs?|staircases?|steps|climb(s|ing)?|hik(e|es|ing)|steep|cobbl\w*|uphill|tram 28)\b"
    hits = [line for line in lines if re.search(barriers, line, re.I)]
    if hits:
        _add(checks, "Accessibility Barriers Mentioned", "NEEDS REVIEW",
             f"Mentions a possible barrier for a wheelchair user; check the context: '{_excerpt(hits[0])}'")
    else:
        _add(checks, "Accessibility Barriers Mentioned", "PASS",
             f"No stairs, climbs or steep routes mentioned across {len(lines)} itinerary entries.")
    _needs_judgment(checks, "Step-free, wheelchair-accessible activities (semantic)",
                    "every activity and transfer must be step-free and wheelchair accessible.")


def _eval_A12(parsed, raw_text, checks):
    """A-12: 2-day London itinerary for 2 adults and a 2-year-old."""
    if not _check_itinerary_shape(checks, parsed, raw_text, "London", 2):
        return
    _check_travellers(checks, parsed, "2 adults and a 2-year-old toddler",
                      [r"\b2 adults?\b|\b2 parents", r"toddler|2[- ]year|infant|baby|child|kid"],
                      [(r"^\W*2 adults?\W*$", "the toddler is missing")])
    _scan_for_terms(checks, "No Adult-Only Venues", _trip_lines(parsed),
                    r"\b(nightclubs?|pub crawl|bar crawl|cocktail bars?|wine tasting|casinos?)\b", "an adult-only venue")
    _needs_judgment(checks, "Toddler-appropriate activities (semantic)",
                    "every activity must suit a 2-year-old (naps, pushchair access, pace).")


def _eval_A13(parsed, raw_text, checks):
    """A-13: 3-day Paris trip for 2 adults on 300 EUR total. Flagging the budget or staying within it both pass."""
    if not _itinerary_days(parsed):
        prose = _prose(parsed, raw_text)
        flagged = re.search(r"budget|unrealistic|not enough|is ?n[o']t enough|insufficient|too low|exceed", prose, re.I)
        _add(checks, "Unrealistic Budget Flagged", "PASS" if flagged else "NEEDS REVIEW",
             f"No itinerary; flagged the budget instead: '{_excerpt(prose)}'" if flagged
             else f"No itinerary and no clear statement about the budget: '{_excerpt(prose)}'")
        return
    _check_itinerary_shape(checks, parsed, raw_text, "Paris", 3)
    _check_travellers(checks, parsed, "2 adults", *TWO_ADULTS)
    _scan_for_terms(checks, "No Luxury Options Within Budget", _trip_lines(parsed, ("comfort", "stays", "food", "transport")),
                    r"\b((5|five)[- ]star|luxury|michelin|ritz|four seasons|first[- ]class|private (driver|chauffeur|tour)|champagne)\b",
                    "an option that clearly exceeds 300 EUR in total")
    _needs_judgment(checks, "Plan credibly fits 300 EUR (semantic)",
                    "accommodation, transport, food and activities for 2 adults over 3 days must credibly total 300 EUR or less, "
                    "or the response must say the budget is unrealistic.")


def _eval_A14(parsed, raw_text, checks):
    """A-14: three cities, 4 hours each, in one day."""
    _check_flagged(checks, parsed, raw_text, "12 hours in three cities plus travel cannot fit in one day",
                   r"travel time|not enough time|too (much|many)|which (constraint|city|cities)|relax")


def _eval_A15(parsed, raw_text, checks):
    """A-15: no travel details at all."""
    _check_flagged(checks, parsed, raw_text, "the input contains no travel details",
                   r"destination|where would you|trip details|travel details|tell me more|which city|when (are|would) you")


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


# ----------------------------------------------------
# Target B (Prateek) per-case checks
# Prateek's knowledge base is intentionally fictional (his real company data is classified). Its five documents,
# served by /api/doc_return_all, are the source of truth: answers are judged against them, never against the
# real world, and fictional names or figures are never a failure in themselves.
# ----------------------------------------------------

_NO_ANSWER = re.compile(
    r"no reliable answer|not (available|mentioned|specified|provided|included|covered|stated)|"
    r"no (information|data|details|mention)|does ?n[o']t (contain|include|mention|specify|provide)|"
    r"cannot (find|answer)|could ?n[o']t find|insufficient information|unable to (find|answer)",
    re.I,
)


def _answer_body(raw_text: str) -> str:
    """The answer itself: before the Source section, without the standard verification footer."""
    body = re.split(r"\*\*Sources?:?\*\*|\n\s*Sources?:", raw_text, maxsplit=1, flags=re.I)[0]
    return body.split("Verification required:")[0]


def _abstains(raw_text: str) -> bool:
    return bool(_NO_ANSWER.search(_answer_body(raw_text)))


def _has_number(text: str, number: str) -> bool:
    """Exact numeric match: '35' never matches '350', and '2.5' never matches '25'."""
    return bool(re.search(rf"(?<![\d.]){re.escape(number)}(?!\d|[.,]\d)", text))


def _check_retrieved(checks, raw_text: str, name: str, pattern: str, question: str) -> bool:
    """One retrieval check. Returns False on a miss, so later fact checks don't repeat the same failure."""
    if _abstains(raw_text):
        _add(checks, name, "FAIL", f"Returned no answer for {question}, though the documents cover it: '{_excerpt(_answer_body(raw_text))}'")
        return False
    found = re.search(pattern, raw_text, re.I)
    _add(checks, name, "PASS" if found else "FAIL",
         f"Identified the source: '{_excerpt(raw_text[max(0, found.start() - 60): found.end() + 60])}'" if found
         else f"Answered without identifying the right project or document: '{_excerpt(_answer_body(raw_text))}'")
    return True


def _check_fact(checks, name: str, found: bool, passed: str, failed: str):
    _add(checks, name, "PASS" if found else "FAIL", passed if found else failed)


def _check_absent_fact(checks, raw_text: str, invented_pattern: str, invented_what: str):
    """For facts that aren't in any document: inventing a value fails, saying it's unavailable passes."""
    name = "Absent Fact Acknowledged"
    invented = re.search(invented_pattern, _answer_body(raw_text), re.I)
    if invented:
        _add(checks, name, "FAIL", f"Invented {invented_what} that no document contains: '{invented.group(0)}'")
    elif _abstains(raw_text):
        _add(checks, name, "PASS", f"Said the information isn't available: '{_excerpt(_answer_body(raw_text))}'")
    else:
        _add(checks, name, "NEEDS REVIEW",
             f"Neither says the information is unavailable nor gives a value; check whether it borrows facts "
             f"from an unrelated document: '{_excerpt(_answer_body(raw_text))}'")


def _eval_P05(parsed, raw_text, checks):
    """P-05: paraphrased Elm question without the keywords 'Elm' or 'sun'."""
    if not _check_retrieved(checks, raw_text, "Correct Document Retrieved (Project Elm)", r"\belm\b",
                            "a paraphrased question about Project Elm"):
        return
    body = _answer_body(raw_text)
    _check_fact(checks, "Sunlight Duration (2.5 hours)", _has_number(body, "2.5"),
                "States the 2.5-hour minimum.", f"Does not state the 2.5-hour minimum: '{_excerpt(body)}'")
    _check_fact(checks, "Reference Date (21 March)", bool(re.search(r"21(st)? march|march 21", body, re.I)),
                "States the 21 March reference date.", f"Does not state the 21 March reference date: '{_excerpt(body)}'")


def _eval_P06(parsed, raw_text, checks):
    """P-06: BENG 2 (fossil energy) for the unnamed new-build project, which is Project Maple."""
    if not _check_retrieved(checks, raw_text, "Project Maple Identified", r"\bmaple\b",
                            "an indirect question about Project Maple"):
        return
    body = _answer_body(raw_text)
    name = "BENG 2 Value (35 kWh/m2/yr)"
    if _has_number(body, "35"):
        _add(checks, name, "PASS", "States the BENG 2 maximum of 35 kWh/m2 per year.")
    elif _has_number(body, "60"):
        _add(checks, name, "FAIL", f"Gave 60, Project Maple's BENG 1 limit, instead of the BENG 2 maximum of 35: '{_excerpt(body)}'")
    else:
        _add(checks, name, "FAIL", f"Does not state the BENG 2 maximum of 35 kWh/m2 per year: '{_excerpt(body)}'")


def _eval_P07(parsed, raw_text, checks):
    """P-07: no document gives a noise requirement for Project Cedar."""
    _check_absent_fact(checks, raw_text, r"\d+(\.\d+)?\s*d\s?B", "a noise level for Project Cedar")


def _eval_P08(parsed, raw_text, checks):
    """P-08: no document gives a fire safety rating for Project Birch."""
    _check_absent_fact(checks, raw_text, r"\b(REI|EI|RE|R)\s?\d{2,3}\b|\bclass\s+[A-F]\d?\b|\beuroclass\b|\d+\s*(min|minutes)\b",
                       "a fire safety rating for Project Birch")


def _eval_P09(parsed, raw_text, checks):
    """P-09: false premise of 60% renewable; the document says BENG 3 is at least 45%."""
    name = "False Premise Corrected (45%, not 60%)"
    body = _answer_body(raw_text)
    has_45, has_60 = _has_number(body, "45"), _has_number(body, "60")
    if has_45 and has_60:
        _add(checks, name, "NEEDS REVIEW", f"States both 45% and 60%; decide whether it corrects the premise: '{_excerpt(body)}'")
    elif has_45:
        _add(checks, name, "PASS", f"Gives the documented 45 percent minimum: '{_excerpt(body)}'")
    elif _abstains(raw_text):
        _add(checks, name, "FAIL", "Did not correct the premise, though Project Maple's document states a 45 percent minimum (BENG 3).")
    elif has_60:
        _add(checks, name, "FAIL", f"Accepted the false 60 percent premise: '{_excerpt(body)}'")
    else:
        _add(checks, name, "NEEDS REVIEW", f"Neither corrects nor repeats the premise: '{_excerpt(body)}'")


def _eval_P10(parsed, raw_text, checks):
    """P-10: 12% of a 25 m2 living room is 3 m2 (worked example in the Birch document)."""
    if not _check_retrieved(checks, raw_text, "Project Birch Document Retrieved", r"\bbirch\b",
                            "a question about Project Birch's daylight rule"):
        return
    body = _answer_body(raw_text)
    area = re.search(r"(?<![\d.])3(\.0+)?\s*(m²|m2|m\^2|square met(re|er)s?|sq\.? ?m)", body, re.I)
    _check_fact(checks, "Derived Area (3 m²)", bool(area),
                "States at least 3 m² of equivalent daylight area.", f"Does not state 3 m²: '{_excerpt(body)}'")


def _eval_P11(parsed, raw_text, checks):
    """P-11: cumulative vs combined noise."""
    if not _check_retrieved(checks, raw_text, "Noise Definitions Retrieved", r"cumulative|combined",
                            "a question about the noise definitions"):
        return
    body = _answer_body(raw_text)
    _check_fact(checks, "Annoyance Correction Mentioned", bool(re.search(r"annoyance", body, re.I)),
                "Mentions the annoyance correction.", f"Does not mention the annoyance correction: '{_excerpt(body)}'")
    _check_fact(checks, "Indoor Noise Level Use Mentioned", bool(re.search(r"indoor", body, re.I)),
                "Mentions that combined noise determines the indoor noise level.",
                f"Does not mention the indoor noise level: '{_excerpt(body)}'")
    _needs_judgment(checks, "Correction attributed to cumulative, not combined (semantic)",
                    "the annoyance correction must belong to cumulative noise, and combined noise must be described as uncorrected.")


def _eval_P12(parsed, raw_text, checks):
    """P-12: Elm's sun hours are Section 3.1 of the Elm policy. The noise report is also Section 3.1."""
    if not _check_retrieved(checks, raw_text, "Project Elm Document Cited", r"project elm|sun hours",
                            "a citation question about Project Elm"):
        return
    _check_fact(checks, "Section 3.1 Cited", _has_number(raw_text, "3.1"),
                "Cites Section 3.1.", f"Does not cite Section 3.1: '{_excerpt(raw_text)}'")
    cites_noise = bool(re.search(r"noise definitions", raw_text, re.I))
    _add(checks, "Noise Report Not Cited", "FAIL" if cites_noise else "PASS",
         "Cites the noise definitions report, which is a different document that also uses Section 3.1." if cites_noise
         else "Does not cite the unrelated noise definitions report.")


_NATIONAL_SCOPE = re.compile(r"dutch|netherlands|national|new homes|building decree|bouwbesluit|\bbbl\b", re.I)
_SCOPE_NEGATION = re.compile(r"\b(not|no|isn't|is not|doesn't|does not|don't|do not|only)\b", re.I)


def _eval_P13(parsed, raw_text, checks):
    """
    P-13: scope grounding. The only BENG 1 figure in the documents is Project Maple's own target (60), and that
    document says it does not state national regulations. Judges the answer's claim, not whether words like
    'fictional' appear anywhere.
    """
    name = "Project Target Not Presented As National Regulation"
    body = _answer_body(raw_text)
    if _abstains(raw_text):
        _add(checks, name, "PASS", f"Said the documents don't give national limits: '{_excerpt(body)}'")
        return
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", body) if _has_number(s, "60")]
    generalised = [s for s in sentences
                   if _NATIONAL_SCOPE.search(s) and not re.search(r"\bmaple\b", s, re.I) and not _SCOPE_NEGATION.search(s)]
    if generalised:
        _add(checks, name, "FAIL",
             f"Presents Project Maple's project target as the national limit, which its source document says it is not: "
             f"'{_excerpt(generalised[0])}'")
    elif sentences:
        _add(checks, name, "NEEDS REVIEW",
             f"Gives 60 kWh/m2 per year with a project attribution or qualification; decide whether the answer still "
             f"presents it as the national limit: '{_excerpt(sentences[0])}'")
    else:
        _add(checks, name, "NEEDS REVIEW", f"Neither declines nor gives a figure: '{_excerpt(body)}'")


# ----------------------------------------------------
# Registry: each per-case check set is tied to its exact pre-registered input
# ----------------------------------------------------

CASE_INPUTS: Dict[str, str] = {
    "A-05": "Plan a trip to Rome from 2026-12-10 to 2026-12-05 for 2 adults.",
    "A-06": "Plan a 5-day trip to Lisbon from 2026-11-02 to 2026-11-04 for 2 adults.",
    "A-07": "Plan a trip to Amsterdam from 2027-02-29 to 2027-03-02 for 2 adults.",
    "A-08": "Plan a 4-day trip to Kyoto for 2 adults.",
    "A-09": "Plan a 3-day trip to Rome for 2 adults. Do not include any museums.",
    "A-10": "Plan a 2-day trip to Barcelona for 2 adults. We are strictly vegetarian, so every meal suggestion must be vegetarian.",
    "A-11": "Plan a 2-day trip to Lisbon for 1 adult who uses a wheelchair. Every activity must be step-free and wheelchair accessible.",
    "A-12": "Plan a 2-day trip to London for 2 adults and a 2-year-old toddler.",
    "A-13": "Plan a 3-day trip to Paris for 2 adults with a total budget of 300 EUR, including accommodation.",
    "A-14": "Plan a 1-day trip for 2 adults that visits Kyoto, Osaka and Hiroshima, spending at least 4 hours in each city.",
    "A-15": "What's the weather like?",
    "P-05": "On the March equinox, what is the minimum daily exposure required at the centre of the main living-room window for the Exampletown housing project?",
    "P-06": "For the invented new-build housing project, what is the maximum allowed primary fossil energy use?",
    "P-07": "What is the indoor noise level requirement for the Project Cedar meeting room?",
    "P-08": "What fire safety rating is required for Project Birch?",
    "P-09": "Why does Project Maple require a renewable energy share of at least 60 percent?",
    "P-10": "How much equivalent daylight area does a 25 m2 living room in Project Birch need?",
    "P-11": "What is the difference between cumulative noise and combined noise?",
    "P-12": "Which document and section describe the sun hours requirement for Project Elm?",
    "P-13": "What are the actual Dutch BENG 1 limits for new homes?",
}

_CASE_CHECKS = {
    "A-05": _eval_A05, "A-06": _eval_A06, "A-07": _eval_A07, "A-08": _eval_A08, "A-09": _eval_A09,
    "A-10": _eval_A10, "A-11": _eval_A11, "A-12": _eval_A12, "A-13": _eval_A13, "A-14": _eval_A14,
    "A-15": _eval_A15,
    "P-05": _eval_P05, "P-06": _eval_P06, "P-07": _eval_P07, "P-08": _eval_P08, "P-09": _eval_P09,
    "P-10": _eval_P10, "P-11": _eval_P11, "P-12": _eval_P12, "P-13": _eval_P13,
}


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
