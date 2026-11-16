/* bubble.test.js — seat-speech bubble watermark + teammate-zone CSS anchors
 * (node:test, zero deps).
 *
 * Covers two areas:
 * 1) Queue watermark: the first frame only seeds (never displays), keys dedupe, a
 *    batch queues in arrival order, same text under different keys shows twice each
 *    (per-entry 1s/3s timing lives in bubble-queue.test.js);
 * 2) practice-opponent game_agent say entries (match.chat {seat,text,t}): the first
 *    frame only seeds and never pops; only new non-human seat entries enter; a
 *    human's hand-typed say never shows a bubble.
 *
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { pickNewMatchSays, matchSayKey } from "../shared/chat.js";
import { createBubbleQueue } from "../shared/bubble-queue.js";

/* ---------------- queue watermark (seed / dedupe / order) ----------------
 * The match page has no "refetch host chat history" path: bubbles are driven only by
 * match.chat snapshot deltas (watermark signature = matchSayKey). These tests lock the
 * queue's own seed/dedupe semantics instead of modelling the removed host-history API.
 */

/** 不触发任何计时的气泡队列：只观察是否入队/是否被去重。 */
function memoryQueue() {
  const shown = [];
  const q = createBubbleQueue({
    setTimer: () => 1,
    clearTimer: () => {},
    onShow: (item) => shown.push(item.key),
  });
  return { q, shown };
}

test("首帧播种：seed 记水位且不弹旧条目；同 key 再入队被去重", () => {
  const { q, shown } = memoryQueue();
  assert.equal(q.seed(["say:1", "say:2"]), 2, "播种返回新增条数");
  assert.equal(shown.length, 0, "首帧绝不自动弹历史气泡");
  assert.equal(q.enqueue({ key: "say:2", text: "旧" }), "duplicate");
  assert.equal(q.enqueue({ key: "say:3", text: "新" }), "queued");
  assert.deepEqual(shown, ["say:3"]);
});

test("同一批次的多条新条目：按到达顺序全部入队（不取最新、不合并）", () => {
  const { q, shown } = memoryQueue();
  q.seed(["say:1"]);
  ["say:3", "say:4"].forEach((key) => q.enqueue({ key, text: "x" }));
  assert.deepEqual(shown, ["say:3"], "首条立即展示");
  assert.deepEqual(q.pendingKeys(), ["say:4"], "后续条目排队，不被最新一条吞掉");
  assert.equal(q.enqueue({ key: "say:1", text: "x" }), "duplicate", "已播种的旧 key 不再入队");
});

test("相同内容不同 key：两条独立消息，各自入队（不误吞同文本新消息）", () => {
  const { q } = memoryQueue();
  assert.equal(q.enqueue({ key: "say:8", text: "好" }), "queued");
  assert.equal(q.enqueue({ key: "say:9", text: "好" }), "queued", "同文不同 key 是两条");
});

/* ---------------- practice 对手 say 水位（match.chat） ---------------- */

const PRACTICE = {
  id: "mp",
  mode_options: { mode: "bot", role: "teammate", difficulty: "advanced" },
  seats: [
    { kind: "human", name: "阿空" },
    { kind: "ai", name: "林夕" },
  ],
  me: 0,
};

test("match.chat 首帧旧 speech：只播种水位，pickNewMatchSays 不返回", () => {
  const oldChat = [{ seat: 1, text: "历史旧发言", t: 10, at: 1 }];
  const seeded = new Set(oldChat.map(matchSayKey));
  const { nextSeen, entries } = pickNewMatchSays(oldChat, seeded, {
    practice: true,
    seats: PRACTICE.seats,
    me: 0,
  });
  assert.equal(entries.length, 0, "initial snapshot 不回放历史 speech");
  assert.equal(nextSeen.size, 1);
});

test("match.chat 新 say：仅陪练局非人类座位展示，人类手打 say 不弹", () => {
  const seeded = new Set([matchSayKey({ seat: 1, text: "旧", t: 10, at: 1 })]);
  const chat = [
    { seat: 1, text: "旧", t: 10, at: 1 },
    { seat: 0, text: "人类手打", t: 11, at: 2 },
    { seat: 1, text: "新发言", t: 12, at: 3 },
  ];
  const { entries } = pickNewMatchSays(chat, seeded, {
    practice: true,
    seats: PRACTICE.seats,
    me: 0,
  });
  assert.deepEqual(
    entries.map((e) => e.text),
    ["新发言"],
    "只对手座位 say 弹；人类 say 不进气泡"
  );
});

test("match.chat：非陪练局（人设本人局）一律不弹对手 say（人设走自己的座位气泡）", () => {
  const seeded = new Set();
  const chat = [{ seat: 1, text: "对手发言", t: 1, at: 1 }];
  const { entries } = pickNewMatchSays(chat, seeded, {
    practice: false,
    seats: PRACTICE.seats,
    me: 0,
  });
  assert.equal(entries.length, 0);
});

test("match.chat 同版本帧（say 与落子原子共用 version）：新 say 仍被识别并展示", () => {
  // 后端契约：say 随落子原子追加，共用同一 version；前端水位按签名而非
  // version，同版本帧里的新条目必须照常进入。
  const seeded = new Set([matchSayKey({ seat: 1, text: "旧", t: 10, at: 1 })]);
  const chat = [
    { seat: 1, text: "旧", t: 10, at: 1 },
    { seat: 1, text: "原子随落子的 say", t: 20, at: 2 },
  ];
  const { entries } = pickNewMatchSays(chat, seeded, {
    practice: true,
    seats: PRACTICE.seats,
    me: 0,
  });
  assert.deepEqual(entries.map((e) => e.text), ["原子随落子的 say"]);
});

test("match.chat 同 t 但 at 不同：签名区分（同一毫秒两条 say 不丢）", () => {
  const a = { seat: 1, text: "一", t: 20, at: 2 };
  const b = { seat: 1, text: "二", t: 20, at: 3 };
  assert.notEqual(matchSayKey(a), matchSayKey(b));
  const { entries } = pickNewMatchSays([a, b], new Set([matchSayKey(a)]), {
    practice: true,
    seats: PRACTICE.seats,
    me: 0,
  });
  assert.deepEqual(entries.map((e) => e.text), ["二"]);
});

test("match.chat 缺 seat 字段的脏条目：签名可生成但不弹（无座位不展示）", () => {
  const { entries } = pickNewMatchSays([{ text: "无座位", t: 1 }], new Set(), {
    practice: true,
    seats: PRACTICE.seats,
    me: 0,
  });
  assert.equal(entries.length, 0);
});

test("match.chat allowSeat 拥有完整裁决权（收整条 say）：可按 by 标记放行代下宿主发言", () => {
  // 代下宿主的 say 按人类座位落库（by="host"）：旧实现里内置的人类座位
  // 过滤先于回调执行，这类条目永远被拒。新契约：allowSeat 是唯一裁决者，
  // 按条目的 by 标记精确放行，其余仍按座位身份拒绝。
  const seeded = new Set([matchSayKey({ seat: 1, text: "旧", t: 10, at: 1 })]);
  const chat = [
    { seat: 1, text: "旧", t: 10, at: 1 },
    { seat: 0, text: "人类手打", t: 11, at: 2 },
    { seat: 1, text: "对手新发言", t: 12, at: 3 },
    { seat: 0, text: "我替你守这里", t: 13, at: 4, by: "host" },
  ];
  const { entries } = pickNewMatchSays(chat, seeded, {
    practice: true,
    seats: PRACTICE.seats,
    me: 0,
    allowSeat: (seatIndex, seat, entry) =>
      (entry && entry.by === "host") ||
      (!!seat && seat.kind !== "human" && seatIndex !== 0),
  });
  assert.deepEqual(
    entries.map((e) => e.text),
    ["对手新发言", "我替你守这里"],
    "代下宿主发言（人类座位 + by=host）放行；人类手打 say 仍拒绝"
  );
});

/* ---------------- match.js wiring: one bubble per seat queue ---------------- */

/* Normalise line endings before source-text assertions: multi-line regexes must not
 * depend on LF, or a CRLF checkout fails spuriously. */
const matchSrc = fs
  .readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "match.js"), "utf8")
  .replace(/\r\n/g, "\n");

test("match.js：没有宿主聊天输入/SSE（旁白走插件 notice 通道，页面只读气泡）", () => {
  assert.doesNotMatch(matchSrc, /chat-form|chat-text|chat-send|sendChatMessage|NoriStream/, "不得保留聊天输入与 SSE");
  assert.doesNotMatch(matchSrc, /fetchWebchatConfig|webchat\/chat/, "不得调用宿主 webchat 接口");
  assert.match(matchSrc, /initUiConfig\(\)/, "ui-config 仍按局拉取（气泡上限/只读/人设名）");
});

test("match.js：座位 say 增量按到达顺序入各自座位队列（不覆盖成最新一条）", () => {
  const fn = matchSrc.match(/function showSeatSays\([\s\S]*?\n\}/);
  assert.ok(fn, "缺少 showSeatSays");
  assert.match(fn[0], /seatQueue\(idx\)\.enqueue\(\{ key, text: t \}\)/, "对手 say 入各自座位队列");
  const apply = matchSrc.match(/function applyMatchChat\([\s\S]*?\n\}/);
  assert.match(apply[0], /matchSayKey\(e\)\)/, "签名作为去重键（同 t 不同 at 不丢）");
  assert.match(apply[0], /chat\.saySeen = new Set\(arr\.map\(matchSayKey\)/, "首帧只播种水位");
});

test("match.js：离页清理销毁全部座位气泡排队器（清计时器与排队）", () => {
  const dispose = matchSrc.match(/function disposeChat\(\) \{[\s\S]*?\n\}/);
  assert.ok(dispose, "缺少 disposeChat");
  assert.match(dispose[0], /hideAllSeatBubbles\(\)/);
  const hide = matchSrc.match(/function hideAllSeatBubbles\(\) \{[\s\S]*?\n\}/);
  assert.ok(hide);
  assert.match(hide[0], /chat\.seatQueues\.forEach\(\(q\) => q\.dispose\(\)\)/, "销毁排队器");
});

/* ---------------- app.css：合法注释 + 头像锚点 / 375 边界 ---------------- */

const css = fs
  .readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "app.css"), "utf8")
  .replace(/\r\n/g, "\n");

test("app.css 无 CSS 非法 `//` 行注释（旧缺陷会丢弃后续规则）", () => {
  const illegal = css
    .split("\n")
    .map((line, i) => ({ line: line.trim(), n: i + 1 }))
    .filter(({ line }) => line.startsWith("//"));
  assert.deepEqual(illegal, [], "发现 // 行注释会导致 CSS 解析丢弃规则");
});

test("队友区头像包裹层实际声明 position:relative（气泡锚点）", () => {
  const m = css.match(/\.mate-avatar-wrap\s*\{([^}]*)\}/);
  assert.ok(m, "缺少 .mate-avatar-wrap 规则");
  assert.match(m[1], /position\s*:\s*relative/, "气泡必须锚在头像包裹层上");
});

test("375 档队友气泡有视口内宽度上限（头像右侧、不越界）", () => {
  assert.match(
    css,
    /@media\s*\(max-width:\s*480px\)\s*\{[\s\S]*?\.mate-zone\s+\.seat-bubble\s*\{[\s\S]*?left\s*:\s*calc\(100%\s*\+\s*10px\)[\s\S]*?max-width\s*:\s*min\(260px,\s*calc\(100vw\s*-\s*104px\)\)/,
    "375 档必须收敛气泡宽度并保持头像右缘锚点"
  );
});

test("375 档纯文本气泡垂直居中（不为空媒体区预留高度，尾巴仍指头像）", () => {
  assert.match(
    css,
    /\.seat-bubble:not\(\.rival-bubble\)\.show:has\(\.seat-bubble-media:empty\)\s*\{[^}]*top\s*:\s*50%[^}]*translateY\(-50%\)/,
    "纯文本气泡应以头像中轴对齐，避免窄屏向上溢出顶栏"
  );
});
