/* media.test.js — 对局页媒体共享层轻量测试（node:test，零依赖）。
 * 覆盖：KiraAI chain / nori segments 抽取 / 宿主媒体端点 URL / 历史不重播
 * （水位线）/ 同消息双渲染不重复播 / 队列串行与 error 收尾 / 播放状态机
 * （playing/pause/blocked）/ 认证媒体加载缓存 / 图片装载占位机（fake DOM：
 * loading→loaded / failed / 无 src / 气泡自撤 / alt 保留）。
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  extractMessageParts,
  selectAutoPlayVoices,
  parseContentMarkdown,
  archivedMediaUrl,
  webchatMediaUrl,
  normalizeElementPath,
  isSameOriginUrl,
  cleanMediaTitle,
  createMediaLoader,
  createTtsQueue,
  mountImagePart,
  mountPlaceholderPart,
} from "../shared/media.js";

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/* ---------------- KiraAI chain 抽取 ---------------- */

test("chain 归档形态：图片/语音按 element_path 拼宿主端点；文本原样", () => {
  const { parts, fromSegments } = extractMessageParts({
    id: "m1",
    role: "assistant",
    chain: [
      { type: "text", text: "看这张" },
      { type: "image", file_type: "archive", name: "cat.png", caption: "开心猫" },
      { type: "record", file_type: "archive", name: "v.wav", transcript: "你好" },
    ],
  });
  assert.equal(fromSegments, true);
  assert.deepEqual(parts, [
    { kind: "text", text: "看这张" },
    { kind: "image", src: "/api/messages/m1/media/1", title: "开心猫" },
    { kind: "audio", src: "/api/messages/m1/media/2", title: "你好" },
  ]);
});

test("chain 未归档（file_type=unavailable）→ 占位且 deferred=false（不再等）", () => {
  const { parts } = extractMessageParts({
    id: 9,
    role: "assistant",
    chain: [{ type: "sticker", file_type: "unavailable", name: "s.png" }],
  });
  assert.deepEqual(parts, [{ kind: "placeholder", voice: false, title: "s.png", deferred: false }]);
});

test("chain 引用消息：文本标注，不递归展开媒体", () => {
  const { parts } = extractMessageParts({
    id: 3,
    chain: [{ type: "reply", message_content: "上一手", chain: [{ type: "image", file_type: "archive" }] }],
  });
  assert.deepEqual(parts, [{ kind: "text", text: "[引用] 上一手" }]);
});

/* ---------------- nori segments 抽取（旧载荷/fixture） ---------------- */

test("emoji+text 段：表情包渲染为图片，情绪作悬停提示，不再显示 [表情包:…] 文本", () => {
  const msg = {
    id: 12,
    role: "assistant",
    content: "[表情包:开心]你输啦",
    segments: [
      { type: "emoji", text: "开心", hash: "h1", local_media: true, media_kind: "image", local_url: "/api/messages/12/media/h1" },
      { type: "text", text: "你输啦" },
    ],
  };
  const { parts, fromSegments } = extractMessageParts(msg);
  assert.equal(fromSegments, true);
  assert.deepEqual(parts, [
    { kind: "image", src: "/api/messages/12/media/h1", title: "开心" },
    { kind: "text", text: "你输啦" },
  ]);
  const asText = parts.filter((p) => p.kind === "text").map((p) => p.text).join("");
  assert.ok(!asText.includes("[表情包"), "不得把 [表情包:…] 标记当文本重复渲染");
});

test("voice 段：local_media=true → audio(local_url)；local_media=false → 未缓存占位且不发请求", () => {
  const ok = extractMessageParts({
    id: 1,
    role: "assistant",
    content: "[语音]",
    segments: [{ type: "voice", text: "", hash: "v1", local_media: true, media_kind: "voice", local_url: "/api/messages/12/media/v1" }],
  });
  assert.deepEqual(ok.parts, [{ kind: "audio", src: "/api/messages/12/media/v1", title: "" }]);

  const missing = extractMessageParts({
    id: 2,
    role: "assistant",
    content: "[语音]",
    segments: [{ type: "voice", text: "", hash: "v2", local_media: false, media_kind: "voice" }],
  });
  assert.deepEqual(missing.parts, [{ kind: "placeholder", voice: true, title: "", deferred: false }]);
});

test("用户媒体描述 TEXT 段（[表情包]描述+hash）渲染原图，描述仅作提示", () => {
  const { parts } = extractMessageParts({
    id: 5,
    role: "user",
    content: "[表情包]一只橘猫在打滚",
    segments: [{ type: "text", text: "[表情包]一只橘猫在打滚", hash: "h3", local_media: true, media_kind: "image", local_url: "/api/messages/12/media/h3" }],
  });
  assert.deepEqual(parts, [{ kind: "image", src: "/api/messages/12/media/h3", title: "[表情包]一只橘猫在打滚" }]);
});

test("segments 全为非渲染段（at/reply）时回退 content 纯文本", () => {
  const { parts, fromSegments } = extractMessageParts({
    id: 3,
    role: "user",
    content: "@诺里 你好",
    segments: [{ type: "at", target_user_id: "nori" }],
  });
  assert.equal(fromSegments, false);
  assert.deepEqual(parts, [{ kind: "text", text: "@诺里 你好" }]);
});

test("纯文本 [表情包:…] 内容（无 hash/无 URL）绝不伪造图片", () => {
  const { parts } = extractMessageParts({
    id: 4,
    role: "assistant",
    content: "[表情包:一只猫咪在打滚]",
  });
  assert.deepEqual(parts, [{ kind: "text", text: "[表情包:一只猫咪在打滚]" }]);
  assert.ok(!parts.some((p) => p.kind === "image"));
});

test("content markdown 兜底：![表情](…) 与 [语音](…) 保留原始 URL（同源走 Bearer blob）", () => {
  const img = parseContentMarkdown("![表情](http://127.0.0.1:8000/v1/media/abc.png)哈哈你输啦");
  assert.deepEqual(img, [
    { kind: "image", src: "http://127.0.0.1:8000/v1/media/abc.png", title: "表情" },
    { kind: "text", text: "哈哈你输啦" },
  ]);
  const voice = parseContentMarkdown("[语音](https://host.example/v1/media/xyz.wav)");
  assert.deepEqual(voice, [{ kind: "audio", src: "https://host.example/v1/media/xyz.wav", title: "语音" }]);
  assert.equal(parseContentMarkdown("普通文本"), null);
});

/* ---------------- URL 解析 ---------------- */

test("媒体 URL：KiraAI 归档/网页聊天端点；同源判定外链/data:/blob: 原样", () => {
  assert.equal(archivedMediaUrl(12, "0"), "/api/messages/12/media/0");
  assert.equal(archivedMediaUrl(12, "2.1"), "/api/messages/12/media/2.1");
  assert.equal(archivedMediaUrl("", "0"), "", "缺 message_id 不拼端点");
  assert.equal(archivedMediaUrl(12, "../etc"), "", "非法 element_path 拒绝");
  assert.equal(webchatMediaUrl(7, "1"), "/api/webchat/messages/7/media/1");
  assert.equal(normalizeElementPath(" 3.0 "), "3.0");
  assert.equal(normalizeElementPath(""), "");
  assert.equal(isSameOriginUrl("/api/messages/12/media/0", "http://localhost:8000/match.html"), true);
  assert.equal(isSameOriginUrl("https://else.example/v1/media/x", "http://localhost:8000/"), false);
  assert.equal(isSameOriginUrl("data:image/png;base64,AA"), false);
  assert.equal(isSameOriginUrl("blob:http://localhost/xx"), false);
  assert.equal(cleanMediaTitle("[语音]今天天气不错"), "今天天气不错");
  assert.equal(cleanMediaTitle("[表情包]一只橘猫"), "一只橘猫");
  assert.equal(cleanMediaTitle("[表情包:开心]"), "", "整枚标记形态剥掉（desc 在标记内）");
});

/* ---------------- 自动播放选择（历史不重播 / 仅诺里新消息） ---------------- */

test("水位线：初次拉历史只记水位不播；旧消息不播；仅诺里新语音播；用户语音不播", () => {
  const history = [
    { id: 1, role: "user", content: "hi" },
    { id: 2, role: "assistant", content: "[语音]", segments: [{ type: "voice", text: "", hash: "v1", local_media: true, media_kind: "voice", local_url: "/api/messages/12/media/v1" }] },
    { id: 3, role: "assistant", content: "纯文本回复" },
  ];
  // 初载（历史/重连）：只推水位（诺里最大 id，含纯文本消息）
  const first = selectAutoPlayVoices(history, 0, { firstLoad: true });
  assert.deepEqual(first.jobs, []);
  assert.equal(first.nextWatermark, 3);
  // 同一批历史再来一次（SSE/轮询重拉）：仍不播
  const again = selectAutoPlayVoices(history, first.nextWatermark, { firstLoad: false });
  assert.deepEqual(again.jobs, []);
  assert.equal(again.nextWatermark, 3);
  // 新诺里语音消息：入队一次
  const withNew = [
    ...history,
    { id: 4, role: "assistant", content: "[语音]", segments: [{ type: "voice", text: "", hash: "v4", local_media: true, media_kind: "voice", local_url: "/api/messages/12/media/v4" }] },
    { id: 5, role: "user", content: "[语音]", segments: [{ type: "voice", text: "", hash: "u5", local_media: true, media_kind: "voice", local_url: "/api/messages/12/media/u5" }] },
  ];
  const sel = selectAutoPlayVoices(withNew, 3, { firstLoad: false });
  assert.deepEqual(sel.jobs, [{ key: "4:0", url: "/api/messages/12/media/v4", title: "语音" }]);
  assert.equal(sel.nextWatermark, 4, "用户消息不推水位");
  // 同一条消息再重拉（聊天窗+气泡双渲染场景同 key）：不重复播
  const sel2 = selectAutoPlayVoices(withNew, sel.nextWatermark, { firstLoad: false });
  assert.deepEqual(sel2.jobs, []);
});

test("seg_elements 注解未就绪的语音：水位先不越过，等重拉补上 local_url 后仍能播", () => {
  const pending = [
    { id: 6, role: "assistant", content: "[语音]", segments: [{ type: "voice", text: "" }] },
  ];
  const sel = selectAutoPlayVoices(pending, 5, { firstLoad: false });
  assert.deepEqual(sel.jobs, []);
  assert.equal(sel.nextWatermark, 5, "水位停在未就绪消息之前");
  const ready = selectAutoPlayVoices(
    [{ id: 6, role: "assistant", content: "[语音]", segments: [{ type: "voice", text: "", hash: "v6", local_media: true, media_kind: "voice", local_url: "/api/messages/12/media/v6" }] }],
    sel.nextWatermark,
    { firstLoad: false }
  );
  assert.deepEqual(ready.jobs, [{ key: "6:0", url: "/api/messages/12/media/v6", title: "语音" }]);
});

test("乐观本地消息（无 DB id）永不自动播", () => {
  const sel = selectAutoPlayVoices(
    [{ role: "assistant", content: "[语音](http://127.0.0.1:8000/v1/media/tmp.wav)" }],
    0,
    { firstLoad: false }
  );
  assert.deepEqual(sel.jobs, []);
  assert.equal(sel.nextWatermark, 0);
});

/* ---------------- TTS 队列 ---------------- */

function fakeAudioFactory(created, log, playImpl) {
  return (src, job) => {
    const listeners = {};
    const audio = {
      src,
      paused: true,
      addEventListener(t, fn) {
        (listeners[t] = listeners[t] || []).push(fn);
      },
      removeEventListener(t, fn) {
        const arr = listeners[t];
        if (arr) {
          const i = arr.indexOf(fn);
          if (i >= 0) arr.splice(i, 1);
        }
      },
      emit(t) {
        (listeners[t] || []).slice().forEach((fn) => fn({ type: t }));
      },
      play() {
        audio.paused = false;
        log.push(["play", job.key]);
        return playImpl ? playImpl(job) : Promise.resolve();
      },
      pause() {
        const was = audio.paused;
        audio.paused = true;
        if (!was) audio.emit("pause");
      },
      removeAttribute(name) {
        if (name === "src") audio.src = "";
      },
      load() {
        log.push(["load", job.key]);
      },
    };
    created.push(audio);
    return audio;
  };
}

test("队列：同 key 去重（同消息双渲染只播一次），串行播放（前一条 ended 后才播下一条）", async () => {
  const created = [];
  const log = [];
  const states = [];
  const q = createTtsQueue({
    loadSrc: (u) => Promise.resolve(u),
    makeAudio: fakeAudioFactory(created, log),
    onState: (s) => states.push(`${s.state}:${s.key || "-"}`),
  });
  assert.equal(q.enqueue({ key: "a", url: "u:a", title: "A" }), true);
  assert.equal(q.enqueue({ key: "a", url: "u:a", title: "A" }), false, "同 key 二次入队被拒");
  await flush();
  assert.equal(created.length, 1);
  created[0].emit("playing");
  assert.deepEqual(q.snapshot(), { state: "playing", key: "a" });
  q.enqueue({ key: "b", url: "u:b", title: "B" });
  await flush();
  assert.equal(created.length, 1, "串行：第一条未结束不开始第二条");
  created[0].emit("ended");
  await flush();
  assert.equal(created.length, 2);
  created[1].emit("playing");
  assert.deepEqual(q.snapshot(), { state: "playing", key: "b" });
  created[1].emit("ended");
  await flush();
  assert.deepEqual(q.snapshot(), { state: "idle", key: null });
  assert.deepEqual(log.filter((x) => x[0] === "play"), [["play", "a"], ["play", "b"]]);
  assert.ok(states.includes("playing:a"), "speaker 动效信号只在真实 playing 时出现");
  assert.ok(states.includes("idle:-"), "ended 后清播放态");
});

test("队列：元素 error → 置 error 并继续下一条；失败 key 永不重试（不无限重播）", async () => {
  const created = [];
  const log = [];
  const states = [];
  const q = createTtsQueue({
    loadSrc: (u) => Promise.resolve(u),
    makeAudio: fakeAudioFactory(created, log),
    onState: (s) => states.push(s.state),
  });
  q.enqueue({ key: "bad", url: "u:bad" });
  q.enqueue({ key: "next", url: "u:next" });
  await flush();
  created[0].emit("error");
  await flush();
  assert.ok(states.includes("error"), "error 状态被上报（UI 清喇叭动效）");
  assert.equal(created.length, 2, "出错后继续队列");
  created[1].emit("playing");
  assert.deepEqual(q.snapshot(), { state: "playing", key: "next" });
  assert.equal(q.enqueue({ key: "bad", url: "u:bad" }), false, "失败的 key 不再入队");
});

test("队列：play() 抛非拦截类错误（格式不支持）→ 置 error 且队列不卡死", async () => {
  const created = [];
  const log = [];
  const states = [];
  const unsupported = () =>
    Promise.reject(Object.assign(new Error("format"), { name: "NotSupportedError" }));
  const q = createTtsQueue({
    loadSrc: (u) => Promise.resolve(u),
    makeAudio: fakeAudioFactory(created, log, (job) => (job.key === "fmt" ? unsupported() : Promise.resolve())),
    onState: (s) => states.push(`${s.state}:${s.key || "-"}`),
  });
  q.enqueue({ key: "fmt", url: "u:fmt" });
  q.enqueue({ key: "good", url: "u:good" });
  await flush();
  assert.ok(states.includes("error:fmt"), "play() 非拦截错误按 error 收尾");
  created[1].emit("playing");
  assert.deepEqual(q.snapshot(), { state: "playing", key: "good" }, "队列继续，不卡死");
  created[1].emit("ended");
  await flush();
  assert.deepEqual(q.snapshot(), { state: "idle", key: null });
});

test("队列：pause 事件 → paused（清喇叭动效），toggle 续播", async () => {
  const created = [];
  const log = [];
  const q = createTtsQueue({
    loadSrc: (u) => Promise.resolve(u),
    makeAudio: fakeAudioFactory(created, log),
    onState: () => {},
  });
  q.enqueue({ key: "p", url: "u:p" });
  await flush();
  created[0].emit("playing");
  q.toggle({ key: "p", url: "u:p" }); // 播放中点击 → 暂停
  await flush();
  assert.deepEqual(q.snapshot(), { state: "paused", key: "p" });
  q.toggle({ key: "p", url: "u:p" }); // 暂停中点击 → 续播
  created[0].emit("playing"); // 真实浏览器由 Audio 派发 playing 事件
  await flush();
  assert.deepEqual(q.snapshot(), { state: "playing", key: "p" });
});

test("自动播放被拦截：不自动重试、不声称已播放；unlock（手势）恢复；超上限放弃", async () => {
  const created = [];
  const log = [];
  const states = [];
  const deny = () => Promise.reject(Object.assign(new Error("denied"), { name: "NotAllowedError" }));
  const q = createTtsQueue({
    loadSrc: (u) => Promise.resolve(u),
    makeAudio: fakeAudioFactory(created, log, (job) => (job.key === "t2" ? Promise.resolve() : deny(job))),
    onState: (s) => states.push(`${s.state}:${s.key || "-"}`),
  });
  q.enqueue({ key: "t1", url: "u:t1" });
  q.enqueue({ key: "t2", url: "u:t2" });
  await flush();
  assert.deepEqual(q.snapshot(), { state: "blocked", key: "t1" });
  await flush();
  await flush();
  assert.equal(created.length, 1, "被拦截后不自动重试、不出队下一条");
  assert.ok(!states.includes("playing:t1"), "未真实出声绝不发 playing 信号");
  // 第一次手势解锁：再试一次，仍被拦（attempts=2）
  q.unlock();
  await flush();
  assert.deepEqual(q.snapshot(), { state: "blocked", key: "t1" });
  // 第二次手势解锁：attempts=3 达上限 → 放弃自动播（不声称已播放），队列继续 t2
  q.unlock();
  await flush();
  assert.equal(created.length, 4, "放弃 t1 后继续队列（3 次尝试 + t2）");
  assert.equal(q.enqueue({ key: "t1", url: "u:t1" }), false, "放弃的 key 不再入队");
  created[3].emit("playing");
  assert.deepEqual(q.snapshot(), { state: "playing", key: "t2" });
  created[3].emit("ended");
  await flush();
  assert.deepEqual(q.snapshot(), { state: "idle", key: null });
});

test("dispose：暂停音频、清队列、状态上报 disposed", async () => {
  const created = [];
  const log = [];
  const states = [];
  const q = createTtsQueue({
    loadSrc: (u) => Promise.resolve(u),
    makeAudio: fakeAudioFactory(created, log),
    onState: (s) => states.push(s.state),
  });
  q.enqueue({ key: "d", url: "u:d" });
  await flush();
  created[0].emit("playing");
  q.enqueue({ key: "d2", url: "u:d2" });
  q.dispose();
  assert.deepEqual(q.snapshot(), { state: "disposed", key: null });
  assert.equal(created[0].paused, true, "dispose 暂停了正在播放的音频");
  assert.equal(q.enqueue({ key: "d3", url: "u:d3" }), false, "dispose 后拒绝入队");
  created[0].emit("ended");
  await flush();
  assert.equal(created.length, 1, "dispose 后不再开始新任务");
});

test("切会话重建队列（match.js rebind 同款模式）：旧队列停播，新队列 seen 复位，旧 key 不拦新会话音频", async () => {
  // 旧会话：入队并在播
  const created1 = [];
  const log1 = [];
  const q1 = createTtsQueue({
    loadSrc: (u) => Promise.resolve(u),
    makeAudio: fakeAudioFactory(created1, log1),
    onState: () => {},
  });
  q1.enqueue({ key: "4:0", url: "u:old" });
  await flush();
  created1[0].emit("playing");
  assert.deepEqual(q1.snapshot(), { state: "playing", key: "4:0" });
  // 切会话整编：dispose 旧队列 → 在播音频暂停
  q1.dispose();
  assert.equal(created1[0].paused, true);
  assert.equal(q1.enqueue({ key: "9:0", url: "u:new" }), false, "旧队列 dispose 后拒绝入队");
  // 新会话全新队列：seen 复位，旧消息 id 形态的 key 也可正常入队播放
  const created2 = [];
  const log2 = [];
  const q2 = createTtsQueue({
    loadSrc: (u) => Promise.resolve(u),
    makeAudio: fakeAudioFactory(created2, log2),
    onState: () => {},
  });
  assert.equal(q2.has("4:0"), false, "新队列不携带旧会话 seen");
  assert.equal(q2.enqueue({ key: "4:0", url: "u:new-session-same-id" }), true, "旧 key 不拦新会话音频");
  await flush();
  created2[0].emit("playing");
  assert.deepEqual(q2.snapshot(), { state: "playing", key: "4:0" });
});

/* ---------------- 认证媒体加载 ---------------- */

test("loader：同源 Bearer fetch → blob URL，同 URL 合并/缓存一次拉取；data: 原样；失败抛错", async () => {
  let calls = 0;
  let seq = 0;
  let seenAuth = "";
  const loader = createMediaLoader({
    getToken: () => "tok",
    fetchImpl: async (url, opts) => {
      calls += 1;
      seenAuth = (opts && opts.headers && opts.headers.Authorization) || "";
      if (String(url).endsWith("bad")) return { ok: false, status: 404 };
      return { ok: true, blob: async () => ({ size: 3 }) };
    },
    createObjectUrl: () => `blob:x${++seq}`,
  });
  const a = await loader.objectUrl("/api/messages/12/media/h1");
  const b = await loader.objectUrl("/api/messages/12/media/h1");
  assert.equal(a, b, "缓存命中同一路 blob");
  assert.equal(calls, 1, "同 URL 只拉一次");
  assert.equal(seenAuth, "Bearer tok", "令牌进请求头");
  const passthrough = await loader.objectUrl("data:image/png;base64,AA");
  assert.equal(passthrough, "data:image/png;base64,AA", "data: 不 fetch");
  await assert.rejects(() => loader.objectUrl("/api/messages/12/media/bad"));
  loader.revokeAll();
  await assert.rejects(() => loader.objectUrl("/api/messages/12/media/h1"), undefined, "revoke 后不可再用");
});

test("loader：revokeAll 后在途响应迟到 → 不创建/回填 blob，调用方收到释放错误", async () => {
  let release;
  let createdCount = 0;
  const loader = createMediaLoader({
    fetchImpl: () =>
      new Promise((resolve) => {
        release = resolve;
      }),
    createObjectUrl: () => {
      createdCount += 1;
      return "blob:late";
    },
  });
  const pending = loader.objectUrl("/api/messages/12/media/slow");
  loader.revokeAll(); // 切会话/离页：全量清理
  release({ ok: true, blob: async () => ({ size: 4 }) });
  await assert.rejects(pending, /媒体加载器已释放/, "迟到结果作废");
  assert.equal(createdCount, 0, "释放后绝不创建 object URL（不泄漏）");
  assert.equal(await loader.objectUrl("/api/messages/12/media/slow").catch(() => "closed"), "closed");
});

/* ---------------- 图片装载占位机（极简 DOM 桩，不依赖浏览器） ---------------- */

/** 只实现 nori-media.js DOM 构建器行为面的 fake document。
 * onSrcSet 可模拟「缓存命中、赋 src 同步触发 load」的极端时序。 */
function makeFakeDocument({ onSrcSet = null } = {}) {
  const created = [];
  class FakeEl {
    constructor(tag) {
      this.tagName = String(tag).toUpperCase();
      this.childNodes = [];
      this.parentNode = null;
      this.className = "";
      this.attrs = {};
      this.innerHTML = "";
      this.textContent = "";
      this.title = "";
      this.alt = "";
      this.decoding = "";
      let srcVal = "";
      Object.defineProperty(this, "src", {
        configurable: true,
        enumerable: true,
        get: () => srcVal,
        set: (v) => {
          srcVal = String(v == null ? "" : v);
          if (srcVal && typeof onSrcSet === "function") onSrcSet(this);
        },
      });
      this.listeners = {};
      created.push(this);
      const self = this;
      this.classList = {
        add(c) {
          if (!self.className.split(/\s+/).includes(c)) {
            self.className = (self.className ? `${self.className} ` : "") + c;
          }
        },
        contains(c) {
          return self.className.split(/\s+/).includes(c);
        },
      };
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return this.attrs[k]; }
    removeAttribute(k) {
      delete this.attrs[k];
      if (k === "src") this.src = "";
    }
    appendChild(n) { n.parentNode = this; this.childNodes.push(n); return n; }
    remove() {
      const p = this.parentNode;
      if (!p) return;
      const i = p.childNodes.indexOf(this);
      if (i >= 0) p.childNodes.splice(i, 1);
      this.parentNode = null;
    }
    replaceWith(n) {
      const p = this.parentNode;
      if (!p) return;
      const i = p.childNodes.indexOf(this);
      if (i >= 0) p.childNodes[i] = n;
      this.parentNode = null;
      n.parentNode = p;
    }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    removeEventListener(t, fn) {
      const arr = this.listeners[t];
      if (arr) {
        const i = arr.indexOf(fn);
        if (i >= 0) arr.splice(i, 1);
      }
    }
    decode() { return this.__decodeResult || Promise.resolve(); }
  }
  return { createElement: (tag) => new FakeEl(tag), created };
}

function useFakeDoc() {
  const doc = makeFakeDocument();
  globalThis.document = doc;
  return doc;
}

const imgIn = (doc) => doc.created.find((n) => n.tagName === "IMG");

test("图片装载：先 loading 占位（skel 微光、aria-hidden、无可见 img），解码成功才原位亮图且保留 alt", async () => {
  const doc = useFakeDoc();
  let fetches = 0;
  const loader = {
    objectUrl: () => {
      fetches += 1;
      return Promise.resolve("blob:x1");
    },
  };
  const body = doc.createElement("div");
  const ph = mountImagePart({ src: "/api/messages/12/media/h1", title: "开心猫" }, loader, {
    className: "msg-media-img",
  });
  body.appendChild(ph);
  assert.equal(fetches, 1, "有 src 即拉取");
  assert.ok(ph.className.includes("msg-media-loading"), "loading 占位类");
  assert.ok(ph.className.includes("skel"), "诺里 skel 微光语言");
  assert.equal(ph.getAttribute("aria-hidden"), "true", "占位不进读屏");
  const img = imgIn(doc);
  assert.ok(img, "img 已预创建");
  assert.equal(img.parentNode, null, "解码完成前 img 绝不入 DOM（不闪 alt/破图）");
  assert.equal(img.alt, "开心猫", "alt 保留在真实 img 上");
  assert.equal(img.title, "开心猫", "悬停提示保留");
  assert.equal(img.src, "", "src 未就绪不赋值");
  // blob 就绪但解码未完成：img 仍未展示
  let decodeOk;
  img.__decodeResult = new Promise((res) => { decodeOk = res; });
  await flush();
  assert.equal(img.src, "blob:x1", "blob URL 已挂上");
  assert.equal(img.parentNode, null, "解码未完成仍不入 DOM");
  decodeOk();
  await flush();
  assert.equal(img.parentNode, body, "解码成功后原位换入");
  assert.equal(body.childNodes[0], img, "占位被 img 替换");
  assert.ok(img.classList.contains("msg-media-img"), "聊天窗图片类保留");
  assert.ok(img.classList.contains("is-ready"), "亮场淡入类");
});

test("图片装载：无 decode 时先挂 load 再赋 src，缓存同步 load 也不会永久卡 loading", async () => {
  let syncLoadCount = 0;
  const doc = makeFakeDocument({
    onSrcSet: (node) => {
      if (node.tagName !== "IMG") return;
      syncLoadCount += 1;
      (node.listeners.load || []).slice().forEach((fn) => fn()); // 模拟缓存命中同步 load
    },
  });
  globalThis.document = doc;
  const body = doc.createElement("div");
  const ph = mountImagePart(
    { src: "/api/messages/12/media/cached", title: "[表情包]开心猫" },
    { objectUrl: () => Promise.resolve("blob:cached") },
    {}
  );
  body.appendChild(ph);
  const img = imgIn(doc);
  // 老内核：无 decode()，只能靠 load 事件
  Object.defineProperty(img, "decode", { value: undefined, configurable: true });
  await flush();
  assert.equal(syncLoadCount, 1, "赋 src 即同步 load");
  assert.equal(img.parentNode, body, "监听先于 src，同步 load 也能亮图");
  assert.equal(img.alt, "开心猫", "alt 保留且剥掉媒体标记");
  assert.equal(body.childNodes[0], img);
});

test("图片装载：blob 拉取/解码失败 → onFail 换可读「加载失败」占位，绝不插入 img", async () => {
  // 拉取失败（HTTP 404 等）
  const doc1 = useFakeDoc();
  const body1 = doc1.createElement("div");
  const ph1 = mountImagePart({ src: "/api/messages/12/media/bad", title: "[表情包]一只橘猫在打滚" }, {
    objectUrl: () => Promise.reject(new Error("HTTP 404")),
  }, {
    onFail: (node) =>
      node.replaceWith(mountPlaceholderPart({ voice: false, title: "[表情包]一只橘猫在打滚", failed: true })),
  });
  body1.appendChild(ph1);
  await flush();
  const box1 = body1.childNodes[0];
  assert.equal(box1.className, "msg-media-ph", "失败换可读占位");
  assert.equal(box1.childNodes[1].textContent, "图片加载失败");
  assert.equal(box1.childNodes[2].textContent, "一只橘猫在打滚", "描述剥离标记后保留");
  assert.ok(!body1.childNodes.some((n) => n.tagName === "IMG"), "全程无 img");

  // 解码失败（坏损 blob）
  const doc2 = useFakeDoc();
  const body2 = doc2.createElement("div");
  const ph2 = mountImagePart({ src: "/api/messages/12/media/corrupt", title: "" }, {
    objectUrl: () => Promise.resolve("blob:corrupt"),
  }, {
    onFail: (node) =>
      node.replaceWith(mountPlaceholderPart({ voice: false, title: "", failed: true })),
  });
  body2.appendChild(ph2);
  const img2 = imgIn(doc2);
  img2.__decodeResult = Promise.reject(new Error("decode fail"));
  await flush();
  await flush();
  const box2 = body2.childNodes[0];
  assert.equal(box2.className, "msg-media-ph", "解码失败同路兜底");
  assert.equal(box2.childNodes[1].textContent, "图片加载失败");
  assert.equal(img2.src, "", "坏损 blob 的 src 已摘除");
  assert.ok(!body2.childNodes.some((n) => n.tagName === "IMG"), "破损图标永不展示");
});

test("图片装载：无 src 不创建 img、不发请求，兜底占位在挂载后落地", async () => {
  const doc = useFakeDoc();
  let fetches = 0;
  const loader = {
    objectUrl: () => {
      fetches += 1;
      return Promise.resolve("blob:x");
    },
  };
  const body = doc.createElement("div");
  const ph = mountImagePart({ src: "", title: "没图" }, loader, {
    onFail: (node) =>
      node.replaceWith(mountPlaceholderPart({ voice: false, title: "没图", failed: true })),
  });
  body.appendChild(ph);
  assert.equal(fetches, 0, "无 src 不发请求");
  assert.ok(!doc.created.some((n) => n.tagName === "IMG"), "无 src 不创建 img");
  await flush();
  assert.equal(body.childNodes[0].className, "msg-media-ph", "兜底占位落地");
  assert.equal(body.childNodes[0].childNodes[1].textContent, "图片加载失败");
});

test("图片装载：onFail 缺省（座位气泡路）失败整块自撤，不留破损图标", async () => {
  const doc = useFakeDoc();
  const body = doc.createElement("div");
  let rejectFetch;
  const ph = mountImagePart({ src: "/api/messages/12/media/late", title: "表情" }, {
    objectUrl: () => new Promise((_, rej) => { rejectFetch = rej; }),
  });
  body.appendChild(ph);
  rejectFetch(new Error("boom"));
  await flush();
  assert.equal(body.childNodes.length, 0, "气泡路失败自撤（:empty 收起）");
});

test("「图片未缓存」占位缺省文案不变（failed 缺省时向后兼容）", () => {
  useFakeDoc(); // mountPlaceholderPart 走 document.createElement
  const box = mountPlaceholderPart({ voice: false, title: "[表情包]橘猫" });
  assert.equal(box.className, "msg-media-ph");
  assert.equal(box.childNodes[1].textContent, "图片未缓存", "历史语义不变");
  const voiceBox = mountPlaceholderPart({ voice: true, title: "" });
  assert.equal(voiceBox.childNodes[1].textContent, "语音未缓存");
});
