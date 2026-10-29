// Presentation view-model checks (no DOM, no network).
//
// Run with: node tests/js/presentation.test.mjs
// The Python suite shells out to this file and skips cleanly when Node is absent.

import assert from "node:assert/strict";

import {
  applyFrame,
  createState,
  executionSteps,
  resolutionOf,
  roundInfo,
  stageOf,
} from "../../demo/web/presentation/model.js";

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
  });
}

function decision(seq, action, round, maxRounds = 3) {
  return event({
    seq,
    entryType: "decision",
    kind: action,
    phase: "negotiation",
    round,
    maxRounds,
    performative: action.toUpperCase(),
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

applyFrame(accepted, { type: "run_started", runId: "r1", scenario: "target" });
assert.equal(accepted.scenario, "target");
assert.equal(stageOf(accepted), "negotiating");

applyFrame(accepted, proposal(1, 1));
assert.deepEqual(roundInfo(accepted), { round: 1, maxRounds: 3 });

// The same frame replayed after a reconnect must not duplicate the row.
applyFrame(accepted, proposal(1, 1));
assert.equal(accepted.negotiation.length, 1);

applyFrame(accepted, decision(2, "counter", 1));
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
applyFrame(accepted, artifact(7, "execution"));
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
applyFrame(rejected, { type: "run_started", runId: "r2", scenario: "reject" });
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
applyFrame(aborted, { type: "run_started", runId: "r3", scenario: "abort" });
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
applyFrame(failed, { type: "run_started", runId: "r4", scenario: "information" });
applyFrame(failed, finished("incomplete", null, "server unavailable"));
assert.equal(stageOf(failed), "failed");
assert.equal(failed.error, "server unavailable");

console.log("presentation model harness: ok");
