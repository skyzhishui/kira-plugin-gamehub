/* api.test.js — 宿主契约适配层（shared/api.js）测试（node:test，零依赖）。
 *
 * 覆盖 KiraAI 与 nori 的差异点（其余 MatchConnection 语义由原样移植保证）：
 * 1) 插件基址 / REST 基址 / 页面基址常量为 KiraAI 的 id；
 * 2) 宿主口令只读 localStorage["jwt_token"]；
 * 3) 对局 WS 地址走 /ws/plugin/<id>/matches/<id>/ws，座位口令 ?t=、
 *    宿主口令 ?token=（不是 API 基址下的 /matches/<id>/ws）。
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import { PLUGIN_ID, API_BASE, PAGE_BASE, WS_BASE, HOST_TOKEN_KEY, localToken, matchWsUrl, api, ApiError } from "../shared/api.js";

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

test("常量：插件 id / REST 基址 / 页面基址 / WS 基址", () => {
  assert.equal(PLUGIN_ID, "kira-plugin-gamehub");
  assert.equal(API_BASE, "/api/plugin/kira-plugin-gamehub");
  assert.equal(PAGE_BASE, "/page/plugin/kira-plugin-gamehub/hub");
  assert.equal(WS_BASE, "/ws/plugin/kira-plugin-gamehub");
  assert.equal(HOST_TOKEN_KEY, "jwt_token");
  assert.ok(!API_BASE.includes("nori"), "不得残留 nori 插件 id");
});

test("localToken 只读宿主 JWT（缺失/隐私模式返回空串）", () => {
  const store = new Map([["jwt_token", "abc"]]);
  const ls = { getItem: (k) => (store.has(k) ? store.get(k) : null) };
  assert.equal(withGlobals({ localStorage: ls }, () => localToken()), "abc");
  assert.equal(withGlobals({ localStorage: { getItem: () => null } }, () => localToken()), "");
  const boom = { getItem() { throw new Error("denied"); } };
  assert.equal(withGlobals({ localStorage: boom }, () => localToken()), "", "隐私模式不抛");
});

test("matchWsUrl：/ws/plugin 路由 + 座位口令 t= + 宿主口令 token=", () => {
  const url = withGlobals(
    {
      location: { protocol: "https:", host: "kira.example:8443" },
      localStorage: { getItem: (k) => (k === "jwt_token" ? "HOST JWT" : null) },
    },
    () => matchWsUrl("m 1", "SEAT/TOK")
  );
  assert.equal(
    url,
    "wss://kira.example:8443/ws/plugin/kira-plugin-gamehub/matches/m%201/ws?t=SEAT%2FTOK&token=HOST+JWT"
  );
  assert.ok(!url.includes("/api/plugin/"), "WS 不在 API 基址下");
});

test("matchWsUrl：观战（无座位口令）只带宿主口令；http 走 ws:", () => {
  const url = withGlobals(
    {
      location: { protocol: "http:", host: "localhost:5267" },
      localStorage: { getItem: (k) => (k === "jwt_token" ? "J" : null) },
    },
    () => matchWsUrl("m-2", "")
  );
  assert.equal(url, "ws://localhost:5267/ws/plugin/kira-plugin-gamehub/matches/m-2/ws?token=J");
});

test("matchWsUrl：两者都缺时不带 query（握手失败由 4003 提示）", () => {
  const url = withGlobals(
    {
      location: { protocol: "http:", host: "localhost" },
      localStorage: { getItem: () => null },
    },
    () => matchWsUrl("m-3", "")
  );
  assert.equal(url, "ws://localhost/ws/plugin/kira-plugin-gamehub/matches/m-3/ws");
});

/* ---------------- 主题契约（宿主 webui stores/app.ts） ---------------- */

const fs = await import("node:fs");
const lobbyHtml = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const matchHtml = fs.readFileSync(new URL("../match.html", import.meta.url), "utf8");
const appCss = fs.readFileSync(new URL("../app.css", import.meta.url), "utf8");
const dzCss = fs.readFileSync(new URL("../games/doudizhu/table.css", import.meta.url), "utf8");

test("主题：读 localStorage[\"theme\"]、切 documentElement 的 dark 类、只认 storage key=theme", () => {
  for (const [name, html] of [["index.html", lobbyHtml], ["match.html", matchHtml]]) {
    assert.match(html, /localStorage\.getItem\("theme"\)/, `${name} 读宿主主题键`);
    assert.match(html, /classList\.toggle\("dark"/, `${name} 切 dark 类`);
    assert.match(html, /prefers-color-scheme: dark/, `${name} 系统偏好兜底`);
    assert.match(html, /e\.key === "theme"/, `${name} 只响应 theme 键`);
    assert.doesNotMatch(html, /data-theme/, `${name} 不再使用 data-theme 属性`);
    assert.doesNotMatch(html, /MutationObserver/, `${name} 不再观察父级/自身 data-theme`);
    assert.doesNotMatch(html, /window\.parent/, `${name} 不再读父级 frame`);
  }
});

test("主题：深色选择器与宿主一致用 html.dark（两个样式表）", () => {
  assert.ok(appCss.includes("html.dark"), "app.css 深色调色板挂在 html.dark 上");
  assert.ok(dzCss.includes("html.dark"), "牌桌样式同样跟随 html.dark");
  assert.ok(!/\[data-theme/.test(appCss + dzCss), "不得残留 data-theme 选择器");
});

/* ---------------- 错误码透传（shared/i18n.js 依赖） ---------------- */

const withGlobalsAsync = async (globals, fn) => {
  const saved = {};
  for (const [k, v] of Object.entries(globals)) {
    saved[k] = globalThis[k];
    globalThis[k] = v;
  }
  try {
    return await fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) globalThis[k] = v;
  }
};

const fakeResp = (status, payload) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => payload,
});

const catchErr = async (fn) => {
  try {
    await fn();
    return null;
  } catch (err) {
    return err;
  }
};

test("api：HTTP 失败信封把 code 透传到 ApiError", async () => {
  const err = await withGlobalsAsync(
    {
      localStorage: { getItem: () => null },
      fetch: async () => fakeResp(403, { ok: false, data: null, error: "座位口令无效", code: "bad_token" }),
    },
    () => catchErr(() => api("/matches/m1"))
  );
  assert.ok(err instanceof ApiError);
  assert.equal(err.status, 403);
  assert.equal(err.code, "bad_token");
  assert.equal(err.message, "座位口令无效");
});

test("api：2xx + ok:false 也透传 code；成功信封返回 data", async () => {
  const err = await withGlobalsAsync(
    {
      localStorage: { getItem: () => null },
      fetch: async () => fakeResp(200, { ok: false, data: null, error: "对局不存在", code: "not_found" }),
    },
    () => catchErr(() => api("/matches/none"))
  );
  assert.ok(err instanceof ApiError);
  assert.equal(err.code, "not_found");

  const data = await withGlobalsAsync(
    {
      localStorage: { getItem: () => null },
      fetch: async () => fakeResp(200, { ok: true, data: { a: 1 }, error: "" }),
    },
    () => api("/games")
  );
  assert.deepEqual(data, { a: 1 });

  const bare = new ApiError("x", 409);
  assert.equal(bare.code, "", "旧的两参构造仍兼容");
  assert.equal(new ApiError("x").status, 0);
});
