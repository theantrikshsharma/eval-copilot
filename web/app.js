import {
  statusMeta, extractQuotes, findHighlights, segment, readableResponse, caseTitle, nextCaseId,
  runsByCase, verdictSentence, relativeTime, absoluteTime, parseTimestamp, parseViewState, serializeViewState,
  evaluationNote,
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

class ApiError extends Error {}

async function api(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      ...options,
    });
  } catch {
    throw new ApiError("Can't reach the Eval Co-pilot server. Check that it's running, then try again.");
  }
  if (!resp.ok) {
    let detail = `The server returned HTTP ${resp.status}.`;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Keep the generic message.
    }
    throw new ApiError(detail);
  }
  return resp.json();
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
  if (!details.has(resultId)) details.set(resultId, await api(`/api/results/${resultId}`));
  return details.get(resultId);
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
  const index = detail.checks.findIndex((c) => c.status !== "PASS");
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
  const query = state.query.trim().toLowerCase();
  return state.pairs.filter((p) => {
    if (state.filter !== "All" && p.target_app.toLowerCase() !== state.filter.toLowerCase()) return false;
    if (!query) return true;
    return [p.pair_id, p.target_app, p.input, p.expected_behaviour].some((v) => (v || "").toLowerCase().includes(query));
  });
}

function renderCaseList() {
  const list = $("case-list");
  const pairs = visiblePairs();
  if (!pairs.length) {
    list.replaceChildren(h("li", { class: "case-empty" }, state.pairs.length ? "No cases match. Clear the search or pick another app." : "No test cases yet."));
    return;
  }
  const grouped = runsByCase(state.results);
  list.replaceChildren(
    ...pairs.map((p) => {
      const runs = grouped.get(p.pair_id) || [];
      const latest = runs[0];
      const trace = runs.slice(0, 6).reverse();
      const statusText = latest ? `Latest: ${statusMeta(latest.overall_status).label}` : "Not run yet";
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
            latest ? glyph(latest.overall_status, "glyph-sm") : h("span", { class: "glyph glyph-none glyph-sm" }),
          ),
          h("span", { class: "case-item-title" }, keepDatesTogether(caseTitle(p.input))),
          trace.length > 1
            ? h("span", { class: "trace", "aria-hidden": "true" },
              trace.map((r) => h("span", { class: `trace-tick tick-${statusMeta(r.overall_status).key}` })))
            : null,
        ),
      );
    }),
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
    h("ul", { class: "tally-list" },
      rows.map(([status, count]) =>
        h("li", { class: count ? "" : "is-zero" }, glyph(status, "glyph-sm"), h("span", {}, statusMeta(status).label), h("strong", {}, String(count))),
      ),
    ),
    top.length
      ? h("details", { class: "tally-top" },
        h("summary", {}, "Checks that fail most"),
        h("ol", {}, top.map((t) => h("li", {}, h("span", {}, t.check_name), h("strong", {}, String(t.fail_count))))))
      : null,
    state.backend ? h("p", { class: "tally-store" }, `Saved to ${state.backend}`) : null,
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
  return api(`/api/compare?result_id_a=${a}&result_id_b=${b}`);
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
  const label = (document.getElementById("run-label")?.value || "").trim();
  state.running = true;
  const started = performance.now();
  updateRunButton(0);
  runTimer = setInterval(() => updateRunButton(Math.round((performance.now() - started) / 1000)), 1000);

  try {
    const detail = await api("/api/execute", {
      method: "POST",
      body: JSON.stringify({ pair_id: pair.pair_id, run_id: label || null }),
    });
    details.set(detail.result_id, detail);
    [state.results, state.summary] = await Promise.all([api("/api/results"), api("/api/summary")]);
    state.resultId = detail.result_id;
    state.checkIndex = defaultCheckIndex(detail);
    state.compare = null;
    state.compareData = null;
    state.freshResultId = detail.result_id;
    syncUrl();
    const meta = statusMeta(detail.overall_status);
    announce(`Run finished. ${meta.label}. ${verdictSentence(detail.overall_status, detail.checks)}`);
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
        const meta = statusMeta(r.overall_status);
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
      h("p", { class: "empty-line" }, "This case hasn't been run yet. Run it to see how the app responds and which checks pass."),
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
  const meta = statusMeta(detail.overall_status);
  const index = runs.findIndex((r) => r.result_id === detail.result_id);
  const previous = runs[index + 1];
  const fresh = state.freshResultId === detail.result_id;

  result.className = `result result-${meta.key}${fresh ? " is-fresh" : ""}`;
  result.replaceChildren(
    h("div", { class: "verdict" },
      h("div", { class: "verdict-main" },
        h("p", { class: `verdict-word tone-${meta.key}` }, glyph(detail.overall_status, "glyph-lg"), h("span", {}, meta.label)),
        h("p", { class: "verdict-sentence" }, verdictSentence(detail.overall_status, detail.checks)),
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
          const meta = statusMeta(c.status);
          return h("li", { style: `--i:${i}` },
            h("button", {
              type: "button",
              class: `check check-${meta.key}`,
              "data-index": String(i),
              "aria-pressed": String(i === state.checkIndex),
              on: { click: () => selectCheck(i) },
            },
              glyph(c.status),
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
  const tone = selectedCheck ? statusMeta(selectedCheck.status).key : "";

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
      `Run #${r.result_id}, ${statusMeta(r.overall_status).label}, ${relativeTime(r.timestamp)}`));

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
      h("span", { class: `pill pill-${statusMeta(a.overall_status).key}` }, glyph(a.overall_status, "glyph-sm"), statusMeta(a.overall_status).label),
      h("span", { class: "transition-arrow" }, icon("arrow"), h("span", { class: "sr-only" }, "became")),
      h("span", { class: `pill pill-${statusMeta(b.overall_status).key}` }, glyph(b.overall_status, "glyph-sm"), statusMeta(b.overall_status).label),
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
              compareCell(c.status_a, c.evidence_a),
              compareCell(c.status_b, c.evidence_b),
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
  note.textContent = exists ? `${id} already exists. Saving replaces its input and expected behaviour.` : "";
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

  const save = $("new-case-save");
  save.disabled = true;
  save.textContent = "Saving…";
  try {
    await api("/api/golden-pairs", { method: "POST", body: JSON.stringify(payload) });
    state.pairs = await api("/api/golden-pairs");
    dialog.close();
    toast(`Saved ${payload.pair_id}`);
    state.filter = "All";
    renderFilter();
    openCase(payload.pair_id);
  } catch (err) {
    $("form-error").textContent = err.message;
  } finally {
    save.disabled = false;
    save.textContent = "Save test case";
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
    Object.assign(state, { pairs, results, summary, backend });
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
