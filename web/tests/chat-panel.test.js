/* chat-panel.test.js — 对局聊天窗口（web/chat-panel.js）纯逻辑 + 接线测试。
 *
 * 覆盖：
 * 1) 消息归一/合并：按 id 去重、ts 正序、重复 id 覆盖不重复追加；
 * 2) WS agent_message 去重（相同文本 2s 窗口内只收一次）；
 * 3) 字数硬上限 500（按码点截断）、计数 >400 才出现、发送闸门；
 * 4) enabled 联合判定（ui-config.chat_enabled + GET /chat.enabled）；
 * 5) 未读计数（折叠才累计，展开贴底清零）；
 * 6) 自动滚动暂停/恢复判定（贴底阈值）；
 * 7) 折叠状态 sessionStorage 读写（"1"/"0"，异常回退）；
 * 8) 面板接线：假 fetch + 假 DOM 驱动 createChatPanel（GET 回填、POST 乐观
 *    追加不重复、agent 帧、禁用态、抽屉 chip、折叠持久化）。
 *
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  CHAT_MAX_CHARS,
  CHAT_COUNTER_FROM,
  CHAT_DEDUPE_MS,
  CHAT_STICK_PX,
  CHAT_COLLAPSED_KEY,
  CHAT_KEEP_MAX,
  CHAT_TEXT,
  normalizeMessage,
  mergeMessages,
  isDuplicateText,
  clampChatText,
  chatCharCount,
  showCharCounter,
  chatCounterText,
  canSend,
  resolveEnabled,
  nextUnread,
  shouldAutoScroll,
  readCollapsed,
  writeCollapsed,
  chatClock,
  createChatPanel,
} from "../chat-panel.js";
import { ERROR_TEXT } from "../shared/i18n.js";

/* ---------------- 最小 DOM 桩（同 doudizhu-browser.test.js 风格） ---------------- */

class FakeClassList {
  constructor() {
    this.set = new Set();
  }
  set value(v) {
    this.set = new Set(String(v == null ? "" : v).split(/\s+/).filter(Boolean));
  }
  get value() {
    return [...this.set].join(" ");
  }
  add(...names) {
    names.forEach((n) => n && this.set.add(String(n)));
  }
  remove(...names) {
    names.forEach((n) => this.set.delete(String(n)));
  }
  toggle(name, force) {
    const has = this.set.has(name);
    const on = force === undefined ? !has : !!force;
    if (on) this.set.add(name);
    else this.set.delete(name);
    return on;
  }
  contains(name) {
    return this.set.has(name);
  }
}

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = new Map();
    this.classList = new FakeClassList();
    this.textContentRaw = "";
    this.disabled = false;
    this.value = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.focused = 0;
  }
  set className(v) {
    this.classList.value = v;
  }
  get className() {
    return this.classList.value;
  }
  set textContent(v) {
    this.textContentRaw = String(v == null ? "" : v);
    this.children = [];
  }
  get textContent() {
    return this.textContentRaw + this.children.map((c) => c.textContent).join("");
  }
  appendChild(node) {
    if (!node) return node;
    node.parentElement = this;
    this.children.push(node);
    return node;
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  dispatch(type, event = {}) {
    for (const fn of this.listeners.get(type) || []) fn({ target: this, preventDefault() {}, ...event });
  }
  click() {
    if (this.disabled) return;
    this.dispatch("click");
  }
  focus() {
    this.focused += 1;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return this.attributes[name] != null ? this.attributes[name] : null;
  }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((c) => c !== this);
    }
    this.parentElement = null;
  }
}

function walk(node, out = []) {
  out.push(node);
  node.children.forEach((c) => walk(c, out));
  return out;
}

function byClass(root, cls) {
  return walk(root).filter((n) => n.classList.contains(cls));
}

function installDom() {
  const prev = { document: globalThis.document };
  globalThis.document = {
    createElement: (tag) => new FakeEl(tag),
    createTextNode: (text) => {
      const n = new FakeEl("#text");
      n.textContent = String(text);
      return n;
    },
  };
  return {
    restore() {
      globalThis.document = prev.document;
    },
  };
}

function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    snapshot: () => Object.fromEntries(map),
  };
}

/* ---------------- 假 fetch：记录请求，按路由回包 ---------------- */

function installFetch(handler) {
  const prev = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    const call = { url: String(url), method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null };
    calls.push(call);
    const out = handler(call);
    const status = out.status || 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => out.body,
    };
  };
  return {
    calls,
    restore() {
      globalThis.fetch = prev;
    },
  };
}

const ok = (data) => ({ body: { ok: true, data } });
const fail = (code, error, status = 400) => ({ status, body: { ok: false, data: null, error, code } });

const msg = (id, ts, role, sender, text) => ({ id, ts, role, sender, text });

/* ---------------- 消息归一 / 合并 ---------------- */

test("normalizeMessage：role 只认 player/agent，ts 秒级补成毫秒，缺 id 合成稳定键，空文本丢弃", () => {
  const a = normalizeMessage(msg("m1", 1700000000000, "agent", "汐祈", "  好呀  "));
  assert.deepEqual(a, { id: "m1", ts: 1700000000000, role: "agent", sender: "汐祈", text: "好呀" });
  const sec = normalizeMessage({ ts: 1700000000, role: "player", text: "hi" });
  assert.equal(sec.ts, 1700000000000, "秒级时间戳转毫秒");
  assert.equal(sec.role, "player");
  assert.match(sec.id, /^x:player\|/, "缺 id 时合成稳定键");
  assert.equal(normalizeMessage({ text: "   " }), null, "空白文本丢弃");
  assert.equal(normalizeMessage(null), null);
  const weird = normalizeMessage({ text: "x", role: "system" });
  assert.equal(weird.role, "player", "未知 role 归一为 player");
});

test("mergeMessages：按 id 去重、ts 升序、重复 id 覆盖不重复追加", () => {
  const first = mergeMessages([], [msg("b", 2000, "agent", "A", "二"), msg("a", 1000, "player", "我", "一")]);
  assert.deepEqual(first.list.map((m) => m.id), ["a", "b"], "按 ts 升序");
  assert.equal(first.added.length, 2);

  const second = mergeMessages(first.list, [msg("a", 1000, "player", "我", "一（修正）"), msg("c", 3000, "agent", "A", "三")]);
  assert.deepEqual(second.list.map((m) => m.id), ["a", "b", "c"]);
  assert.deepEqual(second.added.map((m) => m.id), ["c"], "同 id 不算新增");
  assert.equal(second.list.find((m) => m.id === "a").text, "一（修正）", "重复 id 用新内容覆盖");
});

test("mergeMessages：POST 回包与 WS 同 id 撞车只保留一条（无重复气泡）", () => {
  const posted = msg("srv-1", 5000, "player", "阿空", "你好");
  const once = mergeMessages([], [posted]);
  const twice = mergeMessages(once.list, [posted]);
  assert.equal(twice.list.length, 1);
  assert.equal(twice.added.length, 0);
});

test("mergeMessages：WS 合成 id 与库回填 id 不同、同文本 2s 内 → 只留一条；超过窗口各留一条", () => {
  const ws = { id: "", ts: 1_700_000_000_000, role: "agent", sender: "汐祈", text: "这手走天元" };
  const withWs = mergeMessages([], [ws]);
  assert.equal(withWs.list.length, 1);
  const backfill = mergeMessages(withWs.list, [
    msg("db-1", 1_700_000_000_500, "agent", "汐祈", "这手走天元"),
  ]);
  assert.equal(backfill.list.length, 1, "同一条回复不因 id 不同重复显示");
  assert.equal(backfill.added.length, 0);
  const later = mergeMessages(backfill.list, [
    msg("db-2", 1_700_000_010_000, "agent", "汐祈", "这手走天元"),
  ]);
  assert.equal(later.list.length, 2, "窗口外的同文是两条独立回复");
});

test("mergeMessages：保留上限裁剪最旧消息（DOM 不无限增长）", () => {
  const many = Array.from({ length: CHAT_KEEP_MAX + 25 }, (_, i) => msg(`m${i}`, 1000 + i, "agent", "A", `第 ${i} 条`));
  const { list } = mergeMessages([], many);
  assert.equal(list.length, CHAT_KEEP_MAX);
  assert.equal(list[0].id, "m25", "最旧的被裁掉");
});

/* ---------------- WS 去重 ---------------- */

test("isDuplicateText：相同文本 2s 内重复 → 重复；窗口外/不同文本/不同角色不算", () => {
  const now = 1_700_000_000_000;
  const list = [msg("w1", now, "agent", "A", "我下这里")];
  assert.equal(isDuplicateText(list, "我下这里", now), true, "2s 内同文去重");
  assert.equal(isDuplicateText(list, "我下这里", now + CHAT_DEDUPE_MS), true, "恰在窗口边界仍去重");
  assert.equal(isDuplicateText(list, "我下这里", now + CHAT_DEDUPE_MS + 1), false, "窗口外可再次出现");
  assert.equal(isDuplicateText(list, "另一句", now), false, "不同文本不拦");
  assert.equal(isDuplicateText(list, "", now), true, "空文本视为重复（不追加）");
  const player = [msg("p1", now, "player", "我", "好")];
  assert.equal(isDuplicateText(player, "好", now), false, "玩家自己发的同文不被 agent 去重误伤");
});

/* ---------------- 字数上限 ---------------- */

test("clampChatText：硬上限 500，按码点截断（emoji 不被劈半）", () => {
  assert.equal(CHAT_MAX_CHARS, 500);
  assert.equal(clampChatText("短"), "短");
  const long = "字".repeat(600);
  const cut = clampChatText(long);
  assert.equal(chatCharCount(cut), 500);
  assert.equal(cut, "字".repeat(500));
  const emoji = "🙂".repeat(600);
  assert.equal(chatCharCount(clampChatText(emoji)), 500, "按码点而不是 UTF-16 长度");
  assert.equal(clampChatText(null), "");
});

test("showCharCounter / chatCounterText：>400 才出现，500 时计数满", () => {
  assert.equal(CHAT_COUNTER_FROM, 400);
  assert.equal(showCharCounter("字".repeat(400)), false, "正好 400 不显示");
  assert.equal(showCharCounter("字".repeat(401)), true, "401 起显示");
  assert.equal(chatCounterText("字".repeat(400)), "");
  assert.equal(chatCounterText("字".repeat(420)), "420/500");
  assert.equal(chatCounterText("字".repeat(500)), "500/500");
});

test("canSend：未开放 / 在途 / 空白 / 超上限都不发；正常文本可发", () => {
  assert.equal(canSend({ enabled: false, text: "你好" }), false, "未开放");
  assert.equal(canSend({ enabled: true, text: "你好", sending: true }), false, "在途");
  assert.equal(canSend({ enabled: true, text: "   " }), false, "空白");
  assert.equal(canSend({ enabled: true, text: "字".repeat(501) }), false, "超上限");
  assert.equal(canSend({ enabled: true, text: "字".repeat(500) }), true, "恰 500 可发");
  assert.equal(canSend({ enabled: true, text: "  你好  " }), true);
  assert.equal(canSend(), false, "缺省不发");
});

/* ---------------- 启用判定 / 未读 / 滚动 ---------------- */

test("resolveEnabled：任一侧明确 false 即禁用；未知（undefined）不误伤", () => {
  assert.equal(resolveEnabled(true, true), true);
  assert.equal(resolveEnabled(false, true), false, "ui-config 关");
  assert.equal(resolveEnabled(true, false), false, "GET /chat 关");
  assert.equal(resolveEnabled(false, false), false);
  assert.equal(resolveEnabled(undefined, true), true, "ui-config 未知 → 由 /chat 决定");
  assert.equal(resolveEnabled(undefined, undefined), true, "都未知不误伤（服务端仍是最终裁决）");
});

test("nextUnread：折叠时累计；展开且贴底清零；展开但滚上去仍累计", () => {
  assert.equal(nextUnread({ unread: 0, collapsed: true, addedCount: 2 }), 2);
  assert.equal(nextUnread({ unread: 2, collapsed: true, addedCount: 1 }), 3);
  assert.equal(nextUnread({ unread: 5, collapsed: false, addedCount: 3, nearBottom: true }), 0, "展开贴底清零");
  assert.equal(nextUnread({ unread: 5, collapsed: false, addedCount: 3, nearBottom: false }), 8, "滚上去仍累计");
  assert.equal(nextUnread({ unread: 4, collapsed: true, addedCount: 0 }), 4, "无新增不变");
  assert.equal(nextUnread({ unread: 4, collapsed: false, addedCount: 0, nearBottom: true }), 0, "展开贴底即清零");
});

test("shouldAutoScroll：距底 <= 24px 视为贴底，滚上去则暂停自动滚动", () => {
  assert.equal(CHAT_STICK_PX, 24);
  assert.equal(shouldAutoScroll({ scrollTop: 900, scrollHeight: 1000, clientHeight: 100 }), true, "正好贴底");
  assert.equal(shouldAutoScroll({ scrollTop: 876, scrollHeight: 1000, clientHeight: 100 }), true, "距底 24px 内");
  assert.equal(shouldAutoScroll({ scrollTop: 800, scrollHeight: 1000, clientHeight: 100 }), false, "滚上去");
  assert.equal(shouldAutoScroll({ scrollTop: 0, scrollHeight: 0, clientHeight: 0 }), true, "无内容按贴底");
  assert.equal(shouldAutoScroll({}), true, "缺省不拦滚动");
});

/* ---------------- 折叠持久化 / 时间 ---------------- */

test("readCollapsed/writeCollapsed：sessionStorage \"1\"/\"0\"，异常回退展开", () => {
  const store = memoryStorage();
  assert.equal(readCollapsed(store), false, "缺省展开");
  writeCollapsed(store, true);
  assert.equal(store.snapshot()[CHAT_COLLAPSED_KEY], "1");
  assert.equal(readCollapsed(store), true);
  writeCollapsed(store, false);
  assert.equal(store.snapshot()[CHAT_COLLAPSED_KEY], "0");
  assert.equal(readCollapsed(store), false);
  const broken = {
    getItem() {
      throw new Error("denied");
    },
    setItem() {
      throw new Error("denied");
    },
  };
  assert.equal(readCollapsed(broken), false, "读失败回退展开");
  writeCollapsed(broken, true); // 不抛
  assert.equal(readCollapsed(null), false);
  assert.equal(CHAT_COLLAPSED_KEY, "gamehub_chat_collapsed");
});

test("chatClock：当天 HH:MM，跨天 MM-DD HH:MM，无效时间空串", () => {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 9, 5).getTime();
  assert.equal(chatClock(today), "09:05");
  const old = new Date(2020, 0, 2, 23, 7).getTime();
  assert.equal(chatClock(old), "01-02 23:07");
  assert.equal(chatClock(null), "");
  assert.equal(chatClock(0), "");
});

/* ---------------- 错误码双语（i18n.js） ---------------- */

test("i18n：对局聊天新增错误码都有 zh/en 且双语不同", () => {
  for (const code of ["chat_disabled", "not_ready", "text_empty", "text_too_long", "rate_limited"]) {
    assert.ok(ERROR_TEXT[code], `缺少错误码 ${code}`);
    assert.ok(ERROR_TEXT[code].zh.length > 0, `${code}.zh 非空`);
    assert.ok(ERROR_TEXT[code].en.length > 0, `${code}.en 非空`);
    assert.notEqual(ERROR_TEXT[code].zh, ERROR_TEXT[code].en, `${code} 双语不同`);
  }
  assert.equal(ERROR_TEXT.chat_disabled.zh, "对局聊天未开放");
  assert.equal(ERROR_TEXT.chat_disabled.en, "Match chat is disabled.");
});

/* ---------------- 面板接线（假 DOM + 假 fetch） ---------------- */

function mountPanel({ panelHost, chipHost, storage, handler, getAgentName } = {}) {
  const dom = installDom();
  const net = installFetch(handler);
  const panel = createChatPanel({
    panelHost,
    chipHost,
    getAgentName,
    storage: storage || memoryStorage(),
    now: () => 1_700_000_010_000,
  });
  panel.mount();
  return {
    dom,
    net,
    panel,
    restore() {
      dom.restore();
      net.restore();
    },
  };
}

const flush = () => new Promise((r) => setImmediate(r));

test("面板：GET /chat 回填 → 空状态消失、消息按序渲染（player 右/agent 左）", async () => {
  const host = new FakeEl("div");
  const env = mountPanel({
    panelHost: host,
    handler: () =>
      ok({
        enabled: true,
        messages: [
          msg("m1", 1_700_000_000_000, "agent", "汐祈", "来一局？"),
          msg("m2", 1_700_000_001_000, "player", "阿空", "好呀"),
        ],
      }),
  });
  try {
    await env.panel.load();
    assert.equal(env.net.calls[0].url.endsWith("/chat?limit=100"), true, "回填带 limit=100");
    const rows = byClass(host, "chat-win-msg");
    assert.equal(rows.length, 2);
    assert.ok(rows[0].classList.contains("theirs"), "agent 靠左");
    assert.ok(rows[1].classList.contains("mine"), "player 靠右");
    assert.ok(rows[0].textContent.includes("汐祈"));
    assert.ok(rows[0].textContent.includes("来一局？"), "文本渲染");
    assert.equal(byClass(host, "chat-win-empty").length, 0, "有消息时无空状态");
  } finally {
    env.restore();
  }
});

test("面板：整表重建期间暂停 aria-live，重建后恢复（历史回填不逐条朗读）", async () => {
  const host = new FakeEl("div");
  const env = mountPanel({
    panelHost: host,
    handler: () =>
      ok({
        enabled: true,
        messages: [
          msg("m1", 1_700_000_000_000, "agent", "汐祈", "来一局？"),
          msg("m2", 1_700_000_001_000, "player", "阿空", "好呀"),
        ],
      }),
  });
  try {
    const scroller = byClass(host, "chat-win-list")[0];
    assert.ok(scroller, "聊天滚动区存在");
    let live = scroller.getAttribute("aria-live");
    // Observe the announcement state at the exact moment each row is appended.
    const appendedUnder = [];
    const setAttr = scroller.setAttribute.bind(scroller);
    scroller.setAttribute = (name, value) => {
      if (name === "aria-live") live = String(value);
      return setAttr(name, value);
    };
    const append = scroller.appendChild.bind(scroller);
    scroller.appendChild = (node) => {
      appendedUnder.push(live);
      return append(node);
    };

    await env.panel.load();

    assert.ok(appendedUnder.length >= 2, "回填渲染了消息行");
    assert.deepEqual([...new Set(appendedUnder)], ["off"], "每一行都在暂停播报时追加");
    assert.equal(live, "polite", "重建结束后恢复礼貌播报，后续增量仍会朗读");
  } finally {
    env.restore();
  }
});

test("面板：空列表显示中文空状态文案", async () => {
  const host = new FakeEl("div");
  const env = mountPanel({ panelHost: host, handler: () => ok({ enabled: true, messages: [] }) });
  try {
    await env.panel.load();
    const empty = byClass(host, "chat-win-empty")[0];
    assert.ok(empty, "空状态存在");
    assert.equal(empty.textContent, CHAT_TEXT.empty);
  } finally {
    env.restore();
  }
});

test("面板：POST 成功 → 乐观追加服务端回包（含发送者/时间），输入框清空，不重复", async () => {
  const host = new FakeEl("div");
  const sent = msg("srv-9", 1_700_000_009_000, "player", "阿空", "我先占中腹");
  const env = mountPanel({
    panelHost: host,
    handler: (call) => {
      if (call.method === "POST") return ok({ message: sent });
      return ok({ enabled: true, messages: [] });
    },
  });
  try {
    await env.panel.load();
    const input = byClass(host, "chat-win-text")[0];
    input.value = "我先占中腹";
    input.dispatch("input");
    const form = byClass(host, "chat-win-input")[0];
    form.dispatch("submit");
    await flush();
    assert.equal(env.net.calls.at(-1).method, "POST");
    assert.deepEqual(env.net.calls.at(-1).body, { text: "我先占中腹" });
    assert.equal(byClass(host, "chat-win-msg").length, 1, "追加一条");
    assert.ok(byClass(host, "chat-win-msg")[0].textContent.includes("我先占中腹"));
    assert.equal(input.value, "", "发送成功后清空输入");
    // 同一 id 的回包重放（重试/服务端重放）→ 按 id 去重，不重复追加
    input.value = "我先占中腹";
    input.dispatch("input");
    form.dispatch("submit");
    await flush();
    assert.equal(byClass(host, "chat-win-msg").length, 1, "同 id 回包不重复追加");
    assert.equal(env.panel.state().list.length, 1);
  } finally {
    env.restore();
  }
});

test("面板：WS agent_message → agent 行，sender 用宿主主 agent 名，同文 2s 内去重", async () => {
  const host = new FakeEl("div");
  const env = mountPanel({
    panelHost: host,
    getAgentName: () => "汐祈",
    handler: () => ok({ enabled: true, messages: [] }),
  });
  try {
    await env.panel.load();
    assert.equal(env.panel.onAgentMessage({ type: "agent_message", sid: "s1", text: "这手走天元" }), true);
    assert.equal(env.panel.onAgentMessage({ type: "agent_message", sid: "s2", text: "这手走天元" }), false, "2s 内同文去重");
    assert.equal(env.panel.onAgentMessage({ type: "agent_message", sid: "s3", text: "另一句" }), true);
    const rows = byClass(host, "chat-win-msg");
    assert.equal(rows.length, 2);
    assert.ok(rows[0].classList.contains("theirs"));
    assert.ok(rows[0].textContent.includes("汐祈"), "sender 用宿主主 agent 名");
    assert.equal(env.panel.onAgentMessage({ type: "agent_message", text: "  " }), false, "空文本不追加");
  } finally {
    env.restore();
  }
});

test("面板：WS 帧带库内 id/ts（权威身份）→ 按 id 归并；refresh 补拉同 id 不重复", async () => {
  const host = new FakeEl("div");
  let messages = [msg("42", 1_700_000_008_000, "agent", "汐祈", "E1。")];
  const env = mountPanel({
    panelHost: host,
    getAgentName: () => "汐祈",
    handler: () => ok({ enabled: true, messages }),
  });
  try {
    await env.panel.load();
    assert.equal(byClass(host, "chat-win-msg").length, 1);
    // 同一条回复的实时 WS 帧：带库内 id/ts（秒级）→ 与回填行同 id，不重复追加
    assert.equal(
      env.panel.onAgentMessage({ type: "agent_message", sid: "gamehub:dm:admin", id: "42", ts: 1_700_000_008, text: "E1。" }),
      false,
      "同 id 帧不重复追加（重连补拉不会出现两条）"
    );
    assert.equal(byClass(host, "chat-win-msg").length, 1);
    // 新回复：新 id 照常追加
    assert.equal(
      env.panel.onAgentMessage({ type: "agent_message", sid: "gamehub:dm:admin", id: "43", ts: 1_700_000_009, text: "A2，锁胜。" }),
      true
    );
    assert.equal(byClass(host, "chat-win-msg").length, 2);
    // 模拟 WS 重连后的 refresh 补拉：列表里已有的 id 不产生重复
    messages = [
      msg("42", 1_700_000_008_000, "agent", "汐祈", "E1。"),
      msg("43", 1_700_000_009_000, "agent", "汐祈", "A2，锁胜。"),
      msg("44", 1_700_000_010_000, "agent", "汐祈", "收尾了，H2。"),
    ];
    await env.panel.refresh();
    assert.equal(byClass(host, "chat-win-msg").length, 3, "refresh 只补入漏掉的新行");
    assert.equal(env.panel.state().list.length, 3);
  } finally {
    env.restore();
  }
});

test("面板：refresh 无新行不重绘；请求失败静默（不打扰用户，hintError 不变）", async () => {
  const host = new FakeEl("div");
  let messages = [msg("7", 1_700_000_001_000, "agent", "汐祈", "先看你想怎么输")];
  let shouldFail = false;
  const env = mountPanel({
    panelHost: host,
    handler: () => {
      if (shouldFail) return fail("not_ready", "对局服务不可用", 503);
      return ok({ enabled: true, messages });
    },
  });
  try {
    await env.panel.load();
    const rowsBefore = byClass(host, "chat-win-msg").length;
    await env.panel.refresh();
    assert.equal(byClass(host, "chat-win-msg").length, rowsBefore, "无新行不重绘");
    messages = [...messages, msg("8", 1_700_000_002_000, "agent", "汐祈", "下一手 B6")];
    await env.panel.refresh();
    assert.equal(byClass(host, "chat-win-msg").length, rowsBefore + 1, "新行照常追加");
    shouldFail = true;
    await env.panel.refresh();
    assert.equal(byClass(host, "chat-win-msg").length, rowsBefore + 1, "失败保持原状");
    assert.equal(env.panel.state().hintError, "", "补拉失败静默，不渲染失败提示");
  } finally {
    env.restore();
  }
});

test("面板：enabled=false（GET /chat）→ 输入/发送禁用并显示中文提示；ui-config 关同样禁用", async () => {
  const host = new FakeEl("div");
  const env = mountPanel({ panelHost: host, handler: () => ok({ enabled: false, messages: [] }) });
  try {
    await env.panel.load();
    const input = byClass(host, "chat-win-text")[0];
    const send = byClass(host, "chat-win-send")[0];
    assert.equal(input.disabled, true);
    assert.equal(send.disabled, true);
    assert.equal(input.placeholder, CHAT_TEXT.disabled);
    assert.equal(byClass(host, "chat-win-hint")[0].textContent, CHAT_TEXT.disabled);
    assert.equal(env.panel.isEnabled(), false);
    assert.equal(canSend({ enabled: env.panel.isEnabled(), text: "你好" }), false, "禁用态不发");
  } finally {
    env.restore();
  }
  const host2 = new FakeEl("div");
  const env2 = mountPanel({ panelHost: host2, handler: () => ok({ enabled: true, messages: [] }) });
  try {
    await env2.panel.load();
    assert.equal(env2.panel.isEnabled(), true);
    env2.panel.applyUiConfig({ chat_enabled: false, host_agent_name: "汐祈" });
    assert.equal(env2.panel.isEnabled(), false, "ui-config.chat_enabled=false 联合禁用");
    assert.equal(byClass(host2, "chat-win-text")[0].disabled, true);
  } finally {
    env2.restore();
  }
});

test("面板：发送失败 → 错误码本地化为中文提示（chat_disabled 同时禁用输入）", async () => {
  const host = new FakeEl("div");
  const env = mountPanel({
    panelHost: host,
    handler: (call) => (call.method === "POST" ? fail("rate_limited", "发送太频繁") : ok({ enabled: true, messages: [] })),
  });
  try {
    await env.panel.load();
    const input = byClass(host, "chat-win-text")[0];
    input.value = "再发一句";
    input.dispatch("input");
    byClass(host, "chat-win-input")[0].dispatch("submit");
    await flush();
    const hint = byClass(host, "chat-win-hint")[0];
    assert.ok(hint.classList.contains("is-error"));
    assert.equal(hint.textContent, ERROR_TEXT.rate_limited.zh);
    assert.equal(byClass(host, "chat-win-msg").length, 0, "失败不乐观追加");
  } finally {
    env.restore();
  }
});

test("面板：输入超 500 字就地截断；401 字出现计数，400 字不出现", () => {
  const host = new FakeEl("div");
  const env = mountPanel({ panelHost: host, handler: () => ok({ enabled: true, messages: [] }) });
  try {
    const input = byClass(host, "chat-win-text")[0];
    const counter = byClass(host, "chat-win-count")[0];
    input.value = "字".repeat(400);
    input.dispatch("input");
    assert.ok(counter.classList.contains("hidden"), "400 字不显示计数");
    input.value = "字".repeat(401);
    input.dispatch("input");
    assert.ok(!counter.classList.contains("hidden"), "401 字显示计数");
    assert.equal(counter.textContent, "401/500");
    input.value = "字".repeat(650);
    input.dispatch("input");
    assert.equal(chatCharCount(input.value), 500, "硬上限截断到 500");
    assert.equal(counter.textContent, "500/500");
    assert.ok(counter.classList.contains("is-full"));
  } finally {
    env.restore();
  }
});

test("面板：折叠 → body 隐藏、未读徽标累计；展开清零并持久化到 sessionStorage", async () => {
  const host = new FakeEl("div");
  const store = memoryStorage();
  const env = mountPanel({
    panelHost: host,
    storage: store,
    handler: () => ok({ enabled: true, messages: [] }),
  });
  try {
    await env.panel.load();
    const fold = byClass(host, "chat-win-fold")[0];
    fold.click();
    assert.equal(env.panel.state().collapsed, true);
    assert.ok(byClass(host, "chat-win")[0].classList.contains("folded"));
    assert.equal(store.snapshot()[CHAT_COLLAPSED_KEY], "1", "折叠状态持久化");
    assert.equal(fold.getAttribute("aria-expanded"), "false");
    env.panel.onAgentMessage({ type: "agent_message", sid: "s1", text: "在吗" });
    env.panel.onAgentMessage({ type: "agent_message", sid: "s2", text: "走一步？" });
    const badge = byClass(host, "chat-win-unread")[0];
    assert.equal(badge.textContent, "2", "折叠时新消息累计未读");
    assert.ok(!badge.classList.contains("hidden"));
    fold.click();
    assert.equal(env.panel.state().unread, 0, "展开清零");
    assert.ok(badge.classList.contains("hidden"));
    assert.equal(store.snapshot()[CHAT_COLLAPSED_KEY], "0");
  } finally {
    env.restore();
  }
});

test("面板：sessionStorage 记忆折叠 → 构造即折叠；窄屏 chip 同步开合并显示未读", async () => {
  const host = new FakeEl("div");
  const dock = new FakeEl("div");
  const store = memoryStorage({ [CHAT_COLLAPSED_KEY]: "1" });
  const env = mountPanel({
    panelHost: host,
    chipHost: dock,
    storage: store,
    handler: () => ok({ enabled: true, messages: [] }),
  });
  try {
    const root = byClass(host, "chat-win")[0];
    assert.ok(root.classList.contains("folded"), "刷新后保持折叠");
    const chip = byClass(dock, "chat-win-chip")[0];
    assert.ok(chip, "窄屏入口 chip 存在");
    env.panel.onAgentMessage({ type: "agent_message", sid: "s1", text: "在吗" });
    const chipBadge = byClass(chip, "chat-win-unread")[0];
    assert.equal(chipBadge.textContent, "1", "chip 上显示未读");
    chip.click();
    assert.equal(env.panel.state().collapsed, false, "点 chip 展开抽屉");
    assert.equal(store.snapshot()[CHAT_COLLAPSED_KEY], "0");
    assert.ok(chipBadge.classList.contains("hidden"));
  } finally {
    env.restore();
  }
});

test("面板：窄屏（matchMedia 命中）默认收成 chip，不改写已存的桌面偏好", () => {
  const prevMM = globalThis.matchMedia;
  globalThis.matchMedia = (q) => ({ matches: String(q).includes("max-width: 719px"), media: q });
  try {
    const host = new FakeEl("div");
    const dock = new FakeEl("div");
    const store = memoryStorage();
    const env = mountPanel({ panelHost: host, chipHost: dock, storage: store, handler: () => ok({ enabled: true, messages: [] }) });
    try {
      assert.ok(byClass(host, "chat-win")[0].classList.contains("folded"), "抽屉默认关闭，不遮棋盘");
      assert.ok(host.classList.contains("folded"));
      assert.equal(store.snapshot()[CHAT_COLLAPSED_KEY], undefined, "不改写桌面端记忆");
      byClass(dock, "chat-win-chip")[0].click();
      assert.equal(env.panel.state().collapsed, false);
      assert.equal(store.snapshot()[CHAT_COLLAPSED_KEY], "0", "用户主动打开才落盘");
    } finally {
      env.restore();
    }
  } finally {
    globalThis.matchMedia = prevMM;
  }
});

test("面板：向上滚动 → 暂停自动滚动并显示「回到最新」；点它贴底清零", async () => {
  const host = new FakeEl("div");
  const env = mountPanel({
    panelHost: host,
    handler: () =>
      ok({
        enabled: true,
        messages: Array.from({ length: 5 }, (_, i) => msg(`m${i}`, 1_700_000_000_000 + i * 1000, "agent", "A", `第 ${i} 条`)),
      }),
  });
  try {
    await env.panel.load();
    const scroller = byClass(host, "chat-win-list")[0];
    // 用户滚上去（离底很远）
    scroller.scrollTop = 0;
    scroller.scrollHeight = 600;
    scroller.clientHeight = 200;
    scroller.dispatch("scroll");
    assert.equal(env.panel.state().nearBottom, false);
    const jump = byClass(host, "chat-win-jump")[0];
    assert.ok(!jump.classList.contains("hidden"), "出现「回到最新」");
    // 滚上去时新消息不自动贴底（scrollTop 不被重置）
    env.panel.onAgentMessage({ type: "agent_message", sid: "s1", text: "新的一条" });
    assert.equal(scroller.scrollTop, 0, "暂停自动滚动");
    jump.click();
    assert.equal(scroller.scrollTop, scroller.scrollHeight, "点「回到最新」贴底");
    assert.equal(env.panel.state().nearBottom, true);
    assert.ok(jump.classList.contains("hidden"), "贴底后浮标收起");
  } finally {
    env.restore();
  }
});

test("面板：贴底时新消息自动滚到最新（scrollTop 跟随 scrollHeight）", async () => {
  const host = new FakeEl("div");
  const env = mountPanel({ panelHost: host, handler: () => ok({ enabled: true, messages: [] }) });
  try {
    await env.panel.load();
    const scroller = byClass(host, "chat-win-list")[0];
    scroller.scrollTop = 600; // 800 - 200：正好贴底
    scroller.scrollHeight = 800;
    scroller.clientHeight = 200;
    scroller.dispatch("scroll");
    assert.equal(env.panel.state().nearBottom, true, "初始贴底");
    env.panel.onAgentMessage({ type: "agent_message", sid: "s1", text: "来一条" });
    assert.equal(scroller.scrollTop, scroller.scrollHeight, "自动滚到最新");
  } finally {
    env.restore();
  }
});

test("面板：destroy 移除面板与 chip，不残留", () => {
  const host = new FakeEl("div");
  const dock = new FakeEl("div");
  const env = mountPanel({ panelHost: host, chipHost: dock, handler: () => ok({ enabled: true, messages: [] }) });
  try {
    assert.equal(host.children.length, 1);
    assert.equal(dock.children.length, 1);
    env.panel.destroy();
    assert.equal(host.children.length, 0);
    assert.equal(dock.children.length, 0);
  } finally {
    env.restore();
  }
});

/* ---------------- 接线：api.js 的 agent_message 分发 ---------------- */

test("MatchConnection：agent_message 帧交给 onAgentMessage（默认 no-op，不吞其它帧）", async () => {
  const { MatchConnection } = await import("../shared/api.js");
  const seen = [];
  const conn = new MatchConnection({
    matchId: "m1",
    onMatch: (m) => seen.push(["match", m.id]),
    onError: (e) => seen.push(["error", e.code]),
    onAgentMessage: (f) => seen.push(["agent", f.text]),
  });
  const frame = (obj) => conn._onFrame({ data: JSON.stringify(obj) });
  frame({ type: "match", match: { id: "m1" } });
  frame({ type: "agent_message", sid: "s1", text: "你好" });
  frame({ type: "pong" });
  frame({ type: "error", message: "x", code: "bad_request" });
  frame({ type: "unknown_frame" });
  frame("不是 JSON");
  assert.deepEqual(seen, [["match", "m1"], ["agent", "你好"], ["error", "bad_request"]]);
  // 默认构造不炸（onAgentMessage 缺省 no-op）
  const plain = new MatchConnection({ matchId: "m2" });
  plain._onFrame({ data: JSON.stringify({ type: "agent_message", text: "x" }) });
});

/* ---------------- 源码接线断言 ---------------- */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
/* Normalise line endings before source-text assertions (multi-line regexes must not
 * depend on LF; a CRLF checkout fails spuriously). */
const readSrc = (name) => fs.readFileSync(path.resolve(here, "..", name), "utf8").replace(/\r\n/g, "\n");

test("match.js：只做接线（构造面板 / 喂 ui-config / 分发 agent_message），不内联聊天逻辑", () => {
  const src = readSrc("match.js");
  assert.match(src, /import \{ createChatPanel \} from "\.\/chat-panel\.js"/, "引入独立模块");
  assert.match(src, /createChatPanel\(\{/, "构造面板");
  assert.match(src, /chatWin\.applyUiConfig\(cfg\)/, "ui-config 喂给面板");
  assert.match(src, /onAgentMessage: \(frame\) =>/, "WS agent_message 分发");
  assert.doesNotMatch(src, /chat-win-bubble|chat-win-jump|chat-win-text/, "聊天窗口 DOM 不在 match.js 内");
});

test("match.html：面板挂载点（右栏）与窄屏入口（动作行下）都在", () => {
  const html = readSrc("match.html");
  assert.match(html, /id="side-chat"/, "右栏面板挂载点");
  assert.match(html, /id="chat-dock"/, "窄屏入口挂载点");
  const actionsAt = html.indexOf('id="actions"');
  const dockAt = html.indexOf('id="chat-dock"');
  assert.ok(dockAt > actionsAt, "入口在动作行之后");
});

test("app.css：对局聊天窗口段只复用既有变量（不引入新色板），窄屏抽屉不全屏", () => {
  const css = readSrc("app.css");
  const section = css.slice(css.indexOf("/* ---------------- 对局聊天窗口"));
  const body = section.slice(0, section.indexOf("/* ---------------- 对局页响应式"));
  assert.doesNotMatch(body, /--[a-z-]+\s*:/, "只复用既有变量，不定义新色板");
  assert.match(body, /@media \(max-width: 719px\)/, "窄屏抽屉断点");
  assert.match(body, /max-height:\s*65vh/, "抽屉不全屏");
});
