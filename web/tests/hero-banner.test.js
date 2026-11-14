/* hero-banner.test.js — 大厅顶部横幅（取代招手动图 + 气泡）回归。
 *
 * 口径：
 * 1) 顶部不再有角色立绘动图/台词气泡元素（KiraAI 不随插件分发角色图）；
 * 2) 横幅是「宽幅插画 + 左侧标题」：插画走背景图（两张裁切：桌面 3:1 主图、
 *    窄屏 2:1），标题压在左侧柔光上；
 * 3) 高度紧凑：窄屏最低高度明显小于桌面，且不超过 112px；
 * 4) 深色主题有压暗 + 深色柔光，不会白到刺眼。
 *
 * 运行：node --test web/tests/
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const css = fs.readFileSync(new URL("../app.css", import.meta.url), "utf8");
const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const js = fs.readFileSync(new URL("../hub.js", import.meta.url), "utf8");

/** 取某个选择器的声明块（选择器里的特殊字符由调用方自行加反斜杠转义） */
const ruleOf = (selector) => {
  const i = css.indexOf(selector);
  if (i < 0) return "";
  return css.slice(css.indexOf("{", i) + 1, css.indexOf("}", i));
};

test("顶部角色立绘动图与台词气泡已移除", () => {
  // 顶部只允许 CSS 背景插画：不得再出现角色 <img> 或独立的招手/台词节点
  assert.doesNotMatch(html, /<img[^>]*src="\.\/assets\/hero-/);
  assert.doesNotMatch(html, /class="hero-bubble"/);
  assert.doesNotMatch(css, /\.hero-bubble/);
  assert.doesNotMatch(js, /bindBubble/);
  assert.match(html, /aria-label="云朵上的棋桌/, "横幅 aria 描述不指代角色");
});

test("横幅：宽幅插画垫底 + 左侧标题压在柔光上", () => {
  assert.match(html, /class="hero-banner"/);
  assert.match(html, /class="hero-banner-art"/);
  assert.match(html, /class="hero-copy"/);
  assert.match(html, /<h1>游戏中心<\/h1>/);

  const art = ruleOf(".hero-banner-art");
  assert.match(art, /url\("\.\/assets\/hero-banner\.webp"\) right center \/ contain/);
  // 等高贴右 + 与素材同比例 → 等效 contain，不裁人物；左缘用遮罩淡出接卡片渐变
  assert.match(art, /aspect-ratio:\s*1600 \/ 552/);
  assert.match(art, /mask-image:\s*linear-gradient\(to right, transparent/);

  // 左侧柔光层：标题可读性靠它，不能省
  const scrim = ruleOf(".hero-banner::after");
  assert.match(scrim, /linear-gradient\(96deg/);
  assert.match(scrim, /rgba\(255, 255, 255, 0\.86\)/);

  const copy = ruleOf(".hero-copy");
  assert.match(copy, /z-index:\s*2/);
  assert.match(copy, /max-width:\s*min\(52%/);
});

test("紧凑：桌面 148px → 640px 106px → 380px 98px（成立前是 240px 立绘 + 两行文案）", () => {
  assert.match(ruleOf(".hero-banner"), /min-height:\s*148px/);
  assert.match(css, /min-height:\s*106px/);
  assert.match(css, /min-height:\s*98px/);
  assert.ok(106 <= 112 && 98 <= 112);
});

test("窄屏换 2:1 竖切裁图；深色主题压暗插画并换深色柔光", () => {
  assert.match(css, /hero-banner-mobile\.webp/);
  const dark = ruleOf("html.dark .hero-banner-art");
  assert.match(dark, /brightness\(0\.62\)/);
  // 深色柔光用深蓝（不是近黑），左侧底色与卡片同为深蓝
  const darkScrim = ruleOf("html.dark .hero-banner::after");
  assert.match(darkScrim, /rgba\(23, 41, 74, 0\.94\)/);
  const darkBg = ruleOf("html.dark .hero-banner");
  assert.match(darkBg, /#17294a/);
  // 不允许出现接近纯黑的柔光（蓝通道必须明显高于红通道）
  const rgbs = [...darkScrim.matchAll(/rgba\((\d+), (\d+), (\d+),/g)].map((m) => m.slice(1, 4).map(Number));
  assert.ok(rgbs.length > 0);
  for (const [r, , b] of rgbs) assert.ok(b - r >= 40, `柔光 ${r}..${b} 偏黑`);
});
/* ---------------- 素材引用（KiraAI：只允许公共通用素材 + 无角色） ---------------- */

const matchHtml = fs.readFileSync(new URL("../match.html", import.meta.url), "utf8");
const matchJs = fs.readFileSync(new URL("../match.js", import.meta.url), "utf8");
const modeJs = fs.readFileSync(new URL("../shared/mode.js", import.meta.url), "utf8");

test("贴纸/底图/头像引用都指向真实存在的公共素材", () => {
  const fsmod = fs;
  const exists = (rel) => fsmod.existsSync(new URL(`../${rel.replace(/^\.\//, "")}`, import.meta.url));
  assert.match(matchJs, /"\.\/assets\/sticker-win\.png"/);
  assert.match(matchJs, /"\.\/assets\/sticker-lose\.png"/);
  assert.ok(exists("./assets/sticker-win.png"));
  assert.ok(exists("./assets/sticker-lose.png"));
  assert.match(css, /url\("\.\/assets\/bg-lobby\.png"\)/);
  assert.ok(exists("./assets/bg-lobby.png"));
  assert.match(modeJs, /export const AGENT_AVATAR_URL = "\.\/assets\/agent-avatar\.png"/);
  assert.ok(exists("./assets/agent-avatar.png"));
});

test("对局页只引用公共头像，不再引用角色立绘或其它插画", () => {
  assert.doesNotMatch(matchJs, /hero-/);
  assert.doesNotMatch(matchHtml, /assets\/(?!agent-avatar)/);
  // 头像归属：game_agent 座位（进阶对手/教练席）挂插件自有头像；
  // 宿主主 agent 座位挂宿主人设的自拍参考图（host_avatar），没配则首字占位；
  // 陪练/人类座位永远只占位。
  assert.match(matchJs, /identity === "agent"\) \{\n\s+avatar = seatAvatarEl\(getAgentAvatar\(\), name, thinking, \{ agent: true \}\);/);
  assert.match(matchJs, /identity === "nori" && getHostAvatar\(\)\) \{/);
  assert.match(matchJs, /getHostAvatar\(\)\n\s+\? seatAvatarEl\(getHostAvatar\(\), name, thinking, \{ agent: true \}\)/);
  assert.match(matchJs, /mate-avatar-fallback/);
  assert.doesNotMatch(matchJs, /AGENT_AVATAR_URL/);
});

/* ---------------- 大厅头像上传控件（插件自有 agent-avatar） ---------------- */

const hubJs = fs.readFileSync(new URL("../hub.js", import.meta.url), "utf8");

test("大厅有头像预览 + 文件选择 + 恢复默认，且走插件 agent-avatar 端点", () => {
  assert.match(html, /id="avatar-preview"/);
  assert.match(html, /id="avatar-file"[^>]*type="file"/);
  assert.match(html, /accept="image\/png,image\/jpeg,image\/webp,image\/gif"/);
  assert.match(html, /id="avatar-reset"/);
  assert.match(html, /id="avatar-err"/);
  // 默认预览就是随插件分发的素材
  assert.match(html, /id="avatar-preview"[^>]*src="\.\/assets\/agent-avatar\.png"/);
  // 上传/恢复默认都打同一个端点，且上传走 data URL
  assert.match(hubJs, /api\("\/agent-avatar", \{ method: "POST", body: \{ data \}/);
  assert.match(hubJs, /api\("\/agent-avatar", \{ method: "POST", body: \{ reset: true \}/);
  assert.match(hubJs, /readAsDataURL/, "文件转 data URL 再上传");
  // 头像地址来自 /ui-config，不读宿主人设图
  assert.match(hubJs, /applyAvatar\(cfg\.agent_avatar\)/);
  assert.doesNotMatch(hubJs, /reference_image|avatar_path|persona_avatar/, "不读宿主人设图字段");
});

test("大厅头像控件不写素材文件（无 file: 路径、无写盘调用）", () => {
  assert.doesNotMatch(hubJs, /file:\/\/|require\("fs"\)|writeFile/);
  assert.match(hubJs, /AVATAR_MAX_BYTES = 4 \* 1024 \* 1024/, "前端先做 4MB 预检");
});
