// Pure helpers for the MANTIS front end. No DOM access, so they run under `node --test`.

export const STATUSES = ["PASS", "FAIL", "ERROR", "NEEDS REVIEW"];

const STATUS_META = {
  PASS: { key: "pass", label: "Pass" },
  FAIL: { key: "fail", label: "Fail" },
  ERROR: { key: "error", label: "Error" },
  "NEEDS REVIEW": { key: "review", label: "Needs review" },
  // Display-only: an optional check whose provider couldn't run. Not a verdict.
  "NOT EVALUATED": { key: "unevaluated", label: "Not evaluated" },
};

export function checkDisplayStatus(check) {
  return check.unavailable ? "NOT EVALUATED" : check.status;
}

/** The display verdict from the API, or the stored status for older API responses. */
export function runVerdict(run) {
  return run.verdict || run.overall_status;
}

export function statusMeta(status) {
  return STATUS_META[status] || { key: "unknown", label: status || "Not run" };
}

const MIN_QUOTE_CHARS = 3;
const DOUBLE_QUOTED = /["“]([^"”]+)["”]/g;
const SINGLE_QUOTED = /(?:^|[\s(])'([^']+?)'(?=[\s.,;:)!?]|$)/g;
const KEYWORD_LIST = /keywords?:\s*([^.]+)/gi;
const ELLIPSIS = /\.\.\.|…/;

/** Pulls the text a check points at out of its evidence string, so it can be found in the response. */
export function extractQuotes(evidence) {
  if (!evidence) return [];
  const found = [];
  for (const m of evidence.matchAll(DOUBLE_QUOTED)) found.push(...m[1].split(ELLIPSIS));
  for (const m of evidence.matchAll(SINGLE_QUOTED)) found.push(m[1]);
  for (const m of evidence.matchAll(KEYWORD_LIST)) found.push(...m[1].split(","));
  const cleaned = found.map((q) => q.trim()).filter((q) => q.length >= MIN_QUOTE_CHARS);
  return [...new Set(cleaned)];
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Finds each needle in the text, ignoring case and whitespace differences. Returns merged, sorted ranges. */
export function findHighlights(text, needles) {
  const ranges = [];
  for (const needle of needles) {
    const pattern = escapeRegExp(needle.trim()).replace(/\s+/g, "\\s+");
    if (!pattern) continue;
    for (const m of text.matchAll(new RegExp(pattern, "gi"))) {
      if (m[0].length) ranges.push({ start: m.index, end: m.index + m[0].length });
    }
  }
  ranges.sort((a, b) => a.start - b.start || b.end - a.end);
  const merged = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r.start <= last.end) last.end = Math.max(last.end, r.end);
    else merged.push({ ...r });
  }
  return merged;
}

/** Splits text into plain and highlighted segments for rendering with textContent. */
export function segment(text, ranges) {
  const parts = [];
  let cursor = 0;
  for (const { start, end } of ranges) {
    if (start > cursor) parts.push({ text: text.slice(cursor, start), marked: false });
    parts.push({ text: text.slice(start, end), marked: true });
    cursor = end;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), marked: false });
  return parts;
}

/** Pretty-prints JSON responses and decodes \u escapes, matching what the LLM judge reads. */
export function readableResponse(raw) {
  if (!raw) return "";
  try {
    const value = JSON.parse(raw);
    if (value !== null && typeof value === "object") return JSON.stringify(value, null, 2);
  } catch {
    // Not JSON: show as-is.
  }
  return raw;
}

const PRATEEK_NOTE =
  "* Evaluation note: Prateek's application uses fictional data. Retrieval accuracy can only be independently " +
  "verified when trusted reference information is available. Other checks evaluate observable API behaviour and " +
  "response constraints.";

/** A context note shown with a case, or null. Only Prateek's cases have one. */
export function evaluationNote(targetApp) {
  return (targetApp || "").trim().toLowerCase() === "prateek" ? PRATEEK_NOTE : null;
}

export function caseTitle(input, max = 64) {
  const text = (input || "").trim().replace(/\s+/g, " ");
  if (!text) return "Empty input";
  const firstSentence = text.split(/(?<=[.?!])\s/)[0];
  if (firstSentence.length > max) return `${text.slice(0, max).replace(/\s+\S*$/, "")}…`;
  return firstSentence.length < text.length ? firstSentence.replace(/[.?!]$/, "") : firstSentence;
}

export function nextCaseId(pairs, target) {
  const prefix = (target || "X").trim().charAt(0).toUpperCase();
  const numbers = pairs
    .map((p) => p.pair_id.match(new RegExp(`^${prefix}-(\\d+)$`)))
    .filter(Boolean)
    .map((m) => Number(m[1]));
  const next = (numbers.length ? Math.max(...numbers) : 0) + 1;
  return `${prefix}-${String(next).padStart(2, "0")}`;
}

/** Groups result rows (newest first) by case. */
export function runsByCase(results) {
  const byCase = new Map();
  for (const r of results) {
    if (!byCase.has(r.pair_id)) byCase.set(r.pair_id, []);
    byCase.get(r.pair_id).push(r);
  }
  return byCase;
}

/** Cases matching the target app, search text and, if set, the verdict of their latest run. */
export function filterCases(pairs, results, { target = "All", query = "", status = null } = {}) {
  const needle = query.trim().toLowerCase();
  const latest = runsByCase(results);
  return pairs.filter((p) => {
    if (target !== "All" && p.target_app.toLowerCase() !== target.toLowerCase()) return false;
    if (needle && ![p.pair_id, p.target_app, p.input, p.expected_behaviour].some((v) => (v || "").toLowerCase().includes(needle))) {
      return false;
    }
    if (!status) return true;
    const newest = (latest.get(p.pair_id) || [])[0];
    return Boolean(newest) && runVerdict(newest) === status;
  });
}

export function countStatuses(checks) {
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0]));
  for (const c of checks) if (c.status in counts) counts[c.status] += 1;
  return counts;
}

/** One plain sentence explaining a run's verdict, derived from its checks. */
export function verdictSentence(status, checks) {
  const total = checks.filter((c) => STATUSES.includes(c.status)).length;
  const counts = countStatuses(checks);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  switch (status) {
    case "PASS":
      return `All ${plural(total, "check")} passed.`;
    case "FAIL":
      return `${counts.FAIL} of ${plural(total, "check")} failed.`;
    case "ERROR":
      return "The target app didn't return a usable response, so no behaviour was checked.";
    case "NEEDS REVIEW":
      return `${plural(counts["NEEDS REVIEW"], "check")} couldn't be decided automatically. Read the evidence and decide.`;
    default:
      return "";
  }
}

export function parseTimestamp(ts) {
  if (!ts) return null;
  const hasZone = /[zZ]|[+-]\d\d:?\d\d$/.test(ts);
  const date = new Date(hasZone ? ts : `${ts.replace(" ", "T")}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

// The interface copy is English, so dates are formatted in English to match it
const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "always" });
const ABSOLUTE = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });
const TIME_UNITS = [["minute", 60], ["hour", 3600], ["day", 86400], ["week", 604800]];

export function relativeTime(ts, now = new Date()) {
  const date = parseTimestamp(ts);
  if (!date) return "";
  const seconds = Math.round((now - date) / 1000);
  if (seconds < 45) return "just now";
  let unit = TIME_UNITS[0];
  for (const u of TIME_UNITS) if (seconds >= u[1]) unit = u;
  return RELATIVE.format(-Math.round(seconds / unit[1]), unit[0]);
}

export function absoluteTime(ts) {
  const date = parseTimestamp(ts);
  return date ? ABSOLUTE.format(date) : "";
}

/** Reads the view state from a URL query string. */
export function parseViewState(search) {
  const params = new URLSearchParams(search);
  const toInt = (v) => (v && /^\d+$/.test(v) ? Number(v) : null);
  const compare = (params.get("compare") || "").split(",").map(toInt);
  return {
    caseId: params.get("case"),
    resultId: toInt(params.get("run")),
    checkIndex: toInt(params.get("check")),
    compare: compare.length === 2 && compare.every((n) => n !== null) ? compare : null,
  };
}

export function serializeViewState({ caseId, resultId, checkIndex, compare }) {
  const params = new URLSearchParams();
  if (caseId) params.set("case", caseId);
  if (resultId != null) params.set("run", String(resultId));
  if (checkIndex != null) params.set("check", String(checkIndex));
  if (compare) params.set("compare", compare.join(","));
  // A literal comma keeps shared links readable; URLSearchParams decodes either form
  const query = params.toString().replace(/%2C/g, ",");
  return query ? `?${query}` : "";
}
