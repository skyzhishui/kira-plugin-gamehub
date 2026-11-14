/* chat-readonly.test.js — 只读聊天判定轻量测试（node:test，零依赖）。
 * 契约：只读判定完全以 /ui-config chat_read_only 为准（目标为非 web 适配器
 * 会话时后端置 true）；前端不从 notice_target_session_id 非空自行推断。
 * KiraAI 对局页只渲染只读气泡（旁白走插件 notice 通道），但提示文案与判定
 * 仍按后端契约解析，供将来接入宿主聊天面板时复用。
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import { isChatReadOnly, CHAT_READ_ONLY_HINT, fetchUiConfig } from "../shared/chat.js";

test("缺省与旧后端字段缺席一律可聊天", () => {
  assert.equal(isChatReadOnly(null), false, "cfg 缺失不可崩、按可聊天");
  assert.equal(isChatReadOnly(undefined), false);
  assert.equal(isChatReadOnly({}), false, "旧后端无新字段 → 可聊天");
  assert.equal(isChatReadOnly({ chat_enabled: true, page_session: "s1" }), false);
  assert.equal(isChatReadOnly({ notice_target_session_id: "" }), false);
});

test("chat_read_only=true 只读；false 可聊", () => {
  assert.equal(isChatReadOnly({ chat_read_only: true }), true);
  assert.equal(isChatReadOnly({ chat_read_only: true, notice_target_session_id: "" }), true);
  assert.equal(isChatReadOnly({ chat_read_only: true, notice_target_session_id: "live2d-ext" }), true);
  assert.equal(isChatReadOnly({ chat_read_only: false }), false);
});

test("前端不得从 notice_target_session_id 自行判断：非空目标+标志false 仍可聊天", () => {
  // 目标为 web 会话（可聊）或后端尚未落标志：一律以 chat_read_only 为准
  assert.equal(isChatReadOnly({ notice_target_session_id: "web-target-s2" }), false);
  assert.equal(isChatReadOnly({ chat_read_only: false, notice_target_session_id: "web-target-s2" }), false);
  assert.equal(isChatReadOnly({ chat_read_only: false, notice_target_session_id: "live2d-ext" }), false, "标志为准，不做防御性覆盖");
  assert.equal(isChatReadOnly({ notice_target_session_id: 123 }), false, "非布尔/任意类型不触发");
});

test("提示文案单源：不声称「已投递/已发送」，含发往其他渠道与仅可查看语义", () => {
  assert.equal(typeof CHAT_READ_ONLY_HINT, "string");
  assert.ok(CHAT_READ_ONLY_HINT.includes("发往其他渠道"), "含发往渠道说明");
  assert.ok(CHAT_READ_ONLY_HINT.includes("仅可查看"), "含只读说明");
  assert.ok(!CHAT_READ_ONLY_HINT.includes("已投递"), "非 web 只读不等于实际发送成功");
  assert.ok(!CHAT_READ_ONLY_HINT.includes("已发送"), "不得声称发送成功");
});

test("chat.js 在 Node 可安全导入（无顶层浏览器全局依赖）", () => {
  assert.equal(typeof fetchUiConfig, "function");
});
