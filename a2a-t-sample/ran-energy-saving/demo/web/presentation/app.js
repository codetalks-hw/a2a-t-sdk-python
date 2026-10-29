/** Presentation page: one request, one negotiation, one report.
 *
 * The page is audience-facing. It renders the story of a single energy-saving request and keeps
 * every transport, model and trace detail out of the way (the raw frames stay in the collapsed
 * "technical details" section).
 */

import {
  STATUS,
  applyFrame,
  createState,
  executionSteps,
  resolutionOf,
  roundInfo,
  stageOf,
} from "./model.js";
import {
  actionLabel,
  copyFor,
  decisionLine,
  format,
  proposalLine,
  scenarioLabel,
  scenarioStory,
} from "./copy.js";

const byId = (id) => document.getElementById(id);

const els = {
  banner: byId("connection-banner"),
  bannerText: byId("connection-text"),
  title: byId("title"),
  story: byId("story"),
  scenariosLabel: byId("scenarios-label"),
  chips: byId("scenario-chips"),
  requestTitle: byId("request-title"),
  requestHint: byId("request-hint"),
  requestText: byId("request-text"),
  requestError: byId("request-error"),
  reset: byId("reset-request"),
  start: byId("start"),
  ossRole: byId("oss-role"),
  ossSide: byId("oss-side"),
  ossState: byId("oss-state"),
  nmsRole: byId("nms-role"),
  nmsSide: byId("nms-side"),
  nmsState: byId("nms-state"),
  cellsRole: byId("cells-role"),
  cellsSide: byId("cells-side"),
  cellsState: byId("cells-state"),
  negotiationLink: byId("negotiation-link"),
  applyLink: byId("apply-link"),
  reportLink: byId("report-link"),
  negotiationName: byId("act-negotiation-name"),
  executionName: byId("act-execution-name"),
  resultName: byId("act-result-name"),
  negotiationMeta: byId("negotiation-meta"),
  executionMeta: byId("execution-meta"),
  resultMeta: byId("result-meta"),
  roundDots: byId("round-dots"),
  negotiationEmpty: byId("negotiation-empty"),
  messages: byId("messages"),
  taskLine: byId("task-line"),
  steps: byId("steps"),
  resultBody: byId("result-body"),
  techSummary: byId("tech-summary"),
  techLog: byId("tech-log"),
  acts: {
    negotiation: byId("act-negotiation"),
    execution: byId("act-execution"),
    result: byId("act-result"),
  },
  bodies: {
    negotiation: byId("act-negotiation-body"),
    execution: byId("act-execution-body"),
    result: byId("act-result-body"),
  },
};

const ui = {
  meta: null,
  copy: copyFor("zh-CN"),
  selected: null,
  view: createState(),
  source: null,
  lastStage: null,
  renderedMessages: 0,
  busy: false,
};

boot();

async function boot() {
  bindActHeads();
  els.start.addEventListener("click", startRun);
  els.reset.addEventListener("click", resetRequest);

  try {
    const response = await fetch("/api/meta");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    ui.meta = await response.json();
  } catch {
    showError(ui.copy.startFailed);
    return;
  }

  ui.copy = copyFor(ui.meta.language);
  ui.selected = ui.meta.defaultScenario || ui.meta.scenarios?.[0] || null;
  applyStaticCopy();
  renderChips();
  els.requestText.value = ui.meta.scenarioRequests?.[ui.selected] || "";
  renderStory();
  resetActs();
  render();
}

function applyStaticCopy() {
  const copy = ui.copy;
  document.documentElement.lang = copy.lang;
  document.title = copy.title;
  els.title.textContent = copy.title;
  els.scenariosLabel.textContent = copy.scenarioLabel;
  els.requestTitle.textContent = copy.requestTitle;
  els.requestHint.textContent = copy.requestHint;
  els.reset.textContent = copy.reset;
  els.bannerText.textContent = copy.connectionLost;
  els.ossRole.textContent = copy.map.ossRole;
  els.ossSide.textContent = copy.map.ossSide;
  els.nmsRole.textContent = copy.map.nmsRole;
  els.nmsSide.textContent = copy.map.nmsSide;
  els.cellsRole.textContent = copy.map.cellsRole;
  els.cellsSide.textContent = copy.map.cellsSide;
  els.negotiationName.textContent = copy.acts.negotiation;
  els.executionName.textContent = copy.acts.execution;
  els.resultName.textContent = copy.acts.result;
  els.negotiationLink.textContent = copy.map.negotiationIdle;
  els.applyLink.textContent = copy.map.applyIdle;
  els.reportLink.textContent = copy.map.report;
  els.techSummary.textContent = copy.tech;
  for (const step of els.steps.querySelectorAll("[data-step]")) {
    step.querySelector(".step-name").textContent = copy.steps[step.dataset.step] || "";
  }
  updateStartLabel();
}

function renderChips() {
  els.chips.replaceChildren();
  for (const name of ui.meta?.scenarios || []) {
    const chip = el("button", "chip", scenarioLabel(ui.copy, name));
    chip.type = "button";
    chip.dataset.scenario = name;
    chip.addEventListener("click", () => selectScenario(name));
    els.chips.append(chip);
  }
  updateChipState();
}

function updateChipState() {
  for (const chip of els.chips.children) {
    chip.disabled = ui.busy;
    chip.setAttribute("aria-pressed", String(chip.dataset.scenario === ui.selected));
  }
}

function selectScenario(name) {
  if (ui.busy) return;
  closeStream();
  ui.selected = name;
  ui.view = createState();
  ui.view.scenario = name;
  ui.lastStage = null;
  els.requestText.value = ui.meta?.scenarioRequests?.[name] || "";
  hideError();
  renderStory();
  resetActs();
  render();
}

function resetRequest() {
  if (ui.busy) return;
  els.requestText.value = ui.meta?.scenarioRequests?.[ui.selected] || "";
  hideError();
}

async function startRun() {
  if (!ui.selected || ui.busy) return;
  hideError();
  closeStream();
  const request = els.requestText.value.trim();

  ui.view = createState();
  ui.view.scenario = ui.selected;
  ui.view.status = STATUS.RUNNING;
  ui.lastStage = null;
  ui.renderedMessages = 0;
  els.messages.replaceChildren();
  els.techLog.replaceChildren();
  setBusy(true);

  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ scenario: ui.selected, userRequest: request || undefined }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    ui.view.runId = payload.runId;
    subscribe(payload.runId);
    render();
  } catch (error) {
    setBusy(false);
    ui.view = createState();
    ui.view.scenario = ui.selected;
    ui.lastStage = null;
    render();
    const detail = error instanceof Error && error.message ? ` (${error.message})` : "";
    showError(`${ui.copy.startFailed}${detail}`);
  }
}

function subscribe(runId) {
  closeStream();
  const source = new EventSource(`/api/runs/${runId}/events`);
  ui.source = source;
  source.onopen = () => hideBanner();
  source.onerror = () => {
    if (ui.source === source && !ui.view.finished) showBanner();
  };
  source.onmessage = (event) => {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch {
      return;
    }
    logFrame(frame);
    applyFrame(ui.view, frame);
    if (frame.type === "finished") {
      closeStream();
      setBusy(false);
    }
    render();
  };
}

function closeStream() {
  if (ui.source !== null) {
    ui.source.close();
    ui.source = null;
  }
}

function setBusy(busy) {
  ui.busy = busy;
  els.requestText.disabled = busy;
  els.reset.disabled = busy;
  els.start.disabled = busy;
  updateChipState();
  updateStartLabel();
}

function updateStartLabel() {
  els.start.textContent = ui.busy ? ui.copy.running : ui.copy.start;
}

function render() {
  const stage = stageOf(ui.view);
  applyStageTransitions(stage);
  renderMap(stage);
  renderNegotiation(stage);
  renderExecution(stage);
  renderResult(stage);
  els.start.disabled = ui.busy;
}

function applyStageTransitions(stage) {
  if (stage === ui.lastStage) return;
  ui.lastStage = stage;
  if (stage === "idle" || stage === "negotiating") {
    showAct("negotiation", true, true);
    showAct("execution", false, true);
    showAct("result", false, true);
    return;
  }
  if (stage === "executing") {
    showAct("negotiation", true, false);
    showAct("execution", true, true);
    showAct("result", false, true);
    return;
  }
  showAct("negotiation", true, false);
  showAct("execution", ui.view.taskCreated, false);
  showAct("result", true, true);
}

function showAct(name, visible, expanded) {
  const act = els.acts[name];
  act.hidden = !visible;
  if (visible) setExpanded(name, expanded);
}

function setExpanded(name, expanded) {
  const act = els.acts[name];
  const head = act.querySelector(".act-head");
  const body = els.bodies[name];
  head.setAttribute("aria-expanded", String(expanded));
  body.hidden = !expanded;
  act.dataset.expanded = String(expanded);
}

function bindActHeads() {
  for (const name of Object.keys(els.acts)) {
    const head = els.acts[name].querySelector(".act-head");
    head.addEventListener("click", () => {
      setExpanded(name, head.getAttribute("aria-expanded") !== "true");
    });
  }
}

function resetActs() {
  ui.renderedMessages = 0;
  els.messages.replaceChildren();
  els.negotiationEmpty.hidden = false;
  els.taskLine.hidden = true;
  els.resultBody.replaceChildren();
  els.roundDots.replaceChildren();
  els.negotiationMeta.textContent = "";
  els.executionMeta.textContent = "";
  els.resultMeta.textContent = "";
  for (const step of els.steps.querySelectorAll("[data-step]")) step.dataset.state = "pending";
  showAct("negotiation", true, true);
  showAct("execution", false, true);
  showAct("result", false, true);
}

function renderStory() {
  els.story.textContent = scenarioStory(ui.copy, ui.selected);
}

function renderMap(stage) {
  const copy = ui.copy;
  const resolution = resolutionOf(ui.view);
  const { round, maxRounds } = roundInfo(ui.view);
  const steps = executionSteps(ui.view);
  const applyDone = steps[1].done;

  switch (stage) {
    case "negotiating":
      setNode("oss", "active", copy.map.ossNegotiating);
      setNode("nms", "active", copy.map.nmsNegotiating);
      setNode("cells", "idle", copy.map.cellsIdle);
      break;
    case "executing":
      setNode("oss", "done", copy.map.ossAgreed);
      setNode("nms", "active", copy.map.nmsExecuting);
      setNode("cells", applyDone ? "done" : "active", applyDone ? copy.map.cellsApplied : copy.map.cellsApplying);
      break;
    case "done":
      setNode("oss", "done", copy.map.ossReported);
      setNode("nms", "done", copy.map.nmsReported);
      setNode("cells", "done", copy.map.cellsApplied);
      break;
    case "ended":
      setNode("oss", "muted", copy.map.ossEnded);
      setNode("nms", resolution === "aborted" ? "failed" : "muted", copy.map.nmsEnded);
      setNode("cells", "idle", copy.map.cellsIdle);
      break;
    case "failed":
      setNode("oss", "muted", copy.map.ossEnded);
      setNode("nms", "failed", copy.map.nmsEnded);
      setNode("cells", "idle", copy.map.cellsIdle);
      break;
    default:
      setNode("oss", "idle", copy.map.idle);
      setNode("nms", "idle", copy.map.idle);
      setNode("cells", "idle", copy.map.idle);
  }

  let negotiationLabel = copy.map.negotiationIdle;
  let negotiationState = "idle";
  if (resolution === "rejected" || resolution === "aborted") {
    negotiationLabel = copy.map.negotiationEnded;
    negotiationState = "muted";
  } else if (stage === "negotiating" || stage === "executing" || stage === "done") {
    if (resolution === "accepted" || stage === "executing" || stage === "done") {
      negotiationLabel = copy.map.negotiationAgreed;
      negotiationState = "done";
    } else if (maxRounds > 0) {
      negotiationLabel = format(copy.map.negotiationWaiting, {
        round: Math.max(round, 1),
        max: maxRounds,
      });
      negotiationState = "active";
    }
  } else if (stage === "ended" || stage === "failed") {
    negotiationLabel = copy.map.negotiationEnded;
    negotiationState = "muted";
  }
  setLink("negotiation", negotiationState, negotiationLabel);
  els.negotiationLink.textContent = negotiationLabel;

  if (stage === "executing" || stage === "done") {
    setLink("apply", applyDone ? "done" : "active", applyDone ? copy.map.applied : copy.map.applying);
    els.applyLink.textContent = applyDone ? copy.map.applied : copy.map.applying;
  } else {
    setLink("apply", "idle", copy.map.applyIdle);
    els.applyLink.textContent = copy.map.applyIdle;
  }

  const reported = stage === "done";
  els.reportLink.parentElement.dataset.state = reported ? "done" : "idle";
}

function setNode(name, state, label) {
  const node = document.querySelector(`[data-node="${name}"]`);
  node.dataset.state = state;
  node.querySelector(".node-state").textContent = label;
}

function setLink(name, state, label) {
  const link = document.querySelector(`[data-link="${name}"]`);
  link.dataset.state = state;
  link.querySelector(".link-label").textContent = label;
}

function renderNegotiation(stage) {
  const { round, maxRounds } = roundInfo(ui.view);
  els.roundDots.replaceChildren();
  for (let index = 1; index <= maxRounds; index += 1) {
    const dot = el("span", "round-dot");
    dot.dataset.filled = String(index <= round);
    els.roundDots.append(dot);
  }

  const resolution = resolutionOf(ui.view);
  if (resolution === "rejected" || resolution === "aborted") {
    els.negotiationMeta.textContent = ui.copy.map.negotiationEnded;
  } else if (stage === "negotiating" || stage === "executing") {
    els.negotiationMeta.textContent = resolution === "accepted"
      ? ui.copy.map.negotiationAgreed
      : maxRounds > 0
        ? ui.copy.round(Math.max(round, 1), maxRounds)
        : "";
  } else if (stage === "done") {
    els.negotiationMeta.textContent = resolution === "accepted" ? ui.copy.map.negotiationAgreed : "";
  } else if (stage === "ended" || stage === "failed") {
    els.negotiationMeta.textContent = ui.copy.map.negotiationEnded;
  } else {
    els.negotiationMeta.textContent = "";
  }

  if (ui.renderedMessages > ui.view.negotiation.length) {
    els.messages.replaceChildren();
    ui.renderedMessages = 0;
  }
  for (let index = ui.renderedMessages; index < ui.view.negotiation.length; index += 1) {
    els.messages.append(messageNode(ui.view.negotiation[index]));
  }
  ui.renderedMessages = ui.view.negotiation.length;

  if (ui.view.negotiation.length === 0) {
    els.negotiationEmpty.hidden = false;
    els.negotiationEmpty.textContent =
      stage === "idle" ? ui.copy.emptyNegotiation : ui.copy.emptyNegotiationRunning;
  } else {
    els.negotiationEmpty.hidden = true;
    scrollMessages();
  }
}

function messageNode(message) {
  const node = el("div", "msg");
  node.dataset.side = message.side;
  node.dataset.action = message.action;
  node.append(
    el("span", "msg-who", ui.copy.who[message.side] || ""),
    el("span", "msg-action", actionLabel(ui.copy, message.action)),
    el("span", "msg-round", message.round ? ui.copy.roundShort(message.round) : ""),
    el("span", "msg-text", messageText(message)),
  );
  return node;
}

function messageText(message) {
  const scenario = ui.view.scenario || ui.selected;
  if (message.action === "propose") {
    return proposalLine(ui.copy, scenario, message.round) || ui.copy.genericProposal;
  }
  if (message.action === "abort") return ui.copy.abortLine;
  if (message.action === "end") return ui.copy.endLine;
  return decisionLine(ui.copy, scenario, message.round) || actionLabel(ui.copy, message.action);
}

function scrollMessages() {
  if (els.bodies.negotiation.hidden) return;
  els.bodies.negotiation.scrollTop = els.bodies.negotiation.scrollHeight;
}

function renderExecution(stage) {
  const steps = executionSteps(ui.view);
  const done = steps.filter((step) => step.done).length;
  els.taskLine.hidden = !ui.view.taskCreated;
  els.taskLine.textContent = ui.copy.taskCreated;

  for (const step of steps) {
    const item = els.steps.querySelector(`[data-step="${step.key}"]`);
    item.dataset.state = step.done ? "done" : step.active ? "active" : "pending";
    item.querySelector(".step-name").textContent = ui.copy.steps[step.key] || "";
  }

  if (stage === "executing") {
    const active = steps.find((step) => step.active);
    els.executionMeta.textContent = active ? ui.copy.steps[active.key] : `${done} / 3`;
  } else if (stage === "done") {
    els.executionMeta.textContent = ui.copy.executionDone;
  } else {
    els.executionMeta.textContent = "";
  }
}

function renderResult(stage) {
  const view = ui.view;
  els.resultBody.replaceChildren();

  if (view.status === STATUS.FAILED) {
    els.resultMeta.textContent = "";
    const card = el("div", "result-card is-failed");
    card.append(el("h3", "result-title", ui.copy.failed.title));
    if (view.error) card.append(el("p", "result-reason", view.error));
    els.resultBody.append(card);
    return;
  }

  if (view.report) {
    const percent = reportPercent(view.report);
    els.resultMeta.textContent = Number.isFinite(percent) ? `${Math.round(percent)}%` : "";
    els.resultBody.append(reportCard(view.report, percent));
    return;
  }

  if (stage === "ended") {
    const resolution = resolutionOf(view);
    els.resultMeta.textContent = ui.copy.notCreated;
    const card = el("div", "result-card is-ended");
    card.append(el("h3", "result-title", ui.copy.ended.title));
    const reason =
      resolution === "rejected"
        ? ui.copy.ended.rejected
        : resolution === "aborted"
          ? ui.copy.ended.aborted
          : ui.copy.ended.generic;
    card.append(el("p", "result-reason", reason));
    const { round, maxRounds } = roundInfo(view);
    if (maxRounds > 0) card.append(el("p", "result-note", ui.copy.round(round, maxRounds)));
    els.resultBody.append(card);
    return;
  }

  if (stage === "done") {
    els.resultMeta.textContent = "";
    const card = el("div", "result-card");
    card.append(el("h3", "result-title", ui.copy.report.title));
    card.append(el("p", "result-reason", ui.copy.report.noNumbers));
    card.append(el("p", "result-note", ui.copy.report.onDuty));
    els.resultBody.append(card);
  }
}

function reportCard(report, percent) {
  const energy = isObject(report.energy) ? report.energy : {};
  const card = el("div", "report-card");
  const head = el("div", "report-head");
  head.append(el("h3", "report-title", ui.copy.report.title));
  if (Number.isFinite(percent)) head.append(el("span", "report-rate", `${Math.round(percent)}%`));
  card.append(head);

  const cell = report.cell_name || report.cell_id || "—";
  const windowLabel = report.window || "—";
  card.append(
    el(
      "p",
      "report-line",
      `${ui.copy.report.cell} ${cell} · ${ui.copy.report.window} ${windowLabel}`,
    ),
  );

  const bar = el("div", "energy-bar");
  const fill = el("span", "energy-fill");
  const clamped = Number.isFinite(percent) ? Math.min(Math.max(percent, 0), 100) : 0;
  fill.style.width = `${clamped}%`;
  bar.append(fill);
  card.append(bar);

  const baseline = numberOrNull(energy.baseline_kwh);
  const remaining = numberOrNull(energy.remaining_kwh);
  const saved = numberOrNull(energy.saved_kwh) ?? numberOrNull(report.estimated_saving_kwh);
  if (baseline !== null || remaining !== null) {
    const legend = el("div", "energy-legend");
    if (baseline !== null) {
      legend.append(el("span", "", `${ui.copy.report.baseline} ${baseline.toFixed(1)} kWh`));
    }
    if (remaining !== null) {
      legend.append(el("span", "", `${ui.copy.report.remaining} ${remaining.toFixed(1)} kWh`));
    }
    card.append(legend);
  }
  if (saved !== null) {
    card.append(el("p", "report-saved", `${ui.copy.report.saved} ≈ ${saved.toFixed(1)} kWh`));
  }
  card.append(el("p", "report-note", ui.copy.report.onDuty));
  return card;
}

function reportPercent(report) {
  const energy = isObject(report.energy) ? report.energy : {};
  return (
    numberOrNull(energy.saving_percent) ??
    numberOrNull(report.target_rate) ??
    numberOrNull(report.estimated_saving_percent) ??
    Number.NaN
  );
}

function logFrame(frame) {
  if (els.techLog.childElementCount > 600) return;
  const line = document.createElement("span");
  line.textContent = JSON.stringify(frame);
  els.techLog.append(line, document.createTextNode("\n"));
}

function showError(message) {
  els.requestError.textContent = message;
  els.requestError.hidden = false;
}

function hideError() {
  els.requestError.hidden = true;
  els.requestError.textContent = "";
}

function showBanner() {
  els.banner.hidden = false;
}

function hideBanner() {
  els.banner.hidden = true;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function numberOrNull(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
