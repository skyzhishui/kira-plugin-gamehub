/* hero-banner.test.js — lobby hero banner + shared-asset wiring regression.
 *
 * Contract:
 * 1) the lobby banner is a wide illustration with the title on a left scrim; the
 *    character standee / speech bubble it replaced is gone for good;
 * 2) the banner stays compact (narrower screens are clearly shorter) and the dark
 *    theme dims the art with a deep-blue scrim (not near-black);
 * 3) every sticker / background / avatar reference points at a real shared asset;
 * 4) the lobby avatar control (preview / file picker / reset) talks only to the
 *    plugin's own agent-avatar endpoint and never writes asset files.
 *
 * Run: cd web && node --test
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

/** Read source text with normalised line endings before asserting.
 * This file uses multi-line regexes (e.g. /...\n\s+.../); a Windows checkout
 * (core.autocrlf=true / .gitattributes eol=crlf) yields CRLF and byte-level
 * assertions fail spuriously (T-1 regression). */
const readSrc = (rel) => fs.readFileSync(new URL(rel, import.meta.url), "utf8").replace(/\r\n/g, "\n");

const css = readSrc("../app.css");
const html = readSrc("../index.html");
const js = readSrc("../hub.js");
const matchHtml = readSrc("../match.html");
const matchJs = readSrc("../match.js");

/** Declarations of a selector block (callers escape special characters themselves) */
const ruleOf = (selector) => {
  const i = css.indexOf(selector);
  if (i < 0) return "";
  return css.slice(css.indexOf("{", i) + 1, css.indexOf("}", i));
};

test("顶部角色立绘动图与台词气泡已移除", () => {
  // Only a CSS background illustration is allowed: no character <img>, no hand-wave
  // or speech node left behind in markup, styles or the lobby script.
  assert.doesNotMatch(html, /<img[^>]*src="\.\/assets\/hero-/);
  assert.doesNotMatch(html, /class="hero-bubble"/);
  assert.doesNotMatch(css, /\.hero-bubble/);
  assert.doesNotMatch(js, /bindBubble/);
});

test("横幅：宽幅插画垫底 + 左侧标题压在柔光上", () => {
  assert.match(html, /class="hero-banner"/);
  assert.match(html, /class="hero-banner-art"/);
  assert.match(html, /class="hero-copy"/);
  assert.match(html, /<h1>游戏中心<\/h1>/);

  const art = ruleOf(".hero-banner-art");
  assert.match(art, /url\("\.\/assets\/hero-banner\.webp"\) right center \/ contain/);
  // Equal-height right-aligned art at the source ratio → effective contain, no crop;
  // the left edge fades out through a mask into the card gradient.
  assert.match(art, /aspect-ratio:\s*1600 \/ 552/);
  assert.match(art, /mask-image:\s*linear-gradient\(to right, transparent/);

  // Left scrim carries the title's readability — must not be dropped.
  const scrim = ruleOf(".hero-banner::after");
  assert.match(scrim, /linear-gradient\(96deg/);
  assert.match(scrim, /rgba\(255, 255, 255, 0\.86\)/);

  const copy = ruleOf(".hero-copy");
  assert.match(copy, /z-index:\s*2/);
  assert.match(copy, /max-width:\s*min\(52%/);
});

test("紧凑：窄屏明显低于桌面高度，且不超过 112px", () => {
  const desktop = Number(/min-height:\s*(\d+)px/.exec(ruleOf(".hero-banner"))?.[1]);
  const heights = [...css.matchAll(/min-height:\s*(\d+)px/g)].map((m) => Number(m[1]));
  assert.equal(desktop, 148, "桌面横幅高度");
  assert.ok(heights.includes(106) && heights.includes(98), "640px/380px 两档断点高度");
  assert.ok(106 <= 112 && 98 <= 112 && 106 < desktop && 98 < 106, "窄屏逐档收窄");
});

test("窄屏换 2:1 竖切裁图；深色主题压暗插画并换深色柔光", () => {
  assert.match(css, /hero-banner-mobile\.webp/);
  const dark = ruleOf("html.dark .hero-banner-art");
  assert.match(dark, /brightness\(0\.62\)/);
  // Dark scrim is a deep blue (not near-black), matching the card background.
  const darkScrim = ruleOf("html.dark .hero-banner::after");
  assert.match(darkScrim, /rgba\(23, 41, 74, 0\.94\)/);
  const darkBg = ruleOf("html.dark .hero-banner");
  assert.match(darkBg, /#17294a/);
  const rgbs = [...darkScrim.matchAll(/rgba\((\d+), (\d+), (\d+),/g)].map((m) => m.slice(1, 4).map(Number));
  assert.ok(rgbs.length > 0);
  for (const [r, , b] of rgbs) assert.ok(b - r >= 40, `柔光 ${r}..${b} 偏黑`);
});

/* ---------------- asset references ---------------- */

test("贴纸/底图引用都指向真实存在的公共素材", () => {
  const exists = (rel) => fs.existsSync(new URL(`../${rel.replace(/^\.\//, "")}`, import.meta.url));
  assert.match(matchJs, /"\.\/assets\/sticker-win\.png"/);
  assert.match(matchJs, /"\.\/assets\/sticker-lose\.png"/);
  assert.ok(exists("./assets/sticker-win.png"));
  assert.ok(exists("./assets/sticker-lose.png"));
  assert.match(css, /url\("\.\/assets\/bg-lobby\.png"\)/);
  assert.ok(exists("./assets/bg-lobby.png"));
});

test("对局页只引用公共头像，不再引用角色立绘或其它插画", () => {
  assert.doesNotMatch(matchJs, /hero-/);
  assert.doesNotMatch(matchHtml, /assets\/(?!agent-avatar)/);
  // Seat ownership: game_agent seats (advanced opponent / coach) use the plugin's own
  // avatar; the host agent seat uses the host persona's selfie (host_avatar) or a
  // first-character placeholder; practice/human seats only ever get the placeholder.
  assert.match(matchJs, /identity === "agent"\) \{\n\s+avatar = seatAvatarEl\(getAgentAvatar\(\), name, thinking, \{ agent: true \}\);/);
  assert.match(matchJs, /identity === "nori" && getHostAvatar\(\)\) \{/);
  assert.match(matchJs, /getHostAvatar\(\)\n\s+\? seatAvatarEl\(getHostAvatar\(\), name, thinking, \{ agent: true \}\)/);
  assert.match(matchJs, /mate-avatar-fallback/);
  assert.doesNotMatch(matchJs, /AGENT_AVATAR_URL/);
});

/* ---------------- lobby avatar control (plugin-owned agent-avatar) ---------------- */

test("大厅有头像预览 + 文件选择 + 恢复默认，且走插件 agent-avatar 端点", () => {
  assert.match(html, /id="avatar-preview"/);
  assert.match(html, /id="avatar-file"[^>]*type="file"/);
  assert.match(html, /accept="image\/png,image\/jpeg,image\/webp,image\/gif"/);
  assert.match(html, /id="avatar-reset"/);
  assert.match(html, /id="avatar-err"/);
  // Default preview is the asset shipped with the plugin.
  assert.match(html, /id="avatar-preview"[^>]*src="\.\/assets\/agent-avatar\.png"/);
  // Upload and reset both hit the same endpoint; upload goes through a data URL.
  assert.match(js, /api\("\/agent-avatar", \{ method: "POST", body: \{ data \}/);
  assert.match(js, /api\("\/agent-avatar", \{ method: "POST", body: \{ reset: true \}/);
  assert.match(js, /readAsDataURL/, "文件转 data URL 再上传");
  // Avatar URL comes from /ui-config, never from host persona image fields.
  assert.match(js, /applyAvatar\(\(cfg && cfg\.agent_avatar\) \|\| AGENT_AVATAR_URL\)/, "头像随启动一起取");
  assert.doesNotMatch(js, /reference_image|avatar_path|persona_avatar/, "不读宿主人设图字段");
});

test("大厅头像控件不写素材文件（无 file: 路径、无写盘调用）", () => {
  assert.doesNotMatch(js, /file:\/\/|require\("fs"\)|writeFile/);
  assert.match(js, /AVATAR_MAX_BYTES = 4 \* 1024 \* 1024/, "前端先做 4MB 预检");
});
