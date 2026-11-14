/* nori-active-matches.test.js — M8「进行中的棋局」统一操作台测试（node:test，零依赖）。
 *
 * 覆盖三块真实逻辑（非纯 fixture 复述）：
 * 1) active_matches 契约解析：新后端富载荷归一化、同名玩家以「对局ID+座位」区分、
 *    旧后端缺字段/脏数据安全降级、终局与 AI 局不可接管判定；
 * 2) 「进入操作」编排：control 端点拿 token → sessionStorage → 跳 match
 *    （口令不进链接、缺 token/终局失败不落库不跳转）；
 * 3) 按局 ui-config：fetchUiConfig 携带 ?match_id=（初始化/热更新同一入口），
 *    不同对局解析出不同 chat_session / read_only，不共用全局配置。
 *
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  parseActiveMatches,
  isControllable,
  controlBlockedReason,
  sourceLabel,
  noticeSummary,
  turnSummary,
  playerKey,
  kindLabel,
  matchHref,
  enterControl,
} from "../shared/matches.js";
import { HOST_PLACEHOLDER } from "../shared/mode.js";
import { fetchUiConfig, chatSessionFromConfig, isChatReadOnly } from "../shared/chat.js";

/* ---------------- 1) 契约解析 ---------------- */

/* 按后端契约构造的载荷：两局进行中 + 终局 + bot 互弈，含同名不同 uid 玩家 */
const CONTRACT_PAYLOAD = {
  games: [{ kind: "gomoku", name: { zh: "五子棋" } }],
  current: null,
  active_matches: [
    {
      id: "a1b2c3d4",
      kind: "gomoku",
      game_name: "五子棋",
      players: [
        { seat: 0, kind: "human", actor: "uid-alice", name: "阿空" },
        { seat: 1, kind: "human", actor: "uid-alicia", name: "阿空" },
      ],
      source: { kind: "chat", platform: "qq", session_id: "qq-123" },
      status: { outcome: "", resultText: "" },
      move_count: 24,
      turn_text: "轮到 阿空（uid-alicia）行棋",
      notice_target: { session_id: "qq-123", platform: "qq", status: "override", error: "" },
      can_control: true,
    },
    {
      id: "b2c3d4e5",
      kind: "chess",
      game_name: "国际象棋",
      players: [
        { seat: 0, kind: "ai", actor: "", name: "汐祈（Seki）" },
        { seat: 1, kind: "human", actor: "uid-bob", name: "小波" },
      ],
      source: { kind: "webui", platform: "", session_id: "" },
      status: "",
      move_count: 3,
      turn_text: "汐祈（Seki）思考中…",
      notice_target: null,
      can_control: false,
    },
    {
      id: "c3d4e5f6",
      kind: "gomoku",
      game_name: "五子棋",
      players: [
        { seat: 0, kind: "human", actor: "uid-carl", name: "老卡" },
        { seat: 1, kind: "ai", actor: "", name: "汐祈（Seki）" },
      ],
      source: { kind: "chat", platform: "live2d", session_id: "l2d-9" },
      status: { outcome: "win", resultText: "黑方胜" },
      move_count: 51,
      turn_text: "黑方胜",
      notice_target: { session_id: "l2d-9", platform: "live2d", status: "invalid", error: "目标会话不存在，已回落网页" },
      can_control: false,
    },
    {
      id: "d4e5f6a7",
      kind: "gomoku",
      game_name: "五子棋",
      players: [
        { seat: 0, kind: "bot", actor: "", name: "陪练甲" },
        { seat: 1, kind: "bot", actor: "", name: "陪练乙" },
      ],
      source: { kind: "chat", platform: "", session_id: "s-x" },
      status: "",
      move_count: 0,
      turn_text: "",
      notice_target: {},
      can_control: false,
    },
  ],
};

test("契约解析：字段归一化 + 数量", () => {
  const ms = parseActiveMatches(CONTRACT_PAYLOAD);
  assert.equal(ms.length, 4);
  const [chatPair, webui, overMatch, botPair] = ms;
  assert.equal(chatPair.gameName, "五子棋");
  assert.equal(chatPair.moveCount, 24);
  assert.equal(chatPair.turnText, "轮到 阿空（uid-alicia）行棋");
  assert.equal(webui.source.kind, "webui");
  assert.equal(botPair.players.length, 2);
  assert.equal(botPair.notice.ready, false, "空 notice_target → 未就绪");
  assert.deepEqual(parseActiveMatches(null), [], "响应缺失不崩");
  assert.deepEqual(parseActiveMatches({ games: [] }), [], "旧后端无 active_matches → 空数组");
  assert.deepEqual(parseActiveMatches({ active_matches: [null, {}, { no_id: 1 }, "junk"] }), [], "脏条目一律丢弃");
});

test("同名玩家可区分：名字相同、user_id 不同、key=对局ID+座位（绝不用名字当 key）", () => {
  const [chatPair] = parseActiveMatches(CONTRACT_PAYLOAD);
  const [p0, p1] = chatPair.players;
  assert.equal(p0.name, "阿空");
  assert.equal(p1.name, "阿空", "两名玩家映射名相同");
  assert.notEqual(p0.actor, p1.actor, "user_id 不同 → 同名可区分");
  assert.equal(playerKey(chatPair.id, p0), "a1b2c3d4#0");
  assert.equal(playerKey(chatPair.id, p1), "a1b2c3d4#1");
  assert.notEqual(playerKey(chatPair.id, p0), playerKey(chatPair.id, p1), "key 按座位区分，不按名字");
  // 跨对局同座位也不冲突
  const other = { id: "zz", players: [p0] };
  assert.notEqual(playerKey(chatPair.id, p0), playerKey(other.id, p0));
});

test("可接管判定：can_control ∧ 未终局 ∧ 有人类座位（禁 AI/bot 座位）", () => {
  const [chatPair, webui, overMatch, botPair] = parseActiveMatches(CONTRACT_PAYLOAD);
  assert.equal(isControllable(chatPair), true, "chat 开局人类局可接管");
  assert.equal(isControllable(webui), false, "can_control=false 不可接管");
  assert.equal(isControllable(overMatch), false, "终局不可接管（终局不准 control）");
  assert.equal(isControllable(botPair), false, "bot 互弈无人类座位不可接管");
  // 防御：后端误给 can_control=true 但对局已结束 → 前端仍拒绝
  assert.equal(isControllable({ ...overMatch, canControl: true }), false, "终局防御");
  // 防御：无人类座位即使 can_control=true 也拒绝（禁 AI/bot 座位）
  assert.equal(isControllable({ ...botPair, canControl: true }), false, "AI 座位防御");
  assert.equal(isControllable(null), false);
});

test("不可接管原因文案：已结束 → AI 对局 → 暂不可接管", () => {
  const [chatPair, webui, overMatch, botPair] = parseActiveMatches(CONTRACT_PAYLOAD);
  assert.equal(controlBlockedReason(overMatch), "已结束");
  assert.equal(controlBlockedReason(botPair), "无人类玩家座位");
  assert.equal(controlBlockedReason(webui), "暂不可接管");
  assert.equal(controlBlockedReason(chatPair), "暂不可接管", "可控局不展示原因（按钮本身可点）");
});

test("开局来源与 notice 诊断文案", () => {
  const [chatPair, webui, overMatch, botPair] = parseActiveMatches(CONTRACT_PAYLOAD);
  assert.equal(sourceLabel(chatPair.source), "聊天开局 · qq");
  assert.equal(sourceLabel(webui.source), "网页开局");
  assert.equal(sourceLabel(botPair.source), "聊天开局", "无平台也能标明聊天来源");
  assert.equal(sourceLabel(null), "未知来源");

  const n = noticeSummary(chatPair.notice);
  assert.equal(n.ready, true);
  assert.equal(n.platform, "qq");
  assert.equal(n.statusText, "已指定");
  assert.equal(n.error, "");

  const bad = noticeSummary(overMatch.notice);
  assert.equal(bad.platform, "live2d");
  assert.equal(bad.statusText, "无效");
  assert.equal(bad.error, "目标会话不存在，已回落网页", "诊断 error 原样透出（textContent 渲染）");

  assert.equal(noticeSummary(null).ready, false);
  assert.equal(noticeSummary({}).ready, false);
  assert.equal(noticeSummary(botPair.notice).ready, false, "空对象视为未就绪");
});

test("轮次行：手数 + 轮次文案；缺省回落", () => {
  const [chatPair, webui, overMatch, botPair] = parseActiveMatches(CONTRACT_PAYLOAD);
  assert.equal(turnSummary(chatPair), "第 24 手 · 轮到 阿空（uid-alicia）行棋");
  assert.equal(turnSummary(webui), "第 3 手 · 汐祈（Seki）思考中…");
  assert.equal(turnSummary(overMatch), "第 51 手 · 黑方胜");
  assert.equal(turnSummary(botPair), "等待开局");
  assert.equal(turnSummary({ moveCount: null, turnText: "", status: { resultText: "白方胜" } }), "白方胜");
});

test("status 双形态归一：对象 {outcome} 与简字符串", () => {
  const ms = parseActiveMatches([
    { id: "s1", status: { outcome: "draw", resultText: "和棋" } },
    { id: "s2", status: "finished" },
    { id: "s3", status: "active" },
    { id: "s4" },
  ]);
  assert.equal(ms[0].status.over, true);
  assert.equal(ms[0].status.resultText, "和棋");
  assert.equal(ms[1].status.over, true, "字符串 finished 视为终局");
  assert.equal(ms[2].status.over, false, "active 不误判终局");
  assert.equal(ms[3].status.over, false, "缺省不算终局");
});

test("跳转地址：对局 ID 进 query 且编码；观战与接管共用同一地址形态", () => {
  assert.equal(matchHref("m1"), "./match.html?id=m1");
  assert.equal(matchHref("a b&c"), `./match.html?id=${encodeURIComponent("a b&c")}`);
  assert.equal(matchHref(""), "./match.html?id=");
});

test("kindLabel：宿主座位回落占位「AI」，bot 原样，人类不标注", () => {
  assert.equal(kindLabel("ai"), HOST_PLACEHOLDER);
  assert.equal(kindLabel("bot"), "陪练");
  assert.equal(kindLabel("human"), "");
});

/* ---------------- 2) 「进入操作」编排 ---------------- */

test("enterControl：control 拿 token → sessionStorage → 跳 match；口令不进链接", async () => {
  const calls = [];
  const stored = [];
  let nav = "";
  const rid = await enterControl("m-chat-1", {
    api: async (path) => {
      calls.push(path);
      assert.equal(path, "/matches/m-chat-1/control");
      return {
        match_id: "m-chat-1",
        seat: 1,
        token: "tok-xyz-42",
        url: "https://host/page/plugin/kira-plugin-gamehub/hub/match.html?id=m-chat-1&t=tok-xyz-42",
      };
    },
    writeToken: (id, t) => stored.push([id, t]),
    navigate: (href) => {
      nav = href;
    },
  });
  assert.equal(calls.length, 1, "只打一次 control 端点");
  assert.equal(rid, "m-chat-1");
  assert.deepEqual(stored, [["m-chat-1", "tok-xyz-42"]], "human token 存 sessionStorage 键");
  assert.equal(nav, "./match.html?id=m-chat-1");
  assert.ok(!nav.includes("tok"), "口令不进链接：响应 url 字段不用于跳转");
});

test("enterControl：以响应 match_id 为准落库与跳转", async () => {
  let nav = "";
  const stored = [];
  await enterControl("stale-id", {
    api: async () => ({ match_id: "fresh-id", token: "t2", url: "" }),
    writeToken: (id, t) => stored.push([id, t]),
    navigate: (href) => {
      nav = href;
    },
  });
  assert.deepEqual(stored, [["fresh-id", "t2"]]);
  assert.equal(nav, "./match.html?id=fresh-id");
});

test("enterControl：缺 token 失败——不落 storage、不跳转、错误上抛", async () => {
  const stored = [];
  let navigated = false;
  await assert.rejects(
    enterControl("m1", {
      api: async () => ({ match_id: "m1", token: "", url: "" }),
      writeToken: (id, t) => stored.push([id, t]),
      navigate: () => {
        navigated = true;
      },
    }),
    /口令/
  );
  assert.deepEqual(stored, [], "失败不写 token");
  assert.equal(navigated, false, "失败不跳转");
});

test("enterControl：终局/无权限（409）上抛，调用方可恢复按钮重试", async () => {
  let navigated = false;
  await assert.rejects(
    enterControl("m-over", {
      api: async () => {
        const err = new Error("对局已结束，不能接管");
        err.status = 409;
        throw err;
      },
      writeToken: () => {},
      navigate: () => {
        navigated = true;
      },
    }),
    /已结束/
  );
  assert.equal(navigated, false);
});

test("enterControl：缺对局 ID / 缺 api 注入直接失败", async () => {
  await assert.rejects(enterControl("", { api: async () => ({}) }), /对局 ID/);
  await assert.rejects(enterControl("m1", {}), /api/);
});

/* ---------------- 3) 按局 ui-config ---------------- */

test("fetchUiConfig 携带 ?match_id：初始化与热更新同参数；不传参保持旧路径", async () => {
  const prev = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), auth: (opts && opts.headers && opts.headers.Authorization) || "" });
    const q = new URL(url, "http://localhost").searchParams;
    const mid = q.get("match_id") || "";
    return {
      ok: true,
      json: async () => ({
        ok: true,
        data: {
          chat_enabled: true,
          page_session: "room-fixed",
          chat_session: mid === "m1" ? "sess-for-m1" : mid === "m2" ? "sess-for-m2" : "",
          chat_user: mid ? `user-${mid}` : "",
          chat_read_only: mid === "m2", // m2 为非 web 目标局 → 只读
        },
      }),
    };
  };
  try {
    const c1 = await fetchUiConfig("m1");
    assert.equal(calls[0].url, "/api/plugin/kira-plugin-gamehub/ui-config?match_id=m1");
    assert.equal(chatSessionFromConfig(c1), "sess-for-m1");
    assert.equal(isChatReadOnly(c1), false);

    const c2 = await fetchUiConfig("m2");
    assert.equal(chatSessionFromConfig(c2), "sess-for-m2", "不同局解析到不同会话（不共用全局配置）");
    assert.equal(isChatReadOnly(c2), true, "非 web 目标局只读");

    const c0 = await fetchUiConfig();
    assert.equal(calls[calls.length - 1].url, "/api/plugin/kira-plugin-gamehub/ui-config", "大厅调用不带参数");
    assert.equal(chatSessionFromConfig(c0), "room-fixed", "缺省回落固定会话（原行为不变）");

    await fetchUiConfig("a b&c/d");
    assert.equal(
      calls[calls.length - 1].url,
      `/api/plugin/kira-plugin-gamehub/ui-config?match_id=${encodeURIComponent("a b&c/d")}`
    );
    assert.ok(calls.every((c) => c.auth === ""), "Node 无 localStorage：无 token 时不带 Authorization（不崩）");
  } finally {
    globalThis.fetch = prev;
  }
});

test("按局聊天三件套：ui-config?match_id 返回什么就用什么（chat_session 优先，page_session 仅回落）", async () => {
  const prev = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({
      ok: true,
      data: {
        page_session: "room-fixed",
        chat_session: "web-target-of-this-match",
        chat_user: "playerB",
        chat_read_only: false,
        notice_target_session_id: "qq-other-platform",
      },
    }),
  });
  try {
    const cfg = await fetchUiConfig("m-web-9");
    assert.equal(chatSessionFromConfig(cfg), "web-target-of-this-match", "WebUI 操盘聊天局跟随本局 web 目标");
    assert.notEqual(chatSessionFromConfig(cfg), "room-fixed", "page_session 不作聊天目标");
    assert.equal(isChatReadOnly(cfg), false, "web 目标可写");
    assert.equal(chatSessionFromConfig(await fetchUiConfig("m-other")), "web-target-of-this-match");
  } finally {
    globalThis.fetch = prev;
  }
});
