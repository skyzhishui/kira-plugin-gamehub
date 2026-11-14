/* chat.js — 对局页「聊天」共享层：单管理员的插件身份 + 座位气泡纯逻辑。
 *
 * KiraAI 宿主差异（相对 nori 原版）：
 * - 本插件没有宿主 webchat 面板：牌桌旁白走插件自己的 notice 通道
 *   （host/notices.py → ctx.publish_notice），因此这里没有 SSE 事件流与
 *   发送入口；对局页只渲染 match 视图 / WS 帧里的气泡。宿主只读历史
 *   （fetchHistory → /api/messages）与 /ui-config 的聊天目标字段保留为
 *   纯解析接口，供将来接入只读面板或单测使用。
 * - 身份是插件自己的 identity.json，只有一个显示名：
 *   GET /identity → { actor, name, effective_name, page_session }
 *   POST /identity { name } → 同上（无 user_id、无会话切换）。
 * - 其余纯逻辑（气泡水位键、match.chat 增量挑选、代次门）原样保留并单测。
 */

import { api, ApiError, localToken } from "./api.js";

/* ---------------- 身份（插件 REST） ---------------- */

export function fetchIdentity() {
  return api("/identity");
}

/** 只提交显示名（服务端契约仅接受 name）。 */
export function saveIdentity({ name } = {}) {
  return api("/identity", { method: "POST", body: { name: String(name == null ? "" : name).trim() } });
}

/* ---------------- 只读/目标诊断（/ui-config 字段，纯解析） ---------------- */

/** 固定中文提示（单源，测试断言用） */
export const CHAT_READ_ONLY_HINT = "陪玩发言发往其他渠道，此处仅可查看";

/** /ui-config → 游戏聊天是否只读。只认 chat_read_only === true。 */
export function isChatReadOnly(cfg) {
  return !!(cfg && typeof cfg === "object" && cfg.chat_read_only === true);
}

/** notice 目标诊断字段（notice_target_session_id / notice_target_platform /
 * notice_target_status: default|override|invalid|unknown / notice_target_error）。
 * 前端仅消费 error 做提示，其余为诊断值不进 UI。 */
export function noticeTargetFromConfig(cfg) {
  const empty = { session: "", effectiveSession: "", platform: "", status: "", error: "" };
  if (!cfg || typeof cfg !== "object") return empty;
  const t = (k) => String(cfg[k] ?? "").trim();
  return {
    session: t("notice_target_session_id"),
    effectiveSession: t("effective_notice_target_session_id"),
    platform: t("notice_target_platform"),
    status: t("notice_target_status"),
    error: t("notice_target_error"),
  };
}

/** 实际聊天目标：chat_session 优先（trim），空/纯空白回落 page_session；
 * 两者皆缺 → ""（调用方据此禁用聊天块）。KiraAI 的 ui-config 仍下发这两个
 * 字段（诊断/后续接入宿主聊天用），本页只渲染只读气泡，不发起聊天。 */
export function chatSessionFromConfig(cfg) {
  if (!cfg || typeof cfg !== "object") return "";
  const chatSession = String(cfg.chat_session ?? "").trim();
  if (chatSession) return chatSession;
  return String(cfg.page_session ?? "").trim();
}

/** 发送 user：只解析 chat_user（trim）；空串表示缺席。 */
export function chatUserFromConfig(cfg) {
  if (!cfg || typeof cfg !== "object") return "";
  return String(cfg.chat_user ?? "").trim();
}

/** 发送 user 优先级链（chat_user → 宿主默认 → 身份 → "player"）。 */
export function resolveChatUser({ chatUser = "", webchatDefault = "", identityUserId = "" } = {}) {
  const cu = String(chatUser || "").trim();
  if (cu) return cu;
  const wd = String(webchatDefault || "").trim();
  if (wd) return wd;
  const id = String(identityUserId || "").trim();
  if (id) return id;
  return "player";
}

/** 页面/对局 UI 配置。对局页传当前 match_id（后端按局解析 notice 目标与气泡上限）。 */
export function fetchUiConfig(matchId) {
  const id = matchId == null ? "" : String(matchId).trim();
  return api(id ? `/ui-config?match_id=${encodeURIComponent(id)}` : "/ui-config");
}

/** 会话代次门（防旧响应覆盖新视图）：切局/重绑时 next() 推进，
 * 在途响应凭捕获代次 isCurrent 判过期。纯逻辑，Node 可测。 */
export function createGenerationGate() {
  let gen = 0;
  return {
    next: () => {
      gen += 1;
      return gen;
    },
    current: () => gen,
    isCurrent: (g) => g === gen,
  };
}

/** 宿主会话历史（只读）：GET /api/messages?session_id=&limit= →
 * { items: [{id, role, is_self, content, chain, timestamp}], cursor }。
 * 宿主按时间倒序返回，这里反转成正序后交给调用方。
 * signal：外部中断（切会话重绑时掐断旧拉取，避免旧响应竞态覆盖）。
 * KiraAI 对局页不渲染宿主聊天，但解析函数保留给将来的只读历史面板/测试。 */
export async function fetchHistory(sessionId, { limit = 50, signal } = {}) {
  const sid = String(sessionId == null ? "" : sessionId).trim();
  if (!sid) return [];
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 12000);
  const onExternalAbort = () => ctrl.abort();
  if (signal) {
    if (signal.aborted) ctrl.abort();
    else signal.addEventListener("abort", onExternalAbort, { once: true });
  }
  try {
    const headers = {};
    const t = localToken();
    if (t) headers.Authorization = `Bearer ${t}`;
    const resp = await fetch(`/api/messages?session_id=${encodeURIComponent(sid)}&limit=${encodeURIComponent(limit)}`, {
      headers,
      signal: ctrl.signal,
    });
    let payload = null;
    try {
      payload = await resp.json();
    } catch {
      /* 非 JSON 按失败处理 */
    }
    if (!resp.ok) throw new ApiError(`请求失败（HTTP ${resp.status}）`, resp.status);
    const items = Array.isArray(payload && payload.items) ? payload.items : [];
    return items.slice().reverse();
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (err && err.name === "AbortError") throw new ApiError("请求超时，请稍后重试", 0);
    throw new ApiError("网络错误，请检查连接", 0);
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener("abort", onExternalAbort);
  }
}

/* ---------------- 气泡水位键 ---------------- */

/** 座位气泡去重键。以 DB id 优先：媒体注解落库后 content 可能变化，
 * 但 id 不变 —— 不会把同一条历史消息当新消息重播。
 * 无 DB id（乐观本地消息）才回落 content 文本。 */
export function noriBubbleKey(msg) {
  if (!msg || typeof msg !== "object") return "";
  if (msg.id != null && String(msg.id) !== "") return `id:${msg.id}`;
  const content = String(msg.content == null ? "" : msg.content);
  return content ? `text:${content}` : "";
}

/** match.chat 单条 say 的签名（后端 schema {seat,text,t,at}，无 id）。
 * 同帧历史整体下发时用签名集合做增量水位。 */
export function matchSayKey(entry) {
  if (!entry || typeof entry !== "object") return "";
  return `${entry.seat}|${entry.t == null ? "" : entry.t}|${entry.at == null ? "" : entry.at}|${String(
    entry.text == null ? "" : entry.text
  )}`;
}

/** 对手座位新发言（match.chat 增量）：首帧由调用方只播种 seen，绝不调用本函数。
 * 默认仅陪练局、仅非人类座位（对手 agent）进入结果；人类手打 say 永不弹气泡。
 * allowSeat 可选：提供时拥有**完整裁决权**（不再叠加内置的人类座位/本人过滤），
 * 并以第三参收整条 say —— 代下宿主发言按人类座位落库（by="host"），需要回调
 * 按 by 标记放行这类条目；未提供时保持旧默认语义。返回 {nextSeen, entries}（正序）。 */
export function pickNewMatchSays(chatArr, seen, { practice = false, seats = [], me = -1, allowSeat = null } = {}) {
  const nextSeen = new Set(seen || []);
  const entries = [];
  const custom = typeof allowSeat === "function" ? allowSeat : null;
  for (const e of Array.isArray(chatArr) ? chatArr : []) {
    const key = matchSayKey(e);
    if (!key) continue;
    if (nextSeen.has(key)) continue;
    nextSeen.add(key);
    const seat = Array.isArray(seats) ? seats[e.seat] : null;
    const allowed = custom
      ? !!custom(e.seat, seat, e)
      : !!seat && seat.kind !== "human"
        && !(typeof me === "number" && me >= 0 && e.seat === me)
        && practice;
    if (!allowed) continue;
    entries.push(e);
  }
  return { nextSeen, entries };
}

/** 兼容导出：ApiError 供调用方做 401/403 判定（与 shared/api.js 同一类型）。 */
export { ApiError };
