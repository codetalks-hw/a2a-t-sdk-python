/** Map one SSE frame to one stage scene.
 *
 * The backend runs at its own speed; the presentation player consumes these scenes at a human
 * pace. A scene carries the step it belongs to, how long it should stay on screen, and whether it
 * must wait for the engineer (approval 1, a negotiation decision or the MML review).
 */

export const STEP_KEYS = [
  "request",
  "understand",
  "intent",
  "send",
  "negotiate",
  "strategy",
  "collect",
  "approval",
  "apply",
];

/** Return the zero-based index of a step key, or -1. */
export function stepIndex(key) {
  return STEP_KEYS.indexOf(key);
}

/**
 * Return the scene of one frame, or ``null`` when the frame should be applied without a pause.
 *
 * @param {object} frame - one SSE frame.
 * @param {object} state - the view state *before* the frame is applied.
 * @returns {{key: string, step: number, duration: number, hold: boolean, frame: object}|null}
 */
export function sceneFor(frame, state) {
  if (!isObject(frame)) return null;
  switch (frame.type) {
    case "run_started":
      return scene("request", 600, frame);
    case "event":
      return eventScene(frame.event, state, frame);
    case "trace":
      return traceScene(frame.event, state, frame);
    case "intent_review_requested":
      return scene("intent-review", 0, frame, true);
    case "decision_requested":
      // Confirm mode delivers the proposal and the agent recommendation in one frame; manual
      // mode delivers the proposal alone. Both stop the show for the engineer.
      return scene("decision", 0, frame, true);
    case "decision_recommended":
      return scene("decision", 0, frame, false);
    case "finished":
      return scene("finish", 700, frame);
    default:
      return null;
  }
}

function eventScene(entry, state, frame) {
  if (!isObject(entry)) return scene("event", 0, frame);
  if (entry.entryType === "decision") return scene("decision", 900, frame);
  switch (entry.kind) {
    case "propose":
      return scene("propose", 1400, frame);
    case "abort":
      return scene("abort", 1000, frame);
    case "reject":
      return scene("decision", 900, frame);
    case "accept":
      return scene("decision", 900, frame);
    case "counter":
      return scene("decision", 900, frame);
    case "message":
      return entry.phase === "negotiation" ? scene("end", 700, frame) : scene("event", 0, frame);
    case "status": {
      const label = typeof entry.summary === "string" ? entry.summary : "";
      if (label.includes("Data collection started")) {
        return scene("collect-start", 1600, frame);
      }
      if (label.includes("Data collection completed")) {
        return scene("collect-done", 1200, frame);
      }
      if (label.includes("INPUT_REQUIRED")) {
        // The status only names the state; the plan details arrive in the approval trace span.
        // Hold here unless that span already populated the card.
        return scene("approval", 0, frame, state.approval === null);
      }
      if (label.includes("SUBMITTED")) return scene("strategy", 900, frame);
      if (label.includes("WORKING")) {
        if (state.approvalState === "approved" || state.executionArtifacts >= 2) {
          return scene("apply", 900, frame);
        }
        return scene("strategy", 900, frame);
      }
      if (label.includes("REJECTED")) return scene("approval-rejected", 900, frame);
      if (label.includes("COMPLETED") || label.includes("FAILED")) {
        return scene("finish", 600, frame);
      }
      return scene("event", 0, frame);
    }
    case "artifact": {
      if (entry.phase === "report") return scene("report", 1000, frame);
      const next = state.executionArtifacts + 1;
      if (next === 1) {
        return state.dataCollection === "completed"
          ? scene("trace", 0, frame)
          : scene("collect-done", 900, frame);
      }
      if (next === 2) return scene("approval-ready", 900, frame);
      return scene("apply", 700, frame);
    }
    default:
      return scene("event", 0, frame);
  }
}

function traceScene(span, state, frame) {
  if (!isObject(span)) return scene("trace", 0, frame);
  if (span.kind === "intent_review_requested") {
    // The dedicated intent frame drives the hold and the card; its trace span only adds a row.
    return null;
  }
  if (span.kind === "task_prompt_rendered") {
    return state.intentText === null ? scene("understand", 1200, frame) : scene("trace", 0, frame);
  }
  if (span.kind === "message_sent") {
    const detail = isObject(span.detail) ? span.detail : {};
    const performative = typeof detail.performative === "string" ? detail.performative : "";
    if (performative === "TASK_T") return scene("send", 1200, frame);
    return scene("negotiate-send", 500, frame);
  }
  if (span.kind === "tool") {
    const detail = isObject(span.detail) ? span.detail : {};
    const name = typeof detail.toolName === "string" ? detail.toolName : "";
    if (name === "get_cell_energy_profile") {
      if (state.dataCollection === "completed") return scene("trace", 0, frame);
      return state.dataCollection === "started"
        ? scene("collect-done", 900, frame)
        : scene("collect-start", 1200, frame);
    }
    if (name === "evaluate_energy_saving_plan") return scene("approval-ready", 900, frame);
    if (name === "apply_energy_saving_plan") return scene("apply", 900, frame);
  }
  if (span.kind === "approval") {
    const detail = isObject(span.detail) ? span.detail : {};
    if (span.title === "Approval requested" || detail.approvalState === "requested") {
      return state.approvalState === "waiting" ? scene("trace", 0, frame) : scene("approval", 0, frame, true);
    }
    return scene("trace", 300, frame);
  }
  return scene("trace", 0, frame);
}

function scene(key, duration, frame, hold = false) {
  return {
    key,
    step: stepIndex(stepOf(key)),
    duration,
    hold,
    frame,
  };
}

function stepOf(key) {
  switch (key) {
    case "request":
      return "request";
    case "understand":
      return "understand";
    case "intent-review":
      return "intent";
    case "send":
      return "send";
    case "propose":
    case "decision":
    case "negotiate-send":
    case "end":
    case "abort":
      return "negotiate";
    case "strategy":
      return "strategy";
    case "collect-start":
    case "collect-done":
      return "collect";
    case "approval":
    case "approval-ready":
    case "approval-rejected":
      return "approval";
    case "apply":
    case "report":
    case "finish":
      return "apply";
    default:
      return "request";
  }
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
