/** View model of the presentation page.
 *
 * The reducer consumes the same SSE frames as the Run Explorer but keeps only what an audience
 * needs: the negotiation turns, whether the task was created, the execution milestones and the
 * final report. Everything here is a pure function of the frames, so the page can replay a run
 * after a reconnect without duplicating a row.
 */

export const STATUS = {
  IDLE: "idle",
  RUNNING: "running",
  FINISHED: "finished",
  FAILED: "failed",
};

/** One page state, before any run. */
export function createState() {
  return {
    status: STATUS.IDLE,
    runId: null,
    scenario: null,
    negotiation: [],
    seen: new Set(),
    taskCreated: false,
    taskState: null,
    executionArtifacts: 0,
    reportArtifact: false,
    report: null,
    finalState: null,
    error: null,
    finished: false,
    negotiationEnded: false,
  };
}

/** Apply one SSE frame to the state; returns the same state for chaining. */
export function applyFrame(state, frame) {
  if (!isObject(frame)) return state;
  switch (frame.type) {
    case "run_started": {
      if (state.runId !== null && frame.runId !== state.runId) return state;
      state.runId = typeof frame.runId === "string" ? frame.runId : state.runId;
      if (typeof frame.scenario === "string") state.scenario = frame.scenario;
      if (state.status === STATUS.IDLE) state.status = STATUS.RUNNING;
      return state;
    }
    case "event":
      return applyEvent(state, frame.event);
    case "finished": {
      if (state.finished) return state;
      state.finished = true;
      state.status =
        frame.status === "failed" || (typeof frame.error === "string" && frame.error)
          ? STATUS.FAILED
          : STATUS.FINISHED;
      state.finalState = typeof frame.finalState === "string" ? frame.finalState : null;
      state.report = isObject(frame.report) ? frame.report : null;
      state.error =
        typeof frame.error === "string" && frame.error ? frame.error : state.error;
      return state;
    }
    default:
      return state;
  }
}

function applyEvent(state, entry) {
  if (!isObject(entry)) return state;
  const seq = entry.seq;
  if (Number.isInteger(seq)) {
    if (state.seen.has(seq)) return state;
    state.seen.add(seq);
  }
  const round = intOrNull(entry.round);
  const maxRounds = intOrNull(entry.maxRounds);

  if (entry.entryType === "decision") {
    state.negotiation.push({
      side: "oss",
      action: normaliseAction(entry.kind),
      round,
      maxRounds,
    });
    return state;
  }

  switch (entry.kind) {
    case "propose":
      state.negotiation.push({ side: "nms", action: "propose", round, maxRounds });
      break;
    case "abort":
      state.negotiation.push({ side: "nms", action: "abort", round, maxRounds });
      break;
    case "message":
      if (entry.phase === "negotiation") {
        state.negotiationEnded = true;
        state.negotiation.push({ side: "nms", action: "end", round, maxRounds });
      }
      break;
    case "status": {
      const label = typeof entry.summary === "string" ? entry.summary : "";
      if (label.includes("SUBMITTED") || label.includes("WORKING")) state.taskCreated = true;
      if (label) state.taskState = label;
      break;
    }
    case "artifact":
      if (entry.phase === "report") state.reportArtifact = true;
      else state.executionArtifacts += 1;
      break;
    default:
      break;
  }
  return state;
}

/** The act the page should show: ``idle``, ``negotiating``, ``executing``, ``done``, ``ended``
 * or ``failed``. */
export function stageOf(state) {
  if (state.status === STATUS.FAILED) return "failed";
  if (state.status === STATUS.FINISHED) {
    const completed =
      state.report !== null || state.reportArtifact || state.finalState === "task_completed";
    return completed ? "done" : "ended";
  }
  if (state.status === STATUS.RUNNING) {
    return state.taskCreated ? "executing" : "negotiating";
  }
  return "idle";
}

/** The highest round seen and its budget. */
export function roundInfo(state) {
  let round = 0;
  let maxRounds = 0;
  for (const message of state.negotiation) {
    if (Number.isInteger(message.round)) round = Math.max(round, message.round);
    if (Number.isInteger(message.maxRounds)) maxRounds = Math.max(maxRounds, message.maxRounds);
  }
  return { round, maxRounds };
}

/** How the negotiation ended: ``accepted``, ``rejected``, ``aborted`` or ``null``. */
export function resolutionOf(state) {
  for (let index = state.negotiation.length - 1; index >= 0; index -= 1) {
    const message = state.negotiation[index];
    if (message.action === "abort") return "aborted";
    if (message.side === "oss" && message.action === "accept") return "accepted";
    if (message.side === "oss" && message.action === "reject") return "rejected";
  }
  return null;
}

/** The three NMS execution milestones and their live status. */
export function executionSteps(state) {
  const steps = [
    { key: "evaluate", done: state.executionArtifacts >= 1 },
    { key: "apply", done: state.executionArtifacts >= 2 },
    { key: "report", done: state.reportArtifact || state.report !== null },
  ];
  const activeIndex =
    state.status === STATUS.RUNNING ? steps.findIndex((step) => !step.done) : -1;
  return steps.map((step, index) => ({ ...step, active: index === activeIndex }));
}

function normaliseAction(value) {
  const action = typeof value === "string" ? value.trim().toLowerCase() : "";
  return ["accept", "reject", "abort", "counter"].includes(action) ? action : "counter";
}

function intOrNull(value) {
  return Number.isInteger(value) && value > 0 ? value : null;
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
