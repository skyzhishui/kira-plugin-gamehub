/* interaction-behaviour.test.js — behaviour tests for the pure UI helpers.
 *
 * Covers the logic extracted out of the page modules so it can be driven without a
 * browser or new dependencies:
 * - shared/mode.js      turnSeatOf      (current-turn seat, three-seat rule)
 * - shared/spectator.js parseSpectatorJoin (join envelope validation)
 * - shared/radiogroup.js radioTarget / onRadioKey / applyRadioState
 * - shared/focus-keep.js focusKeyOf / restoreFocus
 *
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";

import { turnSeatOf } from "../shared/mode.js";
import { parseSpectatorJoin } from "../shared/spectator.js";
import { radioTarget, onRadioKey, applyRadioState } from "../shared/radiogroup.js";
import { focusKeyOf, restoreFocus, resetFocusMemo } from "../shared/focus-keep.js";

/* ---------------- minimal element stand-ins (no DOM dependency) ---------------- */

class FakeClassList {
  constructor() {
    this.set = new Set();
  }
  add(name) {
    this.set.add(name);
  }
  contains(name) {
    return this.set.has(name);
  }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : !!force;
    if (on) this.set.add(name);
    else this.set.delete(name);
    return on;
  }
}

class FakeNode {
  constructor({ act = "", disabled = false, hidden = false } = {}) {
    this.dataset = {};
    this.attributes = {};
    // Real DOM: el.dataset.act = x also writes the data-act attribute.
    if (act) {
      this.dataset.act = act;
      this.attributes["data-act"] = act;
    }
    this.classList = new FakeClassList();
    if (hidden) this.classList.add("hidden");
    this.disabled = disabled;
    this.tabIndex = -1;
    this.children = [];
    this.focusCalls = 0;
    this.focusedWith = null;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return this.attributes[name] != null ? this.attributes[name] : null;
  }
  focus(options) {
    this.focusCalls += 1;
    this.focusedWith = options;
  }
  contains(node) {
    // Element.contains returns true for the node itself, as in the real DOM.
    return node === this || this.children.includes(node);
  }
  querySelector(selector) {
    const m = /^\[data-act="(.+)"\]$/.exec(selector);
    if (!m) return null;
    return this.children.find((c) => c.dataset.act === m[1]) || null;
  }
}

/* ---------------- turnSeatOf ---------------- */

test("turnSeatOf: status.waitingOn wins over view fields (number or single-item array)", () => {
  const seats = [{ kind: "human" }, { kind: "ai" }, { kind: "ai" }];
  assert.equal(turnSeatOf({ seats, status: { waitingOn: 2 }, view: { turn: 1 } }).index, 2);
  assert.equal(turnSeatOf({ seats, status: { waitingOn: [1] }, view: { turn: 2 } }).index, 1);
  assert.equal(turnSeatOf({ seats, status: { waitingOn: [1, 2] }, view: { turn: 2 } }).index, 2,
    "a multi-item waitingOn is not treated as a single seat");
});

test("turnSeatOf: two-seat games use view.toPlay, three-seat games use view.turn", () => {
  const two = { seats: [{ kind: "human" }, { kind: "ai" }] };
  assert.equal(turnSeatOf({ ...two, view: { toPlay: 1 } }).index, 0);
  assert.equal(turnSeatOf({ ...two, view: { toPlay: 2 } }).index, 1);
  // view.turn of 1 is not a turn source for a two-seat game.
  assert.equal(turnSeatOf({ ...two, view: { turn: 1 } }), null);

  const three = { seats: [{ kind: "human" }, { kind: "ai" }, { kind: "bot" }], mode_options: { lineup: "nori_bot" } };
  assert.equal(turnSeatOf({ ...three, view: { turn: 0 } }).index, 0);
  assert.equal(turnSeatOf({ ...three, view: { turn: 2 } }).index, 2);
});

test("turnSeatOf: match-over null turn is never read as seat 0", () => {
  const seats = [{ kind: "human" }, { kind: "ai" }, { kind: "ai" }];
  const three = { seats, mode_options: { lineup: "nori_agent" } };
  assert.equal(turnSeatOf({ ...three, view: { turn: null } }), null);
  assert.equal(turnSeatOf({ ...three, view: {} }), null);
  assert.equal(turnSeatOf({ ...three }), null, "no view at all degrades to null");
});

test("turnSeatOf: a declared lineup counts as three-seat even before seats arrive", () => {
  assert.equal(turnSeatOf({ mode_options: { lineup: "agent_bot" }, seats: [], view: { turn: 1 } }), null,
    "no dangling seat when the seat list is empty");
  const legacy = { seats: [{ kind: "human" }, { kind: "bot" }, { kind: "bot" }], view: { turn: 1 } };
  assert.equal(turnSeatOf(legacy).index, 1, "seats>2 (no lineup) still counts");
});

test("turnSeatOf: out-of-range / negative / junk input returns null", () => {
  const seats = [{ kind: "human" }, { kind: "ai" }];
  assert.equal(turnSeatOf({ seats, view: { toPlay: 3 } }), null);
  assert.equal(turnSeatOf({ seats, status: { waitingOn: 9 } }), null);
  assert.equal(turnSeatOf({ seats, view: { turn: -1 } }), null);
  assert.equal(turnSeatOf(null), null);
  assert.equal(turnSeatOf(undefined), null);
  assert.equal(turnSeatOf("junk"), null);
  assert.equal(turnSeatOf({}), null);
});

/* ---------------- parseSpectatorJoin ---------------- */

test("parseSpectatorJoin: accepts only { ok:true, data:{credential,uid} } with both non-empty strings", () => {
  assert.deepEqual(parseSpectatorJoin({ ok: true, data: { credential: " c ", uid: " m1:ab " } }), {
    credential: "c",
    uid: "m1:ab",
  }, "both sides are trimmed");

  const badEnvelopes = [
    null,
    undefined,
    {},
    "not-an-object",
    { ok: false, error: "邀请已失效" },
    { ok: true },
    { ok: true, data: null },
    { ok: true, data: {} },
    { ok: true, data: { credential: "c" } },
    { ok: true, data: { uid: "u" } },
    { ok: true, data: { credential: "  ", uid: "u" } },
    { ok: true, data: { credential: "c", uid: "   " } },
    { ok: true, data: { credential: 12, uid: "u" } },
    { ok: true, data: { credential: "c", uid: 5 } },
    { data: { credential: "c", uid: "u" } },
  ];
  for (const bad of badEnvelopes) {
    assert.throws(() => parseSpectatorJoin(bad), /邀请|Invitation/, `must reject: ${JSON.stringify(bad)}`);
  }
  // A rejected invite passes the server-provided reason through.
  assert.throws(() => parseSpectatorJoin({ ok: false, error: "邀请已失效" }), /邀请已失效/);
});

/* ---------------- radiogroup ---------------- */

const OPTIONS = [{ value: "a" }, { value: "b" }, { value: "c" }];

test("radioTarget: arrows wrap, Home/End jump, other keys do nothing", () => {
  assert.equal(radioTarget({ key: "ArrowDown" }, OPTIONS, "a"), "b");
  assert.equal(radioTarget({ key: "ArrowRight" }, OPTIONS, "c"), "a", "wraps forward");
  assert.equal(radioTarget({ key: "ArrowUp" }, OPTIONS, "a"), "c", "wraps backward");
  assert.equal(radioTarget({ key: "ArrowLeft" }, OPTIONS, "b"), "a");
  assert.equal(radioTarget({ key: "Home" }, OPTIONS, "c"), "a");
  assert.equal(radioTarget({ key: "End" }, OPTIONS, "a"), "c");
  assert.equal(radioTarget({ key: "Tab" }, OPTIONS, "a"), null);
  assert.equal(radioTarget({ key: "Enter" }, OPTIONS, "a"), null);
  assert.equal(radioTarget(null, OPTIONS, "a"), null);
  assert.equal(radioTarget({ key: "ArrowDown" }, [], "a"), null);
});

test("radioTarget: no current selection enters from the edge (next -> first, prev -> last)", () => {
  // Missing / stale current value must not clamp to index 0: "next" would then
  // land on the second option and skip the first one.
  assert.equal(radioTarget({ key: "ArrowDown" }, OPTIONS, ""), "a", "next from nothing -> first");
  assert.equal(radioTarget({ key: "ArrowRight" }, OPTIONS, undefined), "a");
  assert.equal(radioTarget({ key: "ArrowUp" }, OPTIONS, "stale"), "c", "prev from nothing -> last");
  assert.equal(radioTarget({ key: "ArrowLeft" }, OPTIONS, null), "c");
  assert.equal(radioTarget({ key: "Home" }, OPTIONS, ""), "a");
  assert.equal(radioTarget({ key: "End" }, OPTIONS, ""), "c");
  // Single-option group: both directions stay on the only option.
  const one = [{ value: "only" }];
  assert.equal(radioTarget({ key: "ArrowDown" }, one, ""), "only");
  assert.equal(radioTarget({ key: "ArrowUp" }, one, ""), "only");
  assert.equal(radioTarget({ key: "ArrowDown" }, one, "only"), "only", "single option wraps onto itself");
});

test("onRadioKey: navigation prevents default, commits, non-navigation is a no-op", () => {
  const commits = [];
  let prevented = 0;
  const ev = { key: "ArrowDown", preventDefault: () => (prevented += 1) };
  assert.equal(onRadioKey(ev, OPTIONS, "a", (v) => commits.push(v)), true);
  assert.deepEqual(commits, ["b"]);
  assert.equal(prevented, 1);

  assert.equal(onRadioKey({ key: "Tab", preventDefault: () => (prevented += 1) }, OPTIONS, "a", (v) => commits.push(v)), false);
  assert.deepEqual(commits, ["b"], "no extra commit for non-navigation keys");
  assert.equal(prevented, 1, "Tab is not prevented");
});

test("applyRadioState: selected option gets .on, aria-checked and the single Tab stop", () => {
  const nodes = Object.fromEntries(OPTIONS.map((o) => [o.value, new FakeNode()]));
  applyRadioState(["a", "b", "c"], "b", nodes);
  assert.equal(nodes.b.classList.contains("on"), true);
  assert.equal(nodes.b.getAttribute("aria-checked"), "true");
  assert.equal(nodes.b.tabIndex, 0);
  for (const other of ["a", "c"]) {
    assert.equal(nodes[other].classList.contains("on"), false);
    assert.equal(nodes[other].getAttribute("aria-checked"), "false");
    assert.equal(nodes[other].tabIndex, -1, "roving tabindex leaves one stop in the group");
  }
  // Re-selecting moves the stop; the previous one is cleared.
  applyRadioState(["a", "b", "c"], "c", nodes);
  assert.equal(nodes.c.tabIndex, 0);
  assert.equal(nodes.b.tabIndex, -1);
  assert.equal(nodes.b.getAttribute("aria-checked"), "false");
});

/* ---------------- focus-keep ---------------- */

function scopeWith(children, { hidden = false } = {}) {
  const scope = new FakeNode({ hidden });
  scope.children = children;
  return scope;
}

test("focusKeyOf: returns data-act only while focus is inside the managed scopes", () => {
  resetFocusMemo();
  const resign = new FakeNode({ act: "resign" });
  const row = scopeWith([resign]);
  assert.equal(focusKeyOf([row], resign), "resign");
  assert.equal(focusKeyOf([row], new FakeNode({ act: "elsewhere" })), "", "outside the scope -> empty and memo cleared");
});

test("restoreFocus: refocuses the same control after a rebuild", () => {
  resetFocusMemo();
  const rebuilt = new FakeNode({ act: "resign" });
  const row = scopeWith([rebuilt]);
  assert.equal(restoreFocus("resign", [row]), true);
  assert.equal(rebuilt.focusCalls, 1);
  assert.deepEqual(rebuilt.focusedWith, { preventScroll: true });
});

test("restoreFocus: disabled control parks focus on the container, memo keeps the key", () => {
  resetFocusMemo();
  const rebuilt = new FakeNode({ act: "resign", disabled: true });
  const row = scopeWith([rebuilt]);
  assert.equal(restoreFocus("resign", [row]), true);
  assert.equal(rebuilt.focusCalls, 0, "disabled buttons cannot take focus");
  assert.equal(row.focusCalls, 1, "focus goes to the container instead");
  // The memo survives so the next rebuild (request finished) can come back.
  assert.equal(focusKeyOf([row], row), "resign");
});

test("restoreFocus: a control that disappeared falls back to the container", () => {
  resetFocusMemo();
  const row = scopeWith([]);
  assert.equal(restoreFocus("delegate", [row]), true);
  assert.equal(row.focusCalls, 1);
});

test("restoreFocus: hidden containers never receive focus; empty key is a no-op", () => {
  resetFocusMemo();
  const row = scopeWith([], { hidden: true });
  assert.equal(restoreFocus("resign", [row]), false);
  assert.equal(row.focusCalls, 0);
  assert.equal(restoreFocus("", [row]), false);
});

test("focusKeyOf/restoreFocus: memo carries a disabled-rebuild key back to the enabled control", () => {
  resetFocusMemo();
  // Rebuild 1: control disabled -> container focused, key remembered.
  const disabled = new FakeNode({ act: "resign", disabled: true });
  const row1 = scopeWith([disabled]);
  restoreFocus("resign", [row1]);
  assert.equal(focusKeyOf([row1], row1), "resign", "container focus recalls the memo");
  // Rebuild 2: control enabled again -> focus returns to it.
  const enabled = new FakeNode({ act: "resign" });
  const row2 = scopeWith([enabled]);
  restoreFocus(focusKeyOf([row2], row2), [row2]);
  assert.equal(enabled.focusCalls, 1);
  assert.equal(enabled.focusedWith && enabled.focusedWith.preventScroll, true);
});