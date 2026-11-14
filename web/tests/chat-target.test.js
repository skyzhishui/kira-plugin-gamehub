/* chat-target.test.js — 聊天目标解析轻量测试（node:test，零依赖）。
 * 契约（KiraAI /ui-config 与 nori 一致）：chat_session 优先，缺省回落
 * page_session；chat_user 优先且不被 /identity 覆盖；chat_read_only 仅对
 * 有效非 web 目标为 true；notice_target_* 为诊断字段。
 * fetchHistory 换成 KiraAI /api/messages（只读历史解析，供将来面板复用）。
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  chatSessionFromConfig,
  chatUserFromConfig,
  resolveChatUser,
  noticeTargetFromConfig,
  isChatReadOnly,
  createGenerationGate,
  fetchHistory,
  CHAT_READ_ONLY_HINT,
} from "../shared/chat.js";

test("chat_session 优先；缺省/非 web 只读场景回落 page_session", () => {
  assert.equal(chatSessionFromConfig({ chat_session: "s2", page_session: "s1" }), "s2");
  assert.equal(chatSessionFromConfig({ page_session: "s1" }), "s1", "未选目标回落固定会话");
  assert.equal(chatSessionFromConfig({ chat_session: "  s2 ", page_session: "s1" }), "s2", "trim");
  assert.equal(chatSessionFromConfig({ chat_session: "   ", page_session: "s1" }), "s1", "纯空白视同缺席");
  assert.equal(chatSessionFromConfig({}), "", "两者皆缺 → 空串（调用方禁用聊天块）");
  assert.equal(chatSessionFromConfig(null), "", "cfg 缺失不崩");
});

test("chat_user 优先解析；缺席返回空串交给调用方回退链（webchat 默认 → identity）", () => {
  assert.equal(chatUserFromConfig({ chat_user: "playerB" }), "playerB");
  assert.equal(chatUserFromConfig({ chat_user: " u2 " }), "u2");
  assert.equal(chatUserFromConfig({ chat_user: "  " }), "");
  assert.equal(chatUserFromConfig({}), "");
  assert.equal(chatUserFromConfig(null), "");
});

test("契约样例：有效 web 目标 → 三件套一致跟随，可写", () => {
  const cfg = { page_session: "room-fixed", chat_session: "web-target", chat_user: "playerB", chat_read_only: false };
  assert.equal(chatSessionFromConfig(cfg), "web-target");
  assert.equal(chatUserFromConfig(cfg), "playerB");
  assert.equal(isChatReadOnly(cfg), false);
  // page_session 不被用作聊天目标（身份/房间字段）
  assert.notEqual(chatSessionFromConfig(cfg), "room-fixed");
});

test("契约样例：非 web 只读 → 读固定 web 历史（回落 page_session）但只读", () => {
  const cfg = { page_session: "room-fixed", chat_read_only: true, notice_target_session_id: "live2d-ext" };
  assert.equal(chatSessionFromConfig(cfg), "room-fixed", "chat_session 缺席回落固定 web 历史");
  assert.equal(isChatReadOnly(cfg), true);
  // notice_target_* 为诊断字段，不影响会话与判定
  const withDiag = { ...cfg, notice_target_error: "boom", notice_target_status: "invalid" };
  assert.equal(chatSessionFromConfig(withDiag), "room-fixed");
  assert.equal(isChatReadOnly(withDiag), true);
});

test("默认（无目标）可写：无 chat_session、read_only 缺省", () => {
  const cfg = { page_session: "room-fixed" };
  assert.equal(chatSessionFromConfig(cfg), "room-fixed");
  assert.equal(chatUserFromConfig(cfg), "");
  assert.equal(isChatReadOnly(cfg), false);
  assert.equal(typeof CHAT_READ_ONLY_HINT, "string");
});

test("notice_target_* 诊断字段：全字段解析、trim、缺省空串、cfg 缺失不崩", () => {
  const cfg = {
    notice_target_session_id: "raw-id",
    effective_notice_target_session_id: "eff-id",
    notice_target_platform: "live2d",
    notice_target_status: "override",
    notice_target_error: "",
  };
  const d = noticeTargetFromConfig(cfg);
  assert.deepEqual(d, { session: "raw-id", effectiveSession: "eff-id", platform: "live2d", status: "override", error: "" });
  assert.deepEqual(noticeTargetFromConfig({ notice_target_status: "  invalid  " }).status, "invalid");
  assert.deepEqual(noticeTargetFromConfig({}), { session: "", effectiveSession: "", platform: "", status: "", error: "" });
  assert.deepEqual(noticeTargetFromConfig(null).status, "");
});

test("notice_target_error 透出：回落 web 可写时应提示（error 非空）", () => {
  const fallback = noticeTargetFromConfig({
    notice_target_status: "invalid",
    notice_target_error: "陪玩目标会话无效，已回落本页会话",
    chat_read_only: false,
  });
  assert.ok(fallback.error, "error 应非空供 UI 提示");
  assert.equal(isChatReadOnly(fallbackCfg(fallback)), false, "回落场景可写");
  // 有效非 web 只读场景：error 为空（提示条让位给只读条）
  const ro = noticeTargetFromConfig({ notice_target_status: "override", notice_target_error: "" });
  assert.equal(ro.error, "");
});

// noticeTargetFromConfig 只回诊断字段，这里补回 cfg 形态供判定断言
function fallbackCfg(diag) {
  return { notice_target_status: diag.status, notice_target_error: diag.error, chat_read_only: false };
}

test("resolveChatUser：chat_user 优先且不被 identity 覆盖；缺席走回退链", () => {
  // 关键契约：chat_user 在场时 identity / webchat 默认一律不生效
  assert.equal(
    resolveChatUser({ chatUser: "cu", webchatDefault: "wd", identityUserId: "id" }),
    "cu",
    "chat_user 优先，不被 identity 覆盖"
  );
  assert.equal(resolveChatUser({ chatUser: "cu", identityUserId: "id" }), "cu");
  // chat_user 缺席回退链：webchat 默认 → identity → player
  assert.equal(resolveChatUser({ webchatDefault: "wd", identityUserId: "id" }), "wd");
  assert.equal(resolveChatUser({ identityUserId: "id" }), "id");
  assert.equal(resolveChatUser({}), "player");
  assert.equal(resolveChatUser(), "player");
  assert.equal(resolveChatUser({ chatUser: "  ", webchatDefault: " wd " }), "wd", "trim 后回退");
});

test("会话代次门：推进后旧代次响应一律过期（防旧 reload 覆盖新会话）", () => {
  const gate = createGenerationGate();
  const g0 = gate.current();
  gate.next(); // 切会话/重绑
  assert.equal(gate.current(), g0 + 1);
  assert.equal(gate.isCurrent(g0), false, "旧代次在途响应作废：不渲染/不推水位/不播旧 TTS");
  assert.equal(gate.isCurrent(gate.current()), true, "新代次响应正常生效");
  gate.next();
  assert.equal(gate.isCurrent(g0 + 1), false, "再次切换后上一代次同样过期");
  assert.equal(gate.isCurrent(gate.current()), true);
});

test("fetchHistory signal：外部 abort 即刻中断在途拉取（重绑掐断旧请求）", async () => {
  const prev = globalThis.fetch;
  let sawSignalAbort = false;
  globalThis.fetch = (url, opts) =>
    new Promise((resolve, reject) => {
      opts.signal.addEventListener("abort", () => {
        sawSignalAbort = true;
        const e = new Error("The operation was aborted.");
        e.name = "AbortError";
        reject(e);
      });
    });
  try {
    const ctrl = new AbortController();
    const pending = fetchHistory("sess-old", { signal: ctrl.signal });
    ctrl.abort();
    await assert.rejects(pending);
    assert.ok(sawSignalAbort, "外部 abort 联动到 fetch 的内部 signal");
  } finally {
    globalThis.fetch = prev;
  }
});

test("fetchHistory 常规流（带 signal）不受影响：KiraAI /api/messages 解析 + 倒序反转", async () => {
  const prev = globalThis.fetch;
  let seenUrl = "";
  globalThis.fetch = async (url) => {
    seenUrl = String(url);
    return {
      ok: true,
      json: async () => ({ items: [{ id: 2, content: "b" }, { id: 1, content: "a" }] }),
    };
  };
  try {
    const ctrl = new AbortController();
    const items = await fetchHistory("sess", { signal: ctrl.signal });
    assert.match(seenUrl, /^\/api\/messages\?session_id=sess&limit=/);
    assert.deepEqual(items.map((i) => i.id), [1, 2]);
    assert.equal(ctrl.signal.aborted, false, "正常完成不触发 abort");
  } finally {
    globalThis.fetch = prev;
  }
});
