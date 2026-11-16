/* shared/focus-keep.js — restore keyboard focus across wholesale DOM rebuilds, pure.
 *
 * The match page rebuilds the action row (and the level chips) on every WS frame and
 * action response. Clearing a container with textContent = "" also drops the focused
 * node, so keyboard users get thrown back to the top of the page on each refresh.
 *
 * Approach: record the focused control's stable key before the rebuild, then find the
 * same key afterwards. If the control is now disabled (busy) or gone, focus the
 * container itself (tabindex="-1") so the user stays in context. A memo carries the
 * key across a disabled rebuild so the control is refocused once it is enabled again.
 * This module is DOM-free apart from reading document.activeElement and calling
 * focus(), so it can be tested with plain object stand-ins.
 */

const MEMO = { key: "" };

/** Reset the memo (tests / page re-init). */
export function resetFocusMemo() {
  MEMO.key = "";
}

/** Key of the focused element when it sits inside one of `scopes`; "" otherwise.
 * An element without data-act (e.g. the container itself) falls back to the memo, so
 * a rebuild that began while the control was disabled can still find its way back. */
export function focusKeyOf(scopes, activeElement, getAttr = (node) => node.getAttribute("data-act")) {
  const active = activeElement;
  if (!active || typeof active.getAttribute !== "function") return "";
  const inside = (Array.isArray(scopes) ? scopes : []).some(
    (scope) => scope && typeof scope.contains === "function" && scope.contains(active)
  );
  if (!inside) {
    MEMO.key = ""; // focus left the managed area: stop restoring it
    return "";
  }
  return getAttr(active) || MEMO.key || "";
}

/** Focus the control carrying `key` within `scopes`; fall back to the first scope
 * element that is focusable (not hidden). Returns true when focus was moved. */
export function restoreFocus(key, scopes, { hiddenClass = "hidden" } = {}) {
  if (!key) return false;
  const list = Array.isArray(scopes) ? scopes : [];
  for (const scope of list) {
    if (!scope || typeof scope.querySelector !== "function") continue;
    const next = scope.querySelector(`[data-act="${key}"]`);
    if (!next) continue;
    MEMO.key = key;
    if (!next.disabled) {
      next.focus({ preventScroll: true });
      return true;
    }
    if (typeof scope.focus === "function" && !scope.classList.contains(hiddenClass)) {
      scope.focus({ preventScroll: true });
      return true;
    }
  }
  // The control disappeared entirely (e.g. "resign" once the match is over): fall
  // back to the container rather than dropping focus onto body.
  MEMO.key = key;
  const fallback = list[0];
  if (fallback && typeof fallback.focus === "function" && !fallback.classList.contains(hiddenClass)) {
    fallback.focus({ preventScroll: true });
    return true;
  }
  return false;
}