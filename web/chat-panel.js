/* chat-panel.js — 对局页「对局聊天窗口」（独立于座位 say 气泡的第二块聊天面板）。
 *
 * 数据链（后端契约，前端只按此接线）：
 * - GET  /chat?limit=100 → { enabled, messages:[{id,ts,role,sender,text}] }（按 ts 正序）
 * - POST /chat {text}    → { message:{id,ts,role:"player",sender,text} }
 * - WS   {type:"agent_message", sid, text[, id, ts]} → 追加一条 role="agent" 的回复
 *        （带库内 id/ts 时按 id 归并，重连补拉不重复）
 * - GET  /ui-config 的 chat_enabled 与上面 enabled 联合决定输入区是否可用
 *
 * 设计要点：
 * - 本模块独占聊天窗口逻辑；match.js 只做接线（构造 / 初始化 / 喂 ui-config
 *   与 agent_message 帧）。纯逻辑（合并去重 / 字数上限 / 未读 / 自动滚动判定）
 *   全部导出为无 DOM 的纯函数，Node 可直接单测。
 * - 一律 textContent 渲染，不拼 HTML。
 * - 折叠状态存 sessionStorage（gamehub_chat_collapsed）。
 * - 桌面：右栏（.side-col）内、走子记录下方；窄屏（<720px）：动作行下的抽屉
 *   入口，打开后是贴底抽屉，从不全屏。
 */

import { api } from "./shared/api.js";
import { errorText } from "./shared/i18n.js";

/* ---------------- 常量 ---------------- */

export const CHAT_MAX_CHARS = 500;
/** 超过该字数才出现实时计数（>400 起出现，500 封顶）。 */
export const CHAT_COUNTER_FROM = 400;
const CHAT_PAGE_LIMIT = 100;
export const CHAT_COLLAPSED_KEY = "gamehub_chat_collapsed";
/** 相同文本在该毫秒窗口内重复到达视为同一条（WS 帧与 POST 回包可能撞车）。 */
export const CHAT_DEDUPE_MS = 2000;
/** 距底部多少 px 内仍算「贴底」（继续自动滚动）。 */
export const CHAT_STICK_PX = 24;
/** 列表最多保留的条数（DOM 不无限增长）。 */
export const CHAT_KEEP_MAX = 200;

export const CHAT_TEXT = {
  title: "对局聊天",
  empty: "还没有人说话，先打个招呼吧。",
  disabled: "对局聊天未开放",
  placeholder: "说点什么…",
  send: "发送",
  sending: "发送中",
  latest: "有新消息 · 回到最新",
  collapse: "收起聊天",
  expand: "展开聊天",
  me: "我",
  agent: "AI",
  sendFailed: "发送失败，请重试",
  loadFailed: "聊天记录加载失败",
};

/* ---------------- 纯逻辑：消息归一 ---------------- */

/** 秒级时间戳兜底成毫秒。 */
function toMs(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n < 1e12 ? Math.round(n * 1000) : Math.round(n);
}

/** WS 回复去重：指定角色（默认 agent）的相同文本、且在窗口内已出现过 → 重复。 */
export function isDuplicateText(list, text, now = Date.now(), { windowMs = CHAT_DEDUPE_MS, role = "agent" } = {}) {
  const t = String(text == null ? "" : text).trim();
  if (!t) return true;
  for (const m of Array.isArray(list) ? list : []) {
    if (!m || m.text !== t) continue;
    if (role && m.role !== role) continue;
    if (Math.abs(now - toMs(m.ts)) <= windowMs) return true;
  }
  return false;
}

/** 归一一条消息：role 只认 player/agent，ts 转毫秒，缺 id 合成稳定键。 */
export function normalizeMessage(raw) {
  if (!raw || typeof raw !== "object") return null;
  const text = String(raw.text == null ? "" : raw.text).trim();
  if (!text) return null;
  const role = raw.role === "agent" ? "agent" : raw.role === "spectator" ? "spectator" : "player";
  const ts = toMs(raw.ts);
  const id = raw.id != null && String(raw.id) !== "" ? String(raw.id) : `x:${role}|${ts}|${text}`;
  return {
    id,
    ts,
    role,
    ...(raw.uid ? { uid: String(raw.uid) } : {}),
    sender: String(raw.sender == null ? "" : raw.sender).trim(),
    text,
  };
}

/**
 * 合并进现有列表：按 id 去重（重复 id 用新内容覆盖）、按 ts 升序、裁剪到
 * CHAT_KEEP_MAX。返回 { list, added }，added 只含真正新增的条目。
 * agent 行额外做「同文本 2s 内」去重：WS 帧用的合成 id 与库里 id 不同，
 * 回填时不能把同一条回复显示两次。
 */
export function mergeMessages(list, incoming) {
  const out = Array.isArray(list) ? list.slice() : [];
  const index = new Map();
  out.forEach((m, i) => index.set(m.id, i));
  const added = [];
  for (const raw of Array.isArray(incoming) ? incoming : [incoming]) {
    const msg = normalizeMessage(raw);
    if (!msg) continue;
    const at = index.get(msg.id);
    if (at != null) {
      out[at] = { ...out[at], ...msg };
      continue;
    }
    if (msg.role === "agent" && isDuplicateText(out, msg.text, msg.ts)) continue;
    index.set(msg.id, out.length);
    out.push(msg);
    added.push(msg);
  }
  out.sort((a, b) => a.ts - b.ts);
  return { list: out.slice(-CHAT_KEEP_MAX), added };
}

/* ---------------- 纯逻辑：字数上限 / 发送闸门 ---------------- */

/** 硬上限：按码点截断（emoji 不会被劈成半个）。 */
export function clampChatText(value, max = CHAT_MAX_CHARS) {
  const chars = Array.from(String(value == null ? "" : value));
  return chars.length > max ? chars.slice(0, max).join("") : chars.join("");
}

export function chatCharCount(value) {
  return Array.from(String(value == null ? "" : value)).length;
}

/** 实时计数只在 400 字之后出现。 */
export function showCharCounter(value, from = CHAT_COUNTER_FROM) {
  return chatCharCount(value) > from;
}

/** 计数文案：`420/500`；未到阈值返回空串（不显示）。 */
export function chatCounterText(value) {
  return showCharCounter(value) ? `${chatCharCount(value)}/${CHAT_MAX_CHARS}` : "";
}

/** 发送闸门：未开放 / 在途 / 空白 / 超上限都不发。 */
export function canSend({ enabled = false, text = "", sending = false } = {}) {
  if (!enabled || sending) return false;
  const t = String(text == null ? "" : text).trim();
  return t.length > 0 && chatCharCount(t) <= CHAT_MAX_CHARS;
}

/* ---------------- 纯逻辑：启用状态 / 未读 / 滚动 ---------------- */

/** ui-config.chat_enabled 与 GET /chat 的 enabled 联合判定：任一侧明确 false 即禁用。
 * 缺省（undefined/null）= 未知，不误伤（服务端 POST 仍是最终裁决）。 */
export function resolveEnabled(cfgEnabled, apiEnabled) {
  return cfgEnabled !== false && apiEnabled !== false;
}

/**
 * 未读计数：折叠时到达的新消息才累计；展开且贴底时清零。
 * 返回新的未读数（不修改入参）。
 */
export function nextUnread({ unread = 0, collapsed = false, addedCount = 0, nearBottom = true } = {}) {
  if (!collapsed && nearBottom) return 0;
  if (!(addedCount > 0)) return unread;
  return unread + addedCount;
}

/** 是否贴底（距离 <= stick）：决定新消息到达时要不要自动滚。 */
export function shouldAutoScroll({ scrollTop = 0, scrollHeight = 0, clientHeight = 0, stick = CHAT_STICK_PX } = {}) {
  const distance = Number(scrollHeight) - Number(clientHeight) - Number(scrollTop);
  if (!Number.isFinite(distance)) return true;
  return distance <= stick;
}

/* ---------------- 纯逻辑：折叠状态持久化 ---------------- */

export function readCollapsed(storage, key = CHAT_COLLAPSED_KEY) {
  try {
    return !!storage && storage.getItem(key) === "1";
  } catch {
    return false;
  }
}

export function writeCollapsed(storage, collapsed, key = CHAT_COLLAPSED_KEY) {
  try {
    if (storage) storage.setItem(key, collapsed ? "1" : "0");
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

/* ---------------- 纯逻辑：时间 ---------------- */

/** HH:MM；跨天补 MM-DD；无有效时间返回空串。 */
export function chatClock(ts) {
  const ms = toMs(ts);
  if (!ms) return "";
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return hm;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

/* ---------------- DOM ---------------- */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text);
  return n;
}

function defaultStorage() {
  try {
    return typeof sessionStorage !== "undefined" ? sessionStorage : null;
  } catch {
    return null;
  }
}

/** 窄屏（<720px）用抽屉：初始化时抽屉默认关闭，不吃掉桌面端的展开记忆。 */
function narrowViewport() {
  try {
    return typeof globalThis.matchMedia === "function" && !!globalThis.matchMedia("(max-width: 719px)").matches;
  } catch {
    return false;
  }
}

/**
 * 聊天窗口控制器。opts：
 * - panelHost  面板挂载点（桌面右栏）
 * - chipHost   窄屏入口挂载点（动作行下方），可为空
 * - getAgentName  当前宿主主 agent 名（agent 行的 sender）
 * - onError    (err, text) 可选：失败时的上层提示（toast）
 * - storage / now  可注入（测试用）
 */
export function createChatPanel({
  panelHost = null,
  chipHost = null,
  getAgentName = () => CHAT_TEXT.agent,
  onError = null,
  storage = defaultStorage(),
  now = () => Date.now(),
  matchId = "",
} = {}) {
  /* ---- 内部状态 ---- */
  let list = [];
  let cfgEnabled; // undefined = 未知
  let apiEnabled; // undefined = 未知
  let sending = false;
  let unread = 0;
  let collapsed = readCollapsed(storage);
  let nearBottom = true;
  let hintError = "";
  let loaded = false;

  const isEnabled = () => resolveEnabled(cfgEnabled, apiEnabled);

  /* ---- 结构 ---- */
  const root = el("section", "chat-win");
  root.setAttribute("aria-label", CHAT_TEXT.title);

  const head = el("div", "chat-win-head");
  const dot = el("i", "chat-win-dot", "");
  dot.setAttribute("aria-hidden", "true");
  head.appendChild(dot);
  head.appendChild(el("span", "chat-win-title", CHAT_TEXT.title));
  const badge = el("span", "chat-win-unread hidden");
  badge.setAttribute("aria-live", "polite");
  head.appendChild(badge);
  const foldBtn = el("button", "chat-win-fold");
  foldBtn.type = "button";
  foldBtn.appendChild(el("span", "chat-win-fold-ic", "▾"));
  head.appendChild(foldBtn);
  root.appendChild(head);

  const body = el("div", "chat-win-body");
  const listWrap = el("div", "chat-win-list-wrap");
  const scroller = el("div", "chat-win-list");
  scroller.setAttribute("role", "log");
  scroller.setAttribute("aria-live", "polite");
  listWrap.appendChild(scroller);

  const jump = el("button", "chat-win-jump hidden");
  jump.type = "button";
  jump.appendChild(el("span", "chat-win-jump-ic", "↓"));
  jump.appendChild(el("span", "chat-win-jump-text", CHAT_TEXT.latest));
  listWrap.appendChild(jump);
  body.appendChild(listWrap);

  const form = el("form", "chat-win-input");
  const field = el("div", "chat-win-field");
  const input = el("input", "chat-win-text");
  input.type = "text";
  input.autocomplete = "off";
  input.maxLength = CHAT_MAX_CHARS;
  input.placeholder = CHAT_TEXT.placeholder;
  const counter = el("span", "chat-win-count hidden");
  field.appendChild(input);
  field.appendChild(counter);
  const sendBtn = el("button", "btn primary chat-win-send", CHAT_TEXT.send);
  sendBtn.type = "submit";
  form.appendChild(field);
  form.appendChild(sendBtn);
  body.appendChild(form);

  const hint = el("div", "chat-win-hint hidden");
  hint.setAttribute("role", "status");
  body.appendChild(hint);

  root.appendChild(body);

  /* 窄屏入口（抽屉开关 + 未读数） */
  let chip = null;
  let chipBadge = null;
  if (chipHost) {
    chip = el("button", "btn sm chat-win-chip");
    chip.type = "button";
    const chipDot = el("i", "chat-win-dot", "");
    chipDot.setAttribute("aria-hidden", "true");
    chip.appendChild(chipDot);
    chip.appendChild(el("span", null, CHAT_TEXT.title));
    chipBadge = el("span", "chat-win-unread hidden");
    chip.appendChild(chipBadge);
    chipHost.appendChild(chip);
  }

  /* ---- 渲染 ---- */

  function renderBadge() {
    const text = unread > 99 ? "99+" : String(unread);
    const show = collapsed && unread > 0;
    [badge, chipBadge].forEach((b) => {
      if (!b) return;
      b.textContent = text;
      b.classList.toggle("hidden", !show);
    });
  }

  function renderHead() {
    const label = collapsed ? CHAT_TEXT.expand : CHAT_TEXT.collapse;
    foldBtn.setAttribute("aria-expanded", String(!collapsed));
    foldBtn.setAttribute("aria-label", label);
    foldBtn.title = label;
    root.classList.toggle("folded", collapsed);
    if (panelHost) panelHost.classList.toggle("folded", collapsed);
    if (chip) {
      chip.setAttribute("aria-expanded", String(!collapsed));
      chip.title = label;
    }
    renderBadge();
  }

  function renderEmpty() {
    scroller.textContent = "";
    scroller.appendChild(el("p", "chat-win-empty", CHAT_TEXT.empty));
  }

  function messageRow(m, { fresh = false } = {}) {
    const mine = m.role === "player";
    const row = el("div", `chat-win-msg ${mine ? "mine" : "theirs"}${fresh ? " is-new" : ""}`);
    const main = el("div", "chat-win-msg-main");
    const meta = el("div", "chat-win-meta");
    const who = m.sender || (mine ? CHAT_TEXT.me : String(getAgentName() || CHAT_TEXT.agent));
    meta.appendChild(el("span", "chat-win-who", who));
    const t = chatClock(m.ts);
    if (t) meta.appendChild(el("span", "chat-win-time", t));
    main.appendChild(meta);
    main.appendChild(el("div", "chat-win-bubble", m.text));
    row.appendChild(main);
    return row;
  }

  function renderList() {
    if (!list.length) {
      renderEmpty();
      return;
    }
    // Suspend announcements during a full-table rebuild (backfill / first fill /
    // trim / out-of-order): aria-live would read every new node, turning a history
    // backfill into one long repeated monologue. polite is restored after the
    // rebuild, so items appended incrementally are still announced one by one.
    scroller.setAttribute("aria-live", "off");
    scroller.textContent = "";
    list.forEach((m) => scroller.appendChild(messageRow(m)));
    scroller.setAttribute("aria-live", "polite");
  }

  function renderJump() {
    jump.classList.toggle("hidden", nearBottom || collapsed || !list.length);
  }

  function renderInput() {
    const on = isEnabled();
    input.disabled = !on;
    input.placeholder = on ? CHAT_TEXT.placeholder : CHAT_TEXT.disabled;
    sendBtn.disabled = !on || sending;
    sendBtn.textContent = sending ? CHAT_TEXT.sending : CHAT_TEXT.send;
    root.classList.toggle("is-off", !on);

    const count = chatCharCount(input.value);
    const show = showCharCounter(input.value);
    counter.textContent = chatCounterText(input.value);
    counter.classList.toggle("hidden", !show);
    counter.classList.toggle("is-full", count >= CHAT_MAX_CHARS);

    const text = hintError || (!on ? CHAT_TEXT.disabled : "");
    hint.textContent = text;
    hint.classList.toggle("hidden", !text);
    hint.classList.toggle("is-error", !!hintError);
  }

  function scrollToLatest() {
    nearBottom = true;
    renderJump();
    scroller.scrollTop = scroller.scrollHeight;
    // 刚展开 / 刚插入节点时高度还在算：下一帧再贴一次底（用户已滚走就不动）
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => {
        if (nearBottom && !collapsed) scroller.scrollTop = scroller.scrollHeight;
      });
    }
  }

  /* ---- 状态变更 ---- */

  function clearUnread() {
    unread = 0;
    renderBadge();
  }

  /** 追加一批消息（GET 回填 / POST 回包 / WS 帧共用）。backfill 不累计未读。 */
  function ingest(incoming, { backfill = false } = {}) {
    const merged = mergeMessages(list, incoming);
    const added = merged.added.length;
    list = merged.list;
    // 回填按「列表顺序」而不是服务端到达顺序渲染（契约是 ts 正序，但这里不信任）
    if (backfill) {
      renderList();
      renderJump();
      if (!collapsed) scrollToLatest();
      return added;
    }
    if (added) {
      // 整表重绘：空状态文案、首次填充、超出保留上限，或新消息不在末尾时
      const lastAddedIndex = Math.max(...merged.added.map((m) => list.indexOf(m)));
      if (
        scroller.children.length === 0 ||
        list.length - added === 0 ||
        list.length >= CHAT_KEEP_MAX ||
        lastAddedIndex !== list.length - 1
      ) {
        renderList();
      } else {
        merged.added.forEach((m) => scroller.appendChild(messageRow(m, { fresh: true })));
      }
      unread = nextUnread({ unread, collapsed, addedCount: added, nearBottom });
      renderBadge();
      renderJump();
      if (nearBottom && !collapsed) scrollToLatest();
    }
    return added;
  }

  /* ---- 网络 ---- */

  async function load() {
    try {
      const data = await api(`/chat?limit=${CHAT_PAGE_LIMIT}${matchId ? `&match_id=${encodeURIComponent(matchId)}` : ""}`);
      if (data && typeof data === "object") {
        apiEnabled = data.enabled === false ? false : data.enabled === true ? true : apiEnabled;
        renderInput();
        ingest(Array.isArray(data.messages) ? data.messages : [], { backfill: true });
      }
      hintError = "";
    } catch (err) {
      if (err && err.code === "chat_disabled") {
        apiEnabled = false;
      } else {
        hintError = errorText(err, CHAT_TEXT.loadFailed);
      }
    } finally {
      loaded = true;
      renderInput();
    }
  }

  async function submit() {
    const text = clampChatText(input.value).trim();
    if (!canSend({ enabled: isEnabled(), text, sending })) {
      if (!text && isEnabled() && !sending) {
        try {
          input.focus({ preventScroll: true });
        } catch {
          /* ignore */
        }
      }
      return;
    }
    sending = true;
    hintError = "";
    renderInput();
    try {
      const data = await api(`/chat${matchId ? `?match_id=${encodeURIComponent(matchId)}` : ""}`, { method: "POST", body: { text, ...(matchId ? { match_id: matchId } : {}) } });
      const msg = data && data.message ? data.message : null;
      // 只清掉已提交的文本（提交期间用户可能又打了字）
      if (clampChatText(input.value).trim() === text) input.value = "";
      if (msg) ingest([msg]); // 服务端回包即为唯一真相，按 id 去重后追加
      scrollToLatest();
    } catch (err) {
      const textOut = errorText(err, CHAT_TEXT.sendFailed);
      if (err && err.code === "chat_disabled") apiEnabled = false;
      hintError = textOut;
      // 抽屉/面板收着时看不到内联提示，交给页面 toast
      if (typeof onError === "function" && collapsed) onError(err, textOut);
    } finally {
      sending = false;
      renderInput();
    }
  }

  /* ---- 交互 ---- */

  function setCollapsed(next) {
    collapsed = !!next;
    writeCollapsed(storage, collapsed);
    if (!collapsed) {
      clearUnread();
      scrollToLatest();
      try {
        input.focus({ preventScroll: true });
      } catch {
        /* ignore */
      }
    }
    renderHead();
    renderJump();
  }

  foldBtn.addEventListener("click", () => setCollapsed(!collapsed));
  if (chip) chip.addEventListener("click", () => setCollapsed(!collapsed));

  jump.addEventListener("click", () => {
    clearUnread();
    scrollToLatest();
  });

  scroller.addEventListener("scroll", () => {
    const stick = shouldAutoScroll({
      scrollTop: scroller.scrollTop,
      scrollHeight: scroller.scrollHeight,
      clientHeight: scroller.clientHeight,
    });
    if (stick === nearBottom) return;
    nearBottom = stick;
    if (stick) clearUnread();
    renderJump();
  });

  input.addEventListener("input", () => {
    // 硬上限：粘贴超长文本就地截断（maxlength 对粘贴在部分内核不生效）
    const clamped = clampChatText(input.value);
    if (clamped !== input.value) input.value = clamped;
    renderInput();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });

  /* ---- 对外 API ---- */

  function mount() {
    // 窄屏：抽屉默认收起（保留桌面端记忆，不改写 sessionStorage）
    if (!collapsed && narrowViewport()) collapsed = true;
    if (panelHost) {
      panelHost.appendChild(root);
      panelHost.classList.toggle("folded", collapsed); // 右栏让位给走子记录
    }
    renderHead();
    renderInput();
    renderList();
    renderJump();
    return root;
  }

  /** ui-config 结果（host_agent_name 已由 match.js 写进模式层，这里只取 chat_enabled）。 */
  function applyUiConfig(cfg) {
    if (!cfg || typeof cfg !== "object") return;
    if (typeof cfg.chat_enabled === "boolean") {
      cfgEnabled = cfg.chat_enabled;
      renderInput();
    }
  }

  /** WS agent_message 帧 → agent 行。帧带库内 id/ts 时（权威身份）直接按 id
   * 归并——重连补拉同一条回复不会重复；无 id 的旧帧保持合成键 + 同文本 2s 去重。 */
  function onAgentMessage(frame) {
    if (!frame || typeof frame !== "object") return false;
    if (frame.type === "chat_message") return ingest([frame]) > 0;
    const text = String(frame.text == null ? "" : frame.text).trim();
    if (!text) return false;
    const sender = String(getAgentName() || CHAT_TEXT.agent);
    const frameId = frame.id == null ? "" : String(frame.id);
    if (frameId) {
      return ingest([{
        id: frameId,
        ts: toMs(frame.ts) || now(),
        role: "agent",
        sender,
        text,
      }]) > 0;
    }
    const ts = now();
    if (isDuplicateText(list, text, ts)) return false;
    const sid = String(frame.sid == null ? "" : frame.sid);
    return ingest([{ id: sid ? `ws:${sid}:${ts}` : "", ts, role: "agent", sender, text }]) > 0;
  }

  /** WS 重连后补拉（onLive 钩子）：只在新行真正到达时重绘，不打断滚动/未读。
   * 与 load() 的区别：失败静默（下一次建连再试），不渲染加载失败提示。
   * Refill goes through ingest(backfill) rather than bare renderList: a reconnect can
   * deliver several rows at once, and repainting the table directly would skip the
   * unread counter and the "jump to latest" button updates. */
  async function refresh() {
    try {
      const data = await api(`/chat?limit=${CHAT_PAGE_LIMIT}${matchId ? `&match_id=${encodeURIComponent(matchId)}` : ""}`);
      if (!data || typeof data !== "object") return;
      apiEnabled = data.enabled === false ? false : data.enabled === true ? true : apiEnabled;
      renderInput();
      ingest(Array.isArray(data.messages) ? data.messages : [], { backfill: true });
    } catch {
      /* 补拉失败不打扰用户：漏掉的帧在下一次建连/刷新时回填 */
    }
  }

  function destroy() {
    if (root.parentElement) root.remove();
    if (chip && chip.parentElement) chip.remove();
    list = [];
  }

  return {
    root,
    mount,
    load,
    refresh,
    applyUiConfig,
    onAgentMessage,
    setCollapsed,
    destroy,
    isEnabled,
    isLoaded: () => loaded,
    // 供测试/诊断读取内部状态
    state: () => ({ list, unread, collapsed, nearBottom, sending, hintError, cfgEnabled, apiEnabled }),
  };
}
