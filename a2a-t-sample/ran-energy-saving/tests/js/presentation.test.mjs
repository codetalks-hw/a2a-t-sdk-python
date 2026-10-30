// Presentation view-model checks (no DOM, no network).
//
// Run with: node tests/js/presentation.test.mjs
// The Python suite shells out to this file and skips cleanly when Node is absent.

import assert from "node:assert/strict";

import {
  applyFrame,
  createState,
  executionSteps,
  pendingHuman,
  resolutionOf,
  roundInfo,
  stageOf,
} from "../../demo/web/presentation/js/state.js";
import { sceneFor } from "../../demo/web/presentation/js/scenes.js";
import { createPlayer } from "../../demo/web/presentation/js/player.js";
import { parseIntent, plainSentence } from "../../demo/web/presentation/js/intent.js";
import {
  approvalCard,
  decisionCard,
  decisionPayload,
  intentCard,
  remainingSeconds,
} from "../../demo/web/presentation/js/decisions.js";
import { copyFor } from "../../demo/web/presentation/copy.js";

const copy = copyFor("zh-CN");
const event = (payload) => ({ type: "event", event: payload });

function proposal(seq, round, maxRounds = 3) {
  return event({
    seq,
    entryType: "event",
    kind: "propose",
    phase: "negotiation",
    round,
    maxRounds,
    performative: "PROPOSE",
    content: `full proposal ${round}`,
  });
}

function decision(seq, action, round, maxRounds = 3, detail = null) {
  return event({
    seq,
    entryType: "decision",
    kind: action,
    phase: "negotiation",
    round,
    maxRounds,
    performative: action.toUpperCase(),
    detail,
  });
}

function status(seq, summary) {
  return event({ seq, entryType: "event", kind: "status", phase: "execution", summary });
}

function artifact(seq, phase) {
  return event({ seq, entryType: "event", kind: "artifact", phase });
}

function finished(finalState, report = null, error = null) {
  return { type: "finished", status: error ? "failed" : "finished", finalState, report, error };
}

// --- Accepted negotiation with a task and a report ----------------------

const accepted = createState();
assert.equal(stageOf(accepted), "idle");

applyFrame(accepted, { type: "run_started", runId: "r1", scenario: "target", mode: "auto" });
assert.equal(accepted.scenario, "target");
assert.equal(accepted.mode, "auto");
assert.equal(stageOf(accepted), "negotiating");

applyFrame(accepted, proposal(1, 1));
assert.deepEqual(roundInfo(accepted), { round: 1, maxRounds: 3 });
assert.equal(accepted.negotiation[0].content, "full proposal 1");

// The same frame replayed after a reconnect must not duplicate the row.
applyFrame(accepted, proposal(1, 1));
assert.equal(accepted.negotiation.length, 1);

applyFrame(accepted, decision(2, "counter", 1, 3, { counterParams: { window: "00:00~06:00" } }));
assert.deepEqual(accepted.negotiation[1].detail.counterParams, { window: "00:00~06:00" });
applyFrame(accepted, proposal(3, 2));
applyFrame(accepted, decision(4, "accept", 2));
assert.equal(resolutionOf(accepted), "accepted");
assert.equal(stageOf(accepted), "negotiating");

applyFrame(accepted, status(5, "TASK_STATE_SUBMITTED"));
assert.equal(stageOf(accepted), "executing");
assert.equal(accepted.taskCreated, true);

assert.deepEqual(
  executionSteps(accepted).map((step) => step.done),
  [false, false, false],
);

applyFrame(accepted, artifact(6, "execution"));
assert.deepEqual(
  executionSteps(accepted).map((step) => step.done),
  [false, false, false],
);
applyFrame(accepted, artifact(7, "execution"));
assert.deepEqual(
  executionSteps(accepted).map((step) => step.done),
  [true, false, false],
);
applyFrame(accepted, artifact(8, "execution"));
assert.deepEqual(
  executionSteps(accepted).map((step) => step.done),
  [true, true, false],
);

applyFrame(accepted, artifact(9, "report"));
assert.deepEqual(
  executionSteps(accepted).map((step) => step.done),
  [true, true, true],
);

applyFrame(
  accepted,
  finished("task_completed", { energy: { saving_percent: 30 }, cell_name: "Cell 1" }),
);
assert.equal(stageOf(accepted), "done");
assert.equal(accepted.report.energy.saving_percent, 30);

// --- Rejected negotiation: no task --------------------------------------

const rejected = createState();
applyFrame(rejected, { type: "run_started", runId: "r2", scenario: "reject", mode: "auto" });
applyFrame(rejected, proposal(1, 1));
applyFrame(rejected, decision(2, "reject", 1));
applyFrame(
  rejected,
  event({ seq: 3, entryType: "event", kind: "message", phase: "negotiation" }),
);
applyFrame(rejected, finished("no_task_negotiation_ended"));
assert.equal(resolutionOf(rejected), "rejected");
assert.equal(stageOf(rejected), "ended");
assert.equal(rejected.taskCreated, false);

// --- Aborted negotiation: the NMS abort wins over the last counter ------

const aborted = createState();
applyFrame(aborted, { type: "run_started", runId: "r3", scenario: "abort", mode: "auto" });
applyFrame(aborted, proposal(1, 1));
applyFrame(aborted, decision(2, "counter", 1));
applyFrame(aborted, proposal(3, 2));
applyFrame(aborted, decision(4, "counter", 2));
applyFrame(aborted, proposal(5, 3));
applyFrame(aborted, decision(6, "counter", 3));
applyFrame(
  aborted,
  event({ seq: 7, entryType: "event", kind: "abort", phase: "negotiation", round: 3 }),
);
applyFrame(aborted, finished("no_task_negotiation_aborted"));
assert.equal(resolutionOf(aborted), "aborted");
assert.equal(stageOf(aborted), "ended");
assert.deepEqual(roundInfo(aborted), { round: 3, maxRounds: 3 });

// --- A failed run keeps its error --------------------------------------

const failed = createState();
applyFrame(failed, { type: "run_started", runId: "r4", scenario: "information", mode: "auto" });
applyFrame(failed, finished("incomplete", null, "server unavailable"));
assert.equal(stageOf(failed), "failed");
assert.equal(failed.error, "server unavailable");

// --- Confirm mode: the decision frame carries the recommendation --------

const confirming = createState();
applyFrame(confirming, { type: "run_started", runId: "r5", scenario: "target", mode: "confirm" });
applyFrame(confirming, proposal(1, 1));
applyFrame(confirming, {
  type: "decision_requested",
  round: 1,
  maxRounds: 3,
  proposal: { kind: "target", round: 1 },
  recommendation: {
    action: "counter",
    rationale: "overlap",
    counter_proposal: "keep 17:30~20:00",
  },
  timeoutSeconds: 300,
  requestedAt: new Date().toISOString(),
});
assert.equal(pendingHuman(confirming), "decision");
assert.equal(confirming.decision.recommendation.action, "counter");
const card = decisionCard(confirming, copy);
assert.ok(card);
assert.ok(card.options.some((option) => option.action === "confirm"));
assert.equal(decisionPayload(card, "confirm", copy).action, "counter");
assert.equal(decisionPayload(card, "counter", copy, "new text").counter_proposal, "new text");
assert.equal(decisionPayload(card, "reject", copy).action, "reject");

// In confirm mode a decision without a recommendation is not offered yet.
const thinking = createState();
applyFrame(thinking, { type: "run_started", runId: "r6", scenario: "target", mode: "confirm" });
applyFrame(thinking, {
  type: "decision_requested",
  round: 1,
  maxRounds: 3,
  proposal: { kind: "target" },
});
assert.equal(decisionCard(thinking, copy), null);

applyFrame(confirming, decision(2, "counter", 1));
assert.equal(pendingHuman(confirming), null);

// --- Approval 1: intent review ------------------------------------------

const intentReview = createState();
applyFrame(intentReview, { type: "run_started", runId: "r9", scenario: "night", mode: "confirm" });
applyFrame(intentReview, {
  type: "intent_review_requested",
  promptText: "## Operation Type\nCreate",
  timeoutSeconds: 300,
  requestedAt: new Date().toISOString(),
});
assert.equal(pendingHuman(intentReview), "intent");
const intentReviewCard = intentCard(intentReview, copy);
assert.ok(intentReviewCard);
assert.ok(intentReviewCard.options.some((option) => option.action === "approve"));
intentReview.intentReview.resolved = true;
assert.equal(pendingHuman(intentReview), null);
applyFrame(intentReview, finished("intent_not_confirmed"));
assert.equal(stageOf(intentReview), "notSent");

// --- Approval gate ------------------------------------------------------

const approval = createState();
applyFrame(approval, { type: "run_started", runId: "r7", scenario: "information", mode: "confirm" });
applyFrame(approval, status(1, "TASK_STATE_SUBMITTED"));
applyFrame(approval, status(2, "TASK_STATE_WORKING"));
applyFrame(approval, status(3, "TASK_STATE_INPUT_REQUIRED"));
assert.equal(approval.approvalState, "waiting");
assert.equal(pendingHuman(approval), "approval");
const approvalModel = approvalCard(approval, copy);
assert.ok(approvalModel);
assert.ok(approvalModel.options.some((option) => option.action === "export"));
approval.approvalExported = true;
const exported = approvalCard(approval, copy);
assert.ok(exported.options.some((option) => option.action === "approve"));
assert.ok(exported.mml.text.includes("SET ENERGYSAVING"));

applyFrame(approval, {
  type: "trace",
  event: {
    seq: 4,
    kind: "approval",
    title: "Approval requested",
    startedAt: new Date().toISOString(),
    detail: {
      cellId: "cell-001",
      window: "00:00~06:00",
      rate: "30%",
      approvalState: "requested",
      timeoutSeconds: 120,
    },
  },
});
assert.equal(approval.approval.cellId, "cell-001");
assert.equal(approvalCard(approval, copy).text.includes("cell-001"), true);

applyFrame(approval, status(5, "TASK_STATE_WORKING"));
assert.equal(approval.approvalState, "approved");
assert.equal(pendingHuman(approval), null);

const notApplied = createState();
applyFrame(notApplied, { type: "run_started", runId: "r8", scenario: "information", mode: "confirm" });
applyFrame(notApplied, status(1, "TASK_STATE_SUBMITTED"));
applyFrame(notApplied, {
  type: "trace",
  event: {
    seq: 2,
    kind: "approval",
    title: "Approval requested",
    detail: { cellId: "cell-001", approvalState: "requested" },
  },
});
applyFrame(notApplied, status(3, "TASK_STATE_REJECTED"));
applyFrame(notApplied, finished("task_rejected"));
assert.equal(stageOf(notApplied), "notApplied");

// --- Scene mapping ------------------------------------------------------

const sceneState = createState();
applyFrame(sceneState, { type: "run_started", runId: "s1", scenario: "target", mode: "confirm" });
assert.equal(sceneFor({ type: "decision_requested", round: 1 }, sceneState).hold, true);
assert.equal(
  sceneFor({ type: "event", event: { kind: "status", summary: "TASK_STATE_INPUT_REQUIRED" } }, sceneState)
    .hold,
  true,
);
assert.equal(
  sceneFor(
    { type: "trace", event: { kind: "task_prompt_rendered", title: "Task-T prompt rendered" } },
    sceneState,
  ).step,
  1,
);
assert.equal(
  sceneFor({ type: "event", event: { kind: "artifact", phase: "report" } }, sceneState).step,
  8,
);
assert.equal(
  sceneFor(
    { type: "event", event: { kind: "status", summary: "Data collection started: ..." } },
    sceneState,
  ).step,
  6,
);
assert.equal(sceneFor({ type: "finished", finalState: "task_completed" }, sceneState).key, "finish");

// --- Player: a hold waits for the engineer, then resumes ----------------

const applied = [];
const player = createPlayer({
  onApply: (frame) => applied.push(frame.id),
  onRewind: () => {},
  onChange: () => {},
  onScene: () => {},
});
player.enqueue({ key: "decision", step: 3, duration: 0, hold: true, frame: { id: 1 } });
assert.deepEqual(applied, [1]);
assert.equal(player.status().hold, true);
player.enqueue({ key: "propose", step: 3, duration: 0, hold: false, frame: { id: 2 } });
assert.deepEqual(applied, [1]);
player.release();
assert.deepEqual(applied, [1, 2]);
assert.equal(player.status().hold, false);

const stepping = [];
const stepper = createPlayer({
  onApply: (frame) => stepping.push(frame.id),
  onRewind: () => {},
  onChange: () => {},
  onScene: () => {},
});
stepper.setMode("step");
stepper.enqueue({ key: "request", step: 0, duration: 0, frame: { id: 1 } });
assert.deepEqual(stepping, [1]);
assert.equal(stepper.status().waitingNext, true);
stepper.enqueue({ key: "understand", step: 1, duration: 0, frame: { id: 2 } });
assert.deepEqual(stepping, [1]);
stepper.next();
assert.deepEqual(stepping, [1, 2]);

// --- Intent parsing -----------------------------------------------------

const englishTaskT = `## Operation Type
Create

## Task Object
The area is the Songshanhu Administration Committee.

## Task Context
Time-segment-based guaranteed throughput target: {00:00~07:00,2Mbps}, {07:00~17:30,10Mbps}, {17:30~23:00,20Mbps}, {23:00~24:00,2Mbps}.`;
const english = parseIntent(englishTaskT);
assert.equal(english.operation, "create");
assert.equal(english.area, "Songshanhu Administration Committee");
assert.equal(english.slots.length, 4);
assert.equal(english.overlap, null);
const englishSentence = plainSentence(english, "en-US");
assert.ok(englishSentence.includes("Perform RAN energy saving in Songshanhu Administration Committee"));
assert.ok(englishSentence.includes("Guarantee 2 Mbps from 00:00 to 07:00"));

const overlapTaskT = englishTaskT.replace(
  "{23:00~24:00,2Mbps}",
  "{23:00~24:00,2Mbps}, {20:00~00:00,10Mbps}",
);
assert.deepEqual(parseIntent(overlapTaskT).overlap, { start: 1200, end: 1440 });

const chineseTaskT = `## 操作类型(Operation Type)

创建

## 任务对象(Task Object)

区域为松山湖管委会。

## 任务上下文(Task Context)

分时段保障速率目标：{00:00~07:00,2Mbps}、{07:00~17:30,10Mbps}、{17:30~23:00,20Mbps}、{23:00~24:00,2Mbps}。`;
const chinese = parseIntent(chineseTaskT);
assert.equal(chinese.operation, "create");
assert.equal(chinese.area, "松山湖管委会");
assert.equal(chinese.slots.length, 4);

const feasibility = parseIntent(
  `## 操作类型(Operation Type)

创建

## 任务上下文(Task Context)

站点列表1为[站点名称1, 站点名称2]；分时段保障速率目标为 {00:00~06:00,2Mbps}、{06:00~18:00,10Mbps}；背景事件为在00:00~18:00停电。`,
);
assert.deepEqual(feasibility.outage, { start: 0, end: 1080 });
assert.deepEqual(feasibility.sites, ["站点名称1", "站点名称2"]);

const outageTaskT = `## Operation Type

Create

## Task Object

The area is the Songshanhu Administration Committee. Site list 1: ["Site 1", "Site 2"].

## Task Context

Time-segment-based guaranteed throughput target: {00:00~03:00,10Mbps}, {03:00~05:00,2Mbps}, {05:00~24:00,10Mbps}. Background event: power outage from 03:00 to 05:00.`;
const outage = parseIntent(outageTaskT);
assert.equal(outage.area, "Songshanhu Administration Committee");
assert.deepEqual(outage.sites, ["Site 1", "Site 2"]);
assert.deepEqual(outage.outage, { start: 180, end: 300 });
assert.equal(outage.slots.length, 3);

// --- Remaining time -----------------------------------------------------

const requestedAt = new Date(Date.now() - 10_000).toISOString();
const left = remainingSeconds(requestedAt, 120);
assert.ok(left !== null && left <= 111 && left >= 109, left);

console.log("presentation model harness: ok");
