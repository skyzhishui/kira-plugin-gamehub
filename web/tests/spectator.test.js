import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { localToken, matchWsUrl } from "../shared/api.js";
import { normalizeMessage } from "../chat-panel.js";

test("spectator transport does not forward host JWT or seat tokens", () => {
  const originalDocument = globalThis.document;
  const originalLocation = globalThis.location;
  const originalSession = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  globalThis.document = { body: { dataset: { spectator: "true" } } };
  globalThis.location = { protocol: "http:", host: "localhost:15267", search: "?id=m1" };
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: { getItem: () => "guest-credential" } });
  try {
    assert.equal(localToken(), "guest-credential");
    const url = new URL(matchWsUrl("m1", "secret-seat"));
    assert.equal(url.searchParams.get("access"), "guest-credential");
    assert.equal(url.searchParams.has("token"), false);
    assert.equal(url.searchParams.has("t"), false);
  } finally {
    globalThis.document = originalDocument;
    globalThis.location = originalLocation;
    if (originalSession) Object.defineProperty(globalThis, "sessionStorage", originalSession);
    else delete globalThis.sessionStorage;
  }
});

test("spectator messages preserve their native role, nickname and UID", () => {
  const line = normalizeMessage({ id: "1", role: "spectator", uid: "m1:abc", sender: "Alice", text: "hello", ts: 1700000000000 });
  assert.equal(line.role, "spectator");
  assert.equal(line.uid, "m1:abc");
  assert.equal(line.sender, "Alice");
});

test("match controls remove rename and never offer spectator restart", () => {
  const source = fs.readFileSync(new URL("../match.js", import.meta.url), "utf8");
  const html = fs.readFileSync(new URL("../match.html", import.meta.url), "utf8");
  assert.equal(source.includes("openRename"), false);
  assert.equal(html.includes("rename-dlg"), false);
  assert.match(source, /if \(mine\) row\.appendChild\(actionBtn\(\{\s*label: restartLabel/);
  assert.match(source, /\/invite`/);
});
