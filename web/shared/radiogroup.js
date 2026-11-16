/* shared/radiogroup.js — single-select (radio-group) keyboard behaviour, pure.
 *
 * role="radiogroup" / role="radio" promises native single-select interaction:
 * arrow keys move and select, Home/End jump to the first/last option, and the group
 * keeps a single Tab stop (roving tabindex). Keeping the logic here (instead of
 * inline in the lobby page module) lets Node tests drive it with plain object
 * elements and no DOM dependency.
 */

const RADIO_NEXT = new Set(["ArrowDown", "ArrowRight", "Down", "Right"]);
const RADIO_PREV = new Set(["ArrowUp", "ArrowLeft", "Up", "Left"]);

/** Key event -> the option value to select next; null for non-navigation keys.
 * `options` is [{value, ...}] in visual order. */
export function radioTarget(ev, options, current) {
  if (!ev || !Array.isArray(options) || !options.length) return null;
  const key = String(ev.key || "");
  const at = options.findIndex((o) => o.value === current);
  if (RADIO_NEXT.has(key)) return options[(at + 1 + options.length) % options.length].value;
  if (RADIO_PREV.has(key)) return options[(at - 1 + options.length) % options.length].value;
  if (key === "Home") return options[0].value;
  if (key === "End") return options[options.length - 1].value;
  return null;
}

/** Handle a keydown on a radio option: moves selection when it is a navigation key.
 * `commit(value)` performs the state change (and usually moves focus). */
export function onRadioKey(ev, options, current, commit) {
  const next = radioTarget(ev, options, current);
  if (next == null) return false;
  if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
  commit(next);
  return true;
}

/** Apply single-select presentation to a value -> element map.
 * Sets `.on`, `aria-checked` and a roving `tabIndex` (selected = 0, others = -1).
 * Elements only need classList.toggle / setAttribute / tabIndex. */
export function applyRadioState(values, selected, elements) {
  for (const value of Array.isArray(values) ? values : []) {
    const node = elements && elements[value];
    if (!node) continue;
    const on = value === selected;
    node.classList.toggle("on", on);
    node.setAttribute("aria-checked", String(on));
    node.tabIndex = on ? 0 : -1;
  }
}