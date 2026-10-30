/** Run state of the presentation page.
 *
 * The reducer consumes the same SSE frames as the Run Explorer. It keeps what the stage and the
 * evidence panels need: the negotiation turns, the structured intent, the trace rows, the pending
 * human decisions and the final report. It is a pure function of the frames so a replay after a
 * reconnect (or a step back) never duplicates a row.
 */

export const STATUS = {
  IDLE: "idle",
  RUNNING: "running",
  FINISHED: "finished",
  FAILED: "failed",
};

export const APPROVAL = {
  WAITING: "waiting",
  APPROVED: "approved",
  REJECTED: "rejected",
};

/** One page state, before any run. */
export function createState() {
  return {
    status: STATUS.IDLE,
    runId: null,
    scenario: null,
    mode: null,
    protocol: "a2a-t",
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
    intentText: null,
    intentReview: null,
    dataCollection: null,
    trace: [],
    decision: null,
    approval: null,
    approvalState: null,
    approvalExported: false,
    nmsTool: null,
    lastStatusText: "",
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
      if (typeof frame.mode === "string") state.mode = frame.mode;
      if (frame.protocol === "plain") state.protocol = "plain";
      else if (frame.protocol === "a2a-t") state.protocol = "a2a-t";
      if (state.status === STATUS.IDLE) state.status = STATUS.RUNNING;
      return state;
    }
    case "event":
      return applyEvent(state, frame.event);
    case "trace":
      return applyTrace(state, frame.event);
    case "intent_review_requested": {
      state.intentReview = {
        promptText: typeof frame.promptText === "string" ? frame.promptText : "",
        timeoutSeconds: numberOrNull(frame.timeoutSeconds),
        requestedAt: typeof frame.requestedAt === "string" ? frame.requestedAt : null,
        resolved: false,
      };
      if (!state.intentText && state.intentReview.promptText) {
        state.intentText = state.intentReview.promptText;
      }
      return state;
    }
    case "decision_requested": {
      state.decision = {
        proposal: isObject(frame.proposal) ? frame.proposal : {},
        round: intOrNull(frame.round),
        maxRounds: intOrNull(frame.maxRounds),
        performative: typeof frame.performative === "string" ? frame.performative : "",
        timeoutSeconds: numberOrNull(frame.timeoutSeconds),
        requestedAt: typeof frame.requestedAt === "string" ? frame.requestedAt : null,
        recommendation: isObject(frame.recommendation) ? frame.recommendation : null,
        resolved: false,
      };
      state.trace.push({
        tag: "a2at",
        title: "Decision requested",
        titleKey: "decisionRequested",
        sub: `round ${frame.round ?? "?"} · ${frame.proposal?.kind ?? ""}`,
        chip: "decision",
        chipKind: "neg",
        decision: true,
      });
      return state;
    }
    case "decision_recommended": {
      if (state.decision !== null) {
        state.decision.recommendation = isObject(frame.decision) ? frame.decision : null;
      }
      return state;
    }
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
      if (state.decision !== null) state.decision.resolved = true;
      if (state.intentReview !== null) state.intentReview.resolved = true;
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
  const summary = typeof entry.summary === "string" ? entry.summary : "";
  const content = typeof entry.content === "string" ? entry.content : "";

  state.trace.push({
    tag: traceTag(entry),
    title: traceTitle(entry, summary),
    titleKey:
      entry.kind === "status" && summary.includes("INPUT_REQUIRED") ? "approval" : null,
    sub: traceSub(entry),
    chip: entry.performative || entry.kind || "",
    chipKind: chipKind(entry),
    entry,
  });

  if (entry.entryType === "decision") {
    state.negotiation.push({
      side: "oss",
      action: normaliseAction(entry.kind),
      round,
      maxRounds,
      summary,
      content,
      detail: isObject(entry.detail) ? entry.detail : null,
      confirmedBy: entry.confirmedBy || entry.detail?.confirmedBy || null,
    });
    if (state.decision !== null) state.decision.resolved = true;
    return state;
  }

  switch (entry.kind) {
    case "propose":
      state.negotiation.push({
        side: "nms",
        action: "propose",
        round,
        maxRounds,
        summary,
        content,
        negotiationType: entry.negotiationType || null,
      });
      break;
    case "abort":
      state.negotiation.push({
        side: "nms",
        action: "abort",
        round,
        maxRounds,
        summary,
        content,
      });
      break;
    case "message":
      if (entry.phase === "negotiation") {
        state.negotiationEnded = true;
        state.negotiation.push({
          side: "nms",
          action: "end",
          round,
          maxRounds,
          summary,
          content,
        });
      }
      break;
    case "status": {
      const label = summary || "";
      if (label) state.lastStatusText = label;
      if (label.includes("Data collection started")) state.dataCollection = "started";
      if (label.includes("Data collection in progress")) state.dataCollection = "progress";
      if (label.includes("Data collection completed")) state.dataCollection = "completed";
      if (label.includes("SUBMITTED") || label.includes("WORKING") || label.includes("INPUT_REQUIRED")) {
        state.taskCreated = true;
      }
      if (label.includes("INPUT_REQUIRED")) {
        if (state.approval === null) state.approval = { generic: true };
        state.approvalState = APPROVAL.WAITING;
      } else if (label.includes("WORKING") && state.approvalState === APPROVAL.WAITING) {
        // A WORKING update after the gate means the change was approved (or the gate was off).
        state.approvalState = APPROVAL.APPROVED;
      } else if (label.includes("REJECTED")) {
        state.approvalState = APPROVAL.REJECTED;
      }
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

function applyTrace(state, span) {
  if (!isObject(span)) return state;
  const seq = span.seq;
  if (Number.isInteger(seq)) {
    if (state.seen.has(seq)) return state;
    state.seen.add(seq);
  }
  const kind = typeof span.kind === "string" ? span.kind : "trace";
  const detail = isObject(span.detail) ? span.detail : null;

  state.trace.push({
    tag: traceSpanTag(span, kind),
    title: typeof span.title === "string" ? span.title : kind,
    titleKey:
      kind === "decision_requested"
        ? "decisionRequested"
        : kind === "approval"
          ? "approval"
          : kind === "intent_review_requested"
            ? "intentReview"
            : null,
    sub: typeof span.purpose === "string" ? span.purpose : "",
    chip: kind === "tool" ? "tool" : kind === "llm" ? "LLM" : "",
    chipKind: kind === "tool" ? "status" : "",
    entry: span,
  });

  if (kind === "task_prompt_rendered" && detail && typeof detail.promptText === "string") {
    state.intentText = detail.promptText;
  }
  if (kind === "tool" && detail && typeof detail.toolName === "string") {
    state.nmsTool = detail.toolName;
  }
  if (kind === "approval") {
    const approved = detail && detail.approved === true;
    if (span.title === "Approval requested" || (detail && detail.approvalState === "requested")) {
      state.approval = {
        cellId: detail?.cellId ?? null,
        window: detail?.window ?? null,
        rate: detail?.rate ?? detail?.targetRate ?? null,
        taskId: detail?.taskId ?? null,
        timeoutSeconds: numberOrNull(detail?.timeoutSeconds),
        requestedAt:
          typeof span.startedAt === "string"
            ? span.startedAt
            : typeof span.ts === "string"
              ? span.ts
              : null,
        generic: false,
      };
      state.approvalState = APPROVAL.WAITING;
      state.approvalExported = false;
    } else if (approved) {
      state.approvalState = APPROVAL.APPROVED;
    } else if (detail && detail.approvalState === "rejected") {
      state.approvalState = APPROVAL.REJECTED;
    }
  }
  return state;
}

/** The act the page should show. */
export function stageOf(state) {
  if (state.status === STATUS.FAILED) return "failed";
  if (state.status === STATUS.FINISHED) {
    if (state.finalState === "intent_not_confirmed") return "notSent";
    if (state.finalState === "task_rejected" && state.protocol === "plain") return "plainRejected";
    if (state.finalState === "task_rejected" || state.approvalState === APPROVAL.REJECTED) {
      return "notApplied";
    }
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

/** Ending of a run that stopped without a task: the resolution plus the peer's last text.
 *
 * A plain terminal message from the RAN Agent is the only place its real reason survives (for
 * example a failed accept validation); a rejected or aborted negotiation carries its own copy.
 */
export function endingOf(state) {
  let detail = "";
  for (let index = state.negotiation.length - 1; index >= 0; index -= 1) {
    const message = state.negotiation[index];
    if (message.side === "nms" && message.action === "end") {
      detail = String(message.summary || message.content || "").trim();
      break;
    }
  }
  return { resolution: resolutionOf(state), detail };
}

/** The NMS execution milestones and their live status. */
export function executionSteps(state) {
  // The NMS publishes one artifact per tool: profile, evaluate, apply. The report is separate.
  const steps = [
    { key: "evaluate", done: state.executionArtifacts >= 2 },
    { key: "apply", done: state.executionArtifacts >= 3 },
    { key: "report", done: state.reportArtifact || state.report !== null },
  ];
  const activeIndex =
    state.status === STATUS.RUNNING ? steps.findIndex((step) => !step.done) : -1;
  return steps.map((step, index) => ({ ...step, active: index === activeIndex }));
}

/** Whether the run currently waits for the engineer. */
export function pendingHuman(state) {
  if (state.status !== STATUS.RUNNING) return null;
  if (state.intentReview !== null && !state.intentReview.resolved) return "intent";
  if (state.approvalState === APPROVAL.WAITING) return "approval";
  if (state.decision !== null && !state.decision.resolved) return "decision";
  return null;
}

function traceSpanTag(span, kind) {
  if (kind === "approval") return "mie";
  if (kind === "tool" || kind === "execution") return "nms";
  if (kind === "task_prompt_rendered" || kind === "decision_requested") return "oss";
  if (kind === "message_sent") return "a2at";
  if (kind === "llm") {
    const detail = isObject(span.detail) ? span.detail : {};
    return detail.provider === "langgraph" ? "nms" : "oss";
  }
  return "oss";
}

function traceTag(entry) {
  if (entry.entryType === "decision") return "oss";
  if (entry.direction === "client->server") return "a2at";
  if (entry.direction === "server->client") return "nms";
  return "a2at";
}

function traceTitle(entry, summary) {
  if (entry.kind === "propose") return "PROPOSE · NMS";
  if (entry.kind === "abort") return "ABORT · NMS";
  if (entry.kind === "accept") return "ACCEPT · OSS";
  if (entry.kind === "reject") return "REJECT · OSS";
  if (entry.kind === "counter") return "COUNTER · OSS";
  if (entry.kind === "status") return summary.replace("TASK_STATE_", "") || "status";
  if (entry.kind === "artifact") return entry.phase === "report" ? "report artifact" : "tool result";
  return summary || entry.kind || "event";
}

function traceSub(entry) {
  if (entry.entryType === "decision") return entry.rationale || "";
  if (entry.kind === "status" || entry.kind === "artifact") return "";
  return entry.negotiationType || entry.performative || "";
}

function chipKind(entry) {
  if (entry.kind === "status") return "status";
  if (entry.kind === "artifact") return "art";
  if (entry.entryType === "decision") return "neg";
  if (entry.kind === "reject" || entry.kind === "abort") return "no";
  return "";
}

function normaliseAction(value) {
  const action = typeof value === "string" ? value.trim().toLowerCase() : "";
  return ["accept", "reject", "abort", "counter"].includes(action) ? action : "counter";
}

function intOrNull(value) {
  return Number.isInteger(value) && value > 0 ? value : null;
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
