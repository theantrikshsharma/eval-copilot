"""
Offline tests for the per-case checks (A-05 to A-15, P-05 to P-13).
Responses are hand-built in each app's real format, so nothing touches the network.
The LLM judge is disabled except in the integration tests at the bottom.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import tests  # noqa: F401  isolation first: no .env, no real credentials (see tests/__init__.py)

from adapters import AdapterResponse
from evaluator import CASE_INPUTS, evaluate_response
from judges import ModelReply

MODEL_KEYS = ("GROQ_API_KEY", "OPENROUTER_API_KEY")
VERIFICATION = (
    "Verification required: Check the cited source and confirm that its scope, conditions, "
    "date/version and applicability match your project before using this information."
)
ELM_SOURCE = "FICTIONAL training policy: sun hours for Project Elm, Section 3.1 (Sunlight / bezonning)."
NOISE_SOURCE = "FICTIONAL training report: noise definitions, Section 3.1 (Acoustics)."
MAPLE_SOURCE = "FICTIONAL training rules: BENG for Project Maple, Section 1.1 (Energy performance)."
BIRCH_SOURCE = "FICTIONAL training rules: daylight for Project Birch, Section 2.1 (Daylight)."
NO_ANSWER = f"No reliable answer found in the fictional document collection.\n\n{VERIFICATION}"


def itinerary(destination, n_days, travelers="2 adults", diet="No restrictions", activities=None, **extra):
    days = [
        {
            "day": i + 1,
            "base": f"Hotel in central {destination}",
            "activities": activities[i] if activities else [f"Morning walk through {destination} old town"],
        }
        for i in range(n_days)
    ]
    data = {
        "destination": destination, "dates": "2026-11-02 to 2026-11-05", "travelers": travelers,
        "pace": "moderate", "transport": "public transport", "comfort": "mid-range", "diet": diet,
        "days": days, **extra,
    }
    return AdapterResponse(success=True, status_code=200, latency_ms=900.0,
                           raw_response=json.dumps(data, indent=2), parsed_data=data)


def text(raw):
    return AdapterResponse(success=True, status_code=200, latency_ms=900.0, raw_response=raw, parsed_data=None)


def answer(body, source):
    return text(f"**Answer**  \n{body}\n\n**Source**  \n- {source}\n\n{VERIFICATION}")


def evaluate(case_id, resp, test_input=None):
    target = "Anupma" if case_id.startswith("A-") else "Prateek"
    inp = CASE_INPUTS[case_id] if test_input is None else test_input
    return evaluate_response(case_id, target, inp, "expected behaviour", resp)


def statuses(result):
    return {c.check_name: c.status for c in result.checks}


def evidence(result, name):
    return next(c.evidence_or_reason for c in result.checks if c.check_name == name)


class OfflineTestCase(unittest.TestCase):
    """Runs with model keys removed, so only deterministic checks decide."""

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in MODEL_KEYS}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


# ----------------------------------------------------
# Anupma: requests that should be flagged, not planned
# ----------------------------------------------------

class TestFlagInsteadOfPlan(OfflineTestCase):

    def test_unflagged_itinerary_for_reversed_dates_fails(self):
        result = evaluate("A-05", itinerary("Rome", 5))
        self.assertEqual(statuses(result)["Invalid Request Flagged"], "FAIL")
        self.assertEqual(result.overall_status, "FAIL")
        self.assertIn("5-day itinerary", evidence(result, "Invalid Request Flagged"))

    def test_question_about_reversed_dates_passes(self):
        resp = text("Your return date (2026-12-05) is before your departure date (2026-12-10). Could you confirm the correct dates?")
        result = evaluate("A-05", resp)
        self.assertEqual(statuses(result)["Invalid Request Flagged"], "PASS")
        self.assertEqual(result.overall_status, "PASS")

    def test_itinerary_with_a_warning_needs_review(self):
        result = evaluate("A-05", itinerary("Rome", 5, notes="The return date is before the departure date, so I swapped them."))
        self.assertEqual(statuses(result)["Invalid Request Flagged"], "NEEDS REVIEW")

    def test_reply_that_neither_plans_nor_flags_needs_review(self):
        result = evaluate("A-05", text("Sure! Rome is lovely in December."))
        self.assertEqual(statuses(result)["Invalid Request Flagged"], "NEEDS REVIEW")

    def test_itinerary_using_nonexistent_leap_day_fails(self):
        resp = itinerary("Amsterdam", 3)
        resp.parsed_data["dates"] = "2027-02-29 to 2027-03-02"
        resp.raw_response = json.dumps(resp.parsed_data)
        result = evaluate("A-07", resp)
        self.assertEqual(statuses(result)["Nonexistent Date Not Used"], "FAIL")

    def test_silently_substituted_date_still_fails_the_flag_check(self):
        result = evaluate("A-07", itinerary("Amsterdam", 3))
        self.assertEqual(statuses(result)["Nonexistent Date Not Used"], "PASS")
        self.assertEqual(statuses(result)["Invalid Request Flagged"], "FAIL")

    def test_duration_conflict_and_infeasible_trip_use_the_flag_check(self):
        self.assertEqual(statuses(evaluate("A-06", itinerary("Lisbon", 5)))["Invalid Request Flagged"], "FAIL")
        self.assertEqual(statuses(evaluate("A-14", itinerary("Kyoto", 1)))["Invalid Request Flagged"], "FAIL")
        asked = text("Spending 4 hours in each of three cities plus travel time isn't possible in one day. Which constraint should I relax?")
        self.assertEqual(statuses(evaluate("A-14", asked))["Invalid Request Flagged"], "PASS")

    def test_invented_trip_for_irrelevant_input_names_the_defaults(self):
        result = evaluate("A-15", itinerary("Barcelona, Spain", 3, travelers="Family of 4"))
        self.assertEqual(statuses(result)["Invalid Request Flagged"], "FAIL")
        self.assertIn("Barcelona, Spain", evidence(result, "Invalid Request Flagged"))
        self.assertIn("Family of 4", evidence(result, "Invalid Request Flagged"))

    def test_asking_for_trip_details_passes(self):
        result = evaluate("A-15", text("I plan trips! Where would you like to go, when, and who is travelling?"))
        self.assertEqual(result.overall_status, "PASS")


# ----------------------------------------------------
# Anupma: structure, defaults and constraints
# ----------------------------------------------------

class TestItineraryStructure(OfflineTestCase):

    def test_well_formed_kyoto_itinerary_passes(self):
        result = evaluate("A-08", itinerary("Kyoto, Japan", 4))
        self.assertEqual(result.overall_status, "PASS", result.checks)

    def test_wrong_day_count_fails(self):
        result = evaluate("A-08", itinerary("Kyoto", 5))
        self.assertEqual(statuses(result)["Day Count Matches Request"], "FAIL")
        self.assertIn("4", evidence(result, "Day Count Matches Request"))

    def test_skipped_or_repeated_day_numbers_fail(self):
        resp = itinerary("Kyoto", 4)
        resp.parsed_data["days"][2]["day"] = 2
        result = evaluate("A-08", resp)
        self.assertEqual(statuses(result)["Day Count Matches Request"], "FAIL")

    def test_wrong_destination_fails(self):
        result = evaluate("A-08", itinerary("Osaka", 4))
        self.assertEqual(statuses(result)["Destination Matches Request"], "FAIL")

    def test_added_children_are_an_unexpected_default(self):
        result = evaluate("A-08", itinerary("Kyoto", 4, travelers="Family of 4 (2 adults, 2 children)"))
        self.assertEqual(statuses(result)["Travellers Match Request"], "FAIL")

    def test_traveller_wording_variants_are_accepted(self):
        for wording in ("Two adults", "2 adults", "Couple", "2 Adults (couple)"):
            with self.subTest(wording=wording):
                result = evaluate("A-08", itinerary("Kyoto", 4, travelers=wording))
                self.assertEqual(statuses(result)["Travellers Match Request"], "PASS")

    def test_unclear_traveller_wording_needs_review_not_fail(self):
        result = evaluate("A-08", itinerary("Kyoto", 4, travelers="Small travel party"))
        self.assertEqual(statuses(result)["Travellers Match Request"], "NEEDS REVIEW")

    def test_missing_required_field_fails(self):
        resp = itinerary("Kyoto", 4)
        del resp.parsed_data["travelers"]
        result = evaluate("A-08", resp)
        self.assertEqual(statuses(result)["Required Itinerary Fields"], "FAIL")
        self.assertIn("travelers", evidence(result, "Required Itinerary Fields"))

    def test_no_itinerary_for_a_complete_request_fails(self):
        result = evaluate("A-08", text("Kyoto is wonderful in autumn."))
        self.assertEqual(statuses(result)["Required Itinerary Fields"], "FAIL")


class TestAnupmaConstraints(OfflineTestCase):

    def test_museum_visit_violates_exclusion(self):
        acts = [["Vatican Museums and Sistine Chapel"], ["Trastevere food walk"], ["Appian Way bike ride"]]
        result = evaluate("A-09", itinerary("Rome", 3, activities=acts))
        self.assertEqual(statuses(result)["Excluded Activity Absent (museums)"], "FAIL")
        self.assertIn("Vatican Museums", evidence(result, "Excluded Activity Absent (museums)"))

    def test_negated_museum_mention_needs_review(self):
        acts = [["Skip the museums and walk the Appian Way"], ["Trastevere food walk"], ["Villa Ada park"]]
        result = evaluate("A-09", itinerary("Rome", 3, activities=acts))
        self.assertEqual(statuses(result)["Excluded Activity Absent (museums)"], "NEEDS REVIEW")

    def test_itinerary_without_museums_passes(self):
        acts = [["Colosseum exterior walk"], ["Trastevere food walk"], ["Appian Way bike ride"]]
        result = evaluate("A-09", itinerary("Rome", 3, activities=acts))
        self.assertEqual(result.overall_status, "PASS", result.checks)

    def test_meat_suggestion_violates_vegetarian_diet(self):
        acts = [["Lunch: jamón ibérico tapas at La Boqueria"], ["Sagrada Família"]]
        result = evaluate("A-10", itinerary("Barcelona", 2, diet="Vegetarian", activities=acts))
        self.assertEqual(statuses(result)["No Meat or Fish Suggested"], "FAIL")

    def test_meat_word_beside_vegetarian_marker_needs_review(self):
        acts = [["Dinner: vegetarian paella instead of the seafood version"], ["Park Güell"]]
        result = evaluate("A-10", itinerary("Barcelona", 2, diet="Vegetarian", activities=acts))
        self.assertEqual(statuses(result)["No Meat or Fish Suggested"], "NEEDS REVIEW")

    def test_diet_field_defaulting_to_no_restrictions_fails(self):
        result = evaluate("A-10", itinerary("Barcelona", 2, diet="No restrictions"))
        self.assertEqual(statuses(result)["Vegetarian Requirement Recorded"], "FAIL")

    def test_vegetarian_itinerary_passes_deterministic_checks(self):
        result = evaluate("A-10", itinerary("Barcelona", 2, diet="Strictly vegetarian"))
        self.assertEqual(statuses(result)["No Meat or Fish Suggested"], "PASS")
        self.assertEqual(statuses(result)["Vegetarian Requirement Recorded"], "PASS")

    def test_wheelchair_trip_records_one_traveller(self):
        ok = evaluate("A-11", itinerary("Lisbon", 2, travelers="1 adult (wheelchair user)"))
        self.assertEqual(statuses(ok)["Travellers Match Request"], "PASS")
        bad = evaluate("A-11", itinerary("Lisbon", 2, travelers="2 adults"))
        self.assertEqual(statuses(bad)["Travellers Match Request"], "FAIL")

    def test_accessibility_barrier_words_need_review_and_semantic_check_is_queued(self):
        acts = [["Climb the stairs to São Jorge Castle"], ["Oceanário"]]
        result = evaluate("A-11", itinerary("Lisbon", 2, travelers="1 adult", activities=acts))
        self.assertEqual(statuses(result)["Accessibility Barriers Mentioned"], "NEEDS REVIEW")
        self.assertEqual(statuses(result)["Step-free, wheelchair-accessible activities (semantic)"], "NEEDS REVIEW")

    def test_toddler_must_appear_in_travellers(self):
        bad = evaluate("A-12", itinerary("London", 2, travelers="2 adults"))
        self.assertEqual(statuses(bad)["Travellers Match Request"], "FAIL")
        ok = evaluate("A-12", itinerary("London", 2, travelers="2 adults and a 2-year-old toddler"))
        self.assertEqual(statuses(ok)["Travellers Match Request"], "PASS")

    def test_adult_only_venue_fails_for_toddler_trip(self):
        acts = [["Evening pub crawl in Soho"], ["Natural History Museum"]]
        result = evaluate("A-12", itinerary("London", 2, travelers="2 adults, 1 toddler", activities=acts))
        self.assertEqual(statuses(result)["No Adult-Only Venues"], "FAIL")

    def test_luxury_stay_breaks_the_budget(self):
        result = evaluate("A-13", itinerary("Paris", 3, comfort="Five-star hotel near the Louvre"))
        self.assertEqual(statuses(result)["No Luxury Options Within Budget"], "FAIL")

    def test_flagging_an_unrealistic_budget_passes(self):
        result = evaluate("A-13", text("300 EUR in total isn't enough for 3 nights in Paris for 2 people. Could you increase the budget?"))
        self.assertEqual(statuses(result)["Unrealistic Budget Flagged"], "PASS")

    def test_budget_itinerary_queues_semantic_check(self):
        result = evaluate("A-13", itinerary("Paris", 3, comfort="Hostel dorm"))
        self.assertEqual(statuses(result)["Plan credibly fits 300 EUR (semantic)"], "NEEDS REVIEW")


# ----------------------------------------------------
# Prateek: grounded answers from the five fictional documents
# ----------------------------------------------------

class TestPrateekChecks(OfflineTestCase):

    def test_retrieval_miss_is_one_failure_not_several(self):
        result = evaluate("P-05", text(NO_ANSWER))
        failures = [name for name, status in statuses(result).items() if status == "FAIL"]
        self.assertEqual(failures, ["Correct Document Retrieved (Project Elm)"])
        self.assertNotIn("Sunlight Duration (2.5 hours)", statuses(result))

    def test_paraphrased_elm_answer_passes(self):
        resp = answer("Project Elm requires at least 2.5 hours of potential direct sunlight on 21 March.", ELM_SOURCE)
        self.assertEqual(evaluate("P-05", resp).overall_status, "PASS")

    def test_numbers_must_match_exactly(self):
        resp = answer("Project Elm requires at least 25 hours of sunlight on 21 March.", ELM_SOURCE)
        self.assertEqual(statuses(evaluate("P-05", resp))["Sunlight Duration (2.5 hours)"], "FAIL")

    def test_verification_boilerplate_is_not_an_abstention(self):
        resp = answer("Project Elm requires 2.5 hours of direct sun on 21 March.", ELM_SOURCE)
        self.assertNotIn("FAIL", statuses(evaluate("P-05", resp)).values())

    def test_fossil_energy_value(self):
        ok = answer("Project Maple sets BENG 2 at a maximum of 35 kWh/m2 per year.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-06", ok))["BENG 2 Value (35 kWh/m2/yr)"], "PASS")
        wrong = answer("Project Maple allows a maximum of 60 kWh/m2 per year.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-06", wrong))["BENG 2 Value (35 kWh/m2/yr)"], "FAIL")
        near = answer("Project Maple allows 350 kWh/m2 per year.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-06", near))["BENG 2 Value (35 kWh/m2/yr)"], "FAIL")

    def test_absent_noise_fact(self):
        self.assertEqual(statuses(evaluate("P-07", text(NO_ANSWER)))["Absent Fact Acknowledged"], "PASS")
        invented = answer("The Project Cedar meeting room must stay below 35 dB(A).", NOISE_SOURCE)
        self.assertEqual(statuses(evaluate("P-07", invented))["Absent Fact Acknowledged"], "FAIL")
        vague = answer("Combined noise is used to determine the indoor noise level.", NOISE_SOURCE)
        self.assertEqual(statuses(evaluate("P-07", vague))["Absent Fact Acknowledged"], "NEEDS REVIEW")

    def test_absent_fire_rating(self):
        self.assertEqual(statuses(evaluate("P-08", text(NO_ANSWER)))["Absent Fact Acknowledged"], "PASS")
        invented = answer("Project Birch requires REI 60 fire resistance.", BIRCH_SOURCE)
        self.assertEqual(statuses(evaluate("P-08", invented))["Absent Fact Acknowledged"], "FAIL")

    def test_false_premise(self):
        corrected = answer("Project Maple requires at least 45 percent renewable energy, not 60 percent.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-09", corrected))["False Premise Corrected (45%, not 60%)"], "NEEDS REVIEW")
        only_45 = answer("BENG 3 sets a minimum renewable share of 45 percent.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-09", only_45))["False Premise Corrected (45%, not 60%)"], "PASS")
        accepted = answer("Project Maple requires 60 percent renewable energy to cut fossil use.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-09", accepted))["False Premise Corrected (45%, not 60%)"], "FAIL")
        self.assertEqual(statuses(evaluate("P-09", text(NO_ANSWER)))["False Premise Corrected (45%, not 60%)"], "FAIL")

    def test_derived_daylight_area(self):
        for unit in ("3 m²", "3 m2", "3.0 m2"):
            with self.subTest(unit=unit):
                resp = answer(f"A 25 m2 living room in Project Birch needs at least {unit}.", BIRCH_SOURCE)
                self.assertEqual(statuses(evaluate("P-10", resp))["Derived Area (3 m²)"], "PASS")
        wrong = answer("A 25 m2 living room in Project Birch needs 30 m² of glazing.", BIRCH_SOURCE)
        self.assertEqual(statuses(evaluate("P-10", wrong))["Derived Area (3 m²)"], "FAIL")

    def test_noise_definitions(self):
        resp = answer("Cumulative noise applies a correction for annoyance; combined noise does not and sets the indoor noise level.", NOISE_SOURCE)
        result = statuses(evaluate("P-11", resp))
        self.assertEqual(result["Annoyance Correction Mentioned"], "PASS")
        self.assertEqual(result["Indoor Noise Level Use Mentioned"], "PASS")
        self.assertEqual(result["Correction attributed to cumulative, not combined (semantic)"], "NEEDS REVIEW")

    def test_section_citation(self):
        good = answer("Section 3.1 of the Project Elm sun hours policy.", ELM_SOURCE)
        self.assertEqual(evaluate("P-12", good).overall_status, "PASS")
        wrong = answer("Section 3.1 covers this.", NOISE_SOURCE)
        self.assertEqual(statuses(evaluate("P-12", wrong))["Noise Report Not Cited"], "FAIL")

    P13 = "Project Target Not Presented As National Regulation"

    def test_declining_to_give_national_limits_passes(self):
        self.assertEqual(statuses(evaluate("P-13", text(NO_ANSWER)))[self.P13], "PASS")

    def test_project_target_presented_as_national_limit_fails(self):
        presented = answer("Dutch BENG 1 for new homes is a maximum of 60 kWh/m2 per year.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-13", presented))[self.P13], "FAIL")

    def test_the_word_fictional_does_not_rescue_a_contradicted_claim(self):
        claim = answer("According to the fictional data, the Dutch BENG 1 limit for new homes is 60 kWh/m2 per year.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-13", claim))[self.P13], "FAIL")

    def test_maple_attributed_or_negated_figures_need_semantic_review(self):
        attributed = answer("The documents give no national limit; Project Maple sets its own BENG 1 target at 60 kWh/m2 per year.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-13", attributed))[self.P13], "NEEDS REVIEW")
        negated = answer("60 kWh/m2 per year is a project target and is not an actual Dutch regulation.", MAPLE_SOURCE)
        self.assertEqual(statuses(evaluate("P-13", negated))[self.P13], "NEEDS REVIEW")


# ----------------------------------------------------
# Routing guard and status semantics
# ----------------------------------------------------

class TestRealFormatVariants(OfflineTestCase):
    """Formats seen in stored responses: '**Source:**' headings and blank itinerary templates."""

    @staticmethod
    def empty_template(n_days=5):
        data = {
            "destination": "", "dates": "", "travelers": "", "pace": "", "transport": "", "comfort": "", "diet": "",
            "days": [{"day": i + 1, "base": "", "activities": []} for i in range(n_days)],
            "stays": [{"place": "", "name": "", "why": ""}], "food": [{"place": "", "name": "", "dishes": []}],
        }
        return AdapterResponse(success=True, status_code=200, latency_ms=900.0, raw_response=json.dumps(data), parsed_data=data)

    def test_source_heading_with_colon_is_not_part_of_the_answer(self):
        resp = text(
            "**Answer:**  \nDutch BENG 1 for new homes is a maximum of 60 kWh/m2 per year.\n\n"
            "**Source:**  \nFICTIONAL training rules: BENG for Project Maple – \"FICTIONAL EXERCISE ONLY\"\n\n" + VERIFICATION
        )
        self.assertEqual(statuses(evaluate("P-13", resp))["Project Target Not Presented As National Regulation"], "FAIL")

    def test_empty_template_is_named_as_such_when_details_are_missing(self):
        result = evaluate("A-15", self.empty_template())
        self.assertEqual(statuses(result)["Invalid Request Flagged"], "FAIL")
        self.assertIn("empty itinerary template", evidence(result, "Invalid Request Flagged"))

    def test_empty_template_fails_a_complete_request(self):
        result = evaluate("A-08", self.empty_template(4))
        self.assertEqual(statuses(result)["Required Itinerary Fields"], "FAIL")
        self.assertIn("empty itinerary template", evidence(result, "Required Itinerary Fields"))
        self.assertNotIn("Day Count Matches Request", statuses(result))


class TestRoutingAndSemantics(OfflineTestCase):

    def test_reused_id_with_different_input_falls_back_to_generic_checks(self):
        result = evaluate("A-05", itinerary("Oslo", 2), test_input="Plan a 2-day trip to Oslo for 2 adults.")
        self.assertNotIn("Invalid Request Flagged", statuses(result))
        self.assertIn("Behavioural Match Verification", statuses(result))

    def test_target_error_still_short_circuits(self):
        error = AdapterResponse(success=False, latency_ms=25000.0, error="Request timed out after 25s")
        result = evaluate("P-05", error)
        self.assertEqual(result.overall_status, "ERROR")
        self.assertEqual(len(result.checks), 1)

    def test_every_check_has_actionable_evidence(self):
        for case_id in CASE_INPUTS:
            resp = itinerary("Nowhere", 1) if case_id.startswith("A-") else text(NO_ANSWER)
            for check in evaluate(case_id, resp).checks:
                with self.subTest(case=case_id, check=check.check_name):
                    self.assertGreater(len(check.evidence_or_reason), 15)

    def test_no_new_case_can_produce_error_from_a_successful_response(self):
        for case_id in CASE_INPUTS:
            resp = itinerary("Nowhere", 1) if case_id.startswith("A-") else text(NO_ANSWER)
            with self.subTest(case=case_id):
                self.assertNotEqual(evaluate(case_id, resp).overall_status, "ERROR")


class TestSemanticChecksWithJudge(unittest.TestCase):

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test"})
    def test_judge_resolves_semantic_check(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        acts = [["Take the stairs up to São Jorge Castle"], ["Oceanário"]]
        verdict = ModelReply(ok=True, content=json.dumps({
            "verdict": "FAIL", "evidence_quote": "Take the stairs up to São Jorge Castle", "reason": "Stairs are not step-free.",
        }))
        with patch("judges.call_model", return_value=verdict):
            result = evaluate("A-11", itinerary("Lisbon", 2, travelers="1 adult", activities=acts))
        self.assertEqual(statuses(result)["Step-free, wheelchair-accessible activities (semantic)"], "FAIL")

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test"})
    def test_judge_rate_limit_is_not_an_app_failure(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch("judges.call_model", return_value=ModelReply(ok=False, error="HTTP 429: rate limited")):
            result = evaluate("A-11", itinerary("Lisbon", 2, travelers="1 adult"))
        semantic = next(c for c in result.checks if c.check_name.endswith("(semantic)"))
        self.assertEqual(semantic.status, "NEEDS REVIEW")
        self.assertIn("unavailable", semantic.evidence_or_reason)
        self.assertEqual(result.overall_status, "NEEDS REVIEW")


if __name__ == "__main__":
    unittest.main()
