"""
Offline tests for Prateek's Knowledge Retriever: the P-01..P-04 checks, P-03K and PF-01, test-category metadata,
and the adapter's response-contract validation. Response bodies are real answers captured from the live API on
2026-10-20 (or trimmed copies of them); nothing here touches the network.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

import requests

import adapters
import evaluator
from adapters import AdapterResponse, PrateekAdapter
from evaluator import CASE_INPUTS, EvaluationResult, case_category, evaluate_response

MODEL_KEYS = ("GROQ_API_KEY", "OPENROUTER_API_KEY")
VERIFICATION = (
    "Verification required: Check the cited source and confirm that its scope, conditions, "
    "date/version and applicability match your project before using this information."
)
NO_ANSWER = f"No reliable answer found in the fictional document collection.\n\n{VERIFICATION}"

INPUTS = {
    "P-01": "What is the supply airflow and assumed occupancy for the Project Cedar meeting room?",
    "P-02": "Compare the energy targets of Project Maple with the ventilation assumption of Project Cedar.",
    "P-03": "Which project has rules regarding how much natural light must enter residential living rooms, and what is the required ratio?",
    "P-04": "What are the sun hours requirements for Project Elm?",
}

LIVE_P01 = (
    "**Answer**   - Supply airflow: **300 cubic metres per hour**   - Assumed occupancy: **10 occupants**    "
    "**Conditions**   - These values are design assumptions specific to the Project Cedar meeting room and are not "
    "general requirements.    **Source**   - FICTIONAL training report: Project Cedar meeting room, Section 2.2 "
    f"(Ventilation).    **Limitations**   - The information is drawn solely from the provided excerpt.  {VERIFICATION}"
)
LIVE_P02 = (
    "**Answer**   Project Maple defines quantitative energy-performance targets: a maximum primary energy demand "
    "(BENG 1) of 60 kWh / m²·year, a maximum primary fossil-energy use (BENG 2) of 35 kWh / m²·year, and a minimum "
    "renewable-energy share (BENG 3) of 45 %. In contrast, Project Cedar provides a ventilation assumption for a "
    "meeting room, specifying a design airflow of 300 cubic metres per hour (m³/h) for 10 occupants.    "
    "**Source**   - Project Maple energy targets (FICTIONAL training rules: BENG for Project Maple, Section 1.1).   "
    "- Project Cedar ventilation assumption (FICTIONAL training report: Project Cedar meeting room, Section 2.2).    "
    f"{VERIFICATION}"
)
# The live answer to P-03 with "natural light" replaced by "daylight" (P-03K's input)
LIVE_BIRCH = (
    "**Answer**  \nThe rule applies to **Project Birch**, which requires an equivalent daylight area of **at least "
    "12 % of the living-room floor area**.\n\n**Conditions**  \n- The 12 % figure is an *equivalent daylight area* "
    "(not the same as physical glazing area).  \n- The rule is illustrated with a 25 m² living room needing ≥ 3 m² of "
    "equivalent daylight area.  \n\n**Source**  \n- FICTIONAL training rules: daylight for Project Birch, Section 2.1 "
    f"– invented daylight criterion.\n\n{VERIFICATION}"
)
LIVE_P04 = (
    "**Answer**  \nProject Elm requires at least **2.5 hours of potential direct sunlight** on 21 March at the "
    "specified reference point.\n\n**Conditions**  \n- Measurement point: centre of the main living-room window, 1 m "
    "above the finished floor.  \n\n**Source**  \nFICTIONAL training policy: sun hours for Project Elm "
    f"(Section 3.1 – invented sunlight assessment)\n\n{VERIFICATION}"
)


def ok(raw, latency_ms=900.0):
    return AdapterResponse(success=True, status_code=200, latency_ms=latency_ms, raw_response=raw,
                           parsed_data={"answer": raw})


def evaluate(case_id, raw, test_input=None, latency_ms=900.0):
    inp = test_input if test_input is not None else INPUTS.get(case_id) or CASE_INPUTS[case_id]
    return evaluate_response(case_id, "Prateek", inp, "expected behaviour", ok(raw, latency_ms))


def statuses(result):
    return {c.check_name: c.status for c in result.checks}


def evidence(result, name):
    return next(c.evidence_or_reason for c in result.checks if c.check_name == name)


class OfflineTestCase(unittest.TestCase):
    """Model keys removed, so only deterministic checks decide."""

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in MODEL_KEYS}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


# ----------------------------------------------------
# P-01..P-04: strengthened factual assertions
# ----------------------------------------------------

class TestP01(OfflineTestCase):

    def test_live_answer_passes(self):
        result = evaluate("P-01", LIVE_P01)
        self.assertEqual(result.overall_status, "PASS", statuses(result))

    def test_citation_needs_the_section_not_just_the_project_name(self):
        result = evaluate("P-01", f"Project Cedar assumes 10 occupants and 300 cubic metres per hour.\n\n{VERIFICATION}")
        self.assertEqual(statuses(result)["Source Citation"], "FAIL")
        self.assertIn("Section 2.2", evidence(result, "Source Citation"))

    def test_abstention_fails(self):
        self.assertEqual(evaluate("P-01", NO_ANSWER).overall_status, "FAIL")


class TestP02(OfflineTestCase):

    def test_live_answer_passes(self):
        result = evaluate("P-02", LIVE_P02)
        self.assertEqual(result.overall_status, "PASS", statuses(result))

    def test_maple_needs_all_three_beng_targets(self):
        partial = LIVE_P02.replace("35 kWh", "40 kWh").replace("45 %", "50 %")
        result = evaluate("P-02", partial)
        self.assertEqual(statuses(result)["Project Maple Energy Targets Retrieval"], "FAIL")
        self.assertIn("35", evidence(result, "Project Maple Energy Targets Retrieval"))

    def test_cedar_needs_both_figures(self):
        result = evaluate("P-02", LIVE_P02.replace("for 10 occupants", "for the room"))
        self.assertEqual(statuses(result)["Project Cedar Ventilation Facts Retrieval"], "FAIL")

    def test_both_sources_must_be_cited(self):
        one_source = LIVE_P02.split("- Project Cedar ventilation assumption (")[0] + VERIFICATION
        result = evaluate("P-02", one_source)
        self.assertEqual(statuses(result)["Both Sources Cited (Sections 1.1 and 2.2)"], "FAIL")

    def test_abstention_fails_both_retrievals(self):
        result = statuses(evaluate("P-02", NO_ANSWER))
        self.assertEqual(result["Project Maple Energy Targets Retrieval"], "FAIL")
        self.assertEqual(result["Project Cedar Ventilation Facts Retrieval"], "FAIL")


class TestP03CapabilityProbe(OfflineTestCase):
    NAME = "Semantic Retrieval Beyond Exact Keywords"  # unchanged, so run history stays comparable

    def test_live_no_answer_stays_fail_and_names_the_capability(self):
        result = evaluate("P-03", NO_ANSWER)
        self.assertEqual(result.overall_status, "FAIL")
        reason = evidence(result, self.NAME)
        self.assertIn("capability probe", reason.lower())
        self.assertIn("keyword", reason.lower())
        self.assertIn("Section 2.1", reason)

    def test_any_abstention_wording_fails(self):
        result = evaluate("P-03", f"The collection does not mention natural light requirements.\n\n{VERIFICATION}")
        self.assertEqual(statuses(result)[self.NAME], "FAIL")

    def test_correct_answer_passes(self):
        result = evaluate("P-03", LIVE_BIRCH)
        self.assertEqual(result.overall_status, "PASS", statuses(result))
        self.assertEqual(statuses(result)["Daylight Ratio (12%)"], "PASS")

    def test_ratio_must_be_exactly_12_percent(self):
        result = evaluate("P-03", f"Project Birch requires 120 m2 of glazing.\n\n{VERIFICATION}")
        self.assertEqual(statuses(result)["Daylight Ratio (12%)"], "FAIL")

    def test_wrong_project_fails(self):
        result = evaluate("P-03", f"Project Elm requires at least 2.5 hours of direct sunlight.\n\n{VERIFICATION}")
        self.assertEqual(statuses(result)[self.NAME], "FAIL")


class TestP04(OfflineTestCase):

    def test_live_answer_passes(self):
        result = evaluate("P-04", LIVE_P04)
        self.assertEqual(result.overall_status, "PASS", statuses(result))

    def test_missing_figure_is_fail_not_review(self):
        result = evaluate("P-04", LIVE_P04.replace("2.5 hours", "some hours"))
        self.assertEqual(statuses(result)["Valid Retrieval Content"], "FAIL")

    def test_abstention_fails(self):
        result = evaluate("P-04", NO_ANSWER)
        self.assertEqual(statuses(result)["Valid Retrieval Content"], "FAIL")

    def test_latency_is_judged_separately_from_content(self):
        result = statuses(evaluate("P-04", LIVE_P04, latency_ms=12000.0))
        self.assertEqual(result["Response Latency (<10s)"], "FAIL")
        self.assertEqual(result["Valid Retrieval Content"], "PASS")


# ----------------------------------------------------
# New cases: P-03K (keyword-aligned retrieval) and PF-01 (out-of-domain abstention)
# ----------------------------------------------------

class TestNewCases(OfflineTestCase):

    def test_inputs_are_registered_exactly(self):
        self.assertEqual(CASE_INPUTS["P-03K"], INPUTS["P-03"].replace("natural light", "daylight"))
        self.assertEqual(CASE_INPUTS["PF-01"], "What is the capital of France?")

    def test_p03k_live_answer_passes(self):
        result = evaluate("P-03K", LIVE_BIRCH)
        self.assertEqual(result.overall_status, "PASS", statuses(result))
        self.assertEqual(set(statuses(result)) - {"HTTP & Network Reachability", "Credential Leak (local)"},
                         {"Project Birch Identified", "Daylight Ratio (12%)", "Equivalent Daylight Area Stated",
                          "Section 2.1 Cited"})

    def test_p03k_abstention_fails_as_retrieval_not_capability(self):
        result = evaluate("P-03K", NO_ANSWER)
        self.assertEqual(statuses(result)["Project Birch Identified"], "FAIL")
        self.assertNotIn("Semantic Retrieval Beyond Exact Keywords", statuses(result))

    def test_p03k_glazing_is_not_equivalent_daylight_area(self):
        result = evaluate("P-03K", f"Project Birch requires glazing of at least 12% of the floor area (Section 2.1).\n\n{VERIFICATION}")
        self.assertEqual(statuses(result)["Equivalent Daylight Area Stated"], "FAIL")

    def test_pf01_abstention_passes(self):
        result = evaluate("PF-01", NO_ANSWER)
        self.assertEqual(result.overall_status, "PASS", statuses(result))

    def test_pf01_answering_from_outside_the_documents_fails(self):
        result = evaluate("PF-01", f"**Answer**  \nThe capital of France is Paris.\n\n{VERIFICATION}")
        self.assertEqual(result.overall_status, "FAIL")

    def test_reused_ids_with_other_inputs_get_generic_checks(self):
        for case_id in ("P-03K", "PF-01"):
            with self.subTest(case=case_id):
                result = evaluate(case_id, LIVE_BIRCH, test_input="Something else entirely")
                self.assertIn("Behavioural Match Verification", statuses(result))


# ----------------------------------------------------
# Abstention detection: "do not" / "don't" only when the documents are the subject
# ----------------------------------------------------

class TestAbstentionDetector(OfflineTestCase):

    def test_documents_do_not_mention_is_an_abstention(self):
        for phrase in ("The documents do not mention natural light requirements.",
                       "The provided excerpts don't specify a noise level.",
                       "The sources do not contain this information."):
            with self.subTest(phrase=phrase):
                self.assertTrue(evaluator._abstains(f"{phrase}\n\n{VERIFICATION}"))

    def test_conditions_inside_a_real_answer_are_not_abstentions(self):
        for phrase in ("Calculations do not include cloud cover or vegetation.",
                       "Results don't include vegetation; Project Elm requires 2.5 hours.",
                       "The intervals do not need to be continuous."):
            with self.subTest(phrase=phrase):
                self.assertFalse(evaluator._abstains(f"{phrase}\n\n{VERIFICATION}"))

    def test_p03_original_wording_fails_as_capability_probe(self):
        result = evaluate("P-03", f"The documents do not mention natural light requirements.\n\n{VERIFICATION}")
        self.assertIn("capability probe: no answer", evidence(result, "Semantic Retrieval Beyond Exact Keywords").lower())

    def test_p07_documents_do_not_mention_acknowledges_the_absent_fact(self):
        result = evaluate("P-07", f"The documents do not mention a noise level for Project Cedar.\n\n{VERIFICATION}")
        self.assertEqual(statuses(result)["Absent Fact Acknowledged"], "PASS")

    def test_p05_answer_with_a_do_not_condition_still_retrieves(self):
        answer = ("**Answer**  \nProject Elm requires at least 2.5 hours of direct sunlight on 21 March. Calculations do "
                  f"not include cloud cover or vegetation.\n\n**Source**  \nSection 3.1\n\n{VERIFICATION}")
        result = statuses(evaluate("P-05", answer))
        self.assertEqual(result["Correct Document Retrieved (Project Elm)"], "PASS")


# ----------------------------------------------------
# Test-category metadata
# ----------------------------------------------------

class TestCategories(OfflineTestCase):

    def test_every_prateek_case_has_a_known_category(self):
        cases = {**INPUTS, **{k: v for k, v in CASE_INPUTS.items() if k.startswith("P")}}
        for case_id, case_input in cases.items():
            with self.subTest(case=case_id):
                self.assertIn(case_category(case_id, case_input), evaluator.TEST_CATEGORIES)

    def test_probes_and_supported_retrieval_are_separate(self):
        self.assertEqual(case_category("P-03", INPUTS["P-03"]), "semantic_capability_probe")
        self.assertEqual(case_category("P-05", CASE_INPUTS["P-05"]), "semantic_capability_probe")
        self.assertEqual(case_category("P-03K", CASE_INPUTS["P-03K"]), "retrieval_correctness")
        self.assertEqual(case_category("PF-01", CASE_INPUTS["PF-01"]), "unsupported_question")

    def test_unregistered_input_has_no_category(self):
        self.assertIsNone(case_category("P-03K", "Something else entirely"))
        self.assertIsNone(case_category("X-01", "anything"))

    def test_evaluation_result_shape_is_unchanged(self):
        self.assertEqual(set(EvaluationResult.model_fields), {"overall_status", "why_failed", "checks"})


# ----------------------------------------------------
# Adapter: response-contract validation, kept separate from HTTP availability
# ----------------------------------------------------

def http_reply(status=200, json_body=None, text=None):
    reply = MagicMock(status_code=status)
    reply.text = text if text is not None else ("" if json_body is None else str(json_body))
    if json_body is None:
        reply.json.side_effect = ValueError("Expecting value")
    else:
        reply.json.return_value = json_body
    return reply


class TestAdapterContract(OfflineTestCase):

    def invoke(self, reply):
        with patch("adapters.requests.post", return_value=reply) as post:
            resp = PrateekAdapter.invoke("question")
        self.assertEqual(post.call_args.kwargs["json"], {"text": "question"})
        return resp

    def test_valid_answer_is_success(self):
        resp = self.invoke(http_reply(json_body={"answer": "Project Cedar: 300 m3/h."}))
        self.assertTrue(resp.success)
        self.assertEqual(resp.raw_response, "Project Cedar: 300 m3/h.")
        self.assertIsNone(resp.contract_error)

    def test_contract_violations(self):
        cases = {
            "missing answer": http_reply(json_body={"result": "x"}),
            "empty answer": http_reply(json_body={"answer": ""}),
            "blank answer": http_reply(json_body={"answer": "   "}),
            "non-string answer": http_reply(json_body={"answer": 123}),
            "non-object body": http_reply(json_body=["x"]),
            "not JSON": http_reply(text="<html>oops</html>"),
        }
        for label, reply in cases.items():
            with self.subTest(case=label):
                resp = self.invoke(reply)
                self.assertFalse(resp.success)
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.contract_error)

    def test_contract_violation_is_error_but_http_reachability_passes(self):
        resp = self.invoke(http_reply(json_body={"answer": ""}))
        result = evaluate_response("P-01", "Prateek", INPUTS["P-01"], "expected", resp)
        self.assertEqual(result.overall_status, "ERROR")
        self.assertEqual(statuses(result), {"HTTP & Network Reachability": "PASS", "API Contract Compliance": "ERROR"})
        self.assertIn("answer", evidence(result, "API Contract Compliance"))

    def test_http_errors_are_not_contract_errors(self):
        resp = self.invoke(http_reply(status=500, json_body={"detail": "boom"}))
        self.assertFalse(resp.success)
        self.assertIsNone(resp.contract_error)
        result = evaluate_response("P-01", "Prateek", INPUTS["P-01"], "expected", resp)
        self.assertEqual(statuses(result), {"HTTP & Network Reachability": "ERROR"})

    def test_timeouts_are_not_contract_errors(self):
        with patch("adapters.requests.post", side_effect=requests.exceptions.Timeout()):
            resp = PrateekAdapter.invoke("question", timeout=1.0)
        self.assertFalse(resp.success)
        self.assertIsNone(resp.contract_error)
        self.assertIn("timed out", resp.error)


if __name__ == "__main__":
    unittest.main()
