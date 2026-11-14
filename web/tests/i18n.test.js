/* i18n.test.js — 错误文案本地化（shared/i18n.js）测试（node:test，零依赖）。
 *
 * 覆盖：宿主语言判定（默认 zh / language=en 为 en）、已知错误码双语映射、
 * 未知码回退 err.message、空错误回退通用文案。
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import { ERROR_TEXT, uiLanguage, errorText } from "../shared/i18n.js";

const withGlobals = (globals, fn) => {
  const saved = {};
  for (const [k, v] of Object.entries(globals)) {
    saved[k] = globalThis[k];
    globalThis[k] = v;
  }
  try {
    return fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) globalThis[k] = v;
  }
};

const langStore = (value) => ({ getItem: (k) => (k === "language" ? value : null) });

test("uiLanguage：缺省/未知值为 zh，language=en 为 en，隐私模式不抛", () => {
  assert.equal(withGlobals({ localStorage: langStore(null) }, () => uiLanguage()), "zh");
  assert.equal(withGlobals({ localStorage: langStore("zh") }, () => uiLanguage()), "zh");
  assert.equal(withGlobals({ localStorage: langStore("en") }, () => uiLanguage()), "en");
  assert.equal(withGlobals({ localStorage: { getItem() { throw new Error("denied"); } } }, () => uiLanguage()), "zh");
});

test("ERROR_TEXT：每个错误码都有 zh/en 短句", () => {
  const codes = [
    "bad_action", "bad_kind", "bad_mode", "bad_name", "bad_opponent", "bad_request",
    "bad_seat", "bad_token", "game_over", "not_found", "not_ready",
    "actor_busy", "match_full", "stale_version", "internal",
  ];
  for (const code of codes) {
    assert.ok(ERROR_TEXT[code], `缺少错误码 ${code}`);
    assert.equal(typeof ERROR_TEXT[code].zh, "string");
    assert.ok(ERROR_TEXT[code].zh.length > 0, `${code}.zh 非空`);
    assert.equal(typeof ERROR_TEXT[code].en, "string");
    assert.ok(ERROR_TEXT[code].en.length > 0, `${code}.en 非空`);
    assert.notEqual(ERROR_TEXT[code].zh, ERROR_TEXT[code].en, `${code} 双语不同`);
  }
});

test("errorText：已知错误码按语言本地化（中文/英文）", () => {
  const err = { code: "bad_token", message: "座位口令无效" };
  assert.equal(
    withGlobals({ localStorage: langStore(null) }, () => errorText(err)),
    ERROR_TEXT.bad_token.zh
  );
  assert.equal(
    withGlobals({ localStorage: langStore("en") }, () => errorText(err)),
    ERROR_TEXT.bad_token.en
  );
  const stale = { code: "stale_version", message: "版本已更新" };
  assert.equal(
    withGlobals({ localStorage: langStore("en") }, () => errorText(stale)),
    ERROR_TEXT.stale_version.en
  );
});

test("errorText：未知码/无码回退 err.message，再回退 fallback 与通用文案", () => {
  const unknown = { code: "no_such_code", message: "服务端中文" };
  assert.equal(
    withGlobals({ localStorage: langStore("en") }, () => errorText(unknown)),
    "服务端中文"
  );
  assert.equal(
    withGlobals({ localStorage: langStore("en") }, () => errorText({ message: "boom" })),
    "boom"
  );
  assert.equal(
    withGlobals({ localStorage: langStore(null) }, () => errorText({ message: "" }, "回退文案")),
    "回退文案"
  );
});

test("errorText：空错误回退通用文案（zh/en）", () => {
  assert.equal(withGlobals({ localStorage: langStore(null) }, () => errorText(null)), "出现了一个小问题，请重试");
  assert.equal(
    withGlobals({ localStorage: langStore("en") }, () => errorText(undefined)),
    "Something went wrong. Please retry."
  );
  assert.equal(withGlobals({ localStorage: langStore(null) }, () => errorText({})), "出现了一个小问题，请重试");
});
