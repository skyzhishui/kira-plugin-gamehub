/* chat-target.test.js — chat-target diagnostic parsing (node:test, zero deps).
 * Contract (KiraAI /ui-config): the chat_read_only decision and the notice_target_*
 * diagnostic fields. Only interfaces with production callers are kept (match.js
 * consumes noticeTargetFromConfig / isChatReadOnly); host history fetching, session/
 * user resolution and the generation gate had no callers and were deleted together
 * with their implementation.
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";

import { noticeTargetFromConfig, isChatReadOnly } from "../shared/chat.js";

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
