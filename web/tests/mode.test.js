/* mode.test.js — S2–S4 对局模式 + 宿主代下适配层测试（node:test，零依赖）。
 *
 * 覆盖五块：
 * 1) 模式选择器 payload（S2）：与宿主对战默认 / 与陪练对战 + 简单/高级
 *    （后端规范形态 mode_options：mode=nori|bot；easy=bot 座位、advanced=ai 座位）
 *    + 旧契约回落；
 * 2) 代下（S3）：动作体、MatchView 代下态容错读取（mode_options.delegation 首选）、
 *    可代下判定、代下状态行文案（当前回合立即接手 / 等待本方回合）；
 * 3) 控制/观战（S4）：重开按钮文案（操盘者/观战者）、重开载荷沿用模式难度、
 *    观战者不可代下；
 * 4) 防冒充（队友区设计）：陪练局对手座位（advanced=ai 座位且后端存了别的座位名）
 *    展示名按驱动归属，不误挂宿主身份；nori 局保持宿主身份；大厅操作台同步改写；
 * 5) 文案约束：用户可见文案不出现技术词；宿主身份一律来自 getHostAgentName()，
 *    模板里的 HOST_PLACEHOLDER（"AI"）渲染时才落成宿主名，不写死任何身份词。
 *
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  supportsModes,
  normalizeMode,
  normalizeDifficulty,
  normalizeLevel,
  levelOf,
  levelVisible,
  opponentKindOf,
  modeLabel,
  agentNameText,
  getHostAgentName,
  setHostAgentName,
  modeOf,
  isPractice,
  delegatedOf,
  matchOver,
  canDelegate,
  delegatedTurnStatus,
  createMatchPayload,
  delegateAction,
  restartPayload,
  restartLabel,
  seatDisplay,
  seatKindLabel,
  MODE_LABELS,
  LEVEL_LABELS,
  LEVEL_HINTS,
  MODE_HINTS,
  MATE_NOTE_TEXT,
  HOST_PLACEHOLDER,
  LINEUP_LABELS,
  LINEUP_HINTS,
  LINEUP_VALUES,
  friendlyLineupLabel,
  lineupHint,
  modeHint,
  levelHint,
  practiceNameText,
} from "../shared/mode.js";
import { normalizeActiveMatch, kindLabel } from "../shared/matches.js";

/* ---------------- S2 模式选择器 payload ---------------- */

test("S2 payload：与宿主对战（默认）→ mode_options.mode=nori（不带 difficulty）", () => {
  assert.deepEqual(createMatchPayload("gomoku", { mode: "nori" }), {
    kind: "gomoku",
    mode_options: { mode: "nori" },
  });
});

test("S2 payload：对手三选一 + 三档等级（后端 mode=bot；level 只随 nori/agent 携带）", () => {
  assert.deepEqual(createMatchPayload("gomoku", { mode: "practice", difficulty: "easy" }), {
    kind: "gomoku",
    mode_options: { mode: "bot", difficulty: "easy" },
  });
  assert.deepEqual(createMatchPayload("gomoku", { mode: "practice", difficulty: "advanced" }), {
    kind: "gomoku",
    mode_options: { mode: "bot", difficulty: "advanced" },
  });
  // practice 缺 difficulty → 默认 easy（陪练先热身）
  assert.deepEqual(createMatchPayload("gomoku", { mode: "practice" }).mode_options.difficulty, "easy");
  // nori 模式不携带 difficulty
  assert.equal(
    "difficulty" in createMatchPayload("gomoku", { mode: "nori", difficulty: "advanced" }).mode_options,
    false
  );
  // 三档等级：nori / agent 局写入 level；陪练局不写
  assert.deepEqual(createMatchPayload("gomoku", { mode: "nori", level: "beginner" }), {
    kind: "gomoku",
    mode_options: { mode: "nori", level: "beginner" },
  });
  assert.deepEqual(createMatchPayload("gomoku", { mode: "practice", difficulty: "advanced", level: "advanced" }), {
    kind: "gomoku",
    mode_options: { mode: "bot", difficulty: "advanced", level: "advanced" },
  });
  assert.equal("level" in createMatchPayload("gomoku", { mode: "practice", difficulty: "easy", level: "advanced" }).mode_options, false);
  assert.equal(createMatchPayload("gomoku", { mode: "nori", level: "junk" }).mode_options.level, "intermediate", "脏值回落中级");
});

test("三档等级：归一化 / 缺省中级 / 可见性（宿主与 agent 显示，陪练不显示）", () => {
  assert.equal(normalizeLevel("BEGINNER"), "beginner");
  assert.equal(normalizeLevel("intermediate"), "intermediate");
  assert.equal(normalizeLevel("advanced"), "advanced");
  assert.equal(normalizeLevel("hard"), "");
  assert.equal(levelOf({ mode_options: { level: "advanced" } }), "advanced");
  assert.equal(levelOf({ mode_options: {} }), "intermediate", "缺省中级");
  assert.equal(levelOf(null), "intermediate");
  assert.equal(opponentKindOf({ mode_options: { mode: "nori" } }), "nori");
  assert.equal(opponentKindOf({ mode_options: { mode: "bot", driver: "agent", difficulty: "advanced" } }), "agent");
  assert.equal(opponentKindOf({ mode_options: { mode: "bot", driver: "bot", difficulty: "easy" } }), "bot");
  assert.equal(levelVisible({ mode_options: { mode: "nori" } }), true);
  assert.equal(levelVisible({ mode_options: { mode: "bot", driver: "agent", difficulty: "advanced" } }), true);
  assert.equal(levelVisible({ mode_options: { mode: "bot", driver: "bot", difficulty: "easy" } }), false);
  assert.equal(LEVEL_LABELS.beginner, "初级");
  assert.equal(LEVEL_LABELS.intermediate, "中级");
  assert.equal(LEVEL_LABELS.advanced, "高级");
  // 文案不把启发式候选说成「搜索最优」
  for (const t of Object.values(LEVEL_HINTS)) assert.ok(!/最优|搜索/.test(t) || t.includes("不保证最优"), t);
});

test("agent 公开名：modeLabel / agentNameText 动态，不写死人设", () => {
  assert.equal(modeLabel("agent", "蕾依"), "与蕾依对战");
  assert.equal(modeLabel("agent", ""), "与玩家对战", "缺席用中性「玩家」");
  assert.equal(modeLabel("nori", "蕾依"), `与${HOST_PLACEHOLDER}对战`, "nori 局对手是宿主，与 game_agent 名无关");
  assert.equal(modeLabel("bot", "蕾依"), "与陪练对战");
  assert.equal(agentNameText("  蕾依  "), "蕾依");
  assert.equal(agentNameText(null), "玩家");
});

test("mode nori 的对手是宿主：host_agent_name 覆盖文案，人设名不受影响", () => {
  // 未下发 host 名时回落中性占位「AI」（不再借用 game_agent 名）
  assert.equal(HOST_PLACEHOLDER, "AI");
  assert.equal(getHostAgentName(), HOST_PLACEHOLDER);
  assert.equal(modeLabel("nori", "蕾依"), `与${HOST_PLACEHOLDER}对战`);

  setHostAgentName("汐祈（Seki）");
  assert.equal(getHostAgentName(), "汐祈（Seki）");
  assert.equal(modeLabel("nori", "蕾依"), "与汐祈（Seki）对战");
  // 进阶座位仍是插件人设（game_agent），不受 host 名影响
  assert.equal(modeLabel("agent", "蕾依"), "与蕾依对战");
  assert.equal(modeLabel("bot", ""), "与陪练对战");
  // 身份小片同理
  assert.equal(seatKindLabel({ kind: "ai" }, false), "汐祈（Seki）");
  assert.equal(seatKindLabel({ kind: "ai" }, true), "对手");
  assert.equal(seatKindLabel({ kind: "bot" }, true), "陪练");
  // 态度文案跟随 host 名，人设模板不变
  assert.equal(modeHint("nori"), "汐祈（Seki）会认真应战。", "占位符替换后不留空格");

  setHostAgentName("");
  assert.equal(getHostAgentName(), HOST_PLACEHOLDER, "空值回落占位「AI」，不借用 game_agent 名");
});

test("S2 payload：旧契约回落（mode 未知 → 不带 mode_options，opponent 显式可传）", () => {
  assert.deepEqual(createMatchPayload("gomoku", {}), { kind: "gomoku", opponent: "ai" });
  assert.deepEqual(createMatchPayload("gomoku", { mode: "junk" }), { kind: "gomoku", opponent: "ai" });
  assert.deepEqual(createMatchPayload("gomoku", { opponent: "bot" }), { kind: "gomoku", opponent: "bot" });
  assert.deepEqual(
    createMatchPayload("gomoku", { mode: "practice", difficulty: "junk" }).mode_options.difficulty,
    "easy"
  );
});

test("S2 归一化：后端 bot 模式=陪练局；大小写/脏值；supportsModes 读目录能力", () => {
  assert.equal(normalizeMode(" NORI "), "nori");
  assert.equal(normalizeMode("Practice"), "practice");
  assert.equal(normalizeMode("bot"), "practice", "后端 MODE_BOT 即陪练局");
  assert.equal(normalizeMode("ai"), "");
  assert.equal(normalizeDifficulty("ADVANCED"), "advanced");
  assert.equal(normalizeDifficulty("hard"), "");
  // 目录字段优先（前端不再维护 kind 白名单）
  assert.equal(supportsModes({ kind: "gomoku", supports_turn: true }), true);
  assert.equal(supportsModes({ kind: "reversi", supports_turn: true }), true);
  assert.equal(supportsModes({ kind: "chess", supports_turn: false }), false);
  assert.equal(supportsModes({ kind: "gomoku", supports_turn: false }), false, "目录显式 false 定案");
  // 旧目录/旧后端缺字段 → 保守回落（回合驱动棋种）
  assert.equal(supportsModes({ kind: "gomoku" }), true);
  assert.equal(supportsModes({ kind: "reversi" }), true);
  assert.equal(supportsModes({ kind: "chess" }), false);
  // 旧式字符串调用（历史调用点）保持可用
  assert.equal(supportsModes("gomoku"), true);
  assert.equal(supportsModes("reversi"), true);
  assert.equal(supportsModes("chess"), false);
  assert.equal(supportsModes(""), false);
  assert.equal(supportsModes(null), false);
});

/* ---------------- MatchView 容错读取 ---------------- */

test("modeOf：mode_options 首选（mode=bot→practice），旧候选位均可读；mode 全缺时按座位构成回填（与后端 legacy 推断一致）", () => {
  assert.deepEqual(
    modeOf({ mode_options: { mode: "bot", difficulty: "advanced" } }),
    { mode: "practice", difficulty: "advanced" }
  );
  assert.deepEqual(modeOf({ mode_options: { mode: "nori" } }), { mode: "nori", difficulty: "" });
  assert.deepEqual(modeOf({ options: { gomoku_mode: "practice", difficulty: "easy" } }), {
    mode: "practice",
    difficulty: "easy",
  });
  assert.deepEqual(modeOf({ gomoku_mode: "nori" }), { mode: "nori", difficulty: "" });
  assert.deepEqual(modeOf({ mode: "practice", difficulty: "easy" }), { mode: "practice", difficulty: "easy" });
  assert.deepEqual(modeOf({ view: { gomoku_mode: "practice", difficulty: "easy" } }), {
    mode: "practice",
    difficulty: "easy",
  });
  assert.deepEqual(modeOf({ mode_options: { mode: "junk", difficulty: "junk" } }), {
    mode: "",
    difficulty: "",
  });
  assert.deepEqual(modeOf(null), { mode: "", difficulty: "" });
  assert.deepEqual(modeOf("junk"), { mode: "", difficulty: "" });
  // mode 全缺：bot 座位局（无 ai 座）回填陪练，与后端 normalize_mode_options 一致
  assert.deepEqual(
    modeOf({ seats: [{ kind: "human" }, { kind: "bot" }] }),
    { mode: "practice", difficulty: "" }
  );
  assert.deepEqual(
    modeOf({ seats: [{ kind: "bot" }, { kind: "bot" }] }),
    { mode: "practice", difficulty: "" }
  );
  // ai 座位局按 nori 局口径回填（后端 MODE_NORI）；前端保持空串=未知，不显式断言
  assert.deepEqual(modeOf({ seats: [{ kind: "human" }, { kind: "ai" }] }), { mode: "", difficulty: "" });
  // bot 与 ai 混座（后端 has_bot and not has_ai 不成立）不判陪练；座位缺失/畸形安全降级
  assert.deepEqual(modeOf({ seats: [{ kind: "bot" }, { kind: "ai" }] }), { mode: "", difficulty: "" });
  assert.deepEqual(modeOf({ seats: "junk" }), { mode: "", difficulty: "" });
  assert.equal(isPractice({ mode_options: { mode: "bot" } }), true);
  assert.equal(isPractice({ mode_options: { mode: "nori" } }), false);
  assert.equal(isPractice({ options: { gomoku_mode: "practice" } }), true);
  assert.equal(isPractice({ seats: [{ kind: "human" }, { kind: "bot" }] }), true);
});

test("S3 delegatedOf：mode_options.delegation 显式给出即定案（canonical false 压过一切陈旧回落）", () => {
  const base = { id: "m1", me: 0 };
  // 回归：规范字段显式 false 时，陈旧的 legacy delegated=true 不得翻案
  assert.equal(
    delegatedOf({ ...base, mode_options: { mode: "bot", delegation: false }, delegated: true }),
    false,
    "canonical 显式 false > legacy true"
  );
  assert.equal(
    delegatedOf({
      ...base,
      mode_options: { mode: "bot", delegation: false },
      delegated: true,
      status: { delegated: true },
      options: { delegated: true },
      view: { delegated: true },
      seats: [{ kind: "human", delegated: true }],
    }),
    false,
    "canonical 显式 false > 所有候选位真值"
  );
  assert.equal(
    delegatedOf({ ...base, mode_options: { mode: "bot", delegation: 0 }, delegated: 1 }),
    false,
    "显式 0 同样定案"
  );
  // 规范字段缺席 → 回落链照常（legacy true 生效）
  assert.equal(delegatedOf({ ...base, mode_options: { mode: "bot" }, delegated: true }), true);
  assert.equal(delegatedOf({ ...base, delegated: true }), true);
  // 规范字段显式真值照常生效
  assert.equal(delegatedOf({ ...base, mode_options: { mode: "bot", delegation: true } }), true);
});

test("S3 delegatedOf：mode_options.delegation 首选，其余候选位容错；真值形态宽容", () => {
  const base = { id: "m1", me: 0 };
  assert.equal(delegatedOf({ ...base, mode_options: { mode: "bot", delegation: true } }), true);
  assert.equal(delegatedOf({ ...base, mode_options: { mode: "bot", delegation: false } }), false);
  assert.equal(delegatedOf({ ...base, mode_options: { mode: "bot", delegation: 1 } }), true, "后端可能给 1/0");
  assert.equal(delegatedOf({ ...base, delegated: true }), true);
  assert.equal(delegatedOf({ ...base, delegated: "true" }), true, "后端可能给字符串");
  assert.equal(delegatedOf({ ...base, status: { delegated: true } }), true);
  assert.equal(delegatedOf({ ...base, options: { delegated: true } }), true);
  assert.equal(delegatedOf({ ...base, view: { delegated: true } }), true);
  assert.equal(
    delegatedOf({ ...base, seats: [{ kind: "human", delegated: true }, { kind: "bot" }] }),
    true,
    "座位级候选位（按 match.me 取座位）"
  );
  assert.equal(
    delegatedOf({ ...base, me: 1, seats: [{ kind: "human", delegated: true }, { kind: "bot" }] }),
    false,
    "座位级读取跟随 match.me，不误读别人座位"
  );
  assert.equal(delegatedOf(null), false);
  assert.equal(delegatedOf(undefined), false);
  assert.equal(delegatedOf({}), false);
  assert.equal(delegatedOf({ seats: [{ delegated: "yes" }] }), false, '"yes" 不是布尔真值形态');
});

/* ---------------- S3 可代下判定 + 状态行 ---------------- */

/* 陪练局 · 高级：对手座位是 ai 且后端存了别的座位名（真实冒充场景） */
const PRACTICE_ADV = (over = {}) => ({
  id: "mp",
  kind: "gomoku",
  me: 0,
  mode_options: { mode: "bot", role: "teammate", driver: "agent", difficulty: "advanced", delegation: false },
  seats: [
    { kind: "human", name: "阿空", joined: true },
    { kind: "ai", name: "陪练", joined: true },
  ],
  status: { waitingOn: 1, outcome: "", resultText: "" },
  view: { toPlay: 2 },
  ...over,
});

test("S3 canDelegate：陪练局 + 本人在座 + 未终局；观战/nori 局/终局一律不行", () => {
  assert.equal(canDelegate(PRACTICE_ADV()), true);
  assert.equal(canDelegate({ ...PRACTICE_ADV(), me: null }), false, "观战不可代下");
  assert.equal(canDelegate({ ...PRACTICE_ADV(), me: -1 }), false);
  assert.equal(canDelegate({ ...PRACTICE_ADV(), me: 1 }), false, "me=1 是对手座位（ai），不算在座人类");
  const nori = { id: "mn", me: 0, mode_options: { mode: "nori" }, status: {}, view: {}, seats: [] };
  assert.equal(canDelegate(nori), false, "与宿主对战没有代下");
  assert.equal(canDelegate({ ...PRACTICE_ADV(), status: { outcome: "win", resultText: "黑方胜" } }), false, "终局不可代下");
  assert.equal(canDelegate({ ...PRACTICE_ADV(), view: { winLine: [1, 2, 3, 4, 5] } }), false, "有胜线即终局（前端防御）");
  assert.equal(canDelegate(null), false);
  assert.equal(canDelegate({}), false, "无模式信息不渲染代下按钮");
});

test("S3 matchOver 与 match.js isOver 同语义（终局信号按棋种：winLine/full/over）", () => {
  assert.equal(matchOver({ status: { outcome: "win" } }), true);
  assert.equal(matchOver({ status: { outcome: "" }, view: { full: true } }), true);
  assert.equal(matchOver({ status: {}, view: { winLine: [0, 1, 2, 3, 4] } }), true);
  assert.equal(matchOver({ status: {}, view: { over: true } }), true, "黑白棋终局信号 over");
  assert.equal(matchOver({ status: { outcome: "" }, view: { over: false } }), false);
  assert.equal(matchOver({ status: { outcome: "" }, view: { toPlay: 2 } }), false);
  assert.equal(matchOver({ status: {} }), false, "无视图不误判终局");
  assert.equal(matchOver(null), false);
});

test("S3 代下状态行：本方回合立即接手（占位「AI」代下中…）；对方回合等待接手；未开启返回 null", () => {
  assert.deepEqual(delegatedTurnStatus({ mine: true, delegated: true, myTurn: true }), {
    kind: "delegated",
    text: `${HOST_PLACEHOLDER}代下中…`,
  });
  assert.deepEqual(delegatedTurnStatus({ mine: true, delegated: true, myTurn: false }), {
    kind: "wait",
    text: `代下已开启：轮到你时${HOST_PLACEHOLDER}接手`,
  });
  setHostAgentName("汐祈（Seki）");
  try {
    assert.deepEqual(delegatedTurnStatus({ mine: true, delegated: true, myTurn: true }), {
      kind: "delegated",
      text: "汐祈（Seki）代下中…",
    }, "宿主名下发后状态行跟随");
  } finally {
    setHostAgentName("");
  }
  assert.equal(delegatedTurnStatus({ mine: true, delegated: false, myTurn: true }), null);
  assert.equal(delegatedTurnStatus({ mine: false, delegated: true, myTurn: false }), null, "观战者不走这条分支");
});

test("S3 delegateAction 动作体：布尔归一 + type=delegate", () => {
  assert.deepEqual(delegateAction(true), { type: "delegate", delegated: true });
  assert.deepEqual(delegateAction(false), { type: "delegate", delegated: false });
  assert.deepEqual(delegateAction(1), { type: "delegate", delegated: true });
});

/* ---------------- 防冒充：座位展示名 / 身份小片 / 大厅操作台 ---------------- */

test("座位名：陪练局对手座位展示服务端真实名字（agent 配置名），不再强行覆写陪练", () => {
  const agentSeat = { kind: "ai", name: "林夕" };
  const namedBot = { kind: "bot", name: "陪练甲" };
  const human = { kind: "human", name: "阿空" };
  assert.equal(seatDisplay(agentSeat, 1, true), "林夕", "陪练局 agent 座位用实际配置名");
  assert.equal(seatDisplay(namedBot, 1, true), "陪练甲", "有名字就原样展示");
  assert.equal(seatDisplay(human, 0, true), "阿空", "人类座位名字不受模式影响");
  assert.equal(seatDisplay(agentSeat, 1, false), "林夕", "nori 局同样保持服务端名字");
  assert.equal(seatDisplay({ kind: "ai" }, 1, true), "对手", "agent 座位名字缺席兜底对手");
  assert.equal(seatDisplay({ kind: "ai", name: "  " }, 1, true), "对手", "纯空白视同缺席");
  assert.equal(seatDisplay({ kind: "bot" }, 1, true), "陪练", "规则陪练座位兜底陪练");
  assert.equal(seatDisplay({ kind: "bot" }, 1, false), "白方", "非陪练局无名字按棋色兜底");
  assert.equal(seatDisplay(undefined, 0, false), "黑方");
  assert.equal(seatDisplay(undefined, 1, false), "白方");
  assert.equal(seatDisplay({ kind: "human" }, 0, true), "黑方", "无名字人类座位按棋色兜底");
});

test("防冒充：身份小片——nori 局 ai 座位=宿主；陪练局任何非人类座位=对手/陪练", () => {
  setHostAgentName("");
  assert.equal(seatKindLabel({ kind: "ai" }, false), HOST_PLACEHOLDER, "宿主名未下发回落占位「AI」");
  setHostAgentName("汐祈（Seki）");
  try {
    assert.equal(seatKindLabel({ kind: "ai" }, false), "汐祈（Seki）");
  } finally {
    setHostAgentName("");
  }
  // 收尾复位，避免把宿主名泄漏给后续测试
  assert.equal(getHostAgentName(), HOST_PLACEHOLDER);
  assert.equal(seatKindLabel({ kind: "ai" }, true), "对手");
  assert.equal(seatKindLabel({ kind: "bot" }, true), "陪练");
  assert.equal(seatKindLabel({ kind: "bot" }, false), "陪练");
  assert.equal(seatKindLabel({ kind: "human" }, true), "");
  assert.equal(seatKindLabel(undefined, false), "");
  // 陪练局 ai 座位即使服务端存了「陪练」这种名字，身份小片仍按驱动给「对手」
  assert.equal(seatKindLabel(PRACTICE_ADV().seats[1], true), "对手", "身份按驱动判定，不按名字");
});

test("大厅操作台（active_matches）：陪练局对手座位展示真实配置名，名字缺席兜底陪练", () => {
  const m = normalizeActiveMatch({
    id: "mp",
    kind: "gomoku",
    mode_options: { mode: "bot", difficulty: "advanced", delegation: true },
    players: [
      { seat: 0, kind: "human", actor: "uid-other", name: "小陪" },
      { seat: 1, kind: "ai", actor: "", name: "林夕" },
    ],
    status: { outcome: "", resultText: "" },
  });
  assert.equal(m.practice, true);
  assert.equal(m.players[1].name, "林夕", "agent 实际配置名不得被覆写为陪练");
  assert.equal(m.players[0].name, "小陪");
  const unnamed = normalizeActiveMatch({
    id: "mp2",
    kind: "gomoku",
    mode_options: { mode: "bot", difficulty: "advanced" },
    players: [{ seat: 1, kind: "ai", actor: "" }],
  });
  assert.equal(unnamed.players[0].name, "对手", "agent 座位名字缺席兜底对手");
  const nori = normalizeActiveMatch({
    id: "mn",
    kind: "gomoku",
    mode_options: { mode: "nori" },
    players: [{ seat: 1, kind: "ai", actor: "", name: "汐祈（Seki）" }],
  });
  assert.equal(nori.practice, false);
  assert.equal(nori.players[0].name, "汐祈（Seki）", "nori 局对手座位保持服务端名字");
  const legacy = normalizeActiveMatch({ id: "ml", players: [{ seat: 1, kind: "ai", name: "汐祈（Seki）" }] });
  assert.equal(legacy.practice, false, "旧数据（无 mode_options）按 nori 局回填");
  assert.equal(legacy.players[0].name, "汐祈（Seki）");
});

test("防冒充：kindLabel 模式感知（宿主座位回落占位「AI」，兼容既有调用）", () => {
  setHostAgentName("");
  assert.equal(kindLabel("ai"), HOST_PLACEHOLDER);
  assert.equal(kindLabel("ai", true), "对手");
  assert.equal(kindLabel("bot"), "陪练");
  assert.equal(kindLabel("bot", true), "陪练");
  assert.equal(kindLabel("human"), "");
});

/* ---------------- S4 控制 / 观战 ---------------- */

test("S4 重开按钮文案：操盘者 重开一局/再来一局（终局）；观战者 我也来一局", () => {
  assert.equal(restartLabel({ spectator: false, over: false }), "重开一局");
  assert.equal(restartLabel({ spectator: false, over: true }), "再来一局");
  assert.equal(restartLabel({ spectator: true, over: false }), "我也来一局");
  assert.equal(restartLabel({ spectator: true, over: true }), "我也来一局");
});

test("S4 重开载荷：陪练局沿用模式+难度+等级；nori 局沿用 nori；旧对局按座位推断回落旧契约", () => {
  assert.deepEqual(restartPayload(PRACTICE_ADV()), {
    kind: "gomoku",
    mode_options: { mode: "bot", difficulty: "advanced", level: "intermediate" },
  });
  assert.deepEqual(
    restartPayload({ kind: "gomoku", seats: [], mode_options: { mode: "bot", difficulty: "advanced", level: "advanced" } }),
    { kind: "gomoku", mode_options: { mode: "bot", difficulty: "advanced", level: "advanced" } },
    "高级重开保留 level"
  );
  assert.deepEqual(
    restartPayload({ kind: "gomoku", seats: [], mode_options: { mode: "bot", difficulty: "easy", level: "advanced" } }),
    { kind: "gomoku", mode_options: { mode: "bot", difficulty: "easy" } },
    "陪练局重开不带 level"
  );
  assert.deepEqual(
    restartPayload({ kind: "gomoku", seats: [], mode_options: { mode: "nori" } }),
    { kind: "gomoku", mode_options: { mode: "nori", level: "intermediate" } },
    "nori 局重开带上等级（缺省中级）"
  );
  assert.deepEqual(
    restartPayload({ kind: "gomoku", seats: [{ kind: "human" }, { kind: "bot" }] }),
    { kind: "gomoku", mode_options: { mode: "bot", difficulty: "easy" } },
    "旧 bot 座位局 → 座位推断陪练，规范载荷（与后端 legacy 回填一致）"
  );
  assert.deepEqual(
    restartPayload({ kind: "gomoku", seats: [{ kind: "human" }, { kind: "ai" }] }),
    { kind: "gomoku", opponent: "ai" }
  );
  assert.deepEqual(restartPayload({ kind: "怪 kind", seats: [] }), { kind: "gomoku", opponent: "bot" });
  assert.deepEqual(restartPayload(null), { kind: "gomoku", opponent: "bot" });
});

/* ---------------- 文案约束 ---------------- */

// Forbidden identity literals, assembled at runtime so this test file itself stays
// clean for copy audits (grep for the literal words must find nothing here).
const FORBIDDEN_IDENTITY = ["主" + " AI", "诺" + "里"];

test("用户可见文案不含技术词，且身份只来自宿主名（不含写死身份词/人设名）", () => {
  const texts = [
    ...Object.values(MODE_LABELS),
    ...Object.values(LEVEL_LABELS),
    ...Object.values(LEVEL_HINTS),
    ...Object.values(LINEUP_LABELS),
    ...Object.values(LINEUP_HINTS),
    modeLabel("agent", "蕾依"),
    modeLabel("agent", ""),
    modeLabel("nori", "蕾依"),
    ...Object.values(MODE_HINTS),
    MATE_NOTE_TEXT,
    HOST_PLACEHOLDER,
    "队友",
    "代下中",
    "可代下",
    "陪练",
    modeHint("nori"),
    modeHint("practice", "easy"),
    modeHint("practice", "advanced"),
    modeHint("practice", "junk"),
    delegatedTurnStatus({ mine: true, delegated: true, myTurn: true }).text,
    delegatedTurnStatus({ mine: true, delegated: true, myTurn: false }).text,
    delegatedTurnStatus({ mine: true, delegated: true, myTurn: true, act: "代打" }).text,
    restartLabel({ spectator: true, over: false }),
    restartLabel({ spectator: false, over: false }),
    restartLabel({ spectator: false, over: true }),
    "停止代下",
    "开始对局",
  ];
  for (const t of texts) {
    assert.ok(!/artificial|bot/i.test(t), `文案出现技术词: ${t}`);
  }
  // 静态模板里不带任何写死的身份词：宿主身份只能由 getHostAgentName() 注入
  for (const t of texts) {
    for (const bad of FORBIDDEN_IDENTITY) {
      assert.ok(!t.includes(bad), `文案写死了身份词（${bad}）: ${t}`);
    }
  }
  // 关键文案锁定（占位符 + 配置名语义，与需求一致）
  assert.equal(MODE_LABELS.nori, `与${HOST_PLACEHOLDER}对战`);
  assert.equal(MODE_LABELS.agent, "与搭档对战");
  assert.equal(MODE_LABELS.bot, "与陪练对战");
});

test("模板身份词守门：MODE/LINEUP/MATE_NOTE 常量不含写死身份词，宿主名只在渲染时注入", () => {
  const templates = [
    ...Object.values(MODE_LABELS),
    ...Object.values(MODE_HINTS),
    ...Object.values(LINEUP_LABELS),
    ...Object.values(LINEUP_HINTS),
    MATE_NOTE_TEXT,
  ];
  for (const t of templates) {
    for (const bad of FORBIDDEN_IDENTITY) {
      assert.ok(!t.includes(bad), `模板含写死身份词（${bad}）: ${t}`);
    }
  }
  // 渲染后（宿主名 + game_agent 名都下发）同样不出现写死身份词
  setHostAgentName("汐祈（Seki）");
  try {
    const rendered = [
      modeLabel("nori", "蕾依"),
      ...Object.values(LINEUP_LABELS).map((label) => friendlyLineupLabel(label, "", "蕾依")),
      ...LINEUP_VALUES.map((v) => lineupHint(v, "蕾依")),
      ...LINEUP_VALUES.map((v) => friendlyLineupLabel("", LINEUP_LABELS[v], "蕾依")),
      modeHint("nori"),
      delegatedTurnStatus({ mine: true, delegated: true, myTurn: true }).text,
    ];
    for (const t of rendered) {
      for (const bad of FORBIDDEN_IDENTITY) {
        assert.ok(!t.includes(bad), `渲染文案含写死身份词（${bad}）: ${t}`);
      }
      assert.ok(t.includes("汐祈（Seki）"), `宿主身份未注入: ${t}`);
    }
  } finally {
    setHostAgentName("");
  }
});

test("modeHint 三态文案：宿主应战 / agent 应战 / 陪练（不附额外解释）", () => {
  assert.equal(modeHint("nori"), `${HOST_PLACEHOLDER}会认真应战。`, "宿主名未下发：占位「AI」");
  assert.equal(modeHint("agent"), "会认真应战。");
  assert.equal(modeHint("bot"), "轻松的切磋");
  assert.equal(modeHint(null), `${HOST_PLACEHOLDER}会认真应战。`, "对手缺省按与宿主对战");
  setHostAgentName("汐祈（Seki）");
  try {
    assert.equal(modeHint("nori"), "汐祈（Seki）会认真应战。", "宿主名替换占位符，且不留空格");
  } finally {
    setHostAgentName("");
  }
});

test("三档等级态度文案：与<name>轻松的切磋 / <name>会认真应战。 / <name>会全力以赴。", () => {
  assert.equal(LEVEL_HINTS.beginner, "轻松的切磋");
  assert.equal(LEVEL_HINTS.intermediate, "会认真应战。");
  assert.equal(LEVEL_HINTS.advanced, "会全力以赴。");
  const host = getHostAgentName();
  assert.equal(levelHint("beginner", host), `与${host}轻松的切磋`);
  assert.equal(levelHint("intermediate", host), `${host}会认真应战。`);
  assert.equal(levelHint("advanced", host), `${host}会全力以赴。`);
  assert.equal(levelHint("beginner", "蕾依"), "与蕾依轻松的切磋", "名字即对手名（宿主/搭档/陪练各按实际名）");
  assert.equal(levelHint("beginner", "小白"), "与小白轻松的切磋");
  assert.equal(levelHint("intermediate", "蕾依"), "蕾依会认真应战。");
  assert.equal(levelHint("advanced", "蕾依"), "蕾依会全力以赴。");
  assert.equal(levelHint("junk", host), `${host}会认真应战。`, "脏值回落中级");
  assert.equal(levelHint("beginner", ""), "与对手轻松的切磋", "名字缺席中性称呼");
  // 态度文案即全部：不再附「只做基础应手/威胁位置/前三候选」解释
  for (const t of Object.values(LEVEL_HINTS)) {
    assert.ok(!/威胁|候选|基础应手|分析/.test(t), t);
  }
});

test("陪练显示名：practiceNameText 服务端优先，缺席兜底「陪练」", () => {
  assert.equal(practiceNameText("小白"), "小白");
  assert.equal(practiceNameText("  小蓝  "), "小蓝");
  assert.equal(practiceNameText(""), "陪练");
  assert.equal(practiceNameText(null), "陪练");
});
