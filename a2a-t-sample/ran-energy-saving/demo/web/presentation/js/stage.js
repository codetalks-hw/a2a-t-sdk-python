/** The live stage: four actors, two zones, the A2A-T channel, bubbles and flying packets. */

import { format, proposalLine, decisionLine } from "../copy.js";
import {
  executionSteps,
  negotiationExpected,
  pendingHuman,
  resolutionOf,
  roundInfo,
  stageOf,
} from "./state.js";
import { svg } from "./dom.js";

const REDUCED =
  typeof window !== "undefined" &&
  window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function createStage(els) {
  buildCells(els.cellsFigure);
  let bubbles = {};
  const packetTimers = new Set();
  let dayProgress = 0;
  let dayTimer = null;

  function stopDays() {
    if (dayTimer !== null) {
      window.cancelAnimationFrame(dayTimer);
      dayTimer = null;
    }
  }

  function animateDays(copy) {
    stopDays();
    const start = performance.now();
    const frame = (now) => {
      const t = Math.min(1, (now - start) / 2600);
      dayProgress = t;
      const day = Math.max(1, Math.min(8, Math.round(1 + t * 7)));
      els.nmsBar.style.width = `${Math.round(t * 100)}%`;
      setActor(
        "nms",
        { cls: "active", text: format(copy.states.nmsCollecting, { day }) },
        copy,
      );
      if (t < 1) {
        dayTimer = window.requestAnimationFrame(frame);
      } else {
        dayTimer = null;
      }
    };
    dayTimer = window.requestAnimationFrame(frame);
  }

  function clearPackets() {
    for (const timer of packetTimers) window.clearTimeout(timer);
    packetTimers.clear();
    for (const node of els.stage.querySelectorAll(".packet")) node.remove();
    for (const node of els.stage.querySelectorAll(".lane.hot")) node.classList.remove("hot");
  }

  function reset() {
    clearPackets();
    stopDays();
    dayProgress = 0;
    bubbles = {};
    for (const node of Object.values(els.bubbles)) {
      node.className = "bubble";
      node.replaceChildren();
    }
    els.nmsBar.style.width = "0";
    els.approval.className = "approval";
    els.approvalState.textContent = "";
    for (let index = 0; index < 6; index += 1) setCell(index, 3, false, false);
  }

  function note(actor, who, text, neg = false) {
    bubbles[actor] = { who, text, neg };
    paintBubble(actor);
  }

  function paintBubble(actor) {
    const node = els.bubbles[actor];
    if (!node) return;
    const entry = bubbles[actor];
    if (!entry || !entry.text) {
      node.className = "bubble";
      node.replaceChildren();
      return;
    }
    node.className = `bubble show${entry.neg ? " neg" : ""}`;
    node.replaceChildren();
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = entry.who;
    const body = document.createElement("div");
    body.className = "clamp";
    body.textContent = entry.text;
    node.append(who, body);
  }

  function fly(laneId, label, kind, duration = 1100) {
    if (REDUCED) return;
    const path = document.getElementById(laneId);
    if (!path) return;
    const length = path.getTotalLength();
    const packet = document.createElement("div");
    packet.className = `packet ${kind}`;
    packet.textContent = label;
    els.stage.append(packet);
    path.classList.add("hot");
    const total = Math.max(160, duration);
    const start = performance.now();
    const frame = (now) => {
      const t = Math.min(1, (now - start) / total);
      const eased = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      const point = path.getPointAtLength(length * eased);
      packet.style.left = `${(point.x / 12).toFixed(2)}%`;
      packet.style.top = `${(point.y / 5.2).toFixed(2)}%`;
      if (t < 1) {
        requestAnimationFrame(frame);
      } else {
        path.classList.remove("hot");
        packet.classList.add("land");
        const timer = window.setTimeout(() => {
          packet.remove();
          packetTimers.delete(timer);
        }, 320);
        packetTimers.add(timer);
      }
    };
    requestAnimationFrame(frame);
  }

  /** React to one scene: transient packet and bubble. */
  function scene(scene, state, copy) {
    const key = scene.key;
    const scenario = state.scenario;
    const { round, maxRounds } = roundInfo(state);

    if (key === "request") {
      fly("lane-request", copy.start, "req", 900);
      // Put the real request on the stage: the audience should see the ask, not only "typing".
      note("eng", copy.roles.eng, (state.userRequest || "").trim() || copy.states.engWriting);
      return;
    }
    if (key === "understand") {
      note("oss", copy.roles.oss, copy.states.ossUnderstanding);
      return;
    }
    if (key === "intent-review") {
      note("oss", copy.roles.oss, copy.intentReview.text, true);
      note("eng", copy.roles.eng, copy.states.engIntent, true);
      return;
    }
    if (key === "send") {
      const plain = (state.protocol || "a2a-t") === "plain";
      fly("lane-task", plain ? copy.packet.plain : copy.packet.taskT, "task", 1400);
      note("oss", copy.roles.oss, plain ? copy.states.ossSendingPlain : copy.states.ossSending);
      note("nms", copy.roles.nms, plain ? copy.states.nmsPlainReading : copy.states.nmsValidating);
      note("eng", copy.roles.eng, copy.states.engApproved);
      return;
    }
    if (key === "strategy") {
      note("nms", copy.roles.nms, copy.states.nmsStrategy);
      note("oss", copy.roles.oss, copy.states.ossFollowing);
      return;
    }
    if (key === "collect-start") {
      note("nms", copy.roles.nms, copy.dataCollection.start);
      note("cells", copy.roles.cells, copy.dataCollection.noMml);
      note("eng", copy.roles.eng, copy.states.engApproved);
      return;
    }
    if (key === "collect-progress") {
      note("nms", copy.roles.nms, copy.dataCollection.progress);
      note("cells", copy.roles.cells, copy.dataCollection.noMml);
      animateDays(copy);
      return;
    }
    if (key === "collect-done") {
      stopDays();
      dayProgress = 1;
      els.nmsBar.style.width = "100%";
      note("nms", copy.roles.nms, copy.dataCollection.done);
      note("cells", copy.roles.cells, copy.states.cellsBaseline);
      return;
    }
    if (key === "approval-ready") {
      note("nms", copy.roles.nms, copy.states.nmsEvaluating);
      return;
    }
    if (key === "propose") {
      fly("lane-status", "PROPOSE", "neg", 1200);
      note("nms", copy.roles.nms, proposalLine(copy, scenario, round) || copy.negotiation.generic, true);
      note("oss", copy.roles.oss, copy.states.ossNegotiating, true);
      return;
    }
    if (key === "decision") {
      const action = latestDecisionAction(state);
      fly("lane-task", action ? action.toUpperCase() : "DECISION", "task", 1000);
      note("oss", copy.roles.oss, decisionLine(copy, scenario, round) || action, true);
      return;
    }
    if (key === "approval") {
      fly("lane-status", copy.approval.confirmInMie, "neg", 1300);
      fly("lane-return", copy.approval.reviewNeeded, "neg", 1100);
      note("nms", copy.roles.nms, copy.approval.suggestions, true);
      note("oss", copy.roles.oss, copy.approval.waiting, true);
      note("eng", copy.roles.eng, copy.approval.checkingMml, true);
      fly("lane-approval", copy.approval.reviewInMie, "neg", 2000);
      note("cells", copy.roles.cells, copy.approval.policy, true);
      return;
    }
    if (key === "apply") {
      // Nothing reaches the network while the MIE review is open. The client events already
      // order the approval before the apply artifact; this guard keeps a stray frame from
      // showing the MML anyway.
      if (state.approvalState === "waiting") return;
      fly("lane-apply", copy.approval.mmlCommands, "cfg", 1200);
      note("nms", copy.roles.nms, copy.approval.sendingMml);
      note("cells", copy.roles.cells, copy.states.cellsApplying);
      note("eng", copy.roles.eng, copy.approval.acknowledgedInMie);
      return;
    }
    if (key === "report") {
      fly("lane-status", "REPORT", "status", 1200);
      note("nms", copy.roles.nms, copy.states.nmsReporting);
      note("oss", copy.roles.oss, copy.states.ossFollowing);
      return;
    }
    if (key === "end" || key === "abort") {
      note("nms", copy.roles.nms, copy.negotiation.endLine);
      note("oss", copy.roles.oss, copy.negotiation.endLine);
      return;
    }
    if (key === "approval-rejected") {
      fly("lane-status", "REJECTED", "no", 900);
      note("nms", copy.roles.nms, copy.approval.rejected, true);
      note("cells", copy.roles.cells, copy.states.cellsUnchanged);
    }
  }

  /** Draw the persistent actor state of the current run. */
  function render(state, copy) {
    const stage = stageOf(state);
    const pending = pendingHuman(state);
    const { round, maxRounds } = roundInfo(state);
    const resolution = resolutionOf(state);
    const steps = executionSteps(state);
    const applyDone = steps[1].done;

    let eng = { cls: "", text: copy.states.idle };
    let oss = { cls: "", text: copy.states.ossWaiting };
    let nms = { cls: "", text: copy.states.nmsWaiting };
    let cells = { cls: "", text: copy.states.cellsNormal };

    if (stage === "negotiating") {
      eng = { cls: "ok", text: copy.states.engSent };
      if (negotiationExpected(state)) {
        oss = { cls: "active", text: copy.states.ossNegotiating };
        nms = {
          cls: "active",
          text: maxRounds > 0
            ? format(copy.states.nmsNegotiating, {
                round: Math.max(round, 1),
                max: maxRounds,
              })
            : copy.states.nmsValidating,
        };
      } else if (state.protocol === "plain") {
        oss = { cls: "active", text: copy.states.ossSendingPlain };
        nms = { cls: "active", text: copy.states.nmsPlainReading };
      } else {
        // The request is complete: the OSS is only waiting for the Task-T check, and the RAN
        // Agent is validating it. There is no negotiation to display.
        oss = { cls: "active", text: copy.states.ossWaitingAck };
        nms = { cls: "active", text: copy.states.nmsValidating };
      }
    } else if (stage === "executing") {
      eng = { cls: "ok", text: copy.states.engApproved };
      oss = { cls: "ok", text: copy.states.ossFollowing };
      if (state.approvalState === "waiting") {
        nms = { cls: "wait", text: copy.states.nmsApproval };
        eng = { cls: "wait", text: copy.states.engReviewing };
      } else if (state.approvalState === "approved") {
        nms = { cls: "active", text: copy.states.nmsApplying };
      } else if (state.dataCollection === "started" || state.dataCollection === "progress") {
        const day = Math.max(1, Math.min(8, Math.round(1 + dayProgress * 7)));
        nms = {
          cls: "active",
          text: format(copy.states.nmsCollecting, { day }),
        };
      } else if (state.dataCollection === "completed") {
        nms = { cls: "active", text: copy.states.nmsCollectingDone };
      } else if (applyDone) {
        nms = { cls: "active", text: copy.states.nmsReporting };
      } else {
        nms = { cls: "active", text: copy.states.nmsStrategy };
      }
      cells = {
        cls: applyDone ? "ok" : "active",
        text: applyDone
          ? copy.states.cellsApplied
          : state.dataCollection === "completed"
            ? copy.states.cellsBaseline
            : copy.states.cellsMeasuring,
      };
    } else if (stage === "done") {
      eng = { cls: "ok", text: copy.states.engApproved };
      oss = { cls: "ok", text: copy.states.ossDone };
      nms = { cls: "ok", text: copy.states.nmsDone };
      cells = { cls: "ok", text: copy.states.cellsApplied };
    } else if (stage === "notApplied") {
      eng = { cls: "fail", text: copy.approval.rejected };
      oss = { cls: "ok", text: copy.states.ossDone };
      nms = { cls: "fail", text: copy.approval.rejected };
      cells = { cls: "", text: copy.states.cellsUnchanged };
    } else if (stage === "notSent") {
      eng = { cls: "wait", text: copy.states.engIntent };
      oss = { cls: "", text: copy.states.ossWaiting };
      nms = { cls: "", text: copy.states.nmsWaiting };
      cells = { cls: "", text: copy.states.cellsNormal };
    } else if (stage === "ended" || stage === "failed") {
      eng = { cls: "", text: copy.states.engSent };
      oss = { cls: "", text: copy.states.ossDone };
      nms = { cls: stage === "failed" ? "fail" : "", text: copy.negotiation.statusEnded };
      cells = { cls: "", text: copy.states.cellsNormal };
    }

    if (pending === "intent") {
      oss = { cls: "wait", text: copy.states.ossWaitingHuman };
      eng = { cls: "wait", text: copy.states.engIntent };
    }
    if (pending === "decision") {
      oss = { cls: "wait", text: copy.states.ossWaitingHuman };
      eng = { cls: "wait", text: copy.states.engWaiting };
    }
    if (pending === "approval") {
      nms = { cls: "wait", text: copy.states.nmsApproval };
      eng = { cls: "wait", text: copy.states.engReviewing };
    }
    if (resolution === "rejected" || resolution === "aborted") {
      nms = { cls: stage === "failed" ? "fail" : "", text: copy.negotiation.statusEnded };
    }

    setActor("eng", eng, copy);
    setActor("oss", oss, copy);
    setActor("nms", nms, copy);
    setActor("cells", cells, copy);

    const progress =
      stage === "done"
        ? 100
        : stage === "executing"
          ? applyDone || state.dataCollection === "completed" || state.approvalState !== null
            ? 100
            : state.dataCollection === "started" || state.dataCollection === "progress"
              ? Math.round(dayProgress * 100)
              : 20
          : stage === "negotiating"
            ? 12
            : 0;
    els.nmsBar.style.width = `${progress}%`;

    if (pending === "approval" || state.approvalState === "waiting") {
      els.approval.className = "approval waiting";
      els.approvalState.textContent = copy.states.approvalWaiting;
    } else if (state.approvalState === "approved") {
      els.approval.className = "approval approved";
      els.approvalState.textContent = copy.states.approvalApproved;
    } else if (state.approvalState === "rejected") {
      els.approval.className = "approval rejected";
      els.approvalState.textContent = copy.states.approvalRejected;
    } else {
      els.approval.className = "approval";
      els.approvalState.textContent = copy.states.approvalIdle;
    }

    const carriers = stage === "done" ? 2 : stage === "executing" && applyDone ? 2 : 3;
    const battery = stage === "done" || (stage === "executing" && applyDone);
    for (let index = 0; index < 6; index += 1) setCell(index, carriers, battery, false);
  }

  function setActor(name, value, copy) {
    const actor = els.actors[name];
    actor.classList.remove("active", "wait", "ok", "fail");
    if (value.cls) actor.classList.add(value.cls);
    els.states[name].textContent = value.text;
    if (name === "cells") els.cellsName.textContent = copy.roles.cells;
  }

  function buildCells(figure) {
    figure.replaceChildren();
    for (let index = 0; index < 6; index += 1) {
      const column = index % 3;
      const row = Math.floor(index / 3);
      const cx = 58 + column * 122;
      const ay = 30 + row * 62;
      const group = svg("g", { id: `cell-${index}` });
      for (let ring = 1; ring <= 2; ring += 1) {
        const r = 4 + ring * 5;
        const angle = (42 * Math.PI) / 180;
        const x1 = cx + r * Math.cos(-angle);
        const y1 = ay + r * Math.sin(-angle);
        const x2 = cx + r * Math.cos(angle);
        const y2 = ay + r * Math.sin(angle);
        group.append(
          svg("path", {
            class: "cell-wave on",
            "data-k": ring,
            d: `M${x1.toFixed(1)} ${y1.toFixed(1)}A${r} ${r} 0 0 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`,
          }),
          svg("path", {
            class: "cell-wave on",
            "data-k": ring,
            d: `M${(2 * cx - x1).toFixed(1)} ${y1.toFixed(1)}A${r} ${r} 0 0 0 ${(2 * cx - x2).toFixed(1)} ${y2.toFixed(1)}`,
          }),
        );
      }
      group.append(
        svg("path", {
          class: "cell-mast",
          d: `M${cx} ${ay}L${cx - 6} ${ay + 26}M${cx} ${ay}L${cx + 6} ${ay + 26}M${cx - 4} ${ay + 20}h8`,
        }),
        svg("circle", { cx, cy: ay, r: 2.2, fill: "#000" }),
        svg("g", { class: "cell-bat", id: `cell-bat-${index}` }),
      );
      const batteryGroup = group.querySelector(`#cell-bat-${index}`);
      batteryGroup.append(
        svg("rect", {
          x: cx + 9,
          y: ay + 15,
          width: 12,
          height: 7,
          fill: "#fff",
          stroke: "#000",
          "stroke-width": 1.1,
        }),
        svg("rect", { x: cx + 21, y: ay + 17.2, width: 2, height: 2.6, fill: "#000" }),
        svg("rect", { x: cx + 10.2, y: ay + 16.2, width: 4, height: 4.6, fill: "#FFD200" }),
      );
      group.append(
        svg(
          "text",
          {
            class: "cell-lbl",
            id: `cell-lbl-${index}`,
            x: cx,
            y: ay + 38,
            "text-anchor": "middle",
          },
          index < 2 ? `cell-00${index + 1}` : `site-${index + 1}`,
        ),
      );
      figure.append(group);
    }
  }

  function setCell(index, carriers, battery, hit) {
    const group = document.getElementById(`cell-${index}`);
    if (!group) return;
    for (const wave of group.querySelectorAll(".cell-wave")) {
      wave.classList.toggle("on", Number(wave.dataset.k) <= carriers);
    }
    const bat = document.getElementById(`cell-bat-${index}`);
    if (bat) bat.classList.toggle("on", Boolean(battery));
    const label = document.getElementById(`cell-lbl-${index}`);
    if (label) label.classList.toggle("hit", Boolean(hit));
  }

  function latestDecisionAction(state) {
    for (let index = state.negotiation.length - 1; index >= 0; index -= 1) {
      const turn = state.negotiation[index];
      if (turn.side === "oss") return turn.action;
    }
    return null;
  }

  return { render, scene, note, reset, fly };
}

export function setStageCopy(copy) {
  const set = (id, value) => {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  };
  set("zone-label-it", copy.map.zones[0]);
  set("zone-label-net", copy.map.zones[1]);
  set("chan-label", copy.map.channel);
  set("chan-sub1", copy.map.channelSub1);
  set("chan-sub2", copy.map.channelSub2);
  set("lane-task-label", copy.map.laneTask);
  set("lane-status-label", copy.map.laneStatus);
  set("lane-approval-label", copy.map.laneApproval);
  set("eng-name", copy.roles.eng);
  set("eng-org", copy.roles.engOrg);
  set("oss-name", copy.roles.oss);
  set("oss-org", copy.roles.ossOrg);
  set("nms-name", copy.roles.nms);
  set("nms-org", copy.roles.nmsOrg);
  set("nms-note", copy.roles.nmsNote);
  set("approval-name", copy.roles.approval);
  set("approval-org", copy.roles.approvalOrg);
  set("cells-org", copy.roles.cellsOrg);
}
