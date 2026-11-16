/* bubble-queue.test.js — seat-speech bubble queue (node:test, zero deps, fake clock).
 *
 * Requirements locked down (host-agent seat and practice/advanced-opponent seats share
 * one rule set):
 * 1) Messages show one at a time in arrival order: never merged, never "latest only";
 * 2) Each stays >= 1s (min interval); without a backlog it auto-hides by 3s;
 * 3) With a backlog the next entry rotates in right at 1s (no idle waiting);
 * 4) The first snapshot only seeds, it never plays; dedupe is by key; same content
 *    under different keys is two independent messages;
 * 5) reset/dispose clear timers and queued entries.
 *
 * The match page feeds this from match.chat snapshot deltas (no SSE or streaming
 * reply); the old provisional entry / finalize / history-refill wiring had no callers,
 * so its tests were removed together with the implementation.
 *
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  createBubbleQueue,
  BUBBLE_MIN_SHOW_MS,
  BUBBLE_MAX_SHOW_MS,
} from "../shared/bubble-queue.js";

/* ---------------- 假时钟：可手动推进，记录每条展示的起止 ---------------- */

function fakeClock() {
  let t = 0;
  let seq = 0;
  const timers = new Map(); // id -> {at, fn}
  return {
    now: () => t,
    setTimer(fn, ms) {
      const id = ++seq;
      timers.set(id, { at: t + Math.max(0, ms), fn });
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    pending: () => timers.size,
    /** 推进 ms 毫秒，按到期顺序触发（同一时刻按注册顺序）。 */
    advance(ms) {
      const end = t + ms;
      for (;;) {
        let next = null;
        for (const [id, timer] of timers) {
          if (timer.at <= end && (!next || timer.at < next.timer.at || (timer.at === next.timer.at && id < next.id))) {
            next = { id, timer };
          }
        }
        if (!next) break;
        timers.delete(next.id);
        t = next.timer.at;
        next.timer.fn();
      }
      t = end;
    },
  };
}

function makeQueue(opts = {}) {
  const clock = fakeClock();
  const shown = []; // {key,text,at}
  const hidden = []; // {key,at}
  const q = createBubbleQueue({
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    now: clock.now,
    onShow: (item) => shown.push({ key: item.key, text: item.text, at: clock.now() }),
    onHide: (item) => hidden.push({ key: item.key, at: clock.now() }),
    ...opts,
  });
  return { q, clock, shown, hidden };
}

/* ---------------- 基本顺序 / 1s / 3s ---------------- */

test("常量：最短 1s、最长 3s", () => {
  assert.equal(BUBBLE_MIN_SHOW_MS, 1000);
  assert.equal(BUBBLE_MAX_SHOW_MS, 3000);
});

test("单条：立即展示，3s 后自动收起（空闲上限）", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.enqueue({ key: "say:1", text: "第一句" });
  assert.deepEqual(shown.map((s) => s.text), ["第一句"]);
  assert.equal(shown[0].at, 0);
  clock.advance(999);
  assert.equal(hidden.length, 0, "1s 前不收起");
  clock.advance(1);
  assert.equal(hidden.length, 0, "1s 时无排队也不收起（可停到 3s）");
  clock.advance(2000);
  assert.deepEqual(hidden.map((h) => h.key), ["say:1"], "满 3s 收起");
  assert.equal(q.currentKey(), "");
  assert.equal(clock.pending(), 0, "收起后无遗留计时器");
});

test("两条几乎同时到达：按到达顺序展示，第二条满 1s 才换", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.enqueue({ key: "say:1", text: "A" });
  q.enqueue({ key: "say:2", text: "B" });
  assert.deepEqual(shown.map((s) => s.text), ["A"], "第二条先排队");
  assert.deepEqual(q.pendingKeys(), ["say:2"]);
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.text), ["A", "B"], "满 1s 按顺序换 B");
  assert.equal(hidden.length, 0, "换条不算收起");
  clock.advance(3000);
  assert.deepEqual(hidden.map((h) => h.key), ["say:2"], "B 无排队 → 3s 收起");
});

test("三条连发：逐条展示，每条 1s，最后一条 3s 收起（不合并、不取最新）", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.enqueue({ key: "say:1", text: "一" });
  q.enqueue({ key: "say:2", text: "二" });
  q.enqueue({ key: "say:3", text: "三" });
  clock.advance(1000);
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.text), ["一", "二", "三"], "全部按到达顺序展示");
  assert.deepEqual(
    shown.map((s) => s.at),
    [0, 1000, 2000],
    "每条至少 1s 的间隔"
  );
  assert.equal(hidden.length, 0);
  clock.advance(2999);
  assert.equal(hidden.length, 0);
  clock.advance(1);
  assert.deepEqual(hidden.map((h) => h.key), ["say:3"], "最后一条 3s 收起");
});

test("展示中途到达的新消息：等满 1s 才替换（不打断正在看的这条）", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "say:1", text: "A" });
  clock.advance(400);
  q.enqueue({ key: "say:2", text: "B" });
  assert.deepEqual(shown.map((s) => s.text), ["A"], "400ms 时 A 还在展示");
  clock.advance(599); // t=999
  assert.deepEqual(shown.map((s) => s.text), ["A"]);
  clock.advance(1); // t=1000
  assert.deepEqual(shown.map((s) => s.text), ["A", "B"], "满 1s 立即推进，不空等到 3s");
  assert.equal(shown[1].at, 1000);
});

test("空闲时新消息到达：立即展示（不排队等），旧条目已收起", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.enqueue({ key: "say:1", text: "A" });
  clock.advance(3000); // A 已收起
  assert.deepEqual(hidden.map((h) => h.key), ["say:1"]);
  q.enqueue({ key: "say:2", text: "B" });
  assert.equal(shown.at(-1).at, 3000, "空闲时立即展示");
  assert.equal(shown.at(-1).text, "B");
});

test("突发 5 条：全部依次展示，单条最长 3s、最短 1s", () => {
  const { q, clock, shown, hidden } = makeQueue();
  ["一", "二", "三", "四", "五"].forEach((text, i) => q.enqueue({ key: `say:${i + 1}`, text }));
  clock.advance(4000);
  assert.deepEqual(shown.map((s) => s.text), ["一", "二", "三", "四", "五"]);
  for (let i = 1; i < shown.length; i++) {
    assert.ok(shown[i].at - shown[i - 1].at >= BUBBLE_MIN_SHOW_MS, "相邻两条间隔 ≥ 1s");
  }
  clock.advance(1000); // t=5000, 最后一条从 4000 起算满 3s 于 7000
  clock.advance(2000);
  assert.equal(hidden.at(-1).key, "say:5");
  assert.equal(hidden.at(-1).at, 7000, "最后一条 3s 收起");
});

test("队列持续有积压时单条不会停到 3s（满 1s 就换）", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "say:1", text: "A" });
  for (let i = 0; i < 3; i++) {
    clock.advance(1000);
    q.enqueue({ key: `say:${i + 2}`, text: `N${i}` });
  }
  const gaps = shown.slice(1).map((s, i) => s.at - shown[i].at);
  assert.deepEqual(gaps, [1000, 1000, 1000], "有排队 → 每条正好 1s");
});

/* ---------------- dedupe / seeding / same content different key ---------------- */

test("key 去重：同 key 再次入队被忽略（展示完也不重播）", () => {
  const { q, clock, shown } = makeQueue();
  assert.equal(q.enqueue({ key: "say:7", text: "同一句" }), "queued");
  assert.equal(q.enqueue({ key: "say:7", text: "同一句" }), "duplicate");
  clock.advance(3000);
  assert.equal(q.enqueue({ key: "say:7", text: "同一句" }), "duplicate", "展示完也不重播");
  assert.deepEqual(shown.map((s) => s.key), ["say:7"]);
});

test("相同内容不同 key：是两条独立消息，各弹一次", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "say:11", text: "好" });
  q.enqueue({ key: "say:12", text: "好" });
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.key), ["say:11", "say:12"], "同文不同 key 不合并");
});

test("初载播种：seed 记水位且不展示；之后的同 key 不再入队", () => {
  const { q, clock, shown, hidden } = makeQueue();
  const added = q.seed(["say:1", "say:2", "say:3"]);
  assert.equal(added, 3);
  assert.equal(shown.length, 0, "历史首载绝不自动弹");
  assert.equal(clock.pending(), 0, "播种不建计时器");
  assert.equal(q.enqueue({ key: "say:3", text: "旧消息" }), "duplicate");
  assert.equal(q.enqueue({ key: "say:4", text: "新消息" }), "queued");
  assert.deepEqual(shown.map((s) => s.key), ["say:4"]);
  assert.equal(hidden.length, 0);
});

/* ---------------- reset / dispose ---------------- */

test("reset：清计时器与排队条目、收起气泡（默认清水位）", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.seed(["say:0"]);
  q.enqueue({ key: "say:1", text: "A" });
  q.enqueue({ key: "say:2", text: "B" });
  q.reset();
  assert.equal(q.size(), 0, "排队与当前条目全清");
  assert.deepEqual(hidden.map((h) => h.key), ["say:1"], "当前气泡收起");
  assert.equal(clock.pending(), 0, "计时器清空");
  clock.advance(10000);
  assert.equal(shown.length, 1, "reset 后不再有任何展示");
  assert.equal(q.enqueue({ key: "say:1", text: "A" }), "queued", "水位清空后可重新展示");
});

test("reset({keepSeen:true})：保留水位（切局防重播同 key）", () => {
  const { q } = makeQueue();
  q.seed(["say:1"]);
  q.reset({ keepSeen: true });
  assert.equal(q.enqueue({ key: "say:1", text: "A" }), "duplicate");
});

test("dispose：之后再入队全部忽略，计时器清空", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "say:1", text: "A" });
  q.dispose();
  assert.equal(q.isDisposed(), true);
  assert.equal(clock.pending(), 0);
  assert.equal(q.enqueue({ key: "say:2", text: "B" }), "disposed");
  clock.advance(10000);
  assert.equal(shown.length, 1);
});

test("setMaxShowMs：配置可收紧上限，但不得低于最短展示 1s", () => {
  const { q, clock, hidden } = makeQueue();
  q.setMaxShowMs(900);
  assert.equal(q.maxShowMs, 1000, "不得低于最短展示 1s");
  q.setMaxShowMs(2000);
  assert.equal(q.maxShowMs, 2000);
  q.enqueue({ key: "say:1", text: "A" });
  clock.advance(1999);
  assert.equal(hidden.length, 0, "未到配置上限不收起");
  clock.advance(1);
  assert.deepEqual(hidden.map((h) => h.key), ["say:1"], "按配置上限收起");
});