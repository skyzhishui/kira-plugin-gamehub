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

import { PLUGIN_ID, API_BASE, PAGE_BASE, WS_BASE, HOST_TOKEN_KEY, localToken, matchWsUrl } from "../shared/api.js";

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
