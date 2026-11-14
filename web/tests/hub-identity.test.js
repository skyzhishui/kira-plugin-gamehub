/* tests/hub-identity.test.js — 大厅身份文案守门（node:test，零依赖）。
 *
 * 用户口径：大厅里所有涉及宿主主 agent 的文案都必须打印宿主真名
 * （宿主当前激活人设的名字），不能出现写死的「与AI对战」「主 AI」之类身份词。
 * 这里用源码静态检查 + 真实模块渲染两条路径锁住：
 *
 * 1) 模板层：MODE_LABELS / MODE_HINTS / LINEUP_LABELS / LINEUP_HINTS 里宿主
 *    一律走 HOST_PLACEHOLDER，渲染后必须等于注入的宿主名；
 * 2) 启动层：hub.js 必须先拿到 host_agent_name 再渲染（否则晚到的身份
 *    会让按钮永久停在兜底名）；服务端 /games 必须直接下发 host_agent_name；
 * 3) index.html 的横幅占位只能是中性「AI」，不得写死身份词。
 *
 * 运行：node --test web/tests/
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const hubJs = fs.readFileSync(new URL("../hub.js", import.meta.url), "utf8");
const modeJs = fs.readFileSync(new URL("../shared/mode.js", import.meta.url), "utf8");

const HOST = "汐祈（Seki）";

/* ---------------- 模板层 ---------------- */

test("大厅文案模板不含写死的身份词（宿主名一律走占位）", async () => {
  const m = await import("../shared/mode.js");
  const templates = [
    ...Object.values(m.MODE_LABELS),
    ...Object.values(m.MODE_HINTS),
    ...Object.values(m.LINEUP_LABELS),
    ...Object.values(m.LINEUP_HINTS),
  ];
  assert.ok(templates.length >= 10, "模板集合不应为空");
  for (const text of templates) {
    assert.doesNotMatch(text, /主\s*AI/, `模板写死了「主 AI」: ${text}`);
    assert.doesNotMatch(text, /诺里/, `模板写死了人设名: ${text}`);
  }
});

test("「与{宿主}对战」由注入的宿主名渲染", async () => {
  const m = await import("../shared/mode.js");
  assert.equal(m.MODE_LABELS.nori, `与${m.HOST_PLACEHOLDER}对战`);

  m.setHostAgentName(HOST);
  assert.equal(m.modeLabel("nori", "nori"), `与${HOST}对战`);
  assert.equal(m.modeLabel("nori", "蕾依"), `与${HOST}对战`, "nori 模式永远指宿主本人");
  assert.equal(m.modeHint("nori"), `${HOST}会认真应战。`);

  m.setHostAgentName("");
  assert.equal(m.modeLabel("nori", "nori"), `与${m.HOST_PLACEHOLDER}对战`);
});

/* ---------------- 启动层 ---------------- */

test("hub.js 先取身份再渲染（避免晚到的宿主名停在兜底值）", () => {
  // init 必须在渲染前拿到 host_agent_name：/games 已带，/ui-config 只做补齐
  assert.match(hubJs, /host_agent_name/);
  const initBlock = hubJs.slice(hubJs.indexOf("async function init()"), hubJs.indexOf("window.addEventListener(\"unhandledrejection\""));
  assert.ok(initBlock.includes("applyPersona(data)"), "init 先上身份");
  assert.ok(
    initBlock.indexOf("applyPersona(data)") < initBlock.indexOf("render(data)"),
    "身份必须先于 render 落地"
  );
  assert.match(initBlock, /Promise\.allSettled/, "两个接口并行取，取齐后一次性上屏");
});

test("applyPersona 会写入 /games 下发的宿主名", () => {
  const fn = hubJs.slice(hubJs.indexOf("function applyPersona"), hubJs.indexOf("/* ---------------- 对手头像"));
  assert.match(fn, /data\.host_agent_name/, "宿主名来自 /games 顶层字段");
  assert.match(fn, /setHostAgentName\(/, "写入共享身份状态");
});

test("横幅占位是中性「AI」，不是身份词", () => {
  assert.match(html, /id="hero-agent">AI</, "横幅初始值只能是中性占位");
  assert.doesNotMatch(html, /主\s*AI/, "页面不得写死「主 AI」");
  assert.doesNotMatch(html, /诺里/, "页面不得写死人设名");
});

test("mode.js 的宿主占位声明在任何模板之前（避免 TDZ）", () => {
  assert.ok(
    modeJs.indexOf('export const HOST_PLACEHOLDER') < modeJs.indexOf("export const MODE_LABELS"),
    "MODE_LABELS 引用 HOST_PLACEHOLDER，声明必须在前"
  );
});
