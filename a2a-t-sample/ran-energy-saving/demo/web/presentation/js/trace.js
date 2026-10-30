/** Session trace: the merged protocol events and spans of one run.
 *
 * Every row is clickable: an event row opens the message it carried in the negotiation panel.
 */

import { el } from "./dom.js";

export function createTrace(els, getCopy, onSelect) {
  function render(state) {
    const copy = getCopy();
    const rows = state.trace.slice(-200);
    const scroll = els.body.scrollTop;
    els.rows.replaceChildren();
    els.empty.hidden = rows.length > 0;
    for (const row of rows) {
      els.rows.append(rowNode(row, copy, onSelect));
    }
    if (els.body.dataset.follow !== "false") {
      els.body.scrollTop = els.body.scrollHeight;
    } else {
      els.body.scrollTop = scroll;
    }
  }

  return { render };
}

function rowNode(row, copy, onSelect) {
  const node = el("li", "trace-row");
  const entry = row.entry;
  const clickable = Boolean(entry);
  if (clickable) {
    node.classList.add("click");
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    const open = () => onSelect(entry);
    node.addEventListener("click", open);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  }

  const time = el("span", "w", shortTime(row.ts || entry?.ts || entry?.startedAt));
  const tag = el("span", `tag ${row.tag || "a2at"}`, copy.trace.tags[row.tag] || row.tag || "");
  const text = el("span", "t", (row.titleKey && copy.trace[row.titleKey]) || row.title || "");
  if (row.sub) text.append(el("small", "", row.sub));
  const chip = el("span", row.chipKind ? `chip ${row.chipKind}` : "chip", row.chip || "");
  node.append(time, tag, text, chip);
  return node;
}

function wireEntry(entry) {
  if (!entry || typeof entry !== "object") return null;
  if (entry.entryType === "decision") {
    return {
      side: "oss",
      action: entry.kind,
      summary: entry.summary || "",
      detail: entry.detail || null,
      negotiationType: entry.negotiationType || null,
    };
  }
  if (entry.kind === "propose" || entry.kind === "abort" || entry.kind === "message") {
    return {
      side: "nms",
      action: entry.kind === "propose" ? "propose" : entry.kind,
      summary: entry.summary || "",
      content: entry.content || "",
      negotiationType: entry.negotiationType || null,
    };
  }
  if (entry.entryType === "trace") {
    const detail = entry.detail && typeof entry.detail === "object" ? entry.detail : {};
    const lines = [`kind: ${entry.kind || "trace"}`, `title: ${entry.title || ""}`];
    if (entry.purpose) lines.push(`purpose: ${entry.purpose}`);
    if (detail.toolName) lines.push(`tool: ${detail.toolName}`);
    if (detail.promptText) lines.push("", String(detail.promptText));
    return {
      side: "nms",
      action: "trace",
      summary: entry.title || "",
      content: lines.join("\n"),
      negotiationType: entry.kind || "trace",
    };
  }
  return {
    side: "nms",
    action: entry.kind || "event",
    summary: entry.summary || "",
    content: entry.summary || "",
    negotiationType: entry.negotiationType || null,
  };
}

function shortTime(value) {
  if (typeof value !== "string" || value === "") return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(11, 19);
  return date.toLocaleTimeString(undefined, { hour12: false });
}
