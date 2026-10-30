/** The three human moments of the demo: intent review, negotiation decision and MML review. */

import { actionLabel, format, proposalLine } from "../copy.js";

/** Build approval 1: the engineer confirms the structured intent before the Task-T is sent. */
export function intentCard(state, copy) {
  const review = state.intentReview;
  if (review === null || review.resolved) return null;
  return {
    type: "intent",
    kicker: copy.intentReview.kicker,
    title: copy.intentReview.title,
    text: copy.intentReview.text,
    detail: copy.intentReview.detail,
    remaining: remainingSeconds(review.requestedAt, review.timeoutSeconds),
    options: [
      { action: "approve", label: copy.intentReview.confirm, primary: true },
      { action: "edit", label: copy.intentReview.edit },
      { action: "cancel", label: copy.intentReview.cancel, danger: true },
    ],
  };
}

/** Build the negotiation decision card, or ``null`` when no decision is pending. */
export function decisionCard(state, copy) {
  const decision = state.decision;
  if (decision === null || decision.resolved) return null;
  const round = decision.round ?? decision.proposal?.round ?? 1;
  const maxRounds = decision.maxRounds ?? decision.proposal?.max_rounds ?? 1;
  const kind = decision.proposal?.kind || "";
  const recommendation = decision.recommendation;
  const recommendedAction = recommendation?.action || null;
  // In confirm mode the OSS Agent is still thinking: do not offer a decision before its
  // recommendation arrives, otherwise the engineer could answer without the agent's input.
  if (state.mode === "confirm" && !recommendation) return null;
  const proposalText =
    proposalLine(copy, state.scenario, round) ||
    firstLine(decision.proposal?.content) ||
    copy.negotiation.generic;

  const options = [];
  if (recommendedAction) {
    options.push({ action: "confirm", label: copy.decision.confirm, primary: true });
  } else {
    options.push({ action: "accept", label: copy.decision.acceptNms, primary: true });
  }
  if (recommendedAction && recommendedAction !== "accept") {
    options.push({ action: "accept", label: copy.decision.acceptNms });
    options.push({ action: "counter", label: copy.decision.editCounter });
  }
  options.push({ action: "reject", label: copy.decision.reject, danger: true });

  return {
    type: "decision",
    kicker: copy.decision.kicker,
    title: format(copy.decision.title, { round, maxRounds }),
    text: proposalText,
    detail: recommendation
      ? `${format(copy.decision.recommendation, {
          action: actionLabel(copy, recommendedAction),
        })}${recommendation.rationale ? ` — ${recommendation.rationale}` : ""}`
      : copy.decision.question,
    kind,
    recommendedAction,
    recommendation,
    counterText: recommendation?.counter_proposal || "",
    options,
    remaining: remainingSeconds(decision.requestedAt, decision.timeoutSeconds),
  };
}

/** Build approval 2 (the MIE MML review), or ``null`` when no review is pending. */
export function approvalCard(state, copy) {
  if (state.approvalState !== "waiting") return null;
  const approval = state.approval || {};
  const cell = approval.cellId || "—";
  const window = approval.window || "—";
  const rate = approval.rate || "—";
  const detail = approval.generic
    ? copy.approval.waiting
    : format(copy.approval.detail, { cell, window, rate });
  const options = state.approvalExported
    ? [
        { action: "approve", label: copy.approval.acknowledge, primary: true },
        { action: "reject", label: copy.approval.reject, danger: true },
      ]
    : [
        { action: "export", label: copy.approval.export, primary: true },
        { action: "reject", label: copy.approval.reject, danger: true },
      ];
  return {
    type: "approval",
    kicker: copy.approval.kicker,
    title: copy.approval.title,
    text: detail,
    detail: copy.approval.note,
    mml: state.approvalExported
      ? {
          label: copy.approval.sample,
          text: copy.approval.mml(cell, window, String(rate).replace("%", "")),
        }
      : null,
    policy: copy.approval.policy,
    remaining: remainingSeconds(approval.requestedAt, approval.timeoutSeconds),
    options,
  };
}

/** Build the ``NegotiationDecision`` payload of one click. */
export function decisionPayload(card, action, copy, counterText) {
  if (action === "confirm" && card.recommendation) {
    return clean({
      action: card.recommendation.action,
      rationale: card.recommendation.rationale || copy.decision.acceptRationale,
      filled_params: card.recommendation.filled_params ?? null,
      counter_proposal: card.recommendation.counter_proposal ?? null,
      counter_params: card.recommendation.counter_params ?? null,
      confirmed_intent: card.recommendation.confirmed_intent ?? null,
      feasibility_summary: card.recommendation.feasibility_summary ?? null,
    });
  }
  if (action === "counter") {
    return clean({
      action: "counter",
      rationale: copy.decision.counterRationale,
      counter_proposal: (counterText || "").trim() || card.counterText,
    });
  }
  if (action === "reject") {
    return { action: "reject", rationale: copy.decision.rejectRationale };
  }
  if (action === "abort") {
    return { action: "abort", rationale: copy.decision.abortRationale };
  }
  return { action: "accept", rationale: copy.decision.acceptRationale };
}

/** Seconds left in a human window, or ``null`` when the backend did not send a budget. */
export function remainingSeconds(requestedAt, timeoutSeconds) {
  const timeout = Number(timeoutSeconds);
  if (!Number.isFinite(timeout) || timeout <= 0) return null;
  const start = Date.parse(requestedAt || "");
  if (!Number.isFinite(start)) return Math.round(timeout);
  return Math.max(0, Math.round(timeout - (Date.now() - start) / 1000));
}

/** Format seconds as ``m:ss``. */
export function clock(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const value = Math.max(0, Math.round(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

function firstLine(content) {
  if (typeof content !== "string") return "";
  const line = content
    .split(/\r?\n/)
    .map((item) => item.replace(/^#+\s*/, "").replace(/\*\*/g, "").trim())
    .find((item) => item !== "");
  return line ? line.slice(0, 180) : "";
}

function clean(payload) {
  const result = {};
  for (const [key, value] of Object.entries(payload)) {
    if (value !== null && value !== undefined && value !== "") result[key] = value;
  }
  return result;
}
