/** Step inspector: Summary / Protocol / LLM request / LLM response / Tool call / Raw JSON.
 *
 * Payloads are fetched lazily: protocol event frames carry the transcript entry, while the full
 * ``event`` envelope (metadata, raw parts) lives in the stored span. Selecting such a row calls the
 * span endpoint once and caches the result.
 *
 * Every value is rendered as a text node or a built element; JSON is shown as a real DOM tree (or a
 * ``<pre>`` with ``textContent``), never as HTML.
 */

import {
  actorClass,
  actorLabel,
  append,
  callout,
  clear,
  el,
  fact,
  statusBadge,
} from "./dom.js";
import {
  EM_DASH,
  countLabel,
  formatClock,
  formatDuration,
  formatJson,
  formatNumber,
  formatTokens,
  parseJsonText,
  shortId,
  truncate,
} from "./format.js";
import { FINAL_STATE_LABELS, PHASE_INDEX } from "./store.js";

/** Tabs of the inspector, in keyboard order. */
export const INSPECTOR_TABS = [
  { key: "summary", label: "Summary" },
  { key: "protocol", label: "Protocol" },
  { key: "llm-request", label: "LLM request" },
  { key: "llm-response", label: "LLM response" },
  { key: "tool", label: "Tool call" },
  { key: "raw", label: "Raw JSON" },
];

/** One-step inspector. */
export class Inspector {
  constructor({ elements, callbacks = {} }) {
    this.elements = elements;
    this.callbacks = callbacks;
    this.store = null;
    this.item = null;
    this.remoteSpan = null;
    this.payloadState = "idle";
    this.payloadError = null;
    this.tab = "summary";
    this.index = -1;
    this.total = 0;
    this.loadToken = 0;
    this.bind();
  }

  /** Wire the static tab and navigation controls. */
  bind() {
    const { tabs } = this.elements;
    for (const button of tabs.querySelectorAll("[data-tab]")) {
      button.addEventListener("click", () => this.selectTab(button.dataset.tab));
    }
    this.elements.prevButton?.addEventListener("click", () => this.callbacks.onStep?.(-1));
    this.elements.nextButton?.addEventListener("click", () => this.callbacks.onStep?.(1));
    this.elements.closeButton?.addEventListener("click", () => this.callbacks.onClose?.());
  }

  /** Show one item, fetching its full span when the frame did not carry it. */
  show(item, { store, index = -1, total = 0 }) {
    this.loadToken += 1;
    this.store = store;
    this.item = item;
    this.index = index;
    this.total = total;
    this.remoteSpan = item?.remoteSpan ?? null;
    this.payloadState = "idle";
    this.payloadError = null;
    this.rawMode = "parsed";
    this.resetScroll = true;
    const available = this.availableTabs();
    if (!available.includes(this.tab)) this.tab = "summary";
    this.elements.root.setAttribute("aria-hidden", "false");
    this.render();
    if (item && this._needsFetch(item)) this._loadRemote(item);
  }

  /** Refresh an item already shown without re-fetching its payload. */
  update(item, { store, index = -1, total = 0 }) {
    if (!item) {
      this.showRun(store, total);
      return;
    }
    if (!this.item || this.item.key !== item.key) {
      this.show(item, { store, index, total });
      return;
    }
    this.store = store;
    this.index = index;
    this.total = total;
    this.render();
  }

  /** Show the run summary (no step selected). */
  showRun(store, total = 0) {
    this.loadToken += 1;
    this.store = store;
    this.item = null;
    this.remoteSpan = null;
    this.index = -1;
    this.total = total;
    this.tab = "summary";
    this.resetScroll = true;
    this.elements.root.setAttribute("aria-hidden", "false");
    this.render();
  }

  /** Close the inspector. */
  hide() {
    this.item = null;
    this.remoteSpan = null;
    this.index = -1;
    this.elements.root.setAttribute("aria-hidden", "true");
  }

  /** Select one tab by key, ignoring unavailable tabs. */
  selectTab(key) {
    if (!INSPECTOR_TABS.some((tab) => tab.key === key)) return;
    if (!this.availableTabs().includes(key)) return;
    this.tab = key;
    this.render();
  }

  /** Select the tab at a one-based keyboard position (1–6). */
  selectTabByNumber(number) {
    const tab = INSPECTOR_TABS[number - 1];
    if (tab) this.selectTab(tab.key);
  }

  /** Return the tabs available for the current item. */
  availableTabs() {
    if (!this.item) return ["summary", "raw"];
    const tabs = ["summary"];
    if (this._hasProtocol(this.item)) tabs.push("protocol");
    if (this.item.kind === "llm") tabs.push("llm-request", "llm-response");
    if (this.item.kind === "tool") tabs.push("tool");
    tabs.push("raw");
    return tabs;
  }

  /** Render the header, tab state and active body. */
  render() {
    const { root, stepLabel, kindLabel, tabs, body, prevButton, nextButton } = this.elements;
    const item = this.item;
    const previousScroll = this.resetScroll ? 0 : body.scrollTop;
    const available = new Set(this.availableTabs());
    for (const button of tabs.querySelectorAll("[data-tab]")) {
      const enabled = available.has(button.dataset.tab);
      button.disabled = !enabled;
      button.setAttribute("aria-selected", String(button.dataset.tab === this.tab));
      button.title = enabled ? button.textContent : `${button.textContent} is not recorded for this step`;
    }
    prevButton.disabled = this.index <= 0;
    nextButton.disabled = this.index < 0 || this.index >= this.total - 1;

    for (const actor of ["client", "server", "llm", "tool", "human", "system"]) {
      root.classList.remove(`actor-${actor}`);
    }
    if (!item) {
      stepLabel.textContent = "RUN SUMMARY";
      kindLabel.textContent = this.store?.runId ? `Run ${shortId(this.store.runId)}` : "No run selected";
      root.dataset.kind = "run";
      clear(body);
      body.appendChild(this._runSummary());
      body.scrollTop = previousScroll;
      this.resetScroll = false;
      return;
    }

    stepLabel.textContent = `STEP ${String(item.number).padStart(2, "0")} / ${this.total || "?"} · P${PHASE_INDEX[item.phase] || "?"}.${item.phaseStep ?? "—"}`;
    const kindParts = [actorLabel(item.actor), item.kind || "event"];
    if (Number.isInteger(item.round)) kindParts.push(`R${item.round}`);
    kindLabel.textContent = kindParts.join(" · ");
    root.dataset.kind = item.kind || "event";
    root.classList.add(actorClass(item.actor));

    clear(body);
    const payload = this.remoteSpan || item.payload || item;
    switch (this.tab) {
      case "protocol":
        body.appendChild(this._protocolTab(item, payload));
        break;
      case "llm-request":
        body.appendChild(this._llmRequestTab(item));
        break;
      case "llm-response":
        body.appendChild(this._llmResponseTab(item));
        break;
      case "tool":
        body.appendChild(this._toolTab(item));
        break;
      case "raw":
        body.appendChild(this._rawTab(item, payload));
        break;
      default:
        body.appendChild(this._summaryTab(item));
        break;
    }
    body.scrollTop = previousScroll;
    this.resetScroll = false;
  }

  /** Fetch the stored span of one item that did not carry its payload. */
  async _loadRemote(item) {
    const token = ++this.loadToken;
    this.payloadState = "loading";
    this.payloadError = null;
    if (!this.item || this.item.key !== item.key) return;
    try {
      const span = await this.callbacks.fetchSpan?.(item.spanId);
      if (token !== this.loadToken) return;
      if (span) {
        this.remoteSpan = span;
        this.store?.mergeSpan(item.spanId, span);
      }
      this.payloadState = "ready";
    } catch (error) {
      if (token !== this.loadToken) return;
      this.payloadState = "error";
      this.payloadError = String(error?.message ?? error);
    }
    if (this.item && this.item.key === item.key) this.render();
  }

  /** Whether one item needs a lazy span request. */
  _needsFetch(item) {
    if (!item.spanId || item.source === "trace") return false;
    if (item.detailLoaded) return false;
    return true;
  }

  /** Whether one item exposes the Protocol tab. */
  _hasProtocol(item) {
    if (item.source === "run_started" || item.source === "finished") return true;
    if (item.source === "event") return true;
    const detail = this.remoteSpan?.detail ?? item.detail ?? {};
    return Boolean(
      detail.event ||
        detail.performative ||
        detail.templateUri ||
        detail.runStarted ||
        detail.finished ||
        detail.metadata ||
        item.templateUri ||
        item.performative,
    );
  }

  /** Render the run-level summary shown before the first selection. */
  _runSummary() {
    const store = this.store;
    const root = el("div", "inspector-run");
    if (!store || !store.runId) {
      root.appendChild(
        callout(
          "No run loaded",
          "Start a run from the left rail. Every phase, protocol message, LLM call and tool call is numbered here in causal order.",
        ),
      );
      return root;
    }
    const meta = store.runMeta();
    const ids = store.ids();
    const analysis = store.derived().analysis;
    const decisionLog = store.derived().decisionLog;
    const pulses = this._milestones(store);

    root.appendChild(el("h2", "inspector-title", meta.scenario ? `Run · ${meta.scenario}` : "Run summary"));
    if (meta.description) root.appendChild(el("p", "inspector-lede", meta.description));
    if (meta.userRequest) root.appendChild(this._textBlock("User request", meta.userRequest));

    const story = el("section", "inspector-section");
    story.appendChild(el("h3", "inspector-section-title", "Story"));
    const list = el("ol", "story-list");
    for (const item of pulses) {
      const row = el("li", "story-row");
      const button = el("button", "story-button");
      button.type = "button";
      append(
        button,
        el("span", "story-number", `#${String(item.number).padStart(2, "0")}`),
        el("span", "story-text", item.title),
      );
      button.addEventListener("click", () => this.callbacks.onSelectKey?.(item.key));
      row.appendChild(button);
      list.appendChild(row);
    }
    story.appendChild(list);
    root.appendChild(story);

    const facts = el("dl", "facts-grid");
    append(
      facts,
      fact("Outcome", FINAL_STATE_LABELS[store.finishedFrame?.finalState] || store.finishedFrame?.finalState || EM_DASH),
      fact("Status", store.status),
      fact("Context", shortId(ids.contextId)),
      fact("Task", shortId(ids.taskId)),
      fact("Wall clock", formatClock(runElapsed(store))),
      fact("LLM", `${countLabel(analysis.llmCalls, "call")}${analysis.scriptedCalls ? ` · ${analysis.scriptedCalls} scripted` : ""}`),
      fact("Tokens", tokenSummary(analysis)),
      fact("Tools", `${countLabel(analysis.tools, "call")} · ${analysis.toolErrors} errors`),
      fact("Errors / retries", `${analysis.errors} / ${analysis.retries}`),
      fact("Decisions", formatNumber(analysis.decisions)),
    );
    root.appendChild(facts);

    const phases = el("section", "inspector-section");
    phases.appendChild(el("h3", "inspector-section-title", "Phase durations"));
    phases.appendChild(phaseDurationList(store));
    root.appendChild(phases);

    const decisions = el("section", "inspector-section");
    decisions.appendChild(el("h3", "inspector-section-title", "Decision log"));
    if (decisionLog.length === 0) {
      decisions.appendChild(el("p", "placeholder", "No decisions recorded."));
    } else {
      const log = el("ol", "inspector-decisions");
      for (const entry of decisionLog) {
        const row = el("li", "inspector-decision");
        append(
          row,
          el("span", "decision-round", entry.round ? `R${entry.round}` : "—"),
          el("span", "decision-action", String(entry.action || "decision").toUpperCase()),
          el("span", "decision-source", entry.source),
          el("span", "decision-rationale", truncate(entry.rationale, 140)),
        );
        log.appendChild(row);
      }
      decisions.appendChild(log);
    }
    root.appendChild(decisions);
    return root;
  }

  /** Return up to eight clickable milestones of the run. */
  _milestones(store) {
    const items = store.list();
    const chosen = [];
    const seenPhases = new Set();
    for (const item of items) {
      const isMilestone =
        item.source === "run_started" ||
        item.entryType === "decision" ||
        item.kind === "message_sent" ||
        item.kind === "task_prompt_rendered" ||
        item.kind === "llm" ||
        item.kind === "tool" ||
        item.source === "finished";
      if (!isMilestone) continue;
      const phaseSeen = seenPhases.has(item.phase);
      if (item.kind === "llm" || item.kind === "tool") {
        if (phaseSeen) continue;
      }
      seenPhases.add(item.phase);
      chosen.push(item);
      if (chosen.length >= 8) break;
    }
    return chosen;
  }

  /** Render the Summary tab. */
  _summaryTab(item) {
    const root = el("div", "inspector-tab inspector-summary");
    const heading = el("div", "inspector-title-row");
    append(
      heading,
      el("h2", "inspector-title", item.title),
      el("span", `actor-badge ${actorClass(item.actor)}`, actorLabel(item.actor)),
      statusBadge(item.status),
    );
    if (Number.isInteger(item.round)) heading.appendChild(el("span", "round-chip", `R${item.round}`));
    root.appendChild(heading);

    if (item.summary && item.summary !== item.title) root.appendChild(el("p", "inspector-lede", item.summary));
    if (item.purpose) root.appendChild(this._textBlock("Why this step", item.purpose));
    if (item.rationale && item.rationale !== item.summary) {
      root.appendChild(this._textBlock("Rationale", item.rationale));
    }

    const facts = el("dl", "facts-grid");
    append(
      facts,
      fact("Global step", `#${String(item.number).padStart(2, "0")}`),
      fact("Phase-local", `P${PHASE_INDEX[item.phase] || "?"}.${item.phaseStep ?? EM_DASH}`),
      fact("Phase", item.phase),
      fact("Actor / side", `${item.actor}${item.side ? ` · ${item.side}` : ""}`),
      fact("Kind", item.kind),
      item.stage ? fact("Stage", item.stage) : null,
      item.durationMs !== null ? fact("Duration", formatDuration(item.durationMs)) : null,
      item.kind === "llm" ? fact("Model", item.model || EM_DASH) : null,
      item.kind === "llm" ? fact("Tokens", formatTokens(item.tokens)) : null,
      item.direction ? fact("Direction", item.direction) : null,
      item.round !== null && item.round !== undefined ? fact("Round", String(item.round)) : null,
      item.contextId ? fact("Context", item.contextId) : null,
      item.taskId ? fact("Task", item.taskId) : null,
      item.templateUri ? fact("Template", item.templateUri) : null,
      item.startedAt ? fact("Started", item.startedAt) : null,
      item.endedAt ? fact("Ended", item.endedAt) : null,
    );
    root.appendChild(facts);

    if (item.entryType === "decision" || item.action) {
      const decision = el("section", "inspector-section");
      decision.appendChild(el("h3", "inspector-section-title", "Decision"));
      const decisionFacts = el("dl", "facts-grid");
      append(
        decisionFacts,
        fact("Action", item.action || item.kind),
        item.filledParams ? fact("Filled params", JSON.stringify(item.filledParams)) : null,
        item.counterProposal ? fact("Counter proposal", item.counterProposal) : null,
        item.counterParams ? fact("Counter params", JSON.stringify(item.counterParams)) : null,
        item.confirmedIntent ? fact("Confirmed intent", item.confirmedIntent) : null,
        item.feasibilitySummary ? fact("Feasibility", item.feasibilitySummary) : null,
      );
      decision.appendChild(decisionFacts);
      root.appendChild(decision);
    }

    if (item.source === "run_started" || item.source === "finished") {
      const detail = this.remoteSpan?.detail ?? item.detail ?? {};
      const run = detail.runStarted || item.payload || {};
      if (run.description) root.appendChild(this._textBlock("Scenario", run.description));
      if (run.userRequest) root.appendChild(this._textBlock("User request", run.userRequest));
      if (Array.isArray(run.expectedSequence) && run.expectedSequence.length > 0) {
        const expected = el("section", "inspector-section");
        expected.appendChild(el("h3", "inspector-section-title", "Expected sequence"));
        const list = el("ol", "expected-list");
        for (const step of run.expectedSequence) {
          list.appendChild(
            el("li", "", `${step.actor} · ${step.performative}${step.round ? ` · R${step.round}` : ""}`),
          );
        }
        expected.appendChild(list);
        root.appendChild(expected);
      }
      const finished = detail.finished || (item.source === "finished" ? item.payload : null);
      if (finished) {
        if (finished.finalState) {
          root.appendChild(this._textBlock("Final state", String(finished.finalState)));
        }
        if (finished.report) root.appendChild(this._jsonSection("Report", finished.report, true));
        if (finished.error) root.appendChild(callout("Run error", String(finished.error), "error"));
      }
    }

    const causal = this._causal(item);
    if (causal) root.appendChild(causal);
    return root;
  }

  /** Render previous/next causal links. */
  _causal(item) {
    if (!this.store) return null;
    const items = this.store.list();
    const position = items.findIndex((candidate) => candidate.key === item.key);
    if (position < 0) return null;
    const section = el("section", "inspector-section");
    section.appendChild(el("h3", "inspector-section-title", "Causal order"));
    const list = el("div", "causal-links");
    for (const [label, offset] of [["Previous", -1], ["Next", 1]]) {
      const neighbour = items[position + offset];
      if (!neighbour) continue;
      const button = el("button", "causal-button");
      button.type = "button";
      append(
        button,
        el("span", "causal-label", label),
        el("span", "causal-title", `#${String(neighbour.number).padStart(2, "0")} ${truncate(neighbour.title, 60)}`),
      );
      button.addEventListener("click", () => this.callbacks.onSelectKey?.(neighbour.key));
      list.appendChild(button);
    }
    section.appendChild(list);
    return section;
  }

  /** Render the Protocol tab. */
  _protocolTab(item, payload) {
    const root = el("div", "inspector-tab inspector-protocol");
    const detail = this.remoteSpan?.detail ?? item.detail ?? {};
    const event = detail.event || payload?.event || null;
    const metadata = event?.metadata && typeof event.metadata === "object" ? event.metadata : null;
    const facts = el("dl", "facts-grid");
    append(
      facts,
      fact("Direction", item.direction || EM_DASH),
      fact("Phase", item.phase),
      fact("Actor", item.actor),
      item.performative ? fact("Performative", item.performative) : null,
      item.negotiationType ? fact("Negotiation", item.negotiationType) : null,
      item.templateUri ? fact("Template URI", item.templateUri) : null,
      event?.message_id ? fact("Message id", event.message_id) : null,
      event?.task_id || item.taskId ? fact("Task id", event?.task_id || item.taskId) : null,
      event?.context_id || item.contextId ? fact("Context id", event?.context_id || item.contextId) : null,
      event?.kind ? fact("A2A kind", event.kind) : null,
      event?.state ? fact("Task state", event.state) : null,
      event?.name ? fact("Artifact", event.name) : null,
      event?.artifact_id ? fact("Artifact id", event.artifact_id) : null,
    );
    root.appendChild(facts);

    if (this.payloadState === "loading") {
      root.appendChild(callout("Loading payload…", "Reading the stored span envelope.", "muted"));
    } else if (this.payloadState === "error") {
      root.appendChild(
        callout("Payload unavailable", this.payloadError || "The span could not be read.", "warn"),
      );
    }

    if (item.entryType === "decision" || item.action) {
      const decision = {
        action: item.action || item.kind,
        rationale: item.rationale || item.summary || null,
        filled_params: item.filledParams || null,
        counter_proposal: item.counterProposal || null,
        counter_params: item.counterParams || null,
        confirmed_intent: item.confirmedIntent || null,
        feasibility_summary: item.feasibilitySummary || null,
      };
      root.appendChild(this._jsonSection("Decision payload", decision, true));
    }

    if (event) {
      const negotiationContext = metadata ? findKey(metadata, "negotiationContext") : null;
      if (negotiationContext !== null) {
        root.appendChild(this._jsonSection("Negotiation context", negotiationContext, true));
      }
      const negotiationText = metadata ? findTextMetadata(metadata, "Negotiation-T") : null;
      if (negotiationText) root.appendChild(this._textBlock("Negotiation-T text", negotiationText));
      const taskText = metadata ? findTextMetadata(metadata, "Task-T") : null;
      if (taskText) root.appendChild(this._textBlock("Task-T text", taskText));
      if (event.text) root.appendChild(this._textBlock("Message text", event.text));
      if (event.data) root.appendChild(this._jsonSection("Artifact data", event.data, true));
      if (event.metadata) root.appendChild(this._jsonSection("Metadata", event.metadata));
      if (event.parts) root.appendChild(this._jsonSection("Raw parts", event.parts));
      root.appendChild(this._jsonSection("Raw event", event));
    } else if (item.source === "run_started" || detail.runStarted) {
      root.appendChild(this._jsonSection("Run started", detail.runStarted || detail, true));
    } else if (item.source === "finished" || detail.finished) {
      root.appendChild(this._jsonSection("Finished", detail.finished || detail, true));
    } else {
      const summary = {
        direction: item.direction,
        performative: item.performative,
        templateUri: item.templateUri,
        detail,
      };
      root.appendChild(this._jsonSection("Protocol detail", summary));
    }

    if (detail._traceTruncated) {
      root.appendChild(callout("Payload truncated", "The backend truncated this payload to keep the trace small.", "warn"));
    }
    return root;
  }

  /** Render the LLM request tab. */
  _llmRequestTab(item) {
    const root = el("div", "inspector-tab inspector-llm-request");
    const detail = this.remoteSpan?.detail ?? item.detail ?? {};
    const request = detail.request;
    root.appendChild(this._llmIdentity(item, request));

    if (this.payloadState === "loading") {
      root.appendChild(callout("Loading payload…", "Reading the stored LLM span.", "muted"));
      return root;
    }
    if (!request || typeof request !== "object") {
      root.appendChild(callout("No LLM request captured", "This span does not carry a request payload.", "muted"));
      return root;
    }

    const system = systemInstructionText(request);
    if (system) root.appendChild(this._collapsibleText("System instruction", system));

    const messages = request.messages;
    const contents = request.contents;
    if (Array.isArray(messages) && messages.length > 0) {
      root.appendChild(this._messageList(messages, "Messages"));
    } else if (Array.isArray(contents) && contents.length > 0) {
      root.appendChild(this._messageList(contents, "Contents"));
    } else {
      root.appendChild(el("p", "placeholder", "No messages or contents were recorded."));
    }

    const schema = request.jsonSchema ?? request.responseSchema;
    if (schema !== null && schema !== undefined) {
      root.appendChild(this._jsonSection("JSON schema", schema));
    }
    if (Array.isArray(request.tools) && request.tools.length > 0) {
      root.appendChild(this._toolDeclarations(request.tools));
    }
    root.appendChild(this._generationParams(item, request));

    const actions = el("div", "inspector-actions");
    actions.appendChild(
      this._actionButton("Copy request JSON", () => this._copy(request, "LLM request copied.")),
    );
    root.appendChild(actions);
    return root;
  }

  /** Render the LLM response tab. */
  _llmResponseTab(item) {
    const root = el("div", "inspector-tab inspector-llm-response");
    const detail = this.remoteSpan?.detail ?? item.detail ?? {};
    const response = detail.response;
    root.appendChild(this._llmIdentity(item, detail.request));

    if (this.payloadState === "loading") {
      root.appendChild(callout("Loading payload…", "Reading the stored LLM span.", "muted"));
      return root;
    }
    if (!response || typeof response !== "object") {
      root.appendChild(
        callout(
          item.error ? "LLM call failed" : "No LLM response captured",
          item.error || "The provider returned no response payload for this call.",
          item.error ? "error" : "muted",
        ),
      );
      return root;
    }

    const facts = el("dl", "facts-grid");
    append(
      facts,
      fact("Status", item.status || EM_DASH),
      fact("Model", response.model || response.modelVersion || item.model || EM_DASH),
      fact("Latency", formatDuration(item.durationMs)),
      fact("Tokens", formatTokens(item.tokens)),
      response.finishReason ? fact("Finish reason", String(response.finishReason)) : null,
    );
    root.appendChild(facts);

    const raw = typeof response.rawContent === "string" ? response.rawContent : null;
    const text = typeof response.text === "string" ? response.text : null;
    const parsedCandidate = raw ?? text;
    const parsed = response.parsed ?? (parsedCandidate ? parseJsonText(parsedCandidate).value : null);
    const hasPayload = Boolean(raw || text || response.content);
    if (hasPayload) {
      const rawMode = this.rawMode ?? "parsed";
      const toggle = el("div", "segmented small");
      for (const [key, label] of [["raw", "Raw"], ["parsed", "Parsed"]]) {
        const button = el("button", `segment${key === rawMode ? " is-active" : ""}`, label);
        button.type = "button";
        button.setAttribute("aria-pressed", String(key === rawMode));
        button.addEventListener("click", () => {
          this.rawMode = key;
          this.render();
        });
        toggle.appendChild(button);
      }
      root.appendChild(toggle);
      if (rawMode === "raw") {
        root.appendChild(this._pre(raw || text || formatJson(response.content)));
      } else if (parsed !== null && parsed !== undefined) {
        root.appendChild(this._jsonTreeSection("Parsed response", parsed));
      } else if (response.content) {
        root.appendChild(this._jsonTreeSection("Response content", response.content));
      } else {
        root.appendChild(el("p", "placeholder", "The response is not valid JSON."));
      }
    }

    const calls = Array.isArray(response.functionCalls) ? response.functionCalls : [];
    if (calls.length > 0) {
      const section = el("section", "inspector-section");
      section.appendChild(el("h3", "inspector-section-title", "Function calls"));
      for (const call of calls) {
        section.appendChild(this._jsonTreeSection(call.name || "function call", call));
      }
      root.appendChild(section);
    }

    if (response.parseError) {
      root.appendChild(callout("JSON parse failure", response.parseError, "error"));
    }
    if (response.errorMessage) {
      root.appendChild(callout("Provider error", response.errorMessage, "error"));
    }
    if (item.error) root.appendChild(callout("Call error", item.error, "error"));
    if (response.usage) root.appendChild(this._jsonSection("Usage", response.usage, true));

    const actions = el("div", "inspector-actions");
    actions.appendChild(
      this._actionButton("Copy response JSON", () => this._copy(response, "LLM response copied.")),
    );
    root.appendChild(actions);
    return root;
  }

  /** Render the Tool call tab. */
  _toolTab(item) {
    const root = el("div", "inspector-tab inspector-tool");
    const detail = this.remoteSpan?.detail ?? item.detail ?? {};
    const toolName = detail.toolName || item.title.replace(/^Tool call · /, "");
    if (this.payloadState === "loading") {
      root.appendChild(callout("Loading payload…", "Reading the stored tool span.", "muted"));
    }
    const facts = el("dl", "facts-grid");
    append(
      facts,
      fact("Tool", toolName || EM_DASH),
      fact("Stage", detail.stage || item.stage || EM_DASH),
      fact("Side", item.side || EM_DASH),
      fact("Duration", formatDuration(item.durationMs)),
      fact("Status", item.status || EM_DASH),
      item.error ? fact("Error", item.error) : null,
      detail.agent ? fact("Agent", detail.agent) : null,
    );
    root.appendChild(facts);
    root.appendChild(this._jsonSection("Arguments", detail.args ?? null, true));
    root.appendChild(this._jsonSection("Result", detail.result ?? null, true));

    if (item.parentSpanId) {
      const parent = this.store?.getItemBySpan(item.parentSpanId);
      if (parent) {
        const actions = el("div", "inspector-actions");
        const button = this._actionButton(
          `Open the calling LLM (#${String(parent.number).padStart(2, "0")})`,
          () => this.callbacks.onSelectKey?.(parent.key),
        );
        actions.appendChild(button);
        root.appendChild(actions);
      }
    }
    if (item.error) root.appendChild(callout("Tool error", item.error, "error"));
    return root;
  }

  /** Render the Raw JSON tab. */
  _rawTab(item, payload) {
    const root = el("div", "inspector-tab inspector-raw");
    const raw = this.remoteSpan || payload || item;
    const actions = el("div", "inspector-actions");
    append(
      actions,
      this._actionButton("Copy JSON", () => this._copy(raw, "Span JSON copied.")),
      this._actionButton("Download JSON", () => this._download(`${item.spanId || item.key}.json`, raw)),
    );
    root.appendChild(actions);
    if (this.payloadState === "loading") {
      root.appendChild(callout("Loading payload…", "Reading the stored span envelope.", "muted"));
    }
    if (this.payloadState === "error") {
      root.appendChild(callout("Payload unavailable", this.payloadError || "The span could not be read.", "warn"));
    }
    root.appendChild(this._pre(formatJson(raw)));
    return root;
  }

  /** Render the identity strip shared by both LLM tabs. */
  _llmIdentity(item, request) {
    const facts = el("dl", "facts-grid identity-strip");
    append(
      facts,
      fact("Stage", item.stage || EM_DASH),
      fact("Purpose", item.purpose || (request && request.model) || EM_DASH),
      fact("Provider", item.detail?.provider || (request && request.model) || EM_DASH),
      fact("Side / round", `${item.side || EM_DASH}${Number.isInteger(item.round) ? ` · R${item.round}` : ""}`),
      fact("Latency", formatDuration(item.durationMs)),
      fact("Tokens", formatTokens(item.tokens)),
    );
    return facts;
  }

  /** Render one message list (SDK messages or ADK contents). */
  _messageList(messages, title) {
    const section = el("section", "inspector-section");
    section.appendChild(el("h3", "inspector-section-title", title));
    for (const message of messages) {
      const block = el("div", "message-block");
      const role = message.role || "unknown";
      block.appendChild(el("span", `role-chip role-${role}`, role));
      if (typeof message.content === "string") {
        block.appendChild(this._pre(message.content));
      } else if (Array.isArray(message.parts)) {
        for (const part of message.parts) {
          if (typeof part.text === "string") block.appendChild(this._pre(part.text));
          else block.appendChild(this._jsonTreeSection(part.functionCall ? "functionCall" : "part", part));
        }
      } else {
        block.appendChild(this._jsonTreeSection("message", message));
      }
      section.appendChild(block);
    }
    return section;
  }

  /** Render the tool declarations of one LLM request. */
  _toolDeclarations(tools) {
    const section = el("section", "inspector-section");
    section.appendChild(el("h3", "inspector-section-title", `Tools (${tools.length})`));
    for (const tool of tools) {
      const block = el("div", "tool-declaration");
      block.appendChild(el("h4", "tool-name", tool.name || tool.tool || "tool"));
      if (tool.description) block.appendChild(el("p", "tool-description", tool.description));
      if (tool.parameters !== undefined) block.appendChild(this._jsonTreeSection("Parameters", tool.parameters));
      section.appendChild(block);
    }
    return section;
  }

  /** Render the generation parameters. */
  _generationParams(item, request) {
    const facts = el("dl", "facts-grid");
    const params = {};
    for (const key of ["model", "temperature", "topP", "maxTokens", "maxOutputTokens", "labels", "messageCount"]) {
      if (request[key] !== undefined && request[key] !== null) params[key] = request[key];
    }
    for (const key of ["temperature", "maxTokens", "topP", "labels"]) {
      if (item.detail?.[key] !== undefined && params[key] === undefined) params[key] = item.detail[key];
    }
    for (const [key, value] of Object.entries(params)) {
      facts.appendChild(fact(key, typeof value === "object" ? JSON.stringify(value) : String(value)));
    }
    if (facts.childElementCount === 0) return el("p", "placeholder", "No generation parameters were recorded.");
    const section = el("section", "inspector-section");
    section.appendChild(el("h3", "inspector-section-title", "Generation parameters"));
    section.appendChild(facts);
    return section;
  }

  /** Render a labelled text block with a scrollable pre. */
  _textBlock(title, text) {
    const section = el("section", "inspector-section");
    section.appendChild(el("h3", "inspector-section-title", title));
    section.appendChild(this._pre(text));
    return section;
  }

  /** Render a collapsible text block. */
  _collapsibleText(title, text) {
    const details = el("details", "inspector-section collapsible");
    details.appendChild(el("summary", "inspector-section-title", title));
    details.appendChild(this._pre(text));
    return details;
  }

  /** Render a labelled JSON tree that starts open. */
  _jsonSection(title, value, open = false) {
    const section = el("section", "inspector-section");
    section.appendChild(el("h3", "inspector-section-title", title));
    section.appendChild(this._jsonTree(value, open));
    return section;
  }

  /** Render a collapsible JSON tree section. */
  _jsonTreeSection(title, value) {
    const details = el("details", "json-section collapsible");
    details.appendChild(el("summary", "json-section-title", title));
    details.appendChild(this._jsonTree(value, true));
    return details;
  }

  /** Build a DOM tree of one JSON value. */
  _jsonTree(value, open = false, depth = 0) {
    if (value === null) return el("span", "json-null", "null");
    if (typeof value === "boolean") return el("span", "json-boolean", String(value));
    if (typeof value === "number") return el("span", "json-number", formatNumber(value));
    if (typeof value === "string") {
      const text = value.length > 4000 ? `${value.slice(0, 4000)}… [${value.length - 4000} chars]` : value;
      return el("span", "json-string", text);
    }
    if (Array.isArray(value)) {
      if (value.length === 0) return el("span", "json-empty", "[]");
      const details = el("details", "json-tree");
      details.open = open || depth < 1;
      details.appendChild(el("summary", "json-summary", `Array(${value.length})`));
      const list = el("ol", "json-array");
      const limit = Math.min(value.length, 200);
      for (let index = 0; index < limit; index += 1) {
        const row = el("li", "json-row");
        append(row, el("span", "json-key", String(index)), this._jsonTree(value[index], false, depth + 1));
        list.appendChild(row);
      }
      if (value.length > limit) list.appendChild(el("li", "json-more", `… ${value.length - limit} more items`));
      details.appendChild(list);
      return details;
    }
    if (typeof value === "object") {
      const keys = Object.keys(value);
      if (keys.length === 0) return el("span", "json-empty", "{}");
      const details = el("details", "json-tree");
      details.open = open || depth < 1;
      details.appendChild(el("summary", "json-summary", `Object(${keys.length})`));
      const list = el("dl", "json-object");
      for (const key of keys) {
        const row = el("div", "json-row");
        append(row, el("dt", "json-key", key), this._jsonTree(value[key], false, depth + 1));
        list.appendChild(row);
      }
      details.appendChild(list);
      return details;
    }
    return el("span", "json-empty", String(value));
  }

  /** Render one scrollable preformatted block. */
  _pre(text) {
    const pre = el("pre", "payload");
    pre.textContent = typeof text === "string" ? text : formatJson(text);
    return pre;
  }

  /** Render one action button. */
  _actionButton(label, handler) {
    const button = el("button", "secondary-button", label);
    button.type = "button";
    button.addEventListener("click", () => {
      Promise.resolve(handler()).catch((error) => this.callbacks.notify?.(String(error?.message ?? error)));
    });
    return button;
  }

  /** Copy a value as JSON through the clipboard API, with a textarea fallback. */
  async _copy(value, message) {
    const text = typeof value === "string" ? value : formatJson(value);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = el("textarea", "sr-only");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    this.callbacks.notify?.(message);
  }

  /** Download a value as a JSON file. */
  _download(name, value) {
    const text = typeof value === "string" ? value : formatJson(value);
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = el("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    this.callbacks.notify?.("Download started.");
  }
}

/** Return the text of one ADK system instruction (string or Content). */
function systemInstructionText(request) {
  const instruction = request.systemInstruction;
  if (typeof instruction === "string" && instruction) return instruction;
  if (instruction && typeof instruction === "object") {
    if (Array.isArray(instruction.parts)) {
      const text = instruction.parts.map((part) => part.text || "").join("\n").trim();
      if (text) return text;
    }
    return formatJson(instruction);
  }
  const messages = Array.isArray(request.messages) ? request.messages : [];
  const systems = messages.filter((message) => message.role === "system" && typeof message.content === "string");
  return systems.length > 0 ? systems.map((message) => message.content).join("\n\n") : null;
}

/** Find the first metadata key containing a marker. */
function findKey(metadata, marker) {
  if (!metadata || typeof metadata !== "object") return null;
  for (const [key, value] of Object.entries(metadata)) {
    if (key.includes(marker)) return value;
  }
  return null;
}

/** Find the first string metadata value whose key contains a marker. */
function findTextMetadata(metadata, marker) {
  if (!metadata || typeof metadata !== "object") return null;
  for (const [key, value] of Object.entries(metadata)) {
    if (key.includes(marker) && typeof value === "string" && value) return value;
  }
  return null;
}

/** Summarize the token analysis of the rail. */
function tokenSummary(analysis) {
  if (analysis.llmCalls === 0) return EM_DASH;
  if (analysis.numericCalls === 0) return "n/a (scripted)";
  return `${formatNumber(analysis.promptTokens)} in / ${formatNumber(analysis.completionTokens)} out`;
}

/** Render the phase duration bars of the rail. */
export function phaseDurationList(store) {
  const derived = store.derived();
  const list = el("ul", "phase-durations");
  const max = Math.max(
    1,
    ...derived.groups.map((group) => group.stats.durationMs ?? 0),
  );
  for (const group of derived.groups) {
    const row = el("li", "phase-duration");
    const width = group.stats.durationMs ? Math.max(3, (group.stats.durationMs / max) * 100) : 0;
    const bar = el("span", "phase-duration-bar");
    bar.style.width = `${width}%`;
    append(
      row,
      el("span", "phase-duration-label", group.short),
      bar,
      el("span", "phase-duration-value", group.stats.durationMs ? formatDuration(group.stats.durationMs) : "—"),
    );
    list.appendChild(row);
  }
  return list;
}

/** Return the run wall clock of one store. */
export function runElapsed(store) {
  const start = store.runStarted?.startedAt;
  if (!start) return 0;
  const startMs = Date.parse(start);
  const finished = store.finishedFrame;
  let endMs = Date.now();
  const snapshotFinished = store.snapshot?.finishedAt ?? store.snapshot?.transcript?.finishedAt;
  if (finished && snapshotFinished) endMs = Date.parse(snapshotFinished);
  else if (store.status === "finished" || store.status === "failed") {
    const last = store.list()[store.list().length - 1];
    if (last?.startedAt) endMs = Date.parse(last.startedAt);
  }
  if (Number.isNaN(startMs) || Number.isNaN(endMs)) return 0;
  return Math.max(0, endMs - startMs);
}
