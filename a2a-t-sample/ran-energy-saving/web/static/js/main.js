/** Bootstrap of the RAN energy-saving Run Explorer.
 *
 * Wiring only: the store owns state, the views render it, and this module connects the DOM events,
 * the SSE stream, the manual decision form and the keyboard map.
 */

import { ApiClient } from "./api.js";
import { append, el, statusInfo } from "./dom.js";
import {
  EM_DASH,
  formatClock,
  formatCountdown,
  formatDuration,
  formatNumber,
  shortId,
  truncate,
} from "./format.js";
import { FilterState, FollowController, availableRounds, isAtBottom } from "./filters.js";
import { Inspector, phaseDurationList, runElapsed } from "./inspector.js";
import { SHORTCUTS, installShortcuts } from "./shortcuts.js";
import { RunStore, itemKey } from "./store.js";
import { TimelineView } from "./timeline.js";

const $ = (id) => document.getElementById(id);

const api = new ApiClient();
const store = new RunStore();
const filters = new FilterState();
const follow = new FollowController();
const app = {
  meta: null,
  runId: null,
  subscription: null,
  loading: true,
  startError: null,
  decisionSeq: null,
  decisionAt: null,
  decisionTimeout: null,
  decisionSubmitting: false,
  decisionTimedOut: false,
  inspectorOpen: true,
  theme: "auto",
  lastSnapshotAt: 0,
  disconnected: false,
  finalizing: false,
};

const timeline = new TimelineView({
  timeline: $("timeline"),
  stats: $("timeline-stats"),
  onSelect: (key) => selectItem(key),
  states: {
    empty: $("timeline-empty"),
    loading: $("timeline-loading"),
    error: $("timeline-error"),
    errorText: $("timeline-error-text"),
    noTrace: $("timeline-no-trace"),
  },
});

const inspector = new Inspector({
  elements: {
    root: $("inspector"),
    stepLabel: $("inspector-step"),
    kindLabel: $("inspector-kind"),
    tabs: $("inspector-tabs"),
    body: $("inspector-body"),
    prevButton: $("inspector-prev"),
    nextButton: $("inspector-next"),
    closeButton: $("inspector-close"),
  },
  callbacks: {
    onStep: (offset) => stepInspector(offset),
    onClose: () => closeInspector(),
    onSelectKey: (key) => selectItem(key),
    fetchSpan: (spanId) => api.getSpan(app.runId, spanId),
    notify: (message) => notify(message),
  },
});

let renderQueued = false;

/** Queue one coalesced render on the next animation frame. */
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderAll();
  });
}

/** Render every view from the current store state. */
function renderAll() {
  renderHeader();
  renderContext();
  renderStepper();
  renderRail();
  renderTimeline();
  renderManual();
  renderFooter();
  renderInspector();
  syncFollow();
}

/** Render the header identity/status row. */
function renderHeader() {
  const runId = store.runId;
  const runButton = $("run-id");
  runButton.disabled = !runId;
  runButton.textContent = runId ? `Run ${shortId(runId)}` : "No run";
  runButton.title = runId || "No run started";

  const waiting = store.pendingDecision;
  const status = waiting ? "waiting" : store.status;
  const info = statusInfo(
    status === "running"
      ? "running"
      : status === "finished"
        ? "completed"
        : status === "failed"
          ? "failed"
          : status === "waiting"
            ? "waiting"
            : "pending",
  );
  const pill = $("status-pill");
  pill.dataset.status = status;
  pill.className = `status-pill status-${status}`;
  pill.textContent = `${info.glyph} ${
    status === "running"
      ? "running"
      : status === "finished"
        ? "completed"
        : status === "failed"
          ? "failed"
          : status === "waiting"
            ? "waiting"
            : "idle"
  }`;

  $("elapsed").textContent = formatClock(runElapsed(store));
  const token = store.tokenTotal();
  $("token-total").textContent = token
    ? token.total !== undefined && token.total > 0
      ? `${formatNumber(token.total)} tok`
      : "n/a (scripted)"
    : EM_DASH;
  $("start-button").disabled = !app.meta || store.running || store.pendingDecision;

  const themeButton = $("theme-toggle");
  if (themeButton) themeButton.textContent = `Theme: ${app.theme}`;
}

/** Render the context strip. */
function renderContext() {
  const meta = store.runMeta();
  const ids = store.ids();
  $("ctx-scenario").textContent = meta.scenario || EM_DASH;
  $("ctx-mode").textContent = meta.mode || EM_DASH;
  $("ctx-transport").textContent = meta.transport || EM_DASH;
  $("ctx-discovery").textContent = meta.discovery || EM_DASH;
  $("ctx-language").textContent = meta.language || app.meta?.language || EM_DASH;
  $("ctx-model").textContent = meta.modelLabel || meta.modelMode || EM_DASH;
  $("context-id").textContent = ids.contextId || EM_DASH;
  $("task-id").textContent = ids.taskId || EM_DASH;
  $("current-activity").textContent = store.derived().activity || "Idle";
  $("rail-language").textContent = app.meta?.language || EM_DASH;
}

/** Render the eight-phase stepper. */
function renderStepper() {
  const derived = store.derived();
  for (const button of document.querySelectorAll(".phase-step-button")) {
    const key = button.dataset.phase;
    const state = derived.phaseStates[key] || "pending";
    const group = derived.groups.find((candidate) => candidate.key === key);
    const item = button.closest(".phase-step");
    item.className = `phase-step phase-${state}`;
    button.setAttribute("aria-current", state === "active" ? "step" : "false");
    const meta = button.querySelector(".phase-step-meta");
    meta.replaceChildren();
    if (state === "pending") {
      meta.textContent = "pending";
      continue;
    }
    if (state === "skipped") {
      meta.textContent = "skipped";
      continue;
    }
    if (state === "failed") {
      meta.textContent = "failed";
      continue;
    }
    const parts = [];
    if (group?.stats.durationMs !== null && group?.stats.durationMs !== undefined) {
      parts.push(formatDuration(group.stats.durationMs));
    } else {
      parts.push(`${group?.stats.steps ?? 0} steps`);
    }
    if (key === "resolution") {
      const label = store.resolutionLabel();
      if (label) parts.push(label);
    }
    meta.textContent = parts.join(" · ");
    if (key === "negotiation" && group?.rounds?.length) {
      for (const round of group.rounds) {
        const chip = el(
          "span",
          `round-chip round-${round.terminal ? "done" : "active"}`,
          `R${round.round}${round.terminal ? " ✓" : " ◐"}`,
        );
        meta.appendChild(document.createTextNode(" "));
        meta.appendChild(chip);
      }
    }
  }
}

/** Render the analysis, decision log and export rail. */
function renderRail() {
  const derived = store.derived();
  const analysis = derived.analysis;
  $("analysis-wall").textContent = formatClock(runElapsed(store));
  $("analysis-llm").textContent = `${formatNumber(analysis.llmCalls)}${
    analysis.scriptedCalls ? ` (${analysis.scriptedCalls} scripted)` : ""
  }`;
  $("analysis-tokens").textContent = tokenAnalysisText(analysis);
  $("analysis-tools").textContent = `${formatNumber(analysis.tools)} · ${analysis.toolErrors} errors`;
  $("analysis-errors").textContent = `${analysis.errors} / ${analysis.retries}`;
  $("analysis-parse").textContent = formatNumber(analysis.parseFailures);
  $("analysis-decisions").textContent = formatNumber(analysis.decisions);

  const freshDurations = phaseDurationList(store);
  $("phase-durations").replaceChildren(...Array.from(freshDurations.children));

  const anomalies = $("anomalies-list");
  anomalies.replaceChildren();
  if (derived.anomalies.length === 0) {
    anomalies.appendChild(el("li", "placeholder", "None detected."));
  } else {
    for (const anomaly of derived.anomalies) {
      const row = el("li", `anomaly anomaly-${anomaly.kind}`);
      if (anomaly.key) {
        const button = el("button", "anomaly-button", anomaly.message);
        button.type = "button";
        button.addEventListener("click", () => selectItem(anomaly.key));
        row.appendChild(button);
      } else {
        row.textContent = anomaly.message;
      }
      anomalies.appendChild(row);
    }
  }

  const decisionLog = $("decision-log");
  decisionLog.replaceChildren();
  if (derived.decisionLog.length === 0) {
    decisionLog.appendChild(el("li", "placeholder", "No decisions yet."));
  } else {
    for (const entry of derived.decisionLog) {
      const row = el("li", "decision-entry");
      const button = el("button", "decision-button");
      button.type = "button";
      append(
        button,
        el("span", "decision-head", `${entry.round ? `R${entry.round} ` : ""}${String(entry.action).toUpperCase()} · ${entry.source}`),
        el("span", "decision-rationale", truncate(entry.rationale, 120)),
      );
      button.addEventListener("click", () => selectItem(entry.key));
      row.appendChild(button);
      decisionLog.appendChild(row);
    }
  }

  const enabled = Boolean(store.runId);
  const urls = store.runId
    ? api.exportUrls(store.runId)
    : { trace: "#", transcriptJson: "#", transcriptMarkdown: "#" };
  setLink("export-trace", enabled, urls.trace);
  setLink("export-json", enabled, urls.transcriptJson);
  setLink("export-md", enabled, urls.transcriptMarkdown);
  setLink("menu-trace", enabled, urls.trace);
  setLink("menu-transcript", enabled, urls.transcriptJson);

  renderRoundFilter();

  for (const chip of document.querySelectorAll("#filter-chips [data-filter]")) {
    const active = chip.dataset.filter === filters.filter;
    chip.classList.toggle("is-active", active);
    chip.setAttribute("aria-pressed", String(active));
  }
  for (const button of document.querySelectorAll("[data-view]")) {
    const active = button.dataset.view === filters.view;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  if ($("actor-filter").value !== filters.actor) $("actor-filter").value = filters.actor;
  if ($("search-input").value !== filters.search) $("search-input").value = filters.search;
}

/** Enable or disable one download link. */
function setLink(id, enabled, href) {
  const link = $(id);
  if (!link) return;
  if (enabled) {
    link.href = href;
    link.setAttribute("download", "");
    link.removeAttribute("aria-disabled");
  } else {
    link.href = "#";
    link.setAttribute("aria-disabled", "true");
    link.removeAttribute("download");
  }
}

/** Render the timeline, preserving scroll position when the reader is not following. */
function renderTimeline() {
  const scroll = $("timeline-scroll");
  const previousTop = scroll.scrollTop;
  timeline.render({
    store,
    filters,
    selectedKey: store.selectedKey,
    viewState: { loading: app.loading, error: app.startError },
    onClearFilters: () => resetFilters(),
  });
  if (follow.enabled && !follow.paused) {
    scroll.scrollTop = scroll.scrollHeight;
  } else {
    scroll.scrollTop = previousTop;
  }
}

/** Render the manual decision panels (manual mode only). */
function renderManual() {
  const meta = store.runMeta();
  const manual = store.pendingDecision && meta.mode === "manual";
  $("manual-panel-rail").hidden = !manual;
  $("manual-pinned").hidden = !manual;
  if (!manual) return;

  const request = store.decisionRequest;
  const proposal = request?.proposal || {};
  const round = request?.round ?? proposal.round ?? "?";
  const maxRounds = request?.maxRounds ?? proposal.max_rounds ?? "?";
  const kind = proposal.kind || "negotiation";
  const performative = request?.performative || proposal.performative || "PROPOSE";
  const content = proposal.content || "";
  const summary = `${kind} · R${round}/${maxRounds} · ${performative}${content ? `\n${content}` : ""}`;
  for (const node of document.querySelectorAll('[data-role="manual-context"]')) {
    node.textContent = summary;
  }
  const roundNode = document.querySelector("#manual-pinned [data-role='manual-round']");
  if (roundNode) roundNode.textContent = `R${round} · ${kind}`;
  updateCountdown();
  for (const button of document.querySelectorAll(".decision-submit")) {
    button.disabled = app.decisionSubmitting || app.decisionTimedOut;
  }
}

/** Render the footer connection state. */
function renderFooter() {
  const last = store.list()[store.list().length - 1];
  $("last-event").textContent = last
    ? `#${String(last.number).padStart(2, "0")} · ${truncate(last.title, 80)}`
    : "No events yet";
  const banner = $("connection-banner");
  banner.hidden = !(app.disconnected && store.running);
  syncConnection();
}

/** Render the inspector for the selection or the run summary. */
function renderInspector() {
  if (!app.inspectorOpen) {
    inspector.hide();
    return;
  }
  const keys = timeline.orderedKeys.length > 0 ? timeline.orderedKeys : store.list().map(itemKey);
  const key = store.selectedKey;
  if (!key) {
    inspector.showRun(store, keys.length);
    return;
  }
  const item = store.getItem(key);
  if (!item) {
    inspector.showRun(store, keys.length);
    return;
  }
  inspector.update(item, { store, index: keys.indexOf(key), total: keys.length });
}

/** Synchronize both follow toggles with the controller. */
function syncFollow() {
  const header = $("follow-toggle");
  const toolbar = $("timeline-follow");
  const label = follow.label();
  header.setAttribute("aria-pressed", String(!follow.paused));
  $("follow-label").textContent = follow.paused ? label : "Follow";
  header.classList.toggle("is-paused", follow.paused);
  toolbar.setAttribute("aria-pressed", String(!follow.paused));
  toolbar.textContent = follow.paused ? label : "Following";
  toolbar.classList.toggle("is-paused", follow.paused);
}

/** Sync the connection indicator and banner copy. */
function syncConnection() {
  const node = $("connection-state");
  let state = "idle";
  let label = "idle";
  if (app.disconnected) {
    state = "reconnecting";
    label = "reconnecting";
  } else if (store.pendingDecision) {
    state = "waiting";
    label = "waiting for decision";
  } else if (store.status === "running") {
    state = "live";
    label = "live";
  } else if (store.status === "finished") {
    state = "finished";
    label = "stream closed";
  } else if (store.status === "failed") {
    state = "failed";
    label = "failed";
  }
  node.dataset.state = state;
  $("connection-label").textContent = label;
}

/** Rebuild the round filter options while keeping the current selection when possible. */
function renderRoundFilter() {
  const select = $("round-filter");
  const rounds = availableRounds(store.list());
  const current = filters.round;
  const signature = rounds.join(",");
  if (select.dataset.signature !== signature) {
    select.replaceChildren();
    const all = el("option", "", "All rounds");
    all.value = "all";
    select.appendChild(all);
    for (const round of rounds) {
      const option = el("option", "", `R${round}`);
      option.value = String(round);
      select.appendChild(option);
    }
    select.dataset.signature = signature;
  }
  if (current !== "all" && !rounds.includes(Number(current))) {
    filters.round = "all";
  }
  select.value = filters.round;
}

/** Open the inspector for one selection. */
function selectItem(key) {
  store.select(key);
  const item = store.getItem(key);
  // An LLM row is first-class: opening it lands directly on its request payload.
  if (item?.kind === "llm") inspector.tab = "llm-request";
  app.inspectorOpen = true;
  follow.pauseForReader();
  applyInspectorLayout();
  scheduleRender();
}

/** Close the inspector and clear the selection. */
function closeInspector() {
  store.select(null);
  app.inspectorOpen = false;
  applyInspectorLayout();
  scheduleRender();
}

/** Move the inspector selection by one row. */
function stepInspector(offset) {
  const keys = timeline.orderedKeys.length > 0 ? timeline.orderedKeys : store.list().map(itemKey);
  store.selectByOffset(offset, keys);
  if (store.selectedKey) {
    app.inspectorOpen = true;
    follow.pauseForReader();
    applyInspectorLayout();
    scheduleRender();
  }
}

/** Apply the open/closed layout classes of the inspector. */
function applyInspectorLayout() {
  const overlay = window.matchMedia("(max-width: 1119px)").matches;
  document.body.classList.toggle("inspector-open", app.inspectorOpen);
  document.body.classList.toggle("inspector-closed", !app.inspectorOpen);
  $("inspector").setAttribute("aria-hidden", String(!app.inspectorOpen));
  $("inspector-backdrop").hidden = !(app.inspectorOpen && overlay);
}

/** Reset all filters and the search box. */
function resetFilters() {
  filters.filter = "all";
  filters.actor = "all";
  filters.round = "all";
  filters.search = "";
  scheduleRender();
}

/** Start one run from the rail form. */
async function startRun(event) {
  event.preventDefault();
  if (!app.meta) {
    showStartError("Process metadata is unavailable; the server is not ready.");
    return;
  }
  app.startError = null;
  $("run-error").hidden = true;
  $("run-error").textContent = "";
  $("start-button").disabled = true;
  stopSubscription();
  api.clearSpanCache();
  try {
    const payload = await api.startRun({
      scenario: $("scenario-select").value,
      mode: $("mode-select").value,
      transport: $("transport-select").value,
      discovery: $("discovery-select").value,
    });
    app.runId = payload.runId;
    store.beginRun(payload.runId, app.meta);
    app.decisionSeq = null;
    app.decisionAt = null;
    app.decisionTimeout = null;
    app.decisionTimedOut = false;
    app.decisionSubmitting = false;
    app.disconnected = false;
    app.finalizing = false;
    resetDecisionForms();
    history.replaceState(null, "", `#run=${payload.runId}`);
    connect(payload.runId);
    scheduleRender();
  } catch (error) {
    showStartError(String(error?.message ?? error));
    $("start-button").disabled = false;
  }
}

/** Show one start failure in the rail, and in the timeline only when it is empty. */
function showStartError(message) {
  app.startError = store.list().length === 0 ? message : null;
  const node = $("run-error");
  node.hidden = false;
  node.textContent = message;
  $("start-button").disabled = false;
  scheduleRender();
}

/** Summarize LLM token usage for the rail (scripted runs never show a zero). */
function tokenAnalysisText(analysis) {
  if (analysis.llmCalls === 0) return EM_DASH;
  if (analysis.numericCalls === 0) return "n/a (scripted)";
  const base = `${formatNumber(analysis.promptTokens)} in / ${formatNumber(analysis.completionTokens)} out`;
  return analysis.scriptedCalls > 0 ? `${base} (+${analysis.scriptedCalls} scripted)` : base;
}

/** Open the event stream of one run. */
function connect(runId) {
  stopSubscription();
  app.disconnected = false;
  app.subscription = api.subscribe(runId, {
    onOpen: () => {
      app.disconnected = false;
      scheduleRender();
    },
    onFrame: (frame) => handleFrame(frame),
    onError: () => {
      if (!store.running) return;
      app.disconnected = true;
      scheduleRender();
    },
    onClose: () => {
      app.disconnected = false;
      finalizeRun();
    },
  });
  scheduleRender();
}

/** Handle one new SSE frame. */
function handleFrame(frame) {
  if (frame.type === "decision_requested") {
    if (app.decisionSeq !== frame.seq) {
      app.decisionSeq = frame.seq;
      app.decisionAt =
        typeof frame.requestedAt === "string" ? Date.parse(frame.requestedAt) : Date.now();
      app.decisionTimeout =
        typeof frame.timeoutSeconds === "number" ? frame.timeoutSeconds : null;
      app.decisionTimedOut = false;
      app.decisionSubmitting = false;
      resetDecisionForms();
    }
  }
  const changed = store.applyFrame(frame);
  if (!changed) return;
  app.startError = null;
  if (frame.type === "finished") {
    app.decisionAt = null;
    app.decisionTimedOut = false;
    finalizeRun();
  } else if (follow.paused) {
    follow.noteNew();
  }
  announce(frame);
  scheduleRender();
}

/** Fetch the terminal snapshot and close the stream bookkeeping (once per run). */
async function finalizeRun() {
  if (app.finalizing) return;
  app.finalizing = true;
  stopSubscription();
  if (!app.runId) return;
  try {
    const snapshot = await api.getRun(app.runId);
    store.applySnapshot(snapshot);
  } catch {
    // The frames already carry the outcome; the snapshot only enriches it.
  }
  scheduleRender();
}

/** Announce one new frame in the polite live region. */
function announce(frame) {
  let message = "";
  if (frame.type === "run_started") message = `Run started: ${frame.scenario || "run"}`;
  else if (frame.type === "finished") {
    message = frame.status === "failed" ? `Run failed: ${frame.error || "unknown error"}` : "Run finished.";
  } else if (frame.type === "decision_requested") {
    message = `Manual decision requested for round ${frame.round ?? "?"}.`;
  } else if (frame.type === "event" || frame.type === "trace") {
    const last = store.list()[store.list().length - 1];
    message = last ? `New step ${last.number}: ${last.title}` : "New step.";
  }
  if (message) $("live-region").textContent = message;
}

/** Stop the current SSE subscription. */
function stopSubscription() {
  if (app.subscription) {
    app.subscription.close();
    app.subscription = null;
  }
}

/** Show one transient notification in the live region. */
function notify(message) {
  $("live-region").textContent = message;
}

/** Resume a run from the URL hash after a reload. */
async function resumeRun(runId) {
  app.loading = false;
  app.runId = runId;
  store.beginRun(runId, app.meta);
  api.clearSpanCache();
  app.subscription = null;
  app.finalizing = false;
  try {
    const snapshot = await api.getRun(runId);
    store.applySnapshot(snapshot);
  } catch (error) {
    app.startError = `Run ${shortId(runId)} could not be loaded: ${String(error?.message ?? error)}`;
    history.replaceState(null, "", location.pathname);
    app.runId = null;
    store.reset();
    store.setMeta(app.meta);
    scheduleRender();
    return;
  }
  connect(runId);
  scheduleRender();
}

/** Read the run id from the URL hash. */
function runIdFromHash() {
  const match = /^#run=([A-Za-z0-9_-]+)$/.exec(location.hash);
  return match ? match[1] : null;
}

/** Populate the run setup selects from the process metadata. */
function populateMeta(meta) {
  fillSelect($("scenario-select"), meta.scenarios || [], meta.defaultScenario);
  fillSelect($("mode-select"), meta.modes || [], meta.defaultMode);
  fillSelect($("transport-select"), meta.transports || [], meta.defaultTransport);
  fillSelect($("discovery-select"), meta.discoveries || [], meta.defaultDiscovery);
  $("rail-language").textContent = meta.language || EM_DASH;
}

/** Fill one select with options, keeping one default selected. */
function fillSelect(select, values, selected) {
  select.replaceChildren();
  for (const value of values) {
    const option = el("option", "", value);
    option.value = value;
    select.appendChild(option);
  }
  if (selected && values.includes(selected)) select.value = selected;
}

/** True when a filter/search is active. */
function isFilterActive() {
  return (
    filters.filter !== "all" ||
    filters.actor !== "all" ||
    filters.round !== "all" ||
    Boolean(filters.search.trim())
  );
}

/** Scroll to one phase from the stepper, clearing filters when the phase is hidden. */
function goToPhase(phase) {
  if (isFilterActive()) {
    resetFilters();
    renderAll();
  }
  timeline.scrollToPhase(phase);
}

/** Toggle following from the header, toolbar or keyboard. */
function toggleFollow() {
  follow.toggle();
  if (!follow.paused) {
    const scroll = $("timeline-scroll");
    scroll.scrollTop = scroll.scrollHeight;
  }
  syncFollow();
}

/** Render or update the manual decision countdown. */
function updateCountdown() {
  if (!store.pendingDecision) return;
  let text = "waiting";
  if (app.decisionAt && !Number.isNaN(app.decisionAt)) {
    const elapsed = Math.max(0, (Date.now() - app.decisionAt) / 1000);
    if (app.decisionTimeout !== null) {
      const remaining = app.decisionTimeout - elapsed;
      text = formatCountdown(remaining);
      if (remaining <= 0) {
        text = "0:00 · timed out";
        app.decisionTimedOut = true;
        for (const button of document.querySelectorAll(".decision-submit")) button.disabled = true;
      }
    } else {
      text = `waiting ${Math.floor(elapsed)}s`;
    }
  }
  for (const node of document.querySelectorAll('[data-role="countdown"]')) node.textContent = text;
}

/** Reset both decision forms to their empty state. */
function resetDecisionForms() {
  for (const field of document.querySelectorAll("[data-field]")) {
    if (field.dataset.field === "action") field.value = "accept";
    else field.value = "";
  }
  for (const node of document.querySelectorAll(".decision-error")) {
    node.hidden = true;
    node.textContent = "";
  }
  for (const button of document.querySelectorAll(".decision-submit")) button.disabled = false;
}

/** Mirror a decision field value across both panels. */
function syncDecisionField(source) {
  for (const target of document.querySelectorAll(`[data-field="${source.dataset.field}"]`)) {
    if (target !== source) target.value = source.value;
  }
}

/** Submit the manual decision of one panel. */
async function submitDecision(button) {
  if (!store.pendingDecision || !app.runId || app.decisionTimedOut || app.decisionSubmitting) return;
  const panel = button.closest(".manual-panel, .manual-pinned") || document;
  const read = (name) => panel.querySelector(`[data-field="${name}"]`)?.value ?? "";
  const errorNode = panel.querySelector(".decision-error");
  let filled;
  let counterParams;
  try {
    filled = parseOptionalJson(read("filled"), "Filled params");
    counterParams = parseOptionalJson(read("counterParams"), "Counter params");
  } catch (error) {
    showDecisionError(errorNode, String(error.message));
    return;
  }
  const decision = {
    action: read("action") || "accept",
    rationale: read("rationale").trim(),
  };
  if (filled !== null) decision.filled_params = filled;
  const counter = read("counter").trim();
  if (counter) decision.counter_proposal = counter;
  if (counterParams !== null) decision.counter_params = counterParams;

  app.decisionSubmitting = true;
  for (const node of document.querySelectorAll(".decision-submit")) node.disabled = true;
  try {
    await api.submitDecision(app.runId, decision);
    showDecisionError(errorNode, null);
    notify("Decision submitted.");
  } catch (error) {
    showDecisionError(errorNode, String(error?.message ?? error));
    app.decisionSubmitting = false;
    for (const node of document.querySelectorAll(".decision-submit")) node.disabled = false;
  }
}

/** Parse one optional JSON text field. */
function parseOptionalJson(text, label) {
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${label}: invalid JSON (${error.message})`);
  }
}

/** Show or clear one decision form error. */
function showDecisionError(node, message) {
  if (!node) return;
  node.hidden = !message;
  node.textContent = message || "";
}

/** Open the shortcuts dialog. */
function openShortcuts() {
  const dialog = $("shortcuts-dialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

/** Close the shortcuts dialog. */
function closeShortcuts() {
  const dialog = $("shortcuts-dialog");
  if (dialog.open) dialog.close();
  else dialog.removeAttribute("open");
}

/** Populate the shortcuts dialog from the shared map. */
function renderShortcuts() {
  const list = $("shortcuts-list");
  list.replaceChildren();
  for (const entry of SHORTCUTS) {
    const row = el("div", "shortcut-row");
    append(row, el("dt", "shortcut-keys", entry.keys), el("dd", "shortcut-description", entry.description));
    list.appendChild(row);
  }
}

/** Apply one theme and persist the preference. */
function applyTheme(theme) {
  app.theme = theme;
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("a2at-ran-theme", theme);
  } catch {
    // Private-browsing storage failures are irrelevant to the explorer.
  }
  $("theme-toggle").textContent = `Theme: ${theme}`;
}

/** Cycle auto → light → dark. */
function cycleTheme() {
  const order = ["auto", "light", "dark"];
  applyTheme(order[(order.indexOf(app.theme) + 1) % order.length]);
}

/** Wire the static toolbar, form and dialog controls. */
function bindControls() {
  $("run-form").addEventListener("submit", startRun);
  $("run-id").addEventListener("click", () => {
    if (!store.runId) return;
    navigator.clipboard?.writeText(store.runId).then(
      () => notify("Run id copied."),
      () => notify(store.runId),
    );
  });
  $("follow-toggle").addEventListener("click", toggleFollow);
  $("timeline-follow").addEventListener("click", toggleFollow);
  $("expand-all").addEventListener("click", () => timeline.expandAll());
  $("collapse-all").addEventListener("click", () => timeline.collapseAll());

  for (const chip of document.querySelectorAll("#filter-chips [data-filter]")) {
    chip.addEventListener("click", () => {
      filters.filter = chip.dataset.filter;
      scheduleRender();
    });
  }
  for (const button of document.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => {
      filters.view = button.dataset.view;
      scheduleRender();
    });
  }
  $("actor-filter").addEventListener("change", (event) => {
    filters.actor = event.target.value;
    scheduleRender();
  });
  $("round-filter").addEventListener("change", (event) => {
    filters.round = event.target.value;
    scheduleRender();
  });
  $("search-input").addEventListener("input", (event) => {
    filters.search = event.target.value;
    scheduleRender();
  });
  $("connection-retry").addEventListener("click", () => {
    app.subscription?.reopen?.();
    app.disconnected = false;
    scheduleRender();
  });

  for (const button of document.querySelectorAll(".phase-step-button")) {
    button.addEventListener("click", () => goToPhase(button.dataset.phase));
  }

  for (const button of document.querySelectorAll(".decision-submit")) {
    button.addEventListener("click", () => submitDecision(button));
  }
  for (const button of document.querySelectorAll(".decision-focus")) {
    button.addEventListener("click", () => {
      $("manual-pinned").scrollIntoView({ behavior: "smooth", block: "start" });
      $("manual-pinned").querySelector("[data-field='rationale']")?.focus();
    });
  }
  for (const field of document.querySelectorAll("[data-field]")) {
    field.addEventListener("input", () => syncDecisionField(field));
    field.addEventListener("change", () => syncDecisionField(field));
  }

  $("inspector-backdrop").addEventListener("click", () => closeInspector());

  $("menu-button").addEventListener("click", (event) => {
    event.stopPropagation();
    const panel = $("menu-panel");
    const open = panel.hidden;
    panel.hidden = !open;
    $("menu-button").setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (event) => {
    const panel = $("menu-panel");
    if (panel.hidden) return;
    if (event.target.closest("#menu-panel") || event.target.closest("#menu-button")) return;
    panel.hidden = true;
    $("menu-button").setAttribute("aria-expanded", "false");
  });
  $("theme-toggle").addEventListener("click", cycleTheme);
  $("menu-shortcuts").addEventListener("click", () => {
    $("menu-panel").hidden = true;
    openShortcuts();
  });
  $("shortcuts-close").addEventListener("click", closeShortcuts);
  $("rail-toggle").addEventListener("click", () => {
    const expanded = document.body.classList.toggle("rail-expanded");
    $("rail-toggle").setAttribute("aria-expanded", String(expanded));
  });

  let scrollQueued = false;
  $("timeline-scroll").addEventListener("scroll", () => {
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      const element = $("timeline-scroll");
      if (isAtBottom(element)) follow.resume();
      else follow.pauseForReader();
      syncFollow();
    });
  });

  installShortcuts({
    next: () => stepInspector(1),
    previous: () => stepInspector(-1),
    open: () => {
      if (store.selectedKey) {
        app.inspectorOpen = true;
        applyInspectorLayout();
        scheduleRender();
      } else if (timeline.orderedKeys.length > 0) {
        selectItem(timeline.orderedKeys[0]);
      }
    },
    escape: () => {
      if ($("shortcuts-dialog").open) {
        closeShortcuts();
        return;
      }
      if (!$("menu-panel").hidden) {
        $("menu-panel").hidden = true;
        return;
      }
      if (document.activeElement === $("search-input")) {
        $("search-input").blur();
        return;
      }
      if (app.inspectorOpen) closeInspector();
    },
    search: () => $("search-input").focus(),
    follow: toggleFollow,
    tab: (number) => {
      if (!app.inspectorOpen) {
        app.inspectorOpen = true;
        applyInspectorLayout();
        scheduleRender();
      }
      inspector.selectTabByNumber(number);
    },
    previousStep: () => stepInspector(-1),
    nextStep: () => stepInspector(1),
    help: openShortcuts,
    submitDecision: () => {
      const button = document.querySelector(".decision-submit:not([disabled])");
      if (button) submitDecision(button);
    },
  });

  window.addEventListener("hashchange", () => {
    const runId = runIdFromHash();
    if (runId && runId !== app.runId) resumeRun(runId);
  });
  window.matchMedia("(max-width: 1119px)").addEventListener("change", applyInspectorLayout);
}

/** Watch the run status in the background so ids/report enrich even if a frame is missed. */
function startPolling() {
  setInterval(async () => {
    if (!app.runId || !store.running) return;
    const now = Date.now();
    if (now - app.lastSnapshotAt < 2000) return;
    app.lastSnapshotAt = now;
    try {
      const snapshot = await api.getRun(app.runId);
      if (snapshot.runId === app.runId) store.applySnapshot(snapshot);
      scheduleRender();
    } catch {
      // The SSE stream is authoritative for live state; a failed poll is not an error.
    }
  }, 1000);
}

/** Update clocks and the manual countdown without rebuilding the timeline. */
function startTicker() {
  setInterval(() => {
    $("elapsed").textContent = formatClock(runElapsed(store));
    $("analysis-wall").textContent = formatClock(runElapsed(store));
    updateCountdown();
  }, 250);
}

/** Load the process metadata and restore a run from the URL hash. */
async function boot() {
  bindControls();
  renderShortcuts();
  const savedTheme = (() => {
    try {
      return localStorage.getItem("a2at-ran-theme") || "auto";
    } catch {
      return "auto";
    }
  })();
  applyTheme(savedTheme);
  // The run summary is the default inspector on wide screens; narrow layouts start uncluttered.
  app.inspectorOpen = !window.matchMedia("(max-width: 1119px)").matches;
  applyInspectorLayout();
  startPolling();
  startTicker();
  scheduleRender();

  try {
    app.meta = await api.getMeta();
    store.setMeta(app.meta);
    populateMeta(app.meta);
  } catch (error) {
    app.startError = `Process metadata is unavailable: ${String(error?.message ?? error)}`;
  }
  app.loading = false;
  const runId = runIdFromHash();
  if (runId) await resumeRun(runId);
  else scheduleRender();
}

boot();
