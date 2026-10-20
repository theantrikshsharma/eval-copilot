import {
  statusMeta, extractQuotes, findHighlights, segment, readableResponse, caseTitle, nextCaseId,
  runsByCase, verdictSentence, relativeTime, absoluteTime, parseTimestamp, parseViewState, serializeViewState,
  evaluationNote, checkDisplayStatus, runVerdict, filterCases, visibleChecks, isHiddenCheck, runNeedsToken,
  parseGoldenPairs,
} from "./lib.js";

const MAX_HISTORY_CHIPS = 40;
const MAX_INPUT_AS_TITLE = 140;

/** Non-breaking hyphens between digits, so dates like 2020-01-01 never split across lines. */
const keepDatesTogether = (text) => text.replace(/(\d)-(?=\d)/g, "$1‑");

// ----------------------------------------------------
// Icons (trusted constants; the only markup ever set with innerHTML)
// ----------------------------------------------------

const ICONS = {
  pass: '<path d="m4.5 8.4 2.3 2.3 4.7-5" />',
  fail: '<path d="m5.2 5.2 5.6 5.6M10.8 5.2l-5.6 5.6" />',
  error: '<path d="M8 4.6v4.2" /><path d="M8 11.3v.1" />',
  review: '<path d="M6.3 6.3a1.8 1.8 0 1 1 2.5 1.7c-.5.2-.8.6-.8 1.1v.3" /><path d="M8 11.3v.1" />',
  unknown: '<circle cx="8" cy="8" r="1" />',
  unevaluated: '<path d="M5 8h6" />',
  play: '<path d="M5.5 3.8v8.4L12 8z" />',
  compare: '<path d="M5 3 2.5 5.5 5 8M2.5 5.5h8M11 8l2.5 2.5L11 13M13.5 10.5h-8" />',
  copy: '<rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1.5" /><path d="M10.5 5.5V4a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5" />',
  close: '<path d="m4 4 8 8M12 4l-8 8" />',
  arrow: '<path d="M2.5 8h10M9 4.5 12.5 8 9 11.5" />',
  spinner: '<path d="M8 2.5a5.5 5.5 0 1 0 5.5 5.5" />',
};

function icon(name, className = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", className);
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

function glyph(status, size = "") {
  const meta = statusMeta(status);
  const el = h("span", { class: `glyph glyph-${meta.key} ${size}` });
  el.append(icon(meta.key));
  return el;
}

// ----------------------------------------------------
// DOM helper: every string child becomes a text node, so target output can never inject markup.
// ----------------------------------------------------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "on") for (const [evt, fn] of Object.entries(value)) el.addEventListener(evt, fn);
    else if (key === "style") el.setAttribute("style", value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);
const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ----------------------------------------------------
// API
// ----------------------------------------------------

class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

async function api(path, options = {}) {
  const { token, ...init } = options;
  const headers = {};
  if (init.body) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  let resp;
  try {
    resp = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError("Can't reach the MANTIS server. Check that it's running, then try again.");
  }
  if (!resp.ok) {
    let detail = `The server returned HTTP ${resp.status}.`;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Keep the generic message.
    }
    throw new ApiError(detail, resp.status);
  }
  return resp.json();
}

// The access token lives in sessionStorage: kept for this tab only, never in the page source or URL.
const TOKEN_KEY = "mantis.accessToken";

function readToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function saveToken(value) {
  try {
    if (value) sessionStorage.setItem(TOKEN_KEY, value);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage unavailable: the token is simply asked for again next time.
  }
}

function askForToken() {
  const tokenDialog = $("token-dialog");
  const input = $("token-input");
  input.value = "";
  tokenDialog.showModal();
  input.focus();
  return new Promise((resolve) => {
    tokenDialog.addEventListener("close", () => {
      const token = tokenDialog.returnValue === "save" ? input.value.trim() : "";
      if (token) saveToken(token);
      resolve(Boolean(token));
    }, { once: true });
  });
}

/** True when the user can proceed with a write: no token needed, one is stored, or they just entered one. */
async function ensureWriteAccess(needsToken = state.writesRequireToken) {
  return !needsToken || Boolean(readToken()) || askForToken();
}

async function apiWrite(path, payload) {
  try {
    return await api(path, { method: "POST", body: JSON.stringify(payload), token: readToken() });
  } catch (err) {
    if (err.status === 401) {
      saveToken("");
      err.message = "That access token was rejected. Try again with the correct token.";
    }
    throw err;
  }
}

// ----------------------------------------------------
// State
// ----------------------------------------------------

const TARGETS = [
  { value: "All", label: "All" },
  { value: "Anupma", label: "Anupma", description: "Travel itinerary generator" },
  { value: "Prateek", label: "Prateek", description: "Knowledge retrieval" },
];

const state = {
  pairs: [],
  results: [],
  summary: null,
  backend: "",
  filter: "All",
  statusFilter: null,
  query: "",
  caseId: null,
  resultId: null,
  checkIndex: null,
  compare: null,
  compareData: null,
  running: false,
  freshResultId: null,
};

const details = new Map();

async function loadDetail(resultId) {
  if (!details.has(resultId)) details.set(resultId, withVisibleChecks(await api(`/api/results/${resultId}`)));
  return details.get(resultId);
}

function withVisibleChecks(detail) {
  return { ...detail, checks: visibleChecks(detail.checks) };
}

function runsFor(caseId) {
  return runsByCase(state.results).get(caseId) || [];
}

function currentPair() {
  return state.pairs.find((p) => p.pair_id === state.caseId) || null;
}

function targetDescription(target) {
  return TARGETS.find((t) => t.value.toLowerCase() === (target || "").toLowerCase())?.description || "";
}

function defaultCheckIndex(detail) {
  const index = detail.checks.findIndex((c) => c.status !== "PASS" && !c.unavailable);
  return index === -1 ? null : index;
}

// ----------------------------------------------------
// URL state
// ----------------------------------------------------

function syncUrl(mode = "push") {
  const url = serializeViewState(state) || location.pathname;
  if (url === location.search) return;
  history[mode === "push" ? "pushState" : "replaceState"](null, "", url);
}

window.addEventListener("popstate", () => {
  const view = parseViewState(location.search);
  openCase(view.caseId, view, { history: "none" });
});

// ----------------------------------------------------
// Rail: filter, search, case list, tally
// ----------------------------------------------------

function renderFilter() {
  const group = $("target-filter");
  group.replaceChildren(
    ...TARGETS.map((t) =>
      h("button", {
        type: "button",
        role: "radio",
        "aria-checked": String(state.filter === t.value),
        tabindex: state.filter === t.value ? "0" : "-1",
        on: { click: () => setFilter(t.value) },
      }, t.label),
    ),
  );
}

function setFilter(value) {
  state.filter = value;
  renderFilter();
  renderCaseList();
  $("target-filter").querySelector('[aria-checked="true"]').focus();
}

$("target-filter").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
  const index = TARGETS.findIndex((t) => t.value === state.filter);
  const step = e.key === "ArrowRight" ? 1 : -1;
  setFilter(TARGETS[(index + step + TARGETS.length) % TARGETS.length].value);
  e.preventDefault();
});

function visiblePairs() {
  return filterCases(state.pairs, state.results, { target: state.filter, query: state.query, status: state.statusFilter });
}

function emptyListMessage() {
  if (!state.pairs.length) return "No test cases yet.";
  if (!state.statusFilter) return "No cases match. Clear the search or pick another app.";
  const app = state.filter === "All" ? "" : `${state.filter} `;
  return `No ${app}cases whose latest run is ${statusMeta(state.statusFilter).label}.`;
}

function renderCaseList() {
  const list = $("case-list");
  const pairs = visiblePairs();
  renderStatusBanner(pairs.length);
  if (!pairs.length) {
    list.replaceChildren(h("li", { class: "case-empty" },
      h("span", {}, emptyListMessage()),
      state.statusFilter
        ? h("button", { class: "btn btn-quiet btn-sm", type: "button", on: { click: () => setStatusFilter(null) } }, "Clear status filter")
        : null,
    ));
    return;
  }
  const grouped = runsByCase(state.results);
  list.replaceChildren(
    ...pairs.map((p) => {
      const runs = grouped.get(p.pair_id) || [];
      const latest = runs[0];
      const trace = runs.slice(0, 6).reverse();
      const statusText = latest ? `Latest: ${statusMeta(runVerdict(latest)).label}` : "Not run yet";
      return h("li", {},
        h("a", {
          class: "case-item",
          href: `?case=${encodeURIComponent(p.pair_id)}`,
          "aria-current": p.pair_id === state.caseId ? "page" : null,
          "aria-label": `${p.pair_id}, ${caseTitle(p.input)}. ${statusText}.`,
          on: {
            click: (e) => {
              if (e.metaKey || e.ctrlKey || e.shiftKey) return;
              e.preventDefault();
              openCase(p.pair_id);
              closeRail();
            },
          },
        },
          h("span", { class: "case-item-top" },
            h("span", { class: "case-item-id", translate: "no" }, p.pair_id),
            h("span", { class: "case-item-target" }, p.target_app),
            latest ? glyph(runVerdict(latest), "glyph-sm") : h("span", { class: "glyph glyph-none glyph-sm" }),
          ),
          h("span", { class: "case-item-title" }, keepDatesTogether(caseTitle(p.input))),
          trace.length > 1
            ? h("span", { class: "trace", "aria-hidden": "true" },
              trace.map((r) => h("span", { class: `trace-tick tick-${statusMeta(runVerdict(r)).key}` })))
            : null,
        ),
      );
    }),
  );
}

function setStatusFilter(status) {
  state.statusFilter = state.statusFilter === status ? null : status;
  renderTally();
  renderCaseList();
  const pressed = document.querySelector(`.tally-btn[data-status="${status}"]`);
  (pressed || $("case-search")).focus();
}

function renderStatusBanner(matches) {
  const banner = $("status-filter-banner");
  if (!state.statusFilter) {
    banner.hidden = true;
    banner.replaceChildren();
    return;
  }
  const label = statusMeta(state.statusFilter).label;
  banner.hidden = false;
  banner.replaceChildren(
    glyph(state.statusFilter, "glyph-sm"),
    h("span", {}, `${matches} ${matches === 1 ? "case" : "cases"} whose latest run is ${label}`),
    h("button", {
      class: "icon-btn", type: "button", "aria-label": `Clear the ${label} filter`, title: "Clear filter",
      on: { click: () => setStatusFilter(null) },
    }, icon("close")),
  );
}

function renderTally() {
  const s = state.summary;
  if (!s) return;
  const rows = [
    ["PASS", s.pass_count], ["FAIL", s.fail_count], ["ERROR", s.error_count], ["NEEDS REVIEW", s.needs_review_count],
  ];
  const top = s.top_failing_checks || [];
  $("tally").replaceChildren(
    h("p", { class: "tally-total" }, h("strong", {}, String(s.total_executions)), s.total_executions === 1 ? " run saved" : " runs saved"),
    h("ul", { class: "tally-list", "aria-label": "Filter cases by the status of their latest run" },
      rows.map(([status, count]) => {
        const label = statusMeta(status).label;
        const active = state.statusFilter === status;
        return h("li", {},
          h("button", {
            type: "button",
            class: `tally-btn${count ? "" : " is-zero"}`,
            "data-status": status,
            "aria-pressed": String(active),
            "aria-label": `${label}: ${count} ${count === 1 ? "run" : "runs"}. ${active ? "Clear this filter" : `Show cases whose latest run is ${label}`}`,
            title: active ? "Clear this filter" : `Show cases whose latest run is ${label}`,
            on: { click: () => setStatusFilter(status) },
          }, glyph(status, "glyph-sm"), h("span", {}, label), h("strong", {}, String(count))),
        );
      }),
    ),
    s.reclassified_runs
      ? h("p", { class: "tally-note" },
        `${s.reclassified_runs} older ${s.reclassified_runs === 1 ? "run was" : "runs were"} recorded as Needs review only because an optional check couldn't run.`)
      : null,
    top.length
      ? h("details", { class: "tally-top" },
        h("summary", {}, "Checks that fail most"),
        h("ol", {}, top.map((t) => h("li", {}, h("span", {}, t.check_name), h("strong", {}, String(t.fail_count))))))
      : null,
    state.durable === false
      ? h("p", { class: "tally-store is-warning" }, "Read-only: this deployment has no durable database, so runs can't be saved.")
      : state.backend ? h("p", { class: "tally-store" }, `Saved to ${state.backend}`) : null,
  );
}

$("case-search").addEventListener("input", (e) => {
  state.query = e.target.value;
  renderCaseList();
});

// ----------------------------------------------------
// Opening a case, a run, a check
// ----------------------------------------------------

async function openCase(caseId, view = {}, { history: historyMode = "push" } = {}) {
  const pair = state.pairs.find((p) => p.pair_id === caseId) || state.pairs[0];
  if (!pair) {
    state.caseId = null;
    renderView();
    return;
  }
  state.caseId = pair.pair_id;
  const runs = runsFor(state.caseId);
  const owned = (id) => runs.some((r) => r.result_id === id);
  state.resultId = owned(view.resultId) ? view.resultId : runs[0]?.result_id ?? null;
  state.compare = view.compare && view.compare.every(owned) ? view.compare : null;
  state.compareData = null;
  state.checkIndex = null;

  renderCaseList();
  renderView({ loading: true });
  try {
    if (state.resultId != null) {
      const detail = await loadDetail(state.resultId);
      state.checkIndex = view.checkIndex != null && view.checkIndex < detail.checks.length ? view.checkIndex : defaultCheckIndex(detail);
    }
    if (state.compare) state.compareData = await loadCompare(state.compare);
  } catch (err) {
    toast(err.message, "error");
  }
  if (historyMode !== "none") syncUrl(historyMode);
  renderView();
}

async function selectRun(resultId) {
  state.resultId = resultId;
  state.compare = null;
  state.compareData = null;
  try {
    const detail = await loadDetail(resultId);
    state.checkIndex = defaultCheckIndex(detail);
  } catch (err) {
    toast(err.message, "error");
  }
  syncUrl();
  renderView();
  document.querySelector(`.history [data-result="${resultId}"]`)?.focus();
}

function selectCheck(index) {
  state.checkIndex = state.checkIndex === index ? null : index;
  syncUrl("replace");
  renderResultPanes();
  document.querySelector(`.check[data-index="${index}"]`)?.focus();
}

async function loadCompare([a, b]) {
  const data = await api(`/api/compare?result_id_a=${a}&result_id_b=${b}`);
  data.individual_checks = data.individual_checks.filter((c) => !isHiddenCheck(c.check_name, c.status_a, c.status_b));
  return data;
}

async function startCompare(a, b) {
  state.compare = [a, b];
  try {
    state.compareData = await loadCompare(state.compare);
  } catch (err) {
    state.compare = null;
    toast(err.message, "error");
  }
  syncUrl();
  renderView();
  document.querySelector(".compare h2")?.focus();
}

function stopCompare() {
  state.compare = null;
  state.compareData = null;
  syncUrl();
  renderView();
  document.querySelector(".compare-trigger")?.focus();
}

// ----------------------------------------------------
// Running a test
// ----------------------------------------------------

let runTimer = null;

async function runTest() {
  const pair = currentPair();
  if (!pair || state.running) return;
  if (!(await ensureWriteAccess(runNeedsToken(pair, state.writesRequireToken, state.openExecuteTargets)))) return;
  const label = (document.getElementById("run-label")?.value || "").trim();
  state.running = true;
  const started = performance.now();
  updateRunButton(0);
  $("result")?.classList.add("is-running");
  runTimer = setInterval(() => updateRunButton(Math.round((performance.now() - started) / 1000)), 1000);

  try {
    const detail = withVisibleChecks(await apiWrite("/api/execute", { pair_id: pair.pair_id, run_id: label || null }));
    details.set(detail.result_id, detail);
    [state.results, state.summary] = await Promise.all([api("/api/results"), api("/api/summary")]);
    state.resultId = detail.result_id;
    state.checkIndex = defaultCheckIndex(detail);
    state.compare = null;
    state.compareData = null;
    state.freshResultId = detail.result_id;
    syncUrl();
    const verdict = runVerdict(detail);
    announce(`Run finished. ${statusMeta(verdict).label}. ${verdictSentence(verdict, displayChecks(detail))}`);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    clearInterval(runTimer);
    state.running = false;
    renderCaseList();
    renderTally();
    renderView();
    state.freshResultId = null;
  }
}

function updateRunButton(seconds) {
  const btn = $("run-btn");
  if (!btn) return;
  btn.disabled = state.running;
  btn.setAttribute("aria-busy", String(state.running));
  btn.replaceChildren(
    state.running ? icon("spinner", "icon spin") : icon("play"),
    state.running ? `Running… ${seconds}s` : "Run test",
  );
}

document.addEventListener("keydown", (e) => {
  const typing = e.target.closest("input, textarea, select, [contenteditable]");
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !$("new-case-dialog").open) {
    e.preventDefault();
    runTest();
  } else if (e.key === "/" && !typing && !$("new-case-dialog").open) {
    e.preventDefault();
    openRail();
    $("case-search").focus();
  } else if (e.key === "Escape" && document.body.classList.contains("rail-open")) {
    closeRail();
  }
});

// ----------------------------------------------------
// Main view
// ----------------------------------------------------

function renderView({ loading = false } = {}) {
  const view = $("view");
  view.setAttribute("aria-busy", String(loading));
  const pair = currentPair();
  if (!pair) {
    view.replaceChildren(
      h("section", { class: "empty-state" },
        h("h1", {}, "No test cases yet"),
        h("p", {}, "A test case is an input you send to an app and what a good response should do. Add one to start testing."),
        h("button", { class: "btn btn-primary", type: "button", on: { click: openNewCase } }, icon("arrow"), "New test case"),
      ),
    );
    return;
  }

  const runs = runsFor(pair.pair_id);
  // A short input is the title itself, so it isn't repeated below it
  const input = (pair.input || "").trim();
  const inputIsTitle = input.length > 0 && input.length <= MAX_INPUT_AS_TITLE;
  const title = keepDatesTogether(inputIsTitle ? input : caseTitle(input, 90));
  view.replaceChildren(
    h("article", { class: "case", "aria-labelledby": "case-title" },
      h("header", { class: "case-head" },
        h("p", { class: "case-kicker" },
          h("span", { class: "case-id", translate: "no" }, pair.pair_id),
          h("span", {}, pair.target_app),
          targetDescription(pair.target_app) ? h("span", { class: "muted" }, targetDescription(pair.target_app)) : null,
        ),
        h("h1", { id: "case-title" }, title),
      ),
      h("dl", { class: "spec" },
        inputIsTitle ? null : h("div", {}, h("dt", {}, "Input sent"), h("dd", {}, pair.input || "(empty)")),
        h("div", {}, h("dt", {}, "Expected behaviour"), h("dd", {}, pair.expected_behaviour)),
      ),
      evaluationNote(pair.target_app) ? h("p", { class: "eval-note" }, evaluationNote(pair.target_app)) : null,
      h("div", { class: "runbar" },
        h("div", { class: "run-controls" },
          h("button", {
            class: "btn btn-primary run-btn",
            id: "run-btn",
            type: "button",
            title: "Run test (Ctrl or ⌘ + Enter)",
            on: { click: runTest },
          }),
          h("details", { class: "run-label" },
            h("summary", {}, "Label this run"),
            h("input", { id: "run-label", type: "text", placeholder: "e.g. after-prompt-fix", "aria-label": "Run label", autocomplete: "off", spellcheck: "false" }),
          ),
        ),
        runs.length ? renderHistory(runs) : null,
      ),
      h("section", { class: "result", id: "result", "aria-label": "Result" }),
    ),
  );
  updateRunButton(0);
  if (loading) {
    $("result").replaceChildren(h("div", { class: "skeleton", "aria-hidden": "true" }, h("span"), h("span"), h("span")));
    return;
  }
  renderResult(runs);
}

function renderHistory(runs) {
  const shown = runs.slice(0, MAX_HISTORY_CHIPS);
  const label = runs.length > shown.length
    ? `Latest ${shown.length} of ${runs.length} runs, newest first. Compare reaches older runs.`
    : `${runs.length} ${runs.length === 1 ? "run" : "runs"}, newest first`;
  return h("div", { class: "history" },
    h("p", { class: "history-label", id: "history-label" }, label),
    h("div", { class: "history-chips", role: "group", "aria-labelledby": "history-label" },
      shown.map((r) => {
        const meta = statusMeta(runVerdict(r));
        const selected = r.result_id === state.resultId || (state.compare || []).includes(r.result_id);
        return h("button", {
          type: "button",
          class: `chip chip-${meta.key}`,
          "data-result": String(r.result_id),
          "aria-pressed": String(selected),
          "aria-label": `Run ${r.result_id}, ${meta.label}, ${relativeTime(r.timestamp)}`,
          title: `Run #${r.result_id}: ${meta.label}, ${absoluteTime(r.timestamp)}`,
          on: { click: () => selectRun(r.result_id) },
        }, icon(meta.key));
      }),
    ),
  );
}

function renderResult(runs) {
  const result = $("result");
  if (state.resultId == null) {
    result.className = "result result-empty";
    result.replaceChildren(
      h("p", { class: "empty-line" }, "No verdict yet. Every verdict requires evidence, so run this case to collect some."),
    );
    return;
  }
  if (state.compare && state.compareData) {
    result.className = "result";
    result.replaceChildren(renderCompare(runs));
    return;
  }

  const detail = details.get(state.resultId);
  if (!detail) return;
  const verdict = runVerdict(detail);
  const meta = statusMeta(verdict);
  const index = runs.findIndex((r) => r.result_id === detail.result_id);
  const previous = runs[index + 1];
  const fresh = state.freshResultId === detail.result_id;

  result.className = `result result-${meta.key}${fresh ? " is-fresh" : ""}`;
  result.replaceChildren(
    h("div", { class: "verdict" },
      h("div", { class: "verdict-main" },
        h("p", { class: `verdict-word tone-${meta.key}` }, glyph(verdict, "glyph-lg"), h("span", {}, meta.label)),
        h("p", { class: "verdict-sentence" }, verdictSentence(verdict, displayChecks(detail))),
      ),
      h("div", { class: "verdict-side" },
        h("p", { class: "verdict-meta" },
          h("span", {}, `Run #${detail.result_id}`),
          h("span", {}, detail.run_id),
          h("time", {
            datetime: parseTimestamp(detail.timestamp)?.toISOString() || "",
            title: absoluteTime(detail.timestamp),
          }, relativeTime(detail.timestamp)),
        ),
        previous
          ? h("button", {
            class: "btn btn-quiet compare-trigger",
            type: "button",
            on: { click: () => startCompare(previous.result_id, detail.result_id) },
          }, icon("compare"), "Compare with previous run")
          : h("p", { class: "muted small" }, "First run of this case"),
      ),
    ),
    h("div", { class: "panes", id: "panes" }),
  );
  renderResultPanes();
}

function displayChecks(detail) {
  return detail.checks.map((c) => ({ ...c, status: checkDisplayStatus(c) }));
}

function renderResultPanes() {
  const panes = $("panes");
  const detail = details.get(state.resultId);
  if (!panes || !detail) return;
  const selected = state.checkIndex != null ? detail.checks[state.checkIndex] : null;

  panes.replaceChildren(
    h("section", { class: "checks", "aria-labelledby": "checks-title" },
      h("div", { class: "pane-head" },
        h("h2", { id: "checks-title" }, "Checks"),
        h("p", { class: "muted small" }, "Select a check to find its evidence in the response"),
      ),
      h("ol", { class: "check-list" },
        detail.checks.map((c, i) => {
          const shown = checkDisplayStatus(c);
          const meta = statusMeta(shown);
          return h("li", { style: `--i:${i}` },
            h("button", {
              type: "button",
              class: `check check-${meta.key}`,
              "data-index": String(i),
              "aria-pressed": String(i === state.checkIndex),
              on: { click: () => selectCheck(i) },
            },
              glyph(shown),
              h("span", { class: "check-body" },
                h("span", { class: "check-name" }, c.check_name),
                h("span", { class: `check-status tone-${meta.key}` }, meta.label),
                h("span", { class: "check-evidence" }, c.evidence_or_reason),
              ),
            ),
          );
        }),
      ),
    ),
    renderResponsePane(detail.actual_response, selected),
  );

  const firstMark = panes.querySelector(".response-body mark");
  if (firstMark) {
    const body = firstMark.closest(".response-body");
    body.scrollTo({ top: Math.max(0, firstMark.offsetTop - body.clientHeight / 3), behavior: prefersReducedMotion() ? "auto" : "smooth" });
  }
}

function renderResponsePane(raw, selectedCheck, { title = "Response", compact = false } = {}) {
  const text = readableResponse(raw);
  const quotes = selectedCheck ? extractQuotes(selectedCheck.evidence_or_reason) : [];
  const ranges = quotes.length ? findHighlights(text, quotes) : [];
  const tone = selectedCheck ? statusMeta(checkDisplayStatus(selectedCheck)).key : "";

  let hint = null;
  if (selectedCheck && text) {
    if (!quotes.length) hint = "This check doesn't point at specific text";
    else if (!ranges.length) hint = "The quoted text isn't in this response";
    else hint = `${ranges.length} ${ranges.length === 1 ? "passage" : "passages"} highlighted`;
  }

  const body = text
    ? h("pre", { class: `response-body${compact ? " compact" : ""}`, tabindex: "0", translate: "no", "aria-label": `${title} text` },
      segment(text, ranges).map((part) => (part.marked ? h("mark", { class: `mark-${tone}` }, part.text) : part.text)))
    : h("p", { class: "response-empty" }, "No response body. The target failed before answering, so there's nothing to inspect.");

  return h("section", { class: "response", "aria-label": title },
    h("div", { class: "pane-head" },
      h("h2", {}, title),
      hint ? h("p", { class: "muted small", "aria-live": "polite" }, hint) : null,
      text
        ? h("button", {
          class: "icon-btn",
          type: "button",
          "aria-label": `Copy ${title.toLowerCase()}`,
          title: "Copy",
          on: { click: () => copyText(text) },
        }, icon("copy"))
        : null,
    ),
    body,
  );
}

// ----------------------------------------------------
// Compare view
// ----------------------------------------------------

function renderCompare(runs) {
  const data = state.compareData;
  const [a, b] = [data.execution_a, data.execution_b];
  const summary = data.summary;
  const runOptions = (selectedId) =>
    runs.map((r) => h("option", { value: String(r.result_id), selected: r.result_id === selectedId },
      `Run #${r.result_id}, ${statusMeta(runVerdict(r)).label}, ${relativeTime(r.timestamp)}`));

  const groups = [
    ["Fixed", summary.failures_resolved, "pass"],
    ["Still failing", summary.failures_remaining, "fail"],
    ["Newly failing", summary.new_failures, "fail"],
  ].filter(([, names]) => names.length);

  return h("div", { class: "compare" },
    h("div", { class: "compare-head" },
      h("h2", { tabindex: "-1" }, "Comparing two runs"),
      h("button", { class: "btn btn-quiet", type: "button", on: { click: stopCompare } }, icon("close"), "Done comparing"),
    ),
    h("div", { class: "compare-picks" },
      h("label", {}, h("span", {}, "Before"),
        h("select", { on: { change: (e) => startCompare(Number(e.target.value), b.result_id) } }, runOptions(a.result_id))),
      h("span", { class: "compare-arrow", "aria-hidden": "true" }, icon("arrow")),
      h("label", {}, h("span", {}, "After"),
        h("select", { on: { change: (e) => startCompare(a.result_id, Number(e.target.value)) } }, runOptions(b.result_id))),
    ),
    h("p", { class: "transition" },
      h("span", { class: `pill pill-${statusMeta(runVerdict(a)).key}` }, glyph(runVerdict(a), "glyph-sm"), statusMeta(runVerdict(a)).label),
      h("span", { class: "transition-arrow" }, icon("arrow"), h("span", { class: "sr-only" }, "became")),
      h("span", { class: `pill pill-${statusMeta(runVerdict(b)).key}` }, glyph(runVerdict(b), "glyph-sm"), statusMeta(runVerdict(b)).label),
    ),
    groups.length
      ? h("div", { class: "delta" },
        groups.map(([label, names, tone]) =>
          h("div", { class: `delta-group tone-border-${tone}` },
            h("h3", {}, label, h("span", { class: "count" }, String(names.length))),
            h("ul", {}, names.map((n) => h("li", {}, n))),
          )))
      : h("p", { class: "muted" }, "No check changed between pass and fail across these runs."),
    h("div", { class: "table-wrap" },
      h("table", { class: "compare-table" },
        h("caption", { class: "sr-only" }, `Check results in run ${a.result_id} and run ${b.result_id}`),
        h("thead", {}, h("tr", {},
          h("th", { scope: "col" }, "Check"),
          h("th", { scope: "col" }, `Before, run #${a.result_id}`),
          h("th", { scope: "col" }, `After, run #${b.result_id}`),
        )),
        h("tbody", {},
          data.individual_checks.map((c) =>
            h("tr", { class: c.status_a !== c.status_b ? "is-changed" : "" },
              h("th", { scope: "row" }, c.check_name, c.status_a !== c.status_b ? h("span", { class: "changed-tag" }, "Changed") : null),
              compareCell(c.unavailable_a ? "NOT EVALUATED" : c.status_a, c.evidence_a),
              compareCell(c.unavailable_b ? "NOT EVALUATED" : c.status_b, c.evidence_b),
            ))),
      ),
    ),
    h("details", { class: "compare-responses" },
      h("summary", {}, "Show both responses"),
      h("div", { class: "compare-response-grid" },
        renderResponsePane(a.actual_response, null, { title: `Before, run #${a.result_id}`, compact: true }),
        renderResponsePane(b.actual_response, null, { title: `After, run #${b.result_id}`, compact: true }),
      ),
    ),
  );
}

function compareCell(status, evidence) {
  const meta = statusMeta(status);
  return h("td", {},
    h("span", { class: `cell-status tone-${meta.key}` }, status === "NOT RUN" ? "Not run" : [glyph(status, "glyph-sm"), meta.label]),
    evidence ? h("span", { class: "cell-evidence" }, evidence) : null,
  );
}

// ----------------------------------------------------
// New test case dialog
// ----------------------------------------------------

const dialog = $("new-case-dialog");
const form = $("new-case-form");
let idEdited = false;

function openNewCase() {
  form.reset();
  idEdited = false;
  const target = state.filter !== "All" ? state.filter : currentPair()?.target_app || "Anupma";
  form.querySelector(`input[name="target_app"][value="${target}"]`)?.click();
  form.pair_id.value = nextCaseId(state.pairs, target);
  updateIdNote();
  $("form-error").textContent = "";
  closeRail();
  dialog.showModal();
  form.input.focus();
}

function updateIdNote() {
  const id = form.pair_id.value.trim();
  const exists = state.pairs.some((p) => p.pair_id.toLowerCase() === id.toLowerCase());
  const note = $("field-id-note");
  note.textContent = exists ? `${id} already exists. Choose another ID.` : "";
  note.classList.toggle("is-warning", exists);
}

form.addEventListener("change", (e) => {
  if (e.target.name === "target_app" && !idEdited) {
    form.pair_id.value = nextCaseId(state.pairs, e.target.value);
    updateIdNote();
  }
});
form.pair_id.addEventListener("input", () => {
  idEdited = true;
  updateIdNote();
});
$("new-case-cancel").addEventListener("click", () => dialog.close());
$("new-case-btn").addEventListener("click", openNewCase);
// Clicking outside closes the dialog only when nothing has been typed, so a stray click can't discard work
dialog.addEventListener("click", (e) => {
  const hasDraft = form.input.value.trim() || form.expected_behaviour.value.trim();
  if (e.target === dialog && !hasDraft) dialog.close();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    pair_id: form.pair_id.value.trim(),
    target_app: form.target_app.value,
    input: form.input.value.trim(),
    expected_behaviour: form.expected_behaviour.value.trim(),
  };
  const missing = [
    ["pair_id", "a case ID"], ["input", "an input"], ["expected_behaviour", "the expected behaviour"],
  ].filter(([key]) => !payload[key]);
  for (const field of ["pair_id", "input", "expected_behaviour"]) {
    form[field].setAttribute("aria-invalid", String(!payload[field]));
  }
  if (missing.length) {
    $("form-error").textContent = `Add ${missing.map(([, label]) => label).join(" and ")} to save.`;
    form[missing[0][0]].focus();
    return;
  }
  if (state.pairs.some((p) => p.pair_id.toLowerCase() === payload.pair_id.toLowerCase())) {
    form.pair_id.setAttribute("aria-invalid", "true");
    $("form-error").textContent = `${payload.pair_id} already exists. Choose another ID.`;
    form.pair_id.focus();
    return;
  }
  if (!(await ensureWriteAccess())) return;

  const save = $("new-case-save");
  save.disabled = true;
  save.textContent = "Saving…";
  try {
    await apiWrite("/api/golden-pairs", payload);
    state.pairs = await api("/api/golden-pairs");
    dialog.close();
    toast(`Saved ${payload.pair_id}`);
    // A new case has no runs yet, so clear the filters that would hide it
    state.filter = "All";
    state.statusFilter = null;
    renderFilter();
    renderTally();
    openCase(payload.pair_id);
  } catch (err) {
    $("form-error").textContent = err.message;
  } finally {
    save.disabled = false;
    save.textContent = "Save test case";
  }
});

// ----------------------------------------------------
// Surprise: Universal App Testing
// ----------------------------------------------------

const uDialog = $("universal-dialog");
const uForm = $("universal-form");
let uConfigs = [];

function renderUniversalReview() {
  const text = $("u-pairs").value.trim();
  const { cases, errors } = text ? parseGoldenPairs(text) : { cases: [], errors: [] };
  $("u-review").replaceChildren(
    errors.length
      ? h("ul", { class: "u-errors" }, errors.map((e) => h("li", {}, e)))
      : cases.length
        ? h("div", { class: "table-wrap" }, h("table", { class: "compare-table" },
          h("caption", {}, `${cases.length} golden pair${cases.length === 1 ? "" : "s"} ready to add`),
          h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Input"), h("th", { scope: "col" }, "Expected behaviour"))),
          h("tbody", {}, cases.map((c) => h("tr", {}, h("td", {}, c.input), h("td", {}, c.expected_behaviour)))),
        ))
        : null,
  );
  return { cases, errors };
}

async function openUniversal() {
  $("u-error").textContent = "";
  try {
    uConfigs = await api("/api/universal/configs");
  } catch (err) {
    uConfigs = [];
    $("u-error").textContent = err.message;
  }
  $("u-config").replaceChildren(
    h("option", { value: "" }, "New configuration"),
    ...uConfigs.map((c) => h("option", { value: c.config_id }, `${c.name} (${c.method} ${c.url})`)),
  );
  $("u-new-config").hidden = false;
  renderUniversalReview();
  uDialog.showModal();
}

$("universal-btn").addEventListener("click", openUniversal);
$("u-close").addEventListener("click", () => uDialog.close());
$("u-config").addEventListener("change", (e) => { $("u-new-config").hidden = Boolean(e.target.value); });
$("u-pairs").addEventListener("input", renderUniversalReview);
$("u-file").addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  $("u-pairs").value = await file.text();
  renderUniversalReview();
});

uForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  $("u-error").textContent = "";
  let configId = $("u-config").value;
  let headers = {};
  if (!configId) {
    try {
      headers = JSON.parse(uForm.headers.value || "{}");
    } catch {
      $("u-error").textContent = "Headers must be a JSON object, e.g. {\"Authorization\": \"Bearer …\"}.";
      return;
    }
  }
  const { cases, errors } = renderUniversalReview();
  if (errors.length) {
    $("u-error").textContent = "Fix the golden pairs first.";
    return;
  }
  if (!(await ensureWriteAccess())) return;

  const button = $("u-run");
  button.disabled = true;
  try {
    if (!configId) {
      button.textContent = "Saving configuration…";
      const saved = await apiWrite("/api/universal/configs", {
        config_id: uForm.config_id.value.trim(), name: uForm.name.value.trim(), url: uForm.url.value.trim(),
        method: uForm.method.value, headers, body_template: uForm.body_template.value.trim(),
        response_path: uForm.response_path.value.trim(),
      });
      configId = saved.config_id;
      uForm.headers.value = "{}"; // never keep secrets in the page longer than needed
    }
    if (cases.length) {
      button.textContent = "Adding golden pairs…";
      await apiWrite(`/api/universal/configs/${encodeURIComponent(configId)}/cases`, { cases });
    }
    button.textContent = "Running tests…";
    const out = await apiWrite(`/api/universal/configs/${encodeURIComponent(configId)}/run`, {});
    [state.pairs, state.results, state.summary] = await Promise.all([api("/api/golden-pairs"), api("/api/results"), api("/api/summary")]);
    uDialog.close();
    const counts = out.results.reduce((acc, r) => ({ ...acc, [r.verdict]: (acc[r.verdict] || 0) + 1 }), {});
    toast(`Ran ${out.results.length} cases: ${Object.entries(counts).map(([k, v]) => `${v} ${statusMeta(k).label.toLowerCase()}`).join(", ")}`);
    state.filter = "All";
    state.statusFilter = null;
    renderFilter();
    renderTally();
    const first = out.results.find((r) => r.verdict !== "PASS") || out.results[0];
    openCase(first.pair_id, { resultId: first.result_id });
  } catch (err) {
    $("u-error").textContent = err.message;
  } finally {
    button.disabled = false;
    button.textContent = "Save and run";
  }
});

// ----------------------------------------------------
// Mobile rail, toasts, clipboard
// ----------------------------------------------------

function openRail() {
  if (!window.matchMedia("(max-width: 899px)").matches) return;
  document.body.classList.add("rail-open");
  $("scrim").hidden = false;
  $("rail-toggle").setAttribute("aria-expanded", "true");
  $("case-search").focus();
}

function closeRail() {
  if (!document.body.classList.contains("rail-open")) return;
  document.body.classList.remove("rail-open");
  $("scrim").hidden = true;
  $("rail-toggle").setAttribute("aria-expanded", "false");
  $("rail-toggle").focus();
}

$("rail-toggle").addEventListener("click", openRail);
$("rail-close").addEventListener("click", closeRail);
$("scrim").addEventListener("click", closeRail);

function toast(message, tone = "info") {
  const el = h("div", { class: `toast toast-${tone}` },
    h("span", {}, message),
    h("button", { class: "icon-btn", type: "button", "aria-label": "Dismiss", on: { click: () => el.remove() } }, icon("close")),
  );
  $("toasts").append(el);
  setTimeout(() => el.remove(), tone === "error" ? 9000 : 4000);
}

function announce(message) {
  const el = $("announcer");
  el.textContent = "";
  requestAnimationFrame(() => {
    el.textContent = message;
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied response");
  } catch {
    toast("Couldn't copy. Select the text and copy it manually.", "error");
  }
}

// ----------------------------------------------------
// Boot
// ----------------------------------------------------

async function init() {
  renderFilter();
  try {
    const [pairs, results, summary, health] = await Promise.all([
      api("/api/golden-pairs"), api("/api/results"), api("/api/summary"), api("/api/health"),
    ]);
    const backend = (health.database?.backend || "").split(" (")[0];
    Object.assign(state, {
      pairs, results, summary, backend,
      durable: health.database?.durable !== false,
      writesRequireToken: Boolean(health.writes_require_token),
      openExecuteTargets: health.open_execute_targets || [],
    });
  } catch (err) {
    $("view").setAttribute("aria-busy", "false");
    $("view").replaceChildren(
      h("section", { class: "empty-state" },
        h("h1", {}, "Can't load test cases"),
        h("p", {}, err.message),
        h("button", { class: "btn btn-primary", type: "button", on: { click: () => location.reload() } }, "Try again"),
      ),
    );
    return;
  }
  renderTally();
  const view = parseViewState(location.search);
  await openCase(view.caseId, view, { history: "replace" });
}

init();
