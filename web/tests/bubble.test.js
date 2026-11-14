/* nori-bubble.test.js — 座位发言气泡水位 + 队友区 CSS 锚点回归（node:test，零依赖）。
 *
 * 历史首载/重拉的气泡语义（与 shared/bubble-queue.js 一起锁定）：
 * 1) 初载只播种（seed）水位，绝不弹历史旧表情包；
 * 2) 相同历史重拉不得复播；键以 DB id 优先，媒体注解变化不改 id → 不重放；
 * 3) 同一次重拉里的多条新回复按时间正序全部入队（不取最新、不合并）；
 * 4) 相同内容不同 id 是两条独立消息。
 * 逐条展示的 1s/3s 时序与 SSE 增量结算见 bubble-queue.test.js。
 *
 * 另覆盖 practice 对手 game_agent say（match.chat {seat,text,t}）的水位：
 * 首帧只播种不弹；仅非人类座位的新增量进入；人类手打 say 永不弹。
 *
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { noriBubbleKey, pickNewMatchSays, matchSayKey } from "../shared/chat.js";
import { createBubbleQueue } from "../shared/bubble-queue.js";

/* ---------------- 历史气泡水位（match.js reloadChatHistory 同构） ---------------- */

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

/** 与 match.js 相同的选择逻辑：消费历史里所有未见过的宿主消息（正序）。 */
function takeFresh(items, queue) {
  const fresh = [];
  for (const m of items) {
    if (!m || m.role !== "assistant") continue;
    const key = noriBubbleKey(m);
    if (!key || queue.hasSeen(key)) continue;
    fresh.push(key);
  }
  return fresh;
}

test("历史首载：播种水位且不弹旧表情包；相同历史重拉不重播", () => {
  const history = [
    { id: 1, role: "user", content: "来一局", timestamp: "2026-11-12T10:00:00Z" },
    {
      id: 2,
      role: "assistant",
      content: "[表情包:偷笑]",
      segments: [{ type: "emoji", text: "偷笑", hash: "h2", local_media: true, local_url: "/api/sessions/media/h2" }],
    },
  ];
  const { q, shown } = memoryQueue();
  const keys = history.map(noriBubbleKey).filter(Boolean);
  q.seed(keys); // 初载
  assert.equal(shown.length, 0, "初载绝不自动弹历史气泡");
  assert.equal(q.hasSeen("id:2"), true, "初载必须播种水位");

  takeFresh(history, q).forEach((key) => q.enqueue({ key, text: "x" }));
  assert.equal(shown.length, 0, "未变化的历史重拉不得复播旧表情包");
});

test("媒体注解落库改变 content：id 优先，同一条历史不重放", () => {
  const before = { id: 7, role: "assistant", content: "[表情包]" };
  const after = {
    id: 7,
    role: "assistant",
    content: "[表情包:偷笑]",
    segments: [{ type: "emoji", text: "偷笑", hash: "h7", local_media: true, local_url: "/api/sessions/media/h7" }],
  };
  assert.equal(noriBubbleKey(after), "id:7", "注解变化不改变水位键");
  const { q, shown } = memoryQueue();
  q.seed([noriBubbleKey(before)]);
  takeFresh([after], q).forEach((key) => q.enqueue({ key, text: "x" }));
  assert.equal(shown.length, 0, "旧消息注解补全不得重播");
});

test("同批次多条新回复：按时间正序全部入队，不取最新、不合并", () => {
  const { q, shown } = memoryQueue();
  q.seed(["id:1"]);
  const items = [
    { id: 1, role: "assistant", content: "开场" },
    { id: 2, role: "user", content: "嗯" },
    { id: 3, role: "assistant", content: "这手走这里" },
    {
      id: 4,
      role: "assistant",
      content: "[表情包:得意]",
      segments: [{ type: "emoji", text: "得意", hash: "h4", local_media: true, local_url: "/api/sessions/media/h4" }],
    },
  ];
  takeFresh(items, q).forEach((key) => q.enqueue({ key, text: "x" }));
  // onShow 只触发首条；排队顺序用 pendingKeys 断言
  assert.deepEqual(shown, ["id:3"], "首条立即展示");
  assert.deepEqual(q.pendingKeys(), ["id:4"], "表情包紧随其后排队（不被最新一条吞掉）");
  assert.equal(q.hasSeen("id:1"), true, "已见过的旧消息不再入队");
});

test("相同内容不同 id：两条独立消息，各自入队（不误吞同文本新消息）", () => {
  const { q } = memoryQueue();
  assert.notEqual(noriBubbleKey({ id: 8, content: "好" }), noriBubbleKey({ id: 9, content: "好" }));
  assert.equal(q.enqueue({ key: "id:8", text: "好" }), "queued");
  assert.equal(q.enqueue({ key: "id:9", text: "好" }), "queued", "同文不同 id 是两条");
  // 无 DB id 的乐观消息回落 content 键
  assert.equal(noriBubbleKey({ role: "assistant", content: "你好" }), "text:你好");
  assert.equal(noriBubbleKey(null), "");
  assert.equal(noriBubbleKey({ role: "assistant", id: 0, content: "x" }), "id:0", "id=0 也是有效水位");
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

/* ---------------- match.js 接线：单条临时气泡 + 全量正序入队 + 上下限钳制 ---------------- */

const matchSrc = fs.readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "match.js"),
  "utf8"
);

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

const css = fs.readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "app.css"),
  "utf8"
);

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
