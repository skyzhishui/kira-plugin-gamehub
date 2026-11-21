/* match-chrome.test.js — page-shell wiring that cannot be driven headlessly.
 *
 * The pure helpers behind these fixes are behaviour-tested in
 * interaction-behaviour.test.js (turnSeatOf, parseSpectatorJoin, radiogroup,
 * focus-keep). What is left here is the page wiring for which no browser harness
 * exists: CSS collision rules, the focus scopes, and the lobby cover fallback.
 *
 * 1) connection chip "paused" must not reuse the global .hidden utility;
 * 2) the match page and lobby share one three-seat turn helper (turnSeatOf);
 * 3) action-row rebuilds preserve keyboard focus through shared/focus-keep;
 * 4) the lobby lineup radiogroup delegates keyboard behaviour to shared/radiogroup;
 * 5) the kept seat-bubble media container collapses when empty;
 * 6) the lobby cover placeholder appears only after every candidate asset failed;
 * 7) external_not_mirrored only feeds the status hint, never the input gate.
 *
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const readSrc = (rel) => fs.readFileSync(new URL(rel, import.meta.url), "utf8").replace(/\r\n/g, "\n");

const matchJs = readSrc("../match.js");
const matchHtml = readSrc("../match.html");
const hubJs = readSrc("../hub.js");
const appCss = readSrc("../app.css");

/* ---------------- 1) paused chip: CSS collision ---------------- */

test("paused chip: its own neutral style, never the .hidden display:none utility", () => {
  assert.match(matchJs, /state === "hidden" \? "paused"/, "the paused connection avoids the global hidden class");
  // .hidden is a global display:none !important utility — a paused chip must not use it.
  assert.match(appCss, /\.hidden\s*\{\s*display:\s*none\s*!important;?\s*\}/, "the global utility is still display:none");
  const block = /\.conn-chip\.paused\s*\{([^}]*)\}/.exec(appCss);
  assert.ok(block, "paused has its own rule");
  assert.match(block[1], /color:[^;]+;/, "paused keeps a visible ink colour");
  assert.match(block[1], /border-color:[^;]+;/, "paused keeps a visible border");
  assert.doesNotMatch(block[1], /display\s*:\s*none/, "paused is never hidden");
  assert.doesNotMatch(block[1], /animation\s*:(?!\s*none)/, "paused does not blink");
});

/* ---------------- 2) three-seat turn: one source ---------------- */

test("three-seat turn source is shared: match page delegates and the lobby uses turnSeatOf", () => {
  assert.match(matchJs, /function waitingOnSeat\(match\) \{\s*return turnSeatOf\(match\);/, "match page delegates to the helper");
  assert.match(hubJs, /const w = turnSeatOf\(match\);/, "lobby turn line uses the same helper");
  assert.doesNotMatch(matchJs, /seats\.length >= 3/, "match page keeps no second three-seat rule");
  assert.doesNotMatch(hubJs, /seats\.length >= 3/, "lobby keeps no second three-seat rule");
});

/* ---------------- 3) action-row keyboard focus ---------------- */

test("action row: rebuilds preserve focus through shared/focus-keep", () => {
  assert.match(matchJs, /from "\.\/shared\/focus-keep\.js"/, "uses the shared helper");
  assert.match(matchJs, /focusKeyOf\(ACTION_SCOPE\(\), document\.activeElement\)/, "captures focus before rebuilding");
  assert.match(matchJs, /restoreFocus\(key, ACTION_SCOPE\(\)\)/, "restores focus after rebuilding");
  // The containers must be programmatically focusable for the fallback path.
  assert.match(matchHtml, /id="actions"[^>]*tabindex="-1"/, "action container can receive focus");
  assert.match(matchHtml, /id="level-row"[^>]*tabindex="-1"/, "level row can receive focus");
  // Spectators (no seat) never get restart/resign controls.
  assert.match(matchJs, /if \(mine\) row\.appendChild\(actionBtn\(\{\s*label: restartLabel/, "restart is gated on a seated player");
});

/* ---------------- 4) lobby lineup radiogroup wiring ---------------- */

test("hub.js lineup group delegates its keyboard behaviour to shared/radiogroup.js", () => {
  assert.match(hubJs, /from "\.\/shared\/radiogroup\.js"/, "imports the shared module");
  assert.match(hubJs, /onRadioKey\(/, "keydown navigation goes through the helper");
  assert.match(hubJs, /applyRadioState\(/, "selection presentation goes through the helper");
});

/* ---------------- 5) seat-bubble media container ---------------- */

test("seat-bubble media container is kept and collapses when empty", () => {
  assert.match(matchJs, /el\("span", "seat-bubble-media"\)/, "match.js creates the container");
  assert.match(appCss, /\.seat-bubble-media\s*\{\s*display:\s*block;?\s*\}/);
  assert.match(appCss, /\.seat-bubble-media:empty\s*\{\s*display:\s*none;?\s*\}/, "empty media takes no space");
});

/* ---------------- 6) lobby cover empty state timing ---------------- */

/* ---------------- 6) lobby cover empty state timing ---------------- */

test("cover placeholder: marked empty only after every candidate asset fails", () => {
  assert.match(hubJs, /coverImg\(g\.kind, [^,]+, "card-cover", markEmpty\)/, "handed to the candidate chain as the all-failed callback");
  const fn = hubJs.match(/function coverImg\([\s\S]*?\n\}/);
  assert.ok(fn, "coverImg is present");
  assert.match(fn[0], /if \(i >= list\.length\) \{[\s\S]*?onFail\(\)/, "onFail runs only once candidates are exhausted");
});

/* ---------------- 7) non-blocking "AI notices go elsewhere" hint ---------------- */

test("external_not_mirrored 只加提示条，绝不禁用聊天窗口", () => {
  // match.js 只把它当文案输入；状态条由纯函数决定，输入区不受影响。
  assert.match(matchJs, /chat\.externalNotice = isExternalNotMirrored\(cfg\)/, "wiring reads the flag");
  assert.match(matchJs, /externalNotMirrored: chat\.externalNotice/, "flag reaches the status text helper");
  assert.doesNotMatch(
    matchJs,
    /externalNotice[\s\S]{0,120}?\.disabled\s*=/,
    "the flag must not disable any input"
  );
  // 聊天窗口本身只认 chat_enabled / chat_read_only（本插件不下发只读）。
  const panel = readSrc("../chat-panel.js");
  assert.doesNotMatch(panel, /external_not_mirrored|externalNotice/, "chat window never consumes the flag");
  assert.match(panel, /input\.disabled = !on;/, "input gating stays tied to isEnabled() only");
  const chatShared = readSrc("../shared/chat.js");
  assert.doesNotMatch(chatShared, /isExternalNotMirrored[\s\S]{0,80}?chat_read_only\s*=/, "the flag never rewrites the read-only decision");
  // 信息态与警示态分开配色，不新增色板变量。
  assert.match(appCss, /\.stage > \.chat-status\.is-info\s*\{[^}]*color:\s*var\(--ink-soft\)/);
});
