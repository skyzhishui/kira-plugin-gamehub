/* chat.js — match-page "chat" shared layer: plugin identity + bubble pure logic.
 *
 * KiraAI host differences (relative to the nori original):
 * - This plugin has no host webchat panel: table narration goes through the plugin's
 *   own notice channel (host/notices.py -> ctx.publish_notice), so there is no SSE
 *   event stream, no send entry point, and no host chat history here. The match page
 *   only renders bubble entries from the match view / WS frames. The host-history and
 *   target-resolution API had no callers and was removed.
 * - Identity is the plugin's own identity.json with a single display name:
 *   GET /identity -> { actor, name, effective_name, page_session }
 *   POST /identity { name } -> same shape (no user_id, no session switching).
 * - Remaining pure logic (bubble watermark keys, match.chat delta picking) is kept
 *   and unit-tested.
 */

import { api } from "./api.js";

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

/** Fixed hint: this match's proactive AI lines are not mirrored here, while the
 * page conversation keeps working (non-blocking). */
export const CHAT_EXTERNAL_NOTICE_HINT =
  "AI 的桌边发言会发到另一个会话，不在这里显示。你仍可在本页与 AI 聊天。";

/** /ui-config → 游戏聊天是否只读。只认 chat_read_only === true。 */
export function isChatReadOnly(cfg) {
  return !!(cfg && typeof cfg === "object" && cfg.chat_read_only === true);
}

/** /ui-config → whether this match's proactive AI lines go to another session
 * (chat-origin match or an overridden notice target). Hint-only: it never
 * feeds the writable decision, because player lines always publish into the
 * page session. */
export function isExternalNotMirrored(cfg) {
  return !!(cfg && typeof cfg === "object" && cfg.external_not_mirrored === true);
}

/**
 * Text for #chat-status: the target diagnostic error and the not-mirrored hint
 * share one status line. Read-only yields an empty string so the dedicated
 * #chat-readonly bar keeps that message; a lone error is shown verbatim. The
 * "you can still talk here" wording is dropped when the page chat is disabled.
 */
export function chatStatusText({ readOnly = false, error = "", externalNotMirrored = false, chatEnabled = true } = {}) {
  if (readOnly) return "";
  const showExternal = externalNotMirrored === true && chatEnabled !== false;
  const err = String(error == null ? "" : error).trim();
  if (!err) return showExternal ? CHAT_EXTERNAL_NOTICE_HINT : "";
  // Only normalise the separator when both sentences share the line.
  return showExternal ? `${err.replace(/[。.！!?？；;]+$/u, "")}。${CHAT_EXTERNAL_NOTICE_HINT}` : err;
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
    error: cfg.effective_notice_target_error == null
      ? t("notice_target_error")
      : t("effective_notice_target_error"),
  };
}

/** 页面/对局 UI 配置。对局页传当前 match_id（后端按局解析 notice 目标与气泡上限）。 */
export function fetchUiConfig(matchId) {
  const id = matchId == null ? "" : String(matchId).trim();
  return api(id ? `/ui-config?match_id=${encodeURIComponent(id)}` : "/ui-config");
}

/* ---------------- 气泡水位键 ---------------- */

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
