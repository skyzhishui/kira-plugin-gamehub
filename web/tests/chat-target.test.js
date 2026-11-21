/* chat-target.test.js — chat-target diagnostic parsing (node:test, zero deps).
 * Contract (KiraAI /ui-config): the chat_read_only decision, the notice_target_*
 * diagnostic fields and the non-blocking external_not_mirrored hint. Only
 * interfaces with production callers are kept (match.js consumes
 * noticeTargetFromConfig / isChatReadOnly / isExternalNotMirrored /
 * chatStatusText); host history fetching, session/user resolution and the
 * generation gate had no callers and were deleted together with their
 * implementation.
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";

import { noticeTargetFromConfig, isChatReadOnly, isExternalNotMirrored, chatStatusText, CHAT_EXTERNAL_NOTICE_HINT, CHAT_READ_ONLY_HINT } from "../shared/chat.js";

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
  // 有效非 web 只读场景：error 为空（提示条让位给只读条）
  const ro = noticeTargetFromConfig({ notice_target_status: "override", notice_target_error: "" });
  assert.equal(ro.error, "");
  assert.equal(isChatReadOnly({ chat_read_only: false }), false, "诊断字段不影响可写判定");
});

test("notice diagnostics prefer the effective per-match error", () => {
  const base = { notice_target_error: "全局目标不可用" };
  assert.equal(noticeTargetFromConfig(base).error, "全局目标不可用");
  assert.equal(noticeTargetFromConfig({ ...base, effective_notice_target_error: "" }).error, "");
  assert.equal(noticeTargetFromConfig({ ...base, effective_notice_target_error: "本局目标不可用" }).error, "本局目标不可用");
});

test("external_not_mirrored：AI 主动发言去别处时只加提示，不禁用输入", () => {
  assert.equal(isExternalNotMirrored({ external_not_mirrored: true }), true);
  assert.equal(isExternalNotMirrored({ external_not_mirrored: false }), false);
  assert.equal(isExternalNotMirrored({ external_not_mirrored: "true" }), false, "只认布尔 true");
  assert.equal(isExternalNotMirrored(null), false);

  // 纯提示场景：一句说明，强调本页对话照常收发（不是只读）
  assert.equal(chatStatusText({ externalNotMirrored: true }), CHAT_EXTERNAL_NOTICE_HINT);
  assert.match(CHAT_EXTERNAL_NOTICE_HINT, /另一个会话/);
  assert.match(CHAT_EXTERNAL_NOTICE_HINT, /仍可在本页/);
  assert.equal(isChatReadOnly({ external_not_mirrored: true, chat_read_only: false }), false, "非镜像≠只读");

  // 目标诊断（notice_target_error）原样保留
  assert.equal(chatStatusText({ error: "目标会话无效，已回落本页会话" }), "目标会话无效，已回落本页会话");

  // 两者同时：一条状态里先说诊断，再说非镜像提示
  const both = chatStatusText({
    error: "目标会话无效，已回落本页会话",
    externalNotMirrored: true,
  });
  assert.ok(both.startsWith("目标会话无效，已回落本页会话。"), both);
  assert.ok(both.endsWith(CHAT_EXTERNAL_NOTICE_HINT), both);
  assert.equal(both.match(/目标会话无效/g).length, 1, "不重复诊断句");

  // 只读时让位给只读条（本插件不下发只读，这里只锁语义）
  assert.equal(chatStatusText({ readOnly: true, error: "x", externalNotMirrored: true }), "");
  assert.equal(chatStatusText({}), "");
  assert.ok(CHAT_READ_ONLY_HINT.length > 0, "只读提示仍由独立文案承担");

  // 聊天关闭（或观战）时不再承诺「这里照常收发」，诊断句照旧
  assert.equal(chatStatusText({ externalNotMirrored: true, chatEnabled: false }), "");
  assert.equal(chatStatusText({ error: "目标会话无效", externalNotMirrored: true, chatEnabled: false }), "目标会话无效");
});
