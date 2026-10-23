/** Keyboard map of the Run Explorer.
 *
 * The map is data so the help dialog and the installer cannot drift apart. Typing in a form field
 * suppresses the single-key shortcuts; ``Esc`` and ``Cmd/Ctrl+Enter`` always work.
 */

/** Every shortcut, in help-dialog order. */
export const SHORTCUTS = [
  { keys: "j", description: "Next timeline row" },
  { keys: "k", description: "Previous timeline row" },
  { keys: "Enter", description: "Open the focused/selected row" },
  { keys: "Esc", description: "Close the inspector, dialog or search" },
  { keys: "/", description: "Focus the timeline search" },
  { keys: "f", description: "Toggle follow-new-events" },
  { keys: "1 – 6", description: "Inspector tab (Summary, Protocol, LLM request, LLM response, Tool, Raw JSON)" },
  { keys: "[ / ]", description: "Previous / next step in the inspector" },
  { keys: "?", description: "Show this shortcut list" },
  { keys: "Cmd/Ctrl + Enter", description: "Submit the pending manual decision" },
];

/** Install the keyboard handlers; returns an uninstall function. */
export function installShortcuts(handlers, target = document) {
  const onKeyDown = (event) => {
    if (event.defaultPrevented) return;
    const key = event.key;
    const modified = event.metaKey || event.ctrlKey || event.altKey;

    if (modified && key === "Enter") {
      event.preventDefault();
      handlers.submitDecision?.();
      return;
    }
    if (key === "Escape") {
      handlers.escape?.();
      return;
    }
    if (isTyping(event.target)) return;
    if (modified) return;

    if (key >= "1" && key <= "6") {
      event.preventDefault();
      handlers.tab?.(Number(key));
      return;
    }
    switch (key) {
      case "j":
        event.preventDefault();
        handlers.next?.();
        break;
      case "k":
        event.preventDefault();
        handlers.previous?.();
        break;
      case "Enter":
        event.preventDefault();
        handlers.open?.();
        break;
      case "/":
        event.preventDefault();
        handlers.search?.();
        break;
      case "f":
        event.preventDefault();
        handlers.follow?.();
        break;
      case "[":
        event.preventDefault();
        handlers.previousStep?.();
        break;
      case "]":
        event.preventDefault();
        handlers.nextStep?.();
        break;
      case "?":
        event.preventDefault();
        handlers.help?.();
        break;
      default:
        break;
    }
  };
  target.addEventListener("keydown", onKeyDown);
  return () => target.removeEventListener("keydown", onKeyDown);
}

/** Whether one event target is an editable control. */
export function isTyping(target) {
  if (!target || !(target instanceof Element)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}
