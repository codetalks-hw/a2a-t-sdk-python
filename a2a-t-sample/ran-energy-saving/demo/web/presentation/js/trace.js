/** Session trace: the merged protocol events and spans of one run.
 *
 * Every row is clickable: it opens the message it carried in the middle panel and stays
 * highlighted. The list follows the newest row until the audience picks a row; after that it keeps
 * the selected row in view instead of jumping to the bottom on every frame.
 */

import { el } from "./dom.js";

const FOLLOW_SLACK_PX = 12;

export function createTrace(els, getCopy, onSelect) {
  let selectedKey = null;
  let follow = true;
  let lastState = null;
  // Programmatic scrolls also fire ``scroll``; ignore them so a click near the bottom does not
  // silently re-enable following and yank the list away on the next frame.
  let quietUntil = 0;

  els.body.addEventListener("scroll", () => {
    if (Date.now() < quietUntil) return;
    follow = atBottom(els.body);
  });

  function scrollTo(value) {
    quietUntil = Date.now() + 150;
    els.body.scrollTop = value;
  }

  function render(state) {
    lastState = state;
    const copy = getCopy();
    const rows = state.trace.slice(-200);
    const offset = state.trace.length - rows.length;
    const scroll = els.body.scrollTop;
    els.rows.replaceChildren();
    els.empty.hidden = rows.length > 0;
    rows.forEach((row, index) => {
      const key = rowKey(row, offset + index);
      els.rows.append(rowNode(row, copy, key, () => select(key, row)));
    });
    for (const node of els.rows.children) {
      node.classList.toggle("sel", node.dataset.key === selectedKey);
    }
    if (follow) {
      scrollTo(els.body.scrollHeight);
    } else {
      scrollTo(scroll);
    }
    const selected = selectNode(els.rows, selectedKey);
    if (selected !== null) {
      quietUntil = Date.now() + 150;
      keepVisible(els.body, selected);
    }
  }

  function select(key, row) {
    selectedKey = key;
    follow = false;
    onSelect(row);
    if (lastState !== null) render(lastState);
  }

  function reset() {
    selectedKey = null;
    follow = true;
  }

  return { render, reset };
}

function rowNode(row, copy, key, open) {
  const node = el("li", "trace-row click");
  node.dataset.key = key;
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  node.addEventListener("click", open);
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });

  const time = el("span", "w", shortTime(row.ts || row.entry?.ts || row.entry?.startedAt));
  const tag = el("span", `tag ${row.tag || "a2at"}`, copy.trace.tags[row.tag] || row.tag || "");
  const text = el("span", "t", (row.titleKey && copy.trace[row.titleKey]) || row.title || "");
  if (row.sub) text.append(el("small", "", row.sub));
  const chip = el("span", row.chipKind ? `chip ${row.chipKind}` : "chip", row.chip || "");
  node.append(time, tag, text, chip);
  return node;
}

/** Stable identity of one row: its span/sequence when it has one, else its position. */
export function rowKey(row, index) {
  const entry = row.entry;
  if (entry) {
    if (entry.spanId) return `span:${entry.spanId}`;
    if (entry.seq !== undefined && entry.seq !== null) return `seq:${entry.seq}`;
  }
  if (row.seq !== undefined && row.seq !== null) return `seq:${row.seq}`;
  return `row:${index}`;
}

function selectNode(container, key) {
  if (key === null) return null;
  for (const node of container.children) {
    if (node.dataset.key === key) return node;
  }
  return null;
}

function atBottom(node) {
  return node.scrollTop + node.clientHeight >= node.scrollHeight - FOLLOW_SLACK_PX;
}

/** Keep one row inside the visible part of the trace body without moving the page. */
function keepVisible(view, node) {
  const rect = node.getBoundingClientRect();
  const frame = view.getBoundingClientRect();
  if (rect.top < frame.top) view.scrollTop += rect.top - frame.top;
  else if (rect.bottom > frame.bottom) view.scrollTop += rect.bottom - frame.bottom;
}

function shortTime(value) {
  if (typeof value !== "string" || value === "") return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(11, 19);
  return date.toLocaleTimeString(undefined, { hour12: false });
}

/** The entry the middle panel shows for one row: the real one, or a readable stand-in. */
export function entryOfRow(row) {
  if (row.entry) return row.entry;
  return {
    entryType: "trace",
    kind: row.tag || "trace",
    title: row.title || "",
    summary: row.sub || "",
    detail: {},
  };
}
