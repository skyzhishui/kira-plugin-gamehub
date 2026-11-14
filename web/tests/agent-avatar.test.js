/* agent-avatar.test.js — 对局页头像归属（纯逻辑，node:test 零依赖）。
 *
 * 覆盖：
 * 1) 头像资源地址：插件自己的公共素材 ./assets/agent-avatar.png（KiraAI 不再
 *    引用外部插件页路由，也不出现文件系统路径）；该文件必须真实存在，且是
 *    web/assets/ 下唯一允许的角色图；
 * 2) 归属：只有「陪练局 + agent 驱动（进阶）」的非人类座位配进阶对手头像；
 *    简单规则 bot、人类座位、人设本人局一律不配；
 * 3) driver 容错：规范字段优先，旧对局缺 driver 时按难度回填（advanced=agent）。
 *
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import fs from "node:fs";

import {
  AGENT_AVATAR_URL,
  driverOf,
  getAgentAvatar,
  getPersonaName,
  isAgentOpponent,
  personaNameText,
  personaText,
  resolveAgentAvatar,
  setAgentAvatar,
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

test("归属：进阶陪练的对手（agent 驱动）才配进阶头像", () => {
  assert.equal(isAgentOpponent(PRACTICE_ADV, { kind: "ai", name: "搭档" }), true);
  assert.equal(isAgentOpponent(PRACTICE_ADV, { kind: "ai", name: "诺里" }), true, "名字不参与判定，按模式/驱动");
});

test("简单规则 bot 绝不冒用进阶头像", () => {
  assert.equal(isAgentOpponent(PRACTICE_EASY, { kind: "bot", name: "陪练" }), false);
  assert.equal(isAgentOpponent(PRACTICE_EASY, { kind: "ai", name: "搭档" }), false, "驱动是 bot 即不配");
  assert.equal(isAgentOpponent({ mode_options: { mode: "bot", difficulty: "easy" } }, { kind: "bot" }), false);
});

test("人设本人局 / 人类座位 / 缺数据一律不配进阶头像", () => {
  assert.equal(isAgentOpponent({ mode_options: { mode: "nori", driver: "turn" } }, { kind: "ai", name: "诺里" }), false);
  assert.equal(isAgentOpponent(PRACTICE_ADV, { kind: "human", name: "阿空" }), false);
  assert.equal(isAgentOpponent(null, { kind: "ai" }), false);
  assert.equal(isAgentOpponent(PRACTICE_ADV, null), false);
  assert.equal(isAgentOpponent({}, { kind: "ai" }), false, "旧对局无模式信息按人设本人局回填");
});

test("人设名可配置：留空或 nori 渲染默认「诺里」，其它取值原样渲染", () => {
  assert.equal(personaNameText(""), "诺里");
  assert.equal(personaNameText(null), "诺里");
  assert.equal(personaNameText("  nori  "), "诺里", "默认配置名仍是 nori");
  assert.equal(personaNameText("Nori"), "诺里", "大小写不敏感");
  assert.equal(personaNameText(" 阿狸 "), "阿狸");
  assert.equal(getPersonaName(), "诺里", "纯函数默认值不变（Node 无需初始化）");
  assert.equal(setPersonaName("阿狸"), "阿狸");
  assert.equal(getPersonaName(), "阿狸");
  assert.equal(personaText("诺里会认真应战。"), "阿狸会认真应战。");
  assert.equal(setPersonaName("nori"), "诺里", "恢复默认");
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
  assert.equal(isAgentOpponent(legacyAdvanced, { kind: "ai", name: "搭档" }), true);
  // 纯 legacy bot 座位局（无任何选项行）：陪练语义补齐，缺驱动保守按规则 bot
  const legacyBot = { seats: [{ kind: "human" }, { kind: "bot" }] };
  assert.equal(driverOf(legacyBot), "bot");
  assert.equal(isAgentOpponent(legacyBot, { kind: "bot" }), false, "规则 bot 不配进阶头像");
  // ai 座位局按人设本人局口径：无陪练语义，不配进阶头像
  const legacyNori = { seats: [{ kind: "human" }, { kind: "ai" }] };
  assert.equal(driverOf(legacyNori), "");
  assert.equal(isAgentOpponent(legacyNori, { kind: "ai", name: "诺里" }), false);
});
