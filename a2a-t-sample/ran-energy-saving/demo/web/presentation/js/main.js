/** Presentation page controller.
 *
 * The page is a stage plus an evidence column. Every stage action is driven by the same SSE
 * frames as the Run Explorer; the player only changes their pacing. Two frames can stop the show
 * for a human: a negotiation decision and a change approval.
 */

import { copyFor, format, scenarioLabel, scenarioStory, scenarioTag } from "../copy.js";
import { applyFrame, createState, pendingHuman, resolutionOf, stageOf } from "./state.js";
import { STEP_KEYS, sceneFor } from "./scenes.js";
import { createPlayer } from "./player.js";
import { createStage, setStageCopy } from "./stage.js";
import { createRail } from "./rail.js";
import { createTrace } from "./trace.js";
import { renderIntent } from "./intent.js";
import { approvalCard, clock, decisionCard, decisionPayload, remainingSeconds } from "./decisions.js";
import { clear, el } from "./dom.js";

const byId = (id) => document.getElementById(id);

const els = {
  banner: byId("connection-banner"),
  bannerText: byId("connection-text"),
  brandKicker: byId("brand-kicker"),
  title: byId("title"),
  badge: byId("badge"),
  scenariosLabel: byId("scenarios-label"),
  chips: byId("scenario-chips"),
  editRequest: byId("edit-request"),
  editLabel: byId("edit-label"),
  drawer: byId("drawer"),
  requestTitle: byId("request-title"),
  requestText: byId("request-text"),
  requestHint: byId("request-hint"),
  requestError: byId("request-error"),
  start: byId("start"),
  reset: byId("reset-request"),
  steps: byId("steps"),
  stage: byId("stage"),
  bubbles: {
    eng: byId("b-eng"),
    oss: byId("b-oss"),
    nms: byId("b-nms"),
    cells: byId("b-cells"),
  },
  actors: {
    eng: byId("a-eng"),
    oss: byId("a-oss"),
    nms: byId("a-nms"),
    cells: byId("a-cells"),
  },
  states: {
    eng: byId("s-eng"),
    oss: byId("s-oss"),
    nms: byId("s-nms"),
    cells: byId("s-cells"),
  },
  nmsBar: byId("nms-bar"),
  approval: byId("a-approval"),
  approvalState: byId("approval-state"),
  cellsFigure: byId("cells-figure"),
  cellsName: byId("cells-name"),
  say: byId("say"),
  sayKicker: byId("say-kicker"),
  sayText: byId("say-text-content"),
  sayDetail: byId("say-detail"),
  choices: byId("choices"),
  pbAuto: byId("pb-auto"),
  pbStep: byId("pb-step"),
  speed: byId("speed"),
  loop: byId("loop"),
  prev: byId("prev"),
  pause: byId("pause"),
  next: byId("next"),
  pausedTag: byId("paused-tag"),
  intentBody: byId("intent-body"),
  intentTitle: byId("intent-title"),
  intentSub: byId("intent-sub"),
  negoTitle: byId("nego-title"),
  negoStatus: byId("nego-status"),
  roundDots: byId("round-dots"),
  tabBusiness: byId("tab-business"),
  tabWire: byId("tab-wire"),
  turns: byId("turns"),
  wire: byId("wire"),
  wireTitle: byId("wire-title"),
  wireCode: byId("wire-code"),
  wireNote: byId("wire-note"),
  negoEmpty: byId("nego-empty"),
  traceTitle: byId("trace-title"),
  traceSub: byId("trace-sub"),
  traceToggle: byId("trace-toggle"),
  traceBody: byId("trace-body"),
  traceRows: byId("trace-rows"),
  traceEmpty: byId("trace-empty"),
  tech: byId("tech"),
  techSummary: byId("tech-summary"),
  techLog: byId("tech-log"),
  belowFlow: byId("below-flow"),
  belowTitle: byId("below-title"),
  belowNote: byId("below-note"),
  showsTitle: byId("shows-title"),
  showsList: byId("shows-list"),
  notShownTitle: byId("not-shown-title"),
  notShownList: byId("not-shown-list"),
  legendChat: byId("legend-chat"),
  legendTask: byId("legend-task"),
  legendStatus: byId("legend-status"),
  legendNeg: byId("legend-neg"),
  legendCfg: byId("legend-cfg"),
  legendData: byId("legend-data"),
  splitter: byId("splitter"),
};

const ui = {
  meta: null,
  copy: copyFor("zh-CN"),
  selected: null,
  view: createState(),
  shadow: createState(),
  frameLog: [],
  seenFrames: new Set(),
  lastScene: null,
  source: null,
  busy: false,
  tab: "business",
  selectedTurn: -1,
  wireEntry: null,
  submitting: false,
  counterOpen: false,
  counterText: "",
  deadline: null,
  countdownTimer: null,
  rendering: false,
  looping: false,
  renderingIntent: false,
};

const stage = createStage(els);
const rail = createRail(
  {
    roundDots: els.roundDots,
    status: els.negoStatus,
    empty: els.negoEmpty,
    turns: els.turns,
    wire: els.wire,
    wireTitle: els.wireTitle,
    wireCode: els.wireCode,
    wireNote: els.wireNote,
  },
  () => ui.copy,
  () => ui,
);
const trace = createTrace(
  { body: els.traceBody, rows: els.traceRows, empty: els.traceEmpty },
  () => ui.copy,
  (entry) => rail.showEntry(entry),
);

const player = createPlayer({
  onApply: (frame) => {
    applyFrame(ui.view, frame);
    render();
  },
  onRewind: (index) => {
    rebuild(index);
  },
  onChange: () => renderControls(),
  onScene: (scene) => {
    ui.lastScene = scene;
    stage.scene(scene, ui.view, ui.copy);
    renderNarrator();
    handleLoop(scene);
  },
});

boot();

async function boot() {
  bindStaticEvents();
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
  els.requestText.value = ui.meta.scenarioRequests?.[ui.selected] || "";
  applyStaticCopy();
  renderPresets();
  renderBottomCopy();
  render();
}

function bindStaticEvents() {
  els.start.addEventListener("click", () => startRun());
  els.reset.addEventListener("click", resetRequest);
  els.editRequest.addEventListener("click", () => {
    const open = els.drawer.hidden;
    els.drawer.hidden = !open;
    els.editRequest.setAttribute("aria-expanded", String(open));
    if (open) els.requestText.focus();
  });
  els.requestText.addEventListener("input", () => {
    hideError();
    if (!ui.busy && ui.view.status === "idle") renderIntentPanel();
  });
  els.requestText.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      startRun();
    }
  });
  els.pbAuto.addEventListener("change", () => player.setMode("auto"));
  els.pbStep.addEventListener("change", () => player.setMode("step"));
  els.speed.addEventListener("change", () => player.setSpeed(els.speed.value));
  els.loop.addEventListener("change", () => {
    if (els.loop.checked && !ui.busy && ui.view.status === "idle") startRun();
  });
  els.next.addEventListener("click", onNext);
  els.prev.addEventListener("click", () => player.previous());
  els.pause.addEventListener("click", () => {
    const status = player.status();
    if (status.paused) player.resume();
    else player.pause();
  });
  els.tabBusiness.addEventListener("click", () => setTab("business"));
  els.tabWire.addEventListener("click", () => setTab("wire"));
  els.traceToggle.addEventListener("click", () => {
    const open = els.traceToggle.getAttribute("aria-expanded") === "true";
    els.traceToggle.setAttribute("aria-expanded", String(!open));
    els.traceBody.hidden = open;
    els.rowsHidden = open;
  });
  bindSplitter();
  document.addEventListener("keydown", (event) => {
    if (event.target.closest && event.target.closest("textarea,select,input")) return;
    if (event.key === "ArrowRight" && !els.next.disabled && !els.next.hidden) {
      event.preventDefault();
      els.next.click();
    }
    if (event.key === "ArrowLeft" && !els.prev.disabled) {
      event.preventDefault();
      els.prev.click();
    }
    if (event.key === " " && !(event.target.closest && event.target.closest("button,label"))) {
      event.preventDefault();
      els.pause.click();
    }
  });
}

function onNext() {
  const status = player.status();
  if (status.waitingNext) {
    player.next();
    return;
  }
  if (!ui.busy && ui.view.status === "idle") startRun();
}

function setTab(tab) {
  ui.tab = tab;
  els.tabBusiness.setAttribute("aria-pressed", String(tab === "business"));
  els.tabWire.setAttribute("aria-pressed", String(tab === "wire"));
  rail.render(ui.view);
}

function applyStaticCopy() {
  const copy = ui.copy;
  document.documentElement.lang = copy.lang;
  document.title = copy.title;
  els.brandKicker.textContent = copy.kicker;
  els.title.textContent = copy.title;
  els.badge.textContent = copy.badge;
  els.scenariosLabel.textContent = copy.scenarioLabel;
  els.editLabel.textContent = copy.editRequest;
  els.requestTitle.textContent = copy.requestTitle;
  els.requestHint.textContent = copy.requestHint;
  els.reset.textContent = copy.reset;
  els.start.textContent = copy.start;
  els.bannerText.textContent = copy.connectionLost;
  els.intentTitle.textContent = copy.intent.title;
  els.intentSub.textContent = copy.intent.sub;
  els.negoTitle.textContent = copy.negotiation.title;
  els.traceTitle.textContent = copy.trace.title;
  els.traceSub.textContent = copy.trace.sub;
  els.techSummary.textContent = copy.tech;
  els.legendChat.textContent = copy.legend.chat;
  els.legendTask.textContent = copy.legend.task;
  els.legendStatus.textContent = copy.legend.status;
  els.legendNeg.textContent = copy.legend.neg;
  els.legendCfg.textContent = copy.legend.cfg;
  els.legendData.textContent = copy.legend.data;
  setStageCopy(copy);
  const cellsName = byId("cells-name");
  if (cellsName) cellsName.textContent = copy.roles.cells;
  const skills = byId("oss-skills");
  if (skills) {
    skills.replaceChildren();
    for (const skill of copy.roles.ossSkills) skills.append(el("span", "", skill));
  }
}

function renderBottomCopy() {
  const copy = ui.copy;
  els.belowTitle.textContent = copy.below.flowTitle;
  els.belowFlow.replaceChildren();
  for (const card of copy.below.flow) {
    const node = el("article", "below-card");
    node.append(
      el("span", "when", card.when),
      el("h3", "", card.title),
      el("p", "", card.body),
    );
    els.belowFlow.append(node);
  }
  els.showsTitle.textContent = copy.below.showsTitle;
  els.notShownTitle.textContent = copy.below.notShownTitle;
  clear(els.showsList);
  clear(els.notShownList);
  for (const item of copy.below.shows) els.showsList.append(el("li", "", item));
  for (const item of copy.below.notShown) els.notShownList.append(el("li", "", item));
  els.belowNote.textContent = copy.below.note;
}

function renderPresets() {
  els.chips.replaceChildren();
  for (const name of ui.meta?.scenarios || []) {
    const button = el("button", "preset");
    button.type = "button";
    button.dataset.scenario = name;
    button.setAttribute("aria-pressed", String(name === ui.selected));
    button.title = scenarioStory(ui.copy, name);
    const nameRow = el("span", "nm");
    const mark = el("i", scenarioTag(ui.copy, name) === "ending" ? "end" : "neg");
    nameRow.append(mark, el("span", "", scenarioLabel(ui.copy, name)));
    button.append(nameRow, el("small", "", scenarioTag(ui.copy, name)));
    button.addEventListener("click", () => {
      if (ui.busy) return;
      selectScenario(name, { start: true });
    });
    els.chips.append(button);
  }
}

function selectScenario(name, { start = false } = {}) {
  closeStream();
  player.reset();
  ui.selected = name;
  ui.view = createState();
  ui.shadow = createState();
  ui.frameLog = [];
  ui.seenFrames = new Set();
  ui.lastScene = null;
  ui.tab = "business";
  ui.selectedTurn = -1;
  ui.wireEntry = null;
  ui.counterOpen = false;
  ui.looping = false;
  els.requestText.value = ui.meta?.scenarioRequests?.[name] || "";
  stage.reset();
  hideError();
  for (const chip of els.chips.children) {
    chip.setAttribute("aria-pressed", String(chip.dataset.scenario === name));
  }
  render();
  if (start) startRun();
}

function resetRequest() {
  if (ui.busy) return;
  els.requestText.value = ui.meta?.scenarioRequests?.[ui.selected] || "";
  hideError();
  renderIntentPanel();
}

async function startRun() {
  if (ui.busy) {
    showError(ui.copy.running);
    return;
  }
  const request = els.requestText.value.trim();
  hideError();
  closeStream();
  player.reset();
  ui.view = createState();
  ui.shadow = createState();
  ui.frameLog = [];
  ui.seenFrames = new Set();
  ui.lastScene = null;
  ui.tab = "business";
  ui.selectedTurn = -1;
  ui.wireEntry = null;
  ui.counterOpen = false;
  ui.counterText = "";
  ui.submitting = false;
  ui.deadline = null;
  ui.looping = false;
  stage.reset();
  clear(els.techLog);
  setBusy(true);
  render();

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
    render();
    const detail = error instanceof Error && error.message ? ` (${error.message})` : "";
    showError(`${ui.copy.startFailed}${detail}`);
  }
}

function subscribe(runId) {
  closeStream();
  const source = new EventSource(`/api/runs/${runId}/events`);
  ui.source = source;
  source.onopen = hideBanner;
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
    // An approval trace span carries the plan details and may arrive after the status frame that
    // stopped the show. Apply it immediately so the card is never stuck on a generic message.
    if (frame.type === "trace" && isApprovalTrace(frame.event) && player.status().hold) {
      applyFrame(ui.view, frame);
      applyFrame(ui.shadow, frame);
      render();
    }
    enqueueFrame(frame);
    if (frame.type === "finished") {
      closeStream();
      setBusy(false);
    }
  };
}

function enqueueFrame(frame) {
  const key = frameKey(frame);
  if (key !== null) {
    if (ui.seenFrames.has(key)) return;
    ui.seenFrames.add(key);
  }
  const scene = sceneFor(frame, ui.shadow) || {
    key: "trace",
    step: ui.lastScene ? ui.lastScene.step : 0,
    duration: 0,
    hold: false,
    frame,
  };
  applyFrame(ui.shadow, frame);
  ui.frameLog.push(frame);
  player.enqueue(scene);
}

function frameKey(frame) {
  if (!frame || typeof frame !== "object") return null;
  if (frame.type === "event" || frame.type === "trace") {
    const entry = frame.event || {};
    if (Number.isInteger(entry.seq)) return `${frame.type}:${entry.seq}`;
    if (typeof entry.spanId === "string") return `${frame.type}:${entry.spanId}`;
    return null;
  }
  if (frame.type === "decision_requested" || frame.type === "decision_recommended") {
    return `${frame.type}:${frame.round ?? "?"}:${frame.requestedAt ?? ""}`;
  }
  if (frame.type === "finished") return "finished";
  return frame.type || null;
}

function rebuild(index) {
  const view = createState();
  view.scenario = ui.selected;
  for (let cursor = 0; cursor < index && cursor < ui.frameLog.length; cursor += 1) {
    applyFrame(view, ui.frameLog[cursor]);
  }
  ui.view = view;
  ui.lastScene = index > 0 ? player.sceneAt(index - 1) : null;
  render();
}

function closeStream() {
  if (ui.source !== null) {
    ui.source.close();
    ui.source = null;
  }
}

function setBusy(busy) {
  ui.busy = busy;
  els.start.disabled = busy;
  els.reset.disabled = busy;
  els.requestText.disabled = busy;
  for (const chip of els.chips.children) chip.disabled = busy;
  renderControls();
}

function render() {
  if (ui.rendering) return;
  ui.rendering = true;
  try {
    renderSteps();
    stage.render(ui.view, ui.copy);
    renderIntentPanel();
    rail.render(ui.view);
    trace.render(ui.view);
    renderNarrator();
    renderControls();
  } finally {
    ui.rendering = false;
  }
}

function renderIntentPanel() {
  if (ui.renderingIntent) return;
  ui.renderingIntent = true;
  try {
    const text = ui.view.intentText || els.requestText.value;
    renderIntent(els.intentBody, text, ui.copy);
  } finally {
    ui.renderingIntent = false;
  }
}

function renderSteps() {
  if (ui.steps === undefined) {
    els.steps.replaceChildren();
    for (const key of STEP_KEYS) {
      const item = el("li");
      const button = el("button");
      button.type = "button";
      button.dataset.step = key;
      button.append(
        el("b", "", String(STEP_KEYS.indexOf(key) + 1)),
        el("span", "", ui.copy.steps[key] || key),
        el("em", "", ui.copy.stepWhen[key] || ""),
      );
      button.addEventListener("click", () => {
        const index = STEP_KEYS.indexOf(key);
        if (index >= 0 && player.jumpToStep(index)) player.pause();
      });
      item.append(button);
      els.steps.append(item);
    }
    ui.steps = [...els.steps.children];
  }
  const current = ui.lastScene ? ui.lastScene.step : -1;
  const pending = pendingHuman(ui.view);
  const stage = stageOf(ui.view);
  const negotiationSeen = ui.view.negotiation.length > 0;
  const approvalSeen = ui.view.approval !== null || ui.view.approvalState !== null;
  const finished = ui.view.finished;

  ui.steps.forEach((item, index) => {
    const key = STEP_KEYS[index];
    let state = "pending";
    if (finished) {
      if (stage === "done") state = "done";
      else if (stage === "notApplied") state = index <= 3 ? "done" : index === 4 ? "done" : "off";
      else if (stage === "failed") state = index <= current ? "done" : "off";
      else state = index <= 3 ? "done" : "off";
      if (stage === "notApplied" && index === 5) state = "ask";
    } else if (index < current) {
      state = "done";
    } else if (index === current) {
      state = pending ? "ask" : "now";
    }
    if (key === "negotiate" && !negotiationSeen && (stage === "executing" || stage === "done")) {
      state = "skip";
    }
    if (key === "approval" && ui.meta && ui.meta.approvalGate === false) state = "skip";
    if (key === "approval" && !approvalSeen && stage === "executing" && ui.view.executionArtifacts >= 3) {
      state = "skip";
    }
    if (key === "approval" && stage === "done" && !approvalSeen) state = "skip";
    item.className = state;
    const button = item.querySelector("button");
    if (state === "skip" || state === "off") {
      button.disabled = true;
      button.querySelector("em").textContent = state === "skip" ? ui.copy.skip : ui.copy.off;
    } else {
      button.disabled = false;
    }
  });
}

function renderNarrator() {
  const card = pendingCard();
  if (card) {
    els.say.classList.add("ask");
    els.sayKicker.textContent = card.kicker;
    els.sayText.textContent = card.title;
    els.sayDetail.hidden = false;
    els.sayDetail.replaceChildren();
    els.sayDetail.append(el("span", "", card.text));
    if (card.detail) els.sayDetail.append(el("span", "reason", card.detail));
    if (card.remaining !== null) {
      const line = el("span", "reason");
      line.append(el("span", "", `${copyRemaining(card)} `));
      const value = el("b");
      value.dataset.countdown = "true";
      value.textContent = clock(card.remaining);
      line.append(value);
      els.sayDetail.append(line);
      startCountdown(card.remaining);
    } else {
      stopCountdown();
    }
    renderChoices(card);
    return;
  }

  stopCountdown();
  els.say.classList.remove("ask");
  els.choices.hidden = true;
  clear(els.choices);
  ui.counterOpen = false;
  els.sayDetail.hidden = true;
  els.sayDetail.replaceChildren();

  const scene = ui.lastScene;
  if (scene === null) {
    els.sayKicker.textContent = ui.copy.ready;
    els.sayText.textContent = ui.copy.readyHint;
  } else {
    const stepKey = STEP_KEYS[scene.step] || "request";
    els.sayKicker.textContent = `Step ${scene.step + 1} · ${ui.copy.steps[stepKey]}`;
    els.sayText.textContent = narration(scene, stepKey);
  }
  renderResult();

  if (player.status().hold && pendingHuman(ui.view) === null) player.release();
}

function pendingCard() {
  return decisionCard(ui.view, ui.copy) || approvalCard(ui.view, ui.copy);
}

function renderChoices(card) {
  els.choices.hidden = false;
  els.choices.replaceChildren();
  if (ui.counterOpen) {
    const editor = el("div", "counter-editor");
    const area = el("textarea");
    area.value = ui.counterText;
    area.setAttribute("aria-label", ui.copy.decision.counterLabel);
    area.placeholder = ui.copy.decision.counterPlaceholder;
    area.addEventListener("input", () => {
      ui.counterText = area.value;
    });
    const row = el("div", "choices");
    const send = el("button", "primary", ui.copy.decision.confirm);
    send.type = "button";
    send.addEventListener("click", () => submitDecision("counter", ui.counterText));
    const cancel = el("button", "", ui.copy.decision.cancel);
    cancel.type = "button";
    cancel.addEventListener("click", () => {
      ui.counterOpen = false;
      renderNarrator();
    });
    row.append(send, cancel);
    editor.append(area, row);
    els.choices.append(editor);
    area.focus();
    return;
  }

  for (const option of card.options) {
    const button = el("button", option.primary ? "primary" : option.danger ? "danger" : "", option.label);
    button.type = "button";
    button.disabled = ui.submitting;
    button.addEventListener("click", () => {
      if (card.type === "approval") {
        submitApproval(option.action === "approve");
        return;
      }
      if (option.action === "counter") {
        ui.counterOpen = true;
        ui.counterText = card.counterText || "";
        renderNarrator();
        return;
      }
      submitDecision(option.action);
    });
    els.choices.append(button);
  }
}

async function submitDecision(action, counterText = "") {
  if (ui.submitting) return;
  const card = decisionCard(ui.view, ui.copy);
  if (card === null) return;
  ui.submitting = true;
  renderChoices(card);
  const payload = decisionPayload(card, action, ui.copy, counterText);
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(ui.view.runId)}/decisions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.error || `HTTP ${response.status}`);
    }
    ui.submitting = false;
    ui.counterOpen = false;
    if (ui.view.decision !== null) ui.view.decision.resolved = true;
    player.release();
    render();
  } catch (error) {
    ui.submitting = false;
    const message = error instanceof Error ? error.message : String(error);
    els.sayDetail.append(el("span", "error", `${ui.copy.decision.submitFailed} ${message}`));
    renderChoices(card);
  }
}

async function submitApproval(approved) {
  if (ui.submitting) return;
  ui.submitting = true;
  const card = approvalCard(ui.view, ui.copy);
  if (card) renderChoices(card);
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(ui.view.runId)}/approvals`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ approved }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.error || `HTTP ${response.status}`);
    }
    ui.submitting = false;
    ui.view.approvalState = approved ? "approved" : "rejected";
    player.release();
    render();
  } catch (error) {
    ui.submitting = false;
    const message = error instanceof Error ? error.message : String(error);
    els.sayDetail.append(el("span", "error", `${ui.copy.approval.submitFailed} ${message}`));
    if (card) renderChoices(card);
  }
}

function renderControls() {
  const status = player.status();
  const idle = ui.view.status === "idle" && ui.lastScene === null;
  const finished = ui.view.finished && status.atEnd;
  els.next.hidden = !(idle || status.waitingNext || status.mode === "step");
  els.next.disabled = !(idle ? !ui.busy : status.waitingNext);
  els.next.textContent = idle ? "Start ▶" : "Next step ▶";
  els.next.classList.toggle("next", true);
  els.prev.disabled = status.playhead <= 1;
  els.pause.disabled = status.scenes === 0 || finished;
  els.pause.textContent = status.paused ? "▶ Resume" : "❚❚ Pause";
  els.pause.classList.toggle("on", status.paused);
  els.pbAuto.checked = status.mode === "auto";
  els.pbStep.checked = status.mode === "step";
  els.pausedTag.hidden = !status.paused;
  els.stage.classList.toggle("paused", status.paused);
  if (ui.view.status !== "idle" && !ui.busy) {
    els.start.textContent = ui.copy.start;
  } else if (ui.busy) {
    els.start.textContent = ui.copy.running;
  }
}

function renderResult() {
  if (!ui.view.finished) return;
  const stage = stageOf(ui.view);
  const card = el("div", "result-card");
  if (ui.view.report) {
    card.append(reportCard(ui.view.report, ui.copy));
  } else if (stage === "notApplied") {
    card.append(
      el("h3", "result-title", ui.copy.result.notAppliedTitle),
      el("p", "result-reason", ui.copy.result.notApplied),
    );
  } else if (stage === "ended") {
    const resolution = resolutionOf(ui.view);
    const reason =
      resolution === "aborted" ? ui.copy.result.aborted : ui.copy.result.rejected;
    card.append(
      el("h3", "result-title", ui.copy.result.endedTitle),
      el("p", "result-reason", reason),
    );
  } else if (stage === "failed") {
    card.append(
      el("h3", "result-title", ui.copy.result.failedTitle),
      el("p", "result-reason", ui.view.error || ""),
    );
  } else {
    return;
  }
  els.sayDetail.hidden = false;
  els.sayDetail.append(card);
}

function reportCard(report, copy) {
  const wrapper = el("div", "report-card");
  const percent = reportPercent(report);
  const head = el("div", "report-head");
  head.append(el("h3", "result-title", copy.result.report));
  if (percent !== null) head.append(el("span", "report-rate", `${Math.round(percent)}%`));
  wrapper.append(head);
  const cell = report.cell_name || report.cell_id || "—";
  const windowLabel = report.window || "—";
  wrapper.append(el("p", "report-line", `${copy.result.cell} ${cell} · ${copy.result.window} ${windowLabel}`));
  if (percent !== null) {
    const bar = el("div", "energy-bar");
    const fill = el("span", "energy-fill");
    fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    bar.append(fill);
    wrapper.append(bar);
  }
  const energy = report.energy && typeof report.energy === "object" ? report.energy : {};
  const baseline = numberOrNull(energy.baseline_kwh);
  const remaining = numberOrNull(energy.remaining_kwh);
  const saved = numberOrNull(energy.saved_kwh) ?? numberOrNull(report.estimated_saving_kwh);
  if (baseline !== null || remaining !== null) {
    const legend = el("div", "energy-legend");
    if (baseline !== null) legend.append(el("span", "", `${copy.result.baseline} ${baseline.toFixed(1)} kWh`));
    if (remaining !== null) legend.append(el("span", "", `${copy.result.remaining} ${remaining.toFixed(1)} kWh`));
    wrapper.append(legend);
  }
  if (saved !== null) {
    wrapper.append(el("p", "report-saved", `${copy.result.saved} ≈ ${saved.toFixed(1)} kWh`));
  }
  wrapper.append(el("p", "report-note", copy.result.onDuty));
  return wrapper;
}

function reportPercent(report) {
  const energy = report.energy && typeof report.energy === "object" ? report.energy : {};
  return (
    numberOrNull(energy.saving_percent) ??
    numberOrNull(report.target_rate) ??
    numberOrNull(report.estimated_saving_percent)
  );
}

function narration(scene, stepKey) {
  return ui.copy.narration[scene.key] || ui.copy.narration[stepKey] || scenarioStory(ui.copy, ui.selected);
}

function copyRemaining(card) {
  return card.type === "approval" ? ui.copy.approval.remaining : ui.copy.decision.remaining;
}

function startCountdown(remaining) {
  stopCountdown();
  ui.deadline = Date.now() + remaining * 1000;
  ui.countdownTimer = window.setInterval(() => {
    const node = els.sayDetail.querySelector("[data-countdown]");
    if (!node) return;
    const left = Math.max(0, Math.round((ui.deadline - Date.now()) / 1000));
    node.textContent = clock(left);
    if (left === 0) {
      const card = pendingCard();
      if (card) node.textContent = clock(0);
      stopCountdown();
    }
  }, 1000);
}

function stopCountdown() {
  if (ui.countdownTimer !== null) {
    window.clearInterval(ui.countdownTimer);
    ui.countdownTimer = null;
  }
  ui.deadline = null;
}

function handleLoop(scene) {
  if (scene.key !== "finish" || !els.loop.checked || ui.looping) return;
  ui.looping = true;
  window.setTimeout(() => {
    if (!els.loop.checked) {
      ui.looping = false;
      return;
    }
    const names = ui.meta?.scenarios || [];
    const index = names.indexOf(ui.selected);
    const next = names[(index + 1) % names.length] || ui.selected;
    selectScenario(next, { start: true });
  }, 6000);
}

function isApprovalTrace(span) {
  return Boolean(span && typeof span === "object" && span.kind === "approval");
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
  els.drawer.hidden = false;
  els.editRequest.setAttribute("aria-expanded", "true");
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

function numberOrNull(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function bindSplitter() {
  const splitter = els.splitter;
  const main = document.querySelector(".main");
  const key = "a2at-presentation-right-px";
  const setWidth = (px, save) => {
    const rect = main.getBoundingClientRect();
    const value = Math.max(300, Math.min(rect.width * 0.6, px));
    main.style.setProperty("--rw", `${value.toFixed(0)}px`);
    if (save) {
      try {
        localStorage.setItem(key, String(value));
      } catch {
        /* storage is optional */
      }
    }
  };
  try {
    const saved = Number(localStorage.getItem(key));
    if (Number.isFinite(saved) && saved >= 300 && saved <= 900) {
      main.style.setProperty("--rw", `${saved}px`);
    }
  } catch {
    /* storage is optional */
  }
  let dragging = false;
  const fromPointer = (x) => {
    const rect = main.getBoundingClientRect();
    const padding = parseFloat(getComputedStyle(main).paddingRight) || 0;
    return rect.right - padding - x - 9;
  };
  splitter.addEventListener("pointerdown", (event) => {
    dragging = true;
    try {
      splitter.setPointerCapture(event.pointerId);
    } catch {
      /* pointer capture is best effort */
    }
    splitter.classList.add("drag");
    document.body.classList.add("resizing");
    event.preventDefault();
  });
  splitter.addEventListener("pointermove", (event) => {
    if (dragging) setWidth(fromPointer(event.clientX), false);
  });
  const end = (event) => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("drag");
    document.body.classList.remove("resizing");
    setWidth(fromPointer(event.clientX), true);
  };
  splitter.addEventListener("pointerup", end);
  splitter.addEventListener("pointercancel", end);
  splitter.addEventListener("dblclick", () => {
    main.style.removeProperty("--rw");
    try {
      localStorage.removeItem(key);
    } catch {
      /* storage is optional */
    }
  });
  splitter.addEventListener("keydown", (event) => {
    const current = document.querySelector(".right").getBoundingClientRect().width;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setWidth(current + 30, true);
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      setWidth(current - 30, true);
    }
  });
}

// Debug handle for the demo operator console and the headless smoke test.
if (typeof window !== "undefined") {
  window.__presentation = { ui, player };
}
