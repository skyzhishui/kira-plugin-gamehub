/* agent-avatar.test.js — match-page avatar URL and identity (pure logic, zero deps).
 *
 * Covers:
 * 1) The avatar asset URL is the plugin's own ./assets/agent-avatar.png (KiraAI no
 *    longer references an external plugin-page route or filesystem paths); the file
 *    must exist and is the only character image allowed under web/assets/;
 * 2) Avatar URL resolution: only the default asset or the plugin's own agent-avatar
 *    endpoint is accepted, dirty values fall back;
 * 3) Driver tolerance: canonical fields win; legacy matches without driver fall back
 *    to difficulty (advanced=agent).
 * Which seat gets which avatar is decided by the seatIdentity branches in match.js
 * (page side); here only the URL and driver resolution are locked down.
 *
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";

import fs from "node:fs";

import {
  AGENT_AVATAR_URL,
  HOST_PLACEHOLDER,
  driverOf,
  getAgentAvatar,
  getHostAgentName,
  getPersonaName,
  personaNameText,
  personaText,
  resolveAgentAvatar,
  setAgentAvatar,
  setHostAgentName,
  setPersonaName,
} from "../shared/mode.js";

const AGENT_AVATAR_PATH = "./assets/agent-avatar.png";

const PRACTICE_ADV = {
  mode_options: { mode: "bot", role: "teammate", driver: "agent", difficulty: "advanced" },
};
const PRACTICE_EASY = {
  mode_options: { mode: "bot", role: "teammate", driver: "bot", difficulty: "easy" },
};

test("头像地址是插件自己的相对素材路径，且文件真实存在", () => {
  assert.equal(AGENT_AVATAR_URL, AGENT_AVATAR_PATH);
  assert.ok(AGENT_AVATAR_URL.startsWith("./assets/"), "页面相对路径（静态挂载在 /page/plugin/…/hub/ 下）");
  assert.ok(AGENT_AVATAR_URL.endsWith(".png"));
  assert.ok(!AGENT_AVATAR_URL.includes(".."), "不越出插件页目录");
  assert.ok(!AGENT_AVATAR_URL.includes("file:"), "不是文件系统路径");
  assert.ok(!AGENT_AVATAR_URL.includes("/plugins/"), "不是仓库内直接路径");
  assert.ok(fs.existsSync(new URL(`../${AGENT_AVATAR_URL.slice(2)}`, import.meta.url)), "素材必须存在");
});

test("头像地址解析：只接受插件默认素材或插件自己的 agent-avatar 端点", () => {
  assert.equal(resolveAgentAvatar("./assets/agent-avatar.png"), "./assets/agent-avatar.png");
  assert.equal(
    resolveAgentAvatar("/api/plugin/kira-plugin-gamehub/agent-avatar?v=1700000000"),
    "/api/plugin/kira-plugin-gamehub/agent-avatar?v=1700000000"
  );
  for (const bad of ["", null, undefined, "   ", "https://evil.example/x.png", "/page/plugin/agent/raiy.png", "../../etc/passwd"]) {
    assert.equal(resolveAgentAvatar(bad), "./assets/agent-avatar.png", `脏值回落默认：${bad}`);
  }
});

test("界面共享状态：setAgentAvatar 归一化，getAgentAvatar 返回当前渲染地址", () => {
  assert.equal(getAgentAvatar(), "./assets/agent-avatar.png");
  assert.equal(setAgentAvatar("/api/plugin/kira-plugin-gamehub/agent-avatar?v=7"), "/api/plugin/kira-plugin-gamehub/agent-avatar?v=7");
  assert.equal(getAgentAvatar(), "/api/plugin/kira-plugin-gamehub/agent-avatar?v=7");
  assert.equal(setAgentAvatar("https://evil.example/x.png"), "./assets/agent-avatar.png", "外链不被采用");
  assert.equal(setAgentAvatar(undefined), "./assets/agent-avatar.png", "字段缺席回落默认");
});

test("人设名可配置：留空或 nori 渲染配置标识 nori（不做中文本地化），其它取值原样渲染", () => {
  assert.equal(personaNameText(""), "nori");
  assert.equal(personaNameText(null), "nori");
  assert.equal(personaNameText("  nori  "), "nori", "默认配置名原样返回");
  assert.equal(personaNameText("Nori"), "nori", "大小写不敏感，统一回落到配置标识");
  assert.equal(personaNameText(" 蕾依 "), "蕾依");
  assert.equal(getPersonaName(), "nori", "纯函数默认值不变（Node 无需初始化）");
  assert.equal(setPersonaName("蕾依"), "蕾依");
  assert.equal(getPersonaName(), "蕾依");
  assert.equal(personaText("nori会认真应战。"), "蕾依会认真应战。");
  assert.equal(setPersonaName("nori"), "nori", "恢复默认");
});

test("宿主名：未下发回落占位「AI」，空值同样回落（不再借用 game_agent 名）", () => {
  assert.equal(HOST_PLACEHOLDER, "AI", "占位记号就是中性 AI，模板只在渲染时替换");
  assert.equal(getHostAgentName(), HOST_PLACEHOLDER, "页面初始化前的中性称呼");
  assert.equal(setHostAgentName("汐祈（Seki）"), "汐祈（Seki）");
  assert.equal(getHostAgentName(), "汐祈（Seki）");
  assert.equal(setHostAgentName(""), "");
  assert.equal(getHostAgentName(), HOST_PLACEHOLDER, "空值回落占位「AI」，不借用 game_agent 名");
  assert.equal(getPersonaName(), "nori", "宿主名与 game_agent 名互不影响");
});

test("driverOf：规范字段优先；旧对局缺 driver 时按难度回填（advanced=agent/easy=bot）", () => {
  assert.equal(driverOf(PRACTICE_ADV), "agent");
  assert.equal(driverOf(PRACTICE_EASY), "bot");
  assert.equal(driverOf({ mode_options: { mode: "bot", difficulty: "advanced" } }), "agent");
  assert.equal(driverOf({ mode_options: { mode: "bot", difficulty: "easy" } }), "bot");
  assert.equal(driverOf({ mode_options: { mode: "bot" } }), "bot", "缺难度保守按简单规则 bot");
  assert.equal(driverOf({ mode_options: { mode: "nori" } }), "", "人设本人局没有陪练驱动");
  assert.equal(driverOf(null), "");
  assert.equal(driverOf({ options: { gomoku_mode: "practice", driver: "agent" } }), "agent", "候选位容错");
});

test("driverOf：mode 缺失但座位构成/驱动已写入的旧载荷，按后端 legacy 推断补齐陪练语义", () => {
  // 后端 normalize 对「mode_options 缺 mode」会按座位回填 bot=陪练、driver 照读——
  // 前端 modeOf 现按同一规则回填，driverOf 不再因 mode 空串直接放弃。
  const legacyAdvanced = { seats: [{ kind: "human" }, { kind: "bot" }], mode_options: { driver: "agent" } };
  assert.equal(driverOf(legacyAdvanced), "agent");
  // 纯 legacy bot 座位局（无任何选项行）：陪练语义补齐，缺驱动保守按规则 bot
  const legacyBot = { seats: [{ kind: "human" }, { kind: "bot" }] };
  assert.equal(driverOf(legacyBot), "bot");
  // An ai-seat match reads as host-agent-appears mode: no practice semantics
  const legacyNori = { seats: [{ kind: "human" }, { kind: "ai" }] };
  assert.equal(driverOf(legacyNori), "");
});
