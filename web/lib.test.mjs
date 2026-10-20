// Run: node --test web/
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  extractQuotes, findHighlights, segment, readableResponse, caseTitle, nextCaseId,
  runsByCase, verdictSentence, relativeTime, absoluteTime, parseViewState, serializeViewState, statusMeta,
  evaluationNote, checkDisplayStatus, runVerdict, filterCases, visibleChecks, isHiddenCheck, runNeedsToken,
} from "./lib.js";

test("extracts judge quotes, splitting on ellipses", () => {
  const ev = 'Judge openai/gpt-oss-120b [judge-v2]: FAIL. Invented a city. Quote: "itinerary for Barcelona ... Day 1"';
  assert.deepEqual(extractQuotes(ev), ["itinerary for Barcelona", "Day 1"]);
});

test("extracts single-quoted values without catching apostrophes", () => {
  const ev = "App generated an itinerary for 'Lisbon' despite blank input. The user's kids' menu";
  assert.deepEqual(extractQuotes(ev), ["Lisbon"]);
});

test("extracts keyword lists from deterministic evidence", () => {
  const ev = "Itinerary for 2 adults contains child-oriented keywords: kid, playground.";
  assert.deepEqual(extractQuotes(ev), ["kid", "playground"]);
});

test("returns nothing for evidence without quotes", () => {
  assert.deepEqual(extractQuotes("HTTP 200 received in 312ms"), []);
  assert.deepEqual(extractQuotes(""), []);
});

test("highlights ignore case and whitespace, and merge overlaps", () => {
  const text = "Here is your\n  itinerary for BARCELONA. Day 1: Sagrada";
  const ranges = findHighlights(text, ["itinerary for barcelona", "for Barcelona. Day"]);
  assert.equal(ranges.length, 1);
  assert.equal(text.slice(ranges[0].start, ranges[0].end), "itinerary for BARCELONA. Day");
});

test("needles with regex characters are matched literally", () => {
  const ranges = findHighlights("cost is (12%) total", ["(12%)"]);
  assert.deepEqual(ranges, [{ start: 8, end: 13 }]);
});

test("segment splits text around ranges", () => {
  assert.deepEqual(segment("abcdef", [{ start: 2, end: 4 }]), [
    { text: "ab", marked: false }, { text: "cd", marked: true }, { text: "ef", marked: false },
  ]);
});

test("readableResponse decodes JSON escapes and leaves text alone", () => {
  assert.equal(readableResponse('{"a":"caf\\u00e9"}'), '{\n  "a": "café"\n}');
  assert.equal(readableResponse("plain text"), "plain text");
  assert.equal(readableResponse("300"), "300");
  assert.equal(readableResponse(""), "");
});

test("caseTitle uses the first sentence or trims on a word boundary", () => {
  assert.equal(caseTitle("Plan a 3-day trip to Dubai for 2 adults. Maximum 1 activity per day."), "Plan a 3-day trip to Dubai for 2 adults");
  assert.equal(caseTitle("one two three four five", 10), "one two…");
  assert.equal(caseTitle("Short."), "Short.");
});

test("nextCaseId continues the target's numbering", () => {
  const pairs = [{ pair_id: "A-01" }, { pair_id: "A-04" }, { pair_id: "P-02" }, { pair_id: "TEST-01" }];
  assert.equal(nextCaseId(pairs, "Anupma"), "A-05");
  assert.equal(nextCaseId(pairs, "Prateek"), "P-03");
  assert.equal(nextCaseId([], "Prateek"), "P-01");
});

test("runsByCase keeps newest-first order per case", () => {
  const grouped = runsByCase([
    { pair_id: "A-01", result_id: 3 }, { pair_id: "P-01", result_id: 2 }, { pair_id: "A-01", result_id: 1 },
  ]);
  assert.deepEqual(grouped.get("A-01").map((r) => r.result_id), [3, 1]);
});

test("verdictSentence explains each status", () => {
  const checks = [{ status: "PASS" }, { status: "FAIL" }, { status: "NEEDS REVIEW" }];
  assert.equal(verdictSentence("FAIL", checks), "1 of 3 checks failed.");
  assert.equal(verdictSentence("PASS", [{ status: "PASS" }]), "All 1 check passed.");
  assert.match(verdictSentence("NEEDS REVIEW", checks), /^1 check couldn't/);
  assert.match(verdictSentence("ERROR", []), /didn't return a usable response/);
});

test("relativeTime reads timestamps with and without a zone", () => {
  const now = new Date("2026-10-20T12:00:00Z");
  assert.equal(relativeTime("2026-10-20T11:00:00+00:00", now), "1 hour ago");
  assert.equal(relativeTime("2026-10-20 11:58:00", now), "2 minutes ago");
  assert.equal(relativeTime("2026-10-20T11:59:50Z", now), "just now");
  assert.equal(relativeTime("garbage", now), "");
});

test("absoluteTime formats a full date, or nothing for bad input", () => {
  assert.match(absoluteTime("2026-10-20T11:00:00Z"), /2026/);
  assert.equal(absoluteTime("garbage"), "");
});

test("view state round-trips through the URL", () => {
  const state = { caseId: "A-02", resultId: 17, checkIndex: 1, compare: [12, 17] };
  assert.deepEqual(parseViewState(serializeViewState(state)), state);
  assert.equal(serializeViewState(state), "?case=A-02&run=17&check=1&compare=12,17");
  assert.deepEqual(parseViewState("?case=A-01&run=abc&compare=1"), {
    caseId: "A-01", resultId: null, checkIndex: null, compare: null,
  });
});

test("evaluation note appears only for Prateek's cases", () => {
  assert.match(evaluationNote("Prateek"), /^\* Evaluation note: Prateek's application uses fictional data\./);
  assert.match(evaluationNote(" prateek "), /observable\nAPI behaviour|observable API behaviour/);
  assert.equal(evaluationNote("Anupma"), null);
  assert.equal(evaluationNote(""), null);
  assert.equal(evaluationNote(undefined), null);
});

test("an unavailable optional check displays as Not evaluated, never as a verdict", () => {
  assert.equal(checkDisplayStatus({ status: "NEEDS REVIEW", unavailable: true }), "NOT EVALUATED");
  assert.equal(checkDisplayStatus({ status: "NEEDS REVIEW", unavailable: false }), "NEEDS REVIEW");
  assert.equal(checkDisplayStatus({ status: "PASS" }), "PASS");
  assert.deepEqual(statusMeta("NOT EVALUATED"), { key: "unevaluated", label: "Not evaluated" });
});

test("verdict sentences count only checks that produced a verdict", () => {
  const checks = [{ status: "PASS" }, { status: "PASS" }, { status: "NOT EVALUATED" }];
  assert.equal(verdictSentence("PASS", checks), "All 2 checks passed.");
});

test("runVerdict prefers the display verdict and falls back to the stored status", () => {
  assert.equal(runVerdict({ overall_status: "NEEDS REVIEW", verdict: "PASS" }), "PASS");
  assert.equal(runVerdict({ overall_status: "FAIL" }), "FAIL");
});

const PAIRS = [
  { pair_id: "A-01", target_app: "Anupma", input: "Dubai trip", expected_behaviour: "" },
  { pair_id: "A-02", target_app: "Anupma", input: "", expected_behaviour: "Ask for a destination" },
  { pair_id: "A-03", target_app: "Anupma", input: "London 2020", expected_behaviour: "" },
  { pair_id: "P-01", target_app: "Prateek", input: "Cedar airflow", expected_behaviour: "" },
  { pair_id: "P-02", target_app: "Prateek", input: "Maple vs Cedar", expected_behaviour: "" },
  { pair_id: "P-09", target_app: "Prateek", input: "Never run", expected_behaviour: "" },
];
// Newest first, as the API returns them. A-01's latest run is ERROR even though an older one passed.
const RESULTS = [
  { result_id: 9, pair_id: "A-01", overall_status: "ERROR", verdict: "ERROR" },
  { result_id: 8, pair_id: "P-02", overall_status: "NEEDS REVIEW", verdict: "NEEDS REVIEW" },
  { result_id: 7, pair_id: "P-01", overall_status: "NEEDS REVIEW", verdict: "PASS" },
  { result_id: 6, pair_id: "A-03", overall_status: "FAIL", verdict: "FAIL" },
  { result_id: 5, pair_id: "A-02", overall_status: "FAIL", verdict: "FAIL" },
  { result_id: 4, pair_id: "A-01", overall_status: "PASS", verdict: "PASS" },
];
const ids = (filters) => filterCases(PAIRS, RESULTS, filters).map((p) => p.pair_id);

test("each status filter matches cases by their latest run's verdict", () => {
  assert.deepEqual(ids({ status: "PASS" }), ["P-01"]);
  assert.deepEqual(ids({ status: "FAIL" }), ["A-02", "A-03"]);
  assert.deepEqual(ids({ status: "ERROR" }), ["A-01"]);
  assert.deepEqual(ids({ status: "NEEDS REVIEW" }), ["P-02"]);
});

test("status filters combine with the target filter and search", () => {
  assert.deepEqual(ids({ target: "Anupma", status: "FAIL" }), ["A-02", "A-03"]);
  assert.deepEqual(ids({ target: "Prateek", status: "FAIL" }), []);
  assert.deepEqual(ids({ target: "Prateek", status: "PASS" }), ["P-01"]);
  assert.deepEqual(ids({ target: "All", status: "FAIL", query: "london" }), ["A-03"]);
});

test("clearing the status filter restores every case, including never-run ones", () => {
  assert.deepEqual(ids({ status: null }), ["A-01", "A-02", "A-03", "P-01", "P-02", "P-09"]);
  assert.deepEqual(ids({}), ["A-01", "A-02", "A-03", "P-01", "P-02", "P-09"]);
  assert.ok(!ids({ status: "PASS" }).includes("P-09"), "never-run cases have no latest verdict");
});

test("statusMeta falls back for unknown statuses", () => {
  assert.equal(statusMeta("NEEDS REVIEW").key, "review");
  assert.equal(statusMeta("NOT RUN").label, "NOT RUN");
});

test("Content Safety is hidden unless it failed; other checks are untouched", () => {
  const checks = [
    { check_name: "HTTP & Network Reachability", status: "PASS" },
    { check_name: "Content Safety", status: "NEEDS REVIEW", unavailable: true },
    { check_name: "Content Safety", status: "PASS" },
    { check_name: "Source Citation", status: "FAIL" },
  ];
  assert.deepEqual(visibleChecks(checks).map((c) => c.check_name), ["HTTP & Network Reachability", "Source Citation"]);
  const unsafe = { check_name: "Content Safety", status: "FAIL" };
  assert.deepEqual(visibleChecks([unsafe]), [unsafe], "an unsafe verdict decides the run, so it stays visible");
  assert.equal(isHiddenCheck("Content Safety", "PASS", "NEEDS REVIEW"), true);
  assert.equal(isHiddenCheck("Content Safety", "PASS", "FAIL"), false);
  assert.equal(isHiddenCheck("Source Citation", "PASS"), false);
});

test("runs of open targets never ask for a token", () => {
  const open = ["Prateek"];
  assert.equal(runNeedsToken({ target_app: "Prateek" }, true, open), false);
  assert.equal(runNeedsToken({ target_app: "Anupma" }, true, open), true);
  assert.equal(runNeedsToken({ target_app: "Anupma" }, false, open), false);
  assert.equal(runNeedsToken({ target_app: "Prateek" }, true, []), true);
});

test("golden pair upload is validated before anything is sent", async () => {
  const { parseGoldenPairs } = await import("./lib.js");
  assert.deepEqual(parseGoldenPairs('[{"input": " a ", "expected_behaviour": "b"}]'), { cases: [{ input: "a", expected_behaviour: "b" }], errors: [] });
  assert.equal(parseGoldenPairs('{"cases": [{"input": "a", "expected_behaviour": "b"}]}').cases.length, 1);
  assert.deepEqual(parseGoldenPairs("not json").errors, ["This isn't valid JSON."]);
  assert.deepEqual(parseGoldenPairs("[]").errors, ["Expected a non-empty list of golden pairs."]);
  assert.match(parseGoldenPairs('[{"input": "a"}]').errors[0], /Pair 1 needs/);
  assert.match(parseGoldenPairs(JSON.stringify(Array(51).fill({ input: "a", expected_behaviour: "b" }))).errors[0], /At most 50/);
});
